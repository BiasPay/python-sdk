"""Bookkeeping for event streams that reconnect after they drop.

The sync and async clients run the same loop: open the stream, list what was
missed while it was down, then read the live stream, skipping anything already
yielded. Only the I/O differs, so the state lives here.
"""

from __future__ import annotations

import random
import time
from collections import OrderedDict
from email.utils import parsedate_to_datetime
from typing import Any, List, Mapping, Optional

import httpx

from ._exceptions import APIConnectionError, APIError, BiasError

# Three missed heartbeats (the server sends one every 10 seconds).
IDLE_TIMEOUT_SECONDS = 30.0
MAX_RECONNECT_DELAY_SECONDS = 30.0
# How far before the last yielded record a catch-up looks, for records that committed after it.
CATCH_UP_OVERLAP_MS = 5_000
CATCH_UP_PAGE_SIZE = 100
SEEN_LIMIT = 1_000


def stream_timeout(timeout: float) -> httpx.Timeout:
    return httpx.Timeout(timeout, read=IDLE_TIMEOUT_SECONDS)


def server_time_ms(response: httpx.Response) -> int:
    date = response.headers.get("date")
    if date:
        try:
            return int(parsedate_to_datetime(date).timestamp() * 1000)
        except (TypeError, ValueError):
            pass
    return int(time.time() * 1000)


def reconnect_delay(failures: int) -> float:
    delay = min(2.0**failures, MAX_RECONNECT_DELAY_SECONDS)
    return delay * random.uniform(0.5, 1.0)


def is_retryable(error: BiasError) -> bool:
    if isinstance(error, APIConnectionError):
        return True
    status = error.status_code if isinstance(error, APIError) else None
    return status is not None and (status in (408, 429) or status >= 500)


class StreamProgress:
    """What a stream has yielded, so a reconnect can resume after it."""

    def __init__(self, query: Mapping[str, Any], connected_at: int):
        self._query = query
        self._connected_at = connected_at
        self._seen: "OrderedDict[str, None]" = OrderedDict()
        self._last_created_at: Optional[int] = None
        self._delivered = 0
        self._limit = query.get("limit")

    def accept(self, record: Any) -> bool:
        """Record ``record`` as yielded, unless it already was."""
        record_id = record["id"]
        if record_id in self._seen:
            return False
        self._seen[record_id] = None
        if len(self._seen) > SEEN_LIMIT:
            self._seen.popitem(last=False)
        created_at = record["created_at"]
        if self._last_created_at is None or created_at > self._last_created_at:
            self._last_created_at = created_at
        self._delivered += 1
        return True

    @property
    def done(self) -> bool:
        return self._limit is not None and self._delivered >= self._limit

    def catch_up_query(self, starting_after: Optional[str]) -> dict[str, Any]:
        return {
            **self._query,
            "stream": None,
            "ending_before": None,
            "limit": CATCH_UP_PAGE_SIZE,
            "starting_after": starting_after,
        }

    def take_page(self, page: Any, missed: List[Any]) -> Optional[str]:
        """Add a newest-first page's missed records to ``missed``.

        Returns the cursor for the next page, or ``None`` once caught up.
        """
        floor = (
            self._connected_at
            if self._last_created_at is None
            else self._last_created_at - CATCH_UP_OVERLAP_MS
        )
        items = page["items"]
        for item in items:
            if item["created_at"] < floor:
                return None
            missed.append(item)
        if not page["has_more"] or not items:
            return None
        return items[-1]["id"]
