"""Instance drawer: the /instances/{name} fragment.

Renders the drawer for every contract example plus version-skew variants:
unknown bases, missing fields, null sections and a too-new schema.
"""

import asyncio
import copy
import time

import pytest
from fastapi.testclient import TestClient
from test_web import load_snapshot, make_fleet_client

from agent_dashboard.collector import run_cycle

EXAMPLES = [
    "snapshot-attempt-in-progress.json",
    "snapshot-soft-threshold-handoff.json",
    "snapshot-recovery-hold.json",
    "snapshot-idle-with-last-finished.json",
    "snapshot-truncated.json",
]


def open_drawer(fixture="snapshot-attempt-in-progress.json"):
    client, _, _ = make_fleet_client({"work": load_snapshot(fixture)})
    response = client.get("/instances/work")
    assert response.status_code == 200
    return response.text


def test_drawer_route_exists_for_every_contract_example():
    for fixture in EXAMPLES:
        body = open_drawer(fixture)
        assert "drawer" in body


def test_drawer_section_order():
    body = open_drawer("snapshot-attempt-in-progress.json")
    current = body.index("Current attempt")
    control = body.index("Control", current)
    events = body.index("Recent events", control)
    assert current < control < events


def test_drawer_current_attempt_in_progress():
    body = open_drawer("snapshot-attempt-in-progress.json")
    assert "#977" in body
    assert "Example: add a synthetic widget panel" in body
    # Budget used with its measured/estimated split.
    assert "640,000" in body
    assert "512,000" in body
    assert "128,000" in body
    # Soft threshold value + rule, hard ceiling, turns + continuation.
    assert "3,200,000" in body
    assert "percentage" in body
    assert "4,000,000" in body
    assert ">18 <" in body or ">18<" in body or "18" in body
    assert "86%" in body
    assert "budget-fill" in body
    # Tokens by category.
    assert "120,000" in body  # input_tokens
    assert "350,000" in body  # cache_read_input_tokens
    assert "42,000" in body  # output_tokens
    # Main context vs window with peak, compactions, subagents.
    assert "148,000" in body
    assert "200,000" in body
    assert "151,200" in body
    assert "sub-a1" in body
    assert "sub-a2" in body
    # Provider rate-limit headroom is unavailable, never zero.
    assert "Rate limit" in body or "rate limit" in body
    assert "not exposed through the gateway" in body


def test_drawer_soft_threshold_crossed_shows_rule_and_crossed_at():
    body = open_drawer("snapshot-soft-threshold-handoff.json")
    assert "reserve" in body
    assert "2,960,000" in body  # crossed_at_tokens
    assert "2026-09-30T11:55:00Z" in body  # crossed_at
    assert "crossed" in body.lower()
    # Continuation index beyond zero.
    assert "47" in body


def test_drawer_control_in_progress():
    body = open_drawer("snapshot-attempt-in-progress.json")
    assert "example-owner/example-repo" in body
    assert "0.4.5" in body
    assert "running" in body  # intake
    assert "agent/issue-977" in body  # branch
    assert "model_execution" in body  # phase
    assert "2026-09-30T09:30:00Z" in body  # started
    assert "0 / 3" in body  # consecutive errors
    # Last finished attempt is shown even while an attempt is active.
    assert "#961" in body
    assert "complete" in body
    assert "812,345" in body
    assert "2026-09-29T15:40:00Z" in body
    assert "962" in body  # pull request number


def test_drawer_control_handoff_phase_and_deadline():
    body = open_drawer("snapshot-soft-threshold-handoff.json")
    assert "model_work" in body  # handoff phase
    assert "2026-09-30T12:02:00Z" in body  # model deadline
    assert "2026-09-30T12:04:00Z" in body  # publication deadline


def test_drawer_control_recovery_hold_reason_and_next_action():
    body = open_drawer("snapshot-recovery-hold.json")
    assert "result comment confirmation is not known yet" in body
    assert "agentctl recovery retry" in body
    assert "<code" in body  # next action ready to copy


def test_drawer_idle_shows_no_current_attempt_but_keeps_last_finished():
    body = open_drawer("snapshot-idle-with-last-finished.json")
    assert "n/a" in body  # null current_attempt section
    assert "#961" in body
    assert "complete" in body


def test_drawer_recent_events():
    body = open_drawer("snapshot-attempt-in-progress.json")
    assert "2026-09-30T09:30:00Z" in body  # timestamp
    assert "INFO" in body  # level
    assert "attempt_claimed" in body  # event name
    assert "claimed issue #977" in body  # one-line text


@pytest.mark.parametrize(
    ("fixture", "markers"),
    [
        (
            "snapshot-attempt-in-progress.json",
            ("measured", "estimated", "configured", "sdk_assumed", "unavailable"),
        ),
        (
            "snapshot-soft-threshold-handoff.json",
            ("measured", "estimated", "configured", "unavailable"),
        ),
        ("snapshot-recovery-hold.json", ("measured", "estimated")),
        ("snapshot-idle-with-last-finished.json", ("measured", "estimated")),
        (
            "snapshot-truncated.json",
            ("measured", "estimated", "configured", "sdk_assumed", "unavailable"),
        ),
    ],
)
def test_drawer_every_figure_has_a_basis_marker(fixture, markers):
    body = open_drawer(fixture)
    assert '<span class="basis">' in body
    for marker in markers:
        assert f'<span class="basis">{marker}</span>' in body


def test_drawer_has_no_usd_and_no_charts():
    for fixture in EXAMPLES:
        body = open_drawer(fixture)
        assert "$" not in body
        assert "USD" not in body
        assert "<svg" not in body
        assert "<canvas" not in body


def test_drawer_unknown_instance_is_gone():
    client, _, _ = make_fleet_client({"work": load_snapshot(EXAMPLES[0])})
    body = client.get("/instances/nope").text
    assert "gone" in body.lower()
    assert "nope" in body


def test_drawer_removed_instance_is_gone():
    from agent_dashboard.gateway import ManagedInstance

    client, state, gateway = make_fleet_client(
        {"work": load_snapshot(EXAMPLES[0]), "other": load_snapshot(EXAMPLES[0])}
    )
    assert "640,000" in client.get("/instances/work").text
    # Only "work" disappears; the fleet itself is still polled, so the
    # drawer for a really removed instance renders the gone fragment.
    gateway.set_rows([ManagedInstance("other", "id-other", "running")])
    asyncio.run(run_cycle(state, gateway, now=time.time()))
    gone = client.get("/instances/work")
    assert gone.status_code == 200
    assert "gone" in gone.text.lower()
    assert "640,000" in client.get("/instances/other").text


def test_drawer_empty_fleet_refresh_keeps_drawer():
    """An empty fleet never turns an open drawer into "gone".

    Before the first poll cycle (and whenever a discovery cycle comes
    back empty) the refresh answers 204, so htmx performs no swap and
    the open drawer keeps its last snapshot. This was the self-closing:
    one empty discovery cycle flipped a live drawer into "This instance
    is gone" right after new data was read, then back on the next cycle.
    """
    from agent_dashboard.gateway import ManagedInstance

    client, state, gateway = make_fleet_client({"work": load_snapshot(EXAMPLES[0])})
    assert "640,000" in client.get("/instances/work").text
    # A discovery cycle that comes back empty is transient: keep the drawer.
    gateway.set_rows([])
    asyncio.run(run_cycle(state, gateway, now=time.time()))
    kept = client.get("/instances/work")
    assert kept.status_code == 204
    assert kept.text == ""
    # The drawer heals on its own when discovery reports the instance again.
    gateway.set_rows([ManagedInstance("work", "id-work", "running")])
    gateway.set_snapshot("id-work", load_snapshot(EXAMPLES[0]))
    asyncio.run(run_cycle(state, gateway, now=time.time()))
    healed = client.get("/instances/work")
    assert healed.status_code == 200
    assert "640,000" in healed.text


def test_drawer_startup_empty_fleet_refresh_keeps_drawer():
    """Before the first poll cycle the drawer refresh answers 204."""
    from test_web import make_client

    client, _ = make_client([])
    response = client.get("/instances/work")
    assert response.status_code == 204
    assert response.text == ""


def test_drawer_refresh_swaps_inner_content_only():
    """The aside stays in place: inner swap keeps the scroll container."""
    body = open_drawer("snapshot-attempt-in-progress.json")
    assert 'hx-target="this"' in body
    assert 'hx-select=".drawer-inner"' in body
    assert 'hx-disinherit="*"' in body
    assert 'hx-target="#drawer-holder"' not in body
    assert body.count('id="drawer"') == 1
    assert body.count('class="drawer-inner"') == 1


def test_drawer_refresh_updates_every_figure():
    """A live refresh re-renders the drawer from the newest snapshot."""
    client, state, gateway = make_fleet_client({"work": load_snapshot(EXAMPLES[0])})
    first = client.get("/instances/work")
    assert first.status_code == 200
    assert "640,000" in first.text
    assert 'id="drawer"' in first.text
    snap = load_snapshot(EXAMPLES[0])
    snap["current_attempt"]["budget"]["used"]["value"] = 700000
    gateway.set_snapshot("id-work", snap)
    asyncio.run(run_cycle(state, gateway, now=time.time()))
    second = client.get("/instances/work")
    assert second.status_code == 200
    assert "700,000" in second.text
    assert "640,000" not in second.text
    assert second.text.count('id="drawer"') == 1


def test_drawer_helpers_keep_scroll_and_drop_instance_param():
    client, _, _ = make_fleet_client({"work": load_snapshot(EXAMPLES[0])})
    static = client.get("/static/drawer.js")
    assert static.status_code == 200
    # Closing the drawer empties the holder and drops ?instance=, so a
    # reload does not reopen it.
    assert 'searchParams.delete("instance")' in static.text
    # The in-place refresh keeps the aside's scroll position.
    assert "scrollTop" in static.text
    assert "htmx:beforeSwap" in static.text
    assert "htmx:afterSwap" in static.text


def test_drawer_skewed_snapshot_renders():
    snap = load_snapshot("snapshot-attempt-in-progress.json")
    snap["current_attempt"]["budget"]["used"]["basis"] = "telepathic"
    del snap["current_attempt"]["turns"]
    del snap["control"]["consecutive_errors"]
    del snap["control"]["active_attempt"]["issue_title"]
    del snap["control"]["active_attempt"]["branch"]
    snap["control"]["stop_plan"] = {"reason": "operator-stop", "at": "2026-09-30T10:00:00Z"}
    client, _, _ = make_fleet_client({"work": snap})
    body = client.get("/instances/work").text
    assert "estimated" in body  # unknown basis falls back to estimated
    assert "unavailable" in body  # missing fields never render as zero
    assert "None" not in body  # partial attempt rows skip missing parts
    assert "operator-stop" in body  # stop plan contents are shown


def test_drawer_helpers_are_vendored_no_cdn():
    client, _, _ = make_fleet_client({"work": load_snapshot(EXAMPLES[0])})
    body = client.get("/").text
    assert "/static/drawer.js" in body
    static = client.get("/static/drawer.js")
    assert static.status_code == 200
    assert "closeDrawer" in static.text


def test_drawer_unsupported_schema_keeps_stable_fields_only():
    snap = load_snapshot("snapshot-attempt-in-progress.json")
    snap["schema_version"] = 2
    client, _, _ = make_fleet_client({"work": snap})
    body = client.get("/instances/work").text
    assert "unsupported schema v2" in body
    assert "example-owner/example-repo" in body
    assert "640,000" not in body


def test_drawer_refreshes_on_fleet_sse_events():
    body = open_drawer("snapshot-attempt-in-progress.json")
    assert 'hx-trigger="sse:fleet"' in body
    assert 'hx-get="/instances/work"' in body


def test_index_reopens_drawer_from_query_param():
    client, _, _ = make_fleet_client({"work": load_snapshot(EXAMPLES[0])})
    reopened = client.get("/?instance=work").text
    assert 'id="drawer"' in reopened
    assert "512,000" in reopened  # drawer-only measured split, not in the table
    assert 'id="drawer"' not in client.get("/").text
    gone = client.get("/?instance=nope").text
    assert 'id="drawer"' in gone
    assert "gone" in gone.lower()


def test_table_rows_open_the_drawer():
    client, _, _ = make_fleet_client({"work": load_snapshot(EXAMPLES[0])})
    body = client.get("/").text
    assert 'hx-get="/instances/work"' in body
    assert "?instance=work" in body


def test_drawer_never_reaches_gateway():
    class ExplodingGateway:
        async def list_instances(self):
            raise AssertionError("request path reached the gateway")

        async def telemetry(self, container_id):
            raise AssertionError("request path reached the gateway")

    import asyncio as _asyncio

    from agent_dashboard.app import create_app
    from agent_dashboard.config import Settings
    from agent_dashboard.fleet import FleetState
    from agent_dashboard.gateway import ManagedInstance

    state = FleetState()
    _asyncio.run(
        state.apply_cycle([ManagedInstance("w", "id-w", "running")], {}, now=1000.0)
    )
    app = create_app(
        state=state, settings=Settings(), gateway=ExplodingGateway(), start_poller=False
    )
    client = TestClient(app, base_url="http://localhost")
    assert client.get("/instances/w").status_code == 200
    assert client.get("/instances/nope").status_code == 200


def test_drawer_truncated_snapshot_notes_trimmed_events():
    snap = load_snapshot("snapshot-truncated.json")
    assert snap["truncated"] is True
    client, _, _ = make_fleet_client({"work": snap})
    body = client.get("/instances/work").text
    assert "token_budget_reconciled" in body
    assert "1 / 3" in body


def test_drawer_skewed_null_sections():
    snap = load_snapshot("snapshot-attempt-in-progress.json")
    skewed = copy.deepcopy(snap)
    skewed["control"] = None
    skewed["current_attempt"] = None
    client, _, _ = make_fleet_client({"work": skewed})
    body = client.get("/instances/work").text
    assert "n/a" in body
    assert "Current attempt" in body
    assert "Control" in body
    assert "Recent events" in body
