"""Base aiohttp client class module."""

from __future__ import annotations

import asyncio
import io
import ssl
from abc import ABC, abstractmethod
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Any

import certifi
import msgspec
from aiohttp import BytesPayload, ClientSession, ClientTimeout, FormData, TCPConnector

from yatracker.exceptions import (
    AlreadyExistsError,
    NotAuthorizedError,
    ObjectNotFoundError,
    SufficientRightsError,
    YaTrackerError,
)

if TYPE_CHECKING:
    from aiohttp.typedefs import StrOrURL

DEFAULT_API_HOST = "https://api.tracker.yandex.net"
DEFAULT_API_VERSION = "v2"


class BaseClient(ABC):
    """Represents abstract base class for tracker client."""

    def __init__(
        self,
        org_id: str | int,
        token: str,
        headers: dict[str, str] | None = None,
        api_host: str | None = None,
        api_version: str | None = None,
        **kwargs,
    ) -> None:
        """Set defaults on object init.

        By default, `self._session` is None.
        It will be created on a first API request.
        The second request will use the same `self._session`.
        """
        self._api_version = api_version or DEFAULT_API_VERSION
        self._base_url = api_host or DEFAULT_API_HOST
        _headers = headers.copy() if headers else {}
        _headers.setdefault("X-Org-Id", str(org_id))
        _headers.setdefault("Authorization", f"OAuth {token}")
        self._headers: dict[str, str] = _headers
        self._session: ClientSession | None = None
        self._encoder = msgspec.json.Encoder()

    async def request(
        self,
        method: str,
        uri: str,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        **kwargs,
    ) -> dict[str, Any] | list[dict[str, Any]] | str:
        """Make request."""
        bytes_payload = BytesPayload(
            value=self._encoder.encode(payload),
            content_type="application/json",
        )
        status, body = await self._make_request(
            method=method,
            url=f"/{self._api_version}{uri}",
            params=params,
            data=bytes_payload,
            **kwargs,
        )
        self._check_status(status, body)
        return body

    @abstractmethod
    async def _make_request(
        self,
        method: str,
        url: StrOrURL,
        **kwargs,
    ) -> tuple[int, str]:
        """Get raw response from via http-client.
        :returns: tuple of (status_code, response_body).
        """

    @staticmethod
    def _check_status(status, text):
        if status < HTTPStatus.MULTIPLE_CHOICES:
            return

        if status == HTTPStatus.UNAUTHORIZED:
            raise NotAuthorizedError

        if status == HTTPStatus.FORBIDDEN:
            raise SufficientRightsError

        if status == HTTPStatus.NOT_FOUND:
            raise ObjectNotFoundError

        if status == HTTPStatus.CONFLICT:
            raise AlreadyExistsError

        raise YaTrackerError(text)

    async def close(self):
        """Close the session gracefully."""


class AIOHTTPClient(BaseClient):
    """Base aiohttp client.

    Consists of all methods need to make a request to API:
     - session caching
     - request wrapping
     - exceptions wrapping
     - grace session close
     - e.t.c.
    """

    def __init__(
        self,
        org_id: str | int,
        token: str,
        headers: dict[str, str] | None = None,
        api_host: str | None = None,
        api_version: str | None = None,
        **kwargs,
    ) -> None:
        """Set defaults on object init.

        By default, `self._session` is None.
        It will be created on a first API request.
        The second request will use the same `self._session`.
        """
        super().__init__(
            org_id=org_id,
            token=token,
            headers=headers,
            api_host=api_host,
            api_version=api_version,
            **kwargs,
        )
        self._timeout: ClientTimeout = kwargs.get("timeout") or ClientTimeout(total=0)

    def get_session(self):
        """Get cached session. One session per instance."""
        if isinstance(self._session, ClientSession) and not self._session.closed:
            return self._session

        ssl_context = ssl.create_default_context(cafile=certifi.where())
        connector = TCPConnector(ssl=ssl_context)

        encoder = msgspec.json.Encoder()
        self._session = ClientSession(
            base_url=self._base_url,
            connector=connector,
            headers=self._headers,
            json_serialize=lambda obj: encoder.encode(obj).decode(),
            timeout=self._timeout,
        )
        return self._session

    async def _make_request(
        self,
        method: str,
        url: StrOrURL,
        **kwargs,
    ) -> tuple[int, str]:
        """Make a request.

        :param method: HTTP Method
        :param url: endpoint link
        :param kwargs: data, params, json and other...
        :return: status and result or exception
        """
        session = self.get_session()

        async with session.request(method, url, **kwargs) as response:
            status = response.status
            text = await response.text()

        if status != 200:
            raise self._process_exception(status, text)

        return status, text

    def _prepare_form(self, file: str | Path | io.IOBase) -> FormData:
        """Create form to pass file via multipart/form-data."""
        form = FormData()
        form.add_field("file", self._prepare_file(file))
        return form

    @staticmethod
    def _prepare_file(file: str | Path | io.IOBase):
        """Prepare accepted types to correct file type."""
        if isinstance(file, str):
            return Path(file).open("rb")

        if isinstance(file, io.IOBase):
            return file

        if isinstance(file, Path):
            return file.open("rb")

        msg = f"Not supported file type: `{type(file).__name__}`"
        raise TypeError(msg)

    @staticmethod
    def _process_exception(
        status: int,
        data: dict[str, Any] | str,
    ) -> YaTrackerError:
        """Wrap API exceptions.

        :param status: response status
        :param data: response json converted to dict()
        :return: wrapped exception
        """
        if isinstance(data, dict):
            text = data.get("message") or data.get("detail")
        else:
            text = data
        return YaTrackerError(text)

    async def close(self):
        """Close the session gracefully."""
        if not isinstance(self._session, ClientSession):
            return

        if self._session.closed:
            return

        await self._session.close()
        await asyncio.sleep(0.25)
