"""Drawer Activity widget: state detection, per-turn pairing and previews.

Vocabulary: Attempt log, Evidence file, Managed instance, Implementation
attempt.
"""

from __future__ import annotations

import asyncio
import copy
import json
import time

from fake_gateway import FakeGateway
from fastapi.testclient import TestClient
from test_web import fresh_snapshot, load_snapshot

from agent_dashboard.activity import (
    call_preview,
    elapsed_text,
    find_activity,
    result_preview,
    turn_pairs,
)
from agent_dashboard.app import create_app
from agent_dashboard.attempt_log import read_attempt_log
from agent_dashboard.config import Settings
from agent_dashboard.fleet import FleetState
from agent_dashboard.gateway import FileResult, ManagedInstance

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


def _payload(events, **overrides):
    base = {
        "layout": "attempt-log/v1",
        "issue": 98,
        "started": STARTED_DASH,
        "attempt": {"outcome": None},
        "events": events,
        "total_events": len(events),
        "unreadable": 0,
        "truncated": False,
        "file_size": 0,
    }
    base.update(overrides)
    return base


def _parsed(events):
    result = read_attempt_log(
        json.dumps(_payload(events)).encode(), 0, issue=98, started=STARTED_DASH
    )
    assert result.ok
    return result.events


def make_activity_client(snapshot, log_payload, files=None):
    """A running instance with one telemetry snapshot plus a scripted log."""
    now = time.time()
    state = FleetState()
    gateway = FakeGateway([ManagedInstance("work", "id-work", "running")])
    snap = fresh_snapshot(copy.deepcopy(snapshot), now)
    # Normalize the active attempt reference to the scripted log's issue
    # and a started_at the log routes accept (fractional seconds).
    current = snap.get("current_attempt")
    if isinstance(current, dict):
        current["issue_number"] = 98
    control = snap.get("control")
    if isinstance(control, dict):
        active = control.get("active_attempt")
        if isinstance(active, dict):
            active["issue_number"] = 98
            active["started_at"] = STARTED_COLON
    gateway.set_snapshot("id-work", snap)
    asyncio.run(run_cycle(state, gateway, max_concurrent_execs=4, now=now))
    gateway.set_log("id-work", log_payload)
    for filename, result in (files or {}).items():
        gateway.set_file("id-work", "98", STARTED_DASH, filename, result)
    app = create_app(
        state=state,
        settings=Settings(poll_interval=5.0),
        gateway=gateway,
        start_poller=False,
    )
    return TestClient(app, base_url="http://localhost"), state, gateway


def run_cycle(state, gateway, **kwargs):
    from agent_dashboard.collector import run_cycle as _run

    return _run(state, gateway, **kwargs)


def _file(content: bytes) -> FileResult:
    return FileResult(content=content, total_size=len(content), truncated=False)


BASH_CALL = _file(
    json.dumps({"command": "ls -la", "description": "list files"}).encode()
)
BASH_RESULT = _file(
    json.dumps({"stdout": "a\n", "stderr": "", "is_error": False}).encode()
)
MODEL_TXT = _file(b"working on the widget")


# --- per-turn pairing --------------------------------------------------------


def test_abandoned_call_in_earlier_turn_never_shows_as_current():
    events = _parsed(
        [
            _ev("tool_call", "Bash [main] -> 0001_tool_call.json"),
            _ev("model_response", "-> 0002_model_response.txt (chars=3)"),
            _ev("tool_call", "Bash [main] -> 0003_tool_call.json"),
            _ev("tool_result", "Bash [main] -> 0004_tool_result.json"),
        ]
    )
    pairs = turn_pairs(events)
    assert pairs == {3: 4, 4: 3}
    activity = find_activity(events)
    assert activity.state == "model"
    assert activity.current == ()
    assert activity.last_call is not None and activity.last_call.seq == 3
    assert activity.last_result is not None and activity.last_result.seq == 4


def test_current_call_runs_after_latest_model_response():
    events = _parsed(
        [
            _ev("tool_call", "Bash [main] -> 0001_tool_call.json"),
            _ev("tool_result", "Bash [main] -> 0002_tool_result.json"),
            _ev("model_response", "-> 0003_model_response.txt (chars=3)"),
            _ev("tool_call", "Bash [main] -> 0004_tool_call.json"),
        ]
    )
    activity = find_activity(events)
    assert activity.state == "tool"
    assert [ev.seq for ev in activity.current] == [4]
    assert activity.since is not None and activity.since.seq == 4


def test_parallel_running_calls_count_beyond_the_latest():
    events = _parsed(
        [
            _ev("model_response", "-> 0001_model_response.txt (chars=3)"),
            _ev("tool_call", "Bash [main] -> 0002_tool_call.json"),
            _ev("tool_call", "Read [main] -> 0003_tool_call.json"),
        ]
    )
    activity = find_activity(events)
    assert activity.state == "tool"
    assert [ev.seq for ev in activity.current] == [2, 3]


def test_model_working_uses_last_event_and_last_message():
    events = _parsed(
        [
            _ev("tool_call", "Bash [main] -> 0001_tool_call.json"),
            _ev("tool_result", "Bash [main] -> 0002_tool_result.json"),
            _ev("model_response", "-> 0003_model_response.txt (chars=3)"),
        ]
    )
    activity = find_activity(events)
    assert activity.state == "model"
    assert activity.since is not None and activity.since.seq == 3
    assert activity.last_message is not None and activity.last_message.seq == 3


def test_empty_stream_reads_as_empty():
    assert find_activity(()).state == "empty"
    assert turn_pairs(()) == {}


def test_turn_pairs_never_raise_on_garbage():
    assert turn_pairs(None) == {}  # type: ignore[arg-type]
    assert find_activity(None).state == "empty"  # type: ignore[arg-type]


# --- previews ----------------------------------------------------------------


def test_bash_call_preview_shows_description_and_command():
    head, body = call_preview(
        "Bash", json.dumps({"command": "ls -la", "description": "list"}).encode()
    )
    assert head == "list"
    assert body == "ls -la"


def test_bash_result_preview_shows_stdout_then_stderr():
    head, body, is_error = result_preview(
        "Bash",
        json.dumps({"stdout": "out\n", "stderr": "err\n"}).encode(),
        False,
    )
    assert head == ""
    assert "out" in body and "[stderr]" in body and "err" in body
    assert is_error is False


def test_error_mark_for_failed_and_interrupted_results():
    _, _, failed = result_preview(
        "Bash", json.dumps({"stdout": "", "is_error": True}).encode(), False
    )
    _, _, interrupted = result_preview(
        "Bash", json.dumps({"stdout": "", "interrupted": True}).encode(), False
    )
    assert failed is True
    assert interrupted is True


def test_read_result_preview_shows_path_and_content():
    head, body, is_error = result_preview(
        "Read",
        json.dumps(
            {"file": {"filePath": "/x.py", "content": "hello", "numLines": 1}}
        ).encode(),
        False,
    )
    assert "/x.py" in head and "1 lines" in head
    assert body == "hello"
    assert is_error is False


def test_edit_call_preview_shows_minus_plus_lines():
    head, body = call_preview(
        "Edit",
        json.dumps(
            {"file_path": "/x.py", "old_string": "a", "new_string": "b"}
        ).encode(),
    )
    assert head == "/x.py"
    assert "- a" in body and "+ b" in body


def test_write_result_preview_shows_structured_patch():
    head, body, _ = result_preview(
        "Write",
        json.dumps(
            {
                "filePath": "/x.py",
                "type": "edit",
                "structuredPatch": [{"lines": ["- a", "+ b"]}],
            }
        ).encode(),
        False,
    )
    assert "/x.py" in head
    assert "- a" in body and "+ b" in body


def test_generic_preview_is_compact_json_without_agent_id():
    head, body = call_preview("Glob", json.dumps({"pattern": "**/*.py"}).encode())
    assert head == "**/*.py"
    _, body, _ = result_preview(
        "Glob", json.dumps({"matches": 2, "agent_id": "abc"}).encode(), False
    )
    assert "agent_id" not in body
    assert "2" in body


def test_elapsed_text_renders_seconds_minutes_hours():
    assert elapsed_text("2026-09-27T16:34:00Z", "2026-09-27T16:34:05Z") == "5.0 s"
    assert elapsed_text("2026-09-27T16:34:00Z", "2026-09-27T16:34:50Z") == "50 s"
    assert elapsed_text("2026-09-27T16:34:00Z", "2026-09-27T16:37:04Z") == "3 min 04 s"
    assert elapsed_text("bad", "2026-09-27T16:34:05Z") == ""


# --- route -------------------------------------------------------------------


def test_activity_shows_running_tool_and_last_call():
    snapshot = load_snapshot("snapshot-attempt-in-progress.json")
    events = [
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
    client, _, _ = make_activity_client(
        snapshot,
        _payload(events),
        {
            "0001_tool_call.json": BASH_CALL,
            "0002_tool_result.json": BASH_RESULT,
            "0003_model_response.txt": MODEL_TXT,
            "0004_tool_call.json": _file(json.dumps({"file_path": "/x.py"}).encode()),
        },
    )
    body = client.get("/instances/work/activity").text
    assert "Read" in body
    assert "running" in body
    assert "Last call" in body
    assert "Bash" in body
    assert "list files" in body or "ls -la" in body
    assert "log" in body
    assert "?q=0004_tool_call.json" in body
    assert "?q=0001_tool_call.json" in body


def test_activity_shows_model_working_with_last_message():
    snapshot = load_snapshot("snapshot-attempt-in-progress.json")
    events = [
        _ev("tool_call", "Bash [main] -> 0001_tool_call.json"),
        _ev("tool_result", "Bash [main] -> 0002_tool_result.json"),
        _ev("model_response", "-> 0003_model_response.txt (chars=3)"),
    ]
    client, _, _ = make_activity_client(
        snapshot,
        _payload(events),
        {
            "0001_tool_call.json": BASH_CALL,
            "0002_tool_result.json": BASH_RESULT,
            "0003_model_response.txt": MODEL_TXT,
        },
    )
    body = client.get("/instances/work/activity").text
    assert "Model is working" in body
    assert "working on the widget" in body


def test_activity_marks_failed_results():
    snapshot = load_snapshot("snapshot-attempt-in-progress.json")
    events = [
        _ev("tool_call", "Bash [main] -> 0001_tool_call.json"),
        _ev("tool_result", "Bash [main] -> 0002_tool_result.json"),
    ]
    client, _, _ = make_activity_client(
        snapshot,
        _payload(events),
        {
            "0001_tool_call.json": BASH_CALL,
            "0002_tool_result.json": _file(
                json.dumps({"stdout": "", "is_error": True}).encode()
            ),
        },
    )
    body = client.get("/instances/work/activity").text
    assert "error" in body


def test_activity_truncated_log_links_instead_of_stale_data():
    snapshot = load_snapshot("snapshot-attempt-in-progress.json")
    payload = _payload(
        [_ev("tool_call", "Bash [main] -> 0001_tool_call.json")],
        truncated=True,
        file_size=5 * 1024 * 1024,
    )
    client, _, _ = make_activity_client(snapshot, payload)
    body = client.get("/instances/work/activity").text
    assert "larger than the read cap" in body
    assert "latest calls are not visible" in body
    assert "Open the Attempt log" in body
    assert "ls -la" not in body


def test_activity_stays_visible_through_publishing():
    snapshot = load_snapshot("snapshot-attempt-in-progress.json")
    snapshot["current_attempt"] = None
    assert snapshot["control"]["active_attempt"]["started_at"]
    client, _, _ = make_activity_client(
        snapshot, _payload([_ev("tool_call", "Bash [main] -> 0001_tool_call.json")])
    )
    body = client.get("/instances/work").text
    assert "n/a" in body
    assert "/instances/work/activity" in body


def test_activity_refresh_reads_each_evidence_file_at_most_once():
    snapshot = load_snapshot("snapshot-attempt-in-progress.json")
    events = [
        _ev("tool_call", "Bash [main] -> 0001_tool_call.json"),
        _ev("tool_result", "Bash [main] -> 0002_tool_result.json"),
        _ev("model_response", "-> 0003_model_response.txt (chars=3)"),
    ]
    client, _, gateway = make_activity_client(
        snapshot,
        _payload(events),
        {
            "0001_tool_call.json": BASH_CALL,
            "0002_tool_result.json": BASH_RESULT,
            "0003_model_response.txt": MODEL_TXT,
        },
    )
    client.get("/instances/work/activity")
    first = list(gateway.file_calls)
    assert first
    client.get("/instances/work/activity")
    assert gateway.file_calls == first


def test_activity_drawer_widget_has_fixed_slot_collapse_and_summary():
    snapshot = load_snapshot("snapshot-attempt-in-progress.json")
    client, _, _ = make_activity_client(
        snapshot, _payload([_ev("tool_call", "Bash [main] -> 0001_tool_call.json")])
    )
    body = client.get("/instances/work").text
    assert 'hx-preserve' in body
    assert 'data-activity-group="activity"' in body
    assert "activity-sum" in body
    assert "Loading activity" in body
    assert "sse:fleet" in body
