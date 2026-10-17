"""Fleet overview panel: per-instance outcomes and cost over a time window.

Covers aggregation across instances (including a failing instance and an
empty window), the bounded parallel read, attention-rank order, and the
panel on the fleet page (static, manual refresh, links to history pages).
Vocabulary: Attempt log, Managed instance, Reach, Attention rank.
"""

from __future__ import annotations

import asyncio
import time

from fake_gateway import FakeGateway
from fastapi.testclient import TestClient

from agent_dashboard.app import create_app
from agent_dashboard.charts import OUTCOME_ORDER
from agent_dashboard.config import Settings
from agent_dashboard.fleet import FleetState
from agent_dashboard.gateway import GatewayError, ManagedInstance
from agent_dashboard.history import HISTORY_LAYOUT
from agent_dashboard.overview import collect_overview, overview_legend


def _started(days_ago, now):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - days_ago * 86400))


def _attempt(issue, started, outcome="complete", cost=None):
    record = {"outcome": outcome, "started_at": started, "duration_seconds": 60}
    if cost is not None:
        record["model_usage"] = {"m": {"input_tokens": 10, "cost_usd": cost}}
    return {"issue": issue, "started": started.replace(":", "-"), "attempt": record}


def _payload(entries):
    return {
        "layout": HISTORY_LAYOUT,
        "attempts": list(entries),
        "active_started_at": None,
        "unreadable": 0,
    }


def make_overview_client(rows, histories=None, history_errors=None, **settings_kwargs):
    """A fleet-page client with scripted attempt histories per container ID.

    ``rows`` maps instance names to container states (``running`` or
    ``not running``); container IDs are ``id-<name>``.
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
    kwargs = {"poll_interval": 5.0, "exec_timeout": 5.0}
    kwargs.update(settings_kwargs)
    app = create_app(
        state=state, settings=Settings(**kwargs), gateway=gateway, start_poller=False
    )
    return TestClient(app, base_url="http://localhost"), state, gateway, now


def _panel(body):
    """The static overview panel section of the fleet page."""
    return body.split('id="overview"')[1].split('id="live"')[0]


# --- aggregation across instances --------------------------------------------


def test_overview_aggregates_outcomes_and_cost_per_instance():
    now = time.time()
    histories = {
        "id-alpha": _payload(
            [
                _attempt(1, _started(1, now), "complete", cost=1.0),
                _attempt(2, _started(2, now), "complete", cost=2.0),
                _attempt(3, _started(3, now), "incomplete"),
            ]
        ),
        "id-beta": _payload([_attempt(9, _started(1, now), "handoff", cost=0.5)]),
    }
    client, _, _, _ = make_overview_client(
        {"alpha": "running", "beta": "running"}, histories
    )
    panel = _panel(client.get("/?window=all").text)
    assert "Fleet overview" in panel
    assert "complete: 2, incomplete: 1" in panel
    assert "handoff: 1" in panel
    assert "$3.00" in panel
    assert "$0.50" in panel
    # Fleet totals cover the readable instances.
    assert "$3.50" in panel
    assert "without usage data" in panel


def test_overview_links_each_instance_to_its_history_page():
    now = time.time()
    histories = {
        "id-alpha": _payload([_attempt(1, _started(1, now), "complete", cost=1.0)]),
        "id-beta": _payload([_attempt(2, _started(1, now), "complete", cost=2.0)]),
    }
    client, _, _, _ = make_overview_client(
        {"alpha": "running", "beta": "running"}, histories
    )
    panel = _panel(client.get("/?window=all").text)
    assert "/instances/alpha/history?window=all" in panel
    assert "/instances/beta/history?window=all" in panel


def test_overview_reuses_outcome_colours_and_legend():
    now = time.time()
    histories = {
        "id-alpha": _payload(
            [
                _attempt(1, _started(1, now), "complete", cost=1.0),
                _attempt(2, _started(2, now), "incomplete", cost=2.0),
                _attempt(3, _started(3, now), "handoff", cost=0.5),
            ]
        ),
    }
    client, _, _, _ = make_overview_client({"alpha": "running"}, histories)
    panel = _panel(client.get("/?window=all").text)
    assert "chart-legend" in panel
    for key in ("complete", "incomplete", "handoff"):
        assert f"outcome-{key}" in panel
    # Glyphs carry the non-colour cue, as on the history page.
    assert "●" in panel and "■" in panel and "▲" in panel


def test_overview_failing_instance_listed_with_reach_excluded_from_totals():
    now = time.time()
    histories = {
        "id-good": _payload([_attempt(1, _started(1, now), "complete", cost=1.5)]),
        "id-broken": {"layout": "unknown", "attempts": []},
    }
    client, _, _, _ = make_overview_client(
        {"good": "running", "bad": "running", "broken": "running"},
        histories,
        {"id-bad": GatewayError("docker exec timed out after 5 s")},
    )
    response = client.get("/?window=all")
    assert response.status_code == 200
    panel = _panel(response.text)
    # Every instance is listed with its reach, readable or not.
    for name in ("good", "bad", "broken"):
        assert f"/instances/{name}/history?window=all" in panel
    assert "Could not read the Attempt log" in panel
    assert "layout not recognised" in panel
    # Only the readable instance feeds the totals.
    assert "$1.50" in panel
    assert "2 unreadable instance(s) excluded" in panel


def test_overview_not_running_instance_listed_without_exec():
    client, _, gateway, _ = make_overview_client({"down": "not running"})
    panel = _panel(client.get("/").text)
    assert "down" in panel
    assert "not running" in panel
    assert "running managed instances only" in panel
    assert gateway.history_calls == []


def test_overview_unknown_outcomes_count_as_other():
    now = time.time()
    histories = {
        "id-work": _payload(
            [
                _attempt(1, _started(1, now), "active"),
                _attempt(2, _started(2, now), "interrupted"),
            ]
        ),
    }
    client, _, _, _ = make_overview_client({"work": "running"}, histories)
    panel = _panel(client.get("/?window=all").text)
    assert "other: 2" in panel
    assert "outcome-other" in panel


# --- empty window and missing usage ------------------------------------------


def test_overview_empty_window_shown_not_zero_cost():
    now = time.time()
    histories = {
        "id-work": _payload(
            [
                _attempt(1, _started(40, now), "complete", cost=1.0),
                _attempt(2, _started(50, now), "complete", cost=4.0),
            ]
        ),
    }
    client, _, _, _ = make_overview_client({"work": "running"}, histories)
    panel = _panel(client.get("/?window=7").text)
    assert "No attempts in this window" in panel
    assert "$0.00" not in panel
    # Both attempts (and their $5.00) drop out with the window.
    assert "$1.00" not in panel
    assert "$4.00" not in panel
    everything = _panel(client.get("/?window=all").text)
    assert "$5.00" in everything


def test_overview_attempts_without_usage_are_na_never_zero():
    now = time.time()
    histories = {
        "id-work": _payload([_attempt(5, _started(1, now), "incomplete")]),
    }
    client, _, _, _ = make_overview_client({"work": "running"}, histories)
    panel = _panel(client.get("/?window=all").text)
    assert "n/a" in panel
    assert "without usage data" in panel
    assert "No cost data in this window." in panel
    assert "$0.00" not in panel


def test_overview_measured_zero_cost_still_charts():
    now = time.time()
    histories = {
        "id-work": _payload([_attempt(5, _started(1, now), "complete", cost=0.0)]),
    }
    client, _, _, _ = make_overview_client({"work": "running"}, histories)
    panel = _panel(client.get("/?window=all").text)
    assert "$0.00" in panel
    assert "No cost data in this window." not in panel


# --- bounded parallel read ---------------------------------------------------


def test_overview_slow_instance_neither_delays_nor_breaks_panel():
    now = time.time()
    histories = {
        "id-fast": _payload([_attempt(1, _started(1, now), "complete", cost=1.0)]),
        "id-slow": _payload([_attempt(2, _started(1, now), "complete", cost=9.0)]),
    }
    client, _, gateway, _ = make_overview_client(
        {"fast": "running", "slow": "running"}, histories, exec_timeout=0.2
    )
    gateway.history_delays["id-slow"] = 30.0
    started = time.monotonic()
    response = client.get("/?window=all")
    elapsed = time.monotonic() - started
    assert response.status_code == 200
    assert elapsed < 10.0
    panel = _panel(response.text)
    assert "/instances/slow/history?window=all" in panel
    assert "timed out" in panel
    # The slow $9.00 stays out of the totals.
    assert "$1.00" in panel
    assert "$9.00" not in panel
    assert "$10.00" not in panel


def test_overview_reads_instances_in_parallel():
    now = time.time()
    histories = {
        "id-a": _payload([_attempt(1, _started(1, now), "complete", cost=1.0)]),
        "id-b": _payload([_attempt(2, _started(1, now), "complete", cost=2.0)]),
        "id-c": _payload([_attempt(3, _started(1, now), "complete", cost=3.0)]),
    }
    client, _, gateway, _ = make_overview_client(
        {"a": "running", "b": "running", "c": "running"}, histories
    )
    gateway.history_delay = 0.3
    started = time.monotonic()
    response = client.get("/?window=all")
    elapsed = time.monotonic() - started
    assert response.status_code == 200
    # Sequential reads would take ~0.9 s; parallel reads share one delay.
    assert elapsed < 0.9
    assert sorted(gateway.history_calls) == ["id-a", "id-b", "id-c"]


def test_collect_overview_empty_fleet_reads_nothing():
    class ExplodingGateway:
        async def attempt_history(self, container_id):
            raise AssertionError("no instances to read")

    result = asyncio.run(collect_overview([], ExplodingGateway()))
    assert result.rows == ()
    assert result.readable == 0
    assert result.total_cost == 0.0


def test_collect_overview_rejects_bad_concurrency():
    async def scenario():
        try:
            await collect_overview([], FakeGateway(), max_concurrent_execs=0)
        except ValueError as exc:
            return str(exc)
        return None

    assert "max concurrent execs" in asyncio.run(scenario())


# --- order, window, static panel ----------------------------------------------


def test_overview_order_follows_attention_rank():
    from test_web import fresh_snapshot, load_snapshot

    from agent_dashboard.collector import run_cycle

    now = time.time()
    state = FleetState()
    gateway = FakeGateway(
        [
            ManagedInstance("work", "id-work", "running"),
            ManagedInstance("idle", "id-idle", "running"),
            ManagedInstance("unreach", "id-unreach", "running"),
        ]
    )
    gateway.set_snapshot(
        "id-work", fresh_snapshot(load_snapshot("snapshot-attempt-in-progress.json"), now)
    )
    gateway.set_snapshot(
        "id-idle", fresh_snapshot(load_snapshot("snapshot-idle-with-last-finished.json"), now)
    )
    gateway.set_error_code("id-unreach", "agent_unreachable", "no socket")
    asyncio.run(run_cycle(state, gateway, max_concurrent_execs=4, now=now))
    for name in ("work", "idle", "unreach"):
        gateway.set_history(
            f"id-{name}",
            _payload([_attempt(1, _started(1, now), "complete", cost=1.0)]),
        )
    gateway.set_history_error("id-unreach", GatewayError("no socket"))
    app = create_app(
        state=state, settings=Settings(poll_interval=5.0), gateway=gateway, start_poller=False
    )
    panel = _panel(TestClient(app, base_url="http://localhost").get("/?window=all").text)
    # Unreachable needs the operator first, then working, then idle.
    positions = [panel.index(f"/instances/{name}/history") for name in ("unreach", "work", "idle")]
    assert positions == sorted(positions)


def test_overview_window_filter_links_and_refresh():
    client, _, _, _ = make_overview_client({"work": "running"})
    panel = _panel(client.get("/?window=7").text)
    assert "?window=7" in panel and "?window=30" in panel and "?window=all" in panel
    assert "Refresh" in panel
    assert panel.count('class="current"') == 1


def test_overview_invalid_window_means_all():
    now = time.time()
    histories = {
        "id-work": _payload([_attempt(3, _started(40, now), "complete", cost=4.0)]),
    }
    client, _, _, _ = make_overview_client({"work": "running"}, histories)
    panel = _panel(client.get("/?window=forever").text)
    assert "$4.00" in panel


def test_overview_panel_is_static_no_stream():
    client, _, _, _ = make_overview_client({"work": "running"})
    panel = _panel(client.get("/").text)
    assert "sse-swap" not in panel
    assert "sse:fleet" not in panel
    assert 'href="/instances/work/history?window=' in panel


def test_overview_empty_fleet():
    client, _, _, _ = make_overview_client({})
    panel = _panel(client.get("/").text)
    assert "No managed instances found." in panel


def test_overview_legend_lists_only_outcomes_present_in_stable_order():
    now = time.time()
    state = FleetState()
    asyncio.run(
        state.apply_cycle(
            [ManagedInstance("a", "id-a", "running"), ManagedInstance("b", "id-b", "running")],
            {},
            now=now,
        )
    )
    gateway = FakeGateway()
    gateway.set_history(
        "id-a", _payload([_attempt(1, _started(1, now), "handoff", cost=1.0)])
    )
    gateway.set_history(
        "id-b", _payload([_attempt(2, _started(1, now), "complete", cost=1.0)])
    )
    rows, _ = asyncio.run(state.snapshot(now=now))
    result = asyncio.run(collect_overview(rows, gateway, window="all", now=now))
    assert [entry.key for entry in overview_legend(result)] == ["complete", "handoff"]
    assert set(OUTCOME_ORDER) >= {entry.key for entry in overview_legend(result)}
