"""Shared request option types used by generated SDK methods and models."""

from __future__ import annotations

from typing import IO, Any, Optional, Tuple, Union

FileContent = Union[IO[bytes], bytes]
FileInput = Union[
    FileContent,
    Tuple[Optional[str], FileContent],
    Tuple[Optional[str], FileContent, Optional[str]],
]
"""A multipart file: raw bytes, a binary file object, or ``(filename, content[, content_type])``."""


class NotGiven:
    """Type of the singleton used to distinguish omission from an explicit ``None``."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "NOT_GIVEN"


class _ExplicitNull:
    __slots__ = ()


NOT_GIVEN = NotGiven()
_EXPLICIT_NULL = _ExplicitNull()


def _as_nullable(value: Any) -> Any:
    """Mark an explicit ``None`` so legacy ordinary-None omission remains unchanged."""
    if value is None:
        return _EXPLICIT_NULL
    return value
