"""Activity widget keeps its scroll position across poll cycles.

The drawer's `.activity-body` (fixed height, `overflow-y: auto`) reloads
on every poll cycle (`sse:fleet`), and the call/response `pre.activity-io`
panes inside it (capped height, `overflow-y: auto`) are re-rendered with
it. Each opts into the shared scroll keeper (`scroll-keep.js`) with a
key: the body uses the stable key `activity-body`, while each pane keys
per call (`activity-now-<file>`, `activity-last-call-<file>`,
`activity-last-response-<file>`), so a pane holds still while it shows
the same call and starts at the top when a new call replaces it (the old
key is simply absent after the swap). Positions live in page memory
only, so a reload or reopening the drawer starts every pane at the top.
"""

from __future__ import annotations

import json
import re

from test_activity import (
    BASH_CALL,
    BASH_RESULT,
    MODEL_TXT,
    _ev,
    _file,
    _payload,
    make_activity_client,
)
from test_web import load_snapshot

BODY_KEY = '<div class="activity-body" data-scroll-key="activity-body"'

PANE = re.compile(r'<pre[^>]*class="activity-io[^"]*"[^>]*>')
KEY = re.compile(r'data-scroll-key="([^"]+)"')


def pane_keys(html):
    """Every call/response pane carries a key identifying its call."""
    panes = PANE.findall(html)
    assert panes, "expected call/response panes in the activity fragment"
    keys = []
    for pane in panes:
        match = KEY.search(pane)
        assert match, f"scroll container without a stable key: {pane}"
        keys.append(match.group(1))
    return keys


def running_client(events, files):
    """An activity client whose tail holds `events` with `files` readable."""
    snapshot = load_snapshot("snapshot-attempt-in-progress.json")
    return make_activity_client(snapshot, _payload(events), files)


def tool_and_last_call_events():
    """A running Read call plus a paired Bash last call."""
    return [
        _ev(
            "tool_call",
            "Bash [main] -> 0001_tool_call.json",
            timestamp="2026-09-27T16:34:00Z",
        ),
        _ev(
            "tool_result",
            "Bash [main] -> 0002_tool_result.json",
            timestamp="2026-09-27T16:34:09Z",
        ),
        _ev("model_response", "-> 0003_model_response.txt (chars=3)"),
        _ev(
            "tool_call",
            "Read [main] -> 0004_tool_call.json",
            timestamp="2026-09-27T16:35:00Z",
        ),
    ]


def tool_and_last_call_files():
    return {
        "0001_tool_call.json": BASH_CALL,
        "0002_tool_result.json": BASH_RESULT,
        "0003_model_response.txt": MODEL_TXT,
        "0004_tool_call.json": _file(json.dumps({"file_path": "/x.py"}).encode()),
    }


def test_activity_body_carries_stable_key_across_poll_cycles():
    """The drawer keeps the same body key on every `sse:fleet` refresh."""
    client, _, _ = running_client(tool_and_last_call_events(), tool_and_last_call_files())
    for _ in range(2):
        body = client.get("/instances/work").text
        assert BODY_KEY in body


def test_every_pane_in_activity_fragment_is_keyed_per_call():
    client, _, _ = running_client(tool_and_last_call_events(), tool_and_last_call_files())
    html = client.get("/instances/work/activity").text
    assert pane_keys(html) == [
        "activity-now-0004_tool_call.json",
        "activity-last-call-0001_tool_call.json",
        "activity-last-response-0002_tool_result.json",
    ]


def test_same_call_keeps_same_keys():
    """Consecutive poll cycles re-render the identical key set."""
    client, _, _ = running_client(tool_and_last_call_events(), tool_and_last_call_files())
    first = pane_keys(client.get("/instances/work/activity").text)
    second = pane_keys(client.get("/instances/work/activity").text)
    assert first == second


def test_new_current_call_gets_a_new_now_key():
    """A new "Now" call starts at the top; the untouched Last call holds."""
    client, _, _ = running_client(tool_and_last_call_events(), tool_and_last_call_files())
    before = pane_keys(client.get("/instances/work/activity").text)

    events = [
        *tool_and_last_call_events(),
        _ev("model_response", "-> 0005_model_response.txt (chars=3)"),
        _ev(
            "tool_call",
            "Edit [main] -> 0006_tool_call.json",
            timestamp="2026-09-27T16:36:00Z",
        ),
    ]
    files = {
        **tool_and_last_call_files(),
        "0005_model_response.txt": MODEL_TXT,
        "0006_tool_call.json": _file(
            json.dumps(
                {"file_path": "/x.py", "old_string": "a", "new_string": "b"}
            ).encode()
        ),
    }
    client, _, _ = running_client(events, files)
    after = pane_keys(client.get("/instances/work/activity").text)

    assert "activity-now-0006_tool_call.json" in after
    assert "activity-now-0004_tool_call.json" not in after
    assert "activity-last-call-0001_tool_call.json" in after
    assert "activity-last-response-0002_tool_result.json" in after
    assert before != after


def test_new_last_call_gets_new_last_keys():
    """A new "Last call" pair starts at the top; the same "Now" holds."""
    client, _, _ = running_client(tool_and_last_call_events(), tool_and_last_call_files())
    before = pane_keys(client.get("/instances/work/activity").text)
    assert "activity-last-call-0001_tool_call.json" in before

    events = [
        _ev(
            "tool_call",
            "Bash [main] -> 0011_tool_call.json",
            timestamp="2026-09-27T16:34:00Z",
        ),
        _ev(
            "tool_result",
            "Bash [main] -> 0012_tool_result.json",
            timestamp="2026-09-27T16:34:09Z",
        ),
        _ev("model_response", "-> 0013_model_response.txt (chars=3)"),
        _ev(
            "tool_call",
            "Read [main] -> 0004_tool_call.json",
            timestamp="2026-09-27T16:35:00Z",
        ),
    ]
    files = {
        "0011_tool_call.json": BASH_CALL,
        "0012_tool_result.json": BASH_RESULT,
        "0013_model_response.txt": MODEL_TXT,
        "0004_tool_call.json": _file(json.dumps({"file_path": "/x.py"}).encode()),
    }
    client, _, _ = running_client(events, files)
    after = pane_keys(client.get("/instances/work/activity").text)

    assert "activity-last-call-0011_tool_call.json" in after
    assert "activity-last-response-0012_tool_result.json" in after
    assert "activity-last-call-0001_tool_call.json" not in after
    assert "activity-now-0004_tool_call.json" in after
    assert before != after


def test_activity_fragment_starts_fresh_without_stored_positions():
    """No pane persists scroll in storage: a reload starts at the top."""
    client, _, _ = running_client(tool_and_last_call_events(), tool_and_last_call_files())
    html = client.get("/instances/work/activity").text
    for pane in PANE.findall(html):
        assert "localStorage" not in pane
        assert "sessionStorage" not in pane
    assert "localStorage" not in html
    assert "sessionStorage" not in html


def test_collapse_and_live_summary_keep_working():
    """The scroll keys do not disturb the collapse state or live summary."""
    client, _, _ = running_client(tool_and_last_call_events(), tool_and_last_call_files())
    drawer = client.get("/instances/work").text
    assert BODY_KEY in drawer
    assert "hx-preserve" in drawer
    assert 'data-activity-group="activity"' in drawer
    assert "activity-sum" in drawer
    assert "sse:fleet" in drawer
    assert 'hx-swap="innerHTML"' in drawer
    assert "hx-swap=\"innerHTML scroll" not in drawer
    assert "hx-swap=\"innerHTML show" not in drawer
    fragment = client.get("/instances/work/activity").text
    assert "activity-sum" in fragment
