"""The Fleet Matrix: one table for running and stopped instances plus a KPI strip.

Covers the matrix acceptance criteria: four filter buttons with
server-rendered counts and default pressed states on `/` and `/fleet`;
stopped rows inside the single `table.fleet` (hidden, `data-vc-offline`);
the stopped-only and empty-fleet messages; per-row rank-tier stripes; the
single `fleet-running` scroll key; and the filter script rules from the
static asset (re-applied after `htmx:afterSwap`, no browser storage).
"""

import asyncio
import re
import time

from fake_gateway import FakeGateway
from fastapi.testclient import TestClient
from test_web import fresh_snapshot, load_snapshot, make_client

from agent_dashboard.app import create_app
from agent_dashboard.collector import run_cycle
from agent_dashboard.config import Settings
from agent_dashboard.fleet import FleetState
from agent_dashboard.gateway import ManagedInstance


def make_matrix_client(now=None):
    """A fixture fleet mixing alert/warning, active, idle and stopped rows.

    unreach (rank 0, unreachable), rechold (rank 1), errs (rank 2,
    consecutive errors), hand (rank 3, handoff in progress), work (rank 5,
    active attempt), idle (rank 6) and down (stopped, rank 7). So online=6,
    alerts=4 (ranks 0-4: hand's soft-crossed handoff counts once), working=3
    (errs, hand and work carry an active attempt) and offline=1.
    """
    now = time.time() if now is None else now
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
        "id-hand",
        fresh_snapshot(load_snapshot("snapshot-soft-threshold-handoff.json"), now),
    )
    gateway.set_snapshot(
        "id-work",
        fresh_snapshot(load_snapshot("snapshot-attempt-in-progress.json"), now),
    )
    gateway.set_snapshot(
        "id-idle",
        fresh_snapshot(load_snapshot("snapshot-idle-with-last-finished.json"), now),
    )
    gateway.set_error_code("id-unreach", "agent_unreachable", "no socket")
    asyncio.run(run_cycle(state, gateway, max_concurrent_execs=4, now=now))
    app = create_app(
        state=state,
        settings=Settings(poll_interval=5.0),
        gateway=gateway,
        start_poller=False,
    )
    return TestClient(app, base_url="http://localhost"), state, gateway


def matrix_pane(body):
    """The matrix pane alone, past the rail and its scaled-down filters."""
    return body.partition('class="vc-matrix-pane"')[2]


def tile_count(body, key):
    """The server-rendered count inside one KPI tile."""
    match = re.search(
        r'data-vc-filter="' + key + r'".*?<span class="vc-count">(\d+)</span>',
        matrix_pane(body),
        re.S,
    )
    assert match is not None, f"no vc-count for {key}"
    return int(match.group(1))


def test_kpi_tiles_render_counts_and_defaults():
    client, _, _ = make_matrix_client()
    for path in ("/", "/fleet"):
        body = client.get(path).text
        for key in ("online", "alerts", "working", "offline"):
            assert f'data-vc-filter="{key}"' in body
        assert '<button type="button"' in body
        assert 'data-vc-filter="online" aria-pressed="true"' in body
        for key in ("alerts", "working", "offline"):
            assert f'data-vc-filter="{key}" aria-pressed="false"' in body
        assert tile_count(body, "online") == 6
        assert tile_count(body, "alerts") == 4
        assert tile_count(body, "working") == 3
        assert tile_count(body, "offline") == 1
        # Each tile shows its filter state as an ON/OFF badge plus an LED.
        assert matrix_pane(body).count('<span class="vc-led"') == 4
        assert body.count('<span class="vc-state">ON</span>') == 1
        assert body.count('<span class="vc-state">OFF</span>') == 3


def test_stopped_rows_live_in_the_single_table():
    client, _, _ = make_matrix_client()
    for path in ("/", "/fleet"):
        body = client.get(path).text
        assert body.count('<table class="fleet">') == 1
        assert '<details class="not-running"' not in body
        assert "data-scroll-key=\"fleet-not-running\"" not in body
        match = re.search(
            r'<tr class="fleet-row stripe-offline"[^>]*>', body, re.S
        )
        assert match is not None
        tag = match.group(0)
        assert 'data-vc-offline="1"' in tag
        assert "hidden" in tag
        # The stopped row keeps the drawer link and the Logs button.
        assert 'hx-get="/instances/down"' in body
        assert 'href="/instances/down/history"' in body


def test_stopped_only_matrix_keeps_hidden_rows_and_message():
    client, _ = make_client([("down", "not running")])
    for path in ("/", "/fleet"):
        body = client.get(path).text
        assert "No running managed instances." in body
        assert "No managed instances found." not in body
        assert body.count('<table class="fleet">') == 1
        match = re.search(
            r'<tr class="fleet-row stripe-offline"[^>]*>', body, re.S
        )
        assert match is not None
        assert "hidden" in match.group(0)
        assert '<div class="inst-name">down</div>' in body


def test_empty_matrix_message():
    client, _ = make_client([])
    for path in ("/", "/fleet"):
        body = client.get(path).text
        assert "No managed instances found." in body
        assert '<details class="not-running"' not in body


def test_each_row_carries_its_rank_tier_stripe():
    client, _, _ = make_matrix_client()
    body = client.get("/").text
    # Alert tier (ranks 0-2): unreachable, recovery hold, consecutive errors.
    # Warning tier (ranks 3-4): handoff in progress. Active work (rank 5 or an
    # active attempt): work. Idle (rank 6). Offline (stopped).
    for stripe in (
        "stripe-alert",
        "stripe-warning",
        "stripe-working",
        "stripe-idle",
        "stripe-offline",
    ):
        assert stripe in body
    # Stripes add to the Reach pill and the headline, never replace them.
    assert "<th>Reach</th>" in body
    assert "<th>Status headline</th>" in body
    assert "pill-unreachable" in body
    assert "Recovery hold on #981" in body


def test_scroll_keeper_keys_are_just_fleet_running():
    client, _, _ = make_matrix_client()
    boxes = re.compile(r'<div[^>]*class="table-scroll"[^>]*>')
    for path in ("/", "/fleet"):
        found = boxes.findall(client.get(path).text)
        assert len(found) == 1
        assert 'data-scroll-key="fleet-running"' in found[0]


def test_fleet_script_rules_and_summary_controls():
    client, _ = make_client([("up", "running"), ("down", "not running")])
    for path in ("/", "/fleet"):
        body = client.get(path).text
        # The matrix header bar summarises the active filters and shown count.
        assert "data-vc-summary" in body
        assert "ONLINE (1 SHOWN)" in body
        # All-off shows the empty-filter message with its reset control.
        assert "data-vc-empty" in body
        assert "data-vc-reset" in body
        assert "Reset to Online units" in body
    script = client.get("/static/fleet.js").text
    assert "var vcFilters = { online: true, alerts: false," in script
    assert "working: false, offline: false };" in script
    assert "function toggleVcFilter(key)" in script
    assert "vcFilters.online = false;" in script
    assert "vcFilters.alerts = false;" in script
    assert "vcFilters.working = false;" in script
    assert "htmx:afterSwap" in script
    assert "localStorage" not in script
    assert "sessionStorage" not in script


def test_matrix_colours_are_root_tokens():
    client, _ = make_client([("up", "running"), ("down", "not running")])
    assert client.get("/").status_code == 200
    css = client.get("/static/style.css").text
    assert ":root" in css
    for token in (
        "--vc-stripe-alert",
        "--vc-stripe-warning",
        "--vc-stripe-working",
        "--vc-stripe-idle",
        "--vc-stripe-offline",
    ):
        assert token in css
    # No literal colours in the matrix/rail/cockpit rules: every colour is
    # a var(). ID selectors (`#live`, `#drawer`) contain `#` without being
    # colours, so only hex colour literals and rgb() count.
    import re as _re

    matrix_rules = "\n".join(
        line
        for line in css.splitlines()
        if (".vc-" in line or "stripe-" in line) and "--vc-stripe-" not in line
    )
    assert matrix_rules
    assert _re.search(r"#[0-9a-fA-F]{3,8}\b", matrix_rules) is None
    assert "rgb(" not in matrix_rules


def test_kpi_tiles_and_matrix_panel_bar_follow_variant_c():
    client, _, _ = make_matrix_client()
    for path in ("/", "/fleet"):
        body = client.get(path).text
        # Each tile has a label row and a count row with its filter hint.
        assert body.count('<span class="vc-tile-top">') == 4
        assert body.count('<span class="vc-tile-bottom">') == 4
        assert "filter: hold / handoff / stale" in body
        # The alerts tile carries the alert edge while alerts exist.
        alerts = re.search(
            r'<button[^>]*data-vc-filter="alerts"[^>]*>', matrix_pane(body)
        ).group(0)
        assert "has-alert" in alerts
        # A mono panel bar inside the scroll box names the matrix and its
        # live filter summary.
        box = body.partition('data-scroll-key="fleet-running"')[2]
        bar = box.partition('<div class="vc-panel-bar">')[2].partition("</div>")[0]
        assert "Operations matrix //" in bar
        assert "data-vc-summary" in bar
        assert box.index('class="vc-panel-bar"') < box.index('<table class="fleet">')


def test_rail_carries_scaled_down_filters_sharing_matrix_keys():
    client, _, _ = make_matrix_client()
    for path in ("/", "/fleet"):
        body = client.get(path).text
        rail = body.partition('class="vc-rail"')[2].partition("</nav>")[0]
        strip = rail.partition('class="vc-rail-filters"')[2].partition("</div>")[0]
        chips = re.findall(r'<button[^>]*class="vc-chip[^"]*"[^>]*>', strip)
        assert [re.search(r'data-vc-filter="(\w+)"', c).group(1) for c in chips] == [
            "online",
            "alerts",
            "working",
            "offline",
        ]
        assert 'aria-pressed="true"' in chips[0]
        assert all('aria-pressed="false"' in c for c in chips[1:])
        assert "has-alert" in chips[1]
        counts = re.findall(r'<span class="vc-chip-count">(\d+)</span>', strip)
        assert counts == ["6", "4", "3", "1"]
        # Rail rows carry the matrix filter attributes; offline ones start
        # hidden like their matrix rows, inside a hidden Offline group.
        work = re.search(r'<li class="vc-rail-row"[^>]*>\s*<button[^>]*data-instance="work"', rail)
        assert work is not None
        assert 'data-vc-online="1"' in work.group(0)
        assert 'data-vc-working="1"' in work.group(0)
        assert "hidden" not in work.group(0).partition("<button")[0]
        down = re.search(r'<li class="vc-rail-row"[^>]*>\s*<button[^>]*data-instance="down"', rail)
        assert 'data-vc-offline="1" hidden' in down.group(0)
        assert '<details class="vc-rail-stopped" open hidden>' in rail
    script = client.get("/static/fleet.js").text
    assert 'querySelectorAll("li.vc-rail-row")' in script
    assert "window.applyVcFilters = applyVcFilters;" in script
    assert "window.applyVcFilters" in client.get("/static/drawer.js").text


def test_selected_offline_unit_stays_listed_in_the_rail():
    client, _, _ = make_matrix_client()
    body = client.get("/?instance=down").text
    rail = body.partition('class="vc-rail"')[2].partition("</nav>")[0]
    down = re.search(r'<li class="vc-rail-row"[^>]*>\s*<button[^>]*data-instance="down"', rail)
    assert "hidden" not in down.group(0).partition("<button")[0]
    assert '<details class="vc-rail-stopped" open>' in rail
