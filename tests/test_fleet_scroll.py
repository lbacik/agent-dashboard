"""Fleet tables keep their horizontal scroll position across live refreshes.

Both `.table-scroll` boxes in the fleet fragment (running instances and the
collapsed "Not running" section) opt into the shared scroll keeper
(`scroll-keep.js`) with a stable key, so every `fleet` SSE re-render —
poll cycles and the fresh table sent when the stream reconnects —
reapplies the saved horizontal position. Positions live in page memory
only, so a reload starts both tables at the left edge.
"""

import asyncio
import re

from fake_gateway import FakeGateway
from test_web import make_client, make_live_client

from agent_dashboard.app import create_app
from agent_dashboard.config import Settings

RUNNING_KEY = '<div class="table-scroll" data-scroll-key="fleet-running">'
NOT_RUNNING_KEY = '<div class="table-scroll" data-scroll-key="fleet-not-running">'

SCROLL_BOX = re.compile(r'<div[^>]*class="table-scroll"[^>]*>')


def scroll_boxes(html):
    return SCROLL_BOX.findall(html)


def test_running_table_scroll_container_carries_stable_key():
    client, _ = make_client([("up", "running")])
    for path in ("/", "/fleet"):
        body = client.get(path).text
        assert RUNNING_KEY in body


def test_not_running_table_scroll_container_carries_stable_key():
    client, _ = make_client([("up", "running"), ("down", "not running")])
    for path in ("/", "/fleet"):
        body = client.get(path).text
        assert NOT_RUNNING_KEY in body
        _, _, details = body.partition('<details class="not-running"')
        assert "Not running (1)" in body
        assert NOT_RUNNING_KEY in details


def test_every_scroll_container_in_fragment_has_stable_key():
    """No `.table-scroll` box may lose its key in a future edit."""
    client, _ = make_client([("up", "running"), ("down", "not running")])
    for path in ("/", "/fleet"):
        boxes = scroll_boxes(client.get(path).text)
        assert len(boxes) == 2
        for box in boxes:
            assert 'data-scroll-key="' in box
        keys = sorted(re.search(r'data-scroll-key="([^"]+)"', b).group(1) for b in boxes)
        assert keys == ["fleet-not-running", "fleet-running"]


def test_stopped_only_fragment_still_keys_its_scroll_container():
    client, _ = make_client([("down", "not running")])
    body = client.get("/fleet").text
    assert "No running managed instances." in body
    boxes = scroll_boxes(body)
    assert len(boxes) == 1
    assert boxes[0] == NOT_RUNNING_KEY


def test_stream_reconnect_table_carries_scroll_keys():
    """The fresh table sent when the stream connects uses the same fragment."""
    client, state, _, _ = make_live_client()
    # make_live_client has running + stopped rows: both keys must be live.
    index_boxes = scroll_boxes(client.get("/").text)
    assert len(index_boxes) == 2
    partial = client.get("/fleet").text
    assert RUNNING_KEY in partial
    assert NOT_RUNNING_KEY in partial

    async def scenario():
        assert state is not None
        # Render through the app's own stream renderer: same template file
        # the reconnect event carries.
        app = create_app(
            state=state,
            settings=Settings(poll_interval=5.0),
            gateway=FakeGateway(),
            start_poller=False,
        )
        # The /fleet route and the stream share `_fleet_table.html`; capture
        # the first `fleet` event off the real ASGI stream to prove the
        # reconnect payload carries the same keys.
        sent = []

        async def receive():
            await asyncio.sleep(3600)
            return {"type": "http.disconnect"}  # pragma: no cover

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http",
            "http_version": "1.1",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "method": "GET",
            "scheme": "http",
            "path": "/stream",
            "raw_path": b"/stream",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"host", b"localhost")],
            "client": ("127.0.0.1", 5000),
            "server": ("127.0.0.1", 8000),
            "state": {},
        }
        task = asyncio.create_task(app(scope, receive, send))
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 5.0
            body = b""
            while loop.time() < deadline:
                body = b"".join(
                    m.get("body", b"") for m in sent if m["type"] == "http.response.body"
                )
                if b"event: fleet" in body:
                    break
                await asyncio.sleep(0.01)
            assert b"event: fleet" in body
            assert b'data-scroll-key="fleet-running"' in body
            assert b'data-scroll-key="fleet-not-running"' in body
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(scenario())


def test_scroll_keeper_restores_both_axes_without_moving_page():
    """The shared keeper reapplies both axes on keyed boxes only.

    It never calls a page-level scroll API, so re-rendering the table
    cannot move the page's own vertical position. The fleet swap itself
    is a plain innerHTML swap with no scroll/show directive.
    """
    client, _ = make_client([("up", "running"), ("down", "not running")])
    body = client.get("/").text
    assert "/static/scroll-keep.js" in body
    assert 'sse-swap="fleet"' in body
    assert 'hx-swap="innerHTML"' in body
    assert "hx-swap=\"innerHTML scroll" not in body
    assert "hx-swap=\"innerHTML show" not in body
    keeper = client.get("/static/scroll-keep.js").text
    assert "scrollLeft" in keeper
    assert "scrollTop" in keeper
    assert "htmx:beforeSwap" in keeper
    assert "htmx:afterSwap" in keeper
    for page_api in ("window.scrollTo", "scrollIntoView", "scrollIntoVeiw"):
        assert page_api not in keeper


def test_scroll_positions_are_memory_only():
    """Neither the keeper nor the fleet tables persist scroll in storage."""
    client, _ = make_client([("up", "running"), ("down", "not running")])
    keeper = client.get("/static/scroll-keep.js").text
    assert "localStorage" not in keeper
    assert "sessionStorage" not in keeper
    for path in ("/", "/fleet"):
        body = client.get(path).text
        for box in scroll_boxes(body):
            assert "localStorage" not in box
            assert "sessionStorage" not in box
