"""The Fleet Matrix keeps its horizontal scroll position across live refreshes.

The single `.table-scroll` box in the fleet fragment opts into the shared
scroll keeper (`scroll-keep.js`) with a stable key (`fleet-running`), so every
`fleet` SSE re-render — poll cycles and the fresh table sent when the stream
reconnects — reapplies the saved horizontal position. Positions live in page
memory only, so a reload starts the table at the left edge. There is no
"Not running" section and no `fleet-not-running` key anymore.
"""

import re

from test_web import make_client, make_live_client

RUNNING_KEY = '<div class="table-scroll" data-scroll-key="fleet-running">'

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


def test_stopped_rows_share_the_single_scroll_container():
    client, _ = make_client([("up", "running"), ("down", "not running")])
    for path in ("/", "/fleet"):
        body = client.get(path).text
        assert RUNNING_KEY in body
        assert "data-scroll-key=\"fleet-not-running\"" not in body
        assert '<details class="not-running"' not in body
        assert_keyed_boxes(body, ["fleet-running"])


def test_stopped_only_fragment_still_keys_its_scroll_container():
    client, _ = make_client([("down", "not running")])
    body = client.get("/fleet").text
    assert "No running managed instances." in body
    assert RUNNING_KEY in body
    assert_keyed_boxes(body, ["fleet-running"])


def test_stream_reconnect_table_carries_scroll_keys():
    """The fresh table sent when the stream connects uses the same fragment.

    The stream's `render_fleet` renders `_fleet_table.html` — the same
    template served at `/fleet` — so asserting both live fragments carry
    the identical key set covers the reconnect payload.
    """
    client, _, _, _ = make_live_client()
    index = client.get("/").text
    partial = client.get("/fleet").text
    assert_keyed_boxes(index, ["fleet-running"])
    assert RUNNING_KEY in partial
    assert_keyed_boxes(partial, ["fleet-running"])


def test_scroll_keeper_restores_both_axes_and_holds_the_page_still():
    """The shared keeper reapplies both axes on keyed boxes and the page.

    The Fleet Matrix and the cockpit scroll with the window, and a swap can
    briefly shorten the page (the fresh fleet HTML carries the default
    filter set until `fleet.js` reapplies the active one), which clamps the
    window towards the top. The keeper records the window position with the
    keyed boxes and puts it back in a microtask, after every other
    `htmx:afterSwap` listener ran. It never scrolls anything into view, and
    the fleet swap itself is a plain innerHTML swap with no scroll/show
    directive.
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
    assert "window.scrollY" in keeper
    assert "window.scrollTo(page.left, page.top)" in keeper
    assert "queueMicrotask" in keeper
    assert "scrollIntoView" not in keeper
    # The keeper loads before fleet.js, so its own afterSwap listener runs
    # first; only the deferred restore sees the reapplied filters.
    assert body.index("/static/scroll-keep.js") < body.index("/static/fleet.js")


def test_keeper_records_positions_before_sse_swaps():
    """The `fleet` table arrives over SSE, which swaps through `htmx.swap`.

    htmx fires `htmx:beforeSwap` only for AJAX responses, so the keeper
    must also record positions on the SSE extension's
    `htmx:sseBeforeMessage`, which fires right before the SSE swap.
    """
    client, _ = make_client([("up", "running")])
    keeper = client.get("/static/scroll-keep.js").text
    assert "htmx:sseBeforeMessage" in keeper
    sse = client.get("/static/sse.js").text
    assert "'htmx:sseBeforeMessage'" in sse


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
