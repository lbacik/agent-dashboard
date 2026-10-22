"""PROTOTYPE (throwaway, branch prototype/fleet-view-logs): flexible ranges
for the drawer's attempt statistics card.

A range is a time preset (24h/7d/30d/90d/all), the last N attempts, or a
UTC date span (inclusive). Attempts without a parseable start count only
under "all".
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlencode

from .charts import OUTCOME_ORDER, normalize_outcome
from .history import AttemptSummary, HistoryResult

PRESETS = {"24h": 1, "7d": 7, "30d": 30, "90d": 90, "all": None}
PRESET_LABELS = {"24h": "last 24 h", "7d": "last 7 days", "30d": "last 30 days",
                 "90d": "last 90 days", "all": "all time"}
COUNTS = {"5": 5, "10": 10, "25": 25, "all": None}
DEFAULT_PRESET = "30d"
STRIP_DAYS = 90


@dataclass(frozen=True)
class RangeSpec:
    mode: str = "time"  # time | count | dates
    preset: str = DEFAULT_PRESET
    n: str = "10"
    start: date | None = None
    end: date | None = None

    @property
    def query(self) -> str:
        if self.mode == "count":
            return urlencode({"mode": "count", "n": self.n})
        if self.mode == "dates" and self.start and self.end:
            return urlencode(
                {"mode": "dates", "from": self.start.isoformat(), "to": self.end.isoformat()}
            )
        return urlencode({"mode": "time", "preset": self.preset})

    @property
    def label(self) -> str:
        if self.mode == "count":
            return "all attempts" if self.n == "all" else f"last {self.n} attempts"
        if self.mode == "dates" and self.start and self.end:
            if self.start == self.end:
                return self.start.strftime("%-d %b %Y")
            return f"{self.start.strftime('%-d %b')} – {self.end.strftime('%-d %b %Y')}"
        return PRESET_LABELS[self.preset]

    @property
    def history_window(self) -> str:
        """Closest window the history page understands today."""
        if self.mode == "time" and self.preset in ("7d", "30d"):
            return self.preset[:-1]
        return "all"


def _date(raw: str | None) -> date | None:
    try:
        return date.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def parse_range(params) -> RangeSpec:
    mode = params.get("mode", "time")
    if mode == "count":
        n = params.get("n", "10")
        return RangeSpec(mode="count", n=n if n in COUNTS else "10")
    if mode == "dates":
        start, end = _date(params.get("from")), _date(params.get("to"))
        if start and end:
            if start > end:
                start, end = end, start
            return RangeSpec(mode="dates", start=start, end=end)
    preset = params.get("preset", DEFAULT_PRESET)
    return RangeSpec(preset=preset if preset in PRESETS else DEFAULT_PRESET)


def _day(ts: float) -> date:
    return datetime.fromtimestamp(ts, UTC).date()


def apply_range(result: HistoryResult, spec: RangeSpec, now: float) -> HistoryResult:
    """The history result (read with window=all, newest first) cut to the range."""
    rows = result.summaries
    if spec.mode == "count":
        limit = COUNTS[spec.n]
        rows = rows if limit is None else rows[:limit]
    elif spec.mode == "dates" and spec.start and spec.end:
        rows = tuple(
            s for s in rows
            if s.started_ts is not None and spec.start <= _day(s.started_ts) <= spec.end
        )
    else:
        days = PRESETS[spec.preset]
        if days is not None:
            cutoff = now - days * 86400
            rows = tuple(s for s in rows if s.started_ts is not None and s.started_ts >= cutoff)
    return dataclasses.replace(result, summaries=tuple(rows))


@dataclass(frozen=True)
class StripDay:
    day: date
    counts: tuple[tuple[str, int], ...]
    total: int
    selected: bool


def day_strip(
    summaries: tuple[AttemptSummary, ...],
    spec: RangeSpec,
    selected: tuple[AttemptSummary, ...],
    now: float,
) -> list[StripDay]:
    """Attempts per UTC day over the last STRIP_DAYS days, oldest first."""
    today = _day(now)
    first = today - timedelta(days=STRIP_DAYS - 1)
    per_day: dict[date, dict[str, int]] = {}
    for s in summaries:
        if s.started_ts is None:
            continue
        d = _day(s.started_ts)
        if d >= first:
            bucket = per_day.setdefault(d, {})
            key = normalize_outcome(s.outcome_text)
            bucket[key] = bucket.get(key, 0) + 1
    chosen = {_day(s.started_ts) for s in selected if s.started_ts is not None}

    def is_selected(d: date) -> bool:
        if spec.mode == "dates" and spec.start and spec.end:
            return spec.start <= d <= spec.end
        if spec.mode == "time":
            days = PRESETS[spec.preset]
            return days is None or d > today - timedelta(days=days)
        return d in chosen

    out = []
    for i in range(STRIP_DAYS):
        d = first + timedelta(days=i)
        bucket = per_day.get(d, {})
        counts = tuple((k, bucket[k]) for k in OUTCOME_ORDER if k in bucket)
        out.append(StripDay(d, counts, sum(bucket.values()), is_selected(d)))
    return out
