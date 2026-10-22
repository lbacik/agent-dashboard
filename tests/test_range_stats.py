"""Drawer card range model: presets, custom spans, cutting and the day strip."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

from agent_dashboard.history import HISTORY_LAYOUT, AttemptSummary
from agent_dashboard.range_stats import (
    COOKIE_NAME,
    DEFAULT_PRESET,
    PRESETS,
    STRIP_DAYS,
    apply_range,
    day_strip,
    parse_cookie,
    parse_range,
)

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC).timestamp()


def _started(days_ago: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - days_ago * 86400))


def _summary(issue: int, days_ago: float | None, outcome="complete") -> AttemptSummary:
    started = _started(days_ago) if days_ago is not None else "n/a"
    ts = NOW - days_ago * 86400 if days_ago is not None else None
    return AttemptSummary(issue=issue, started_at=started, started_ts=ts, outcome_text=outcome)


def _result(*rows: AttemptSummary):
    from agent_dashboard.history import HistoryResult

    return HistoryResult(ok=True, summaries=tuple(rows), total=len(rows))


# --- parsing ---------------------------------------------------------------


def test_parse_range_defaults_to_30d():
    assert parse_range({}).preset == DEFAULT_PRESET == "30d"
    assert parse_range({"preset": "bogus"}).preset == "30d"
    assert parse_range({"preset": "7d"}).preset == "7d"


def test_parse_range_presets_in_order():
    assert PRESETS == ("24h", "7d", "30d", "90d", "all")


def test_parse_range_dates_win_over_preset_and_swap_when_reversed():
    spec = parse_range({"preset": "7d", "from": "2026-10-20", "to": "2026-10-10"})
    assert spec.is_custom
    assert spec.start.isoformat() == "2026-10-10"
    assert spec.end.isoformat() == "2026-10-20"


def test_parse_range_single_date_falls_back_to_preset():
    spec = parse_range({"from": "2026-10-10"})
    assert not spec.is_custom
    assert spec.preset == "30d"


def test_parse_range_invalid_dates_fall_back_to_preset():
    spec = parse_range({"from": "not-a-date", "to": "2026-10-10"})
    assert not spec.is_custom


def test_parse_cookie_round_trips_query():
    assert parse_cookie("preset=7d").preset == "7d"
    custom = parse_cookie("from=2026-10-10&to=2026-10-15")
    assert custom.is_custom
    assert parse_cookie("").preset == "30d"
    assert parse_cookie(None).preset == "30d"
    assert parse_cookie("preset=bogus").preset == "30d"
    assert COOKIE_NAME == "attempt_range"


def test_range_query_encoding():
    assert parse_range({"preset": "7d"}).query == "preset=7d"
    custom = parse_range({"from": "2026-10-10", "to": "2026-10-15"})
    assert custom.query == "from=2026-10-10&to=2026-10-15"


# --- labels ----------------------------------------------------------------


def test_preset_labels():
    assert parse_range({"preset": "7d"}).label == "last 7 days"
    assert parse_range({"preset": "24h"}).label == "last 24 hours"
    assert parse_range({"preset": "30d"}).label == "last 30 days"
    assert parse_range({"preset": "90d"}).label == "last 90 days"
    assert parse_range({"preset": "all"}).label == "all time"


def test_custom_labels():
    single = parse_range({"from": "2026-10-15", "to": "2026-10-15"})
    assert single.label == "15 Oct 2026"
    span = parse_range({"from": "2026-10-01", "to": "2026-10-15"})
    assert span.label == "1 Oct – 15 Oct 2026"
    cross_year = parse_range({"from": "2025-12-30", "to": "2026-01-02"})
    assert cross_year.label == "30 Dec 2025 – 2 Jan 2026"


def test_history_window_mapping():
    assert parse_range({"preset": "7d"}).history_window == "preset=7d"
    assert parse_range({"preset": "30d"}).history_window == "preset=30d"
    assert parse_range({"preset": "24h"}).history_window == "preset=24h"
    assert parse_range({"preset": "90d"}).history_window == "preset=90d"
    assert parse_range({"preset": "all"}).history_window == "preset=all"
    custom = parse_range({"from": "2026-10-01", "to": "2026-10-02"})
    assert custom.history_window == "from=2026-10-01&to=2026-10-02"
    assert custom.history_window == custom.query


# --- cutting ---------------------------------------------------------------


def test_apply_range_presets_cut_by_cutoff():
    result = _result(
        _summary(1, 0.5),
        _summary(2, 3),
        _summary(3, 10),
        _summary(4, 100),
    )
    def got(preset):
        return [
            s.issue
            for s in apply_range(result, parse_range({"preset": preset}), NOW).summaries
        ]
    assert got("all") == [1, 2, 3, 4]
    assert got("30d") == [1, 2, 3]
    assert got("7d") == [1, 2]
    assert got("24h") == [1]


def test_apply_range_keeps_full_total():
    result = _result(_summary(1, 0.5), _summary(2, 40))
    cut = apply_range(result, parse_range({"preset": "7d"}), NOW)
    assert len(cut.summaries) == 1
    assert cut.total == 2


def test_apply_range_custom_span_is_inclusive():
    result = _result(_summary(1, 12), _summary(2, 13), _summary(3, 14))
    day = datetime.fromtimestamp(NOW, tz=UTC).date()
    iso = day - timedelta(days=13)
    spec = parse_range({"from": iso.isoformat(), "to": iso.isoformat()})
    assert [s.issue for s in apply_range(result, spec, NOW).summaries] == [2]


def test_apply_range_undated_counts_only_under_all():
    result = _result(_summary(1, 0.5), _summary(2, None))
    assert len(apply_range(result, parse_range({"preset": "all"}), NOW).summaries) == 2
    assert len(apply_range(result, parse_range({"preset": "30d"}), NOW).summaries) == 1
    today = datetime.fromtimestamp(NOW, tz=UTC).date()
    spec = parse_range({"from": (today - timedelta(days=90)).isoformat(), "to": today.isoformat()})
    assert len(apply_range(result, spec, NOW).summaries) == 1


# --- strip -----------------------------------------------------------------


def test_day_strip_spans_90_days_oldest_first():
    strip = day_strip((), parse_range({"preset": "30d"}), NOW)
    assert len(strip) == STRIP_DAYS == 90
    assert [d.iso for d in strip] == sorted(d.iso for d in strip)
    today = datetime.fromtimestamp(NOW, tz=UTC).date()
    assert strip[-1].day == today
    assert strip[0].day == today - timedelta(days=89)
    assert all(d.total == 0 for d in strip)


def test_day_strip_buckets_by_outcome_and_tooltips():
    rows = (
        _summary(1, 0.2, "complete"),
        _summary(2, 0.3, "complete"),
        _summary(3, 0.4, "incomplete"),
    )
    strip = day_strip(rows, parse_range({"preset": "all"}), NOW)
    today = next(d for d in strip if d.day == datetime.fromtimestamp(NOW, tz=UTC).date())
    assert today.total == 3
    assert today.counts == {"complete": 2, "incomplete": 1}
    assert "complete 2" in today.tooltip and "incomplete 1" in today.tooltip


def test_day_strip_empty_day_tooltip_names_no_attempts():
    strip = day_strip((), parse_range({"preset": "all"}), NOW)
    assert "no attempts" in strip[0].tooltip


def test_day_strip_preset_selects_covered_days_including_empty():
    rows = (_summary(1, 1),)
    strip = day_strip(rows, parse_range({"preset": "7d"}), NOW)
    today = datetime.fromtimestamp(NOW, tz=UTC).date()
    selected = [d for d in strip if d.selected]
    assert len(selected) == 7
    assert selected[-1].day == today
    # Empty days inside the preset are highlighted too.
    assert any(d.total == 0 and d.selected for d in strip)
    assert not strip[0].selected


def test_day_strip_all_selects_every_day():
    strip = day_strip((), parse_range({"preset": "all"}), NOW)
    assert all(d.selected for d in strip)


def test_day_strip_custom_selects_only_the_span():
    rows = (_summary(1, 12),)
    day = datetime.fromtimestamp(NOW, tz=UTC).date()
    spec = parse_range(
        {
            "from": (day - timedelta(days=13)).isoformat(),
            "to": (day - timedelta(days=12)).isoformat(),
        }
    )
    strip = day_strip(rows, spec, NOW)
    selected = [d for d in strip if d.selected]
    assert len(selected) == 2


def test_day_strip_ignores_undated_and_old_attempts():
    rows = (_summary(1, None), _summary(2, 200))
    strip = day_strip(rows, parse_range({"preset": "all"}), NOW)
    assert all(d.total == 0 for d in strip)


def test_history_layout_constant_still_importable():
    assert HISTORY_LAYOUT == "attempt-logs/v1"
