"""Drawer Cost section: totals, average, extremes and empty states.

The Cost section lives in the attempt-statistics fragment, so one range
selector drives both: a preset or strip selection re-renders the card and the
section together from the same cut. Vocabulary: Attempt log, Managed
instance.
"""

from __future__ import annotations

import time

from test_drawer_stats import history_with, make_stats_client


def test_cost_section_shows_total_average_extremes_and_notes():
    now = time.time()
    client, _, _, _ = make_stats_client(
        {"work": "running"},
        {
            "id-work": history_with(
                now,
                (1, 1, "complete", 1.0),
                (2, 2, "complete", 3.0),
                (3, 3, "incomplete", None),
            )
        },
    )
    body = client.get("/instances/work/stats?preset=all").text
    # Section head carries the shared range label.
    assert "Cost" in body
    assert "all time" in body
    # Total across the two attempts with usage data; the third is excluded,
    # never counted as zero.
    assert "$4.00" in body
    assert "2 attempt(s) with usage data" in body
    assert "1 without usage data excluded" in body
    assert "$0.00" not in body
    # Average counts only attempts with usage data: 4.00 / 2.
    assert "$2.00 per attempt with usage data" in body
    # Most expensive attempt links to its Attempt log.
    assert "Most expensive" in body
    assert "/instances/work/attempts/2/" in body
    assert "$3.00" in body
    # Last finished attempt: the most recent finished row with its cost.
    assert "Last finished" in body
    # The current attempt has no cost yet.
    assert "The current attempt's cost appears once it finishes." in body


def test_cost_range_change_updates_card_and_section_together():
    now = time.time()
    client, _, _, _ = make_stats_client(
        {"work": "running"},
        {
            "id-work": history_with(
                now,
                (1, 1, "complete", 1.0),
                (2, 40, "complete", 8.0),
            )
        },
    )
    narrow = client.get("/instances/work/stats?preset=7d").text
    # Both the card and the Cost section follow the 7-day cut.
    assert "last 7 days" in narrow
    assert "$1.00" in narrow
    assert "$8.00" not in narrow
    wide = client.get("/instances/work/stats?preset=all").text
    assert "all time" in wide
    assert "$9.00" in wide
    # The total matches the history page for the same range.
    history = client.get("/instances/work/history?preset=all").text
    assert "$9.00" in history
    history_narrow = client.get("/instances/work/history?preset=7d").text
    assert "$1.00" in history_narrow


def test_cost_strip_selection_updates_section():
    now = time.time()
    day = time.strftime("%Y-%m-%d", time.gmtime(now - 5 * 86400))
    client, _, _, _ = make_stats_client(
        {"work": "running"},
        {
            "id-work": history_with(
                now, (1, 5, "complete", 2.5), (2, 6, "complete", 9.0)
            )
        },
    )
    body = client.get(f"/instances/work/stats?from={day}&to={day}").text
    assert "$2.50" in body
    assert "$9.00" not in body
    assert "Most expensive" in body
    assert "/instances/work/attempts/1/" in body


def test_cost_last_finished_ignores_range():
    now = time.time()
    client, _, _, _ = make_stats_client(
        {"work": "running"},
        {
            "id-work": history_with(
                now,
                (1, 1, "complete", 1.0),
                (2, 100, "complete", 7.0),
            )
        },
    )
    body = client.get("/instances/work/stats?preset=7d").text
    # The range total covers only the recent attempt, but the last finished
    # row still names the most recent finished attempt overall (#1 here is
    # newer than the out-of-range #2).
    assert "$1.00" in body
    assert "Last finished" in body
    assert "/instances/work/attempts/1/" in body


def test_cost_no_attempts_yet_renders_words_not_zeros():
    client, _, _, _ = make_stats_client({"work": "running"}, {"id-work": history_with(time.time())})
    body = client.get("/instances/work/stats?preset=all").text
    assert "No attempts recorded yet." in body
    assert "$0.00" not in body
    assert "The current attempt's cost appears once it finishes." in body


def test_cost_no_attempts_in_range_renders_words_not_zeros():
    now = time.time()
    client, _, _, _ = make_stats_client(
        {"work": "running"},
        {"id-work": history_with(now, (1, 100, "complete", 5.0))},
    )
    body = client.get("/instances/work/stats?preset=7d").text
    assert "No attempts in this window." in body
    assert "$0.00" not in body
    # No range total line; the out-of-range attempt only appears as the
    # range-independent last finished row.
    assert "with usage data" not in body
    assert "Last finished" in body


def test_cost_without_usage_data_renders_words_not_zeros():
    now = time.time()
    client, _, _, _ = make_stats_client(
        {"work": "running"},
        {"id-work": history_with(now, (1, 1, "incomplete", None))},
    )
    body = client.get("/instances/work/stats?preset=all").text
    assert "No cost data in this window" in body
    assert "1 attempt(s) without usage data" in body
    assert "$0.00" not in body


def test_cost_unknown_instance_renders_same_hint_as_card():
    client, _, _, _ = make_stats_client({"work": "running"})
    body = client.get("/instances/nope/stats").text
    assert "Unknown Managed instance" in body
    assert body.count("Unknown Managed instance") >= 2
    assert "$0.00" not in body
    assert "$" not in body


def test_cost_unreadable_summary_renders_same_hint():
    client, _, _, _ = make_stats_client(
        {"work": "running"},
        {"id-work": {"layout": "unknown", "attempts": []}},
    )
    body = client.get("/instances/work/stats").text
    assert "layout not recognised" in body
    assert body.count("layout not recognised") >= 2
    assert "$0.00" not in body


def test_cost_stopped_instance_reads_via_copy():
    import json as _json

    from fake_gateway import make_logs_tree

    started_dash = "2026-09-27T16-33-43.385090Z"
    started_colon = "2026-09-27T16:33:43.385090Z"
    attempt = {
        "outcome": "complete",
        "started_at": started_colon,
        "duration_seconds": 60,
        "model_usage": {"m": {"inputTokens": 10, "costUSD": 1.5}},
        "token_budget": {"total_cost_usd": 1.5},
        "model_stop_reason": "end_turn",
    }
    tree = make_logs_tree(
        {f"logs/1/{started_dash}/attempt.json": _json.dumps(attempt).encode()}
    )
    client, _, _, _ = make_stats_client(
        {"down": "not running"}, logs_trees={"id-down": tree}
    )
    body = client.get("/instances/down/stats?preset=all").text
    assert "$1.50" in body
    assert "Most expensive" in body
    assert "/instances/down/attempts/1/" in body


def test_cost_stopped_instance_without_tree_renders_same_hint():
    client, _, _, _ = make_stats_client({"down": "not running"})
    body = client.get("/instances/down/stats").text
    assert "its volume is gone or it holds no logs" in body
    assert body.count("its volume is gone or it holds no logs") >= 2
    assert "$0.00" not in body


def test_cost_section_loads_with_card_slot_once():
    now = time.time()
    client, _, gateway, _ = make_stats_client(
        {"work": "running"},
        {"id-work": history_with(now, (1, 1, "complete", 1.0))},
    )
    drawer = client.get("/instances/work").text
    assert 'id="stats-work"' in drawer
    assert gateway.history_calls == []
    card = client.get("/instances/work/stats").text
    assert "Cost" in card
    assert gateway.history_calls == ["id-work"]
    # Drawer live refreshes never re-read attempt summaries.
    client.get("/instances/work")
    assert gateway.history_calls == ["id-work"]
