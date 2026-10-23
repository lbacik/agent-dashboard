"""SSE stream: a `fleet` event carrying the whole table after every cycle."""

import asyncio
import re
import time

import pytest
from fake_gateway import FakeGateway
from fastapi.testclient import TestClient

from agent_dashboard.app import SSE_RETRY_MS, create_app, fleet_event_stream, format_sse_event
from agent_dashboard.collector import run_cycle
from agent_dashboard.config import Settings
from agent_dashboard.fleet import FleetState
from agent_dashboard.gateway import ManagedInstance


def make_app(state, gateway=None):
    return create_app(
        state=state,
        settings=Settings(poll_interval=5.0),
        gateway=gateway or FakeGateway(),
        start_poller=False,
    )


def test_format_sse_event_prefixes_every_data_line():
    out = format_sse_event("fleet", "<table>\n<tr><td>x</td></tr>\n</table>")
    assert out.startswith("event: fleet\n")
    assert out.endswith("\n\n")
    for line in out.splitlines():
        assert line == "" or line.startswith(("event:", "data:"))
    assert "data: <tr><td>x</td></tr>" in out


def test_subscribers_wake_on_apply_cycle_and_discovery_error():
    async def scenario():
        state = FleetState()
        queue = state.subscribe()
        await state.apply_cycle([ManagedInstance("a", "id", "running")], {}, now=1000.0)
        assert await asyncio.wait_for(queue.get(), timeout=1) == 1
        await state.set_discovery_error("boom")
        assert await asyncio.wait_for(queue.get(), timeout=1) == 2
        state.unsubscribe(queue)
        await state.apply_cycle([], {}, now=1001.0)
        await asyncio.sleep(0)
        assert queue.empty()

    asyncio.run(scenario())


def _scope(path="/stream"):
    return {
        "type": "http",
        "http_version": "1.1",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"localhost")],
        "client": ("127.0.0.1", 5000),
        "server": ("127.0.0.1", 8000),
        "state": {},
    }


def _body(sent):
    return b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")


def _fleet_datas(sent):
    """Data payloads of the `fleet` events buffered so far."""
    datas = []
    for block in _body(sent).split(b"\n\n"):
        lines = block.split(b"\n")
        if not lines or lines[0] != b"event: fleet":
            continue
        payload = []
        for line in lines[1:]:
            assert line.startswith(b"data"), f"unexpected SSE line: {line!r}"
            text = line[len(b"data:") :]
            if text.startswith(b" "):
                text = text[1:]
            payload.append(text)
        datas.append(b"\n".join(payload))
    return datas


async def _wait_for(predicate, timeout=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("timed out waiting for a stream event")
        await asyncio.sleep(0.01)


def _render(rows, error):
    cells = ",".join(f"{r.name}:{r.reach}:{r.headline}" for r in rows)
    return f"<tbody>{cells}|{error}</tbody>"


def test_event_stream_emits_table_per_cycle_and_unsubscribes_on_disconnect():
    """Completing a cycle emits a `fleet` event with the updated row; a
    disconnect ends the stream and removes the subscriber."""

    async def scenario():
        state = FleetState()
        gateway = FakeGateway([ManagedInstance("worker-up", "id-up", "running")])
        gateway.set_snapshot("id-up", {"schema_version": 1})
        connected = True

        async def is_disconnected():
            return not connected

        stream = fleet_event_stream(state, _render, is_disconnected)
        it = stream.__aiter__()
        assert await asyncio.wait_for(it.__anext__(), timeout=2) == f"retry: {SSE_RETRY_MS}\n\n"

        # No cycle has run yet: the first fleet event shows the empty table.
        first = await asyncio.wait_for(it.__anext__(), timeout=2)
        assert first.startswith("event: fleet\n")
        assert b"worker-up" not in first.encode()

        # Completing a cycle emits a fleet event containing the updated row.
        pending = asyncio.create_task(it.__anext__())
        await asyncio.sleep(0.05)
        await run_cycle(state, gateway, now=time.time())
        event = await asyncio.wait_for(pending, timeout=2)
        assert event.startswith("event: fleet\n")
        assert "worker-up:live:Live: reporting telemetry" in event

        # A disconnect ends the stream and unsubscribes the client.
        assert len(state._subscribers) == 1
        connected = False
        pending = asyncio.create_task(it.__anext__())
        await asyncio.sleep(0.05)
        await run_cycle(state, gateway, now=time.time())
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(pending, timeout=2)
        assert len(state._subscribers) == 0
        await stream.aclose()

    asyncio.run(scenario())


def test_stream_serves_events_over_asgi_and_closes_cleanly_on_cancel():
    """End to end through the app: event-stream headers, retry, a fleet
    event per cycle, and subscriber cleanup when the stream task ends."""

    async def scenario():
        state = FleetState()
        gateway = FakeGateway([ManagedInstance("worker-up", "id-up", "running")])
        gateway.set_snapshot("id-up", {"schema_version": 1})
        app = make_app(state, gateway)

        sent: list[dict] = []

        async def receive():
            await asyncio.sleep(3600)
            return {"type": "http.disconnect"}  # pragma: no cover

        async def send(message):
            sent.append(message)

        task = asyncio.create_task(app(_scope(), receive, send))
        try:
            await _wait_for(lambda: len(_fleet_datas(sent)) >= 1)
            start = next(m for m in sent if m["type"] == "http.response.start")
            headers = {k.decode(): v.decode() for k, v in start["headers"]}
            assert start["status"] == 200
            assert "text/event-stream" in headers["content-type"]
            assert headers["cache-control"] == "no-cache"
            assert f"retry: {SSE_RETRY_MS}".encode() in _body(sent)

            before = len(_fleet_datas(sent))
            await run_cycle(state, gateway, now=time.time())
            await _wait_for(lambda: len(_fleet_datas(sent)) > before)

            latest = _fleet_datas(sent)[-1]
            assert b"worker-up" in latest
            assert b"Live: reporting telemetry" in latest

            # Ages are rendered at send time: a failed poll keeps the last
            # good snapshot visible with a fresh age in the next event.
            gateway.set_error_code("id-up", "agent_unreachable", "no socket")
            before = len(_fleet_datas(sent))
            await run_cycle(state, gateway, now=time.time())
            await _wait_for(lambda: len(_fleet_datas(sent)) > before)
            latest = _fleet_datas(sent)[-1]
            assert b"unreachable" in latest
            assert b"no socket" in latest
            match = re.search(rb"(\d+) s ago", latest)
            assert match is not None
            assert int(match.group(1)) <= 5
        finally:
            # Shutdown closes the stream: the subscriber is removed.
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            assert len(state._subscribers) == 0

    asyncio.run(scenario())


def test_index_uses_sse_not_polling():
    client = TestClient(make_app(FleetState()), base_url="http://localhost")
    body = client.get("/").text
    assert 'hx-ext="sse"' in body
    assert 'sse-connect="/stream"' in body
    assert 'sse-swap="fleet"' in body
    assert "/static/sse.js" in body
    assert 'hx-get="/fleet"' not in body
    # Nothing polls: the only trigger is the overview panel's one-shot load.
    assert 'hx-trigger="every' not in body
    assert body.count("hx-trigger") == 1
    assert 'hx-trigger="intersect once"' in body


def test_sse_extension_vendored_no_cdn():
    client = TestClient(make_app(FleetState()), base_url="http://localhost")
    body = client.get("/").text
    assert "cdn" not in body.lower()
    static = client.get("/static/sse.js")
    assert static.status_code == 200
    assert "defineExtension('sse'" in static.text
