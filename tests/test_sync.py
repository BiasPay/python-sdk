import httpx
import pytest
import respx
from conftest import BASE_URL, customer_obj, event_obj, file_obj

from biaspay import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    Bias,
    BiasFile,
    InvalidRequestError,
    RateLimitError,
    ServerError,
)


def make_client(**kw):
    return Bias(api_key="sk_test", base_url=BASE_URL, **kw)


@respx.mock
def test_create_sends_auth_and_headers():
    route = respx.post(f"{BASE_URL}/v1/customers").mock(
        return_value=httpx.Response(201, json=customer_obj())
    )
    client = make_client()
    customer = client.customers.create(name="Acme", email="acme@example.com")

    assert customer.id == "cus_1"
    assert customer.email == "acme@example.com"

    req = route.calls.last.request
    assert req.headers["authorization"] == "Bearer sk_test"
    assert req.headers["api-version"] == "2025-07-01"
    assert "idempotency-key" in req.headers
    assert req.headers["content-type"] == "application/json"
    # None-valued fields are dropped from the body.
    import json

    body = json.loads(req.content)
    assert body == {"name": "Acme", "email": "acme@example.com"}


@respx.mock
def test_typed_dict_params_are_sent_as_given():
    import json

    from biaspay import MetadataInputParam, ShippingDetailsInputParam

    route = respx.post(f"{BASE_URL}/v1/customers").mock(
        return_value=httpx.Response(201, json=customer_obj())
    )
    shipping: ShippingDetailsInputParam = {"name": "Acme"}
    metadata: MetadataInputParam = {"plan": "pro", "legacy": None}
    make_client().customers.create(name="Acme", shipping_details=shipping, metadata=metadata)

    body = json.loads(route.calls.last.request.content)
    assert body["metadata"] == {"plan": "pro", "legacy": None}


def test_resource_attributes_are_snake_case():
    client = make_client()
    assert hasattr(client, "applied_discounts")
    assert not hasattr(client, "appliedDiscounts")


@respx.mock
def test_get_interpolates_path():
    respx.get(f"{BASE_URL}/v1/customers/cus_42").mock(
        return_value=httpx.Response(200, json=customer_obj(id="cus_42"))
    )
    customer = make_client().customers.get("cus_42")
    assert customer.id == "cus_42"


@respx.mock
def test_get_has_no_idempotency_key():
    route = respx.get(f"{BASE_URL}/v1/customers/cus_1").mock(
        return_value=httpx.Response(200, json=customer_obj())
    )
    make_client().customers.get("cus_1")
    assert "idempotency-key" not in route.calls.last.request.headers


@respx.mock
def test_invalid_request_error_maps_fields():
    respx.post(f"{BASE_URL}/v1/customers").mock(
        return_value=httpx.Response(
            400,
            headers={"request-id": "req_abc"},
            json={
                "object": "error",
                "error": {
                    "type": "invalid_request_error",
                    "code": "invalid_parameters",
                    "message": "Bad email",
                    "param": "email",
                    "live": False,
                },
            },
        )
    )
    with pytest.raises(InvalidRequestError) as exc:
        make_client().customers.create(email="nope")
    err = exc.value
    assert err.status_code == 400
    assert err.code == "invalid_parameters"
    assert err.param == "email"
    assert err.request_id == "req_abc"
    assert err.message == "Bad email"


@respx.mock
def test_rate_limit_retries_then_raises():
    route = respx.post(f"{BASE_URL}/v1/customers").mock(
        return_value=httpx.Response(
            429,
            json={"object": "error", "error": {"type": "rate_limit_error", "message": "slow down"}},
        )
    )
    with pytest.raises(RateLimitError):
        make_client(max_retries=2).customers.create(name="Acme")
    # initial attempt + 2 retries
    assert route.call_count == 3


@respx.mock
def test_server_error_retries_then_succeeds():
    route = respx.route(method="POST", url=f"{BASE_URL}/v1/customers").mock(
        side_effect=[
            httpx.Response(500, json={"object": "error", "error": {"type": "server_error"}}),
            httpx.Response(201, json=customer_obj()),
        ]
    )
    customer = make_client(max_retries=1).customers.create(name="Acme")
    assert customer.id == "cus_1"
    assert route.call_count == 2


@respx.mock
def test_replayed_server_error_is_not_retried():
    route = respx.post(f"{BASE_URL}/v1/customers").mock(
        return_value=httpx.Response(
            500,
            headers={"idempotency-replay": "true"},
            json={"object": "error", "error": {"type": "server_error"}},
        )
    )
    with pytest.raises(ServerError):
        make_client(max_retries=2).customers.create(name="Acme")
    assert route.call_count == 1


@respx.mock
def test_server_error_no_retries():
    respx.post(f"{BASE_URL}/v1/customers").mock(
        return_value=httpx.Response(
            500, json={"object": "error", "error": {"type": "server_error"}}
        )
    )
    with pytest.raises(ServerError):
        make_client(max_retries=0).customers.create(name="Acme")


@respx.mock
def test_list_single_page():
    respx.get(f"{BASE_URL}/v1/customers").mock(
        return_value=httpx.Response(
            200,
            json={"object": "list", "has_more": False, "items": [customer_obj("cus_1")]},
        )
    )
    page = make_client().customers.list(limit=10)
    assert page.has_more is False
    assert [c.id for c in page.data] == ["cus_1"]


@respx.mock
def test_auto_paging_follows_cursor():
    def handler(request):
        starting_after = httpx.QueryParams(request.url.query.decode()).get("starting_after")
        if starting_after is None:
            return httpx.Response(
                200,
                json={"object": "list", "has_more": True, "items": [customer_obj("cus_1")]},
            )
        assert starting_after == "cus_1"
        return httpx.Response(
            200,
            json={"object": "list", "has_more": False, "items": [customer_obj("cus_2")]},
        )

    respx.get(f"{BASE_URL}/v1/customers").mock(side_effect=handler)
    ids = [c.id for c in make_client().customers.list(limit=1).auto_paging_iter()]
    assert ids == ["cus_1", "cus_2"]


@respx.mock
def test_file_download():
    respx.get(f"{BASE_URL}/v1/customers/cus_1").mock(
        return_value=httpx.Response(
            200,
            headers={
                "content-type": "application/pdf",
                "content-disposition": 'attachment; filename="invoice.pdf"',
            },
            content=b"%PDF-1.4 fake",
        )
    )
    result = make_client().customers.get("cus_1")
    assert isinstance(result, BiasFile)
    assert result.filename == "invoice.pdf"
    assert result.content == b"%PDF-1.4 fake"


def test_missing_api_key_raises(monkeypatch):
    monkeypatch.delenv("BIAS_API_KEY", raising=False)
    from biaspay import BiasError

    with pytest.raises(BiasError):
        Bias(base_url=BASE_URL)


def test_api_key_from_env(monkeypatch):
    monkeypatch.setenv("BIAS_API_KEY", "sk_env")
    client = Bias(base_url=BASE_URL)
    assert client._config.api_key == "sk_env"


@respx.mock
def test_list_query_params_sent():
    route = respx.get(f"{BASE_URL}/v1/customers").mock(
        return_value=httpx.Response(200, json={"object": "list", "has_more": False, "items": []})
    )
    make_client().customers.list(limit=25)
    params = httpx.QueryParams(route.calls.last.request.url.query.decode())
    assert params["limit"] == "25"


@respx.mock
def test_authentication_error():
    respx.get(f"{BASE_URL}/v1/customers/cus_1").mock(
        return_value=httpx.Response(
            401, json={"object": "error", "error": {"type": "invalid_request_error"}}
        )
    )
    with pytest.raises(AuthenticationError):
        make_client().customers.get("cus_1")


@respx.mock
def test_multipart_upload_sends_file_part():
    route = respx.post(f"{BASE_URL}/v1/files").mock(
        return_value=httpx.Response(201, json=file_obj())
    )
    result = make_client().files.create(
        file=("notes.txt", b"contents", "text/plain"), purpose="invoice_attachment"
    )
    assert result.id == "file_1"
    req = route.calls.last.request
    assert req.headers["content-type"].startswith("multipart/form-data")
    body = req.read()
    assert b'name="file"; filename="notes.txt"' in body
    assert b'name="purpose"\r\n\r\ninvoice_attachment' in body
    assert "idempotency-key" in req.headers


@respx.mock
def test_transport_error_wrapped_as_connection_error():
    respx.post(f"{BASE_URL}/v1/customers").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(APIConnectionError):
        make_client(max_retries=0).customers.create(name="Acme")


@respx.mock
def test_timeout_wrapped_and_retried():
    route = respx.post(f"{BASE_URL}/v1/customers").mock(side_effect=httpx.ConnectTimeout("slow"))
    with pytest.raises(APITimeoutError):
        make_client(max_retries=1).customers.create(name="Acme")
    assert route.call_count == 2  # initial + 1 retry


@respx.mock
def test_context_manager_closes_owned_http():
    respx.get(f"{BASE_URL}/v1/customers/cus_1").mock(
        return_value=httpx.Response(200, json=customer_obj())
    )
    with make_client() as client:
        client.customers.get("cus_1")
        http = client._http
    assert http.is_closed


@respx.mock
def test_injected_http_client_not_closed():
    respx.get(f"{BASE_URL}/v1/customers/cus_1").mock(
        return_value=httpx.Response(200, json=customer_obj())
    )
    http = httpx.Client()
    client = Bias(api_key="sk_test", base_url=BASE_URL, http_client=http)
    client.customers.get("cus_1")
    client.close()
    assert not http.is_closed  # we don't own it
    http.close()


@respx.mock
def test_page_is_iterable():
    respx.get(f"{BASE_URL}/v1/customers").mock(
        return_value=httpx.Response(
            200,
            json={
                "object": "list",
                "has_more": False,
                "items": [customer_obj("cus_1"), customer_obj("cus_2")],
            },
        )
    )
    page = make_client().customers.list()
    assert [c.id for c in page] == ["cus_1", "cus_2"]


def _ndjson(*events):
    """Serialize events as the server does: one JSON object per line."""
    import json

    return ("".join(json.dumps(e) + "\n" for e in events)).encode("utf-8")


@respx.mock
def test_stream_yields_parsed_events():
    body = _ndjson(event_obj("evt_1", "customer.created"), event_obj("evt_2", "payment.succeeded"))
    respx.get(f"{BASE_URL}/v1/events").mock(return_value=httpx.Response(200, content=body))

    events = list(make_client().events.stream(limit=2))
    assert [e.id for e in events] == ["evt_1", "evt_2"]
    assert [e.type for e in events] == ["customer.created", "payment.succeeded"]
    # Each item is a parsed Event model, not a raw dict.
    assert events[0].state.id == "cus_1"


@respx.mock
def test_stream_skips_heartbeats_and_reassembles():
    # Heartbeats are bare spaces (no newline); a real event may arrive glued
    # to the trailing heartbeat space.
    import json

    body = (
        b" "  # initial heartbeat
        + (json.dumps(event_obj("evt_1")) + "\n").encode()
        + b" "  # periodic heartbeat between events
        + (json.dumps(event_obj("evt_2")) + "\n").encode()
    )
    respx.get(f"{BASE_URL}/v1/events").mock(return_value=httpx.Response(200, content=body))

    ids = [e.id for e in make_client().events.stream(limit=2)]
    assert ids == ["evt_1", "evt_2"]


@respx.mock
def test_stream_sends_stream_true_and_filters():
    route = respx.get(f"{BASE_URL}/v1/events").mock(
        return_value=httpx.Response(200, content=_ndjson(event_obj("evt_1")))
    )
    list(make_client().events.stream(limit=1, filters={"type": "customer.created"}))

    params = route.calls.last.request.url.params
    assert params["stream"] == "true"
    assert params["limit"] == "1"
    assert params["filters"] == '{"type":"customer.created"}'


@respx.mock
def test_stream_raises_on_error_status():
    respx.get(f"{BASE_URL}/v1/events").mock(
        return_value=httpx.Response(
            400,
            json={
                "object": "error",
                "error": {
                    "type": "invalid_request_error",
                    "code": "invalid_parameters",
                    "message": "stream cannot be combined with starting_after",
                    "live": False,
                },
            },
        )
    )
    with pytest.raises(InvalidRequestError):
        list(make_client().events.stream())
