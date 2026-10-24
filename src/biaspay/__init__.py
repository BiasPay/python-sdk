"""Bias API Python SDK.

from biaspay import Bias

client = Bias(api_key="sk_...")
customer = client.customers.create(name="Acme", email="a@b.com")
"""

from __future__ import annotations

from . import _generated

# Hand-written names below take precedence: several response schemas share a
# name with an exception class (ServerError, ...), and the exception is what
# callers should reach for. The raw error-body models stay available via
# ``biaspay._generated``.
from ._generated import *  # noqa: F403
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
from ._types import NOT_GIVEN, FileInput, NotGiven

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
    "NOT_GIVEN",
    "NotGiven",
    "FileInput",
    "__version__",
]

__all__ += _generated.__all__


def __dir__() -> list[str]:
    return sorted(set(__all__))
