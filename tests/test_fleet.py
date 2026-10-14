"""Fleet state transitions and the background poll loop (via run_cycle)."""

import asyncio
import time

from fake_gateway import FakeGateway

from agent_dashboard.collector import GOOD, InstancePoll, PollOutcome, run_cycle
from agent_dashboard.fleet import FleetState, poll_forever
from agent_dashboard.gateway import (
    DiscoveryTimeoutError,
    GatewayError,
    ManagedInstance,
)


def good_poll(now: float = 1000.0) -> InstancePoll:
    return InstancePoll(PollOutcome(GOOD, "", now), {"schema_version": 1})


def run(state: FleetState, gateway: FakeGateway, **kwargs) -> None:
    kwargs.setdefault("now", 1000.0)
    asyncio.run(run_cycle(state, gateway, **kwargs))


def test_refresh_replaces_rows():
    state = FleetState()
    gateway = FakeGateway(
        [
            ManagedInstance("b", "id-b", "running"),
            ManagedInstance("a", "id-a", "exited"),
        ]
    )
    run(state, gateway)
    rows, error = asyncio.run(state.snapshot())
    assert error is None
    assert [(r.name, r.state) for r in rows] == [
        ("a", "not running"),
        ("b", "running"),
    ]


def test_removed_container_disappears_next_cycle():
    state = FleetState()
    gateway = FakeGateway([ManagedInstance("gone", "id", "running")])
    run(state, gateway)
    gateway.set_rows([])
    run(state, gateway)
    rows, _ = asyncio.run(state.snapshot())
    assert rows == []


def test_discovery_failure_records_reason_and_keeps_rows():
    state = FleetState()
    gateway = FakeGateway([ManagedInstance("keep", "id", "running")])
    gateway.set_snapshot("id", {"schema_version": 1})
    run(state, gateway)
    gateway._error = DiscoveryTimeoutError("docker ps timed out")
    run(state, gateway)
    rows, error = asyncio.run(state.snapshot())
    assert error == "docker ps timed out"
    assert [r.name for r in rows] == ["keep"]


def test_recovery_clears_error():
    state = FleetState()
    gateway = FakeGateway(error=GatewayError("boom"))
    run(state, gateway)
    _, error = asyncio.run(state.snapshot())
    assert error == "boom"
    gateway._error = None
    run(state, gateway)
    _, error = asyncio.run(state.snapshot())
    assert error is None


def test_recreated_container_drops_snapshot_but_keeps_row():
    state = FleetState()
    gateway = FakeGateway([ManagedInstance("w", "old-id", "running")])
    gateway.set_snapshot("old-id", {"schema_version": 1})
    run(state, gateway, now=1000.0)
    rows, _ = asyncio.run(state.snapshot(now=1005.0))
    assert rows[0].has_snapshot is True
    gateway.set_rows([ManagedInstance("w", "new-id", "running")])
    gateway.set_snapshot("new-id", {"schema_version": 1})
    run(state, gateway, now=1010.0)
    rows, _ = asyncio.run(state.snapshot(now=1015.0))
    assert rows[0].container_id == "new-id"
    assert rows[0].snapshot_age == 5.0
    assert rows[0].has_snapshot is True


def test_apply_cycle_marks_not_running_without_exec():
    state = FleetState()
    asyncio.run(
        state.apply_cycle([ManagedInstance("down", "id", "exited")], {}, now=1000.0)
    )
    rows, _ = asyncio.run(state.snapshot())
    assert [(r.name, r.reach) for r in rows] == [("down", "not running")]


def test_poll_forever_runs_every_interval():
    async def scenario():
        state = FleetState()
        gateway = FakeGateway([ManagedInstance("a", "id", "running")])
        stop = asyncio.Event()
        task = asyncio.create_task(poll_forever(state, gateway, 0.05, stop))
        await asyncio.sleep(0.16)
        stop.set()
        await task
        return gateway.list_calls

    calls = asyncio.run(scenario())
    assert calls >= 2


def test_poll_forever_cycles_never_overlap():
    """Slow cycle + short interval: next cycle starts after the last finished."""
    starts: list[float] = []

    class SlowGateway(FakeGateway):
        async def list_instances(self):
            starts.append(time.monotonic())
            await asyncio.sleep(0.12)
            return list(self._rows)

    async def scenario():
        state = FleetState()
        gateway = SlowGateway([ManagedInstance("a", "id", "running")])
        stop = asyncio.Event()
        task = asyncio.create_task(poll_forever(state, gateway, 0.05, stop))
        await asyncio.sleep(0.4)
        stop.set()
        await task

    asyncio.run(scenario())
    assert len(starts) >= 2
    for first, second in zip(starts, starts[1:]):
        assert second - first >= 0.12


def test_poll_forever_uses_start_based_scheduling():
    """Fast cycle + long interval: gaps match the interval, not the runtime."""
    starts: list[float] = []

    class TimingGateway(FakeGateway):
        async def list_instances(self):
            starts.append(time.monotonic())
            return list(self._rows)

    async def scenario():
        state = FleetState()
        gateway = TimingGateway([ManagedInstance("a", "id", "running")])
        stop = asyncio.Event()
        task = asyncio.create_task(poll_forever(state, gateway, 0.1, stop))
        await asyncio.sleep(0.35)
        stop.set()
        await task

    asyncio.run(scenario())
    assert len(starts) >= 3
    for first, second in zip(starts, starts[1:]):
        assert 0.09 <= second - first <= 0.2
