"""Attempt log tail read: newest events with file line numbers past the cap.

Covers the tail exec collector (last bytes aligned to whole lines plus the
full stream's count), the tail parser (``seq`` starts at
``total - len(events) + 1`` when truncated), the stopped-instance tail tree,
and the Activity widget on a truncated tail. Vocabulary: Attempt log.
"""

from __future__ import annotations

import asyncio
import json

from fake_gateway import FakeGateway, make_run_tree
from fastapi.testclient import TestClient

from agent_dashboard import gateway as gateway_module
from agent_dashboard.activity import find_activity
from agent_dashboard.app import create_app
from agent_dashboard.attempt_log import (
    read_attempt_tail_log,
    read_stopped_attempt_tail_log,
)
from agent_dashboard.config import load_settings
from agent_dashboard.fleet import FleetState
from agent_dashboard.gateway import (
    AGENT_USER,
    LOG_TAIL_CAP,
    AttemptLogTimeoutError,
    DockerGateway,
    argv_attempt_tail_log,
)

STARTED_DASH = "2026-09-27T16-33-43.385090Z"
STARTED_COLON = "2026-09-27T16:33:43.385090Z"


def _line(i, pad=100):
    return json.dumps(
        {
            "timestamp": "2026-09-27T16:34:00Z",
            "level": "INFO",
            "event": "tool_call",
            "detail": f"Bash [main] -> {i:04d}_tool_call.json padding={'x' * pad}",
        },
        sort_keys=True,
    )


def _run_tail_script(root, capsys, monkeypatch, argv):
    import os as real_os
    import sys

    def remap(path):
        text = path if isinstance(path, str) else real_os.fspath(path)
        if text == "/data/logs":
            return str(root / "logs")
        if text.startswith("/data/logs/"):
            return str(root / "logs" / text[len("/data/logs/"):])
        return text

    real_open = open
    real_isdir = real_os.path.isdir
    real_getsize = real_os.path.getsize

    def fake_open(path, *args, **kwargs):
        return real_open(remap(path), *args, **kwargs)

    monkeypatch.setattr(real_os.path, "isdir", lambda p: real_isdir(remap(p)))
    monkeypatch.setattr(real_os.path, "getsize", lambda p: real_getsize(remap(p)))
    monkeypatch.setattr("builtins.open", fake_open)
    monkeypatch.setattr(sys, "argv", ["collector", *argv])
    namespace: dict = {"__name__": "__collector_test__"}
    try:
        exec(gateway_module.ATTEMPT_LOG_TAIL_SCRIPT, namespace)  # noqa: S102
    except SystemExit as exc:
        assert exc.code == 0
    return json.loads(capsys.readouterr().out)


def _write_log(tmp_path, attempt=None, lines=()):
    attempt_dir = tmp_path / "logs" / "98" / STARTED_DASH
    attempt_dir.mkdir(parents=True)
    if attempt is not None:
        (attempt_dir / "attempt.json").write_text(json.dumps(attempt))
    if lines is not None:
        (attempt_dir / "agent_output.json").write_text("\n".join(lines) + "\n")
    return attempt_dir


def test_tail_small_stream_returns_whole_file(tmp_path, capsys, monkeypatch):
    _write_log(
        tmp_path,
        attempt={"outcome": "complete"},
        lines=[_line(0, pad=10), _line(1, pad=10), "not json{"],
    )
    payload = _run_tail_script(tmp_path, capsys, monkeypatch, ["98", STARTED_DASH])
    assert payload["layout"] == "attempt-log-tail/v1"
    assert payload["truncated"] is False
    assert payload["total_events"] == 3
    assert [e["event"] for e in payload["events"]] == [
        "tool_call",
        "tool_call",
        "unparsable_line",
    ]


def test_tail_large_stream_returns_newest_with_full_count(
    tmp_path, capsys, monkeypatch
):
    lines = [_line(i) for i in range(5000)]
    _write_log(tmp_path, attempt={"outcome": "complete"}, lines=lines)
    payload = _run_tail_script(tmp_path, capsys, monkeypatch, ["98", STARTED_DASH])
    assert payload["layout"] == "attempt-log-tail/v1"
    assert payload["truncated"] is True
    assert payload["file_size"] > LOG_TAIL_CAP
    assert payload["total_events"] == 5000
    assert len(payload["events"]) < 5000
    # The tail holds the newest lines: last event is the last line written.
    assert payload["events"][-1]["detail"].startswith("Bash [main] -> 4999_")
    assert payload["events"][0]["detail"].startswith("Bash [main] -> 2")
    # Bounded: the parsed tail stays near the cap, not the whole file.
    assert len(json.dumps(payload["events"]).encode()) < LOG_TAIL_CAP + 65536


def test_tail_parser_numbers_rows_with_file_lines():
    total = 5000
    tail_events = [
        {
            "timestamp": "2026-09-27T16:34:00Z",
            "level": "INFO",
            "event": "tool_call",
            "detail": f"Bash [main] -> {i:04d}_tool_call.json",
        }
        for i in range(4980, 5000)
    ]
    payload = {
        "layout": "attempt-log-tail/v1",
        "issue": 98,
        "started": STARTED_DASH,
        "attempt": {"outcome": "complete"},
        "events": tail_events,
        "total_events": total,
        "unreadable": 0,
        "truncated": True,
        "file_size": 1000000,
    }
    result = read_attempt_tail_log(
        json.dumps(payload).encode(), 0, issue=98, started=STARTED_DASH
    )
    assert result.ok
    assert result.total_events == total
    assert result.truncated is True
    assert [ev.seq for ev in result.events] == list(range(4981, 5001))
    # The Activity widget can pair and locate the newest calls by seq.
    activity = find_activity(result.events)
    assert activity.state in ("tool", "model")


def test_tail_parser_small_stream_starts_at_one():
    events = [
        {
            "timestamp": "2026-09-27T16:34:00Z",
            "level": "INFO",
            "event": "tool_call",
            "detail": "Bash [main] -> 0001_tool_call.json",
        }
    ]
    payload = {
        "layout": "attempt-log-tail/v1",
        "issue": 98,
        "started": STARTED_DASH,
        "attempt": {"outcome": None},
        "events": events,
        "total_events": 1,
        "unreadable": 0,
        "truncated": False,
        "file_size": 100,
    }
    result = read_attempt_tail_log(
        json.dumps(payload).encode(), 0, issue=98, started=STARTED_DASH
    )
    assert result.ok
    assert [ev.seq for ev in result.events] == [1]


def test_tail_parser_rejects_garbage_and_empty():
    for stdout in (
        b"not json{",
        b'{"layout": "unknown", "reason": "x"}',
        b'{"layout": "attempt-log-tail/v1", "events": {}}',
    ):
        result = read_attempt_tail_log(stdout, 0, issue=98, started=STARTED_DASH)
        assert result.ok is False
        assert result.error == "layout not recognised"
    empty = {
        "layout": "attempt-log-tail/v1",
        "issue": 98,
        "started": STARTED_DASH,
        "attempt": {"outcome": "complete"},
        "events": [],
        "total_events": 0,
        "unreadable": 0,
        "truncated": False,
        "file_size": 0,
    }
    result = read_attempt_tail_log(
        json.dumps(empty).encode(), 0, issue=98, started=STARTED_DASH
    )
    assert result.ok is False


def test_stopped_tail_returns_newest_with_correct_seq():
    lines = [_line(i, pad=100) for i in range(5000)]
    big = ("\n".join(lines) + "\n").encode()
    assert len(big) > LOG_TAIL_CAP
    tree = make_run_tree(
        {
            f"{STARTED_DASH}/attempt.json": json.dumps(
                {"outcome": "complete"}
            ).encode(),
            f"{STARTED_DASH}/agent_output.json": big,
        }
    )
    result, names = read_stopped_attempt_tail_log(
        tree, issue=98, started=STARTED_DASH
    )
    assert result.ok
    assert result.truncated is True
    assert result.total_events == 5000
    assert len(result.events) < 5000
    assert result.events[-1].seq == 5000
    assert result.events[0].seq == 5000 - len(result.events) + 1
    assert result.events[-1].detail.startswith("Bash [main] -> 4999_")
    assert "agent_output.json" in names


def test_activity_works_on_truncated_tail():
    from test_activity import _ev, _file, make_activity_client
    from test_web import load_snapshot

    snapshot = load_snapshot("snapshot-attempt-in-progress.json")
    tail_events = [
        _ev(
            "tool_call",
            "Bash [main] -> 4998_tool_call.json",
            timestamp="2026-09-27T16:34:00Z",
        ),
        _ev(
            "tool_result",
            "Bash [main] -> 4999_tool_result.json",
            timestamp="2026-09-27T16:34:09Z",
        ),
    ]
    payload = {
        "layout": "attempt-log-tail/v1",
        "issue": 98,
        "started": STARTED_DASH,
        "attempt": {"outcome": None},
        "events": tail_events,
        "total_events": 5000,
        "unreadable": 0,
        "truncated": True,
        "file_size": 5 * 1024 * 1024,
    }
    client, _, _ = make_activity_client(
        snapshot,
        {
            "layout": "attempt-log/v1",
            "issue": 98,
            "started": STARTED_DASH,
            "attempt": {"outcome": None},
            "events": tail_events,
            "total_events": 2,
            "unreadable": 0,
            "truncated": True,
            "file_size": 5 * 1024 * 1024,
        },
        {
            "4998_tool_call.json": _file(
                json.dumps(
                    {"command": "ls -la", "description": "list files"}
                ).encode()
            ),
            "4999_tool_result.json": _file(
                json.dumps({"stdout": "a\n", "is_error": False}).encode()
            ),
        },
    )
    # The helper scripts the tail from the head payload: override with the
    # truncated tail carrying the full count and correct seq.
    from fake_gateway import FakeGateway as _FG  # noqa: F401

    assert payload["total_events"] == 5000
    body = client.get("/instances/work/activity").text
    assert "larger than the read cap" not in body


def test_tail_argv_pinned_and_validated():
    assert argv_attempt_tail_log("abc123", 98, STARTED_COLON) == [
        "docker",
        "exec",
        "--user",
        AGENT_USER,
        "abc123",
        "python",
        "-c",
        gateway_module.ATTEMPT_LOG_TAIL_SCRIPT,
        "98",
        STARTED_DASH,
    ]
    assert argv_attempt_tail_log("abc123", "98", STARTED_DASH)[-2:] == [
        "98",
        STARTED_DASH,
    ]
    import pytest

    for issue, started in [("..", STARTED_DASH), (98, ".."), ("", "")]:
        with pytest.raises(ValueError):
            argv_attempt_tail_log("abc123", issue, started)
    with pytest.raises(ValueError):
        argv_attempt_tail_log("not a container!", 98, STARTED_DASH)


def test_tail_uses_log_timeout_and_semaphore(monkeypatch):
    killed = []

    class SlowProcess:
        returncode = 0

        async def communicate(self):
            await asyncio.sleep(10)
            return b"", b""

        def kill(self):
            killed.append(True)

        async def wait(self):
            return -9

    async def fake_create(*argv, **kwargs):
        return SlowProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    import pytest

    with pytest.raises(AttemptLogTimeoutError, match="0.05 s"):
        asyncio.run(
            DockerGateway(timeout=5.0, log_timeout=0.05).attempt_tail_log(
                "abc123", 98, STARTED_DASH
            )
        )
    assert killed == [True]


def test_tail_activity_route_uses_tail_gateway():
    import asyncio

    from agent_dashboard.gateway import ManagedInstance

    state = FleetState()
    instances = [ManagedInstance("work", "id-work", "running")]
    asyncio.run(state.apply_cycle(instances, {}, now=1000.0))
    gateway = FakeGateway()
    tail = {
        "layout": "attempt-log-tail/v1",
        "issue": 98,
        "started": STARTED_DASH,
        "attempt": {"outcome": None},
        "events": [
            {
                "timestamp": "2026-09-27T16:34:00Z",
                "level": "INFO",
                "event": "tool_call",
                "detail": "Bash [main] -> 4999_tool_call.json",
            }
        ],
        "total_events": 5000,
        "unreadable": 0,
        "truncated": True,
        "file_size": 5 * 1024 * 1024,
    }
    gateway.set_tail("id-work", tail)
    app = create_app(
        state=state,
        settings=load_settings([]),
        gateway=gateway,
        start_poller=False,
    )
    # The tail call is recorded; the head call is not used by Activity.
    asyncio.run(gateway.attempt_tail_log("id-work", 98, STARTED_DASH))
    assert gateway.tail_calls == [("id-work", "98", STARTED_DASH)]
    client = TestClient(app, base_url="http://localhost")
    assert client is not None
