"""The ``Bias`` (sync) and ``AsyncBias`` (async) clients.

Each client owns an ``httpx`` client, holds config, runs the retry loop, and
wires up the generated resource tree. The two share request-building via
``_transport``; only the actual send differs.
"""

from __future__ import annotations

import os
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncIterator,
    Iterator,
    Mapping,
    Optional,
    Type,
    TypeVar,
)

import httpx
from pydantic import BaseModel, TypeAdapter

from ._exceptions import BiasError

if TYPE_CHECKING:
    # Typed resource attributes (client.customers, ...) come from these mixins.
    # Guarded to avoid an import cycle at module load; the actual attributes are
    # populated at runtime by build_resources().
    from ._generated import _AsyncResources, _SyncResources
else:
    _SyncResources = object
    _AsyncResources = object
from ._transport import (
    DEFAULT_BASE_URL,
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT,
    TransportConfig,
    backoff_seconds,
    build_request,
    parse_response,
    parse_stream_line,
    raise_for_stream_status,
    should_retry,
    wrap_transport_error,
)

T = TypeVar("T")

_API_KEY_ENV = "BIAS_API_KEY"


class _BaseClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        base_url: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        default_headers: Optional[Mapping[str, str]] = None,
    ):
        key = api_key if api_key is not None else os.environ.get(_API_KEY_ENV)
        if not key:
            raise BiasError(
                "No API key provided. Pass api_key=... or set the "
                f"{_API_KEY_ENV} environment variable."
            )
        self._config = TransportConfig(
            api_key=key,
            base_url=base_url or DEFAULT_BASE_URL,
            timeout=timeout,
            max_retries=max_retries,
            default_headers=dict(default_headers or {}),
        )

    @staticmethod
    def _cast(cast_to: Optional[Type[T]], value: Any) -> Any:
        # Don't coerce binary downloads (BiasFile) or other non-JSON returns.
        if cast_to is None or not isinstance(value, (dict, list)):
            return value
        if isinstance(cast_to, type) and issubclass(cast_to, BaseModel):
            return cast_to.model_validate(value)
        return TypeAdapter(cast_to).validate_python(value)


class Bias(_BaseClient, _SyncResources):
    """Synchronous Bias API client.

    Usage::

        client = Bias(api_key="sk_...")
        customer = client.customers.create(name="Acme")
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        base_url: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        default_headers: Optional[Mapping[str, str]] = None,
        http_client: Optional[httpx.Client] = None,
    ):
        super().__init__(
            api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            default_headers=default_headers,
        )
        self._http = http_client or httpx.Client(timeout=timeout)
        self._owns_http = http_client is None
        _attach_resources(self)

    def request(
        self,
        method: str,
        path: str,
        *,
        cast_to: Optional[Type[T]] = None,
        path_params: Optional[Mapping[str, Any]] = None,
        query: Optional[Mapping[str, Any]] = None,
        body: Optional[Mapping[str, Any]] = None,
        content_type: str = "application/json",
        idempotency_key: Optional[str] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> Any:
        prepared = build_request(
            self._config,
            method,
            path,
            path_params=path_params,
            query=query,
            body=body,
            content_type=content_type,
            idempotency_key=idempotency_key,
            extra_headers=extra_headers,
        )
        attempt = 0
        while True:
            try:
                response = self._http.request(
                    prepared.method,
                    prepared.url,
                    headers=prepared.headers,
                    content=prepared.content,
                    files=prepared.files,
                )
            except httpx.HTTPError as exc:
                if attempt < self._config.max_retries:
                    _sleep(backoff_seconds(attempt, None))
                    attempt += 1
                    continue
                raise wrap_transport_error(exc) from exc

            if should_retry(response.status_code) and attempt < self._config.max_retries:
                _sleep(backoff_seconds(attempt, response.headers.get("retry-after")))
                attempt += 1
                continue
            return self._cast(cast_to, parse_response(response))

    def stream_request(
        self,
        method: str,
        path: str,
        *,
        path_params: Optional[Mapping[str, Any]] = None,
        query: Optional[Mapping[str, Any]] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> Iterator[Any]:
        """Open a newline-delimited JSON stream and yield each decoded record.

        The connection stays open for the lifetime of the generator; streams are
        not retried (re-issuing would restart the live feed from scratch).
        """
        prepared = build_request(
            self._config,
            method,
            path,
            path_params=path_params,
            query=query,
            extra_headers=extra_headers,
        )
        try:
            with self._http.stream(
                prepared.method, prepared.url, headers=prepared.headers
            ) as response:
                if response.status_code >= 300 or response.status_code < 200:
                    response.read()
                    raise_for_stream_status(response)
                for line in response.iter_lines():
                    record = parse_stream_line(line)
                    if record is not None:
                        yield record
        except httpx.HTTPError as exc:
            raise wrap_transport_error(exc) from exc

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> "Bias":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class AsyncBias(_BaseClient, _AsyncResources):
    """Asynchronous Bias API client.

    Usage::

        async with AsyncBias(api_key="sk_...") as client:
            customer = await client.customers.create(name="Acme")
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        base_url: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        default_headers: Optional[Mapping[str, str]] = None,
        http_client: Optional[httpx.AsyncClient] = None,
    ):
        super().__init__(
            api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            default_headers=default_headers,
        )
        self._http = http_client or httpx.AsyncClient(timeout=timeout)
        self._owns_http = http_client is None
        _attach_resources(self, is_async=True)

    async def request(
        self,
        method: str,
        path: str,
        *,
        cast_to: Optional[Type[T]] = None,
        path_params: Optional[Mapping[str, Any]] = None,
        query: Optional[Mapping[str, Any]] = None,
        body: Optional[Mapping[str, Any]] = None,
        content_type: str = "application/json",
        idempotency_key: Optional[str] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> Any:
        prepared = build_request(
            self._config,
            method,
            path,
            path_params=path_params,
            query=query,
            body=body,
            content_type=content_type,
            idempotency_key=idempotency_key,
            extra_headers=extra_headers,
        )
        attempt = 0
        while True:
            try:
                response = await self._http.request(
                    prepared.method,
                    prepared.url,
                    headers=prepared.headers,
                    content=prepared.content,
                    files=prepared.files,
                )
            except httpx.HTTPError as exc:
                if attempt < self._config.max_retries:
                    await _asleep(backoff_seconds(attempt, None))
                    attempt += 1
                    continue
                raise wrap_transport_error(exc) from exc

            if should_retry(response.status_code) and attempt < self._config.max_retries:
                await _asleep(backoff_seconds(attempt, response.headers.get("retry-after")))
                attempt += 1
                continue
            return self._cast(cast_to, parse_response(response))

    async def stream_request(
        self,
        method: str,
        path: str,
        *,
        path_params: Optional[Mapping[str, Any]] = None,
        query: Optional[Mapping[str, Any]] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> AsyncIterator[Any]:
        """Open a newline-delimited JSON stream and yield each decoded record.

        The connection stays open for the lifetime of the generator; streams are
        not retried (re-issuing would restart the live feed from scratch).
        """
        prepared = build_request(
            self._config,
            method,
            path,
            path_params=path_params,
            query=query,
            extra_headers=extra_headers,
        )
        try:
            async with self._http.stream(
                prepared.method, prepared.url, headers=prepared.headers
            ) as response:
                if response.status_code >= 300 or response.status_code < 200:
                    await response.aread()
                    raise_for_stream_status(response)
                async for line in response.aiter_lines():
                    record = parse_stream_line(line)
                    if record is not None:
                        yield record
        except httpx.HTTPError as exc:
            raise wrap_transport_error(exc) from exc

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> "AsyncBias":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()


def _attach_resources(client: Any, is_async: bool = False) -> None:
    """Instantiate and attach the generated resource tree onto the client.

    Imported lazily to avoid a circular import (``_generated`` imports the
    client base classes for type hints).
    """
    from ._generated import build_resources

    build_resources(client, is_async=is_async)


def _sleep(seconds: float) -> None:
    import time

    if seconds > 0:
        time.sleep(seconds)


async def _asleep(seconds: float) -> None:
    import asyncio

    if seconds > 0:
        await asyncio.sleep(seconds)
