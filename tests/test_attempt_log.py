"""Attempt log page: full event stream header, rows, paging and gateway read.

Covers the tolerant stream parser (header from ``attempt.json`` with any key
missing, synthetic rows for garbage, unknown events/levels), the gateway's
full-stream read (exact argv, traversal rejected before argv, log timeout
and log semaphore), and the Attempt log page (running only, static, paged at
200 rows). Vocabulary: Attempt log, Evidence file, Managed instance,
Implementation attempt.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fake_gateway import FakeGateway
from fastapi.testclient import TestClient

from agent_dashboard import gateway as gateway_module
from agent_dashboard.app import create_app
from agent_dashboard.attempt_log import (
    LOG_PAGE_SIZE,
    NO_ATTEMPT_JSON,
    format_file_mb,
    paginate,
    parse_page,
    read_attempt_log,
)
from agent_dashboard.config import DEFAULT_LOG_EXEC_TIMEOUT, load_settings
from agent_dashboard.fleet import FleetState
from agent_dashboard.gateway import (
    AGENT_USER,
    AttemptLogTimeoutError,
    DockerGateway,
    GatewayError,
    ManagedInstance,
    argv_attempt_log,
)

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


# Small fixtures cut from the shape of real logs: current thread-tag form,
# old form without a tag, a detail with no file reference, an unknown event
# and an unknown level.
TOOL_CALL_NEW = _ev("tool_call", "Bash [main] -> 0003_tool_call.json")
TOOL_CALL_OLD = _ev("tool_call", "Bash -> 0002_tool_call.json")
TOOL_RESULT_SUBAGENT = _ev(
    "tool_result",
    "Read [subagent:abc123] -> 0004_tool_result.json",
    timestamp="2026-09-27T16:34:05Z",
)
MODEL_RESPONSE = _ev(
    "model_response",
    "-> 0001_model_response.txt (chars=85)",
    timestamp="2026-09-27T16:33:58Z",
)
TOOL_CALL_NO_FILE = _ev(
    "tool_call", 'Bash [main] {"command": "ls", "description": "list"}'
)
UNKNOWN_EVENT = _ev(
    "token_budget_reconciled",
    '{"budget_tokens": 1500000}',
    timestamp="2026-09-27T16:34:10Z",
)
UNKNOWN_LEVEL = _ev(
    "profile_loaded", "profile=default", level="DEBUG",
    timestamp="2026-09-27T16:33:50Z",
)

FULL_ATTEMPT = {
    "outcome": "complete",
    "started_at": STARTED_COLON,
    "completed_at": "2026-09-27T17:02:11.000000Z",
    "duration_seconds": 1712.5,
    "issue_title": "Add a widget",
    "agent_version": "0.5.1",
    "pull_request": {"number": 12, "url": "https://example.test/pr/12"},
    "token_budget": {"total_cost_usd": 1.5},
    "model_usage": {"claude-x": {"input_tokens": 10, "costUSD": 9.99}},
}


_DEFAULT_ATTEMPT = object()


def _payload(events, attempt=_DEFAULT_ATTEMPT, **overrides):
    base = {
        "layout": "attempt-log/v1",
        "issue": 98,
        "started": STARTED_DASH,
        "attempt": dict(FULL_ATTEMPT) if attempt is _DEFAULT_ATTEMPT else attempt,
        "events": events,
        "total_events": len(events),
        "unreadable": 0,
        "truncated": False,
        "file_size": 0,
    }
    base.update(overrides)
    return base


def _read(payload, **kwargs):
    return read_attempt_log(
        json.dumps(payload).encode(), 0, issue=98, started=STARTED_DASH, **kwargs
    )


# --- header parsing ----------------------------------------------------------


def test_full_header_parses_every_field():
    result = _read(_payload([TOOL_CALL_NEW]))
    assert result.ok and result.error is None
    assert result.issue == 98
    assert result.started_at == STARTED_COLON
    assert result.issue_title == "Add a widget"
    assert result.outcome_text == "complete"
    assert result.active is False
    assert result.attempt_missing is False
    assert result.duration_text == "28m 32s"
    # The token budget total wins over the summed model costs.
    assert result.cost_text == "$1.50"
    assert result.agent_version == "0.5.1"
    assert result.pr_number == 12
    assert result.pr_url == "https://example.test/pr/12"
    assert result.total_events == 1


def test_header_cost_falls_back_to_summed_model_costs():
    attempt = {
        "outcome": "complete",
        "model_usage": {
            "claude-a": {"input_tokens": 100, "costUSD": 1.0},
            "claude-b": {"output_tokens": 20, "costUSD": 0.234},
        },
    }
    result = _read(_payload([TOOL_CALL_NEW], attempt=attempt))
    assert result.cost_text == "$1.23"


def test_header_cost_missing_without_usage_is_not_zero():
    result = _read(
        _payload([TOOL_CALL_NEW], attempt={"outcome": "infrastructure_error"})
    )
    assert result.cost_text == "n/a"
    assert result.duration_text == "n/a"
    assert result.agent_version is None
    assert result.pr_number is None


def test_header_tolerates_partial_attempt_and_pr_shapes():
    attempt = {
        "issue_title": "T",
        "pull_request": {"number": "12", "url": ""},
    }
    result = _read(_payload([TOOL_CALL_NEW], attempt=attempt))
    assert result.ok
    assert result.outcome_text == "active"
    assert result.active is True
    assert result.pr_number is None
    assert result.pr_url is None


def test_missing_attempt_json_is_a_notice_not_an_error():
    result = _read(_payload([TOOL_CALL_NEW], attempt=None))
    assert result.ok
    assert result.attempt_missing is True
    assert result.issue_title is None
    assert NO_ATTEMPT_JSON == "no attempt.json: in progress or crashed"


def test_zero_events_is_layout_not_recognised_with_header():
    result = _read(_payload([]))
    assert result.ok is False
    assert result.error == "layout not recognised"
    assert result.issue_title == "Add a widget"
    assert result.outcome_text == "complete"


def test_garbage_payloads_never_raise_and_keep_identity():
    for stdout in (
        b"not json{",
        b"\xff\xfe binary",
        b'{"layout": "unknown", "reason": "x"}',
        b'{"layout": "attempt-log/v1", "events": {}}',
        b'{"layout": "attempt-log/v1"}',
        b"[1, 2]",
        b"null",
    ):
        result = read_attempt_log(stdout, 0, issue=98, started=STARTED_DASH)
        assert result.ok is False
        assert result.error == "layout not recognised"
        assert result.issue == 98


def test_garbage_entries_become_synthetic_rows_in_order():
    payload = _payload(
        [
            TOOL_CALL_NEW,
            None,
            5,
            "just a string",
            ["a", "list"],
            {"no": "event name here"},
            {"event": "", "level": None, "detail": None},
            {"event": "tool_call", "detail": {"nested": ["not", "a", "string"]}},
        ]
    )
    result = _read(payload)
    assert result.ok
    assert [ev.seq for ev in result.events] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert result.events[0].event == "tool_call"
    assert result.events[1].event == "unparsable_line"
    assert result.events[1].level == "WARNING"
    assert result.events[2].event == "unparsable_line"
    assert result.events[3].event == "unparsable_line"
    assert result.events[3].detail == "just a string"
    assert result.events[4].event == "unparsable_line"
    assert result.events[5].event == "unknown"
    assert result.events[6].event == "unknown"
    assert result.events[7].detail == "{'nested': ['not', 'a', 'string']}"


def test_rows_keep_file_order_time_and_level_knowledge():
    result = _read(
        _payload(
            [
                TOOL_CALL_NEW,
                UNKNOWN_EVENT,
                UNKNOWN_LEVEL,
                TOOL_RESULT_SUBAGENT,
                "not json{",
            ]
        )
    )
    assert [ev.seq for ev in result.events] == [1, 2, 3, 4, 5]
    assert [ev.time_text for ev in result.events[:4]] == [
        "16:34:00",
        "16:34:10",
        "16:33:50",
        "16:34:05",
    ]
    assert [ev.level_known for ev in result.events[:4]] == [True, True, False, True]
    assert result.events[2].level == "DEBUG"
    assert result.events[4].event == "unparsable_line"
    assert result.events[4].level == "WARNING"


def test_truncation_and_counters_survive_valid_counters_only():
    payload = _payload(
        [TOOL_CALL_NEW],
        total_events=1,
        unreadable=2,
        truncated=True,
        file_size=5000000,
    )
    result = _read(payload)
    assert result.truncated is True
    assert result.unreadable == 2
    assert result.file_size == 5000000
    assert format_file_mb(5000000) == "4.8 MB"


def test_invalid_counters_fall_back_to_parsed_values():
    payload = _payload(
        [TOOL_CALL_NEW],
        total_events=-1,
        unreadable=True,
        truncated="yes",
        file_size="big",
    )
    result = _read(payload)
    assert result.total_events == 1
    assert result.unreadable == 0
    assert result.truncated is False
    assert result.file_size == 0


# --- paging ------------------------------------------------------------------


def test_paginate_slices_200_rows_and_clamps():
    events = tuple(
        _payload([_ev("tool_call", f"detail {i}")])["events"] for i in range(450)
    )
    parsed = _read(_payload([e[0] for e in events])).events
    assert len(parsed) == 450
    rows, number, pages = paginate(parsed, "2")
    assert (number, pages) == (2, 3)
    assert [ev.seq for ev in rows] == list(range(201, 401))
    rows, number, pages = paginate(parsed, "99")
    assert (number, pages) == (3, 3)
    assert [ev.seq for ev in rows] == list(range(401, 451))
    assert paginate(parsed, "garbage")[1] == 1
    assert paginate(parsed, "-3")[1] == 1
    assert paginate(parsed, None)[1] == 1
    assert LOG_PAGE_SIZE == 200


def test_parse_page_clamps_into_range():
    assert parse_page("2", 3) == 2
    assert parse_page("99", 3) == 3
    assert parse_page("0", 3) == 1
    assert parse_page("nope", 3) == 1
    assert parse_page(None, 1) == 1


# --- collector script --------------------------------------------------------


def _run_log_script(root, capsys, monkeypatch, argv):
    """Execute the shipped ATTEMPT_LOG_SCRIPT with /data remapped onto tmp dirs."""
    import os as real_os
    import sys

    def remap(path):
        text = path if isinstance(path, str) else real_os.fspath(path)
        if text == "/data/logs":
            return str(root / "logs")
        if text.startswith("/data/logs/"):
            return str(root / "logs" / text[len("/data/logs/") :])
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
        exec(gateway_module.ATTEMPT_LOG_SCRIPT, namespace)  # noqa: S102
    except SystemExit as exc:
        assert exc.code == 0
    return json.loads(capsys.readouterr().out)


def _write_log(tmp_path, issue="98", started=STARTED_DASH, attempt=None, lines=()):
    attempt_dir = tmp_path / "logs" / issue / started
    attempt_dir.mkdir(parents=True)
    if attempt is not None:
        (attempt_dir / "attempt.json").write_text(json.dumps(attempt))
    if lines is not None:
        (attempt_dir / "agent_output.json").write_text("\n".join(lines) + "\n")
    return attempt_dir


def _line(name, detail="d", timestamp="2026-09-27T16:34:00Z"):
    return json.dumps(
        {
            "timestamp": timestamp,
            "level": "INFO",
            "event": name,
            "issue_number": 98,
            "phase": "model_execution",
            "detail": detail,
        },
        sort_keys=True,
    )


def test_log_script_returns_full_stream_in_order(tmp_path, capsys, monkeypatch):
    _write_log(
        tmp_path,
        attempt={"outcome": "complete", "issue_title": "T"},
        lines=[
            _line("tool_call", "Bash [main] -> 0001_tool_call.json"),
            _line("token_budget_reconciled", "{}"),
            _line("tool_result", "Bash [main] -> 0002_tool_result.json"),
        ],
    )
    payload = _run_log_script(tmp_path, capsys, monkeypatch, ["98", STARTED_DASH])
    assert payload["layout"] == "attempt-log/v1"
    assert payload["issue"] == 98
    assert payload["started"] == STARTED_DASH
    assert payload["attempt"] == {"outcome": "complete", "issue_title": "T"}
    assert [e["event"] for e in payload["events"]] == [
        "tool_call",
        "token_budget_reconciled",
        "tool_result",
    ]
    assert payload["events"][0]["detail"] == "Bash [main] -> 0001_tool_call.json"
    assert payload["total_events"] == 3
    assert payload["truncated"] is False
    assert payload["file_size"] > 0


def test_log_script_synthesises_unparsable_lines_inline(
    tmp_path, capsys, monkeypatch
):
    _write_log(
        tmp_path,
        attempt={"outcome": "complete"},
        lines=[_line("tool_call", "x"), "not json{", _line("tool_result", "y")],
    )
    payload = _run_log_script(tmp_path, capsys, monkeypatch, ["98", STARTED_DASH])
    assert [e["event"] for e in payload["events"]] == [
        "tool_call",
        "unparsable_line",
        "tool_result",
    ]
    synthetic = payload["events"][1]
    assert synthetic["level"] == "WARNING"
    assert synthetic["detail"] == "not json{"


def test_log_script_missing_files_keep_layout(tmp_path, capsys, monkeypatch):
    _write_log(tmp_path, attempt=None, lines=None)
    (tmp_path / "logs" / "98" / STARTED_DASH / "agent_output.json").unlink(
        missing_ok=True
    )
    payload = _run_log_script(tmp_path, capsys, monkeypatch, ["98", STARTED_DASH])
    assert payload["layout"] == "attempt-log/v1"
    assert payload["attempt"] is None
    assert payload["events"] == []


def test_log_script_missing_logs_is_unknown_layout(tmp_path, capsys, monkeypatch):
    payload = _run_log_script(tmp_path, capsys, monkeypatch, ["98", STARTED_DASH])
    assert payload["layout"] == "unknown"


def test_log_script_rejects_bad_reference_without_a_traceback(
    tmp_path, capsys, monkeypatch
):
    (tmp_path / "logs").mkdir()
    for bad in ("../state/attempt.json", "..", "not-a-timestamp"):
        payload = _run_log_script(tmp_path, capsys, monkeypatch, ["98", bad])
        assert payload["layout"] == "unknown"


def test_log_script_truncates_past_4mib(tmp_path, capsys, monkeypatch):
    lines = [_line("tool_call", f"Bash [main] -> {i:04d}_tool_call.json") for i in range(30000)]
    _write_log(tmp_path, attempt={"outcome": "complete"}, lines=lines)
    payload = _run_log_script(tmp_path, capsys, monkeypatch, ["98", STARTED_DASH])
    assert payload["layout"] == "attempt-log/v1"
    assert payload["truncated"] is True
    assert payload["file_size"] > 4 * 1024 * 1024
    assert payload["total_events"] == len(payload["events"])
    assert payload["total_events"] < 30000


# --- gateway argv, validation, timeout, semaphore ----------------------------


def test_attempt_log_argv_pinned():
    assert argv_attempt_log("abc123", 98, STARTED_COLON) == [
        "docker",
        "exec",
        "--user",
        AGENT_USER,
        "abc123",
        "python",
        "-c",
        gateway_module.ATTEMPT_LOG_SCRIPT,
        "98",
        STARTED_DASH,
    ]


def test_attempt_log_argv_accepts_dash_started():
    assert argv_attempt_log("abc123", "98", STARTED_DASH)[-2:] == ["98", STARTED_DASH]


@pytest.mark.parametrize(
    "issue,started",
    [
        ("..", STARTED_DASH),
        ("98/..", STARTED_DASH),
        ("%2e%2e", STARTED_DASH),
        ("", STARTED_DASH),
        ("-1", STARTED_DASH),
        ("ninety-eight", STARTED_DASH),
        ("98\n rm -rf", STARTED_DASH),
        (98, ".."),
        (98, "/"),
        (98, "%2e%2e"),
        (98, ""),
        (98, "-2026-09-27T16-33-43.385090Z"),
        (98, "../state/attempt.json"),
        (98, "not-a-timestamp"),
        (98, "2026-09-27T16:33:43.385090Z\n injected"),
    ],
)
def test_attempt_log_argv_rejects_traversal_shaped_refs(issue, started):
    with pytest.raises(ValueError):
        argv_attempt_log("abc123", issue, started)


def test_attempt_log_rejects_bad_container_id():
    with pytest.raises(ValueError):
        argv_attempt_log("not a container!", 98, STARTED_DASH)


def test_attempt_log_validation_happens_before_any_spawn(monkeypatch):
    def explode(*argv, **kwargs):
        raise AssertionError("gateway spawned a process for an invalid reference")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", explode)
    for issue, started in [("..", STARTED_DASH), (98, ".."), ("", "")]:
        with pytest.raises(ValueError):
            asyncio.run(DockerGateway().attempt_log("abc123", issue, started))


class _FakeProcess:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self):
        return self._stdout, self._stderr

    def kill(self):
        pass

    async def wait(self):
        return self.returncode


def _patch_create_subprocess(monkeypatch, process=None, *, exc=None):
    seen = {}

    async def fake_create(*argv, **kwargs):
        seen["argv"] = list(argv)
        seen["kwargs"] = kwargs
        if exc is not None:
            raise exc
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    return seen


def test_attempt_log_uses_pinned_argv(monkeypatch):
    proc = _FakeProcess(stdout=b"{}", returncode=0)
    seen = _patch_create_subprocess(monkeypatch, proc)
    result = asyncio.run(DockerGateway().attempt_log("abc123", 98, STARTED_COLON))
    assert seen["argv"] == argv_attempt_log("abc123", 98, STARTED_COLON)
    assert "shell" not in seen["kwargs"]
    assert result.stdout == b"{}"
    assert result.returncode == 0


def test_attempt_log_is_bounded_by_log_timeout_not_exec_timeout(monkeypatch):
    killed = []
    reaped = []

    class SlowProcess(_FakeProcess):
        async def communicate(self):
            await asyncio.sleep(10)
            return b"", b""

        def kill(self):
            killed.append(True)

        async def wait(self):
            reaped.append(True)
            return -9

    async def fake_create(*argv, **kwargs):
        return SlowProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    with pytest.raises(AttemptLogTimeoutError, match="0.05 s"):
        asyncio.run(
            DockerGateway(timeout=5.0, log_timeout=0.05).attempt_log(
                "abc123", 98, STARTED_DASH
            )
        )
    assert killed == [True]
    assert reaped == [True]


def test_attempt_log_missing_cli(monkeypatch):
    _patch_create_subprocess(monkeypatch, exc=FileNotFoundError("no docker"))
    from agent_dashboard.gateway import DockerNotAvailableError

    with pytest.raises(DockerNotAvailableError):
        asyncio.run(DockerGateway().attempt_log("abc123", 98, STARTED_DASH))


def test_attempt_log_semaphore_bounds_concurrent_reads(monkeypatch):
    in_flight = 0
    peak = 0

    class SlowProcess(_FakeProcess):
        async def communicate(self):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            try:
                await asyncio.sleep(0.02)
                return b"{}", b""
            finally:
                in_flight -= 1

    async def fake_create(*argv, **kwargs):
        return SlowProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    async def main():
        gw = DockerGateway()
        await asyncio.gather(
            *[
                gw.attempt_log("abc123", 98, STARTED_DASH)
                for _ in range(8)
            ]
        )

    asyncio.run(main())
    assert peak <= 4
    assert peak > 1


def test_gateway_rejects_bad_log_timeout():
    with pytest.raises(ValueError):
        DockerGateway(log_timeout=0)


# --- settings ----------------------------------------------------------------


def test_log_exec_timeout_default_and_env_and_cli(monkeypatch):
    monkeypatch.delenv("AGENT_DASHBOARD_LOG_EXEC_TIMEOUT", raising=False)
    assert load_settings([]).log_exec_timeout == DEFAULT_LOG_EXEC_TIMEOUT == 15.0
    monkeypatch.setenv("AGENT_DASHBOARD_LOG_EXEC_TIMEOUT", "9")
    assert load_settings([]).log_exec_timeout == 9
    assert load_settings(["--log-exec-timeout", "7"]).log_exec_timeout == 7
    with pytest.raises(ValueError):
        load_settings(["--log-exec-timeout", "0"])


# --- page --------------------------------------------------------------------


def make_log_client(rows, logs=None, logs_error=None):
    state = FleetState()
    instances = []
    for name, container_state in rows:
        raw = "running" if container_state == "running" else "exited"
        instances.append(ManagedInstance(name, f"id-{name}", raw))
    asyncio.run(state.apply_cycle(instances, {}, now=1000.0))
    gateway = FakeGateway()
    for container_id, payload in (logs or {}).items():
        gateway.set_log(container_id, payload)
    if logs_error is not None:
        for container_id, error in logs_error.items():
            gateway.set_log_error(container_id, error)
    app = create_app(
        state=state,
        settings=load_settings([]),
        gateway=gateway,
        start_poller=False,
    )
    return TestClient(app, base_url="http://localhost"), state, gateway


def test_log_page_renders_header_and_paged_table():
    client, _, gateway = make_log_client(
        [("work", "running")],
        {
            "id-work": _payload(
                [TOOL_CALL_NEW, TOOL_RESULT_SUBAGENT, MODEL_RESPONSE]
            )
        },
    )
    response = client.get(f"/instances/work/attempts/98/{STARTED_DASH}")
    assert response.status_code == 200
    body = response.text
    assert "Attempt log" in body
    assert "Add a widget" in body
    assert "complete" in body
    assert STARTED_COLON in body
    assert "28m 32s" in body
    assert "$1.50" in body
    assert "0.5.1" in body
    assert "#12" in body
    assert "https://example.test/pr/12" in body
    assert "page 1/1, 3 events" in body
    assert "tool_call" in body
    # Evidence rows show the tool name, the thread tag, the file name and
    # the open action, with a slot for the inline preview under the row.
    assert '<span class="ev-tool">Bash</span>' in body
    assert '<span class="ev-thread">[main]</span>' in body
    assert '<span class="ev-file">0003_tool_call.json</span>' in body
    assert ">open<" in body
    assert '<span class="ev-thread">[subagent:abc123]</span>' in body
    assert '<span class="ev-file">0001_model_response.txt</span>' in body
    assert ">reply<" in body
    assert 'id="evidence-slot-1"' in body
    assert "toggleEvidence" in body
    assert "<th>#</th>" in body
    assert "Progress chart" in body
    assert f"/instances/work/attempts/98/{STARTED_DASH}/progress" in body
    assert "Refresh" in body
    assert gateway.log_calls == [("id-work", "98", STARTED_DASH)]


def test_log_page_paging_shows_200_row_slices():
    events = [
        _ev("tool_call", f"Bash [main] -> {i:04d}_tool_call.json")
        for i in range(450)
    ]
    client, _, _ = make_log_client([("work", "running")], {"id-work": _payload(events)})
    first = client.get(f"/instances/work/attempts/98/{STARTED_DASH}").text
    assert "page 1/3, 450 events" in first
    assert "-&gt; 0000_tool_call.json" in first or "0000_tool_call.json" in first
    assert "0449_tool_call.json" not in first
    second = client.get(f"/instances/work/attempts/98/{STARTED_DASH}?page=2").text
    assert "page 2/3, 450 events" in second
    assert "0199_tool_call.json" not in second
    assert "0200_tool_call.json" in second
    assert "0399_tool_call.json" in second
    assert "0400_tool_call.json" not in second
    assert "&larr; prev" in second
    last = client.get(f"/instances/work/attempts/98/{STARTED_DASH}?page=99").text
    assert "page 3/3, 450 events" in last
    assert "0449_tool_call.json" in last


def test_log_page_pages_a_929kb_stream():
    # The largest real stream measured 929 KB: paging must hold at that size.
    events = [
        _ev(
            "tool_call",
            f"Bash [main] -> {i:04d}_tool_call.json padding={'x' * 180}",
        )
        for i in range(3000)
    ]
    payload = _payload(events)
    assert len(json.dumps(payload).encode()) > 900 * 1024
    client, _, _ = make_log_client([("work", "running")], {"id-work": payload})
    body = client.get(f"/instances/work/attempts/98/{STARTED_DASH}?page=15").text
    assert "page 15/15, 3000 events" in body
    assert "2999_tool_call.json" in body
    assert "0000_tool_call.json" not in body


def test_log_page_renders_tolerance_rows():
    client, _, _ = make_log_client(
        [("work", "running")],
        {
            "id-work": _payload(
                [TOOL_CALL_NEW, UNKNOWN_EVENT, UNKNOWN_LEVEL, "not json{"]
            )
        },
    )
    body = client.get(f"/instances/work/attempts/98/{STARTED_DASH}").text
    assert "unparsable_line" in body
    assert "WARNING" in body
    assert "token_budget_reconciled" in body
    assert "DEBUG" in body
    # Unknown levels render unstyled: no level class for DEBUG.
    assert "lvl-DEBUG" not in body
    assert "lvl-INFO" in body


def test_log_page_missing_attempt_json_is_a_partial_page():
    client, _, _ = make_log_client(
        [("work", "running")], {"id-work": _payload([TOOL_CALL_NEW], attempt=None)}
    )
    response = client.get(f"/instances/work/attempts/98/{STARTED_DASH}")
    assert response.status_code == 200
    assert NO_ATTEMPT_JSON in response.text
    assert "tool_call" in response.text


def test_log_page_missing_stream_is_layout_not_recognised_with_header():
    client, _, _ = make_log_client(
        [("work", "running")], {"id-work": _payload([])}
    )
    response = client.get(f"/instances/work/attempts/98/{STARTED_DASH}")
    assert response.status_code == 200
    assert "layout not recognised" in response.text
    assert "Add a widget" in response.text


def test_log_page_unknown_layout_never_claims_missing_attempt_json():
    payload = _payload([TOOL_CALL_NEW], layout="bogus-layout/v9")
    client, _, _ = make_log_client(
        [("work", "running")], {"id-work": payload}
    )
    response = client.get(f"/instances/work/attempts/98/{STARTED_DASH}")
    assert response.status_code == 200
    assert "layout not recognised" in response.text
    assert NO_ATTEMPT_JSON not in response.text


def test_log_page_truncated_stream_shows_notice():
    events = [_ev("tool_call", f"detail {i}") for i in range(10)]
    client, _, _ = make_log_client(
        [("work", "running")],
        {
            "id-work": _payload(
                events, truncated=True, file_size=5000000, total_events=10
            )
        },
    )
    body = client.get(f"/instances/work/attempts/98/{STARTED_DASH}").text
    assert "event stream truncated: showing the first 10 events of a 4.8 MB file" in body


def test_log_page_unknown_and_stopped_instances():
    client, _, _ = make_log_client([("work", "running")])
    unknown = client.get(f"/instances/nope/attempts/98/{STARTED_DASH}")
    assert unknown.status_code == 200
    assert "unknown" in unknown.text.lower()
    stopped, _, _ = make_log_client([("down", "not running")])
    body = stopped.get(f"/instances/down/attempts/98/{STARTED_DASH}").text
    assert "running managed instances only" in body


def test_log_page_invalid_reference_and_gateway_error_make_no_500():
    client, _, gateway = make_log_client([("work", "running")])
    bad = client.get("/instances/work/attempts/98/not-a-timestamp")
    assert bad.status_code == 200
    assert "Invalid attempt reference" in bad.text
    assert gateway.log_calls == []
    traversal = client.get("/instances/work/attempts/%2E%2E/" + STARTED_DASH)
    assert traversal.status_code == 200
    assert "Invalid attempt reference" in traversal.text
    assert gateway.log_calls == []
    err_client, _, _ = make_log_client(
        [("work", "running")],
        logs_error={"id-work": GatewayError("docker exec timed out after 15 s")},
    )
    response = err_client.get(f"/instances/work/attempts/98/{STARTED_DASH}")
    assert response.status_code == 200
    assert "Could not read" in response.text


def test_log_page_accepts_colon_form_started():
    client, _, gateway = make_log_client(
        [("work", "running")], {"id-work": _payload([TOOL_CALL_NEW])}
    )
    response = client.get(f"/instances/work/attempts/98/{STARTED_COLON}")
    assert response.status_code == 200
    assert "tool_call" in response.text
    assert gateway.log_calls == [("id-work", "98", STARTED_DASH)]


def test_history_rows_link_to_the_attempt_log_page():
    from test_history import make_history_client

    payload = {
        "layout": "attempt-logs/v1",
        "attempts": [
            {
                "issue": 98,
                "started": STARTED_DASH,
                "attempt": {
                    "outcome": "complete",
                    "started_at": STARTED_COLON,
                    "duration_seconds": 60,
                },
            }
        ],
        "active_started_at": None,
        "unreadable": 0,
    }
    client, _, _ = make_history_client([("work", "running")], {"id-work": payload})
    body = client.get("/instances/work/history?window=all").text
    assert f"/instances/work/attempts/98/{STARTED_DASH}" in body


def test_progress_page_links_to_the_attempt_log_page():
    from test_progress import _progress_payload, _reading_event, make_progress_client

    client, _, _ = make_progress_client(
        [("work", "running")],
        {"id-work": _progress_payload([_reading_event(1, 8, "2026-09-27T16:33:51Z")])},
    )
    body = client.get(f"/instances/work/attempts/98/{STARTED_DASH}/progress").text
    assert f"/instances/work/attempts/98/{STARTED_DASH}" in body
    assert "Attempt log" in body
