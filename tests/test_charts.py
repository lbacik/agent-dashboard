"""Charts on the attempt history page: outcomes per day and attempts timeline.

Aggregation lives in ``agent_dashboard.charts`` and renders from the same
attempt summaries as the history table, so both charts respect the page's
time window filter. Vocabulary: Attempt log, Managed instance.
"""

from __future__ import annotations

import time

from agent_dashboard.charts import (
    normalize_outcome,
    outcome_legend,
    outcomes_per_day,
    timeline_points,
)
from agent_dashboard.history import AttemptSummary


def _summary(issue=1, started_ts=None, outcome="complete", cost=None):
    started_at = "n/a"
    if started_ts is not None:
        started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started_ts))
    return AttemptSummary(
        issue=issue,
        started_at=started_at,
        started_ts=started_ts,
        duration_text="60s",
        outcome_text=outcome,
        cost_text="n/a" if cost is None else f"${cost:.2f}",
        cost_value=cost,
        has_usage=cost is not None,
    )


NOW = 1_790_000_000.0  # 2026-09-22 ish, fixed for day bucketing
DAY = 86400.0


def _ts(days_ago, hour=12):
    base = NOW - days_ago * DAY
    midnight = base - (base % DAY)
    return midnight + hour * 3600


# --- outcome normalisation -------------------------------------------------


def test_normalize_outcome_covers_ticket_outcomes():
    assert normalize_outcome("complete") == "complete"
    assert normalize_outcome("incomplete") == "incomplete"
    assert normalize_outcome("handoff") == "handoff"
    assert normalize_outcome("infrastructure_error") == "infrastructure_error"
    assert normalize_outcome("infrastructure error") == "infrastructure_error"
    assert normalize_outcome("no_changes") == "no_changes"
    assert normalize_outcome("no changes") == "no_changes"
    assert normalize_outcome("No-Changes") == "no_changes"


def test_normalize_outcome_unknown_is_other():
    assert normalize_outcome("active") == "other"
    assert normalize_outcome("interrupted") == "other"
    assert normalize_outcome("") == "other"
    assert normalize_outcome("something new") == "other"


# --- outcomes per day ------------------------------------------------------


def test_buckets_group_per_day_with_outcome_counts():
    summaries = (
        _summary(1, _ts(1, 10), "complete"),
        _summary(2, _ts(1, 15), "incomplete"),
        _summary(3, _ts(2, 9), "complete"),
    )
    buckets = outcomes_per_day(summaries, now=NOW, window="all")
    assert [b.day for b in buckets] == sorted(b.day for b in buckets)
    by_day = {b.day: b for b in buckets}
    day1 = time.strftime("%Y-%m-%d", time.gmtime(_ts(1)))
    day2 = time.strftime("%Y-%m-%d", time.gmtime(_ts(2)))
    assert by_day[day1].counts["complete"] == 1
    assert by_day[day1].counts["incomplete"] == 1
    assert by_day[day1].total == 2
    assert by_day[day2].counts["complete"] == 1
    assert by_day[day2].total == 1


def test_buckets_fill_gap_days_in_range():
    summaries = (_summary(1, _ts(0), "complete"), _summary(2, _ts(2), "handoff"))
    buckets = outcomes_per_day(summaries, now=NOW, window="all")
    assert len(buckets) == 3
    assert buckets[1].total == 0
    assert buckets[1].counts == {}


def test_buckets_window_spans_trailing_days():
    summaries = (_summary(1, _ts(1), "complete"),)
    buckets = outcomes_per_day(summaries, now=NOW, window="7")
    assert len(buckets) == 8  # start day through today, inclusive
    assert sum(b.total for b in buckets) == 1


def test_buckets_empty_when_no_dated_attempts():
    assert outcomes_per_day((), now=NOW, window="all") == []
    undated = (_summary(1, None, "complete"),)
    assert outcomes_per_day(undated, now=NOW, window="all") == []


def test_buckets_single_attempt_is_single_nonempty_day():
    (bucket,) = [
        b
        for b in outcomes_per_day(
            (_summary(1, _ts(0), "complete"),), now=NOW, window="all"
        )
        if b.total
    ]
    assert bucket.total == 1


def test_buckets_unknown_outcomes_count_as_other():
    summaries = (_summary(1, _ts(0), "active"), _summary(2, _ts(0), "interrupted"))
    (bucket,) = [
        b
        for b in outcomes_per_day(summaries, now=NOW, window="all") if b.total
    ]
    assert bucket.counts["other"] == 2


# --- timeline --------------------------------------------------------------


def test_timeline_orders_oldest_first_with_scaled_positions():
    summaries = (
        _summary(2, _ts(1), "complete", cost=1.0),
        _summary(1, _ts(3), "incomplete", cost=0.25),
    )
    points = timeline_points(summaries)
    assert [p.issue for p in points] == [1, 2]
    assert points[0].x01 == 0.0
    assert points[1].x01 == 1.0
    assert points[1].radius > points[0].radius


def test_timeline_single_attempt_is_centred():
    (point,) = timeline_points((_summary(1, _ts(0), "complete", cost=2.0),))
    assert point.x01 == 0.5


def test_timeline_missing_cost_uses_neutral_size():
    points = timeline_points(
        (
            _summary(1, _ts(2), "complete"),
            _summary(2, _ts(1), "handoff", cost=4.0),
            _summary(3, _ts(0), "infrastructure_error"),
        )
    )
    by_issue = {p.issue: p for p in points}
    assert by_issue[1].has_cost is False
    assert by_issue[3].has_cost is False
    assert by_issue[2].has_cost is True
    # Neutral marks share one size and never break scaling.
    assert by_issue[1].radius == by_issue[3].radius
    assert by_issue[1].radius > 0


def test_timeline_missing_cost_alone_renders():
    (point,) = timeline_points((_summary(1, _ts(0), "complete"),))
    assert point.radius > 0
    assert point.has_cost is False


def test_timeline_zero_cost_scales_not_na():
    points = timeline_points(
        (_summary(1, _ts(1), "complete", cost=0.0), _summary(2, _ts(0), "complete", cost=1.0))
    )
    assert all(p.has_cost for p in points)
    assert points[1].radius >= points[0].radius


def test_timeline_excludes_undated_attempts():
    points = timeline_points((_summary(1, None, "complete"), _summary(2, _ts(0), "complete")))
    assert [p.issue for p in points] == [2]


def test_timeline_empty_is_empty():
    assert timeline_points(()) == ()


def test_timeline_tooltip_carries_every_field():
    (point,) = timeline_points(
        (_summary(7, _ts(0), "complete", cost=1.23),)
    )
    tip = point.tooltip
    assert "#7" in tip
    assert point.started_at in tip
    assert "60s" in tip
    assert "complete" in tip
    assert "$1.23" in tip


def test_timeline_shape_differs_per_outcome():
    points = timeline_points(
        (
            _summary(1, _ts(4), "complete", cost=1.0),
            _summary(2, _ts(3), "incomplete", cost=1.0),
            _summary(3, _ts(2), "handoff", cost=1.0),
            _summary(4, _ts(1), "infrastructure_error", cost=1.0),
            _summary(5, _ts(0), "no_changes", cost=1.0),
        )
    )
    shapes = {p.shape for p in points}
    assert len(shapes) == 5


# --- history page ----------------------------------------------------------


def _page_payload():
    import time as _time

    now = _time.time()
    recent = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(now - 86400))
    old = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(now - 40 * 86400))
    return {
        "layout": "attempt-logs/v1",
        "attempts": [
            {
                "issue": 98,
                "started": recent.replace(":", "-"),
                "attempt": {
                    "outcome": "complete",
                    "started_at": recent,
                    "duration_seconds": 60,
                    "model_usage": {"m": {"input_tokens": 10, "cost_usd": 1.23}},
                    "model_stop_reason": "end_turn",
                },
            },
            {
                "issue": 41,
                "started": old.replace(":", "-"),
                "attempt": {
                    "outcome": "infrastructure_error",
                    "started_at": old,
                    "duration_seconds": 17,
                },
            },
        ],
        "active_started_at": None,
        "unreadable": 0,
    }


def test_history_page_renders_both_charts_with_legend():
    from test_history import make_history_client

    client, _, _ = make_history_client([("work", "running")], {"id-work": _page_payload()})
    body = client.get("/instances/work/history?window=all").text
    assert "Outcomes" in body
    assert "Attempts timeline" in body
    assert "chart-legend" in body
    assert "complete" in body
    assert "infrastructure error" in body
    assert "/static/charts.js" in body


def test_history_page_charts_respect_window_filter():
    from test_history import make_history_client

    client, _, _ = make_history_client([("work", "running")], {"id-work": _page_payload()})
    week = client.get("/instances/work/history?window=7").text
    assert "Issue #98" in week
    assert "Issue #41" not in week
    everything = client.get("/instances/work/history?window=all").text
    assert "Issue #41" in everything


def test_history_page_mark_tooltip_shows_every_field():
    from test_history import make_history_client

    client, _, _ = make_history_client([("work", "running")], {"id-work": _page_payload()})
    body = client.get("/instances/work/history?window=all").text
    assert "Duration" in body or "duration" in body.lower()
    assert "$1.23" in body
    # The no-usage attempt still has a mark with a neutral size.
    assert "no-cost" in body


def test_history_page_empty_window_renders_charts_sensibly():
    from test_history import make_history_client

    client, _, _ = make_history_client(
        [("work", "running")],
        {"id-work": {"layout": "attempt-logs/v1", "attempts": [], "unreadable": 0}},
    )
    body = client.get("/instances/work/history").text
    assert "No attempts" in body
    # Charts degrade to a note, never a broken SVG axis.
    assert "<svg" not in body or "No dated attempts" in body


def test_history_page_single_attempt_renders_both_charts():
    import time as _time

    from test_history import make_history_client

    now = _time.time()
    recent = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(now - 3600))
    payload = {
        "layout": "attempt-logs/v1",
        "attempts": [
            {
                "issue": 7,
                "started": recent.replace(":", "-"),
                "attempt": {"outcome": "complete", "started_at": recent},
            }
        ],
        "active_started_at": None,
        "unreadable": 0,
    }
    client, _, _ = make_history_client([("work", "running")], {"id-work": payload})
    body = client.get("/instances/work/history?window=all").text
    assert "Issue #7" in body
    assert "<svg" in body


# --- legend ----------------------------------------------------------------


def test_legend_lists_only_outcomes_present_in_stable_order():
    summaries = (
        _summary(1, _ts(1), "handoff"),
        _summary(2, _ts(0), "complete"),
        _summary(3, _ts(0), "complete"),
    )
    legend = outcome_legend(summaries)
    assert [entry.key for entry in legend] == ["complete", "handoff"]
    assert all(entry.label and entry.shape for entry in legend)
