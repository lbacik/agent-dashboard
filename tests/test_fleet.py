import asyncio

from agent_dashboard.fleet import FleetState, poll_forever
from agent_dashboard.gateway import (
    DiscoveryTimeoutError,
    GatewayError,
    ManagedInstance,
)


class FakeGateway:
    def __init__(self, rows=None, error=None):
        self._rows = list(rows or [])
        self._error = error
        self.calls = 0

    async def list_instances(self):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return list(self._rows)


def test_refresh_replaces_rows():
    state = FleetState()
    asyncio.run(
        state.refresh(
            FakeGateway(
                [
                    ManagedInstance("b", "id-b", "running"),
                    ManagedInstance("a", "id-a", "exited"),
                ]
            )
        )
    )
    rows, error = asyncio.run(state.snapshot())
    assert error is None
    assert [(r.name, r.state) for r in rows] == [
        ("a", "not running"),
        ("b", "running"),
    ]


def test_removed_container_disappears_next_cycle():
    state = FleetState()
    gw = FakeGateway([ManagedInstance("gone", "id", "running")])
    asyncio.run(state.refresh(gw))
    gw._rows = []
    asyncio.run(state.refresh(gw))
    rows, _ = asyncio.run(state.snapshot())
    assert rows == []


def test_discovery_failure_records_reason_and_keeps_rows():
    state = FleetState()
    asyncio.run(state.refresh(FakeGateway([ManagedInstance("keep", "id", "running")])))
    asyncio.run(
        state.refresh(FakeGateway(error=DiscoveryTimeoutError("docker ps timed out")))
    )
    rows, error = asyncio.run(state.snapshot())
    assert error == "docker ps timed out"
    assert [r.name for r in rows] == ["keep"]


def test_recovery_clears_error():
    state = FleetState()
    asyncio.run(state.refresh(FakeGateway(error=GatewayError("boom"))))
    _, error = asyncio.run(state.snapshot())
    assert error == "boom"
    asyncio.run(state.refresh(FakeGateway([])))
    _, error = asyncio.run(state.snapshot())
    assert error is None


def test_poll_forever_runs_every_interval():
    async def scenario():
        state = FleetState()
        gw = FakeGateway([ManagedInstance("a", "id", "running")])
        stop = asyncio.Event()
        task = asyncio.create_task(poll_forever(state, gw, 0.05, stop))
        await asyncio.sleep(0.16)
        stop.set()
        await task
        return gw.calls

    calls = asyncio.run(scenario())
    assert calls >= 2
