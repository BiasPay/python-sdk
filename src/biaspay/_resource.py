"""Base classes for generated resource groups.

There are two parallel trees so each client gets precise types:

* ``SyncResource`` methods return values directly (used by ``Bias``).
* ``AsyncResource`` methods are coroutines that await the transport (used by
  ``AsyncBias``).

The generator emits a sync and an async subclass per resource group; both call
the helpers below.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Iterator, Mapping, Optional, Type, TypeVar

from pydantic import TypeAdapter

from ._pagination import AsyncPage, AsyncPaginator, SyncPage

T = TypeVar("T")


class _ResourceBase:
    def __init__(self, client: Any):
        self._client = client


class SyncResource(_ResourceBase):
    def _request(
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
    ) -> Any:
        return self._client.request(
            method,
            path,
            cast_to=cast_to,
            path_params=path_params,
            query=query,
            body=body,
            content_type=content_type,
            idempotency_key=idempotency_key,
        )

    def _request_page(
        self,
        method: str,
        path: str,
        *,
        item_type: Type[T],
        path_params: Optional[Mapping[str, Any]] = None,
        query: Optional[Mapping[str, Any]] = None,
    ) -> SyncPage[T]:
        page_cls = SyncPage[item_type]
        raw = self._client.request(method, path, cast_to=None, path_params=path_params, query=query)
        page = page_cls.model_validate(raw)
        base_query = dict(query or {})

        def fetch_next(cursor: str):
            next_query = {**base_query, "starting_after": cursor}
            nxt = self._client.request(
                method, path, cast_to=None, path_params=path_params, query=next_query
            )
            p = page_cls.model_validate(nxt)
            object.__setattr__(p, "_fetch_next", fetch_next)
            return p

        object.__setattr__(page, "_fetch_next", fetch_next)
        return page

    def _stream(
        self,
        method: str,
        path: str,
        *,
        item_type: Type[T],
        path_params: Optional[Mapping[str, Any]] = None,
        query: Optional[Mapping[str, Any]] = None,
    ) -> Iterator[T]:
        adapter = TypeAdapter(item_type)
        for record in self._client.stream_request(
            method, path, path_params=path_params, query=query
        ):
            yield adapter.validate_python(record)


class AsyncResource(_ResourceBase):
    async def _request(
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
    ) -> Any:
        return await self._client.request(
            method,
            path,
            cast_to=cast_to,
            path_params=path_params,
            query=query,
            body=body,
            content_type=content_type,
            idempotency_key=idempotency_key,
        )

    def _request_page(
        self,
        method: str,
        path: str,
        *,
        item_type: Type[T],
        path_params: Optional[Mapping[str, Any]] = None,
        query: Optional[Mapping[str, Any]] = None,
    ) -> AsyncPaginator[T]:
        page_cls = AsyncPage[item_type]
        return AsyncPaginator(
            lambda: self._fetch_first_page(page_cls, method, path, path_params, query)
        )

    async def _fetch_first_page(self, page_cls, method, path, path_params, query):
        raw = await self._client.request(
            method, path, cast_to=None, path_params=path_params, query=query
        )
        page = page_cls.model_validate(raw)
        base_query = dict(query or {})

        async def fetch_next(cursor: str):
            next_query = {**base_query, "starting_after": cursor}
            nxt = await self._client.request(
                method, path, cast_to=None, path_params=path_params, query=next_query
            )
            p = page_cls.model_validate(nxt)
            object.__setattr__(p, "_fetch_next", fetch_next)
            return p

        object.__setattr__(page, "_fetch_next", fetch_next)
        return page

    async def _stream(
        self,
        method: str,
        path: str,
        *,
        item_type: Type[T],
        path_params: Optional[Mapping[str, Any]] = None,
        query: Optional[Mapping[str, Any]] = None,
    ) -> AsyncIterator[T]:
        adapter = TypeAdapter(item_type)
        async for record in self._client.stream_request(
            method, path, path_params=path_params, query=query
        ):
            yield adapter.validate_python(record)
