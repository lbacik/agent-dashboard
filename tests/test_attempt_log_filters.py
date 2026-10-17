"""Attempt log filters: level/event/q narrow the timeline, then paging applies.

Vocabulary: Attempt log, Evidence file, Managed instance, Implementation
attempt.
"""

from __future__ import annotations

import asyncio
import json

from fake_gateway import FakeGateway
from fastapi.testclient import TestClient

from agent_dashboard.app import create_app
from agent_dashboard.attempt_log import (
    event_names,
    filter_events,
    level_options,
    read_attempt_log,
)
from agent_dashboard.config import load_settings
from agent_dashboard.fleet import FleetState
from agent_dashboard.gateway import ManagedInstance

STARTED_DASH = "2026-09-27T16-33-43.385090Z"
STARTED_COLON = "2026-09-27T16:33:43.385090Z"


def _ev(name, detail="", level="INFO", timestamp="2026-09-27T16:34:00Z"):
    return {
        "timestamp": timestamp,
        "level": level,
        "event": name,
        "issue_number": 98,
        "phase": "model_execution",
        "detail": detail,
    }


FULL_ATTEMPT = {
    "outcome": "complete",
    "started_at": STARTED_COLON,
    "issue_title": "Add a widget",
}


def _payload(events, **overrides):
    base = {
        "layout": "attempt-log/v1",
        "issue": 98,
        "started": STARTED_DASH,
        "attempt": dict(FULL_ATTEMPT),
        "events": events,
        "total_events": len(events),
        "unreadable": 0,
        "truncated": False,
        "file_size": 0,
    }
    base.update(overrides)
    return base


def _events():
    return (
        _ev("tool_call", "Bash [main] -> 0001_tool_call.json", "INFO"),
        _ev("tool_result", "Bash [main] -> 0002_tool_result.json", "INFO"),
        _ev("model_response", "reply text about widgets", "INFO"),
        _ev("token_budget_reconciled", '{"budget_tokens": 9}', "WARNING"),
        _ev("profile_loaded", "profile=default", "DEBUG"),
        _ev("tool_call", "Read [main] -> 0005_tool_call.json", "ERROR"),
    )


def _parsed(events):
    result = read_attempt_log(
        json.dumps(_payload(list(events))).encode(),
        0,
        issue=98,
        started=STARTED_DASH,
    )
    assert result.ok
    return result.events


# --- pure filter helper ------------------------------------------------------


def test_level_filter_alone():
    events = _parsed(_events())
    rows = filter_events(events, level="INFO")
    assert [ev.seq for ev in rows] == [1, 2, 3]
    assert filter_events(events, level="error")[0].seq == 6


def test_event_filter_alone():
    events = _parsed(_events())
    rows = filter_events(events, event="tool_call")
    assert [ev.seq for ev in rows] == [1, 6]


def test_q_filter_is_case_insensitive_substring_of_detail():
    events = _parsed(_events())
    assert [ev.seq for ev in filter_events(events, q="bash")] == [1, 2]
    assert [ev.seq for ev in filter_events(events, q="WIDGET")] == [3]
    assert [ev.seq for ev in filter_events(events, q="0005_tool")] == [6]


def test_filters_compose():
    events = _parsed(_events())
    rows = filter_events(events, level="INFO", event="tool_call", q="bash")
    assert [ev.seq for ev in rows] == [1]
    assert filter_events(events, level="INFO", event="tool_call") == tuple(
        [events[0]]
    )


def test_blank_filters_match_everything():
    events = _parsed(_events())
    assert filter_events(events) == events
    assert filter_events(events, level="", event="all", q="   ") == events
    assert filter_events(events, level="ALL", q=None) == events


def test_q_all_is_a_literal_substring_not_a_reset():
    events = _parsed(_events())
    rows = filter_events(events, q="all")
    # Only details containing "all" (the two tool_call rows) match;
    # the query must not read as "no filtering".
    assert rows != events
    assert [ev.seq for ev in rows] == [1, 6]


def test_unknown_filter_values_show_empty_not_error():
    events = _parsed(_events())
    assert filter_events(events, level="VERBOSE") == ()
    assert filter_events(events, event="no_such_event") == ()
    assert filter_events(events, q="zzz-no-match") == ()
    # Garbage types never raise either.
    assert filter_events(events, level=None, event=5, q=["x"]) == events


def test_filtered_rows_keep_file_line_numbers():
    events = _parsed(_events())
    rows = filter_events(events, event="tool_call")
    assert [ev.seq for ev in rows] == [1, 6]


def test_event_names_lists_names_observed_in_stream():
    events = _parsed(_events())
    assert event_names(events) == (
        "model_response",
        "profile_loaded",
        "token_budget_reconciled",
        "tool_call",
        "tool_result",
    )


def test_level_options_offer_known_levels_plus_observed_extras():
    events = _parsed(_events())
    options = level_options(events)
    assert options[:4] == ("", "INFO", "WARNING", "ERROR")
    assert "DEBUG" in options


# --- page --------------------------------------------------------------------


def make_log_client(rows, logs=None):
    state = FleetState()
    instances = []
    for name, container_state in rows:
        raw = "running" if container_state == "running" else "exited"
        instances.append(ManagedInstance(name, f"id-{name}", raw))
    asyncio.run(state.apply_cycle(instances, {}, now=1000.0))
    gateway = FakeGateway()
    for container_id, payload in (logs or {}).items():
        gateway.set_log(container_id, payload)
    app = create_app(
        state=state,
        settings=load_settings([]),
        gateway=gateway,
        start_poller=False,
    )
    return TestClient(app, base_url="http://localhost"), state, gateway


def _filtered_client():
    return make_log_client(
        [("work", "running")], {"id-work": _payload(list(_events()))}
    )


def test_page_shows_filter_form_with_observed_events_and_reset():
    client, _, _ = _filtered_client()
    body = client.get(f"/instances/work/attempts/98/{STARTED_DASH}").text
    assert "<form" in body
    assert 'name="level"' in body
    assert 'name="event"' in body
    assert 'name="q"' in body
    for name in ("tool_call", "tool_result", "model_response"):
        assert f'value="{name}"' in body
    assert "Reset" in body
    base = f"/instances/work/attempts/98/{STARTED_DASH}"
    assert f'href="{base}"' in body


def test_each_filter_narrows_and_counts_match_filtered_stream():
    client, _, _ = _filtered_client()
    base = f"/instances/work/attempts/98/{STARTED_DASH}"
    level = client.get(base + "?level=INFO").text
    assert "page 1/1, 3 events" in level
    # The select still lists every observed name; only the rows narrow.
    assert "<td>token_budget_reconciled</td>" not in level
    assert "<td>tool_call</td>" in level
    event = client.get(base + "?event=tool_call").text
    assert "page 1/1, 2 events" in event
    assert "<td>model_response</td>" not in event
    query = client.get(base + "?q=bash").text
    assert "page 1/1, 2 events" in query
    composed = client.get(base + "?level=INFO&event=tool_call&q=bash").text
    assert "page 1/1, 1 events" in composed
    assert "0001_tool_call.json" in composed
    assert "0005_tool_call.json" not in composed


def test_paging_links_keep_active_filters():
    events = [
        _ev("tool_call", f"Bash [main] -> {i:04d}_tool_call.json")
        for i in range(450)
    ]
    client, _, _ = make_log_client(
        [("work", "running")], {"id-work": _payload(events)}
    )
    base = f"/instances/work/attempts/98/{STARTED_DASH}"
    body = client.get(base + "?event=tool_call&page=2").text
    assert "page 2/3, 450 events" in body
    assert "event=tool_call" in body
    assert "page=1" in body
    assert "page=3" in body


def test_filtered_line_numbers_are_file_line_numbers():
    client, _, _ = _filtered_client()
    body = client.get(
        f"/instances/work/attempts/98/{STARTED_DASH}?event=tool_call"
    ).text
    assert "<td>1</td>" in body
    assert "<td>6</td>" in body


def test_unknown_filter_values_render_empty_without_error():
    client, _, _ = _filtered_client()
    base = f"/instances/work/attempts/98/{STARTED_DASH}"
    for query in ("?level=VERBOSE", "?event=no_such_event", "?q=zzz-no-match"):
        response = client.get(base + query)
        assert response.status_code == 200
        assert "page 1/1, 0 events" in response.text
