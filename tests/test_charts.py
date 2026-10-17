"""Charts on the attempt history page: outcomes per day and attempts timeline.

Aggregation lives in ``agent_dashboard.charts`` and renders from the same
attempt summaries as the history table, so both charts respect the page's
time window filter. Vocabulary: Attempt log, Managed instance.
"""

from __future__ import annotations

import time

from agent_dashboard.charts import (
    CACHE_READ_SHARE_DEFINITION,
    cache_read_share,
    cache_share_per_attempt,
    cost_per_attempt,
    cost_soft_threshold,
    cost_totals,
    cumulative_cost_per_day,
    duration_vs_cost,
    format_tokens,
    missing_cost_count,
    missing_token_count,
    normalize_outcome,
    outcome_legend,
    outcomes_per_day,
    timeline_points,
    tokens_per_attempt,
)
from agent_dashboard.history import AttemptSummary


def _tokens_text(value):
    if value is None:
        return "n/a"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"{value:,}"


def _summary(
    issue=1,
    started_ts=None,
    outcome="complete",
    cost=None,
    duration=60.0,
    threshold=None,
    input_tokens=None,
    cache_read=None,
    cache_creation=None,
    output=None,
):
    started_at = "n/a"
    if started_ts is not None:
        started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started_ts))
    has_tokens = any(
        v is not None for v in (input_tokens, cache_read, cache_creation, output)
    )
    return AttemptSummary(
        issue=issue,
        started_at=started_at,
        started_ts=started_ts,
        duration_text="60s",
        duration_value=duration,
        outcome_text=outcome,
        cost_text="n/a" if cost is None else f"${cost:.2f}",
        cost_value=cost,
        cost_threshold_value=threshold,
        has_usage=(cost is not None or has_tokens),
        input_text=_tokens_text(input_tokens),
        cache_read_text=_tokens_text(cache_read),
        cache_creation_text=_tokens_text(cache_creation),
        output_text=_tokens_text(output),
        input_value=input_tokens,
        cache_read_value=cache_read,
        cache_creation_value=cache_creation,
        output_value=output,
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
    assert points[0].x_frac == 0.0
    assert points[1].x_frac == 1.0
    assert points[1].radius > points[0].radius


def test_timeline_single_attempt_is_centred():
    (point,) = timeline_points((_summary(1, _ts(0), "complete", cost=2.0),))
    assert point.x_frac == 0.5


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


# --- cost per attempt --------------------------------------------------------


def test_cost_per_attempt_excludes_no_usage_and_undated():
    summaries = (
        _summary(1, _ts(2), "complete", cost=1.0),
        _summary(2, _ts(1), "incomplete"),  # no usage data: excluded, never zero
        _summary(3, None, "complete", cost=5.0),  # undated: excluded
    )
    points = cost_per_attempt(summaries)
    assert [p.issue for p in points] == [1]
    assert points[0].cost_value == 1.0


def test_cost_per_attempt_orders_oldest_first():
    summaries = (
        _summary(2, _ts(1), "complete", cost=2.0),
        _summary(1, _ts(3), "handoff", cost=0.5),
    )
    points = cost_per_attempt(summaries)
    assert [p.issue for p in points] == [1, 2]
    assert points[0].x_frac == 0.0
    assert points[1].x_frac == 1.0


def test_cost_per_attempt_single_is_centred():
    (point,) = cost_per_attempt((_summary(1, _ts(0), "complete", cost=2.0),))
    assert point.x_frac == 0.5


def test_cost_per_attempt_empty_without_costs():
    assert cost_per_attempt(()) == ()
    assert cost_per_attempt((_summary(1, _ts(0), "complete"),)) == ()
    assert cost_per_attempt((_summary(1, None, "complete", cost=1.0),)) == ()


def test_cost_per_attempt_tooltip_carries_every_field():
    (point,) = cost_per_attempt((_summary(7, _ts(0), "incomplete", cost=1.23),))
    assert "#7" in point.tooltip
    assert "60s" in point.tooltip
    assert "incomplete" in point.tooltip
    assert "$1.23" in point.tooltip


def test_cost_per_attempt_shape_follows_outcome():
    points = cost_per_attempt(
        (
            _summary(1, _ts(1), "complete", cost=1.0),
            _summary(2, _ts(0), "incomplete", cost=1.0),
        )
    )
    assert {p.shape for p in points} == {"circle", "square"}


# --- cumulative cost ---------------------------------------------------------


def test_cumulative_sums_daily_and_carries_gaps_forward():
    summaries = (
        _summary(1, _ts(2, 10), "complete", cost=1.0),
        _summary(2, _ts(2, 15), "incomplete", cost=2.0),
        _summary(3, _ts(0, 9), "complete", cost=4.0),
        _summary(4, _ts(1, 9), "complete"),  # no usage: excluded, never zero
    )
    days = cumulative_cost_per_day(summaries, now=NOW, window="all")
    assert len(days) == 3
    assert [d.day for d in days] == sorted(d.day for d in days)
    assert days[0].daily == 3.0
    assert days[0].cumulative == 3.0
    assert days[1].daily == 0.0
    assert days[1].cumulative == 3.0
    assert days[2].daily == 4.0
    assert days[2].cumulative == 7.0
    assert days[2].cumulative_text == "$7.00"


def test_cumulative_empty_without_cost_data():
    assert cumulative_cost_per_day((), now=NOW, window="all") == []
    assert (
        cumulative_cost_per_day((_summary(1, _ts(0), "complete"),), now=NOW, window="all")
        == []
    )
    assert cumulative_cost_per_day((_summary(1, None, "complete"),), now=NOW) == []


def test_cumulative_window_spans_trailing_days():
    summaries = (_summary(1, _ts(1), "complete", cost=2.5),)
    days = cumulative_cost_per_day(summaries, now=NOW, window="7")
    assert len(days) == 8  # start day through today, inclusive
    assert days[-1].cumulative == 2.5


# --- totals and missing-cost count -------------------------------------------


def test_cost_totals_sum_only_attempts_with_cost():
    summaries = (
        _summary(1, _ts(1), "complete", cost=1.0),
        _summary(2, _ts(0), "complete", cost=2.5),
        _summary(3, _ts(0), "incomplete"),
    )
    totals = cost_totals(summaries)
    assert totals.total == 3.5
    assert totals.with_cost == 2
    assert totals.missing == 1


def test_cost_totals_empty_is_zero():
    totals = cost_totals(())
    assert totals.total == 0.0
    assert totals.with_cost == 0
    assert totals.missing == 0


def test_missing_cost_count_never_plots_zero():
    summaries = (
        _summary(1, _ts(1), "complete", cost=0.0),  # measured zero stays a value
        _summary(2, _ts(0), "complete"),
    )
    assert missing_cost_count(summaries) == 1


# --- soft threshold ----------------------------------------------------------


def test_threshold_none_when_nothing_reports_it():
    summaries = (
        _summary(1, _ts(1), "complete", cost=1.0),
        _summary(2, _ts(0), "complete", cost=2.0),
    )
    assert cost_soft_threshold(summaries) is None
    assert cost_soft_threshold(()) is None


def test_threshold_newest_reporting_attempt_wins():
    summaries = (
        _summary(1, _ts(0), "complete", cost=1.0, threshold=5.0),
        _summary(2, _ts(2), "complete", cost=1.0, threshold=9.5),
        _summary(3, _ts(1), "complete", cost=1.0),
    )
    assert cost_soft_threshold(summaries) == 5.0


def test_threshold_undated_counts_only_as_fallback():
    assert cost_soft_threshold((_summary(1, None, "complete", threshold=4.0),)) == 4.0


# --- duration vs cost --------------------------------------------------------


def test_duration_vs_cost_needs_both_values():
    summaries = (
        _summary(1, _ts(2), "complete", cost=1.0, duration=60.0),
        _summary(2, _ts(1), "incomplete", cost=None, duration=30.0),
        _summary(3, _ts(0), "complete", cost=2.0, duration=None),
    )
    points = duration_vs_cost(summaries)
    assert [p.issue for p in points] == [1]


def test_duration_vs_cost_excludes_undated():
    points = duration_vs_cost(
        (
            _summary(1, None, "complete", cost=1.0, duration=60.0),
            _summary(2, _ts(0), "complete", cost=1.0, duration=60.0),
        )
    )
    assert [p.issue for p in points] == [2]


def test_duration_vs_cost_scales_linearly():
    points = duration_vs_cost(
        (
            _summary(1, _ts(1), "complete", cost=1.0, duration=30.0),
            _summary(2, _ts(0), "incomplete", cost=2.0, duration=60.0),
        )
    )
    by_issue = {p.issue: p for p in points}
    assert by_issue[2].x_frac == 1.0
    assert by_issue[1].x_frac == 0.5
    assert by_issue[2].y_frac == 1.0
    assert by_issue[1].y_frac == 0.5


def test_duration_vs_cost_single_marks_centre_on_degenerate_axes():
    (point,) = duration_vs_cost((_summary(1, _ts(0), "complete", cost=0.0, duration=0.0),))
    assert point.x_frac == 0.5
    assert point.y_frac == 0.5


def test_duration_vs_cost_tooltip_carries_every_field():
    (point,) = duration_vs_cost((_summary(9, _ts(0), "handoff", cost=3.0, duration=90.0),))
    assert "#9" in point.tooltip
    assert "handoff" in point.tooltip
    assert "$3.00" in point.tooltip


def test_duration_vs_cost_marks_coloured_by_outcome():
    points = duration_vs_cost(
        (
            _summary(1, _ts(1), "complete", cost=1.0, duration=10.0),
            _summary(2, _ts(0), "incomplete", cost=1.0, duration=20.0),
        )
    )
    assert {p.outcome_key for p in points} == {"complete", "incomplete"}
    assert {p.shape for p in points} == {"circle", "square"}


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


# --- cost charts on the history page -----------------------------------------


def _cost_page_payload(with_threshold=True):
    import time as _time

    now = _time.time()
    recent = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(now - 86400))
    older = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(now - 2 * 86400))
    mid = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(now - 3 * 86400))
    old = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(now - 40 * 86400))
    recent_attempt = {
        "outcome": "complete",
        "started_at": recent,
        "duration_seconds": 120,
        "model_usage": {"m": {"input_tokens": 10, "cost_usd": 1.50}},
        "model_stop_reason": "end_turn",
    }
    if with_threshold:
        recent_attempt["cost_soft_threshold_usd"] = 2.0
    older_attempt = {
        "outcome": "incomplete",
        "started_at": older,
        "duration_seconds": 3600,
        "model_usage": {"m": {"input_tokens": 20, "cost_usd": 0.50}},
    }
    stale_attempt = {
        "outcome": "handoff",
        "started_at": old,
        "duration_seconds": 60,
        "model_usage": {"m": {"input_tokens": 5, "cost_usd": 4.00}},
    }
    unmeasured_attempt = {
        "outcome": "complete",
        "started_at": mid,
        "duration_seconds": 30,
    }
    return {
        "layout": "attempt-logs/v1",
        "attempts": [
            {
                "issue": 98,
                "started": recent.replace(":", "-"),
                "attempt": recent_attempt,
            },
            {
                "issue": 41,
                "started": older.replace(":", "-"),
                "attempt": older_attempt,
            },
            {
                "issue": 9,
                "started": mid.replace(":", "-"),
                "attempt": unmeasured_attempt,
            },
            {
                "issue": 7,
                "started": old.replace(":", "-"),
                "attempt": stale_attempt,
            },
        ],
        "active_started_at": None,
        "unreadable": 0,
    }


def test_history_page_renders_cost_charts_with_totals():
    from test_history import make_history_client

    client, _, _ = make_history_client(
        [("work", "running")], {"id-work": _cost_page_payload()}
    )
    body = client.get("/instances/work/history?window=all").text
    assert "Cost per attempt" in body
    assert "Cumulative cost" in body
    assert "Duration vs cost" in body
    # Totals count the attempt without usage data instead of plotting zero.
    assert "$6.00" in body  # 1.50 + 0.50 + 4.00 (stale issue #7)
    assert "without usage data" in body
    assert "excluded from cost totals" in body


def test_history_page_threshold_line_only_when_known():
    from test_history import make_history_client

    client, _, _ = make_history_client(
        [("work", "running")], {"id-work": _cost_page_payload(with_threshold=True)}
    )
    body = client.get("/instances/work/history?window=all").text
    assert "soft threshold" in body
    assert 'class="threshold"' in body

    client, _, _ = make_history_client(
        [("work", "running")], {"id-work": _cost_page_payload(with_threshold=False)}
    )
    body = client.get("/instances/work/history?window=all").text
    assert 'class="threshold"' not in body


def test_history_page_cost_charts_respect_window_filter():
    from test_history import make_history_client

    client, _, _ = make_history_client(
        [("work", "running")], {"id-work": _cost_page_payload()}
    )
    week = client.get("/instances/work/history?window=7").text
    # Issue #7 is 40 days old with $4.00 of cost: outside the window, its
    # cost drops out of the total ($6.00 -> $2.00).
    assert "Issue #7" not in week
    assert "$2.00" in week
    assert "$6.00" not in week
    everything = client.get("/instances/work/history?window=all").text
    assert "Issue #7" in everything
    assert "$6.00" in everything


def test_history_page_cost_marks_show_issue_outcome_duration_and_cost():
    from test_history import make_history_client

    client, _, _ = make_history_client(
        [("work", "running")], {"id-work": _cost_page_payload()}
    )
    body = client.get("/instances/work/history?window=all").text
    assert "Issue #98" in body
    assert "incomplete" in body
    assert "$1.50" in body


def test_history_page_no_cost_data_renders_notes_not_zeros():
    import time as _time

    from test_history import make_history_client

    now = _time.time()
    recent = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(now - 86400))
    payload = {
        "layout": "attempt-logs/v1",
        "attempts": [
            {
                "issue": 5,
                "started": recent.replace(":", "-"),
                "attempt": {"outcome": "incomplete", "started_at": recent},
            }
        ],
        "active_started_at": None,
        "unreadable": 0,
    }
    client, _, _ = make_history_client([("work", "running")], {"id-work": payload})
    body = client.get("/instances/work/history?window=all").text
    assert body.count("No cost data in this window.") == 3
    assert "without usage data" in body
    assert "$0.00" not in body


def test_history_page_zero_cost_with_usage_still_charts():
    import time as _time

    from test_history import make_history_client

    now = _time.time()
    recent = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(now - 86400))
    payload = {
        "layout": "attempt-logs/v1",
        "attempts": [
            {
                "issue": 5,
                "started": recent.replace(":", "-"),
                "attempt": {
                    "outcome": "complete",
                    "started_at": recent,
                    "duration_seconds": 5,
                    "model_usage": {"m": {"input_tokens": 10, "cost_usd": 0}},
                },
            }
        ],
        "active_started_at": None,
        "unreadable": 0,
    }
    client, _, _ = make_history_client([("work", "running")], {"id-work": payload})
    body = client.get("/instances/work/history?window=all").text
    # A measured zero is usage data: it charts instead of counting as missing.
    assert "No cost data in this window." not in body
    assert "without usage data" not in body


# --- cache read share --------------------------------------------------------


def test_share_is_cache_read_over_input_side_tokens():
    summary = _summary(
        1, _ts(0), "complete", input_tokens=100, cache_read=300, cache_creation=100
    )
    assert cache_read_share(summary) == 300 / 500


def test_share_ignores_output_tokens():
    summary = _summary(
        1,
        _ts(0),
        "complete",
        input_tokens=100,
        cache_read=300,
        cache_creation=100,
        output=99999,
    )
    assert cache_read_share(summary) == 300 / 500


def test_share_treats_missing_categories_as_zero():
    summary = _summary(1, _ts(0), "complete", input_tokens=100, cache_read=300)
    assert cache_read_share(summary) == 300 / 400


def test_share_missing_cache_read_is_zero_when_input_side_known():
    summary = _summary(1, _ts(0), "complete", input_tokens=100)
    assert cache_read_share(summary) == 0.0


def test_share_missing_usage_is_undefined():
    assert cache_read_share(_summary(1, _ts(0), "complete")) is None


def test_share_cost_only_attempt_is_undefined():
    # Cost without token counters is not token data: no share to plot.
    assert cache_read_share(_summary(1, _ts(0), "complete", cost=1.0)) is None


def test_share_zero_input_side_is_undefined_never_zero():
    summary = _summary(
        1, _ts(0), "complete", input_tokens=0, cache_read=0, cache_creation=0
    )
    assert cache_read_share(summary) is None


def test_share_output_only_is_undefined():
    summary = _summary(1, _ts(0), "complete", output=50)
    assert cache_read_share(summary) is None


def test_share_definition_is_visible_on_the_page():
    from test_history import make_history_client

    client, _, _ = make_history_client(
        [("work", "running")], {"id-work": _token_page_payload()}
    )
    body = client.get("/instances/work/history?window=all").text
    assert CACHE_READ_SHARE_DEFINITION in body


def test_format_tokens_uses_thousands_separators():
    assert format_tokens(120000) == "120,000"
    assert format_tokens(0) == "0"


# --- missing token count -----------------------------------------------------


def test_missing_token_count_never_plots_zero():
    summaries = (
        _summary(1, _ts(1), "complete", input_tokens=0),  # measured zero stays data
        _summary(2, _ts(0), "complete", cost=1.0),  # cost-only: no token data
        _summary(3, _ts(0), "incomplete"),  # nothing at all
    )
    assert missing_token_count(summaries) == 2


def test_missing_token_count_empty_is_zero():
    assert missing_token_count(()) == 0


# --- tokens per attempt ------------------------------------------------------


def test_tokens_per_attempt_excludes_missing_and_undated():
    summaries = (
        _summary(1, _ts(2), "complete", input_tokens=100, cache_read=300),
        _summary(2, _ts(1), "incomplete"),  # no usage data: excluded, never zero
        _summary(3, None, "complete", input_tokens=50),  # undated: excluded
    )
    points = tokens_per_attempt(summaries)
    assert [p.issue for p in points] == [1]
    assert points[0].total == 400


def test_tokens_per_attempt_orders_oldest_first():
    summaries = (
        _summary(2, _ts(1), "complete", input_tokens=200),
        _summary(1, _ts(3), "handoff", input_tokens=50, output=10),
    )
    points = tokens_per_attempt(summaries)
    assert [p.issue for p in points] == [1, 2]
    assert points[0].x_frac == 0.0
    assert points[1].x_frac == 1.0


def test_tokens_per_attempt_single_is_centred():
    (point,) = tokens_per_attempt((_summary(1, _ts(0), "complete", input_tokens=10),))
    assert point.x_frac == 0.5


def test_tokens_per_attempt_empty_without_token_data():
    assert tokens_per_attempt(()) == ()
    assert tokens_per_attempt((_summary(1, _ts(0), "complete"),)) == ()
    assert tokens_per_attempt((_summary(1, _ts(0), "complete", cost=2.0),)) == ()
    assert tokens_per_attempt((_summary(1, None, "complete", input_tokens=5),)) == ()


def test_tokens_per_attempt_keeps_measured_zero():
    (point,) = tokens_per_attempt(
        (_summary(1, _ts(0), "complete", input_tokens=0, cache_read=0),)
    )
    assert point.total == 0


def test_tokens_per_attempt_sums_categories():
    (point,) = tokens_per_attempt(
        (
            _summary(
                1,
                _ts(0),
                "complete",
                input_tokens=100,
                cache_read=300,
                cache_creation=50,
                output=25,
            ),
        )
    )
    assert point.input_value == 100
    assert point.cache_read_value == 300
    assert point.cache_creation_value == 50
    assert point.output_value == 25
    assert point.total == 475


def test_tokens_per_attempt_tooltip_carries_issue_outcome_and_breakdown():
    (point,) = tokens_per_attempt(
        (
            _summary(
                7,
                _ts(0),
                "incomplete",
                input_tokens=100,
                cache_read=300,
                cache_creation=50,
                output=25,
            ),
        )
    )
    assert "#7" in point.tooltip
    assert "incomplete" in point.tooltip
    assert "100" in point.tooltip
    assert "300" in point.tooltip
    assert "Cache read share" in point.tooltip


def test_tokens_per_attempt_shape_follows_outcome():
    points = tokens_per_attempt(
        (
            _summary(1, _ts(1), "complete", input_tokens=10),
            _summary(2, _ts(0), "incomplete", input_tokens=10),
        )
    )
    assert {p.shape for p in points} == {"circle", "square"}


# --- cache share per attempt -------------------------------------------------


def test_share_per_attempt_excludes_undefined_and_undated():
    summaries = (
        _summary(1, _ts(2), "complete", input_tokens=100, cache_read=300),
        _summary(2, _ts(1), "incomplete"),  # missing usage: excluded
        _summary(3, _ts(0), "complete", input_tokens=0),  # zero input-side: excluded
        _summary(4, None, "complete", input_tokens=100, cache_read=50),  # undated
    )
    points = cache_share_per_attempt(summaries)
    assert [p.issue for p in points] == [1]
    assert points[0].share == 300 / 400


def test_share_per_attempt_orders_oldest_first():
    summaries = (
        _summary(2, _ts(1), "complete", input_tokens=100, cache_read=100),
        _summary(1, _ts(3), "handoff", input_tokens=100, cache_read=300),
    )
    points = cache_share_per_attempt(summaries)
    assert [p.issue for p in points] == [1, 2]
    assert points[0].x_frac == 0.0
    assert points[1].x_frac == 1.0
    assert points[0].share == 0.75
    assert points[1].share == 0.5


def test_share_per_attempt_single_is_centred():
    (point,) = cache_share_per_attempt(
        (_summary(1, _ts(0), "complete", input_tokens=100, cache_read=25),)
    )
    assert point.x_frac == 0.5
    assert point.share == 0.2
    assert point.share_text == "20%"


def test_share_per_attempt_empty_without_defined_shares():
    assert cache_share_per_attempt(()) == ()
    assert cache_share_per_attempt((_summary(1, _ts(0), "complete"),)) == ()


def test_share_per_attempt_tooltip_carries_issue_outcome_and_breakdown():
    (point,) = cache_share_per_attempt(
        (_summary(9, _ts(0), "handoff", input_tokens=100, cache_read=300),)
    )
    assert "#9" in point.tooltip
    assert "handoff" in point.tooltip
    assert "300" in point.tooltip
    assert "75%" in point.tooltip


def test_share_per_attempt_shape_follows_outcome():
    points = cache_share_per_attempt(
        (
            _summary(1, _ts(1), "complete", input_tokens=10, cache_read=5),
            _summary(2, _ts(0), "incomplete", input_tokens=10, cache_read=5),
        )
    )
    assert {p.shape for p in points} == {"circle", "square"}


# --- token charts on the history page ----------------------------------------


def _token_page_payload():
    import time as _time

    now = _time.time()
    recent = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(now - 86400))
    older = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(now - 2 * 86400))
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
                    "duration_seconds": 120,
                    "model_usage": {
                        "m": {
                            "input_tokens": 100,
                            "cache_read_input_tokens": 300,
                            "cache_creation_input_tokens": 100,
                            "output_tokens": 50,
                        }
                    },
                    "model_stop_reason": "end_turn",
                },
            },
            {
                "issue": 41,
                "started": older.replace(":", "-"),
                "attempt": {
                    "outcome": "incomplete",
                    "started_at": older,
                    "duration_seconds": 60,
                    "model_usage": {
                        "m": {
                            "input_tokens": 200,
                            "cache_read_input_tokens": 0,
                            "output_tokens": 10,
                        }
                    },
                },
            },
            {
                "issue": 9,
                "started": older.replace(":", "-"),
                "attempt": {"outcome": "handoff", "started_at": older},
            },
            {
                "issue": 7,
                "started": old.replace(":", "-"),
                "attempt": {
                    "outcome": "complete",
                    "started_at": old,
                    "duration_seconds": 30,
                    "model_usage": {"m": {"input_tokens": 10}},
                },
            },
        ],
        "active_started_at": None,
        "unreadable": 0,
    }


def test_history_page_renders_token_charts_with_counts():
    from test_history import make_history_client

    client, _, _ = make_history_client(
        [("work", "running")], {"id-work": _token_page_payload()}
    )
    body = client.get("/instances/work/history?window=all").text
    assert "Tokens per attempt" in body
    assert "Cache read share" in body
    assert CACHE_READ_SHARE_DEFINITION in body
    # Issue #9 has no usage data: excluded from the token charts and counted.
    assert "without usage data" in body
    assert "not charted" in body
    # Stacked segments and the share axis render.
    assert "tok-input" in body
    assert "tok-cache-read" in body
    assert "100%" in body


def test_history_page_token_charts_respect_window_filter():
    from test_history import make_history_client

    client, _, _ = make_history_client(
        [("work", "running")], {"id-work": _token_page_payload()}
    )
    week = client.get("/instances/work/history?window=7").text
    # Issue #7 is 40 days old: outside the window its tokens drop out.
    assert "Issue #98" in week
    assert "Issue #7" not in week
    everything = client.get("/instances/work/history?window=all").text
    assert "Issue #7" in everything


def test_history_page_token_marks_show_issue_outcome_and_breakdown():
    from test_history import make_history_client

    client, _, _ = make_history_client(
        [("work", "running")], {"id-work": _token_page_payload()}
    )
    body = client.get("/instances/work/history?window=all").text
    assert "Issue #98" in body
    assert "complete" in body
    assert "Cache read share" in body
    assert "60%" in body  # 300 / (100 + 300 + 100)


def test_history_page_no_token_data_renders_notes_not_zeros():
    import time as _time

    from test_history import make_history_client

    now = _time.time()
    recent = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(now - 86400))
    payload = {
        "layout": "attempt-logs/v1",
        "attempts": [
            {
                "issue": 5,
                "started": recent.replace(":", "-"),
                "attempt": {"outcome": "incomplete", "started_at": recent},
            }
        ],
        "active_started_at": None,
        "unreadable": 0,
    }
    client, _, _ = make_history_client([("work", "running")], {"id-work": payload})
    body = client.get("/instances/work/history?window=all").text
    assert "No token data in this window." in body
    assert "No cache share data in this window." in body
    assert "without usage data" in body
    assert "token-legend" not in body


def test_history_page_zero_tokens_with_usage_still_charts():
    import time as _time

    from test_history import make_history_client

    now = _time.time()
    recent = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(now - 86400))
    payload = {
        "layout": "attempt-logs/v1",
        "attempts": [
            {
                "issue": 5,
                "started": recent.replace(":", "-"),
                "attempt": {
                    "outcome": "complete",
                    "started_at": recent,
                    "duration_seconds": 5,
                    "model_usage": {"m": {"input_tokens": 0}},
                },
            }
        ],
        "active_started_at": None,
        "unreadable": 0,
    }
    client, _, _ = make_history_client([("work", "running")], {"id-work": payload})
    body = client.get("/instances/work/history?window=all").text
    # A measured zero is usage data: the stacked chart renders a baseline
    # tick instead of counting the attempt as missing, while the share
    # chart (whose denominator is zero) renders its own note. The cost
    # section still reports its own missing-cost note; only the token
    # "not charted" note must stay absent.
    assert "No token data in this window." not in body
    assert "No cache share data in this window." in body
    assert "not charted" not in body
