"""Fleet overview aggregation: per-instance outcomes and cost over a range.

Covers aggregation across instances (including a failing instance and an
empty range), the bounded parallel read and attention-rank order. These
tests exercise the aggregation (``collect_overview``) directly; the lazily
loaded ``/overview`` panel is covered in ``test_overview_panel.py``, and
that ``/`` reads no attempt summaries on load in ``test_web.py``.
Vocabulary: Attempt log, Managed instance, Reach, Attention rank.
"""

from __future__ import annotations

import asyncio
import json
import time

from fake_gateway import FakeGateway, make_logs_tree

from agent_dashboard.charts import OUTCOME_ORDER
from agent_dashboard.fleet import FleetState
from agent_dashboard.gateway import (
    AttemptLogTimeoutError,
    GatewayError,
    ManagedInstance,
    TelemetryTimeoutError,
)
from agent_dashboard.history import HISTORY_LAYOUT
from agent_dashboard.overview import collect_overview, overview_legend
from agent_dashboard.range_stats import RangeSpec


def _started(days_ago, now):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - days_ago * 86400))


def _attempt(issue, started, outcome="complete", cost=None, snake=False):
    record = {"outcome": outcome, "started_at": started, "duration_seconds": 60}
    if cost is not None:
        if snake:
            # Snake_case record kept for tolerance: older payloads still read.
            record["model_usage"] = {"m": {"input_tokens": 10, "cost_usd": cost}}
        else:
            record["model_usage"] = {
                "m": {
                    "inputTokens": 10,
                    "cacheReadInputTokens": 30,
                    "cacheCreationInputTokens": 2,
                    "outputTokens": 5,
                    "costUSD": cost,
                    "costBasis": "managed",
                }
            }
            record["token_budget"] = {"total_cost_usd": cost}
            record["model_stop_reason"] = "end_turn"
    return {"issue": issue, "started": started.replace(":", "-"), "attempt": record}


def _payload(entries):
    return {
        "layout": HISTORY_LAYOUT,
        "attempts": list(entries),
        "active_started_at": None,
        "unreadable": 0,
    }


def make_rows(names, now=None):
    """Fleet rows for running instances, without telemetry (no gateway)."""
    now = time.time() if now is None else now
    state = FleetState()
    asyncio.run(
        state.apply_cycle(
            [ManagedInstance(name, f"id-{name}", "running") for name in names],
            {},
            now=now,
        )
    )
    rows, _ = asyncio.run(state.snapshot(now=now))
    return rows, now


def collect(rows, gateway, now, preset="all", **kwargs):
    return asyncio.run(
        collect_overview(rows, gateway, spec=RangeSpec(preset), now=now, **kwargs)
    )


def by_name(result):
    return {inst.name: inst for inst in result.rows}


# --- aggregation across instances --------------------------------------------


def test_overview_aggregates_outcomes_and_cost_per_instance():
    now = time.time()
    gateway = FakeGateway()
    gateway.set_history(
        "id-alpha",
        _payload(
            [
                _attempt(1, _started(1, now), "complete", cost=1.0),
                # Snake_case record kept for tolerance.
                _attempt(2, _started(2, now), "complete", cost=2.0, snake=True),
                _attempt(3, _started(3, now), "incomplete"),
            ]
        ),
    )
    gateway.set_history(
        "id-beta", _payload([_attempt(9, _started(1, now), "handoff", cost=0.5)])
    )
    rows, now = make_rows(["alpha", "beta"], now)
    result = collect(rows, gateway, now)
    insts = by_name(result)
    assert insts["alpha"].counts == {"complete": 2, "incomplete": 1}
    assert insts["alpha"].outcome_text == "complete: 2, incomplete: 1"
    assert insts["alpha"].cost_total == 3.0
    assert insts["beta"].counts == {"handoff": 1}
    assert insts["beta"].cost_total == 0.5
    # Fleet totals cover the readable instances.
    assert result.total_cost == 3.5
    assert result.total_with == 3
    assert result.total_missing == 1


def test_overview_reuses_outcome_order_and_legend():
    now = time.time()
    gateway = FakeGateway()
    gateway.set_history(
        "id-alpha",
        _payload(
            [
                _attempt(1, _started(1, now), "complete", cost=1.0),
                _attempt(2, _started(2, now), "incomplete", cost=2.0),
                _attempt(3, _started(3, now), "handoff", cost=0.5),
            ]
        ),
    )
    rows, now = make_rows(["alpha"], now)
    result = collect(rows, gateway, now)
    assert [entry.key for entry in overview_legend(result)] == [
        "complete",
        "incomplete",
        "handoff",
    ]
    assert set(OUTCOME_ORDER) >= {
        entry.key for entry in overview_legend(result)
    }


def test_overview_failing_instance_listed_with_reach_excluded_from_totals():
    now = time.time()
    gateway = FakeGateway()
    gateway.set_history(
        "id-good", _payload([_attempt(1, _started(1, now), "complete", cost=1.5)])
    )
    gateway.set_history("id-broken", {"layout": "unknown", "attempts": []})
    gateway.set_history_error("id-bad", GatewayError("docker exec timed out after 5 s"))
    rows, now = make_rows(["good", "bad", "broken"], now)
    result = collect(rows, gateway, now)
    insts = by_name(result)
    # Every instance is listed with its reach, readable or not.
    assert {inst.name for inst in result.rows} == {"good", "bad", "broken"}
    assert insts["good"].ok
    assert not insts["bad"].ok
    assert not insts["broken"].ok
    assert "Could not read the Attempt log" in insts["bad"].error
    assert "layout not recognised" in insts["broken"].error
    # Only the readable instance feeds the totals.
    assert result.readable == 1
    assert result.unreadable == 2
    assert result.total_cost == 1.5


def test_overview_not_running_instance_reads_logs_tree_without_exec():
    now = time.time()
    gateway = FakeGateway()
    state = FleetState()
    asyncio.run(
        state.apply_cycle(
            [
                ManagedInstance("down", "id-down", "exited"),
                ManagedInstance("gone", "id-gone", "exited"),
            ],
            {},
            now=now,
        )
    )
    rows, _ = asyncio.run(state.snapshot(now=now))
    assert {row.state for row in rows} == {"not running"}
    # Stopped-instance run directories carry fractional seconds.
    started = _started(1, now).replace("Z", ".123456Z")
    record = _attempt(7, started, "complete", cost=2.0)["attempt"]
    gateway.set_logs_tree(
        "id-down",
        make_logs_tree(
            {
                f"logs/7/{started.replace(':', '-')}/attempt.json": json.dumps(
                    record
                ).encode()
            }
        ),
    )
    result = collect(rows, gateway, now)
    insts = by_name(result)
    assert insts["down"].ok
    assert insts["down"].counts == {"complete": 1}
    assert insts["down"].cost_total == 2.0
    # No volume: listed with a clear message and left out of the totals.
    assert not insts["gone"].ok
    assert "volume is gone" in insts["gone"].error
    assert result.total_cost == 2.0
    assert gateway.history_calls == []
    assert sorted(gateway.logs_tree_calls) == ["id-down", "id-gone"]


def test_overview_unknown_outcomes_count_as_other():
    now = time.time()
    gateway = FakeGateway()
    gateway.set_history(
        "id-work",
        _payload(
            [
                _attempt(1, _started(1, now), "active"),
                _attempt(2, _started(2, now), "interrupted"),
            ]
        ),
    )
    rows, now = make_rows(["work"], now)
    result = collect(rows, gateway, now)
    assert result.rows[0].counts == {"other": 2}
    assert result.rows[0].outcome_text == "other: 2"


# --- empty window and missing usage ------------------------------------------


def test_overview_empty_window_shown_not_zero_cost():
    now = time.time()
    gateway = FakeGateway()
    gateway.set_history(
        "id-work",
        _payload(
            [
                _attempt(1, _started(40, now), "complete", cost=1.0),
                _attempt(2, _started(50, now), "complete", cost=4.0),
            ]
        ),
    )
    rows, now = make_rows(["work"], now)
    week = collect(rows, gateway, now, preset="7d")
    assert week.rows[0].shown == 0
    assert week.rows[0].cost_text == "No attempts in this window"
    assert week.total_cost == 0.0
    everything = collect(rows, gateway, now, preset="all")
    assert everything.rows[0].cost_total == 5.0


def test_overview_attempts_without_usage_are_na_never_zero():
    now = time.time()
    gateway = FakeGateway()
    gateway.set_history(
        "id-work", _payload([_attempt(5, _started(1, now), "incomplete")])
    )
    rows, now = make_rows(["work"], now)
    result = collect(rows, gateway, now)
    assert result.rows[0].cost_total is None
    assert result.rows[0].cost_text == "n/a"
    assert result.total_cost == 0.0
    assert result.total_missing == 1


def test_overview_measured_zero_cost_still_counts():
    now = time.time()
    gateway = FakeGateway()
    gateway.set_history(
        "id-work", _payload([_attempt(5, _started(1, now), "complete", cost=0.0)])
    )
    rows, now = make_rows(["work"], now)
    result = collect(rows, gateway, now)
    assert result.rows[0].cost_total == 0.0
    assert result.rows[0].cost_text == "$0.00"
    assert result.total_with == 1


def test_overview_default_range_is_30_days_and_keeps_full_history():
    now = time.time()
    gateway = FakeGateway()
    gateway.set_history(
        "id-work",
        _payload(
            [
                _attempt(3, _started(40, now), "complete", cost=4.0),
                _attempt(4, _started(2, now), "handoff", cost=1.0),
            ]
        ),
    )
    rows, now = make_rows(["work"], now)
    result = asyncio.run(collect_overview(rows, gateway, now=now))
    assert result.spec.preset == "30d"
    assert result.rows[0].counts == {"handoff": 1}
    assert result.rows[0].cost_total == 1.0
    # The strip feed keeps every attempt, not just the cut range.
    assert len(result.summaries) == 2


def test_overview_custom_span_counts_only_days_inside():
    from datetime import UTC, datetime, timedelta

    now = time.time()
    gateway = FakeGateway()
    gateway.set_history(
        "id-work",
        _payload(
            [
                _attempt(1, _started(10, now), "complete", cost=1.0),
                _attempt(2, _started(3, now), "incomplete", cost=2.0),
            ]
        ),
    )
    rows, now = make_rows(["work"], now)
    day = datetime.fromtimestamp(now - 10 * 86400, tz=UTC).date()
    spec = RangeSpec(preset="", start=day - timedelta(days=1), end=day + timedelta(days=1))
    result = asyncio.run(collect_overview(rows, gateway, spec=spec, now=now))
    assert result.rows[0].counts == {"complete": 1}
    assert result.total_cost == 1.0


# --- bounded parallel read ---------------------------------------------------


def test_overview_slow_instance_neither_delays_nor_breaks_panel():
    now = time.time()
    gateway = FakeGateway()
    gateway.set_history(
        "id-fast", _payload([_attempt(1, _started(1, now), "complete", cost=1.0)])
    )
    # The gateway's own timeout fires for the slow instance: its typed
    # error becomes that row's message, never a panel failure.
    gateway.set_history_error(
        "id-slow", TelemetryTimeoutError("docker exec timed out after 5 s")
    )
    rows, now = make_rows(["fast", "slow"], now)
    started = time.monotonic()
    result = asyncio.run(collect_overview(rows, gateway, spec=RangeSpec("all"), now=now))
    elapsed = time.monotonic() - started
    assert elapsed < 10.0
    insts = by_name(result)
    assert insts["fast"].ok
    assert not insts["slow"].ok
    assert "timed out" in insts["slow"].error
    assert "Could not read the Attempt log" in insts["slow"].error
    # The slow $9.00 stays out of the totals.
    assert result.total_cost == 1.0


def test_overview_adds_no_timeout_of_its_own(monkeypatch):
    """A read slower than the old 5 s budget still returns a normal row.

    The overview used to wrap every gateway read in ``asyncio.wait_for``;
    the spy fails the test if the overview ever bounds a read itself. The
    delayed read stands in for any read slower than the removed budget but
    within the gateway's own timeout: it must complete as a normal row.
    """
    now = time.time()
    gateway = FakeGateway()
    gateway.set_history(
        "id-work", _payload([_attempt(1, _started(1, now), "complete", cost=1.0)])
    )
    gateway.history_delays["id-work"] = 0.2
    calls = []
    original_wait_for = asyncio.wait_for

    async def spy(coro, timeout):
        calls.append(timeout)
        return await original_wait_for(coro, timeout)

    monkeypatch.setattr(asyncio, "wait_for", spy)
    rows, now = make_rows(["work"], now)
    result = asyncio.run(collect_overview(rows, gateway, spec=RangeSpec("all"), now=now))
    assert calls == []
    assert result.rows[0].ok
    assert result.rows[0].counts == {"complete": 1}
    assert result.total_cost == 1.0


def test_overview_gateway_timeout_is_error_row_for_that_instance_only():
    """Gateway timeouts (running exec and stopped copy) read as error rows."""
    now = time.time()
    gateway = FakeGateway()
    state = FleetState()
    asyncio.run(
        state.apply_cycle(
            [
                ManagedInstance("fast", "id-fast", "running"),
                ManagedInstance("slow", "id-slow", "running"),
                ManagedInstance("down", "id-down", "exited"),
            ],
            {},
            now=now,
        )
    )
    rows, _ = asyncio.run(state.snapshot(now=now))
    gateway.set_history(
        "id-fast", _payload([_attempt(1, _started(1, now), "complete", cost=1.5)])
    )
    gateway.set_history_error(
        "id-slow", TelemetryTimeoutError("docker exec timed out after 5 s")
    )
    gateway.set_logs_tree(
        "id-down", AttemptLogTimeoutError("docker cp timed out after 15 s")
    )
    result = asyncio.run(collect_overview(rows, gateway, spec=RangeSpec("all"), now=now))
    insts = by_name(result)
    assert insts["fast"].ok
    assert not insts["slow"].ok
    assert "Could not read the Attempt log" in insts["slow"].error
    assert "timed out" in insts["slow"].error
    assert not insts["down"].ok
    assert "Could not read the Attempt log" in insts["down"].error
    assert "timed out" in insts["down"].error
    # Only the readable instance feeds the totals.
    assert result.readable == 1
    assert result.unreadable == 2
    assert result.total_cost == 1.5


def test_overview_reads_instances_in_parallel():
    now = time.time()
    gateway = FakeGateway()
    gateway.set_history(
        "id-a", _payload([_attempt(1, _started(1, now), "complete", cost=1.0)])
    )
    gateway.set_history(
        "id-b", _payload([_attempt(2, _started(1, now), "complete", cost=2.0)])
    )
    gateway.set_history(
        "id-c", _payload([_attempt(3, _started(1, now), "complete", cost=3.0)])
    )
    gateway.history_delay = 0.3
    rows, now = make_rows(["a", "b", "c"], now)
    started = time.monotonic()
    result = collect(rows, gateway, now)
    elapsed = time.monotonic() - started
    # Sequential reads would take ~0.9 s; parallel reads share one delay.
    assert elapsed < 0.9
    assert sorted(gateway.history_calls) == ["id-a", "id-b", "id-c"]
    assert result.total_cost == 6.0


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


# --- order and legend ----------------------------------------------------------


def test_overview_order_follows_input_attention_rank():
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
    rows, _ = asyncio.run(state.snapshot(now=now))
    assert [row.name for row in rows] == ["unreach", "work", "idle"]
    for name in ("work", "idle", "unreach"):
        gateway.set_history(
            f"id-{name}",
            _payload([_attempt(1, _started(1, now), "complete", cost=1.0)]),
        )
    gateway.set_history_error("id-unreach", GatewayError("no socket"))
    result = asyncio.run(collect_overview(rows, gateway, spec=RangeSpec("all"), now=now))
    # Row order follows the input (attention-rank) order.
    assert [inst.name for inst in result.rows] == ["unreach", "work", "idle"]


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
    result = asyncio.run(collect_overview(rows, gateway, spec=RangeSpec("all"), now=now))
    assert [entry.key for entry in overview_legend(result)] == ["complete", "handoff"]
    assert set(OUTCOME_ORDER) >= {entry.key for entry in overview_legend(result)}
