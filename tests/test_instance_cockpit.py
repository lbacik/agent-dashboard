"""Instance Cockpit: the fleet rail beside the inline drawer.

Selecting a managed instance switches the workspace from the Fleet Matrix
to the Instance Cockpit: the matrix hides, the Fleet units rail shows on
the left and the instance details render inline beside it. Leaving takes
an explicit action (back/close button, Escape, or Back/Forward).
"""

import re

from test_web import load_snapshot


def make_cockpit_client():
    """One running plus one stopped instance with telemetry for the runner."""
    import asyncio
    import time

    from fake_gateway import FakeGateway
    from fastapi.testclient import TestClient

    from agent_dashboard.app import create_app
    from agent_dashboard.collector import run_cycle
    from agent_dashboard.config import Settings
    from agent_dashboard.fleet import FleetState
    from agent_dashboard.gateway import ManagedInstance

    now = time.time()
    state = FleetState()
    gateway = FakeGateway(
        [
            ManagedInstance("work", "id-work", "running"),
            ManagedInstance("down", "id-down", "exited"),
        ]
    )
    from test_web import fresh_snapshot

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
    return TestClient(app, base_url="http://localhost"), state, gateway


def rail_items(body):
    return re.findall(r'class="vc-rail-item[^"]*"', body)


def test_fleet_renders_rail_and_matrix_pane():
    client, _, _ = make_cockpit_client()
    for path in ("/", "/partials/fleet"):
        body = client.get(path).text
        assert 'class="vc-rail"' in body
        assert 'class="vc-matrix-pane"' in body
        assert '<table class="fleet">' in body


def test_fleet_partial_alias_matches_fleet():
    client, _, _ = make_cockpit_client()
    assert client.get("/fleet").status_code == 200
    assert client.get("/partials/fleet").status_code == 200
    assert "vc-rail" in client.get("/fleet").text
    assert "vc-rail" in client.get("/partials/fleet").text


def test_rail_lists_running_and_stopped_with_stripes():
    client, _, _ = make_cockpit_client()
    for path in ("/", "/partials/fleet"):
        body = client.get(path).text
        # Both units appear in the rail with their matrix rank stripes.
        assert '<span class="vc-rail-name">work</span>' in body
        assert '<span class="vc-rail-name">down</span>' in body
        items = rail_items(body)
        assert len(items) == 2
        assert any("stripe-" in item for item in items)
        assert "stripe-offline" in body
        # Running item shows rank tag, repository, attempt, Reach pill, headline.
        assert "vc-rail-rank" in body
        assert "vc-rail-repo" in body
        assert "vc-rail-attempt" in body
        assert "pill-" in body
        assert "vc-rail-headline" in body
        # Stopped item is marked offline.
        assert "offline" in body.lower()
        # Every rail item switches the cockpit like a matrix row.
        assert body.count('hx-target="#drawer-holder"') >= 2
        assert body.count('hx-push-url="/?instance=') >= 2
        assert 'data-instance="work"' in body
        assert 'data-instance="down"' in body


def test_fleet_scroll_keys_are_rail_and_running():
    import re as _re

    client, _, _ = make_cockpit_client()
    for path in ("/", "/partials/fleet"):
        body = client.get(path).text
        keys = sorted(_re.findall(r'data-scroll-key="([^"]+)"', body))
        assert keys == ["fleet-rail", "fleet-running"]


def test_index_opens_cockpit_with_selected_rail_item():
    client, _, _ = make_cockpit_client()
    body = client.get("/?instance=work").text
    # The drawer renders inside the holder.
    holder = body.partition('id="drawer-holder"')[2]
    assert 'id="drawer"' in holder
    assert "640,000" in holder
    # The rail marks the inspected instance as selected.
    work_tag = None
    for tag in re.findall(r'<button[^>]*class="vc-rail-item[^"]*"[^>]*>', body):
        if 'hx-get="/instances/work"' in tag:
            work_tag = tag
    assert work_tag is not None
    assert "is-selected" in work_tag
    assert 'aria-current="true"' in work_tag or "aria-current" in body
    # The stopped unit is not selected.
    for tag in re.findall(r'<button[^>]*class="vc-rail-item[^"]*"[^>]*>', body):
        if 'hx-get="/instances/down"' in tag:
            assert "is-selected" not in tag


def test_unknown_instance_renders_gone_drawer():
    client, _, _ = make_cockpit_client()
    body = client.get("/?instance=nope").text
    assert '<aside id="drawer" class="drawer" data-scroll-key="drawer"' in body
    assert "gone" in body.lower()


def test_drawer_has_explicit_dismissal_controls():
    client, _, _ = make_cockpit_client()
    body = client.get("/instances/work").text
    assert "← Fleet Matrix" in body
    assert body.count("closeDrawer()") >= 2
    assert "drawer-back" in body
    assert "drawer-close" in body


def test_rail_header_has_matrix_button():
    client, _, _ = make_cockpit_client()
    for path in ("/", "/partials/fleet"):
        body = client.get(path).text
        assert "⊞ Matrix" in body
        assert "vc-rail-matrix" in body
        # The header button closes the cockpit.
        head = body.partition("vc-rail-head")[2].partition("</div>")[0]
        assert "closeDrawer()" in head


def test_drawer_static_asset_rules():
    client, _, _ = make_cockpit_client()
    text = client.get("/static/drawer.js").text
    assert "function closeDrawer()" in text
    assert "Escape" in text
    assert "popstate" in text
    assert 'document.addEventListener("click"' not in text


def test_cockpit_style_rules():
    client, _, _ = make_cockpit_client()
    css = client.get("/static/style.css").text
    assert "#live:has(#drawer)" in css
    assert "#live:not(:has(#drawer))" in css
    assert "grid-template-columns: 280px minmax(0, 1fr)" in css
    drawer_block = css.partition(".drawer {")[2].partition("}")[0]
    assert "fixed" not in drawer_block
    # Rail show/hide rules keyed on the drawer.
    assert "#live:not(:has(#drawer)) .vc-rail" in css
    assert "#live:has(#drawer) .vc-matrix-pane" in css
    # No literal colours in cockpit rules.
    import re as _re

    cockpit_rules = "\n".join(
        line
        for line in css.splitlines()
        if (".vc-rail" in line or ".vc-matrix" in line or "stripe-" in line)
        and "--vc-stripe-" not in line
    )
    assert cockpit_rules
    assert _re.search(r"#[0-9a-fA-F]{3,8}\b", cockpit_rules) is None
    assert "rgb(" not in cockpit_rules


def make_cockpit_client_for(fixture_name):
    """A cockpit client with one running instance on the given contract snapshot."""
    from test_web import load_snapshot

    return make_cockpit_client_for_snapshot(load_snapshot(fixture_name))


def cockpit_columns(body):
    """The three cockpit column bodies in document order."""
    import re as _re

    parts = _re.split(r'data-col="(control|telemetry|history)"', body)
    cols = {}
    for marker, chunk in zip(parts[1::2], parts[2::2]):
        cols[marker] = chunk
    assert list(cols) == ["control", "telemetry", "history"]
    return cols


def test_cockpit_drawer_root_carries_instance():
    client, _, _ = make_cockpit_client()
    body = client.get("/instances/work").text
    assert (
        '<aside id="drawer" class="drawer" data-scroll-key="drawer" data-instance="work"'
        in body
    )
    assert body.count('id="drawer"') == 1
    assert body.count('class="drawer-inner"') == 1


def make_live_cockpit_client():
    """A cockpit client whose active attempt is a valid Attempt log reference.

    The stock fixture's start has no fractional seconds, so it renders no
    Live button; stamping fractional seconds makes the Activity container
    and the header Live link appear.
    """
    import copy

    from test_web import load_snapshot

    snap = copy.deepcopy(load_snapshot("snapshot-attempt-in-progress.json"))
    snap["control"]["active_attempt"]["started_at"] = "2026-09-30T09:30:00.123456Z"
    return make_cockpit_client_for_snapshot(snap)


def test_cockpit_header_bar():
    client = make_live_cockpit_client()
    body = client.get("/instances/work").text
    head = body.partition("drawer-head")[2].partition("vc-cockpit-grid")[0]
    # Instance name, repository, Reach pill and status headline.
    assert '<h2 class="drawer-title">work</h2>' in head
    assert "example-owner/example-repo" in head
    assert "pill-" in head
    # Logs link to Attempt history and the Live link to the current log.
    assert 'href="/instances/work/history"' in head
    assert ">Logs<" in head
    assert "Live #977" in head
    assert "/instances/work/attempts/977/" in head
    # Explicit dismissal controls.
    assert "← Fleet Matrix" in head
    assert "drawer-close" in head


def test_cockpit_grid_has_three_columns_in_order():
    client, _, _ = make_cockpit_client()
    body = client.get("/instances/work").text
    assert 'class="vc-cockpit-grid"' in body
    grid = body.partition('class="vc-cockpit-grid"')[2]
    assert grid.index('data-col="control"') < grid.index('data-col="telemetry"')
    assert grid.index('data-col="telemetry"') < grid.index('data-col="history"')


def test_cockpit_control_column_contents():
    from agent_dashboard.control import control_slug

    client, _, _ = make_cockpit_client()
    body = client.get("/instances/work").text
    cols = cockpit_columns(body)
    control = cols["control"]
    # The Control console slot keeps its stable ID, hx-preserve and the
    # filtered sse:fleet trigger with out-of-band feedback.
    slug = control_slug("work")
    assert f'id="control-console-{slug}"' in control
    assert "hx-preserve" in control
    assert "sse:fleet" in control
    assert f'id="ctl-feedback-{slug}"' in control
    assert f'id="ctl-body-{slug}"' in control
    assert "/instances/work/control" in control
    # Identity & Intake facts.
    assert "example-owner/example-repo" in control
    assert "0.4.5" in control
    assert "running" in control
    assert "none" in control  # stop plan / handoff idle text
    # Active attempt (not a recovery hold here).
    assert "Active attempt" in control
    assert "agent/issue-977" in control
    assert "model_execution" in control
    assert "2026-09-30T09:30:00Z" in control
    # Last finished attempt.
    assert "Last finished attempt" in control
    assert "#961" in control
    assert "complete" in control
    assert "812,345" in control
    assert "#962" in control
    # The command log lives in the console fragment the slot loads.
    assert "/instances/work/control" in control
    commands = client.get("/instances/work/control").text
    assert "Commands (last 10 from telemetry)" in commands


def test_cockpit_recovery_hold_replaces_active_attempt():
    client = make_cockpit_client_for("snapshot-recovery-hold.json")
    body = client.get("/instances/work").text
    cols = cockpit_columns(body)
    control = cols["control"]
    assert "Recovery hold" in control
    assert "result comment confirmation is not known yet" in control
    assert "agentctl recovery retry" in control
    assert "<code" in control
    assert 'id="recovery-next-action"' in control
    assert "Active attempt" not in control


def test_cockpit_telemetry_column_contents():
    client = make_live_cockpit_client()
    body = client.get("/instances/work").text
    cols = cockpit_columns(body)
    telemetry = cols["telemetry"]
    # The Activity container keeps its ID, hx-preserve and scroll keys.
    assert 'id="activity-work"' in telemetry
    assert "hx-preserve" in telemetry
    assert 'data-activity-group="activity"' in telemetry
    assert "activity-sum" in telemetry
    assert 'data-scroll-key="activity-body"' in telemetry
    assert 'hx-get="/instances/work/activity"' in telemetry
    # Attempt telemetry: budget bar and figures, context, turns, cache,
    # errors, soft threshold and handoff status; nothing dropped.
    assert "budget-bar" in telemetry
    assert "640,000" in telemetry
    assert "512,000" in telemetry
    assert "128,000" in telemetry
    assert "4,000,000" in telemetry
    assert "3,200,000" in telemetry
    assert "percentage" in telemetry
    assert "148,000" in telemetry
    assert "200,000" in telemetry
    assert "151,200" in telemetry
    assert "86%" in telemetry
    assert "0 / 3" in telemetry  # consecutive errors
    assert "Consecutive errors" in telemetry
    assert "Handoff status" in telemetry
    assert "120,000" in telemetry  # tokens by category
    assert "350,000" in telemetry
    assert "42,000" in telemetry
    assert "sub-a1" in telemetry  # subagent contexts
    assert "sub-a2" in telemetry
    assert "not exposed through the gateway" in telemetry  # rate-limit reason


def test_cockpit_history_column_contents():
    client, _, _ = make_cockpit_client()
    body = client.get("/instances/work").text
    cols = cockpit_columns(body)
    history = cols["history"]
    # The stats slot keeps its ID, hx-preserve and load-once trigger.
    assert 'id="stats-work"' in history
    assert "hx-preserve" in history
    assert "/instances/work/stats" in history
    assert 'hx-trigger="load"' in history
    # Recent events with their timestamps.
    assert "Recent events" in history
    assert "attempt_claimed" in history
    assert "2026-09-30T09:30:00Z" in history


def test_cockpit_idle_drawer_keeps_facts_without_live_links():
    client = make_cockpit_client_for("snapshot-idle-with-last-finished.json")
    body = client.get("/instances/work").text
    cols = cockpit_columns(body)
    assert "No current attempt (n/a)" in cols["telemetry"]
    # Consecutive errors and the last finished attempt stay visible.
    assert "0 / 3" in cols["telemetry"]
    assert "#961" in cols["control"]
    # History link stays; the Live link needs a current Attempt log.
    assert 'href="/instances/work/history"' in body
    assert "Live #" not in body


def make_cockpit_client_for_snapshot(snap):
    """A cockpit client with one running instance on an explicit snapshot dict."""
    import asyncio
    import time

    from fake_gateway import FakeGateway
    from fastapi.testclient import TestClient
    from test_web import fresh_snapshot

    from agent_dashboard.app import create_app
    from agent_dashboard.collector import run_cycle
    from agent_dashboard.config import Settings
    from agent_dashboard.fleet import FleetState
    from agent_dashboard.gateway import ManagedInstance

    now = time.time()
    state = FleetState()
    gateway = FakeGateway([ManagedInstance("work", "id-work", "running")])
    gateway.set_snapshot("id-work", fresh_snapshot(snap, now))
    asyncio.run(run_cycle(state, gateway, max_concurrent_execs=4, now=now))
    app = create_app(
        state=state,
        settings=Settings(poll_interval=5.0),
        gateway=gateway,
        start_poller=False,
    )
    return TestClient(app, base_url="http://localhost")


def test_cockpit_reduced_fragments_keep_drawer_root():
    import copy

    from test_web import load_snapshot

    client = make_cockpit_client_for("snapshot-attempt-in-progress.json")
    snap = copy.deepcopy(load_snapshot("snapshot-attempt-in-progress.json"))
    snap["schema_version"] = 2
    unsupported = make_cockpit_client_for_snapshot(snap).get("/instances/work").text
    assert (
        '<aside id="drawer" class="drawer" data-scroll-key="drawer" data-instance="work"'
        in unsupported
    )
    assert "unsupported schema v2" in unsupported
    assert "closeDrawer()" in unsupported
    gone = client.get("/instances/nope").text
    assert (
        '<aside id="drawer" class="drawer" data-scroll-key="drawer" data-instance="nope"'
        in gone
    )
    assert "gone" in gone.lower()
    assert gone.count("closeDrawer()") >= 2


def test_cockpit_grid_style_rules():
    client, _, _ = make_cockpit_client()
    css = client.get("/static/style.css").text
    assert "repeat(3, minmax(0, 1fr))" in css
    assert "1360px" in css
    assert "960px" in css
    assert "repeat(auto-fit, minmax(22rem, 1fr))" in css
    assert "vc-cockpit-grid" in css
    # Columns shrink instead of pushing the page sideways.
    assert "minmax(0, 1fr)" in css
    assert ".vc-cockpit-col { min-width: 0;" in css
    # No literal colours in cockpit rules.
    import re as _re

    cockpit_rules = "\n".join(
        line
        for line in css.splitlines()
        if "vc-cockpit" in line or "cockpit" in line
    )
    assert cockpit_rules
    assert _re.search(r"#[0-9a-fA-F]{3,8}\b", cockpit_rules) is None
    assert "rgb(" not in cockpit_rules
