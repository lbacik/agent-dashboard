import pytest
from fastapi.testclient import TestClient

from agent_dashboard.app import ALLOWED_HOSTS, create_app, host_allowed
from agent_dashboard.config import Settings
from agent_dashboard.fleet import FleetRow, FleetState


class FakeGateway:
    """Stand-in for DockerGateway; web tests must never reach real Docker."""

    def __init__(self, rows=None, error=None):
        self._rows = list(rows or [])
        self._error = error
        self.calls = 0

    async def list_instances(self):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return list(self._rows)


def make_client(rows=None, error=None, **kwargs):
    state = FleetState()
    if rows:
        state._instances = {
            name: FleetRow(name=name, state=state_) for name, state_ in rows
        }
    state.last_error = error
    app = create_app(
        state=state,
        settings=Settings(poll_interval=5.0),
        gateway=FakeGateway(),
        start_poller=False,
        **kwargs,
    )
    return TestClient(app, base_url="http://localhost"), state


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
    import asyncio

    asyncio.run(state.refresh(FakeGateway([])))
    assert "gone" not in client.get("/").text


def test_handlers_never_reach_gateway():
    class ExplodingGateway:
        async def list_instances(self):
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

    from agent_dashboard.gateway import ManagedInstance

    gw = FakeGateway([ManagedInstance("auto", "id", "running")])
    state = FleetState()
    app = create_app(
        state=state, settings=Settings(poll_interval=2.0), gateway=gw, start_poller=True
    )
    with TestClient(app, base_url="http://localhost") as client:
        deadline = time.time() + 5
        while gw.calls == 0 and time.time() < deadline:
            time.sleep(0.05)
        assert gw.calls >= 1
        body = client.get("/").text
    rows, error = __import__("asyncio").run(state.snapshot())
    assert error is None
    assert [r.name for r in rows] == ["auto"]
    assert "auto" in body


def test_no_cors_headers():
    client, _ = make_client()
    response = client.get("/", headers={"host": "localhost", "Origin": "https://x.test"})
    assert "access-control-allow-origin" not in response.headers
