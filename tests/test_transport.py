"""Unit tests for the pure transport helpers (no network, no respx)."""

import io
import json
from typing import Optional

import httpx
import pytest
from pydantic import BaseModel, field_serializer

from biaspay import NOT_GIVEN, NotGiven
from biaspay._exceptions import (
    APIConnectionError,
    APITimeoutError,
    InvalidRequestError,
    ServerError,
)
from biaspay._transport import (
    API_VERSION,
    MAX_BACKOFF_SECONDS,
    BiasFile,
    TransportConfig,
    backoff_seconds,
    build_request,
    parse_response,
    should_retry,
    wrap_transport_error,
)
from biaspay._types import _as_nullable


def _config(**kw):
    return TransportConfig(api_key="sk_test", base_url="https://api.test.biaspay.com", **kw)


# --- query encoding ---------------------------------------------------------


def test_query_drops_none_and_encodes_scalars():
    req = build_request(
        _config(),
        "GET",
        "/v1/customers",
        query={"limit": 10, "active": True, "skip": False, "missing": None},
    )
    params = httpx.QueryParams(req.url.split("?", 1)[1])
    assert params["limit"] == "10"
    assert params["active"] == "true"
    assert params["skip"] == "false"
    assert "missing" not in params


def test_query_json_encodes_nested_objects_and_arrays():
    req = build_request(
        _config(),
        "GET",
        "/v1/customers",
        query={"filter": {"status": "active"}, "ids": ["a", "b"]},
    )
    params = httpx.QueryParams(req.url.split("?", 1)[1])
    assert json.loads(params["filter"]) == {"status": "active"}
    assert json.loads(params["ids"]) == ["a", "b"]


# --- path interpolation -----------------------------------------------------


def test_missing_path_param_raises():
    with pytest.raises(ValueError, match="id"):
        build_request(_config(), "GET", "/v1/customers/{id}", path_params={})


def test_none_path_param_raises():
    with pytest.raises(ValueError, match="id"):
        build_request(_config(), "GET", "/v1/customers/{id}", path_params={"id": None})


# --- headers ----------------------------------------------------------------


def test_idempotency_key_only_on_mutations():
    post = build_request(_config(), "POST", "/v1/customers", body={"name": "Acme"})
    assert "idempotency-key" in post.headers
    get = build_request(_config(), "GET", "/v1/customers")
    assert "idempotency-key" not in get.headers


def test_explicit_idempotency_key_is_used():
    req = build_request(
        _config(), "POST", "/v1/customers", body={"name": "Acme"}, idempotency_key="key_123"
    )
    assert req.headers["idempotency-key"] == "key_123"


def test_base_and_extra_headers_merge_lowercased():
    req = build_request(
        _config(default_headers={"X-Base": "b"}),
        "GET",
        "/v1/customers",
        extra_headers={"X-Extra": "e"},
    )
    assert req.headers["x-base"] == "b"
    assert req.headers["x-extra"] == "e"
    assert req.headers["api-version"] == API_VERSION


# --- body encoding ----------------------------------------------------------


def test_json_body_drops_none_values():
    req = build_request(_config(), "POST", "/v1/customers", body={"name": "Acme", "email": None})
    assert json.loads(req.content) == {"name": "Acme"}
    assert req.headers["content-type"] == "application/json"
    assert req.files is None


def test_json_body_serializes_pydantic_model_nested_in_dict():
    from biaspay._generated import CardDetails, PaymentMethodCardCreate

    pm = PaymentMethodCardCreate(
        card=CardDetails(number="4242424242424242", exp_month=12, exp_year=2030, cvc="123")
    )
    req = build_request(
        _config(), "POST", "/v1/payments", body={"amount": 4200, "payment_method": pm}
    )
    assert json.loads(req.content) == {
        "amount": 4200,
        "payment_method": {
            "card": {
                "number": "4242424242424242",
                "exp_month": 12,
                "exp_year": 2030,
                "cvc": "123",
            }
        },
    }


def test_nullable_operation_value_omitted_value_and_null():
    omitted = build_request(
        _config(), "POST", "/v1/reports", body={"date_range": _as_nullable(NOT_GIVEN)}
    )
    value = build_request(
        _config(),
        "POST",
        "/v1/reports",
        body={"date_range": _as_nullable({"start": 1, "end": 2})},
    )
    null = build_request(_config(), "POST", "/v1/reports", body={"date_range": _as_nullable(None)})

    assert json.loads(omitted.content) == {}
    assert json.loads(value.content) == {"date_range": {"start": 1, "end": 2}}
    assert json.loads(null.content) == {"date_range": None}
    assert isinstance(NOT_GIVEN, NotGiven)


def test_explicit_null_marker_is_limited_to_json_bodies():
    query = build_request(_config(), "GET", "/v1/reports", query={"date_range": _as_nullable(None)})
    multipart = build_request(
        _config(),
        "POST",
        "/v1/files",
        body={"file": b"@data", "caption": _as_nullable(None)},
        content_type="multipart/form-data",
    )

    assert query.url.endswith("/v1/reports")
    assert "?" not in query.url
    assert multipart.files == {"file": b"@data"}
    assert multipart.data == {}


def test_nested_nullable_request_models_preserve_explicit_null_only():
    class DateRange(BaseModel):
        start: int
        end: Optional[int] = NOT_GIVEN  # type: ignore[assignment]

    class ReportParameters(BaseModel):
        date_range: Optional[DateRange] = NOT_GIVEN  # type: ignore[assignment]
        timezone: Optional[str] = None

    omitted = ReportParameters()
    value = ReportParameters(date_range=DateRange(start=1))
    null = ReportParameters(date_range=None)

    omitted_body = build_request(_config(), "POST", "/v1/reports", body={"params": omitted})
    value_body = build_request(_config(), "POST", "/v1/reports", body={"params": value})
    null_body = build_request(_config(), "POST", "/v1/reports", body={"params": null})

    assert json.loads(omitted_body.content) == {"params": {}}
    assert json.loads(value_body.content) == {"params": {"date_range": {"start": 1}}}
    assert json.loads(null_body.content) == {"params": {"date_range": None}}


def test_json_body_preserves_non_none_pydantic_defaults():
    class Options(BaseModel):
        enabled: bool = True
        retries: int = 3
        label: Optional[str] = None

        @field_serializer("retries")
        def serialize_retries(self, value: int) -> str:
            return f"retry-{value}"

    body = json.loads(
        build_request(_config(), "POST", "/v1/options", body={"options": Options()}).content
    )
    assert body == {"options": {"enabled": True, "retries": "retry-3"}}


def test_json_body_serializes_list_of_pydantic_models():
    from biaspay._generated import CardDetails

    cards = [
        CardDetails(number="4242424242424242", exp_month=1, exp_year=2030, cvc="123"),
        CardDetails(number="4000000000000002", exp_month=2, exp_year=2031, cvc="456"),
    ]
    req = build_request(_config(), "POST", "/v1/payments", body={"cards": cards})
    body = json.loads(req.content)
    assert [c["number"] for c in body["cards"]] == ["4242424242424242", "4000000000000002"]


def test_json_body_model_uses_wire_aliases_and_drops_none():
    # PaymentMethodCardCreate has optional fields (billing_details, setup_future_usage)
    # that default to None — they must not appear in the serialized body.
    from biaspay._generated import CardDetails, PaymentMethodCardCreate

    pm = PaymentMethodCardCreate(
        card=CardDetails(number="4242424242424242", exp_month=12, exp_year=2030, cvc="123")
    )
    body = json.loads(build_request(_config(), "POST", "/v1/payments", body={"pm": pm}).content)
    assert body["pm"] == {"card": pm.card.model_dump()}  # only the set field survives


def test_multipart_body_uses_files_not_content():
    req = build_request(
        _config(),
        "POST",
        "/v1/files",
        body={"file": b"@data", "purpose": None},
        content_type="multipart/form-data",
    )
    assert req.content is None
    assert req.files == {"file": b"@data"}  # None-valued purpose dropped
    # httpx sets the multipart content-type/boundary itself, so we must not.
    assert "content-type" not in req.headers


# --- retry / backoff --------------------------------------------------------


def test_should_retry_only_429_and_5xx():
    assert should_retry(httpx.Response(429))
    assert should_retry(httpx.Response(500))
    assert should_retry(httpx.Response(503))
    assert not should_retry(httpx.Response(400))
    assert not should_retry(httpx.Response(200))
    assert not should_retry(httpx.Response(404))


def test_should_not_retry_idempotent_replay():
    assert not should_retry(httpx.Response(500, headers={"idempotency-replay": "true"}))


def test_backoff_honors_retry_after_header():
    assert backoff_seconds(0, "2") == 2.0
    # capped at the ceiling
    assert backoff_seconds(0, "999") == MAX_BACKOFF_SECONDS


def test_backoff_ignores_bad_retry_after_and_grows():
    # non-numeric Retry-After falls back to exponential backoff
    assert backoff_seconds(0, "soon") == pytest.approx(0.01)
    assert backoff_seconds(3, None) == pytest.approx(0.08)
    # exponential growth is capped
    assert backoff_seconds(20, None) == MAX_BACKOFF_SECONDS


# --- response parsing -------------------------------------------------------


def test_parse_json_success():
    resp = httpx.Response(200, json={"hello": "world"})
    assert parse_response(resp) == {"hello": "world"}


def test_parse_binary_download_extracts_filename():
    resp = httpx.Response(
        200,
        headers={
            "content-type": "application/pdf",
            "content-disposition": 'attachment; filename="invoice.pdf"',
        },
        content=b"%PDF",
    )
    result = parse_response(resp)
    assert isinstance(result, BiasFile)
    assert result.filename == "invoice.pdf"
    assert result.content == b"%PDF"
    assert result.content_type == "application/pdf"


def test_parse_error_with_structured_body_raises_mapped():
    resp = httpx.Response(400, json={"object": "error", "error": {"type": "invalid_request_error"}})
    with pytest.raises(InvalidRequestError):
        parse_response(resp)


def test_parse_error_with_non_json_body_falls_back():
    resp = httpx.Response(500, headers={"content-type": "text/html"}, text="<h1>oops</h1>")
    with pytest.raises(ServerError) as exc:
        parse_response(resp)
    # the raw text is surfaced as the message
    assert "oops" in exc.value.message


# --- transport error wrapping -----------------------------------------------


def test_wrap_timeout():
    wrapped = wrap_transport_error(httpx.ConnectTimeout("timed out"))
    assert isinstance(wrapped, APITimeoutError)


def test_wrap_connection_error_keeps_cause():
    original = httpx.ConnectError("refused")
    wrapped = wrap_transport_error(original)
    assert isinstance(wrapped, APIConnectionError)
    assert not isinstance(wrapped, APITimeoutError)
    assert wrapped.__cause__ is original


def test_multipart_sends_only_binary_values_as_file_parts():
    upload = ("receipt.pdf", io.BytesIO(b"%PDF"), "application/pdf")
    req = build_request(
        _config(),
        "POST",
        "/v1/files",
        body={"file": upload, "purpose": "dispute_evidence", "public": True},
        content_type="multipart/form-data",
    )
    assert req.files == {"file": upload}
    assert req.data == {"purpose": "dispute_evidence", "public": "true"}


def test_nested_dict_params_keep_explicit_null():
    req = build_request(
        _config(),
        "POST",
        "/v1/customers",
        body={"name": None, "metadata": {"keep": "yes", "drop": None}},
    )
    assert json.loads(req.content) == {"metadata": {"keep": "yes", "drop": None}}


def test_query_encodes_dicts_and_models_as_json():
    from biaspay._generated import PaymentExpand

    as_dict = build_request(
        _config(), "GET", "/v1/payments/p", query={"expand": {"customer": True}}
    )
    as_model = build_request(
        _config(), "GET", "/v1/payments/p", query={"expand": PaymentExpand(customer=True)}
    )
    expected = "expand=%7B%22customer%22%3Atrue%7D"
    assert as_dict.url.endswith(expected)
    assert as_model.url.endswith(expected)
