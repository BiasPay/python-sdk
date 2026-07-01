"""Bias API Python SDK.

from biaspay import Bias

client = Bias(api_key="sk_...")
customer = client.customers.create(name="Acme", email="a@b.com")
"""

from __future__ import annotations

from typing import Any

from . import _generated
from ._client import AsyncBias, Bias
from ._exceptions import (
    APIConnectionError,
    APIError,
    APITimeoutError,
    AuthenticationError,
    BiasError,
    IdempotencyError,
    InvalidRequestError,
    PaymentError,
    RateLimitError,
    ServerError,
)
from ._pagination import AsyncPage, SyncPage
from ._transport import BiasFile

__version__ = "1.0.0"

__all__ = [
    "Bias",
    "AsyncBias",
    "BiasError",
    "APIError",
    "APIConnectionError",
    "APITimeoutError",
    "InvalidRequestError",
    "AuthenticationError",
    "PaymentError",
    "IdempotencyError",
    "RateLimitError",
    "ServerError",
    "SyncPage",
    "AsyncPage",
    "BiasFile",
    "__version__",
]

# Generated model classes are exposed lazily at the top level (e.g.
# ``biaspay.Customer``). Hand-written names above take precedence — several
# response schemas share a name with an exception class (ServerError, ...), and
# the exception is what callers should reach for; the raw error-body models stay
# available via ``biaspay._generated``.
_reserved = set(__all__)
_generated_names = [
    name
    for name in getattr(_generated, "__all__", [])
    if name not in _reserved and isinstance(getattr(_generated, name, None), type)
]
__all__ += _generated_names  # pyright: ignore[reportUnsupportedDunderAll]


def __getattr__(name: str) -> Any:
    if name in _generated_names:
        return getattr(_generated, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
