"""Collector: exec classification and the poll cycle (no timer here)."""

import asyncio
import json
from pathlib import Path

import pytest
from fake_gateway import FakeGateway

from agent_dashboard import collector
from agent_dashboard.collector import (
    AGENT_UNREACHABLE,
    GOOD,
    NOT_SUPPORTED,
    NOT_SUPPORTED_REASON,
    SNAPSHOT_FAILED,
    TIMEOUT,
    UNSUPPORTED_OPERATION,
    classify_telemetry,
    run_cycle,
    timeout_outcome,
)
from agent_dashboard.fleet import FleetState
from agent_dashboard.gateway import (
    DiscoveryTimeoutError,
    GatewayError,
    ManagedInstance,
    TelemetryExec,
)

FIXTURES = Path(__file__).parent / "fixtures" / "contract" / "v1"


def read_fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


GOOD_SNAPSHOTS = [
    "snapshot-idle-with-last-finished.json",
    "snapshot-attempt-in-progress.json",
    "snapshot-soft-threshold-handoff.json",
    "snapshot-recovery-hold.json",
    "snapshot-truncated.json",
]

ERROR_FIXTURES = [
    "error-agent-unreachable.json",
    "error-unsupported-operation.json",
    "error-snapshot-failed.json",
]


def test_contract_fixtures_are_present_with_provenance():
    """Every poll outcome has a recorded fixture; the source commit is noted."""
    for name in GOOD_SNAPSHOTS + ERROR_FIXTURES:
        payload = json.loads(read_fixture(name))
        assert isinstance(payload, dict), name
    source = (FIXTURES / "SOURCE.txt").read_text()
    assert "simple-coding-agent" in source
    assert "58401c44753765813e47cd2367e79373ba81c437" in source


@pytest.mark.parametrize("name", GOOD_SNAPSHOTS)
def test_exit_zero_with_snapshot_is_good(name):
    outcome, snapshot = classify_telemetry(read_fixture(name), 0, now=1000.0)
    assert outcome.kind == GOOD
    assert outcome.reason == ""
    assert outcome.at == 1000.0
    assert snapshot == json.loads(read_fixture(name))
    assert snapshot["schema_version"] == 1


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("error-agent-unreachable.json", AGENT_UNREACHABLE),
        ("error-unsupported-operation.json", UNSUPPORTED_OPERATION),
        ("error-snapshot-failed.json", SNAPSHOT_FAILED),
    ],
)
def test_error_object_uses_its_code(name, kind):
    payload = json.loads(read_fixture(name))
    outcome, snapshot = classify_telemetry(read_fixture(name), 1, now=1000.0)
    assert outcome.kind == kind
    assert outcome.reason == payload["error"]["message"]
    assert outcome.at == 1000.0
    assert snapshot is None


def test_error_code_wins_over_exit_code():
    body = json.dumps(
        {"ok": False, "error": {"code": "agent_unreachable", "message": "no socket"}}
    ).encode()
    outcome, _ = classify_telemetry(body, 0, now=1.0)
    assert outcome.kind == AGENT_UNREACHABLE
    assert outcome.reason == "no socket"


def test_unknown_error_code_is_snapshot_failed():
    body = json.dumps(
        {"ok": False, "error": {"code": "future_code", "message": "new agent"}}
    ).encode()
    outcome, snapshot = classify_telemetry(body, 1, now=1.0)
    assert outcome.kind == SNAPSHOT_FAILED
    assert "future_code" in outcome.reason
    assert snapshot is None


def test_ok_false_without_error_is_snapshot_failed():
    outcome, snapshot = classify_telemetry(b'{"ok": false}', 1, now=1.0)
    assert outcome.kind == SNAPSHOT_FAILED
    assert snapshot is None


@pytest.mark.parametrize(
    "stdout",
    [
        b"",
        b"not json at all",
        b"usage: agentctl [--help] {status}\nagentctl: error: wat\n",
        b"[1, 2, 3]",
        b"null",
        b'"just a string"',
        b"42",
    ],
)
def test_non_object_stdout_is_not_supported(stdout):
    outcome, snapshot = classify_telemetry(stdout, 2, now=1.0)
    assert outcome.kind == NOT_SUPPORTED
    assert outcome.reason == NOT_SUPPORTED_REASON
    assert outcome.reason == "telemetry not supported by this agent version"
    assert snapshot is None


def test_timeout_outcome_reason_pins_exec_timeout():
    outcome = timeout_outcome(5.0, now=1.0)
    assert outcome.kind == TIMEOUT
    assert outcome.reason == "docker exec timed out after 5 s"
    assert outcome.at == 1.0


def test_collector_exports_every_poll_outcome_kind():
    assert collector.POLL_OUTCOME_KINDS == {
        GOOD,
        AGENT_UNREACHABLE,
        UNSUPPORTED_OPERATION,
        SNAPSHOT_FAILED,
        NOT_SUPPORTED,
        TIMEOUT,
    }


def run(state: FleetState, gateway: FakeGateway, **kwargs) -> None:
    kwargs.setdefault("now", 1000.0)
    asyncio.run(run_cycle(state, gateway, **kwargs))


def test_good_snapshot_is_live_with_snapshot():
    state = FleetState()
    gateway = FakeGateway([ManagedInstance("worker-1", "abc123", "running")])
    gateway.set_snapshot("abc123", {"schema_version": 1})
    run(state, gateway)
    rows, error = asyncio.run(state.snapshot(now=1012.0))
    assert error is None
    assert [(r.name, r.reach) for r in rows] == [("worker-1", "live")]
    assert rows[0].snapshot_age == pytest.approx(12.0)
    assert rows[0].has_snapshot is True
    assert rows[0].age_text == "12 s"
    assert gateway.telemetry_calls == ["abc123"]


def test_failure_keeps_last_good_snapshot_with_age():
    state = FleetState()
    gateway = FakeGateway([ManagedInstance("worker-1", "abc123", "running")])
    gateway.set_snapshot("abc123", {"schema_version": 1})
    run(state, gateway, now=1000.0)
    gateway.set_error_code("abc123", "agent_unreachable", "no socket")
    run(state, gateway, now=1030.0)
    rows, _ = asyncio.run(state.snapshot(now=1045.0))
    assert rows[0].reach == "unreachable"
    assert rows[0].has_snapshot is True
    assert rows[0].snapshot_age == pytest.approx(45.0)
    assert rows[0].age_text == "45 s"
    assert "no socket" in rows[0].headline


def test_container_recreation_drops_old_snapshot():
    state = FleetState()
    gateway = FakeGateway([ManagedInstance("worker-1", "abc123", "running")])
    gateway.set_snapshot("abc123", {"schema_version": 1})
    run(state, gateway, now=1000.0)
    gateway.set_rows([ManagedInstance("worker-1", "def456", "running")])
    gateway.set_error_code("def456", "snapshot_failed", "build broke")
    run(state, gateway, now=1010.0)
    rows, _ = asyncio.run(state.snapshot(now=1020.0))
    assert rows[0].reach == "stale"
    assert rows[0].has_snapshot is False
    assert rows[0].snapshot_age is None


def test_exited_containers_get_no_exec():
    state = FleetState()
    gateway = FakeGateway(
        [
            ManagedInstance("up", "id-up", "running"),
            ManagedInstance("down", "id-down", "exited"),
        ]
    )
    gateway.set_snapshot("id-up", {"schema_version": 1})
    run(state, gateway)
    assert gateway.telemetry_calls == ["id-up"]
    rows, _ = asyncio.run(state.snapshot())
    assert {r.name: r.reach for r in rows} == {"down": "not running", "up": "live"}


def test_exec_uses_container_id_resolved_same_cycle():
    state = FleetState()
    gateway = FakeGateway([ManagedInstance("some-name", "real-id-9", "running")])
    gateway.set_snapshot("real-id-9", {"schema_version": 1})
    run(state, gateway)
    assert gateway.telemetry_calls == ["real-id-9"]


def test_concurrency_cap_and_single_flight_per_instance():
    state = FleetState()
    rows = [ManagedInstance(f"w-{i}", f"id-{i}", "running") for i in range(6)]
    gateway = FakeGateway(rows)
    gateway.delay = 0.02
    for i in range(6):
        gateway.set_snapshot(f"id-{i}", {"schema_version": 1})
    run(state, gateway, max_concurrent_execs=2)
    assert gateway.peak_in_flight <= 2
    assert gateway.peak_in_flight == 2
    assert all(peak == 1 for peak in gateway.peak_per_id.values())
    saved, _ = asyncio.run(state.snapshot())
    assert len(saved) == 6


def test_gateway_error_mid_cycle_is_unreachable():
    class ExplodingGateway(FakeGateway):
        async def telemetry(self, container_id: str) -> TelemetryExec:
            raise GatewayError("boom")

    state = FleetState()
    run(state, ExplodingGateway([ManagedInstance("w", "id", "running")]))
    rows, _ = asyncio.run(state.snapshot())
    assert rows[0].reach == "unreachable"
    assert "boom" in rows[0].headline


def test_dashboard_timeout_is_unreachable():
    state = FleetState()
    gateway = FakeGateway([ManagedInstance("w", "id", "running")])
    gateway.set_timeout("id")
    run(state, gateway, exec_timeout=5.0)
    rows, _ = asyncio.run(state.snapshot())
    assert rows[0].reach == "unreachable"
    assert rows[0].headline == "Unreachable: docker exec timed out after 5 s"


def test_discovery_failure_keeps_rows_and_sets_banner():
    state = FleetState()
    gateway = FakeGateway([ManagedInstance("keep", "id", "running")])
    gateway.set_snapshot("id", {"schema_version": 1})
    run(state, gateway)
    gateway._error = DiscoveryTimeoutError("docker ps timed out")
    run(state, gateway)
    rows, error = asyncio.run(state.snapshot())
    assert error == "docker ps timed out"
    assert [r.name for r in rows] == ["keep"]
    assert gateway.telemetry_calls == ["id"]


def test_discovery_recovery_clears_banner():
    state = FleetState()
    gateway = FakeGateway(error=GatewayError("boom"))
    run(state, gateway)
    _, error = asyncio.run(state.snapshot())
    assert error == "boom"
    gateway._error = None
    gateway.set_rows([])
    run(state, gateway)
    _, error = asyncio.run(state.snapshot())
    assert error is None


def test_removed_container_disappears_next_cycle():
    state = FleetState()
    gateway = FakeGateway([ManagedInstance("gone", "id", "running")])
    run(state, gateway)
    gateway.set_rows([])
    run(state, gateway)
    rows, _ = asyncio.run(state.snapshot())
    assert rows == []


def test_run_cycle_rejects_bad_concurrency():
    with pytest.raises(ValueError):
        asyncio.run(run_cycle(FleetState(), FakeGateway([]), max_concurrent_execs=0))
