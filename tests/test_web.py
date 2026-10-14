import asyncio

import pytest
from fake_gateway import FakeGateway
from fastapi.testclient import TestClient

from agent_dashboard.app import ALLOWED_HOSTS, create_app, host_allowed
from agent_dashboard.collector import run_cycle
from agent_dashboard.config import Settings
from agent_dashboard.fleet import FleetState
from agent_dashboard.gateway import ManagedInstance


def make_client(rows=None, error=None, **kwargs):
    """Build a client over a state populated by discovery (no telemetry yet)."""
    state = FleetState()
    instances = []
    for row in rows or []:
        if isinstance(row, ManagedInstance):
            instances.append(row)
        else:
            name, container_state = row
            raw = "running" if container_state == "running" else "exited"
            instances.append(ManagedInstance(name, f"id-{name}", raw))
    asyncio.run(state.apply_cycle(instances, {}, now=1000.0))
    if error is not None:
        asyncio.run(state.set_discovery_error(error))
    app = create_app(
        state=state,
        settings=Settings(poll_interval=5.0),
        gateway=FakeGateway(),
        start_poller=False,
        **kwargs,
    )
    return TestClient(app, base_url="http://localhost"), state


def make_live_client(now=None):
    """A client whose fleet ran one good telemetry cycle at `now`.

    `id-up` reports a good snapshot, `id-old` keeps the fake's default
    (an old agent without telemetry), `id-down` is exited.
    """
    import time as _time

    now = _time.time() if now is None else now
    state = FleetState()
    gateway = FakeGateway(
        [
            ManagedInstance("worker-up", "id-up", "running"),
            ManagedInstance("worker-old", "id-old", "running"),
            ManagedInstance("worker-down", "id-down", "exited"),
        ]
    )
    gateway.set_snapshot("id-up", {"schema_version": 1})
    asyncio.run(
        run_cycle(state, gateway, exec_timeout=5.0, max_concurrent_execs=4, now=now)
    )
    app = create_app(
        state=state,
        settings=Settings(poll_interval=5.0),
        gateway=gateway,
        start_poller=False,
    )
    return TestClient(app, base_url="http://localhost"), state, gateway, now


def test_index_has_one_row_per_instance():
    client, _ = make_client([("worker-b", "running"), ("worker-a", "not running")])
    response = client.get("/")
    assert response.status_code == 200
    body = response.text
    assert body.count("worker-a") == 1
    assert body.count("worker-b") == 1
    assert "running" in body
    assert "not running" in body


def test_fleet_partial_matches_index_rows():
    client, _ = make_client([("worker-a", "running")])
    partial = client.get("/fleet")
    assert partial.status_code == 200
    assert "worker-a" in partial.text
    assert "<table" in partial.text


def test_table_has_reach_and_headline_columns():
    client, _, _, _ = make_live_client()
    body = client.get("/").text
    assert "<th>Reach</th>" in body
    assert "<th>Status headline</th>" in body
    assert "live" in body
    assert "Live: reporting telemetry" in body
    assert "not running" in body
    assert "Not running" in body
    # Old agent without telemetry: unsupported with the spec sentence.
    assert "unsupported" in body
    assert "telemetry not supported by this agent version" in body


def test_failed_poll_keeps_snapshot_age_visible():
    # Cycles run on the wall clock so the handlers (which render age with
    # the wall clock) show a fresh age. Exact ages are pinned through
    # FleetState.snapshot(now=...) in the collector tests.
    import re
    import time

    client, state, gateway, _ = make_live_client()
    gateway.set_error_code("id-up", "agent_unreachable", "no socket")
    asyncio.run(run_cycle(state, gateway, now=time.time()))
    aged = client.get("/").text
    assert "unreachable" in aged
    assert "no socket" in aged
    match = re.search(r"(\d+) s ago", aged)
    assert match is not None
    assert int(match.group(1)) <= 5
    assert client.get("/fleet").status_code == 200


def test_discovery_failure_banner_with_reason():
    client, _ = make_client([], error="docker CLI not found: install Docker")
    for path in ("/", "/fleet"):
        response = client.get(path)
        assert response.status_code == 200
        assert "Discovery failed" in response.text
        assert "docker CLI not found" in response.text


def test_removed_container_disappears():
    client, state = make_client([("gone", "running")])
    assert "gone" in client.get("/").text
    asyncio.run(run_cycle(state, FakeGateway([]), now=1010.0))
    assert "gone" not in client.get("/").text


def test_handlers_never_reach_gateway():
    class ExplodingGateway:
        async def list_instances(self):
            raise AssertionError("request path reached the gateway")

        async def telemetry(self, container_id):
            raise AssertionError("request path reached the gateway")

    state = FleetState()
    app = create_app(
        state=state, settings=Settings(), gateway=ExplodingGateway(), start_poller=False
    )
    client = TestClient(app, base_url="http://localhost")
    assert client.get("/").status_code == 200
    assert client.get("/fleet").status_code == 200


def test_htmx_vendored_no_cdn():
    client, _ = make_client()
    body = client.get("/").text
    assert "/static/htmx.min.js" in body
    assert "cdn" not in body.lower()
    static = client.get("/static/htmx.min.js")
    assert static.status_code == 200
    assert len(static.content) > 1000


def test_templates_are_server_rendered():
    client, _ = make_client([("worker-a", "running")])
    body = client.get("/").text
    assert "<table" in body
    assert "worker-a" in body  # present without running client JS


@pytest.mark.parametrize(
    "host", ["localhost", "localhost:8000", "127.0.0.1", "127.0.0.1:8000"]
)
def test_allowed_hosts(host):
    assert host_allowed(host) is True
    assert ALLOWED_HOSTS == {"localhost", "127.0.0.1"}
    client, _ = make_client()
    assert client.get("/", headers={"host": host}).status_code == 200


@pytest.mark.parametrize(
    "host",
    ["evil.example", "evil.example:8000", "testserver", "0.0.0.0:8000", "[::1]:8000"],
)
def test_rejected_hosts(host):
    assert host_allowed(host) is False
    client, _ = make_client()
    assert client.get("/", headers={"host": host}).status_code == 403


def test_background_poller_runs_discovery():
    import time

    gateway = FakeGateway([ManagedInstance("auto", "id", "running")])
    gateway.set_snapshot("id", {"schema_version": 1})
    state = FleetState()
    app = create_app(
        state=state, settings=Settings(poll_interval=2.0), gateway=gateway, start_poller=True
    )
    with TestClient(app, base_url="http://localhost") as client:
        deadline = time.time() + 5
        while gateway.list_calls == 0 and time.time() < deadline:
            time.sleep(0.05)
        assert gateway.list_calls >= 1
        body = client.get("/").text
    rows, error = asyncio.run(state.snapshot())
    assert error is None
    assert [r.name for r in rows] == ["auto"]
    assert [r.reach for r in rows] == ["live"]
    assert "auto" in body


def test_no_cors_headers():
    client, _ = make_client()
    response = client.get("/", headers={"host": "localhost", "Origin": "https://x.test"})
    assert "access-control-allow-origin" not in response.headers
