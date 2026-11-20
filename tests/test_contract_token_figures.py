"""Contract 1.1 token figures in the dashboard model (no view change).

Parses the pinned package's 1.1 fixture (``status-active-1-1``) into the
Status Snapshot view model: ``context_threshold`` and
``native.handoff_threshold_tokens``, ``subagents_running`` /
``subagents_ended``, and ``budget.by_origin`` with one cell per origin.
A 1.0 fixture still parses with the new figures absent, and the legacy
path's models carry the same fields as absent/unknown (ADR 0017). None of
this renders anywhere yet; the view is the next issue.
"""

from __future__ import annotations

import copy
import json

from agent_dashboard.contract_status import (
    UNKNOWN,
    read_attempt_gauge,
    read_contract_snapshot,
)
from agent_dashboard.drawer import DrawerDetail, read_drawer
from agent_dashboard.snapshot import SnapshotDetail, read_snapshot


def _result(name: str) -> dict:
    """The ``result`` of a pinned package ``status`` fixture."""
    from agent_operator_contract import documents

    return json.loads(documents.load_fixture(name)["stdout"])["result"]


def _fig(value, unit, basis="measured", reason=None) -> dict:
    fig = {"value": value, "unit": unit, "basis": basis}
    if reason is not None:
        fig["reason"] = reason
    return fig


def _with_unknown_origin(result: dict) -> dict:
    """A 1.1 result with an extra origin outside the known names."""
    result = copy.deepcopy(result)
    cells = result["current_attempt"]["budget"]["by_origin"]
    cells["nightly"] = {
        name: _fig(10, unit)
        for name, unit in (
            ("input_tokens", "tokens"),
            ("cache_read_tokens", "tokens"),
            ("cache_write_tokens", "tokens"),
            ("output_tokens", "tokens"),
            ("effective_tokens", "tokens"),
            ("cost_usd", "usd"),
        )
    }
    return result


# --- 1.1: thresholds and sub-agent counts --------------------------------------


def test_1_1_threshold_figures_keep_value_basis_and_reason():
    detail = read_contract_snapshot(_result("status-active-1-1"))
    assert detail is not None and detail.current is not None
    gauge = detail.current.gauge
    # Token figures omit the unit word in the layout (the row names it).
    assert gauge.context_threshold.text == "140,000"
    assert gauge.context_threshold.basis == "configured"
    assert gauge.context_threshold.state == "value"
    assert gauge.handoff_threshold.text == "150,000"
    assert gauge.handoff_threshold.basis == "configured"
    assert gauge.handoff_threshold.state == "value"


def test_1_1_subagent_counts_keep_value_basis_and_reason():
    detail = read_contract_snapshot(_result("status-active-1-1"))
    assert detail is not None and detail.current is not None
    gauge = detail.current.gauge
    assert gauge.subagents_running.text == "1"
    assert gauge.subagents_running.basis == "measured"
    assert gauge.subagents_running.state == "value"
    assert gauge.subagents_ended.text == "3"
    assert gauge.subagents_ended.basis == "measured"
    assert gauge.subagents_ended.state == "value"


def test_1_1_context_used_keeps_its_estimated_basis_and_reason():
    detail = read_contract_snapshot(_result("status-active-1-1"))
    assert detail is not None and detail.current is not None
    gauge = detail.current.gauge
    assert gauge.ctx_main.basis == "estimated"
    assert gauge.ctx_main.reason == "anchored on provider-reported input"


# --- 1.1: spend per origin ------------------------------------------------------


def test_1_1_by_origin_orders_known_origins_then_unknown_by_name():
    detail = read_contract_snapshot(_with_unknown_origin(_result("status-active-1-1")))
    assert detail is not None and detail.current is not None
    cells = detail.current.budget.by_origin
    assert cells is not None
    assert [cell.name for cell in cells] == ["main", "subagent", "handoff", "nightly"]
    assert [cell.known for cell in cells] == [True, True, True, False]


def test_1_1_estimated_origin_cell_is_upper_bound_not_a_present_figure():
    detail = read_contract_snapshot(_result("status-active-1-1"))
    assert detail is not None and detail.current is not None
    cells = detail.current.budget.by_origin
    assert cells is not None
    subagent = next(cell for cell in cells if cell.name == "subagent")
    assert subagent.known is True
    assert subagent.effective_tokens.state == "value"
    assert subagent.effective_tokens.basis == "estimated"
    assert (
        subagent.effective_tokens.reason
        == "upper bound: worst-case charge for a response without usage"
    )
    assert subagent.cost_usd.state == "value"
    assert subagent.cost_usd.text == "$0.70"


def test_1_1_unavailable_origin_cell_is_unknown_never_zero():
    detail = read_contract_snapshot(_result("status-active-1-1"))
    assert detail is not None and detail.current is not None
    cells = detail.current.budget.by_origin
    assert cells is not None
    handoff = next(cell for cell in cells if cell.name == "handoff")
    for figure in (
        handoff.input_tokens,
        handoff.cache_read_tokens,
        handoff.cache_write_tokens,
        handoff.output_tokens,
        handoff.effective_tokens,
        handoff.cost_usd,
    ):
        assert figure.state == "unknown"
        assert figure.text == UNKNOWN
        assert figure.reason == "not recorded"
        assert "0" not in figure.text


def test_1_1_measured_origin_cell_reads_as_present():
    detail = read_contract_snapshot(_result("status-active-1-1"))
    assert detail is not None and detail.current is not None
    cells = detail.current.budget.by_origin
    assert cells is not None
    main = next(cell for cell in cells if cell.name == "main")
    assert main.input_tokens.state == "value"
    assert main.input_tokens.text == "52,000 tokens"
    assert main.effective_tokens.basis == "measured"


def test_1_1_unknown_origin_keeps_its_raw_name():
    detail = read_contract_snapshot(_with_unknown_origin(_result("status-active-1-1")))
    assert detail is not None and detail.current is not None
    cells = detail.current.budget.by_origin
    assert cells is not None
    nightly = cells[-1]
    assert nightly.name == "nightly"
    assert nightly.known is False
    assert nightly.input_tokens.text == "10 tokens"


def test_1_1_several_unknown_origins_sort_by_raw_name():
    result = _with_unknown_origin(_result("status-active-1-1"))
    cells = result["current_attempt"]["budget"]["by_origin"]
    first = next(iter(cells))
    cells["aardvark"] = copy.deepcopy(cells[first])
    detail = read_contract_snapshot(result)
    assert detail is not None and detail.current is not None
    parsed = detail.current.budget.by_origin
    assert parsed is not None
    assert [cell.name for cell in parsed] == [
        "main",
        "subagent",
        "handoff",
        "aardvark",
        "nightly",
    ]


# --- 1.0: the new figures are absent, never an error ----------------------------


def test_1_0_snapshot_parses_with_new_figures_absent():
    detail = read_contract_snapshot(_result("status-active"))
    assert detail is not None and detail.current is not None
    assert detail.current.budget.by_origin is None
    gauge = detail.current.gauge
    assert gauge.context_threshold.state == "unknown"
    assert gauge.handoff_threshold.state == "unknown"
    assert gauge.subagents_running.state == "unknown"
    assert gauge.subagents_ended.state == "unknown"


def test_absent_context_estimate_tokens_is_not_an_error():
    result = _result("status-active")
    assert "native.context_estimate_tokens" not in result["current_attempt"]["figures"]
    assert read_contract_snapshot(result) is not None
    injected = copy.deepcopy(result)
    injected["current_attempt"]["figures"]["native.context_estimate_tokens"] = _fig(
        42000, "tokens", "estimated", "local estimate, no measured response yet"
    )
    assert read_contract_snapshot(injected) is not None


def test_no_current_attempt_means_no_by_origin_and_unknown_figures():
    idle = _result("status-idle")
    detail = read_contract_snapshot(idle)
    assert detail is not None
    assert detail.current is None


def test_read_attempt_gauge_without_by_origin_reads_none():
    from agent_operator_contract import status as contract_status_lib

    budget = contract_status_lib.Budget(
        raw_tokens=contract_status_lib.Figure(1, "tokens", "measured"),
        effective_tokens=contract_status_lib.Figure(1, "tokens", "measured"),
        enforced_tokens="raw",
        limits=(),
    )
    gauge = read_attempt_gauge(budget, {})
    assert gauge.context_threshold.state == "unknown"
    assert gauge.subagents_ended.state == "unknown"


# --- no rendered page changes ------------------------------------------------------


def test_1_1_snapshot_renders_the_token_figures():
    """A 1.1 snapshot renders the Main context and Spend by origin figures."""
    import asyncio

    from fake_gateway import FakeGateway
    from fastapi.testclient import TestClient

    from agent_dashboard.app import create_app
    from agent_dashboard.collector import run_cycle
    from agent_dashboard.config import Settings
    from agent_dashboard.fleet import FleetState
    from agent_dashboard.gateway import CONTRACT_PATH, ManagedInstance

    gateway = FakeGateway(
        [
            ManagedInstance(
                "web-0",
                "abc123",
                "running",
                path=CONTRACT_PATH,
                instance="web-0",
                exec_user="agent",
                backend="coding-agent",
            )
        ]
    )
    gateway.set_contract_snapshot("abc123", _with_unknown_origin(_result("status-active-1-1")))
    state = FleetState()
    asyncio.run(run_cycle(state, gateway, max_concurrent_execs=4, now=1_000_000.0))
    client = TestClient(
        app=create_app(
            state=state,
            settings=Settings(poll_interval=5.0),
            gateway=gateway,
            start_poller=False,
        ),
        base_url="http://localhost",
    )
    for path in ("/", "/instances/web-0"):
        body = client.get(path).text
        assert "<dt>context_threshold</dt>" not in body
        assert "<dt>subagents_running</dt>" not in body
        assert "<dt>subagents_ended</dt>" not in body
        assert "<dt>native.handoff_threshold_tokens</dt>" not in body
        # The Main context figure compares used against the Usable Threshold.
        assert "140,000" in body
    drawer = client.get("/instances/web-0").text
    # The Spend table lists every origin, estimated ones as an upper bound.
    assert "nightly" in drawer
    assert "≤ upper bound" in drawer


# --- legacy path: the same fields, absent/unknown ---------------------------------


def test_legacy_snapshot_detail_carries_the_new_fields_as_absent():
    detail = SnapshotDetail()
    assert detail.context_threshold_text == "unavailable"
    assert detail.context_threshold_basis == ""
    assert detail.handoff_threshold_text == "unavailable"
    assert detail.handoff_threshold_basis == ""
    assert detail.subagents_running is None
    assert detail.subagents_ended is None
    assert detail.by_origin is None


def test_legacy_snapshot_read_leaves_the_new_fields_absent():
    detail = read_snapshot(None, snapshot_at=None, now=0.0)
    assert detail.context_threshold_text == "unavailable"
    assert detail.subagents_running is None
    assert detail.by_origin is None


def test_legacy_drawer_detail_carries_the_new_fields_as_unknown():
    detail = DrawerDetail()
    assert detail.context_threshold.text == "unavailable"
    assert detail.handoff_threshold.text == "unavailable"
    assert detail.subagents_running.text == "unavailable"
    assert detail.subagents_ended.text == "unavailable"
    assert detail.by_origin is None


def test_legacy_drawer_read_leaves_the_new_fields_unknown():
    detail = read_drawer(None)
    assert detail.context_threshold.text == "unavailable"
    assert detail.handoff_threshold.text == "unavailable"
    assert detail.subagents_running.text == "unavailable"
    assert detail.subagents_ended.text == "unavailable"
    assert detail.by_origin is None
