"""Contract Status Snapshot drawer: figures, budget, freshness, events.

Renders the drawer for every pinned ``status`` fixture through replayed
contract ``status`` answers, plus synthetic variants for the edges the
package fixtures do not cover (every basis and unit, unknown enums,
staleness, truncation, native and unknown names). The Activity widget is
covered too: native phase, last activity and the attempt's events, with
no Last call pane and no progress link.
"""

from __future__ import annotations

import asyncio
import copy
import json
import time

from contract_replay import ReplayProcessRunner
from fastapi.testclient import TestClient

from agent_dashboard.app import create_app
from agent_dashboard.collector import run_cycle
from agent_dashboard.config import Settings
from agent_dashboard.contract_status import (
    ContractDetail,
    contract_headline,
    figure_view,
    read_contract_snapshot,
    section_freshness,
    stale_suffix,
)
from agent_dashboard.fleet import FleetState
from agent_dashboard.gateway import (
    CONTRACT_PATH,
    DockerGateway,
    ManagedInstance,
)

STATUS_ARGV = ["--contract", "1", "status"]


def _result(name: str) -> dict:
    """The ``result`` of a pinned package ``status`` fixture."""
    from agent_operator_contract import documents

    return json.loads(documents.load_fixture(name)["stdout"])["result"]


def _recording(result: dict) -> dict:
    """One replayable ``status`` answer carrying this snapshot result."""
    return {
        "argv": list(STATUS_ARGV),
        "exit_code": 0,
        "stdout": json.dumps({"contract": "1.0", "ok": True, "result": result}),
        "stderr": "",
    }


def _instance(name: str = "web-1", container: str = "abc123") -> ManagedInstance:
    return ManagedInstance(
        name,
        container,
        "running",
        path=CONTRACT_PATH,
        instance=name,
        exec_user="agent",
        backend="coding-agent",
   )


def _replay_client(results: list[dict], *, now: float | None = None) -> TestClient:
    """A client whose contract instances replay the given results (one cycle)."""
    at = time.time() if now is None else now
    runner = ReplayProcessRunner([_recording(result) for result in results])
    gateway = DockerGateway(process_runner=runner)
    instances = [
        _instance(f"web-{index}", f"abc{index:03d}") for index in range(len(results))
    ]

    async def _list() -> list[ManagedInstance]:
        return list(instances)

    gateway.list_instances = _list  # type: ignore[method-assign]
    state = FleetState()
    asyncio.run(run_cycle(state, gateway, max_concurrent_execs=4, now=at))
    app = create_app(
        state=state,
        settings=Settings(poll_interval=5.0),
        gateway=gateway,
        start_poller=False,
    )
    return TestClient(app, base_url="http://localhost")


def _fresh_result(result: dict, now: float) -> dict:
    """Stamp every section ``observed_at`` at ``now`` (reads as fresh)."""
    result = copy.deepcopy(result)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    result["instance"]["observed_at"] = stamp
    result["intake"]["observed_at"] = stamp
    if isinstance(result.get("current_attempt"), dict):
        result["current_attempt"]["observed_at"] = stamp
    return result


def _lib_figure(value, unit, basis, reason=None):
    from agent_operator_contract.status import Figure

    return Figure(value=value, unit=unit, basis=basis, reason=reason)


# --- figure view model ---------------------------------------------------------


def test_figure_units_render_with_values_and_bases():
    cases = [
        (1500000, "tokens", "measured", "1,500,000 tokens", "measured"),
        (3.41, "usd", "estimated", "$3.41", "estimated"),
        (20.0, "usd", "configured", "$20.00", "configured"),
        (1325, "seconds", "measured", "1,325 s", "measured"),
        (118, "count", "assumed", "118", "assumed"),
        (0.86, "ratio", "measured", "86%", "measured"),
    ]
    for value, unit, basis, text, marker in cases:
        view = figure_view(_lib_figure(value, unit, basis))
        assert view.text == text, (unit, basis)
        assert view.basis == marker, (unit, basis)
        assert view.unit == unit
        assert view.state == "value"


def test_figure_unavailable_renders_unknown():
    view = figure_view(_lib_figure(None, "tokens", "unavailable"))
    assert view.text == "unknown"
    assert view.basis == ""
    assert view.state == "unknown"


def test_figure_not_applicable_is_distinct_from_unknown():
    view = figure_view(_lib_figure(None, "count", "not_applicable"))
    assert view.text == "not applicable"
    assert view.basis == ""
    assert view.state == "na"
    assert view.state != figure_view(_lib_figure(None, "tokens", "unavailable")).state


def test_figure_unknown_unit_or_basis_renders_unknown():
    assert figure_view(_lib_figure(90, "fortnights", "measured")).text == "unknown"
    assert figure_view(_lib_figure(5, "count", "forthcoming")).text == "unknown"
    assert figure_view(_lib_figure(5, "count", "forthcoming")).state == "unknown"
    assert figure_view(_lib_figure(None, "tokens", "measured")).text == "unknown"
    assert figure_view(_lib_figure("many", "count", "measured")).text == "unknown"


def test_figure_keeps_the_backend_reason():
    view = figure_view(
        _lib_figure(None, "ratio", "unavailable", reason="usage metadata missing")
    )
    assert view.text == "unknown"
    assert view.reason == "usage metadata missing"


# --- budget --------------------------------------------------------------------


def test_budget_names_the_enforced_tokens():
    assert read_contract_snapshot(_result("status-active")).current.budget.enforced == (
        "effective"
    )
    assert (
        read_contract_snapshot(_result("status-simple-coding-agent")).current.budget.enforced
        == "raw"
    )


def test_budget_enforced_none_marks_neither():
    result = _result("status-active")
    result["current_attempt"]["budget"]["enforced_tokens"] = "none"
    assert read_contract_snapshot(result).current.budget.enforced == "none"


def test_budget_enforced_unknown_marks_neither():
    result = _result("status-active")
    result["current_attempt"]["budget"]["enforced_tokens"] = "someday"
    assert read_contract_snapshot(result).current.budget.enforced == "unknown"


def test_limits_show_used_against_limit_with_both_bases():
    detail = read_contract_snapshot(_result("status-active"))
    by_name = {lim.name: lim for lim in detail.current.budget.limits}
    used, limit = by_name["cost_usd"].used, by_name["cost_usd"].limit
    assert (used.text, used.basis) == ("$3.41", "estimated")
    assert (limit.text, limit.basis) == ("$20.00", "configured")
    wall = by_name["wall_clock_seconds"]
    assert (wall.used.text, wall.used.basis) == ("1,325 s", "measured")
    assert (wall.limit.text, wall.limit.basis) == ("7,200 s", "configured")


def test_native_and_unknown_limit_names_survive():
    detail = read_contract_snapshot(_result("status-simple-coding-agent"))
    by_name = {lim.name: lim for lim in detail.current.budget.limits}
    assert by_name["native.review_rounds"].is_native
    assert by_name["native.review_rounds"].used.text == "3"
    result = _result("status-active")
    result["current_attempt"]["budget"]["limits"].append(
        {
            "name": "warp_cores",
            "used": {"value": 1, "unit": "count", "basis": "measured"},
            "limit": {"value": 2, "unit": "count", "basis": "configured"},
        }
    )
    names = [lim.name for lim in read_contract_snapshot(result).current.budget.limits]
    assert "unknown" in names


# --- optional figures ------------------------------------------------------------


def test_optional_figures_follow_display_order_then_native():
    detail = read_contract_snapshot(_result("status-active"))
    assert [opt.name for opt in detail.current.figures] == [
        "cache_hit_rate",
        "turns",
        "tool_calls",
    ]
    simple = read_contract_snapshot(_result("status-simple-coding-agent"))
    assert [opt.name for opt in simple.current.figures] == [
        "cost_usd",
        "context_window",
        "tool_calls",
        "native.queue_depth",
    ]
    assert simple.current.figures[-1].is_native


def test_optional_figure_absent_renders_nothing_never_zero():
    result = _result("status-active")
    result["current_attempt"]["figures"] = {}
    assert read_contract_snapshot(result).current.figures == ()
    detail = read_contract_snapshot(_result("status-idle"))
    assert detail.current is None


def test_optional_figure_present_but_unavailable_renders_unknown():
    detail = read_contract_snapshot(_result("status-active"))
    by_name = {opt.name: opt for opt in detail.current.figures}
    assert by_name["cache_hit_rate"].figure.text == "unknown"
    assert by_name["cache_hit_rate"].figure.reason == "usage metadata missing for 2 responses"
    assert by_name["turns"].figure.text == "not applicable"


# --- freshness -------------------------------------------------------------------


def test_section_age_formats_like_the_fleet_table():
    old = section_freshness(
        "2026-11-06T09:12:05Z", now=1_791_000_000.0, poll_interval=5.0
    )
    assert old.age_text.endswith("ago")
    assert old.stale
    observed = 1_700_000_000.0
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(observed))
    fresh = section_freshness(stamp, now=observed + 12.0, poll_interval=5.0)
    assert fresh.age_text == "12 s ago"
    assert not fresh.stale


def test_section_stale_after_three_poll_intervals():
    kwargs = {"poll_interval": 5.0}
    at = "2026-11-06T09:12:05Z"
    base = 1_791_000_000.0
    observed = base - 1000.0
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(observed))
    assert not section_freshness(stamp, now=observed + 15.0, **kwargs).stale
    assert section_freshness(stamp, now=observed + 16.0, **kwargs).stale
    assert section_freshness(at, now=base, **kwargs).stale


def test_stale_since_names_the_oldest_stale_section():
    result = _result("status-active")
    result["instance"]["observed_at"] = "2026-11-06T09:12:05Z"
    result["intake"]["observed_at"] = "2026-11-06T09:12:06Z"
    result["current_attempt"]["observed_at"] = "2026-11-06T09:12:04Z"
    detail = read_contract_snapshot(result, now=1_791_000_000.0, poll_interval=5.0)
    assert detail.stale_since == "2026-11-06T09:12:04Z"
    assert stale_suffix(detail) == " (data stale since 2026-11-06T09:12:04Z)"
    assert detail.instance_observed.stale
    assert detail.intake_observed.stale
    assert detail.current.freshness.stale


def test_fresh_snapshot_has_no_stale_suffix():
    now = time.time()
    detail = read_contract_snapshot(_fresh_result(_result("status-active"), now), now=now)
    assert detail.stale_since is None
    assert stale_suffix(detail) == ""
    assert not detail.instance_observed.stale


def test_no_now_means_unknown_age_and_never_stale():
    detail = read_contract_snapshot(_result("status-active"))
    assert detail.instance_observed.age_text == "unknown"
    assert not detail.instance_observed.stale
    assert detail.stale_since is None


# --- halt, last finished, events ---------------------------------------------------


def test_halt_and_last_finished_with_delivery_link():
    detail = read_contract_snapshot(_result("offline-status-halted"))
    assert detail.halt.reason == "credential-failure"
    assert detail.halt.detail == "GitHub token rejected (401)"
    assert detail.halt.at == "2026-11-06T09:58:12Z"
    last = detail.last_finished
    assert last.issue_number == 41
    assert last.issue_title == "Fix pagination"
    assert last.outcome_label == "Delivered"
    assert last.delivery_number == 77
    assert last.delivery_url == (
        "https://github.com/example-owner/example-repo/pull/77"
    )
    assert not last.attention_required


def test_last_finished_attention_flag():
    result = _result("status-idle")
    result["last_finished"]["attention_required"] = True
    detail = read_contract_snapshot(result)
    assert detail.last_finished.attention_required
    assert contract_headline(detail) == "Needs operator: #41 — Delivered"


def test_no_halt_no_last_finished_is_none():
    detail = read_contract_snapshot(_result("status-idle"))
    assert detail.halt is None
    assert detail.last_finished is not None
    starting = read_contract_snapshot(_result("status-starting"))
    assert starting.halt is None
    assert starting.last_finished is None


def test_events_newest_first_capped_at_twenty():
    result = _result("status-idle")
    result["recent_events"] = [
        {
            "at": f"2026-11-06T08:{index:02d}:00Z",
            "level": "info",
            "event": "ready",
            "text": f"event {index}",
        }
        for index in range(25)
    ]
    detail = read_contract_snapshot(result)
    assert detail.events_total == 25
    assert len(detail.events) == 20
    assert detail.events[0].text == "event 24"
    assert detail.events[-1].text == "event 5"
    assert detail.events[0].short_time == "08:24:00"


def test_native_events_labelled_unknown_names_raw():
    result = _result("status-idle")
    result["recent_events"] = [
        {
            "at": "2026-11-06T08:50:00Z",
            "level": "info",
            "event": "native.model_tick",
            "text": "tick",
        },
        {
            "at": "2026-11-06T08:51:00Z",
            "level": "info",
            "event": "frobnicated",
            "text": "something new",
        },
    ]
    (native, unknown) = read_contract_snapshot(result).events
    assert native.is_native
    assert native.event == "native.model_tick"
    assert not unknown.is_native
    assert unknown.event == "frobnicated"


def test_unknown_fields_and_enums_never_break_the_snapshot():
    result = _result("status-idle")
    result["instance"]["state"] = "warping"
    result["instance"]["future_field"] = {"nested": [1, 2]}
    result["brand_new_section"] = "ignored"
    detail = read_contract_snapshot(result)
    assert detail is not None
    assert detail.instance_state == "unknown"
    assert contract_headline(detail) is None
    assert read_contract_snapshot(None) is None
    assert read_contract_snapshot([]) is None
    assert read_contract_snapshot({"ok": True}) is None


def test_incoherent_snapshot_reads_as_none():
    assert read_contract_snapshot({"instance": {"state": "active"}}) is None


# --- drawer web coverage -----------------------------------------------------------


def test_drawer_states_headlines_and_modules():
    cases = {
        "status-starting": "Starting: load-volumes",
        "status-recovering": "Recovering",
        "status-idle": "Idle: waiting for a ready issue",
        "status-active": "Working on #42 · run_step",
        "status-paused": "Paused",
        "offline-status-halted": "Halted (credential-failure)",
    }
    for name, headline in cases.items():
        client = _replay_client([_result(name)], now=time.time())
        body = client.get("/instances/web-0").text
        assert headline in body, name
        for module in (
            "01 // Contract status",
            "02 // Current attempt",
            "03 // Activity",
            "04 // Last halt",
            "05 // Recent events",
        ):
            assert module in body, (name, module)
        assert "Contract status" in body
        assert "/instances/web-0/history" not in body
        assert "Logs" not in body
        assert "Live #" not in body


def test_drawer_draining_headline_prefers_pending_stop():
    client = _replay_client([_result("status-draining")], now=time.time())
    assert "Stop requested" in client.get("/instances/web-0").text


def test_drawer_unknown_state_still_renders():
    result = _result("status-idle")
    result["instance"]["state"] = "warping"
    client = _replay_client([result], now=time.time())
    body = client.get("/instances/web-0").text
    assert "01 // Contract status" in body
    assert "unknown" in body
    assert "/instances/web-0/history" not in body


def test_drawer_budget_enforced_and_limits():
    client = _replay_client([_result("status-active")], now=time.time())
    body = client.get("/instances/web-0").text
    assert "1,834,221 tokens" in body
    assert "412,530 tokens" in body
    # The backend enforces effective tokens: exactly one enforced tag.
    assert body.count("enforced-tag") == 1
    assert "Effective tokens" in body
    # Every limit shows used against limit, with both bases.
    assert "$3.41" in body and "$20.00" in body
    assert "1,325 s" in body and "7,200 s" in body
    assert '<span class="basis">measured</span>' in body
    assert '<span class="basis">estimated</span>' in body
    assert '<span class="basis">configured</span>' in body


def test_drawer_optional_figures_and_unknown_states():
    client = _replay_client([_result("status-active")], now=time.time())
    body = client.get("/instances/web-0").text
    assert "118" in body  # tool_calls
    assert "figure-unknown" in body  # cache_hit_rate
    assert "usage metadata missing for 2 responses" in body  # the reason
    assert "figure-na" in body  # turns: not applicable
    assert "not applicable" in body
    assert "figure-unknown" in body and "not applicable" in body


def test_drawer_assumed_basis_cost_and_native_names():
    client = _replay_client([_result("status-simple-coding-agent")], now=time.time())
    body = client.get("/instances/web-0").text
    assert "200,000 tokens" in body
    assert '<span class="basis">assumed</span>' in body
    assert "$2.15" in body
    assert "native.queue_depth" in body
    assert "native.review_rounds" in body
    assert "native-tag" in body
    assert body.count("enforced-tag") == 1  # enforced raw


def test_drawer_unknown_basis_and_unit_render_unknown():
    result = _result("status-active")
    result["current_attempt"]["figures"] = {
        "continuations": {"value": 5, "unit": "count", "basis": "forthcoming"},
        "elapsed_seconds": {"value": 90, "unit": "fortnights", "basis": "measured"},
    }
    client = _replay_client([result], now=time.time())
    body = client.get("/instances/web-0").text
    assert body.count("figure-unknown") == 2
    assert "forthcoming" not in body
    assert "fortnights" not in body


def test_drawer_stale_sections_marked_headline_names_since():
    client = _replay_client([_result("status-active")])
    body = client.get("/instances/web-0").text
    # Every section shows its age and reads as stale; reach is unchanged.
    assert body.count("stale-tag") == 3
    assert "(data stale since 2026-11-06T09:12:04Z)" in body
    fleet = client.get("/").text
    assert "(data stale since 2026-11-06T09:12:04Z)" in fleet
    assert "pill-live" in fleet


def test_drawer_fresh_sections_have_no_stale():
    now = time.time()
    client = _replay_client([_fresh_result(_result("status-active"), now)], now=now)
    body = client.get("/instances/web-0").text
    assert "stale-tag" not in body
    assert "data stale since" not in body
    assert "Working on #42 · run_step" in body


def test_drawer_truncated_snapshot_marks_trimmed_events():
    result = _result("status-idle")
    result["truncated"] = True
    result["recent_events"] = [
        {
            "at": f"2026-11-06T08:{index:02d}:00Z",
            "level": "info",
            "event": "ready",
            "text": f"event {index}",
        }
        for index in range(25)
    ]
    client = _replay_client([result], now=time.time())
    body = client.get("/instances/web-0").text
    assert "Truncated snapshot" in body
    assert "Showing the latest 20 of 25 (older trimmed)." in body
    assert body.count("<li>") == 20
    assert "event 24" in body
    assert "event 04" not in body


def test_drawer_halt_and_last_finished():
    client = _replay_client([_result("offline-status-halted")], now=time.time())
    body = client.get("/instances/web-0").text
    assert "credential-failure" in body
    assert "GitHub token rejected (401)" in body
    assert "2026-11-06T09:58:12Z" in body
    assert "Delivered" in body
    assert "https://github.com/example-owner/example-repo/pull/77" in body
    assert "#77" in body


def test_drawer_last_finished_attention_flag():
    result = _result("status-idle")
    result["last_finished"]["attention_required"] = True
    client = _replay_client([result], now=time.time())
    body = client.get("/instances/web-0").text
    assert "needs operator" in body
    assert "Needs operator: #41 — Delivered" in body


def test_drawer_current_attempt_facts():
    client = _replay_client([_result("status-active")], now=time.time())
    body = client.get("/instances/web-0").text
    assert "#42 · Add CSV export" in body
    assert "run_step" in body
    assert "2026-11-06T08:50:00Z" in body
    assert "2026-11-06T09:11:58Z" in body


def test_drawer_idle_keeps_modules_without_attempt():
    client = _replay_client([_result("status-idle")], now=time.time())
    body = client.get("/instances/web-0").text
    assert "No attempt in progress." in body
    assert "Delivered" in body  # last finished stays visible
    assert "sealed" in body  # its events stay visible
    assert "hx-get=\"/instances/web-0/activity\"" not in body


def test_drawer_active_loads_contract_activity():
    client = _replay_client([_result("status-active")], now=time.time())
    body = client.get("/instances/web-0").text
    assert "hx-get=\"/instances/web-0/activity\"" in body
    assert 'data-scroll-key="activity-body"' in body


def test_activity_shows_phase_last_activity_and_attempt_events():
    client = _replay_client([_result("status-active")], now=time.time())
    body = client.get("/instances/web-0/activity").text
    assert body is not None
    assert "run_step" in body
    assert "last activity" in body.lower()
    assert "2026-11-06T09:11:58Z" in body
    assert "claim_opened" in body
    assert "claimed issue #42" in body
    # No Attempt-log views: no Last call pane, no log or progress links.
    assert "Last call" not in body
    assert "call log" not in body
    assert "response log" not in body
    assert "/attempts/" not in body


def test_activity_summary_names_phase():
    client = _replay_client([_result("status-active")], now=time.time())
    body = client.get("/instances/web-0/activity").text
    assert 'hx-swap-oob="true"' in body
    assert "<strong>run_step</strong>" in body


def test_activity_without_attempt_is_a_hint():
    client = _replay_client([_result("status-idle")], now=time.time())
    body = client.get("/instances/web-0/activity").text
    assert "No attempt in progress." in body
    assert "Last call" not in body


def test_activity_attempt_scoped_events_only():
    result = _result("status-active")
    result["recent_events"] = [
        {
            "at": "2026-11-06T08:50:00Z",
            "attempt_id": "#42/1",
            "issue": 42,
            "level": "info",
            "event": "claim_opened",
            "text": "claimed issue #42",
        },
        {
            "at": "2026-11-06T08:45:01Z",
            "attempt_id": "#41/1",
            "level": "info",
            "event": "sealed",
            "text": "archive sealed for #41/1",
        },
    ]
    client = _replay_client([result], now=time.time())
    body = client.get("/instances/web-0/activity").text
    assert "claimed issue #42" in body
    assert "archive sealed" not in body


def test_contract_detail_defaults_keep_old_constructions_working():
    detail = ContractDetail(pending_stop=True)
    assert contract_headline(detail) == "Stop requested"
    assert detail.current is None
    assert detail.events == ()
    assert stale_suffix(detail) == ""
