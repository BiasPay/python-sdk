"""Unit tests for error-body -> exception mapping."""

from biaspay._exceptions import (
    AuthenticationError,
    IdempotencyError,
    InvalidRequestError,
    PaymentError,
    RateLimitError,
    ServerError,
    exception_from_response,
)


def _raise(status, body, headers=None):
    return exception_from_response(status, headers or {}, body)


def _err(type, **fields):
    return {"object": "error", "error": {"type": type, "message": "boom", **fields}}


# --- mapping by error.type --------------------------------------------------


def test_payment_error_type():
    exc = _raise(402, _err("payment_error", code="card_declined", param="card"))
    assert isinstance(exc, PaymentError)
    assert exc.code == "card_declined"
    assert exc.param == "card"


def test_customer_message_is_exposed_when_present():
    exc = _raise(402, _err("payment_error", customer_message="Your card was declined."))
    assert exc.message == "boom"
    assert exc.customer_message == "Your card was declined."
    assert _raise(500, _err("server_error", customer_message=None)).customer_message is None
    assert _raise(500, _err("server_error")).customer_message is None


def test_idempotency_error_type():
    exc = _raise(409, _err("idempotency_error"))
    assert isinstance(exc, IdempotencyError)


def test_rate_limit_error_type():
    assert isinstance(_raise(429, _err("rate_limit_error")), RateLimitError)


def test_server_error_type():
    assert isinstance(_raise(500, _err("server_error")), ServerError)


# --- authentication, the special case --------------------------------------


def test_401_status_maps_to_authentication_error():
    # Server reports a generic invalid_request_error, but 401 forces auth.
    exc = _raise(401, _err("invalid_request_error"))
    assert isinstance(exc, AuthenticationError)


def test_invalid_api_key_code_maps_to_authentication_error():
    # Even at 400, the invalid_api_key code is surfaced as auth.
    exc = _raise(400, _err("invalid_request_error", code="invalid_api_key"))
    assert isinstance(exc, AuthenticationError)


# --- fallback by status when error.type is missing --------------------------


def test_status_fallback_429():
    assert isinstance(_raise(429, {}), RateLimitError)


def test_status_fallback_409():
    assert isinstance(_raise(409, {}), IdempotencyError)


def test_status_fallback_403_is_auth():
    assert isinstance(_raise(403, {}), AuthenticationError)


def test_status_fallback_4xx_is_invalid_request():
    assert isinstance(_raise(422, {}), InvalidRequestError)


def test_status_fallback_5xx_is_server_error():
    assert isinstance(_raise(503, {}), ServerError)


# --- metadata extraction ----------------------------------------------------


def test_request_id_from_either_header():
    assert _raise(500, {}, {"request-id": "req_a"}).request_id == "req_a"
    assert _raise(500, {}, {"x-request-id": "req_b"}).request_id == "req_b"


def test_default_message_when_body_has_none():
    exc = _raise(503, {})
    assert exc.message == "HTTP 503"
    assert exc.status_code == 503
