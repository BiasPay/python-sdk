import httpx
import pytest
import respx
from conftest import BASE_URL, customer_obj, event_obj

from biaspay import (
    APIConnectionError,
    AsyncBias,
    BiasFile,
    InvalidRequestError,
    RateLimitError,
)


def make_client(**kw):
    return AsyncBias(api_key="sk_test", base_url=BASE_URL, **kw)


@respx.mock
async def test_async_create():
    route = respx.post(f"{BASE_URL}/v1/customers").mock(
        return_value=httpx.Response(201, json=customer_obj())
    )
    async with make_client() as client:
        customer = await client.customers.create(name="Acme")
    assert customer.id == "cus_1"
    assert route.calls.last.request.headers["authorization"] == "Bearer sk_test"


@respx.mock
async def test_async_error_mapping():
    respx.post(f"{BASE_URL}/v1/customers").mock(
        return_value=httpx.Response(
            400,
            json={
                "object": "error",
                "error": {
                    "type": "invalid_request_error",
                    "code": "invalid_parameters",
                    "message": "bad",
                    "param": "email",
                },
            },
        )
    )
    async with make_client() as client:
        with pytest.raises(InvalidRequestError) as exc:
            await client.customers.create(email="nope")
    assert exc.value.param == "email"


@respx.mock
async def test_async_rate_limit_retries():
    route = respx.post(f"{BASE_URL}/v1/customers").mock(
        return_value=httpx.Response(
            429,
            json={"object": "error", "error": {"type": "rate_limit_error"}},
        )
    )
    async with make_client(max_retries=2) as client:
        with pytest.raises(RateLimitError):
            await client.customers.create(name="Acme")
    assert route.call_count == 3


@respx.mock
async def test_async_auto_paging():
    def handler(request):
        starting_after = httpx.QueryParams(request.url.query.decode()).get("starting_after")
        if starting_after is None:
            return httpx.Response(
                200, json={"object": "list", "has_more": True, "items": [customer_obj("cus_1")]}
            )
        return httpx.Response(
            200, json={"object": "list", "has_more": False, "items": [customer_obj("cus_2")]}
        )

    respx.get(f"{BASE_URL}/v1/customers").mock(side_effect=handler)
    ids = []
    async with make_client() as client:
        async for c in client.customers.list(limit=1).auto_paging_iter():
            ids.append(c.id)
    assert ids == ["cus_1", "cus_2"]


@respx.mock
async def test_async_await_list_returns_page():
    respx.get(f"{BASE_URL}/v1/customers").mock(
        return_value=httpx.Response(
            200,
            json={"object": "list", "has_more": False, "items": [customer_obj("cus_1")]},
        )
    )
    async with make_client() as client:
        page = await client.customers.list(limit=10)
    assert page.has_more is False
    assert [c.id for c in page.data] == ["cus_1"]


@respx.mock
async def test_async_server_error_retries_then_succeeds():
    route = respx.route(method="POST", url=f"{BASE_URL}/v1/customers").mock(
        side_effect=[
            httpx.Response(500, json={"object": "error", "error": {"type": "server_error"}}),
            httpx.Response(201, json=customer_obj()),
        ]
    )
    async with make_client(max_retries=1) as client:
        customer = await client.customers.create(name="Acme")
    assert customer.id == "cus_1"
    assert route.call_count == 2


@respx.mock
async def test_async_file_download():
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
    async with make_client() as client:
        result = await client.customers.get("cus_1")
    assert isinstance(result, BiasFile)
    assert result.filename == "invoice.pdf"


@respx.mock
async def test_async_connection_error_retries_then_wraps():
    route = respx.post(f"{BASE_URL}/v1/customers").mock(side_effect=httpx.ConnectError("refused"))
    async with make_client(max_retries=1) as client:
        with pytest.raises(APIConnectionError):
            await client.customers.create(name="Acme")
    assert route.call_count == 2  # initial + 1 retry


@respx.mock
async def test_async_aclose_closes_owned_http():
    client = make_client()
    http = client._http
    await client.aclose()
    assert http.is_closed


def _ndjson(*events):
    import json

    return ("".join(json.dumps(e) + "\n" for e in events)).encode("utf-8")


@respx.mock
async def test_async_stream_yields_parsed_events():
    body = _ndjson(event_obj("evt_1", "customer.created"), event_obj("evt_2", "payment.succeeded"))
    respx.get(f"{BASE_URL}/v1/events").mock(return_value=httpx.Response(200, content=body))

    ids = []
    async with make_client() as client:
        async for event in client.events.stream(limit=2):
            ids.append(event.id)
    assert ids == ["evt_1", "evt_2"]


@respx.mock
async def test_async_stream_skips_heartbeats():
    import json

    body = (
        b" "
        + (json.dumps(event_obj("evt_1")) + "\n").encode()
        + b" "
        + (json.dumps(event_obj("evt_2")) + "\n").encode()
    )
    respx.get(f"{BASE_URL}/v1/events").mock(return_value=httpx.Response(200, content=body))

    ids = []
    async with make_client() as client:
        async for event in client.events.stream():
            ids.append(event.id)
    assert ids == ["evt_1", "evt_2"]


@respx.mock
async def test_async_stream_raises_on_error_status():
    respx.get(f"{BASE_URL}/v1/events").mock(
        return_value=httpx.Response(
            400,
            json={
                "object": "error",
                "error": {
                    "type": "invalid_request_error",
                    "code": "invalid_parameters",
                    "message": "bad stream params",
                    "live": False,
                },
            },
        )
    )
    with pytest.raises(InvalidRequestError):
        async with make_client() as client:
            async for _ in client.events.stream():
                pass
