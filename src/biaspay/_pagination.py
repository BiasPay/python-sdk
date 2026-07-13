"""Cursor pagination helpers.

List endpoints return ``{"object": "list", "has_more": bool, "items": [...]}``
and accept a ``starting_after`` cursor. ``SyncPage``/``AsyncPage`` wrap one page
and expose ``auto_paging_iter()`` to transparently walk every page.
"""

from __future__ import annotations

from typing import (
    TYPE_CHECKING,
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Generator,
    Generic,
    Iterator,
    List,
    TypeVar,
)

from pydantic import BaseModel, Field

if TYPE_CHECKING:  # pragma: no cover
    pass

T = TypeVar("T")


class SyncPage(BaseModel, Generic[T]):
    """A single page of list results from a synchronous client."""

    object: str = "list"
    has_more: bool = False
    items: List[T] = Field(default_factory=list)

    # Set by the resource layer so auto-paging can fetch the next page.
    _fetch_next: Callable[[str], "SyncPage[T]"] = None  # type: ignore[assignment]

    @property
    def data(self) -> List[T]:
        return self.items

    def auto_paging_iter(self) -> Iterator[T]:
        page: SyncPage[T] = self
        while True:
            for item in page.items:
                yield item
            if not page.has_more or not page.items or page._fetch_next is None:
                return
            cursor = _item_id(page.items[-1])
            if cursor is None:
                return
            page = page._fetch_next(cursor)

    def __iter__(self) -> Iterator[T]:  # type: ignore[override]
        return iter(self.items)


class AsyncPage(BaseModel, Generic[T]):
    """A single page of list results from an asynchronous client."""

    object: str = "list"
    has_more: bool = False
    items: List[T] = []

    _fetch_next: "Callable[[str], Awaitable[AsyncPage[T]]]" = None  # type: ignore[assignment]

    @property
    def data(self) -> List[T]:
        return self.items

    async def auto_paging_iter(self) -> AsyncIterator[T]:
        page: AsyncPage[T] = self
        while True:
            for item in page.items:
                yield item
            if not page.has_more or not page.items or page._fetch_next is None:
                return
            cursor = _item_id(page.items[-1])
            if cursor is None:
                return
            page = await page._fetch_next(cursor)


class AsyncPaginator(Generic[T]):
    """Return value of an async ``list()`` method.

    It is both awaitable — ``page = await client.x.list()`` resolves to an
    :class:`AsyncPage` — and directly iterable without awaiting:
    ``async for item in client.x.list().auto_paging_iter()`` fetches the first
    page lazily when iteration begins.
    """

    def __init__(self, fetch_first: Callable[[], Awaitable["AsyncPage[T]"]]):
        self._fetch_first = fetch_first

    def __await__(self) -> Generator[Any, None, "AsyncPage[T]"]:
        return self._fetch_first().__await__()

    async def auto_paging_iter(self) -> AsyncIterator[T]:
        page = await self._fetch_first()
        async for item in page.auto_paging_iter():
            yield item


def _item_id(item: Any) -> Any:
    if isinstance(item, BaseModel):
        return getattr(item, "id", None)
    if isinstance(item, dict):
        return item.get("id")
    return getattr(item, "id", None)
