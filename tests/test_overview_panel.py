"""The fleet page's lazily loaded overview panel (``/overview``).

The panel takes the drawer card's range selector (presets plus a fleet-wide
activity strip) and renders outcomes and cost per instance. Its range is
remembered in its own cookie, apart from the drawer's.
Vocabulary: Attempt log, Managed instance, Reach.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from test_drawer_stats import history_with, make_stats_client

from agent_dashboard.gateway import GatewayError
from agent_dashboard.range_stats import COOKIE_NAME, FLEET_COOKIE_NAME


def two_instances():
    client, _, gateway, now = make_stats_client({"alpha": "running", "beta": "running"})
    gateway.set_history(
        "id-alpha",
        history_with(
            now,
            (1, 1, "complete", 1.0),
            (2, 2, "incomplete", 2.0),
            (3, 40, "complete", 8.0),
        ),
    )
    gateway.set_history("id-beta", history_with(now, (9, 3, "handoff", 0.5)))
    return client, gateway, now


def test_panel_renders_both_charts_presets_and_strip():
    client, _, _ = two_instances()
    body = client.get("/overview").text
    assert "Outcomes per instance" in body
    assert "Cost per instance" in body
    for preset in ("24h", "7d", "30d", "90d", "all"):
        assert f'hx-get="/overview?preset={preset}"' in body
    assert 'hx-target="#overview-slot"' in body
    assert 'class="stats-strip" data-base="/overview?" data-target="#overview-slot"' in body
    # Default range is the drawer's: last 30 days.
    assert "last 30 days" in body
    assert 'aria-current="true">30d</a>' in body
    assert "$3.50" in body  # 1.00 + 2.00 + 0.50 within 30 days


def test_panel_bars_link_to_history_with_the_exact_range():
    client, _, _ = two_instances()
    body = client.get("/overview?preset=7d").text
    assert 'href="/instances/alpha/history?preset=7d"' in body
    assert 'href="/instances/beta/history?preset=7d"' in body


def test_panel_preset_all_includes_old_attempts():
    client, _, _ = two_instances()
    body = client.get("/overview?preset=all").text
    assert "$11.50" in body
    assert "all time" in body


def test_panel_custom_span_from_strip():
    client, _, now = two_instances()
    day = datetime.fromtimestamp(now - 40 * 86400, tz=UTC).date()
    lo, hi = (day - timedelta(days=1)).isoformat(), (day + timedelta(days=1)).isoformat()
    body = client.get(f"/overview?from={lo}&to={hi}").text
    assert "$8.00" in body
    assert 'aria-current="true"' not in body


def test_panel_remembers_its_range_apart_from_the_drawer():
    client, _, _ = two_instances()
    response = client.get("/overview?preset=7d")
    assert response.cookies.get(FLEET_COOKIE_NAME).strip('"') == "preset=7d"
    assert COOKIE_NAME not in response.cookies
    # Without an explicit range the remembered one applies.
    assert "last 7 days" in client.get("/overview").text
    # The drawer card still uses its own default.
    assert "last 30 days" in client.get("/instances/alpha/stats").text


def test_panel_lists_unreadable_instance_outside_totals():
    client, gateway, _ = two_instances()
    gateway.set_history_error("id-beta", GatewayError("no socket"))
    body = client.get("/overview").text
    assert "overview-errors" in body
    assert "Could not read the Attempt log: no socket" in body
    assert "$3.00" in body


def test_panel_with_no_instances():
    client, _, _, _ = make_stats_client({})
    body = client.get("/overview").text
    assert "No managed instances found." in body
    assert "stats-strip" not in body
