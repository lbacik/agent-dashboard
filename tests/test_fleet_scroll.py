"""Fleet tables keep their horizontal scroll position across live refreshes.

Both `.table-scroll` boxes in the fleet fragment (running instances and the
collapsed "Not running" section) opt into the shared scroll keeper
(`scroll-keep.js`) with a stable key, so every `fleet` SSE re-render —
poll cycles and the fresh table sent when the stream reconnects —
reapplies the saved horizontal position. Positions live in page memory
only, so a reload starts both tables at the left edge.
"""

import re

from test_web import make_client, make_live_client

RUNNING_KEY = '<div class="table-scroll" data-scroll-key="fleet-running">'
NOT_RUNNING_KEY = '<div class="table-scroll" data-scroll-key="fleet-not-running">'

SCROLL_BOX = re.compile(r'<div[^>]*class="table-scroll"[^>]*>')


def scroll_boxes(html):
    return SCROLL_BOX.findall(html)


def assert_keyed_boxes(body, expected):
    """Every scroll container carries a stable key, exactly the expected set."""
    boxes = scroll_boxes(body)
    assert len(boxes) == len(expected)
    keys = sorted(re.search(r'data-scroll-key="([^"]+)"', box).group(1) for box in boxes)
    assert keys == sorted(expected)


def test_running_table_scroll_container_carries_stable_key():
    client, _ = make_client([("up", "running")])
    for path in ("/", "/fleet"):
        body = client.get(path).text
        assert RUNNING_KEY in body
        assert_keyed_boxes(body, ["fleet-running"])


def test_not_running_table_scroll_container_carries_stable_key():
    client, _ = make_client([("up", "running"), ("down", "not running")])
    for path in ("/", "/fleet"):
        body = client.get(path).text
        assert NOT_RUNNING_KEY in body
        _, _, details = body.partition('<details class="not-running"')
        assert "Not running (1)" in body
        assert NOT_RUNNING_KEY in details
        assert_keyed_boxes(body, ["fleet-running", "fleet-not-running"])


def test_not_running_only_fragment_still_keys_its_scroll_container():
    client, _ = make_client([("down", "not running")])
    body = client.get("/fleet").text
    assert "No running managed instances." in body
    assert_keyed_boxes(body, ["fleet-not-running"])


def test_stream_reconnect_table_carries_scroll_keys():
    """The fresh table sent when the stream connects uses the same fragment.

    The stream's `render_fleet` renders `_fleet_table.html` — the same
    template served at `/fleet` — so asserting both live fragments carry
    the identical key set covers the reconnect payload.
    """
    client, _, _, _ = make_live_client()
    index = client.get("/").text
    partial = client.get("/fleet").text
    assert_keyed_boxes(index, ["fleet-running", "fleet-not-running"])
    assert RUNNING_KEY in partial
    assert NOT_RUNNING_KEY in partial
    assert_keyed_boxes(partial, ["fleet-running", "fleet-not-running"])


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
    assert 'hx-swap="innerHTML scroll' not in body
    assert 'hx-swap="innerHTML show' not in body
    keeper = client.get("/static/scroll-keep.js").text
    assert "scrollLeft" in keeper
    assert "scrollTop" in keeper
    assert "htmx:beforeSwap" in keeper
    assert "htmx:afterSwap" in keeper
    for page_api in ("window.scrollTo", "scrollIntoView"):
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
