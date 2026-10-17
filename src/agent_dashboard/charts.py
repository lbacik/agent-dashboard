"""Chart aggregation for the attempt history page.

Both charts render from the same :class:`AttemptSummary` rows as the history
table, so they respect the page's time window filter without extra queries:

- outcomes: a stacked bar per UTC day with per-outcome counts;
- timeline: one mark per attempt along time, shaped by outcome and sized
  by cost (attempts without usage data share one neutral size).

Rendering is server-side inline SVG (see ADR 0004): no build step, no
network fetch, so the page works offline over an SSH tunnel. Each outcome
has a stable key, label, CSS class and timeline shape; the legend, tooltips
and axis labels carry the non-colour cue so nothing relies on colour alone.
Attempts without a parseable start time are excluded from both charts and
counted by :func:`undated_count`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .history import WINDOW_DAYS, AttemptSummary

#: Canonical outcome keys in stable legend/stack order. ``other`` covers the
#: dashboard's synthetic states (active, interrupted) and any outcome the
#: agent adds later, so new values never break the charts.
OUTCOME_ORDER = (
    "complete",
    "incomplete",
    "handoff",
    "infrastructure_error",
    "no_changes",
    "other",
)

OUTCOME_LABELS = {
    "complete": "complete",
    "incomplete": "incomplete",
    "handoff": "handoff",
    "infrastructure_error": "infrastructure error",
    "no_changes": "no changes",
    "other": "other",
}

#: Timeline mark shapes, one per outcome: the non-colour cue.
OUTCOME_SHAPES = {
    "complete": "circle",
    "incomplete": "square",
    "handoff": "triangle",
    "infrastructure_error": "diamond",
    "no_changes": "ring",
    "other": "cross",
}

#: Radius for attempts without usage data: one neutral size below any measured
#: mark, never scaled.
NEUTRAL_RADIUS = 4.0
MIN_RADIUS = 6.0
MAX_RADIUS = 13.0

#: Legend glyph per timeline shape: the non-colour cue in text form.
SHAPE_GLYPHS = {
    "circle": "●",
    "square": "■",
    "triangle": "▲",
    "diamond": "◆",
    "ring": "○",
    "cross": "✕",
}

LANES = 3


def normalize_outcome(raw: str | None) -> str:
    """Map a raw outcome text to its canonical chart key.

    Matching is case-insensitive with spaces, dashes and underscores treated
    alike; anything unrecognised (including the empty string) is ``other``.
    """
    if not isinstance(raw, str) or not raw.strip():
        return "other"
    key = raw.strip().lower().replace("-", "_").replace(" ", "_")
    if key in OUTCOME_LABELS:
        return key
    return "other"


def _day_of(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=UTC).strftime("%Y-%m-%d")


@dataclass(frozen=True)
class DayBucket:
    """One stacked bar: per-outcome counts for a UTC day (``YYYY-MM-DD``)."""

    day: str
    counts: dict[str, int] = field(default_factory=dict)
    total: int = 0


@dataclass(frozen=True)
class TimelinePoint:
    """One timeline mark: position, size, shape and tooltip text."""

    issue: int
    started_at: str
    started_ts: float
    duration_text: str
    outcome_text: str
    outcome_key: str
    label: str
    shape: str
    cost_text: str
    has_cost: bool
    x_frac: float
    lane: int
    radius: float
    tooltip: str


@dataclass(frozen=True)
class LegendEntry:
    """One legend row: stable key, human label and timeline shape."""

    key: str
    label: str
    shape: str


def _dated(summaries: tuple[AttemptSummary, ...]) -> list[AttemptSummary]:
    return [s for s in summaries if s.started_ts is not None]


def undated_count(summaries: tuple[AttemptSummary, ...]) -> int:
    """Attempts excluded from both charts for lack of a parseable start."""
    return sum(1 for s in summaries if s.started_ts is None)


def outcomes_per_day(
    summaries: tuple[AttemptSummary, ...] | list[AttemptSummary],
    *,
    now: float,
    window: str = "all",
) -> list[DayBucket]:
    """Bucket dated attempts per UTC day, oldest day first.

    ``summaries`` are already window-filtered by :func:`read_history`; this
    fills the day range so gaps render as empty bars: the trailing
    7/30-day span for those windows, otherwise the earliest-to-latest span.
    """
    rows = tuple(summaries)
    dated = _dated(rows)
    if not dated:
        return []
    days = WINDOW_DAYS.get(window)
    if days is not None:
        start_day = _day_of(now - days * 86400)
        end_day = _day_of(now)
    else:
        stamps = sorted(s.started_ts for s in dated if s.started_ts is not None)
        start_day = _day_of(stamps[0])
        end_day = _day_of(stamps[-1])
    counts: dict[str, dict[str, int]] = {}
    day = start_day
    while day <= end_day:
        counts[day] = {}
        stamp = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC).timestamp()
        day = _day_of(stamp + 86400 + 1)
    for summary in dated:
        assert summary.started_ts is not None
        bucket = _day_of(summary.started_ts)
        if bucket in counts:
            key = normalize_outcome(summary.outcome_text)
            counts[bucket][key] = counts[bucket].get(key, 0) + 1
    return [
        DayBucket(day=name, counts=dict(day_counts), total=sum(day_counts.values()))
        for name, day_counts in counts.items()
    ]


def _tooltip(summary: AttemptSummary) -> str:
    return (
        f"Issue #{summary.issue} · Started {summary.started_at} · "
        f"Duration {summary.duration_text} · Outcome {summary.outcome_text} · "
        f"Cost {summary.cost_text}"
    )


def timeline_points(
    summaries: tuple[AttemptSummary, ...] | list[AttemptSummary],
) -> tuple[TimelinePoint, ...]:
    """One mark per dated attempt, oldest first.

    ``x_frac`` is the fractional time position (a lone attempt sits at 0.5);
    ``radius`` scales with the square root of cost so a 100x run is 10x
    wider, not 100x. Attempts without usage data share :data:`NEUTRAL_RADIUS`
    and ``has_cost=False`` so the template can render them hollow.
    """
    dated = sorted(_dated(tuple(summaries)), key=lambda s: (s.started_ts, s.issue))
    if not dated:
        return ()
    stamps = [s.started_ts for s in dated if s.started_ts is not None]
    lo, hi = min(stamps), max(stamps)
    span = hi - lo
    costs = [s.cost_value for s in dated if s.cost_value is not None]
    positive = [c for c in costs if c is not None and c > 0]
    peak = max(positive) if positive else 0.0
    points: list[TimelinePoint] = []
    for index, summary in enumerate(dated):
        assert summary.started_ts is not None
        if span <= 0:
            x_frac = 0.5
        else:
            x_frac = (summary.started_ts - lo) / span
        if summary.cost_value is None:
            radius = NEUTRAL_RADIUS
            has_cost = False
        elif peak > 0 and summary.cost_value > 0:
            radius = MIN_RADIUS + (MAX_RADIUS - MIN_RADIUS) * math.sqrt(
                summary.cost_value / peak
            )
            has_cost = True
        else:
            # Zero cost with usage elsewhere, or no positive peak: visible
            # but smallest; still a real measurement, never n/a.
            radius = MIN_RADIUS
            has_cost = True
        key = normalize_outcome(summary.outcome_text)
        points.append(
            TimelinePoint(
                issue=summary.issue,
                started_at=summary.started_at,
                started_ts=summary.started_ts,
                duration_text=summary.duration_text,
                outcome_text=summary.outcome_text,
                outcome_key=key,
                label=OUTCOME_LABELS[key],
                shape=OUTCOME_SHAPES[key],
                cost_text=summary.cost_text,
                has_cost=has_cost,
                x_frac=x_frac,
                lane=index % LANES,
                radius=radius,
                tooltip=_tooltip(summary),
            )
        )
    return tuple(points)


def outcome_legend(
    summaries: tuple[AttemptSummary, ...] | list[AttemptSummary],
) -> list[LegendEntry]:
    """Legend entries for the outcomes present, in stable order."""
    present = {normalize_outcome(s.outcome_text) for s in summaries}
    return [
        LegendEntry(key=key, label=OUTCOME_LABELS[key], shape=OUTCOME_SHAPES[key])
        for key in OUTCOME_ORDER
        if key in present
    ]
