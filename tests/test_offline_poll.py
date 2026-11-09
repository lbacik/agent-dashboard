"""Offline Read poll loop and stopped contract rendering.

A stopped contract instance is never started: the poll loop re-inspects it
and runs one throwaway helper per stop (keyed by container ID plus finish
time) through the shared exec semaphore. Rows and drawers show the last
Halt, the last finished attempt and the Intake; no Control console is
offered on a stopped contract instance; HTTP handlers never start an
Offline Read.
"""

import asyncio
import json

from fake_gateway import FakeGateway
from fastapi.testclient import TestClient

from agent_dashboard import collector
from agent_dashboard.app import create_app
from agent_dashboard.collector import (
    GOOD,
    INSTANCE_STARTED,
    INSTANCE_STARTED_REASON,
    OFFLINE_UNAVAILABLE_PREFIX,
    SNAPSHOT_FAILED,
    classify_offline_status,
    run_cycle,
)
from agent_dashboard.config import Settings
from agent_dashboard.fleet import FleetState
from agent_dashboard.gateway import (
    CONTRACT_PATH,
    ControlTimeoutError,
    GatewayError,
    ManagedInstance,
    OfflineInspect,
    OfflineMount,
)

IMAGE = "sha256:" + "ab" * 32
OTHER_IMAGE = "sha256:" + "cd" * 32
MOUNTS = (OfflineMount("data-vol", "/data"),)
FINISH_A = "2026-11-06T09:58:12.123456789Z"
FINISH_B = "2026-11-06T11:00:00.000000000Z"


def _stopped(name="web-1", cid="abc123", exec_user="agent"):
    return ManagedInstance(
        name,
        cid,
        "exited",
        path=CONTRACT_PATH,
        instance=name,
        exec_user=exec_user,
        backend="coding-agent",
    )


def _inspect(image=IMAGE, finished_at=FINISH_A, running=False):
    return OfflineInspect(
        running=running, finished_at=finished_at, image=image, mounts=MOUNTS
    )


def _result(name):
    from agent_operator_contract import documents

    return json.loads(documents.load_fixture(name)["stdout"])["result"]


def _rejected_body():
    return json.dumps(
        {
            "contract": "1.0",
            "ok": False,
            "error": {"code": "rejected", "message": "nope", "details": {}},
        }
    ).encode()


def _run(state, gateway, **kwargs):
    kwargs.setdefault("now", 1000.0)
    asyncio.run(run_cycle(state, gateway, **kwargs))


def _rows(state, now=1012.0):
    rows, error = asyncio.run(state.snapshot(now=now))
    assert error is None
    return {r.name: r for r in rows}


# --- classify --------------------------------------------------------------


def test_rejected_helper_maps_to_instance_started():
    outcome, snapshot = classify_offline_status(_rejected_body(), 1, now=1.0)
    assert outcome.kind == INSTANCE_STARTED
    assert outcome.reason == INSTANCE_STARTED_REASON
    assert snapshot is None


# --- once per stop ---------------------------------------------------------


def test_one_read_per_stop_second_cycle_starts_no_helper():
    state = FleetState()
    gateway = FakeGateway([_stopped()])
    gateway.set_inspect("abc123", _inspect())
    gateway.set_offline_snapshot(IMAGE, _result("offline-status-halted"))
    _run(state, gateway)
    assert len(gateway.offline_calls) == 1
    assert len(gateway.inspect_calls) == 1
    _run(state, gateway, now=1010.0)
    # Inspect re-runs to learn the stop is the same one; the helper does not.
    assert len(gateway.inspect_calls) == 2
    assert len(gateway.offline_calls) == 1


def test_new_finish_time_reads_again():
    state = FleetState()
    gateway = FakeGateway([_stopped()])
    gateway.set_inspect("abc123", _inspect(finished_at=FINISH_A))
    gateway.set_offline_snapshot(IMAGE, _result("offline-status-halted"))
    _run(state, gateway)
    assert len(gateway.offline_calls) == 1
    gateway.set_inspect("abc123", _inspect(finished_at=FINISH_B))
    _run(state, gateway, now=1010.0)
    assert len(gateway.offline_calls) == 2


def test_offline_reads_share_the_global_exec_limit():
    state = FleetState()
    rows = [_stopped(f"web-{i}", f"cid{i:04d}") for i in range(4)]
    gateway = FakeGateway(rows)
    for inst in rows:
        gateway.set_inspect(inst.container_id, _inspect())
        gateway.set_offline_snapshot(IMAGE, _result("offline-status-halted"))
    gateway.offline_delay = 0.05
    _run(state, gateway, max_concurrent_execs=2)
    assert gateway.peak_offline_in_flight <= 2
    assert gateway.peak_offline_in_flight >= 1
    assert len(gateway.offline_calls) == 4


def test_rejected_helper_renders_started_and_stores_no_key():
    state = FleetState()
    gateway = FakeGateway([_stopped()])
    gateway.set_inspect("abc123", _inspect())
    gateway.set_offline_status(IMAGE, _rejected_body(), 1)
    _run(state, gateway)
    row = _rows(state)["web-1"]
    assert row.headline == INSTANCE_STARTED_REASON
    assert row.has_snapshot is False
    # No key was stored, but the row still waits: a retry re-runs the helper.
    gateway.set_offline_snapshot(IMAGE, _result("offline-status-halted"))
    _run(state, gateway, now=1010.0)
    assert len(gateway.offline_calls) == 2
    assert _rows(state, now=1022.0)["web-1"].has_snapshot is True


def test_inspect_running_means_started_without_a_helper():
    state = FleetState()
    gateway = FakeGateway([_stopped()])
    gateway.set_inspect("abc123", _inspect(running=True))
    _run(state, gateway)
    assert gateway.offline_calls == []
    row = _rows(state)["web-1"]
    assert row.headline == INSTANCE_STARTED_REASON


def test_invalid_image_is_unavailable_with_reason_and_key():
    state = FleetState()
    gateway = FakeGateway([_stopped()])
    gateway.set_inspect("abc123", _inspect(image="ubuntu:22.04"))
    _run(state, gateway)
    assert gateway.offline_calls == []
    row = _rows(state)["web-1"]
    assert row.headline.startswith(OFFLINE_UNAVAILABLE_PREFIX)
    assert "ubuntu:22.04" in row.headline
    assert row.has_snapshot is False
    # The key is stored: the same stop is not retried.
    _run(state, gateway, now=1010.0)
    assert gateway.offline_calls == []
    assert len(gateway.inspect_calls) == 2


def test_timeout_retries_next_cycle():
    state = FleetState()
    gateway = FakeGateway([_stopped()])
    gateway.set_inspect("abc123", _inspect())
    gateway.set_offline_error(IMAGE, ControlTimeoutError("docker run timed out"))
    _run(state, gateway)
    row = _rows(state)["web-1"]
    assert row.headline == "Not running"
    assert row.has_snapshot is False
    gateway.set_offline_snapshot(IMAGE, _result("offline-status-halted"))
    _run(state, gateway, now=1010.0)
    assert len(gateway.offline_calls) == 2
    assert _rows(state, now=1022.0)["web-1"].has_snapshot is True


def test_inspect_failure_skips_and_retries():
    state = FleetState()
    gateway = FakeGateway([_stopped()])
    gateway.set_inspect_error("abc123", GatewayError("boom"))
    _run(state, gateway)
    row = _rows(state)["web-1"]
    assert row.headline == "Not running"
    gateway.set_inspect("abc123", _inspect())
    gateway.set_offline_snapshot(IMAGE, _result("offline-status-halted"))
    _run(state, gateway, now=1010.0)
    assert len(gateway.offline_calls) == 1


def test_misconfigured_and_duplicated_stopped_rows_are_skipped():
    state = FleetState()
    bad = ManagedInstance(
        "web-bad",
        "bad001",
        "exited",
        path=CONTRACT_PATH,
        instance="Bad Name!",
        exec_user="agent",
        note="misconfigured contract instance: invalid instance name 'Bad Name!'",
    )
    first = ManagedInstance(
        "dup",
        "aaa111",
        "exited",
        path=CONTRACT_PATH,
        instance="dup",
        exec_user="agent",
        note="duplicated instance address 'dup': neither is polled",
    )
    second = ManagedInstance(
        "dup",
        "bbb222",
        "exited",
        path=CONTRACT_PATH,
        instance="dup",
        exec_user="agent",
        note="duplicated instance address 'dup': neither is polled",
    )
    gateway = FakeGateway([bad, first, second])
    gateway.set_inspect("aaa111", _inspect())
    gateway.set_offline_snapshot(IMAGE, _result("offline-status-halted"))
    _run(state, gateway)
    assert gateway.inspect_calls == []
    assert gateway.offline_calls == []


# --- fleet rendering -------------------------------------------------------


def test_stopped_halted_row_shows_halt_and_needs_operator():
    state = FleetState()
    gateway = FakeGateway([_stopped()])
    gateway.set_inspect("abc123", _inspect())
    gateway.set_offline_snapshot(IMAGE, _result("offline-status-halted"))
    _run(state, gateway)
    row = _rows(state)["web-1"]
    assert row.reach == "not running"
    assert "credential-failure" in row.headline
    assert row.rank == 0
    assert row.has_snapshot is True
    assert row.contract is not None
    assert row.contract.halt is not None
    assert row.contract.halt.reason == "credential-failure"
    assert row.contract.last_finished is not None
    assert row.contract.last_finished.issue_number == 41
    assert row.contract.intake_state == "open"
    # No Control console on a stopped contract instance.
    assert row.control_raw_missing is True
    assert row.control_raw is None
    assert row.control_chips == ()


def test_stopped_idle_row_is_offline_without_halt():
    state = FleetState()
    gateway = FakeGateway([_stopped()])
    gateway.set_inspect("abc123", _inspect())
    gateway.set_offline_snapshot(IMAGE, _result("status-idle"))
    _run(state, gateway)
    row = _rows(state)["web-1"]
    assert row.reach == "not running"
    assert row.rank == 7
    assert row.has_snapshot is True


def test_stopped_row_ignores_live_era_snapshot():
    state = FleetState()
    gateway = FakeGateway(
        [
            ManagedInstance(
                "web-1",
                "abc123",
                "running",
                path=CONTRACT_PATH,
                instance="web-1",
                exec_user="agent",
            )
        ]
    )
    gateway.set_contract_snapshot("abc123", _result("status-active"))
    _run(state, gateway)
    assert _rows(state)["web-1"].has_snapshot is True
    # The container stops before the Offline Read lands: the live snapshot
    # must not render on the stopped row.
    gateway.set_rows([_stopped()])
    gateway.set_inspect_error("abc123", GatewayError("boom"))
    _run(state, gateway, now=1010.0)
    row = _rows(state, now=1022.0)["web-1"]
    assert row.state == "not running"
    assert row.has_snapshot is False
    assert row.headline == "Not running"
    assert row.rank == 7


def test_unavailable_reason_renders_on_the_stopped_row():
    state = FleetState()
    gateway = FakeGateway([_stopped()])
    gateway.set_inspect("abc123", _inspect(image="bad-tag"))
    _run(state, gateway)
    row = _rows(state)["web-1"]
    assert row.headline.startswith(OFFLINE_UNAVAILABLE_PREFIX)
    assert row.rank == 7


# --- HTTP never starts a read ----------------------------------------------


def _client_with_halted_stop():
    state = FleetState()
    gateway = FakeGateway([_stopped()])
    gateway.set_inspect("abc123", _inspect())
    gateway.set_offline_snapshot(IMAGE, _result("offline-status-halted"))
    _run(state, gateway)
    app = create_app(
        state=state,
        settings=Settings(poll_interval=5.0),
        gateway=gateway,
        start_poller=False,
    )
    return TestClient(app, base_url="http://localhost"), state, gateway


def test_http_handlers_never_start_an_offline_read():
    client, _, gateway = _client_with_halted_stop()
    before_inspect = len(gateway.inspect_calls)
    before_offline = len(gateway.offline_calls)
    page = client.get("/")
    assert page.status_code == 200
    assert len(gateway.inspect_calls) == before_inspect
    assert len(gateway.offline_calls) == before_offline
    drawer = client.get("/instances/web-1")
    assert drawer.status_code == 200
    assert len(gateway.inspect_calls) == before_inspect
    assert len(gateway.offline_calls) == before_offline


def test_stopped_contract_cells_show_intake_halt_and_last_finished():
    client, _, _ = _client_with_halted_stop()
    page = client.get("/")
    assert page.status_code == 200
    # Intake cell: the intake state; Attempt cell: halt plus last finished.
    assert "open" in page.text
    assert "credential-failure" in page.text
    assert "Delivered" in page.text


def test_stopped_contract_drawer_shows_halt_last_finished_and_intake():
    client, _, _ = _client_with_halted_stop()
    drawer = client.get("/instances/web-1")
    assert drawer.status_code == 200
    assert "credential-failure" in drawer.text
    assert "Delivered" in drawer.text
    assert "open" in drawer.text


def test_no_control_console_on_a_stopped_contract_instance():
    client, _, _ = _client_with_halted_stop()
    drawer = client.get("/instances/web-1")
    assert drawer.status_code == 200
    assert "control-console-" not in drawer.text
    fragment = client.get("/instances/web-1/control")
    assert fragment.status_code == 200
    assert "unavailable for contract instances" in fragment.text


def test_outcome_kinds_stay_exported():
    assert collector.POLL_OUTCOME_KINDS >= {GOOD, SNAPSHOT_FAILED, INSTANCE_STARTED}
