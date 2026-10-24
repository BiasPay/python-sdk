import json
from datetime import datetime, timezone
from email.utils import format_datetime

import httpx
import pytest
import respx
from conftest import BASE_URL, error_body, event_obj

from biaspay import AsyncBias, AuthenticationError, Bias

EVENTS_URL = f"{BASE_URL}/v1/events"


@pytest.fixture(autouse=True)
def no_reconnect_delay(monkeypatch):
    monkeypatch.setattr("biaspay._client.reconnect_delay", lambda failures: 0)


def event(id, created_at):
    return {**event_obj(id), "created_at": created_at}


def ndjson(*events):
    return "".join(json.dumps(e) + "\n" for e in events).encode()


class DroppedStream(httpx.SyncByteStream, httpx.AsyncByteStream):
    """A stream body that yields ``chunk`` and then loses the connection."""

    def __init__(self, chunk):
        self._chunk = chunk

    def __iter__(self):
        yield self._chunk
        raise httpx.ReadTimeout("stalled")

    async def __aiter__(self):
        yield self._chunk
        raise httpx.ReadTimeout("stalled")


class FakeServer:
    """Answers stream requests and list requests from separate queues."""

    def __init__(self, streams, pages):
        self.streams = list(streams)
        self.pages = list(pages)
        self.stream_requests = []
        self.list_requests = []

    def __call__(self, request):
        if request.url.params.get("stream") == "true":
            self.stream_requests.append(request)
            return self.streams.pop(0)
        self.list_requests.append(request)
        return self.pages.pop(0)


def page(*items, has_more=False):
    return httpx.Response(200, json={"object": "list", "items": list(items), "has_more": has_more})


def reconnecting_server():
    return FakeServer(
        streams=[
            httpx.Response(200, stream=DroppedStream(ndjson(event("evt_1", 10_000)))),
            httpx.Response(200, content=ndjson(event("evt_3", 30_000), event("evt_4", 40_000))),
        ],
        pages=[page(event("evt_3", 30_000), event("evt_2", 20_000), event("evt_1", 10_000))],
    )


def assert_caught_up(server):
    assert len(server.stream_requests) == 2
    params = server.list_requests[0].url.params
    assert "stream" not in params
    assert params["limit"] == "100"
    assert params["filters"] == '{"type":"customer.created"}'


@respx.mock
def test_stream_reconnects_and_lists_what_it_missed():
    server = reconnecting_server()
    respx.get(EVENTS_URL).mock(side_effect=server)

    events = Bias(api_key="sk_test", base_url=BASE_URL).events.stream(
        limit=4, filters={"type": "customer.created"}
    )

    assert [e.id for e in events] == ["evt_1", "evt_2", "evt_3", "evt_4"]
    assert_caught_up(server)


@respx.mock
async def test_async_stream_reconnects_and_lists_what_it_missed():
    server = reconnecting_server()
    respx.get(EVENTS_URL).mock(side_effect=server)

    ids = []
    async with AsyncBias(api_key="sk_test", base_url=BASE_URL) as client:
        async for e in client.events.stream(limit=4, filters={"type": "customer.created"}):
            ids.append(e.id)

    assert ids == ["evt_1", "evt_2", "evt_3", "evt_4"]
    assert_caught_up(server)


@respx.mock
def test_stream_catches_up_from_when_it_connected():
    connected_at = 1_767_225_600_000
    date = format_datetime(datetime.fromtimestamp(connected_at / 1000, timezone.utc), usegmt=True)
    server = FakeServer(
        streams=[
            httpx.Response(200, content=b" ", headers={"date": date}),
            httpx.Response(200, content=b""),
        ],
        pages=[
            page(
                event("evt_4", connected_at + 4_000),
                event("evt_3", connected_at + 3_000),
                has_more=True,
            ),
            page(
                event("evt_2", connected_at + 2_000),
                event("evt_1", connected_at - 1_000),
                has_more=True,
            ),
        ],
    )
    respx.get(EVENTS_URL).mock(side_effect=server)

    events = Bias(api_key="sk_test", base_url=BASE_URL).events.stream(limit=3)

    assert [e.id for e in events] == ["evt_2", "evt_3", "evt_4"]
    cursors = [r.url.params.get("starting_after") for r in server.list_requests]
    assert cursors == [None, "evt_3"]


@respx.mock
def test_stream_retries_transient_failures_and_raises_on_the_rest():
    server = FakeServer(
        streams=[
            httpx.Response(200, content=ndjson(event("evt_1", 10_000))),
            httpx.Response(503, json=error_body("api_error")),
            httpx.Response(401, json=error_body("authentication_error", message="Invalid API key")),
        ],
        pages=[],
    )
    respx.get(EVENTS_URL).mock(side_effect=server)

    ids = []
    with pytest.raises(AuthenticationError, match="Invalid API key"):
        for e in Bias(api_key="sk_test", base_url=BASE_URL).events.stream():
            ids.append(e.id)

    assert ids == ["evt_1"]
    assert len(server.stream_requests) == 3


@respx.mock
def test_stream_times_out_reads_after_three_missed_heartbeats():
    route = respx.get(EVENTS_URL).mock(
        return_value=httpx.Response(200, content=ndjson(event("evt_1", 10_000)))
    )

    list(Bias(api_key="sk_test", base_url=BASE_URL, timeout=5).events.stream(limit=1))

    timeout = route.calls.last.request.extensions["timeout"]
    assert timeout["connect"] == 5
    assert timeout["read"] == 30
