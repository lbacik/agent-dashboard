"""Drawer attempt-statistics card: slot, ranges, memory and hints.

The per-instance figures live in a card at the top of the drawer, loaded once
when the drawer opens and on every range change — never on the poll cycle.
Vocabulary: Attempt log, Managed instance.
"""

from __future__ import annotations

import asyncio
import time

from fake_gateway import FakeGateway
from fastapi.testclient import TestClient

from agent_dashboard.app import create_app
from agent_dashboard.config import Settings
from agent_dashboard.fleet import FleetState
from agent_dashboard.gateway import GatewayError, ManagedInstance
from agent_dashboard.history import HISTORY_LAYOUT


def _started(days_ago: float, now: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - days_ago * 86400))


def _attempt(issue: int, started: str, outcome: str = "complete", cost=None) -> dict:
    record = {"outcome": outcome, "started_at": started, "duration_seconds": 60}
    if cost is not None:
        record["model_usage"] = {"m": {"input_tokens": 10, "cost_usd": cost}}
    return {"issue": issue, "started": started.replace(":", "-"), "attempt": record}


def _payload(entries) -> dict:
    return {
        "layout": HISTORY_LAYOUT,
        "attempts": list(entries),
        "active_started_at": None,
        "unreadable": 0,
    }


def make_stats_client(
    rows, histories=None, history_errors=None, logs_trees=None, **settings_kwargs
):
    """A client with scripted instance states and attempt histories.

    ``rows`` maps names to container states (``running`` or ``not running``);
    container IDs are ``id-<name>``.
    """
    now = time.time()
    state = FleetState()
    instances = []
    for name, container_state in rows.items():
        raw = "running" if container_state == "running" else "exited"
        instances.append(ManagedInstance(name, f"id-{name}", raw))
    asyncio.run(state.apply_cycle(instances, {}, now=now))
    gateway = FakeGateway()
    for container_id, payload in (histories or {}).items():
        gateway.set_history(container_id, payload)
    for container_id, error in (history_errors or {}).items():
        gateway.set_history_error(container_id, error)
    for container_id, tree in (logs_trees or {}).items():
        gateway.set_logs_tree(container_id, tree)
    kwargs = {"poll_interval": 5.0, "exec_timeout": 5.0}
    kwargs.update(settings_kwargs)
    app = create_app(
        state=state, settings=Settings(**kwargs), gateway=gateway, start_poller=False
    )
    return TestClient(app, base_url="http://localhost"), state, gateway, now


def history_with(now, *attempts) -> dict:
    return _payload(
        [
            _attempt(issue, _started(days, now), outcome, cost)
            for issue, days, outcome, cost in attempts
        ]
    )


# --- drawer slot -----------------------------------------------------------


def test_drawer_contains_stats_slot_loaded_once():
    client, _, _, _ = make_stats_client({"work": "running"})
    body = client.get("/instances/work").text
    assert 'id="stats-work"' in body
    assert "hx-preserve" in body
    assert "/instances/work/stats" in body
    assert 'hx-trigger="load"' in body
    assert "Loading…" in body


def test_drawer_slot_survives_refresh_attributes():
    """The refresh swaps the drawer inner, so the slot must opt out via preserve."""
    client, _, _, _ = make_stats_client({"work": "running"})
    first = client.get("/instances/work").text
    second = client.get("/instances/work").text
    assert first.count('id="stats-work"') == 1
    assert second.count('id="stats-work"') == 1
    assert "hx-disinherit" in first


def test_drawer_refresh_never_reads_attempt_summaries():
    now = time.time()
    client, _, gateway, _ = make_stats_client(
        {"work": "running"},
        {"id-work": history_with(now, (1, 1, "complete", 1.0))},
    )
    client.get("/instances/work")
    client.get("/instances/work")
    assert gateway.history_calls == []


def test_drawer_gone_fragment_has_no_stats_slot():
    client, _, _, _ = make_stats_client({"work": "running"})
    body = client.get("/instances/nope").text
    assert "gone" in body.lower()
    assert "/stats" not in body


# --- default card ----------------------------------------------------------


def test_stats_default_range_is_30_days():
    now = time.time()
    client, _, _, _ = make_stats_client(
        {"work": "running"},
        {
            "id-work": history_with(
                now,
                (1, 1, "complete", 1.0),
                (2, 10, "incomplete", None),
                (3, 100, "complete", 4.0),
            )
        },
    )
    body = client.get("/instances/work/stats").text
    assert "last 30 days" in body
    assert "Attempt history →" in body
    assert "/instances/work/history?preset=30d" in body
    # Two of three attempts in range; only the recent one has usage data.
    assert "2" in body and "of 3 attempts" in body
    assert "$1.00" in body
    assert "$4.00" not in body
    assert "1 w/o usage" in body


def test_stats_card_has_strip_bar_and_legend():
    now = time.time()
    client, _, _, _ = make_stats_client(
        {"work": "running"},
        {"id-work": history_with(now, (1, 1, "complete", 1.0), (2, 2, "incomplete", None))},
    )
    body = client.get("/instances/work/stats").text
    # Activity strip: 90 day columns with per-day tooltips.
    assert "stats-strip" in body
    assert body.count("strip-day") >= 90
    assert "data-days=" in body
    # Stacked outcome bar for the range.
    assert "stats-bar" in body
    assert "outcome-complete" in body
    # Legend with the overview glyphs and labels.
    assert "chart-legend" in body
    assert "●" in body and "■" in body
    assert "complete" in body and "incomplete" in body


def test_stats_strip_highlights_preset_days_including_empty():
    now = time.time()
    client, _, _, _ = make_stats_client(
        {"work": "running"},
        {"id-work": history_with(now, (1, 1, "complete", 1.0))},
    )
    body = client.get("/instances/work/stats?preset=7d").text
    assert body.count("is-sel") == 7
    assert "last 7 days" in body


def test_stats_all_selects_every_strip_day():
    now = time.time()
    client, _, _, _ = make_stats_client(
        {"work": "running"},
        {"id-work": history_with(now, (1, 1, "complete", 1.0))},
    )
    body = client.get("/instances/work/stats?preset=all").text
    assert body.count("is-sel") == 90
    assert "all time" in body


# --- presets, day click and drag -------------------------------------------


def test_stats_each_preset_updates_only_the_card():
    now = time.time()
    client, _, _, _ = make_stats_client(
        {"work": "running"},
        {
            "id-work": history_with(
                now,
                (1, 0.5, "complete", 1.0),
                (2, 10, "incomplete", None),
                (3, 100, "complete", 2.0),
            )
        },
    )
    labels = {
        "24h": "last 24 hours",
        "7d": "last 7 days",
        "30d": "last 30 days",
        "90d": "last 90 days",
        "all": "all time",
    }
    for preset, label in labels.items():
        body = client.get(f"/instances/work/stats?preset={preset}").text
        assert label in body
        assert body.count('class="current"') == 1
        assert f"stats?preset={preset}" in body
        # The card fragment carries no drawer chrome.
        assert 'id="drawer"' not in body


def test_stats_preset_history_link_keeps_the_exact_range():
    now = time.time()
    client, _, _, _ = make_stats_client(
        {"work": "running"},
        {"id-work": history_with(now, (1, 1, "complete", 1.0))},
    )
    assert "/history?preset=7d" in client.get("/instances/work/stats?preset=7d").text
    assert "/history?preset=30d" in client.get("/instances/work/stats?preset=30d").text
    assert "/history?preset=24h" in client.get("/instances/work/stats?preset=24h").text
    assert "/history?preset=90d" in client.get("/instances/work/stats?preset=90d").text
    assert "/history?preset=all" in client.get("/instances/work/stats?preset=all").text
    day = time.strftime("%Y-%m-%d", time.gmtime(now - 1 * 86400))
    body = client.get(f"/instances/work/stats?from={day}&to={day}").text
    assert f"/history?from={day}&amp;to={day}" in body


def test_stats_clicking_a_day_selects_that_day():
    now = time.time()
    day = time.strftime("%Y-%m-%d", time.gmtime(now - 5 * 86400))
    label = time.strftime("%-d %b %Y", time.gmtime(now - 5 * 86400))
    client, _, _, _ = make_stats_client(
        {"work": "running"},
        {
            "id-work": history_with(
                now, (1, 5, "complete", 1.0), (2, 6, "incomplete", None)
            )
        },
    )
    body = client.get(f"/instances/work/stats?from={day}&to={day}").text
    assert label in body
    assert "1" in body and "of 2 attempts" in body
    assert "$1.00" in body
    # No preset is current for a strip-picked day.
    assert 'class="current"' not in body


def test_stats_dragging_selects_a_range():
    now = time.time()
    start = time.strftime("%Y-%m-%d", time.gmtime(now - 10 * 86400))
    end = time.strftime("%Y-%m-%d", time.gmtime(now - 5 * 86400))
    start_label = time.strftime("%-d %b", time.gmtime(now - 10 * 86400))
    end_label = time.strftime("%-d %b %Y", time.gmtime(now - 5 * 86400))
    client, _, _, _ = make_stats_client(
        {"work": "running"},
        {
            "id-work": history_with(
                now,
                (1, 1, "complete", 1.0),
                (2, 7, "incomplete", None),
                (3, 100, "complete", 3.0),
            )
        },
    )
    body = client.get(f"/instances/work/stats?from={start}&to={end}").text
    assert f"{start_label} – {end_label}" in body
    assert "1" in body and "of 3 attempts" in body
    assert 'class="current"' not in body


# --- remembered range ------------------------------------------------------


def test_stats_range_is_remembered_across_instances_and_reopenings():
    now = time.time()
    payload = history_with(now, (1, 1, "complete", 1.0))
    client, _, _, _ = make_stats_client(
        {"alpha": "running", "beta": "running"},
        {"id-alpha": payload, "id-beta": payload},
    )
    first = client.get("/instances/alpha/stats?preset=7d")
    assert "last 7 days" in first.text
    cookie = first.headers["set-cookie"].split(";")[0]
    # Another instance opens with the remembered range, no explicit param.
    beta = client.get("/instances/beta/stats", headers={"Cookie": cookie})
    assert "last 7 days" in beta.text
    assert beta.text.count('class="current"') == 1
    # Reopening the drawer keeps it too.
    reopened = client.get("/instances/alpha/stats", headers={"Cookie": cookie})
    assert "last 7 days" in reopened.text


def test_stats_invalid_preset_falls_back_to_default():
    now = time.time()
    client, _, _, _ = make_stats_client(
        {"work": "running"},
        {"id-work": history_with(now, (1, 1, "complete", 1.0))},
    )
    body = client.get("/instances/work/stats?preset=forever").text
    assert "last 30 days" in body


# --- hints instead of figures ----------------------------------------------


def test_stats_unknown_instance_renders_a_hint():
    client, _, _, _ = make_stats_client({"work": "running"})
    body = client.get("/instances/nope/stats").text
    assert "Unknown Managed instance" in body
    assert "stats-bar" not in body
    assert "$" not in body


def test_stats_stopped_instance_reads_via_copy():
    import json as _json

    from fake_gateway import make_logs_tree

    started_dash = "2026-09-27T16-33-43.385090Z"
    started_colon = "2026-09-27T16:33:43.385090Z"
    attempt = {
        "outcome": "complete",
        "started_at": started_colon,
        "duration_seconds": 60,
        "model_usage": {"m": {"input_tokens": 10, "cost_usd": 1.5}},
    }
    tree = make_logs_tree(
        {f"logs/1/{started_dash}/attempt.json": _json.dumps(attempt).encode()}
    )
    client, _, gateway, _ = make_stats_client(
        {"down": "not running"}, logs_trees={"id-down": tree}
    )
    body = client.get("/instances/down/stats").text
    assert "stats-bar" in body
    assert "$1.50" in body
    assert gateway.logs_tree_calls == ["id-down"]
    assert gateway.history_calls == []


def test_stats_stopped_instance_without_tree_renders_a_hint():
    client, _, gateway, _ = make_stats_client({"down": "not running"})
    body = client.get("/instances/down/stats").text
    assert "its volume is gone or it holds no logs" in body
    assert "stats-bar" not in body
    assert "$" not in body
    assert gateway.logs_tree_calls == ["id-down"]
    assert gateway.history_calls == []


def test_stats_unreadable_summary_renders_a_hint_never_zero_figures():
    client, _, _, _ = make_stats_client(
        {"work": "running"},
        {"id-work": {"layout": "unknown", "attempts": []}},
    )
    body = client.get("/instances/work/stats").text
    assert "layout not recognised" in body
    assert "stats-bar" not in body
    assert "$0.00" not in body


def test_stats_gateway_error_renders_a_hint():
    client, _, gateway, _ = make_stats_client(
        {"work": "running"}, history_errors={"id-work": GatewayError("no socket")}
    )
    body = client.get("/instances/work/stats").text
    assert "Could not read the Attempt log" in body
    assert "stats-bar" not in body
    assert "$" not in body
    assert gateway.history_calls == ["id-work"]


def test_stats_empty_range_shows_hint_not_zero_cost():
    now = time.time()
    client, _, _, _ = make_stats_client(
        {"work": "running"},
        {"id-work": history_with(now, (1, 100, "complete", 5.0))},
    )
    body = client.get("/instances/work/stats?preset=7d").text
    assert "No attempts in this window" in body
    assert "0</strong> of 1 attempts" in body
    assert "$0.00" not in body
    assert "$5.00" not in body
    assert "stats-bar" not in body


def test_stats_unreadable_attempts_are_counted():
    now = time.time()
    payload = _payload(
        [
            _attempt(1, _started(1, now), "complete", 1.0),
            {"issue": 2, "started": _started(2, now).replace(":", "-"), "attempt": None},
            {"bogus": True},
        ]
    )
    client, _, _, _ = make_stats_client({"work": "running"}, {"id-work": payload})
    body = client.get("/instances/work/stats?preset=all").text
    assert "of 2 attempts" in body
    assert "1 unreadable" in body


# --- never on the poll cycle -----------------------------------------------


def test_stats_read_once_per_range_change_never_per_poll():
    now = time.time()
    client, _, gateway, _ = make_stats_client(
        {"work": "running"},
        {"id-work": history_with(now, (1, 1, "complete", 1.0))},
    )
    assert gateway.history_calls == []
    client.get("/instances/work/stats")
    assert gateway.history_calls == ["id-work"]
    # Drawer live refreshes re-render the drawer, not the card.
    client.get("/instances/work")
    client.get("/instances/work")
    assert gateway.history_calls == ["id-work"]
    client.get("/instances/work/stats?preset=7d")
    assert gateway.history_calls == ["id-work", "id-work"]


# --- non-colour cues -------------------------------------------------------


def test_stats_never_relies_on_colour_alone():
    now = time.time()
    client, _, _, _ = make_stats_client(
        {"work": "running"},
        {
            "id-work": history_with(
                now, (1, 1, "complete", 1.0), (2, 2, "incomplete", None)
            )
        },
    )
    body = client.get("/instances/work/stats").text
    # Every strip day and bar segment names its outcomes in a tooltip.
    assert "<title>" in body
    assert "complete 1" in body or "complete: 1" in body
    # The legend pairs glyphs with labels.
    for glyph in ("●", "■"):
        assert glyph in body
    assert 'aria-label="complete: 1' in body or "complete" in body


def test_drawer_helpers_cover_the_strip_interaction():
    client, _, _, _ = make_stats_client({"work": "running"})
    static = client.get("/static/drawer.js")
    assert static.status_code == 200
    assert "stats-strip" in static.text
    assert "data-days" in static.text
    assert "htmx.ajax" in static.text
    assert "pointerdown" in static.text and "pointerup" in static.text
