"""Call/result pairing: FIFO per (tool, thread) over the full stream.

Vocabulary: Attempt log, Evidence file, Managed instance, Implementation
attempt.
"""

from __future__ import annotations

import json

from fake_gateway import FakeGateway
from fastapi.testclient import TestClient
from test_attempt_log import STARTED_DASH, _ev

from agent_dashboard.app import create_app
from agent_dashboard.attempt_log import (
    pair_tool_calls,
    paired_files,
    read_attempt_log,
)
from agent_dashboard.config import load_settings
from agent_dashboard.fleet import FleetState
from agent_dashboard.gateway import ManagedInstance

FULL_ATTEMPT = {
    "outcome": "complete",
    "started_at": "2026-09-27T16:33:43.385090Z",
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


def _fixture_events():
    """Parallel calls plus a missing result plus an orphan result."""
    return [
        _ev("tool_call", "Bash [main] -> 0001_tool_call.json"),
        _ev("tool_call", "Bash [main] -> 0002_tool_call.json"),
        _ev("tool_call", "Read [subagent:w1] -> 0003_tool_call.json"),
        _ev("tool_call", "Read [main] -> 0004_tool_call.json"),
        _ev("tool_result", "Bash [main] -> 0005_tool_result.json"),
        _ev("tool_result", "Bash [main] -> 0006_tool_result.json"),
        _ev("tool_result", "Read [subagent:w1] -> 0007_tool_result.json"),
        _ev("tool_result", "Write [main] -> 0008_tool_result.json"),
        _ev("model_response", "-> 0009_model_response.txt (chars=12)"),
    ]


def _parsed(events):
    result = read_attempt_log(
        json.dumps(_payload(events)).encode(),
        0,
        issue=98,
        started=STARTED_DASH,
    )
    assert result.ok
    return result.events


# --- pure pairing ------------------------------------------------------------


def test_fifo_pairs_oldest_unmatched_call_first():
    events = _parsed(_fixture_events())
    assert pair_tool_calls(events) == {
        1: 5,
        5: 1,
        2: 6,
        6: 2,
        3: 7,
        7: 3,
    }


def test_parallel_calls_pair_within_their_thread():
    events = _parsed(
        [
            _ev("tool_call", "Bash [main] -> 0001_tool_call.json"),
            _ev("tool_call", "Bash [subagent:w1] -> 0002_tool_call.json"),
            _ev("tool_result", "Bash [subagent:w1] -> 0003_tool_result.json"),
            _ev("tool_result", "Bash [main] -> 0004_tool_result.json"),
        ]
    )
    assert pair_tool_calls(events) == {1: 4, 4: 1, 2: 3, 3: 2}


def test_old_calls_without_a_thread_tag_still_pair():
    events = _parsed(
        [
            _ev("tool_call", "Bash -> 0001_tool_call.json"),
            _ev("tool_result", "Bash -> 0002_tool_result.json"),
        ]
    )
    assert pair_tool_calls(events) == {1: 2, 2: 1}


def test_unpaired_rows_stay_out_of_the_map():
    events = _parsed(_fixture_events())
    pairs = pair_tool_calls(events)
    # seq 4 is a call without a result; seq 8 is a result without a
    # call; seq 9 is a model response and never pairs.
    assert 4 not in pairs
    assert 8 not in pairs
    assert 9 not in pairs
    assert paired_files(events, pairs) == {
        1: "0005_tool_result.json",
        5: "0001_tool_call.json",
        2: "0006_tool_result.json",
        6: "0002_tool_call.json",
        3: "0007_tool_result.json",
        7: "0003_tool_call.json",
    }


def test_rows_without_evidence_never_pair():
    events = _parsed(
        [
            _ev("tool_call", 'Bash [main] {"command": "ls"}'),
            _ev("tool_result", "Bash [main] -> 0002_tool_result.json"),
            _ev("skill_call", "Bash [main] -> 0003_tool_call.json"),
        ]
    )
    assert pair_tool_calls(events) == {}


def test_pairing_never_raises_on_garbage():
    assert pair_tool_calls(()) == {}
    assert pair_tool_calls(None) == {}  # type: ignore[arg-type]
    assert paired_files((), {}) == {}


# --- page --------------------------------------------------------------------


def _pair_client(events):
    import asyncio

    state = FleetState()
    asyncio.run(
        state.apply_cycle(
            [ManagedInstance("work", "id-work", "running")], {}, now=1000.0
        )
    )
    gateway = FakeGateway()
    gateway.set_log("id-work", _payload(events))
    app = create_app(
        state=state,
        settings=load_settings([]),
        gateway=gateway,
        start_poller=False,
    )
    return TestClient(app, base_url="http://localhost")


def test_page_shows_open_result_and_open_call_with_order_note():
    client = _pair_client(_fixture_events())
    body = client.get(f"/instances/work/attempts/98/{STARTED_DASH}").text
    assert "open result" in body
    assert "open call" in body
    assert "paired by order" in body
    # The FIFO pair target is the link target, not just nearby text.
    assert "0005_tool_result.json" in body
    assert (
        'href="/instances/work/attempts/98/'
        f'{STARTED_DASH}/files/0005_tool_result.json"' in body
    )
    assert (
        'href="/instances/work/attempts/98/'
        f'{STARTED_DASH}/files/0001_tool_call.json"' in body
    )


def test_unpaired_rows_render_without_a_pair_link_or_pair_slot():
    client = _pair_client(_fixture_events())
    body = client.get(f"/instances/work/attempts/98/{STARTED_DASH}").text
    # Every evidence row keeps its own slot; only paired rows get a pair slot.
    assert 'id="evidence-slot-4"' in body
    assert 'id="evidence-pair-slot-4"' not in body
    assert 'id="evidence-slot-8"' in body
    assert 'id="evidence-pair-slot-8"' not in body
    assert 'id="evidence-pair-slot-1"' in body
    assert 'id="evidence-slot-1"' in body
    # No pair link points at the orphan result or the missing result.
    assert "0008_tool_result.json" in body  # its own open link stays
    assert body.count('id="evidence-pair-slot-') == 6


def test_pair_link_survives_a_filter_that_hides_the_counterpart():
    client = _pair_client(_fixture_events())
    body = client.get(
        f"/instances/work/attempts/98/{STARTED_DASH}?event=tool_call"
    ).text
    # Results are filtered out, but each paired call still links at its result.
    assert "<td>tool_result</td>" not in body
    assert "open result" in body
    assert "0005_tool_result.json" in body
    assert "paired by order" in body


def test_pair_link_survives_paging():
    events = [
        _ev("tool_call", "Bash [main] -> 0001_tool_call.json"),
    ] + [
        _ev("profile_loaded", f"profile={i}", level="DEBUG") for i in range(199)
    ] + [
        _ev("tool_result", "Bash [main] -> 0201_tool_result.json"),
    ]
    assert len(events) == 201
    client = _pair_client(events)
    first = client.get(f"/instances/work/attempts/98/{STARTED_DASH}?page=1").text
    assert "page 1/2, 201 events" in first
    # The result row sits on page 2, but the call on page 1 still links
    # at its file.
    assert "<td>tool_result</td>" not in first
    assert "open result" in first
    assert (
        'href="/instances/work/attempts/98/'
        f'{STARTED_DASH}/files/0201_tool_result.json"' in first
    )
    assert "paired by order" in first
    second = client.get(f"/instances/work/attempts/98/{STARTED_DASH}?page=2").text
    assert "0201_tool_result.json" in second
    assert "open call" in second
