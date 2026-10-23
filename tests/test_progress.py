"""Attempt progress chart: limit-reading parsing and series construction.

Covers the parsing of the periodic ``limits_checked`` readings the agent
writes into the Attempt log event stream (including missing fields), the
progress payload reader (Attempt log layout, no-data and handoff cases),
and the turns/budget-over-elapsed series with the configured limits as
reference lines. Vocabulary: Attempt log, Managed instance.
"""

from __future__ import annotations

import json

from agent_dashboard import gateway as gateway_module
from agent_dashboard.gateway import ATTEMPT_EVENTS_LAYOUT
from agent_dashboard.progress import parse_limit_reading, read_progress

FULL_DETAIL = (
    "thread=main; budget_tokens=1234567; measured_tokens=1200000; "
    "estimated_tokens=34567; soft_threshold_tokens=3200000; rule=percentage; "
    "reserve_turns=5; reserve_tokens=500000; max_budget_tokens=4000000; "
    "main_context_tokens=100000; turns=12; max_turns=60; "
    "elapsed_seconds=95; timeout_seconds=3600"
)


def test_parse_full_limit_reading():
    reading = parse_limit_reading(FULL_DETAIL)
    assert reading is not None
    assert reading.thread == "main"
    assert reading.elapsed == 95
    assert reading.turns == 12
    assert reading.budget_tokens == 1234567
    assert reading.soft_threshold_tokens == 3200000
    assert reading.max_budget_tokens == 4000000
    assert reading.max_turns == 60
    assert reading.timeout_seconds == 3600


def test_parse_reading_with_missing_fields_stays_none_never_zero():
    reading = parse_limit_reading("thread=main; turns=3; elapsed_seconds=10")
    assert reading is not None
    assert reading.thread == "main"
    assert reading.turns == 3
    assert reading.elapsed == 10
    assert reading.budget_tokens is None
    assert reading.soft_threshold_tokens is None
    assert reading.max_budget_tokens is None
    assert reading.max_turns is None
    assert reading.timeout_seconds is None


def test_parse_reading_ignores_cache_tail_and_unknown_keys():
    detail = (
        "thread=main; budget_tokens=100; turns=1; elapsed_seconds=5; "
        "input_tokens=50; cache_read_tokens=20; cache_hit_rate=0.4; "
        "cache_miss_tokens=excluded; future_key=7"
    )
    reading = parse_limit_reading(detail)
    assert reading is not None
    assert reading.budget_tokens == 100
    assert reading.turns == 1
    assert reading.elapsed == 5


def test_parse_reading_with_non_numeric_values_stays_none():
    reading = parse_limit_reading("thread=main; turns=many; elapsed_seconds=soon; budget_tokens=?")
    assert reading is not None
    assert reading.thread == "main"
    assert reading.turns is None
    assert reading.elapsed is None
    assert reading.budget_tokens is None


def test_parse_reading_without_recognised_keys_is_none():
    assert parse_limit_reading("input_tokens=50; cache_hit_rate=0.4") is None
    assert parse_limit_reading("not a reading at all") is None
    assert parse_limit_reading("") is None
    assert parse_limit_reading(None) is None


# --- collector script --------------------------------------------------------


def _run_events_script(root, capsys, monkeypatch, argv):
    """Execute the shipped ATTEMPT_EVENTS_SCRIPT with /data remapped onto tmp dirs."""
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
    real_listdir = real_os.listdir
    real_isdir = real_os.path.isdir

    def fake_open(path, *args, **kwargs):
        return real_open(remap(path), *args, **kwargs)

    monkeypatch.setattr(real_os, "listdir", lambda p: real_listdir(remap(p)))
    monkeypatch.setattr(real_os.path, "isdir", lambda p: real_isdir(remap(p)))
    monkeypatch.setattr("builtins.open", fake_open)
    monkeypatch.setattr(sys, "argv", ["collector", *argv])
    namespace: dict = {"__name__": "__collector_test__"}
    try:
        exec(gateway_module.ATTEMPT_EVENTS_SCRIPT, namespace)  # noqa: S102
    except SystemExit as exc:
        assert exc.code == 0
    return json.loads(capsys.readouterr().out)


def _write_attempt(tmp_path, issue="98", started="2026-09-27T16-33-43.385090Z",
                   attempt=None, events=()):
    attempt_dir = tmp_path / "logs" / issue / started
    attempt_dir.mkdir(parents=True)
    (attempt_dir / "attempt.json").write_text(json.dumps(attempt or {"outcome": "complete"}))
    (attempt_dir / "agent_output.json").write_text("\n".join(events) + ("\n" if events else ""))
    return attempt_dir


def _event(name, detail="", timestamp="2026-09-27T16:34:00Z"):
    return json.dumps(
        {
            "timestamp": timestamp,
            "level": "INFO",
            "event": name,
            "issue_number": 98,
            "phase": "model_running",
            "detail": detail,
        },
        sort_keys=True,
    )


def test_events_script_keeps_only_progress_events(tmp_path, capsys, monkeypatch):
    _write_attempt(
        tmp_path,
        events=[
            _event("tool_call", "Bash -> 0001_tool_call.json"),
            _event("limits_checked", "thread=main; turns=1; elapsed_seconds=5"),
            _event("model_response", "-> 0002_model_response.txt (chars=9)"),
            _event("token_soft_threshold_handoff_context_injected", ""),
            _event("operator_handoff_context_delivered", "request=abc"),
        ],
    )
    payload = _run_events_script(
        tmp_path, capsys, monkeypatch, ["98", "2026-09-27T16-33-43.385090Z"]
    )
    assert payload["layout"] == ATTEMPT_EVENTS_LAYOUT
    assert payload["issue"] == 98
    assert payload["started"] == "2026-09-27T16-33-43.385090Z"
    assert payload["attempt"] == {"outcome": "complete"}
    assert [e["event"] for e in payload["events"]] == [
        "limits_checked",
        "token_soft_threshold_handoff_context_injected",
        "operator_handoff_context_delivered",
    ]
    assert payload["events"][0]["detail"] == "thread=main; turns=1; elapsed_seconds=5"
    assert payload["total_events"] == 5
    assert payload["truncated"] is False


def test_events_script_counts_malformed_lines_without_dropping_the_rest(
    tmp_path, capsys, monkeypatch
):
    _write_attempt(
        tmp_path,
        events=[
            _event("limits_checked", "thread=main; turns=1; elapsed_seconds=5"),
            "not json{",
            _event("limits_checked", "thread=main; turns=2; elapsed_seconds=9"),
        ],
    )
    payload = _run_events_script(
        tmp_path, capsys, monkeypatch, ["98", "2026-09-27T16-33-43.385090Z"]
    )
    assert [e["detail"] for e in payload["events"]] == [
        "thread=main; turns=1; elapsed_seconds=5",
        "thread=main; turns=2; elapsed_seconds=9",
    ]
    assert payload["unreadable"] == 1


def test_events_script_missing_files_still_reports_layout(tmp_path, capsys, monkeypatch):
    (tmp_path / "logs").mkdir()
    payload = _run_events_script(
        tmp_path, capsys, monkeypatch, ["98", "2026-09-27T16-33-43.385090Z"]
    )
    assert payload["layout"] == ATTEMPT_EVENTS_LAYOUT
    assert payload["attempt"] is None
    assert payload["events"] == []


def test_events_script_missing_logs_is_unknown_layout(tmp_path, capsys, monkeypatch):
    payload = _run_events_script(
        tmp_path, capsys, monkeypatch, ["98", "2026-09-27T16-33-43.385090Z"]
    )
    assert payload["layout"] == "unknown"


def test_events_script_rejects_bad_reference_without_a_traceback(
    tmp_path, capsys, monkeypatch
):
    (tmp_path / "logs").mkdir()
    payload = _run_events_script(
        tmp_path, capsys, monkeypatch, ["98", "../state/attempt.json"]
    )
    assert payload["layout"] == "unknown"


# --- progress payload reader -------------------------------------------------


def _reading_event(turns, elapsed, timestamp, thread="main", **extra):
    detail = "; ".join(
        [
            f"thread={thread}",
            f"budget_tokens={extra.get('budget', 1000000 + turns * 10000)}",
            f"soft_threshold_tokens={extra.get('soft', 3200000)}",
            f"max_budget_tokens={extra.get('max_budget', 4000000)}",
            f"turns={turns}",
            f"max_turns={extra.get('max_turns', 60)}",
            f"elapsed_seconds={elapsed}",
            f"timeout_seconds={extra.get('timeout', 3600)}",
        ]
    )
    return {"timestamp": timestamp, "level": "INFO", "event": "limits_checked", "detail": detail}


def _payload(events, attempt=None, layout=ATTEMPT_EVENTS_LAYOUT):
    return {
        "layout": layout,
        "issue": 98,
        "started": "2026-09-27T16-33-43.385090Z",
        "attempt": {"outcome": "complete"} if attempt is None else attempt,
        "events": events,
        "total_events": len(events),
        "unreadable": 0,
        "truncated": False,
    }


def _read(payload, **kwargs):
    import json as json_module

    return read_progress(
        json_module.dumps(payload).encode(),
        0,
        issue=98,
        started="2026-09-27T16-33-43.385090Z",
        **kwargs,
    )


def test_read_progress_parses_main_thread_readings_oldest_first():
    result = _read(
        _payload(
            [
                _reading_event(2, 20, "2026-09-27T16:34:03Z"),
                _reading_event(1, 8, "2026-09-27T16:33:51Z"),
            ]
        )
    )
    assert result.ok and result.error is None
    assert result.issue == 98
    assert result.started_at == "2026-09-27T16:33:43.385090Z"
    assert result.outcome_text == "complete"
    assert result.active is False
    assert [r.turns for r in result.readings] == [1, 2]
    assert [r.elapsed for r in result.readings] == [8, 20]


def test_read_progress_limits_come_from_the_latest_reading():
    result = _read(
        _payload(
            [
                _reading_event(1, 8, "2026-09-27T16:33:51Z", max_turns=60, timeout=3600),
                _reading_event(2, 20, "2026-09-27T16:34:03Z", max_turns=60, timeout=3600),
            ]
        )
    )
    assert result.limits.max_turns == 60
    assert result.limits.timeout_seconds == 3600
    assert result.limits.max_budget_tokens == 4000000
    assert result.limits.soft_threshold_tokens == 3200000


def test_read_progress_ignores_subagent_thread_readings():
    result = _read(
        _payload(
            [
                _reading_event(1, 8, "2026-09-27T16:33:51Z"),
                _reading_event(9, 9, "2026-09-27T16:33:52Z", thread="subagent:abc"),
            ]
        )
    )
    assert [r.turns for r in result.readings] == [1]


def test_read_progress_handoff_is_placed_between_readings():
    result = _read(
        _payload(
            [
                _reading_event(1, 10, "2026-09-27T16:33:53Z"),
                {
                    "timestamp": "2026-09-27T16:33:58Z",
                    "level": "INFO",
                    "event": "token_soft_threshold_handoff_context_injected",
                    "detail": "",
                },
                _reading_event(2, 20, "2026-09-27T16:34:03Z"),
            ]
        )
    )
    (mark,) = result.handoffs
    assert mark.name == "token_soft_threshold_handoff_context_injected"
    assert mark.elapsed == 15


def test_read_progress_handoff_without_a_timestamp_is_kept_but_unplaced():
    result = _read(
        _payload(
            [
                _reading_event(1, 10, "2026-09-27T16:33:53Z"),
                {
                    "timestamp": None,
                    "level": "INFO",
                    "event": "operator_handoff_context_delivered",
                    "detail": "request=abc",
                },
            ]
        )
    )
    (mark,) = result.handoffs
    assert mark.elapsed is None


def test_read_progress_active_attempt_without_outcome():
    result = _read(_payload([_reading_event(1, 8, "2026-09-27T16:33:51Z")], attempt={}))
    assert result.active is True
    assert result.outcome_text == "active"


def test_read_progress_limits_fall_back_to_attempt_token_budget():
    attempt = {
        "outcome": "complete",
        "token_budget": {
            "max_budget_tokens": 4000000,
            "effective_soft_threshold_tokens": 3100000,
            "budget_tokens": 1500000,
            "turns": 12,
            "elapsed_seconds": 95,
        },
    }
    result = _read(
        _payload(
            [
                {
                    "timestamp": "2026-09-27T16:33:51Z",
                    "level": "INFO",
                    "event": "limits_checked",
                    "detail": "thread=main; turns=1; elapsed_seconds=8",
                }
            ],
            attempt=attempt,
        )
    )
    assert result.limits.max_budget_tokens == 4000000
    assert result.limits.soft_threshold_tokens == 3100000
    assert result.limits.max_turns is None
    assert result.limits.timeout_seconds is None


def test_read_progress_without_readings_is_ok_with_empty_series():
    result = _read(_payload([], attempt={"outcome": "infrastructure_error"}))
    assert result.ok and result.error is None
    assert result.readings == ()
    assert result.handoffs == ()
    assert result.outcome_text == "infrastructure_error"


def test_read_progress_unknown_layout_is_never_an_error():
    for stdout in (b"not json{", b'{"layout": "unknown", "reason": "x"}', b'{"layout": "other"}'):
        result = read_progress(stdout, 0, issue=98, started="2026-09-27T16-33-43.385090Z")
        assert result.ok is False
        assert result.error == "layout not recognised"


def test_read_progress_events_not_a_list_is_unknown_layout():
    payload = _payload([])
    payload["events"] = {"limits_checked": "x"}
    assert _read(payload).error == "layout not recognised"


# --- series construction -----------------------------------------------------


def _series(payload):
    from agent_dashboard.progress import progress_series

    return progress_series(_read(payload))


def test_series_is_none_without_placeable_readings():
    assert _series(_payload([])) is None
    no_elapsed = _payload(
        [
            {
                "timestamp": "2026-09-27T16:33:51Z",
                "level": "INFO",
                "event": "limits_checked",
                "detail": "thread=main; turns=1",
            }
        ]
    )
    series = _series(no_elapsed)
    assert series is None


def test_series_scales_turns_and_budget_against_elapsed():
    series = _series(
        _payload(
            [
                _reading_event(15, 100, "2026-09-27T16:35:23Z"),
                _reading_event(30, 200, "2026-09-27T16:37:03Z"),
            ],
            attempt={},
        )
    )
    assert series is not None
    assert series.x_max == 3600  # the timeout stretches the axis past the data
    assert [p.x_frac for p in series.turns] == [100 / 3600, 200 / 3600]
    assert [p.x_frac for p in series.budgets] == [100 / 3600, 200 / 3600]
    # Peaks include the configured limits, so a finished attempt never fills the plot.
    assert series.turns_peak == 60
    assert [p.value_frac for p in series.turns] == [15 / 60, 30 / 60]
    assert series.budget_peak == 4000000
    assert series.timeout_frac == 1.0
    assert series.max_turns_frac == 1.0
    assert series.max_budget_frac == 1.0
    assert series.soft_threshold_frac == 3200000 / 4000000


def test_series_axis_ends_at_the_data_without_a_timeout():
    payload = _payload(
        [
            _reading_event(1, 8, "2026-09-27T16:33:51Z", timeout=0, max_turns=0),
            _reading_event(2, 20, "2026-09-27T16:34:03Z", timeout=0, max_turns=0),
        ]
    )
    # Strip the unknown limits: no timeout, no maximums.
    for entry in payload["events"]:
        entry["detail"] = "; ".join(
            part
            for part in entry["detail"].split("; ")
            if not part.startswith(("timeout_seconds", "max_turns", "max_budget", "soft_"))
        )
    series = _series(payload)
    assert series is not None
    assert series.x_max == 20
    assert [p.x_frac for p in series.turns] == [8 / 20, 1.0]
    assert series.turns_peak == 2
    assert series.timeout_frac is None
    assert series.max_turns_frac is None
    assert series.x_ticks[-1] == (1.0, "20s")


def test_series_handoff_marks_land_on_the_time_axis():
    series = _series(
        _payload(
            [
                _reading_event(1, 10, "2026-09-27T16:33:53Z"),
                {
                    "timestamp": "2026-09-27T16:33:58Z",
                    "level": "INFO",
                    "event": "token_soft_threshold_handoff_context_injected",
                    "detail": "",
                },
                _reading_event(2, 20, "2026-09-27T16:34:03Z"),
            ]
        )
    )
    assert series is not None
    assert series.handoff_fracs == [15 / 3600]
    assert series.unplaced_handoffs == 0


def test_series_counts_unplaced_handoffs_and_skipped_readings():
    result = _read(
        _payload(
            [
                _reading_event(1, 10, "2026-09-27T16:33:53Z"),
                {
                    "timestamp": None,
                    "level": "INFO",
                    "event": "operator_handoff_context_delivered",
                    "detail": "",
                },
                {
                    "timestamp": "2026-09-27T16:33:55Z",
                    "level": "INFO",
                    "event": "limits_checked",
                    "detail": "thread=main; turns=5",
                },
            ]
        )
    )
    from agent_dashboard.progress import progress_series

    series = progress_series(result)
    assert series is not None
    assert series.handoff_fracs == []
    assert series.unplaced_handoffs == 1
    assert series.skipped == 1
    assert len(series.turns) == 1


# --- progress page -----------------------------------------------------------


def _progress_payload(events, attempt=None):
    return _payload(events, attempt=attempt)


def make_progress_client(rows, events=None, events_error=None, run_trees=None):
    import asyncio as _asyncio

    from fake_gateway import FakeGateway
    from fastapi.testclient import TestClient

    from agent_dashboard.app import create_app
    from agent_dashboard.config import Settings
    from agent_dashboard.fleet import FleetState
    from agent_dashboard.gateway import ManagedInstance

    state = FleetState()
    instances = []
    for name, container_state in rows:
        raw = "running" if container_state == "running" else "exited"
        instances.append(ManagedInstance(name, f"id-{name}", raw))
    _asyncio.run(state.apply_cycle(instances, {}, now=1000.0))
    gateway = FakeGateway()
    for container_id, payload in (events or {}).items():
        gateway.set_events(container_id, payload)
    if events_error is not None:
        for container_id, error in events_error.items():
            gateway.set_events_error(container_id, error)
    for key, tree in (run_trees or {}).items():
        gateway.set_run_tree(*key, tree)
    app = create_app(
        state=state, settings=Settings(poll_interval=5.0), gateway=gateway, start_poller=False
    )
    return TestClient(app, base_url="http://localhost"), state, gateway


STARTED_DASH = "2026-09-27T16-33-43.385090Z"


def test_progress_page_renders_chart_with_limits_and_handoff():
    client, _, _ = make_progress_client(
        [("work", "running")],
        {
            "id-work": _progress_payload(
                [
                    _reading_event(15, 100, "2026-09-27T16:35:23Z"),
                    {
                        "timestamp": "2026-09-27T16:36:00Z",
                        "level": "INFO",
                        "event": "token_soft_threshold_handoff_context_injected",
                        "detail": "",
                    },
                    _reading_event(30, 200, "2026-09-27T16:37:03Z"),
                ]
            )
        },
    )
    response = client.get(f"/instances/work/attempts/98/{STARTED_DASH}/progress")
    assert response.status_code == 200
    body = response.text
    assert "Attempt progress" in body
    assert "Managed instance" in body
    # Inline SVG reusing the outcome-chart approach: lines, tips, no 500.
    assert "<svg" in body
    assert "prog-turns" in body
    assert "prog-budget" in body
    assert "data-tip" in body
    # Reference lines and the handoff mark render with labels.
    assert "max turns" in body
    assert "max budget 4,000,000" in body
    assert "soft threshold 3,200,000" in body
    assert "Handoff" in body
    assert "token_soft_threshold_handoff_context_injected" in body
    assert "chart-tip" in body
    assert "/static/charts.js" in body


def test_progress_page_without_readings_is_a_message_not_an_error():
    client, _, _ = make_progress_client(
        [("work", "running")],
        {"id-work": _progress_payload([], attempt={"outcome": "infrastructure_error"})},
    )
    response = client.get(f"/instances/work/attempts/98/{STARTED_DASH}/progress")
    assert response.status_code == 200
    assert "No progress data" in response.text
    assert "infrastructure_error" in response.text


def test_progress_page_active_attempt_shows_partial_chart():
    client, _, _ = make_progress_client(
        [("work", "running")],
        {
            "id-work": _progress_payload(
                [_reading_event(1, 8, "2026-09-27T16:33:51Z")], attempt={}
            )
        },
    )
    body = client.get(f"/instances/work/attempts/98/{STARTED_DASH}/progress").text
    assert "<svg" in body
    assert "still running" in body
    assert "partial data" in body


def test_progress_page_unknown_layout_is_a_message_not_500():
    client, _, _ = make_progress_client(
        [("work", "running")], {"id-work": {"layout": "unknown", "reason": "x"}}
    )
    response = client.get(f"/instances/work/attempts/98/{STARTED_DASH}/progress")
    assert response.status_code == 200
    assert "layout not recognised" in response.text


def test_progress_page_unknown_and_stopped_instances():
    import json as _json

    from fake_gateway import make_run_tree

    client, _, _ = make_progress_client([("work", "running")])
    unknown = client.get(f"/instances/nope/attempts/98/{STARTED_DASH}/progress")
    assert unknown.status_code == 200
    assert "unknown" in unknown.text.lower()
    stopped, _, gateway = make_progress_client([("down", "not running")])
    body = stopped.get(f"/instances/down/attempts/98/{STARTED_DASH}/progress").text
    assert "Attempt not found in this instance" in body
    assert "its volume is gone or the attempt is no longer stored" in body
    assert gateway.run_tree_calls == [("id-down", "98", STARTED_DASH)]
    assert gateway.events_calls == []
    reading = _reading_event(15, 100, "2026-09-27T16:35:23Z")
    tree = make_run_tree(
        {
            f"{STARTED_DASH}/attempt.json": _json.dumps({"outcome": "complete"}).encode(),
            f"{STARTED_DASH}/agent_output.json": (
                _json.dumps(reading) + "\n"
            ).encode(),
        }
    )
    ok_client, _, ok_gateway = make_progress_client(
        [("down", "not running")],
        run_trees={("id-down", "98", STARTED_DASH): tree},
    )
    ok_body = ok_client.get(f"/instances/down/attempts/98/{STARTED_DASH}/progress").text
    assert "<svg" in ok_body
    assert ok_gateway.run_tree_calls == [("id-down", "98", STARTED_DASH)]
    traversal = ok_client.get("/instances/down/attempts/%2E%2E/" + STARTED_DASH + "/progress")
    assert traversal.status_code == 200
    assert "Invalid attempt reference" in traversal.text


def test_progress_page_invalid_reference_and_gateway_error():
    client, _, _ = make_progress_client([("work", "running")])
    bad = client.get("/instances/work/attempts/98/not-a-timestamp/progress")
    assert bad.status_code == 200
    assert "Invalid attempt reference" in bad.text
    from agent_dashboard.gateway import GatewayError

    err_client, _, _ = make_progress_client(
        [("work", "running")],
        events_error={"id-work": GatewayError("docker exec timed out after 5 s")},
    )
    response = err_client.get(f"/instances/work/attempts/98/{STARTED_DASH}/progress")
    assert response.status_code == 200
    assert "Could not read" in response.text


def test_progress_page_accepts_colon_form_started():
    client, _, gateway = make_progress_client(
        [("work", "running")],
        {"id-work": _progress_payload([_reading_event(1, 8, "2026-09-27T16:33:51Z")])},
    )
    colon = "2026-09-27T16:33:43.385090Z"
    response = client.get(f"/instances/work/attempts/98/{colon}/progress")
    assert response.status_code == 200
    assert "<svg" in response.text
    assert gateway.events_calls == [("id-work", "98", STARTED_DASH)]


def test_history_rows_link_to_progress():
    from test_history import make_history_client

    payload = {
        "layout": "attempt-logs/v1",
        "attempts": [
            {
                "issue": 98,
                "started": STARTED_DASH,
                "attempt": {
                    "outcome": "complete",
                    "started_at": "2026-09-27T16:33:43.385090Z",
                    "duration_seconds": 60,
                },
            }
        ],
        "active_started_at": None,
        "unreadable": 0,
    }
    client, _, _ = make_history_client([("work", "running")], {"id-work": payload})
    body = client.get("/instances/work/history?window=all").text
    assert f"/instances/work/attempts/98/{STARTED_DASH}/progress" in body
    assert ">Progress</a>" in body


# --- sticky breadcrumb top bar ------------------------------------------------


STARTED_COLON = "2026-09-27T16:33:43.385090Z"


def test_progress_page_has_sticky_breadcrumb_topbar():
    client, _, _ = make_progress_client(
        [("work", "running")],
        {
            "id-work": _progress_payload(
                [_reading_event(1, 8, "2026-09-27T16:33:51Z")]
            )
        },
    )
    body = client.get(f"/instances/work/attempts/98/{STARTED_DASH}/progress").text
    # Breadcrumb trail: Fleet links home, the instance links to its history,
    # the attempt links to its log, Progress is plain current text.
    assert '<nav aria-label="Breadcrumb">' in body
    assert '<a href="/">Fleet</a>' in body
    nav = body.split("Breadcrumb")[1].split("</nav>")[0]
    assert '<a href="/instances/work/history">work</a>' in nav
    assert (
        f'<a href="/instances/work/attempts/98/{STARTED_DASH}">'
        f"#98 · {STARTED_COLON}</a>"
    ) in nav
    assert '<span aria-current="page">Progress</span>' in body
    # The on-demand note and Refresh live in the sticky bar.
    subnav = body.split('<div class="subnav">', 1)[1].split("</div>", 1)[0]
    assert "data is read on demand" in subnav
    assert ">Refresh</a>" in subnav
    assert f"/instances/work/attempts/98/{STARTED_DASH}/progress" in subnav.replace(
        "&amp;", "&"
    )
    # The bottom escape hatches are gone.
    assert "Back to attempt history" not in body
    assert "Back to the fleet" not in body
    assert ">Attempt log</a>" not in body


def test_progress_breadcrumb_renders_for_unknown_instance_errors_and_no_data():
    client, _, _ = make_progress_client(
        [("work", "running")],
        {
            "id-work": _progress_payload(
                [_reading_event(1, 8, "2026-09-27T16:33:51Z")]
            )
        },
    )
    unknown = client.get(f"/instances/nope/attempts/98/{STARTED_DASH}/progress").text
    assert '<nav aria-label="Breadcrumb">' in unknown
    assert '<a href="/">Fleet</a>' in unknown
    assert '<a href="/instances/nope/history">nope</a>' in unknown
    assert '<span aria-current="page">Progress</span>' in unknown
    assert "Back to attempt history" not in unknown

    invalid = client.get("/instances/work/attempts/98/not-a-timestamp/progress").text
    assert '<nav aria-label="Breadcrumb">' in invalid
    assert '<a href="/instances/work/history">work</a>' in invalid
    assert '<span aria-current="page">Progress</span>' in invalid
    assert "Invalid attempt reference" in invalid

    from agent_dashboard.gateway import GatewayError

    err_client, _, _ = make_progress_client(
        [("work", "running")],
        events_error={"id-work": GatewayError("docker exec timed out after 5 s")},
    )
    gateway_body = err_client.get(
        f"/instances/work/attempts/98/{STARTED_DASH}/progress"
    ).text
    assert '<nav aria-label="Breadcrumb">' in gateway_body
    assert '<span aria-current="page">Progress</span>' in gateway_body
    assert "Could not read" in gateway_body

    empty_client, _, _ = make_progress_client(
        [("work", "running")],
        {"id-work": _progress_payload([], attempt={"outcome": "complete"})},
    )
    empty = empty_client.get(
        f"/instances/work/attempts/98/{STARTED_DASH}/progress"
    ).text
    assert '<nav aria-label="Breadcrumb">' in empty
    assert '<span aria-current="page">Progress</span>' in empty
    assert "No progress data" in empty
    assert "Back to attempt history" not in empty
