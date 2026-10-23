"""Attempt history: summaries of one managed instance's implementation attempts.

Covers summary parsing (complete, no-usage, no-outcome, malformed), the
gateway argv validation, and the history page (running only, static with a
time window filter). Vocabulary: Attempt log, Managed instance, Reach.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from fake_gateway import FakeGateway
from fastapi.testclient import TestClient

from agent_dashboard import gateway as gateway_module
from agent_dashboard.app import create_app
from agent_dashboard.config import Settings
from agent_dashboard.fleet import FleetState
from agent_dashboard.gateway import (
    DockerGateway,
    DockerNotAvailableError,
    GatewayError,
    ManagedInstance,
)
from agent_dashboard.history import (
    HISTORY_LAYOUT,
    format_cost,
    format_duration,
    read_history,
)


def _payload(attempts, active_started_at=None, unreadable=0, layout=HISTORY_LAYOUT):
    return {
        "layout": layout,
        "attempts": attempts,
        "active_started_at": active_started_at,
        "unreadable": unreadable,
    }


def _entry(issue, started, attempt):
    return {"issue": issue, "started": started, "attempt": attempt}


COMPLETE = {
    "outcome": "complete",
    "started_at": "2026-09-27T16:33:43.385090Z",
    "completed_at": "2026-09-27T17:02:11.000000Z",
    "duration_seconds": 1712.5,
    "model_usage": {
        "claude-x": {
            "input_tokens": 120000,
            "cache_read_input_tokens": 350000,
            "cache_creation_input_tokens": 12000,
            "output_tokens": 42000,
            "cost_usd": 1.23456,
        }
    },
    "model_stop_reason": "end_turn",
    "issue_title": "Add a widget",
}

NOW = 1_790_000_000.0


def _stdout(payload, returncode=0):
    return json.dumps(payload).encode(), returncode


# --- summary parsing -------------------------------------------------------


def test_complete_attempt_parses_every_field():
    stdout, code = _stdout(_payload([_entry(98, "2026-09-27T16-33-43.385090Z", COMPLETE)]))
    result = read_history(stdout, code, now=NOW)
    assert result.ok and result.error is None
    assert result.total == 1
    (summary,) = result.summaries
    assert summary.issue == 98
    assert summary.issue_title == "Add a widget"
    assert summary.started_at == "2026-09-27T16:33:43.385090Z"
    assert summary.duration_text == "28m 32s"
    assert summary.outcome_text == "complete"
    assert summary.active is False
    assert summary.stop_reason_text == "end_turn"
    assert summary.cost_text == "$1.23"
    assert summary.input_text == "120,000"
    assert summary.cache_read_text == "350,000"
    assert summary.cache_creation_text == "12,000"
    assert summary.output_text == "42,000"
    assert summary.has_usage is True


def test_usage_numeric_values_sum_across_models():
    attempt = {
        "outcome": "complete",
        "started_at": "2026-09-27T16:33:43.385090Z",
        "duration_seconds": 60,
        "model_usage": {
            "claude-a": {"input_tokens": 100, "cache_read_input_tokens": 30},
            "claude-b": {"input_tokens": 200, "output_tokens": 20},
        },
        "model_stop_reason": "end_turn",
    }
    stdout, code = _stdout(_payload([_entry(1, "2026-09-27T16-33-43.385090Z", attempt)]))
    (summary,) = read_history(stdout, code, now=NOW).summaries
    assert summary.input_value == 300
    assert summary.cache_read_value == 30
    assert summary.cache_creation_value is None
    assert summary.output_value == 20


def test_usage_numeric_values_missing_without_usage():
    attempt = {
        "outcome": "infrastructure_error",
        "started_at": "2026-09-27T16:33:43.385090Z",
        "duration_seconds": 17,
    }
    stdout, code = _stdout(_payload([_entry(5, "2026-09-27T16-33-43.385090Z", attempt)]))
    (summary,) = read_history(stdout, code, now=NOW).summaries
    assert summary.input_value is None
    assert summary.cache_read_value is None
    assert summary.cache_creation_value is None
    assert summary.output_value is None


def test_usage_sums_across_models():
    attempt = {
        "outcome": "complete",
        "started_at": "2026-09-27T16:33:43.385090Z",
        "duration_seconds": 60,
        "model_usage": {
            "claude-a": {"input_tokens": 100, "output_tokens": 10, "cost_usd": 0.01},
            "claude-b": {"input_tokens": 200, "output_tokens": 20, "cost_usd": 0.02},
        },
        "model_stop_reason": "end_turn",
    }
    stdout, code = _stdout(_payload([_entry(1, "2026-09-27T16-33-43.385090Z", attempt)]))
    (summary,) = read_history(stdout, code, now=NOW).summaries
    assert summary.input_text == "300"
    assert summary.output_text == "30"
    assert summary.cost_text == "$0.03"
    # Categories absent everywhere render as n/a, never zero.
    assert summary.cache_read_text == "n/a"
    assert summary.cache_creation_text == "n/a"


def test_no_usage_data_renders_na_never_zero():
    attempt = {
        "outcome": "infrastructure_error",
        "started_at": "2026-09-27T16:33:43.385090Z",
        "completed_at": "2026-09-27T16:34:00.000000Z",
        "duration_seconds": 17,
    }
    stdout, code = _stdout(_payload([_entry(5, "2026-09-27T16-33-43.385090Z", attempt)]))
    (summary,) = read_history(stdout, code, now=NOW).summaries
    assert summary.outcome_text == "infrastructure_error"
    assert summary.has_usage is False
    assert summary.cost_text == "n/a"
    assert summary.input_text == "n/a"
    assert summary.cache_read_text == "n/a"
    assert summary.cache_creation_text == "n/a"
    assert summary.output_text == "n/a"
    assert summary.stop_reason_text == "n/a"


def test_zero_cost_with_usage_is_shown_not_na():
    attempt = {
        "outcome": "complete",
        "started_at": "2026-09-27T16:33:43.385090Z",
        "duration_seconds": 5,
        "model_usage": {"m": {"input_tokens": 10, "cost_usd": 0}},
        "model_stop_reason": "end_turn",
    }
    stdout, code = _stdout(_payload([_entry(1, "2026-09-27T16-33-43.385090Z", attempt)]))
    (summary,) = read_history(stdout, code, now=NOW).summaries
    assert summary.cost_text == "$0.00"
    assert summary.input_text == "10"


def test_no_outcome_matching_checkpoint_is_active():
    started = "2026-10-16T10:00:00.000000Z"
    attempt = {"started_at": started}
    payload = _payload(
        [_entry(7, "2026-10-16T10-00-00.000000Z", attempt)],
        active_started_at=started,
    )
    stdout, code = _stdout(payload)
    (summary,) = read_history(stdout, code, now=NOW).summaries
    assert summary.outcome_text == "active"
    assert summary.active is True
    # Active duration is elapsed time, not n/a.
    assert summary.duration_text != "n/a"
    assert summary.cost_text == "n/a"


def test_no_outcome_without_matching_checkpoint_is_interrupted():
    attempt = {"started_at": "2026-10-16T10:00:00.000000Z"}
    payload = _payload(
        [_entry(7, "2026-10-16T10-00-00.000000Z", attempt)],
        active_started_at="2026-10-17T08:00:00.000000Z",
    )
    stdout, code = _stdout(payload)
    (summary,) = read_history(stdout, code, now=NOW).summaries
    assert summary.outcome_text == "interrupted"
    assert summary.active is False
    assert summary.duration_text == "n/a"


def test_no_outcome_without_checkpoint_is_interrupted():
    attempt = {"started_at": "2026-10-16T10:00:00.000000Z"}
    stdout, code = _stdout(_payload([_entry(7, "2026-10-16T10-00-00.000000Z", attempt)]))
    (summary,) = read_history(stdout, code, now=NOW).summaries
    assert summary.outcome_text == "interrupted"


def test_empty_outcome_counts_as_missing():
    attempt = {"outcome": "", "started_at": "2026-10-16T10:00:00.000000Z"}
    stdout, code = _stdout(_payload([_entry(7, "2026-10-16T10-00-00.000000Z", attempt)]))
    (summary,) = read_history(stdout, code, now=NOW).summaries
    assert summary.outcome_text == "interrupted"


def test_started_at_falls_back_to_directory_name():
    attempt = {"outcome": "complete", "duration_seconds": 10}
    stdout, code = _stdout(_payload([_entry(3, "2026-09-27T16-33-43.385090Z", attempt)]))
    (summary,) = read_history(stdout, code, now=NOW).summaries
    assert summary.started_at == "2026-09-27T16:33:43.385090Z"
    assert summary.started_ts is not None


def test_summaries_sort_newest_first():
    attempts = [
        _entry(1, "2026-09-20T10-00-00.000000Z", {"outcome": "complete"}),
        _entry(2, "2026-09-27T10-00-00.000000Z", {"outcome": "complete"}),
        _entry(3, "2026-09-25T10-00-00.000000Z", {"outcome": "complete"}),
    ]
    stdout, code = _stdout(_payload(attempts))
    result = read_history(stdout, code, now=NOW)
    assert [s.issue for s in result.summaries] == [2, 3, 1]


def test_malformed_stdout_is_layout_not_recognised():
    for stdout in (b"not json at all", b'["a list"]', b'{"layout": "other"}', b""):
        result = read_history(stdout, 0, now=NOW)
        assert result.ok is False
        assert result.error == "layout not recognised"
        assert result.summaries == ()


def test_missing_layout_key_is_layout_not_recognised():
    result = read_history(b'{"attempts": []}', 0, now=NOW)
    assert result.ok is False
    assert result.error == "layout not recognised"


def test_attempts_must_be_a_list():
    result = read_history(json.dumps(_payload({"nope": 1})).encode(), 0, now=NOW)
    assert result.ok is False
    assert result.error == "layout not recognised"


def test_bad_entries_are_skipped_and_counted():
    payload = _payload(
        [
            _entry(1, "2026-09-27T10-00-00.000000Z", COMPLETE),
            {"issue": "xx", "started": "bad", "attempt": {}},
            {"issue": 2, "started": "2026-09-27T10-00-00.000000Z", "attempt": [1, 2]},
            "a string entry",
        ],
        unreadable=2,
    )
    stdout, code = _stdout(payload)
    result = read_history(stdout, code, now=NOW)
    assert result.ok is True
    assert result.total == 1
    assert result.unreadable == 2 + 3


def test_duration_value_parsed_for_scatter():
    stdout, code = _stdout(_payload([_entry(98, "2026-09-27T16-33-43.385090Z", COMPLETE)]))
    (summary,) = read_history(stdout, code, now=NOW).summaries
    assert summary.duration_value == 1712.5


def test_active_duration_value_is_elapsed():
    started = "2026-10-16T10:00:00.000000Z"
    attempt = {"started_at": started}
    payload = _payload(
        [_entry(7, "2026-10-16T10-00-00.000000Z", attempt)],
        active_started_at=started,
    )
    stdout, code = _stdout(payload)
    (summary,) = read_history(stdout, code, now=NOW).summaries
    assert summary.duration_value is not None and summary.duration_value >= 0


def test_missing_duration_value_is_none():
    attempt = {"outcome": "interrupted", "started_at": "2026-10-16T10:00:00.000000Z"}
    stdout, code = _stdout(_payload([_entry(7, "2026-10-16T10-00-00.000000Z", attempt)]))
    (summary,) = read_history(stdout, code, now=NOW).summaries
    assert summary.duration_value is None
    assert summary.duration_text == "n/a"


def test_cost_threshold_parsed_when_reported():
    attempt = dict(
        COMPLETE,
        cost_soft_threshold_usd=2.5,
        started_at="2026-09-27T16:33:43.385090Z",
    )
    stdout, code = _stdout(_payload([_entry(98, "2026-09-27T16-33-43.385090Z", attempt)]))
    (summary,) = read_history(stdout, code, now=NOW).summaries
    assert summary.cost_threshold_value == 2.5


def test_cost_threshold_accepts_known_key_variants():
    for key in ("cost_soft_threshold", "soft_threshold_usd", "cost_threshold_usd"):
        attempt = dict(
            COMPLETE, started_at="2026-09-27T16:33:43.385090Z", **{key: 3.0}
        )
        stdout, code = _stdout(
            _payload([_entry(98, "2026-09-27T16-33-43.385090Z", attempt)])
        )
        (summary,) = read_history(stdout, code, now=NOW).summaries
        assert summary.cost_threshold_value == 3.0, key


def test_cost_threshold_missing_is_none():
    stdout, code = _stdout(_payload([_entry(98, "2026-09-27T16-33-43.385090Z", COMPLETE)]))
    (summary,) = read_history(stdout, code, now=NOW).summaries
    assert summary.cost_threshold_value is None


def test_cost_threshold_ignores_unusable_values():
    for bad in (0, -1.5, "2.50", True, None):
        attempt = dict(
            COMPLETE,
            started_at="2026-09-27T16:33:43.385090Z",
            cost_soft_threshold_usd=bad,
        )
        stdout, code = _stdout(
            _payload([_entry(98, "2026-09-27T16-33-43.385090Z", attempt)])
        )
        (summary,) = read_history(stdout, code, now=NOW).summaries
        assert summary.cost_threshold_value is None, bad


def test_stop_reason_dict_is_joined():
    attempt = {
        "outcome": "complete",
        "started_at": "2026-09-27T16:33:43.385090Z",
        "duration_seconds": 5,
        "model_usage": {"m": {"input_tokens": 1}},
        "model_stop_reason": {"m": "end_turn"},
    }
    stdout, code = _stdout(_payload([_entry(1, "2026-09-27T16-33-43.385090Z", attempt)]))
    (summary,) = read_history(stdout, code, now=NOW).summaries
    assert summary.stop_reason_text == "end_turn"


# --- time window filter ----------------------------------------------------


def _aged_attempt(days_ago, issue, now):
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - days_ago * 86400))
    return _entry(issue, started.replace(":", "-"), {"outcome": "complete", "started_at": started})


def test_window_filters_by_started_at():
    attempts = [_aged_attempt(1, 1, NOW), _aged_attempt(10, 2, NOW), _aged_attempt(40, 3, NOW)]
    stdout, code = _stdout(_payload(attempts))
    week = read_history(stdout, code, now=NOW, window="7").summaries
    month = read_history(stdout, code, now=NOW, window="30").summaries
    everything = read_history(stdout, code, now=NOW, window="all").summaries
    assert [s.issue for s in week] == [1]
    assert [s.issue for s in month] == [1, 2]
    assert [s.issue for s in everything] == [1, 2, 3]
    assert read_history(stdout, code, now=NOW, window="all").total == 3
    assert read_history(stdout, code, now=NOW, window="7").total == 3


def test_unknown_window_means_all():
    attempts = [_aged_attempt(40, 3, NOW)]
    stdout, code = _stdout(_payload(attempts))
    assert len(read_history(stdout, code, now=NOW, window="forever").summaries) == 1


def test_unparseable_started_at_always_shown():
    attempts = [_entry(9, "2026-09-27T10-00-00.000000Z", {"outcome": "complete"})]
    stdout, code = _stdout(_payload(attempts))
    assert len(read_history(stdout, code, now=NOW, window="7").summaries) == 1


# --- formatting ------------------------------------------------------------


def test_format_duration():
    assert format_duration(45) == "45s"
    assert format_duration(1712.5) == "28m 32s"
    assert format_duration(7320) == "2h 02m"
    assert format_duration(90000) == "1d 1h"


def test_format_cost():
    assert format_cost(1.23456) == "$1.23"
    assert format_cost(0.004321) == "$0.0043"
    assert format_cost(0) == "$0.00"


# --- gateway argv ----------------------------------------------------------


def test_history_argv_pinned():
    assert gateway_module.argv_attempt_history("abc123") == [
        "docker",
        "exec",
        "--user",
        "agent",
        "abc123",
        "python",
        "-c",
        gateway_module.HISTORY_SCRIPT,
    ]


@pytest.mark.parametrize(
    "bad",
    ["", "a b", "../x", "a;b", "a/b", "worker-1", "abc$", "$()", "a" * 65, "écatura"],
)
def test_history_rejects_unvalidated_container_ids(bad):
    with pytest.raises(ValueError, match="invalid container id"):
        gateway_module.argv_attempt_history(bad)


def test_history_script_is_fixed_no_user_values():
    script = gateway_module.HISTORY_SCRIPT
    assert "/data/logs" in script
    assert "attempt-logs/v1" in script
    assert "attempt.json" in script
    assert "container" not in script.lower()


def test_history_uses_pinned_argv_no_shell(monkeypatch):
    from test_gateway import _FakeProcess, _patch_create_subprocess

    proc = _FakeProcess(stdout=b'{"layout": "attempt-logs/v1", "attempts": []}', returncode=0)
    seen = _patch_create_subprocess(monkeypatch, proc)
    result = asyncio.run(DockerGateway(timeout=5.0).attempt_history("abc123"))
    assert seen["argv"] == gateway_module.argv_attempt_history("abc123")
    assert "shell" not in seen["kwargs"]
    assert result.returncode == 0


def test_history_timeout_kills_client_process(monkeypatch):
    from test_gateway import _FakeProcess

    killed = []
    reaped = []

    class SlowProcess(_FakeProcess):
        async def communicate(self):
            import asyncio as _asyncio

            await _asyncio.sleep(10)
            return b"", b""

        def kill(self):
            killed.append(True)

        async def wait(self):
            reaped.append(True)
            return -9

    async def fake_create(*argv, **kwargs):
        return SlowProcess()

    import asyncio as _asyncio_mod

    monkeypatch.setattr(_asyncio_mod, "create_subprocess_exec", fake_create)
    from agent_dashboard.gateway import TelemetryTimeoutError

    with pytest.raises(TelemetryTimeoutError, match="timed out"):
        asyncio.run(DockerGateway(timeout=0.05).attempt_history("abc123"))
    assert killed == [True]
    assert reaped == [True]


def test_history_missing_cli(monkeypatch):
    from test_gateway import _patch_create_subprocess

    _patch_create_subprocess(monkeypatch, exc=FileNotFoundError("no docker"))
    with pytest.raises(DockerNotAvailableError):
        asyncio.run(DockerGateway(timeout=5.0).attempt_history("abc123"))


# --- collector script --------------------------------------------------------


def _run_collector_script(root, capsys, monkeypatch):
    """Execute the shipped HISTORY_SCRIPT with /data remapped onto tmp dirs."""
    import os as real_os

    def remap(path):
        text = path if isinstance(path, str) else real_os.fspath(path)
        if text == "/data/logs":
            return str(root / "logs")
        if text.startswith("/data/logs/"):
            return str(root / "logs" / text[len("/data/logs/") :])
        if text == "/data/state/attempt.json":
            return str(root / "state" / "attempt.json")
        return text

    real_open = open
    real_listdir = real_os.listdir
    real_isdir = real_os.path.isdir
    real_isfile = real_os.path.isfile

    def fake_open(path, *args, **kwargs):
        return real_open(remap(path), *args, **kwargs)

    monkeypatch.setattr(real_os, "listdir", lambda p: real_listdir(remap(p)))
    monkeypatch.setattr(real_os.path, "isdir", lambda p: real_isdir(remap(p)))
    monkeypatch.setattr(real_os.path, "isfile", lambda p: real_isfile(remap(p)))
    monkeypatch.setattr("builtins.open", fake_open)
    namespace: dict = {"__name__": "__collector_test__"}
    try:
        exec(gateway_module.HISTORY_SCRIPT, namespace)  # noqa: S102
    except SystemExit as exc:
        # The missing-layout branch prints its document, then exits 0.
        assert exc.code == 0
    return json.loads(capsys.readouterr().out)


def test_history_script_collects_attempts(tmp_path, capsys, monkeypatch):
    logs = tmp_path / "logs"
    done = logs / "98" / "2026-09-27T16-33-43.385090Z"
    done.mkdir(parents=True)
    (done / "attempt.json").write_text(json.dumps(COMPLETE))
    (logs / "98" / "not-a-timestamp").mkdir()  # skipped: not an attempt dir
    other = logs / "abc" / "2026-09-27T16-33-43.385090Z"
    other.mkdir(parents=True)  # skipped: issue is not digits
    broken = logs / "99" / "2026-09-27T16-33-43.385090Z"
    broken.mkdir(parents=True)
    (broken / "attempt.json").write_text("not json{")
    state = tmp_path / "state"
    state.mkdir()
    (state / "attempt.json").write_text(
        json.dumps({"started_at": "2026-09-27T16:33:43.385090Z"})
    )
    payload = _run_collector_script(tmp_path, capsys, monkeypatch)
    assert payload["layout"] == HISTORY_LAYOUT
    assert payload["active_started_at"] == "2026-09-27T16:33:43.385090Z"
    assert payload["unreadable"] == 1
    # A broken attempt.json stays listed (attempt None) so its Attempt log
    # stays reachable, instead of only being counted as unreadable.
    assert len(payload["attempts"]) == 2
    by_issue = {entry["issue"]: entry for entry in payload["attempts"]}
    assert by_issue[98]["attempt"]["outcome"] == "complete"
    assert by_issue[98]["started"] == "2026-09-27T16-33-43.385090Z"
    assert by_issue[99]["attempt"] is None
    assert by_issue[99]["has_events"] is False


def test_history_script_missing_logs_is_unknown_layout(tmp_path, capsys, monkeypatch):
    (tmp_path / "state").mkdir()
    payload = _run_collector_script(tmp_path, capsys, monkeypatch)
    assert payload["layout"] == "unknown"
    result = read_history(json.dumps(payload).encode(), 0, now=NOW)
    assert result.ok is False
    assert result.error == "layout not recognised"


# --- history page ----------------------------------------------------------


def _history_payload_old_and_recent():
    now = time.time()
    recent = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 86400))
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 40 * 86400))
    recent_attempt = dict(COMPLETE, started_at=recent, duration_seconds=60)
    old_attempt = {"outcome": "infrastructure_error", "started_at": old, "duration_seconds": 17}
    return {
        "layout": HISTORY_LAYOUT,
        "attempts": [
            {"issue": 98, "started": recent.replace(":", "-"), "attempt": recent_attempt},
            {"issue": 41, "started": old.replace(":", "-"), "attempt": old_attempt},
        ],
        "active_started_at": None,
        "unreadable": 0,
    }


def make_history_client(rows, histories=None, error=None, logs_trees=None):
    """A client with fleet rows plus scripted attempt-history payloads per id."""
    state = FleetState()
    instances = []
    for name, container_state in rows:
        raw = "running" if container_state == "running" else "exited"
        instances.append(ManagedInstance(name, f"id-{name}", raw))
    asyncio.run(state.apply_cycle(instances, {}, now=1000.0))
    if error is not None:
        asyncio.run(state.set_discovery_error(error))
    gateway = FakeGateway()
    for container_id, payload in (histories or {}).items():
        gateway.set_history(container_id, payload)
    for container_id, tree in (logs_trees or {}).items():
        gateway.set_logs_tree(container_id, tree)
    app = create_app(
        state=state, settings=Settings(poll_interval=5.0), gateway=gateway, start_poller=False
    )
    return TestClient(app, base_url="http://localhost"), state, gateway


def test_history_page_lists_every_attempt_newest_first():
    client, _, _ = make_history_client(
        [("work", "running")], {"id-work": _history_payload_old_and_recent()}
    )
    response = client.get("/instances/work/history?window=all")
    assert response.status_code == 200
    body = response.text
    for header in (
        "Issue",
        "Started",
        "Duration",
        "Outcome",
        "Model stop reason",
        "Cost",
        "Input",
        "Cache read",
        "Cache creation",
        "Output",
    ):
        assert header in body
    # The table stays newest-first (the timeline above it runs oldest-first).
    tbody = body.split("<tbody>", 1)[1]
    assert tbody.index("#98") < tbody.index("#41")
    assert "complete" in body
    assert "infrastructure_error" in body
    # No-usage attempt renders n/a, never zero (cost chart axes may label $0.00).
    assert "n/a" in body
    tbody_costs = [line for line in tbody.splitlines() if "<td>" in line]
    assert not any("$0.00" in line for line in tbody_costs)
    assert "120,000" in body


def test_history_page_window_filter():
    client, _, _ = make_history_client(
        [("work", "running")], {"id-work": _history_payload_old_and_recent()}
    )
    body_7 = client.get("/instances/work/history?window=7").text
    assert "#98" in body_7
    assert "#41" not in body_7
    body_all = client.get("/instances/work/history?window=all").text
    assert "#41" in body_all
    # Legacy window links keep working.
    assert "#98" in client.get("/instances/work/history?window=30").text
    assert "#41" not in client.get("/instances/work/history?window=30").text
    # Filter links and a manual refresh, no live stream on this static page.
    assert "?preset=7d" in body_all and "?preset=30d" in body_all
    assert "?preset=all" in body_all and "?preset=24h" in body_all
    assert "?preset=90d" in body_all
    assert "Refresh" in body_all
    assert "sse-swap" not in body_all
    assert "sse:fleet" not in body_all


def _history_client_with_aged_attempts():
    now = time.time()

    def aged(days_ago, issue):
        started = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - days_ago * 86400)
        )
        return {
            "issue": issue,
            "started": started.replace(":", "-"),
            "attempt": dict(COMPLETE, started_at=started),
        }

    payload = _payload(
        [
            aged(0.5, 1),
            aged(3, 2),
            aged(10, 3),
            aged(100, 4),
        ]
    )
    client, _, _ = make_history_client([("work", "running")], {"id-work": payload})
    return client, now


def test_history_page_accepts_every_preset():
    client, _ = _history_client_with_aged_attempts()
    assert 'href="/instances/work/history?preset=24h" class="current"' in client.get(
        "/instances/work/history?preset=24h"
    ).text
    body_24h = client.get("/instances/work/history?preset=24h").text
    assert "#1" in body_24h and "#2" not in body_24h
    body_7d = client.get("/instances/work/history?preset=7d").text
    assert "#2" in body_7d and "#3" not in body_7d
    body_30d = client.get("/instances/work/history?preset=30d").text
    assert "#3" in body_30d and "#4" not in body_30d
    body_90d = client.get("/instances/work/history?preset=90d").text
    assert "#3" in body_90d and "#4" not in body_90d
    body_all = client.get("/instances/work/history?preset=all").text
    assert "#4" in body_all
    # The refresh link preserves the selected range.
    assert "history?preset=7d" in body_7d


def test_history_page_accepts_a_custom_date_span():
    import datetime as _datetime

    client, now = _history_client_with_aged_attempts()
    today = _datetime.datetime.fromtimestamp(now, tz=_datetime.UTC).date()
    start = (today - _datetime.timedelta(days=11)).isoformat()
    end = (today - _datetime.timedelta(days=2)).isoformat()
    body = client.get(f"/instances/work/history?from={start}&to={end}").text
    # Only the attempts started inside the inclusive span remain.
    assert "#2" in body and "#3" in body
    assert "#1" not in body and "#4" not in body
    assert f"from={start}&amp;to={end}" in body


def test_history_page_charts_follow_the_range():
    client, _ = _history_client_with_aged_attempts()
    body_7d = client.get("/instances/work/history?preset=7d").text
    body_all = client.get("/instances/work/history?preset=all").text
    # The 30-day-old attempt feeds the all-time charts but not the 7-day ones.
    assert body_all.count("#3") > body_7d.count("#3")
    assert "Showing 2 of 4 attempts" in body_7d
    assert "Showing 4 of 4 attempts" in body_all


def test_history_page_stopped_instance():
    import json as _json

    from fake_gateway import make_logs_tree

    started = "2026-09-27T16-33-43.385090Z"
    attempt = dict(
        COMPLETE, started_at="2026-09-27T16:33:43.385090Z", duration_seconds=60
    )
    tree = make_logs_tree(
        {
            f"logs/98/{started}/attempt.json": _json.dumps(attempt).encode(),
            f"logs/98/{started}/agent_output.json": b"",
        }
    )
    client, _, gateway = make_history_client(
        [("down", "not running")], logs_trees={"id-down": tree}
    )
    body = client.get("/instances/down/history").text
    assert "#98" in body
    assert "complete" in body
    assert "down" in body
    assert gateway.logs_tree_calls == ["id-down"]
    assert gateway.history_calls == []


def test_history_page_stopped_instance_without_tree_is_a_hint():
    client, _, gateway = make_history_client([("down", "not running")])
    body = client.get("/instances/down/history").text
    assert "its volume is gone or it holds no logs" in body
    assert "down" in body
    assert gateway.logs_tree_calls == ["id-down"]
    assert gateway.history_calls == []


def test_history_page_unknown_instance():
    client, _, _ = make_history_client([("work", "running")])
    response = client.get("/instances/nope/history")
    assert response.status_code == 200
    assert "unknown" in response.text.lower()
    assert "nope" in response.text


def test_history_page_empty_log_directory():
    client, _, _ = make_history_client(
        [("work", "running")],
        {"id-work": {"layout": HISTORY_LAYOUT, "attempts": [], "unreadable": 0}},
    )
    body = client.get("/instances/work/history").text
    assert "No attempts" in body


def test_history_page_unknown_layout():
    client, _, _ = make_history_client(
        [("work", "running")], {"id-work": {"layout": "unknown", "attempts": []}}
    )
    response = client.get("/instances/work/history")
    assert response.status_code == 200
    assert "layout not recognised" in response.text


def test_history_page_gateway_error_is_a_message_not_500():
    client, state, gateway = make_history_client([("work", "running")])
    gateway.set_history_error("id-work", GatewayError("docker exec timed out after 5 s"))
    response = client.get("/instances/work/history")
    assert response.status_code == 200
    assert "Could not read" in response.text


def test_history_page_invalid_container_id_is_a_message_not_500():
    client, state, gateway = make_history_client([("work", "running")])
    gateway.set_history_error("id-work", ValueError("invalid container id: 'x'"))
    response = client.get("/instances/work/history")
    assert response.status_code == 200
    assert "Could not read" in response.text


def test_history_page_uses_dashboard_vocabulary():
    client, _, _ = make_history_client(
        [("work", "running")], {"id-work": _history_payload_old_and_recent()}
    )
    body = client.get("/instances/work/history?window=all").text
    assert "Attempt history" in body
    assert "Managed instance" in body
    assert "session" not in body.lower()


def test_drawer_links_to_history():
    from test_web import load_snapshot, make_fleet_client

    client, _, _ = make_fleet_client({"work": load_snapshot("snapshot-attempt-in-progress.json")})
    body = client.get("/instances/work").text
    assert 'href="/instances/work/history"' in body
    assert "Attempt history" in body


def test_history_single_exec_per_page_view():
    client, _, gateway = make_history_client(
        [("work", "running")], {"id-work": _history_payload_old_and_recent()}
    )
    client.get("/instances/work/history")
    assert gateway.history_calls == ["id-work"]


# --- attempt history gaps: missing summaries, PR column, no-events note ------


def test_missing_attempt_json_lists_row_with_no_outcome_status():
    from agent_dashboard.history import IN_PROGRESS_TEXT, NO_OUTCOME_TEXT

    assert NO_OUTCOME_TEXT == "no outcome (crashed or abandoned)"
    assert IN_PROGRESS_TEXT == "in progress"
    payload = _payload(
        [
            _entry(98, "2026-09-27T16-33-43.385090Z", COMPLETE),
            {
                "issue": 99,
                "started": "2026-09-28T10-00-00.000000Z",
                "attempt": None,
                "has_events": True,
            },
        ]
    )
    stdout, code = _stdout(payload)
    result = read_history(stdout, code, now=NOW, window="all")
    assert result.ok
    assert result.total == 2
    by_issue = {s.issue: s for s in result.summaries}
    missing = by_issue[99]
    assert missing.outcome_text == "no outcome (crashed or abandoned)"
    assert missing.active is False
    assert missing.has_summary is False
    assert missing.has_events is True
    assert missing.started_at == "2026-09-28T10:00:00.000000Z"
    assert missing.started_ts is not None
    assert missing.duration_text == "n/a"
    assert missing.cost_text == "n/a"
    # Newest first: the missing run sorts by its directory name.
    assert [s.issue for s in result.summaries] == [99, 98]


def test_missing_attempt_json_active_is_in_progress():
    started = "2026-10-16T10:00:00.000000Z"
    payload = _payload(
        [
            {
                "issue": 7,
                "started": "2026-10-16T10-00-00.000000Z",
                "attempt": None,
                "has_events": True,
            }
        ],
        active_started_at=started,
    )
    stdout, code = _stdout(payload)
    (summary,) = read_history(stdout, code, now=NOW).summaries
    assert summary.outcome_text == "in progress"
    assert summary.active is True
    assert summary.has_summary is False
    assert summary.duration_text != "n/a"


def test_unparsable_attempt_entry_still_lists_a_row():
    # The collector converts a broken attempt.json to attempt None; the
    # parser keeps that row reachable with the crashed status.
    payload = _payload(
        [
            {
                "issue": 5,
                "started": "2026-09-27T10-00-00.000000Z",
                "attempt": None,
                "has_events": False,
            }
        ],
        unreadable=1,
    )
    stdout, code = _stdout(payload)
    result = read_history(stdout, code, now=NOW)
    assert result.ok
    assert result.total == 1
    assert result.unreadable == 1
    (summary,) = result.summaries
    assert summary.outcome_text == "no outcome (crashed or abandoned)"
    assert summary.has_events is False


def test_non_dict_attempt_entries_are_still_skipped_and_counted():
    payload = _payload(
        [
            _entry(1, "2026-09-27T10-00-00.000000Z", COMPLETE),
            {"issue": 2, "started": "2026-09-27T10-00-00.000000Z", "attempt": [1, 2]},
        ]
    )
    stdout, code = _stdout(payload)
    result = read_history(stdout, code, now=NOW)
    assert result.total == 1
    assert result.unreadable == 1


def test_missing_rows_respect_the_time_window_filter():
    old_started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 40 * 86400))
    payload = _payload(
        [
            {
                "issue": 9,
                "started": old_started.replace(":", "-"),
                "attempt": None,
                "has_events": True,
            },
            _aged_attempt(1, 1, NOW),
        ]
    )
    stdout, code = _stdout(payload)
    assert read_history(stdout, code, now=NOW, window="all").total == 2
    week = read_history(stdout, code, now=NOW, window="7").summaries
    assert [s.issue for s in week] == [1]


def test_charts_count_only_attempts_with_a_usable_summary():
    from agent_dashboard.charts import (
        cost_totals,
        outcomes_per_day,
        timeline_points,
    )

    payload = _payload(
        [
            _entry(98, "2026-09-27T16-33-43.385090Z", COMPLETE),
            {
                "issue": 99,
                "started": "2026-09-27T16-33-43.385090Z",
                "attempt": None,
                "has_events": True,
            },
        ]
    )
    stdout, code = _stdout(payload)
    result = read_history(stdout, code, now=NOW, window="all")
    assert result.total == 2
    charted = tuple(s for s in result.summaries if s.has_summary)
    assert len(charted) == 1
    assert charted[0].issue == 98
    # The missing row never feeds the charts, as today.
    assert sum(b.total for b in outcomes_per_day(charted, now=NOW, window="all")) == 1
    assert len(timeline_points(charted)) == 1
    assert cost_totals(charted).with_cost == 1


def test_pr_fields_parse_link_number_and_blank():
    with_pr = dict(
        COMPLETE,
        pull_request={"number": 12, "url": "https://example.test/pr/12"},
    )
    number_only = dict(COMPLETE, pull_request={"number": 7})
    bad_pr = dict(COMPLETE, pull_request={"number": "12", "url": ""})
    payload = _payload(
        [
            _entry(1, "2026-09-27T16-33-43.385090Z", with_pr),
            _entry(2, "2026-09-28T16-33-43.385090Z", number_only),
            _entry(3, "2026-09-29T16-33-43.385090Z", bad_pr),
            _entry(4, "2026-09-30T16-33-43.385090Z", COMPLETE),
        ]
    )
    stdout, code = _stdout(payload)
    by_issue = {s.issue: s for s in read_history(stdout, code, now=NOW).summaries}
    assert (by_issue[1].pr_number, by_issue[1].pr_url) == (
        12,
        "https://example.test/pr/12",
    )
    assert (by_issue[2].pr_number, by_issue[2].pr_url) == (7, None)
    assert (by_issue[3].pr_number, by_issue[3].pr_url) == (None, None)
    assert (by_issue[4].pr_number, by_issue[4].pr_url) == (None, None)


def test_has_events_defaults_to_true_for_old_payloads():
    stdout, code = _stdout(_payload([_entry(1, "2026-09-27T10-00-00.000000Z", COMPLETE)]))
    (summary,) = read_history(stdout, code, now=NOW).summaries
    assert summary.has_events is True
    assert summary.has_summary is True


def test_history_script_flags_missing_agent_output(tmp_path, capsys, monkeypatch):
    logs = tmp_path / "logs"
    with_events = logs / "98" / "2026-09-27T16-33-43.385090Z"
    with_events.mkdir(parents=True)
    (with_events / "attempt.json").write_text(json.dumps(COMPLETE))
    (with_events / "agent_output.json").write_text('{"event": "x"}\n')
    without_events = logs / "99" / "2026-09-28T10-00-00.000000Z"
    without_events.mkdir(parents=True)
    (without_events / "attempt.json").write_text(json.dumps(COMPLETE))
    missing = logs / "100" / "2026-09-29T10-00-00.000000Z"
    missing.mkdir(parents=True)
    payload = _run_collector_script(tmp_path, capsys, monkeypatch)
    by_issue = {entry["issue"]: entry for entry in payload["attempts"]}
    assert by_issue[98]["has_events"] is True
    assert by_issue[99]["has_events"] is False
    assert by_issue[100]["attempt"] is None
    assert by_issue[100]["has_events"] is False


def test_history_page_lists_missing_row_with_status_and_link():
    payload = {
        "layout": HISTORY_LAYOUT,
        "attempts": [
            {
                "issue": 99,
                "started": "2026-09-28T10-00-00.000000Z",
                "attempt": None,
                "has_events": True,
            }
        ],
        "active_started_at": None,
        "unreadable": 1,
    }
    client, _, _ = make_history_client([("work", "running")], {"id-work": payload})
    body = client.get("/instances/work/history?window=all").text
    assert "no outcome (crashed or abandoned)" in body
    # The row keeps its Attempt log link so the log stays reachable.
    assert "/instances/work/attempts/99/2026-09-28T10-00-00.000000Z" in body
    assert "2026-09-28T10:00:00.000000Z" in body


def test_history_page_marks_active_missing_row_in_progress():
    started = "2026-10-16T10:00:00.000000Z"
    payload = {
        "layout": HISTORY_LAYOUT,
        "attempts": [
            {
                "issue": 7,
                "started": "2026-10-16T10-00-00.000000Z",
                "attempt": None,
                "has_events": True,
            }
        ],
        "active_started_at": started,
        "unreadable": 1,
    }
    client, _, _ = make_history_client([("work", "running")], {"id-work": payload})
    body = client.get("/instances/work/history?window=all").text
    assert "in progress" in body
    assert "active-row" in body


def test_history_page_pr_column_renders_link_number_and_blank():
    payload = {
        "layout": HISTORY_LAYOUT,
        "attempts": [
            {
                "issue": 1,
                "started": "2026-09-27T16-33-43.385090Z",
                "attempt": dict(
                    COMPLETE,
                    pull_request={"number": 12, "url": "https://example.test/pr/12"},
                ),
                "has_events": True,
            },
            {
                "issue": 2,
                "started": "2026-09-28T16-33-43.385090Z",
                "attempt": dict(COMPLETE, pull_request={"number": 7}),
                "has_events": True,
            },
            {
                "issue": 3,
                "started": "2026-09-29T16-33-43.385090Z",
                "attempt": dict(COMPLETE),
                "has_events": True,
            },
        ],
        "active_started_at": None,
        "unreadable": 0,
    }
    client, _, _ = make_history_client([("work", "running")], {"id-work": payload})
    body = client.get("/instances/work/history?window=all").text
    assert "<th>PR</th>" in body
    assert '<a href="https://example.test/pr/12">#12</a>' in body
    assert "#7" in body
    # Plain numbers never link without a URL.
    assert "https://example.test/pr/12" in body
    assert body.count("https://example.test/pr/12") == 1


def test_history_page_no_events_note():
    payload = {
        "layout": HISTORY_LAYOUT,
        "attempts": [
            {
                "issue": 1,
                "started": "2026-09-27T16-33-43.385090Z",
                "attempt": dict(COMPLETE),
                "has_events": False,
            },
            {
                "issue": 2,
                "started": "2026-09-28T16-33-43.385090Z",
                "attempt": dict(COMPLETE),
                "has_events": True,
            },
        ],
        "active_started_at": None,
        "unreadable": 0,
    }
    client, _, _ = make_history_client([("work", "running")], {"id-work": payload})
    body = client.get("/instances/work/history?window=all").text
    assert "<th>Events</th>" in body
    assert "no events" in body
    assert body.count("no events") == 1


def test_history_page_with_missing_rows_still_charts_usable_summaries():
    payload = {
        "layout": HISTORY_LAYOUT,
        "attempts": [
            {
                "issue": 98,
                "started": "2026-09-27T16-33-43.385090Z",
                "attempt": dict(COMPLETE),
                "has_events": True,
            },
            {
                "issue": 99,
                "started": "2026-09-28T10-00-00.000000Z",
                "attempt": None,
                "has_events": False,
            },
        ],
        "active_started_at": None,
        "unreadable": 1,
    }
    client, _, _ = make_history_client([("work", "running")], {"id-work": payload})
    response = client.get("/instances/work/history?window=all")
    assert response.status_code == 200
    body = response.text
    assert "#98" in body and "#99" in body
    assert "no outcome (crashed or abandoned)" in body
    assert "no events" in body
    # Charts still render from the usable summary only.
    assert "Outcomes" in body
    assert "complete" in body


# --- sticky breadcrumb top bar (tracer bullet) --------------------------------


def test_history_page_has_sticky_breadcrumb_topbar():
    client, _, _ = make_history_client(
        [("work", "running")], {"id-work": _history_payload_old_and_recent()}
    )
    body = client.get("/instances/work/history?window=all").text
    # Breadcrumb trail: Fleet links home, the instance is plain current text.
    assert '<nav aria-label="Breadcrumb">' in body
    assert '<a href="/">Fleet</a>' in body
    assert '<span aria-current="page">work</span>' in body
    # The current crumb is text, never a link.
    assert 'href="/instances/work/history"' not in body.split("Breadcrumb")[1].split("</nav>")[0]
    # The on-demand note and Refresh live in the sticky bar.
    subnav = body.split('<div class="subnav">', 1)[1].split("</div>", 1)[0]
    assert "data is read on demand" in subnav
    assert ">Refresh</a>" in subnav
    # The bottom "Back to the fleet" escape hatch is gone.
    assert "Back to the fleet" not in body


def test_history_refresh_keeps_the_selected_range():
    client, _ = _history_client_with_aged_attempts()
    body = client.get("/instances/work/history?preset=7d").text
    subnav = body.split('<div class="subnav">', 1)[1].split("</div>", 1)[0]
    assert "/instances/work/history?preset=7d" in subnav.replace("&amp;", "&")


def test_history_breadcrumb_renders_for_unknown_instance_and_errors():
    from agent_dashboard.gateway import GatewayError

    client, _, _ = make_history_client(
        [("work", "running")], {"id-work": _history_payload_old_and_recent()}
    )
    unknown = client.get("/instances/nope/history").text
    assert '<nav aria-label="Breadcrumb">' in unknown
    assert '<a href="/">Fleet</a>' in unknown
    assert '<span aria-current="page">nope</span>' in unknown
    assert "Back to the fleet" not in unknown

    bad_layout_client, _, _ = make_history_client(
        [("work", "running")], {"id-work": {"layout": "unknown", "attempts": []}}
    )
    error_body = bad_layout_client.get("/instances/work/history").text
    assert '<span aria-current="page">work</span>' in error_body
    assert "Back to the fleet" not in error_body

    err_client, _, gateway = make_history_client([("work", "running")])
    gateway.set_history_error("id-work", GatewayError("boom"))
    gateway_body = err_client.get("/instances/work/history").text
    assert '<nav aria-label="Breadcrumb">' in gateway_body
    assert '<span aria-current="page">work</span>' in gateway_body


def test_fleet_view_has_no_breadcrumb_and_header_is_not_sticky():
    from test_web import make_client

    client, _ = make_client([("worker-a", "running")])
    body = client.get("/").text
    assert 'aria-label="Breadcrumb"' not in body
    assert 'class="subnav"' not in body
    assert 'class="crumbs"' not in body
    css = client.get("/static/style.css").text
    # The history bar pins; the fleet header stays static.
    assert "position: sticky" in css.split(".subnav", 1)[1].split("}", 1)[0]
    topbar = css.split(".topbar {", 1)[1].split("}", 1)[0]
    assert "sticky" not in topbar


def test_subnav_wraps_without_horizontal_scroll_and_uses_tokens():
    from test_web import make_client

    client, _ = make_client([("worker-a", "running")])
    css = client.get("/static/style.css").text
    block = css.split(".subnav {", 1)[1].split("}", 1)[0]
    assert "flex-wrap: wrap" in block
    assert "max-width: 100%" in block
    crumbs = css.split(".crumbs li {", 1)[1].split("}", 1)[0]
    assert "overflow-wrap: anywhere" in crumbs
    # Tokens only: no literal colours in the new bar or crumbs.
    assert "#" not in block
    assert "#" not in css.split(".crumbs li + li::before {", 1)[1].split("}", 1)[0]
