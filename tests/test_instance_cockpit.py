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
