"""Exception hierarchy for the Bias SDK.

API errors are returned by the server as ``{"object": "error", "error": {...}}``
bodies with a 4xx/5xx status. The transport layer converts those into the
exceptions below via :func:`exception_from_response`, so callers can use normal
``try``/``except`` instead of inspecting return values.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

__all__ = [
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
    "exception_from_response",
]


class BiasError(Exception):
    """Base class for every error raised by the SDK."""


class APIConnectionError(BiasError):
    """Raised when the request could not reach the API (network failure)."""

    def __init__(self, message: str = "Connection error", *, cause: Optional[BaseException] = None):
        super().__init__(message)
        self.__cause__ = cause


class APITimeoutError(APIConnectionError):
    """Raised when a request exceeds the configured timeout."""

    def __init__(self, message: str = "Request timed out"):
        super().__init__(message)


class APIError(BiasError):
    """Base class for errors returned by the API with a structured body.

    Attributes mirror the ``error`` object in the response body plus a few
    transport-level details (HTTP status, request id). ``message`` is for
    developers; ``customer_message`` is safe to show a customer as is, or
    ``None`` when the API wrote none.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        type: Optional[str] = None,
        code: Optional[str] = None,
        param: Optional[str] = None,
        customer_message: Optional[str] = None,
        request_id: Optional[str] = None,
        live: Optional[bool] = None,
        body: Optional[Mapping[str, Any]] = None,
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.type = type
        self.code = code
        self.param = param
        self.customer_message = customer_message
        self.request_id = request_id
        self.live = live
        self.body = body


class InvalidRequestError(APIError):
    """The request was malformed or referenced a missing/invalid resource (HTTP 4xx)."""


class AuthenticationError(InvalidRequestError):
    """The API key was missing, invalid, or revoked (HTTP 401)."""


class PaymentError(APIError):
    """A payment could not be processed."""


class IdempotencyError(APIError):
    """An idempotency key was reused with a different request (HTTP 409)."""


class RateLimitError(APIError):
    """Too many requests were sent in a given window (HTTP 429)."""


class ServerError(APIError):
    """The API encountered an internal error (HTTP 5xx)."""


# Maps the server's ``error.type`` discriminator to a specific exception class.
_TYPE_TO_EXCEPTION = {
    "invalid_request_error": InvalidRequestError,
    "payment_error": PaymentError,
    "idempotency_error": IdempotencyError,
    "rate_limit_error": RateLimitError,
    "server_error": ServerError,
}


def exception_from_response(
    status_code: int,
    headers: Mapping[str, str],
    body: Any,
) -> APIError:
    """Build the most specific :class:`APIError` for a non-2xx response."""
    request_id = headers.get("request-id") or headers.get("x-request-id")

    error: Mapping[str, Any] = {}
    if isinstance(body, Mapping) and isinstance(body.get("error"), Mapping):
        error = body["error"]

    error_type = error.get("type")
    cls = _TYPE_TO_EXCEPTION.get(error_type) if error_type else None
    if cls is None:
        cls = _status_to_exception(status_code)

    # An invalid API key comes back as invalid_request_error/401; surface it as
    # the more specific AuthenticationError so callers can catch it on its own.
    code = error.get("code")
    if status_code == 401 or code == "invalid_api_key":
        cls = AuthenticationError

    message = error.get("message") or f"HTTP {status_code}"
    return cls(
        message,
        status_code=status_code,
        type=error_type,
        code=code,
        param=error.get("param"),
        customer_message=error.get("customer_message"),
        request_id=request_id,
        live=error.get("live"),
        body=body if isinstance(body, Mapping) else None,
    )


def _status_to_exception(status_code: int) -> type[APIError]:
    if status_code == 429:
        return RateLimitError
    if status_code == 409:
        return IdempotencyError
    if status_code in (401, 403):
        return AuthenticationError
    if 400 <= status_code < 500:
        return InvalidRequestError
    return ServerError
