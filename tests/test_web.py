import asyncio
import copy
import json
import re
import time
from pathlib import Path

import pytest
from fake_gateway import FakeGateway
from fastapi.testclient import TestClient

from agent_dashboard.app import ALLOWED_HOSTS, create_app, host_allowed
from agent_dashboard.collector import run_cycle
from agent_dashboard.config import Settings
from agent_dashboard.fleet import FleetState
from agent_dashboard.gateway import ManagedInstance

FIXTURES = Path(__file__).parent / "fixtures" / "contract" / "v1"


def load_snapshot(name: str) -> dict:
    return json.loads((FIXTURES / name).read_bytes())


def fresh_snapshot(snap: dict, now: float) -> dict:
    """Stamp a fixture's clocks at `now`: control staleness is wall-clock, so
    the September fixtures would otherwise read as stale against today."""
    snap = copy.deepcopy(snap)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    snap["generated_at"] = stamp
    control = snap.get("control")
    if isinstance(control, dict):
        control["control_observed_at"] = stamp
    return snap


def make_fleet_client(snapshots: dict[str, dict]):
    """A client whose running instances report the given snapshots (one cycle)."""
    now = time.time()
    state = FleetState()
    gateway = FakeGateway(
        [ManagedInstance(name, f"id-{name}", "running") for name in snapshots]
    )
    for name, snap in snapshots.items():
        gateway.set_snapshot(f"id-{name}", fresh_snapshot(snap, now))
    asyncio.run(run_cycle(state, gateway, max_concurrent_execs=4, now=now))
    app = create_app(
        state=state,
        settings=Settings(poll_interval=5.0),
        gateway=gateway,
        start_poller=False,
    )
    return TestClient(app, base_url="http://localhost"), state, gateway


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
    now = time.time() if now is None else now
    state = FleetState()
    gateway = FakeGateway(
        [
            ManagedInstance("worker-up", "id-up", "running"),
            ManagedInstance("worker-old", "id-old", "running"),
            ManagedInstance("worker-down", "id-down", "exited"),
        ]
    )
    gateway.set_snapshot("id-up", {"schema_version": 1})
    asyncio.run(run_cycle(state, gateway, max_concurrent_execs=4, now=now))
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
    # The name also appears in the row's drawer hx-get/hx-push-url, so count
    # the rendered name cells instead of raw occurrences.
    assert body.count('<div class="inst-name">worker-a</div>') == 1
    assert body.count('<div class="inst-name">worker-b</div>') == 1
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


@pytest.mark.parametrize(
    "host", ["raspberrypi", "raspberrypi:8000", "192.168.1.50", "192.168.1.50:8000"]
)
def test_configured_hosts_accepted_with_port(host):
    extra = frozenset({"raspberrypi", "192.168.1.50"})
    assert host_allowed(host, extra) is True


@pytest.mark.parametrize(
    "host", ["evil.example", "evil.example:8000", "other-pi", "10.9.9.9:8000"]
)
def test_unconfigured_hosts_rejected_with_allow_list(host):
    assert host_allowed(host, frozenset({"raspberrypi", "192.168.1.50"})) is False


def test_wildcard_never_matches():
    assert host_allowed("evil.example", frozenset({"*"})) is False
    assert host_allowed("anything", frozenset({"*"})) is False


def test_configured_hosts_pass_middleware():
    from agent_dashboard.config import Settings

    state = FleetState()
    app = create_app(
        state=state,
        settings=Settings(
            poll_interval=5.0, allowed_hosts=frozenset({"raspberrypi", "192.168.1.50"})
        ),
        gateway=FakeGateway(),
        start_poller=False,
    )
    client = TestClient(app, base_url="http://localhost")
    assert client.get("/", headers={"host": "raspberrypi"}).status_code == 200
    assert client.get("/", headers={"host": "192.168.1.50:8000"}).status_code == 200
    assert client.get("/", headers={"host": "evil.example"}).status_code == 403
    assert client.get("/", headers={"host": "evil.example:8000"}).status_code == 403


def test_default_middleware_still_rejects_lan_host():
    client, _ = make_client()
    assert client.get("/", headers={"host": "raspberrypi"}).status_code == 403
    assert client.get("/", headers={"host": "192.168.1.50:8000"}).status_code == 403


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


def test_fleet_table_shows_every_prototype_column():
    client, _, _ = make_fleet_client(
        {"work": load_snapshot("snapshot-attempt-in-progress.json")}
    )
    body = client.get("/").text
    for header in (
        "Instance",
        "Status headline",
        "Reach",
        "Intake",
        "Attempt",
        "Budget",
        "Main context",
        "Turns",
        "Cache hit rate",
        "Errors",
    ):
        assert f"<th>{header}</th>" in body
    assert "example-owner/example-repo" in body
    assert "Working on #977 · model_execution" in body
    assert "running" in body  # intake
    assert "#977 · model_execution" in body  # attempt cell
    assert "640,000 / 4,000,000" in body  # budget used / hard ceiling
    assert "budget-fill" in body and "budget-tick" in body
    assert "hatched" in body  # partly estimated budget
    assert "148,000 (+2)" in body  # main context + running subagents
    assert ">18 <span" in body  # turns, with its basis marker
    assert "86%" in body  # cache hit rate
    assert "0 / 3" in body  # consecutive errors count / max
    assert "unavailable" not in body  # every figure is known here


def test_rows_are_ordered_by_attention_rank():
    now = time.time()
    state = FleetState()
    gateway = FakeGateway(
        [
            ManagedInstance("down", "id-down", "exited"),
            ManagedInstance("unreach", "id-unreach", "running"),
            ManagedInstance("rechold", "id-rechold", "running"),
            ManagedInstance("errs", "id-errs", "running"),
            ManagedInstance("hand", "id-hand", "running"),
            ManagedInstance("work", "id-work", "running"),
            ManagedInstance("idle", "id-idle", "running"),
        ]
    )
    gateway.set_snapshot(
        "id-rechold", fresh_snapshot(load_snapshot("snapshot-recovery-hold.json"), now)
    )
    gateway.set_snapshot(
        "id-errs", fresh_snapshot(load_snapshot("snapshot-truncated.json"), now)
    )
    gateway.set_snapshot(
        "id-hand", fresh_snapshot(load_snapshot("snapshot-soft-threshold-handoff.json"), now)
    )
    gateway.set_snapshot(
        "id-work", fresh_snapshot(load_snapshot("snapshot-attempt-in-progress.json"), now)
    )
    gateway.set_snapshot(
        "id-idle", fresh_snapshot(load_snapshot("snapshot-idle-with-last-finished.json"), now)
    )
    gateway.set_error_code("id-unreach", "agent_unreachable", "no socket")
    asyncio.run(run_cycle(state, gateway, max_concurrent_execs=4, now=now))
    app = create_app(
        state=state,
        settings=Settings(poll_interval=5.0),
        gateway=gateway,
        start_poller=False,
    )
    body = TestClient(app, base_url="http://localhost").get("/").text
    names = ("unreach", "rechold", "errs", "hand", "work", "idle", "down")
    positions = [body.index(name) for name in names]
    assert positions == sorted(positions)
    assert "Recovery hold on #981" in body
    assert "1/3 consecutive infrastructure errors" in body
    assert "Soft threshold crossed, handoff delivering on #981" in body


def test_stale_control_cells_are_marked():
    now = time.time()
    snap = fresh_snapshot(load_snapshot("snapshot-attempt-in-progress.json"), now)
    snap["control"]["control_observed_at"] = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 60.0)
    )
    state = FleetState()
    gateway = FakeGateway([ManagedInstance("work", "id-work", "running")])
    gateway.set_snapshot("id-work", snap)
    asyncio.run(run_cycle(state, gateway, max_concurrent_execs=4, now=now))
    app = create_app(
        state=state,
        settings=Settings(poll_interval=5.0),
        gateway=gateway,
        start_poller=False,
    )
    body = TestClient(app, base_url="http://localhost").get("/").text
    # Intake, attempt and consecutive-error cells carry the stale tag.
    assert body.count("stale-tag") == 3


def test_unsupported_schema_row_uses_stable_fields_only():
    snap = load_snapshot("snapshot-attempt-in-progress.json")
    snap["schema_version"] = 2
    client, _, _ = make_fleet_client({"work": snap})
    body = client.get("/").text
    assert "unsupported schema v2" in body
    assert "unsupported" in body
    assert "example-owner/example-repo" in body
    assert "640,000" not in body  # figures beyond the stable fields stay hidden


def test_no_public_json_api():
    client, _ = make_client()
    paths = {route.path for route in client.app.routes if hasattr(route, "path")}
    assert not any(path.startswith("/api") for path in paths)
    assert {"/", "/fleet", "/stream"} <= paths


def _live_snapshot(now: float) -> dict:
    """Attempt-in-progress fixture with a log-referenceable start time.

    The stock fixture's ``started_at`` has no fractional seconds, so it
    fails ``validate_attempt_ref`` and hides the Live button; stamping a
    fractional start makes the current attempt's log link valid.
    """
    snap = fresh_snapshot(load_snapshot("snapshot-attempt-in-progress.json"), now)
    snap["control"]["active_attempt"]["started_at"] = "2026-09-30T09:30:00.123456Z"
    return snap


def make_live_log_client(now=None):
    """One running instance whose current attempt log reference is valid."""
    now = time.time() if now is None else now
    return make_fleet_client({"work": _live_snapshot(now)})


def test_index_has_lazy_overview_slot_and_reads_no_histories():
    now = time.time()
    state = FleetState()
    gateway = FakeGateway([ManagedInstance("work", "id-work", "running")])
    gateway.set_snapshot(
        "id-work", fresh_snapshot(load_snapshot("snapshot-attempt-in-progress.json"), now)
    )
    asyncio.run(run_cycle(state, gateway, max_concurrent_execs=4, now=now))
    app = create_app(
        state=state,
        settings=Settings(poll_interval=5.0),
        gateway=gateway,
        start_poller=False,
    )
    body = TestClient(app, base_url="http://localhost").get("/").text
    # The panel is an empty slot that fetches /overview once visible.
    assert 'id="overview-slot"' in body
    assert 'hx-get="/overview"' in body
    assert 'hx-trigger="intersect once"' in body
    assert "overview-charts" not in body
    assert gateway.history_calls == []
    assert gateway.logs_tree_calls == []


def test_running_row_has_logs_and_live_buttons():
    client, _, _ = make_live_log_client()
    body = client.get("/").text
    assert 'href="/instances/work/history"' in body
    assert "Live #977" in body
    assert 'href="/instances/work/attempts/977/2026-09-30T09-30-00.123456Z"' in body
    # Neither button opens the drawer: the row's hx-get must not fire.
    assert body.count('onclick="event.stopPropagation()"') >= 2
    # The row itself still opens the drawer.
    assert 'hx-get="/instances/work"' in body


def test_live_button_hidden_when_reference_invalid():
    # The stock fixture's start has no fractional seconds: not a valid
    # attempt reference, so only Logs shows while the row still renders.
    client, _, _ = make_fleet_client(
        {"work": load_snapshot("snapshot-attempt-in-progress.json")}
    )
    body = client.get("/").text
    assert 'href="/instances/work/history"' in body
    assert "Live #" not in body


def test_live_button_hidden_without_active_attempt():
    client, _, _ = make_fleet_client(
        {"idle": load_snapshot("snapshot-idle-with-last-finished.json")}
    )
    body = client.get("/").text
    assert 'href="/instances/idle/history"' in body
    assert "Live #" not in body


def test_not_running_section_holds_stopped_instances():
    client, _ = make_client([("up", "running"), ("down", "not running")])
    body = client.get("/").text
    assert '<details class="not-running" data-persist="not-running">' in body
    assert "Not running (1)" in body
    main, _, details = body.partition('<details class="not-running"')
    assert '<div class="inst-name">up</div>' in main
    assert '<div class="inst-name">down</div>' not in main
    assert '<div class="inst-name">down</div>' in details
    assert "Last finished attempt" in details
    assert 'href="/instances/down/history"' in details
    # Slim rows still open the drawer.
    assert 'hx-get="/instances/down"' in details
    # The section starts collapsed.
    assert " open" not in details.split(">", 1)[0]


def test_no_not_running_section_without_stopped_instances():
    client, _ = make_client([("up", "running")])
    body = client.get("/").text
    assert '<details class="not-running"' not in body
    assert "Not running" not in body


def test_fleet_script_preserves_section_state():
    client, _ = make_client([("up", "running"), ("down", "not running")])
    body = client.get("/").text
    assert 'src="/static/fleet.js"' in body
    script = client.get("/static/fleet.js")
    assert script.status_code == 200
    assert "sessionStorage" in script.text


def test_empty_fleet_message_without_details():
    client, _ = make_client([])
    body = client.get("/").text
    assert "No managed instances found." in body
    assert '<details class="not-running"' not in body
