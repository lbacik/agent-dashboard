"""Chart aggregation for the attempt history page.

All charts render from the same :class:`AttemptSummary` rows as the history
table, so they respect the page's time window filter without extra queries:

- outcomes: a stacked bar per UTC day with per-outcome counts;
- timeline: one mark per attempt along time, shaped by outcome and sized
  by cost (attempts without usage data share one neutral size);
- cost per attempt: one mark per attempt with usage data along time,
  shaped by outcome, with a reference line at the reported cost soft
  threshold when one is known;
- cumulative cost: the running USD total per UTC day;
- duration vs cost: one mark per attempt with usage data and a known
  duration, shaped and coloured by outcome;
- tokens per attempt: one stacked bar per attempt with token data along
  time, with one segment per token category (input, cache read, cache
  creation, output);
- cache read share: one mark per attempt with a defined share along
  time, shaped by outcome, showing prompt-caching efficiency.

Rendering is server-side inline SVG (see ADR 0004): no build step, no
network fetch, so the page works offline over an SSH tunnel. Each outcome
has a stable key, label, CSS class and timeline shape; the legend, tooltips
and axis labels carry the non-colour cue so nothing relies on colour alone.
Attempts without a parseable start time are excluded from every chart and
counted by :func:`undated_count`. Attempts without cost data are excluded
from the cost charts and counted by :func:`missing_cost_count`, never
plotted as zero. Attempts without token data are excluded from the token
charts and counted by :func:`missing_token_count`, never plotted as zero.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .history import WINDOW_DAYS, AttemptSummary, format_cost

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
    span: tuple[str, str] | None = None,
) -> list[DayBucket]:
    """Bucket dated attempts per UTC day, oldest day first.

    ``summaries`` are already window-filtered by :func:`read_history`; this
    fills the day range so gaps render as empty bars: the trailing
    24h/7d/30d/90d-day span for those windows (plus the legacy 7/30-day
    span), the explicit ``span`` (an inclusive ``(start_day, end_day)``
    pair of ``YYYY-MM-DD`` days) for a custom date range, otherwise the
    earliest-to-latest span.
    """
    rows = tuple(summaries)
    dated = _dated(rows)
    if not dated:
        return []
    if span is not None:
        start_day, end_day = span
    else:
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


# --- cost charts -------------------------------------------------------------


@dataclass(frozen=True)
class CostPoint:
    """One cost-per-attempt mark: time position, USD cost and tooltip text."""

    issue: int
    started_at: str
    started_ts: float
    duration_text: str
    outcome_text: str
    outcome_key: str
    label: str
    shape: str
    cost_text: str
    cost_value: float
    x_frac: float
    tooltip: str


@dataclass(frozen=True)
class CumulativeDay:
    """One cumulative-cost bar: the day's spend and the running USD total."""

    day: str
    daily: float
    cumulative: float
    daily_text: str = ""
    cumulative_text: str = ""


@dataclass(frozen=True)
class CostTotals:
    """Cost totals over the window-filtered summaries.

    ``total`` sums only attempts with cost data; ``with_cost`` counts them
    and ``missing`` counts the attempts left out for lack of cost data.
    """

    total: float
    with_cost: int
    missing: int


@dataclass(frozen=True)
class DurationCostPoint:
    """One duration-vs-cost mark: USD cost against seconds, plus tooltip."""

    issue: int
    duration_text: str
    duration_value: float
    outcome_text: str
    outcome_key: str
    label: str
    shape: str
    cost_text: str
    cost_value: float
    x_frac: float
    y_frac: float
    tooltip: str


def _with_cost(summaries: tuple[AttemptSummary, ...]) -> list[AttemptSummary]:
    """Dated attempts with cost data, oldest first."""
    return sorted(
        (
            s
            for s in summaries
            if s.started_ts is not None and s.cost_value is not None
        ),
        key=lambda s: (s.started_ts, s.issue),
    )


def missing_cost_count(
    summaries: tuple[AttemptSummary, ...] | list[AttemptSummary],
) -> int:
    """Attempts left out of the cost charts for lack of cost data."""
    return sum(1 for s in summaries if s.cost_value is None)


def cost_totals(
    summaries: tuple[AttemptSummary, ...] | list[AttemptSummary],
) -> CostTotals:
    """Sum USD cost over attempts with cost data; count the rest as missing."""
    rows = tuple(summaries)
    total = sum(s.cost_value for s in rows if s.cost_value is not None)
    with_cost = sum(1 for s in rows if s.cost_value is not None)
    return CostTotals(total=total, with_cost=with_cost, missing=len(rows) - with_cost)


def cost_soft_threshold(
    summaries: tuple[AttemptSummary, ...] | list[AttemptSummary],
) -> float | None:
    """The page's cost soft threshold, or None when no attempt reports one.

    The newest dated attempt that reports a threshold wins, so a changed
    instance configuration shows up on the next attempt; undated attempts
    only count when nothing dated reports a value.
    """
    best: float | None = None
    best_ts = float("-inf")
    seen_dated = False
    for summary in summaries:
        value = summary.cost_threshold_value
        if value is None:
            continue
        if summary.started_ts is not None:
            if not seen_dated or summary.started_ts >= best_ts:
                best, best_ts, seen_dated = value, summary.started_ts, True
        elif not seen_dated and best is None:
            best = value
    return best


def cost_per_attempt(
    summaries: tuple[AttemptSummary, ...] | list[AttemptSummary],
) -> tuple[CostPoint, ...]:
    """One mark per dated attempt with cost data, oldest first.

    ``x_frac`` is the fractional time position (a lone attempt sits at 0.5).
    Vertical scaling is left to the template: it divides by the peak of the
    costs and the soft threshold (when known) so the reference line always
    lands inside the plot.
    """
    dated = _with_cost(tuple(summaries))
    if not dated:
        return ()
    stamps = [s.started_ts for s in dated if s.started_ts is not None]
    lo, hi = min(stamps), max(stamps)
    span = hi - lo
    points: list[CostPoint] = []
    for summary in dated:
        assert summary.started_ts is not None
        assert summary.cost_value is not None
        if span <= 0:
            x_frac = 0.5
        else:
            x_frac = (summary.started_ts - lo) / span
        key = normalize_outcome(summary.outcome_text)
        points.append(
            CostPoint(
                issue=summary.issue,
                started_at=summary.started_at,
                started_ts=summary.started_ts,
                duration_text=summary.duration_text,
                outcome_text=summary.outcome_text,
                outcome_key=key,
                label=OUTCOME_LABELS[key],
                shape=OUTCOME_SHAPES[key],
                cost_text=summary.cost_text,
                cost_value=summary.cost_value,
                x_frac=x_frac,
                tooltip=_tooltip(summary),
            )
        )
    return tuple(points)


def _cost_day_range(
    summaries: tuple[AttemptSummary, ...],
    *,
    now: float,
    window: str,
    span: tuple[str, str] | None = None,
) -> tuple[str, str] | None:
    """The UTC day span for the cost charts, mirroring :func:`outcomes_per_day`.

    The span covers every dated attempt (not just attempts with cost data)
    so the cost charts line up with the outcomes chart: the trailing
    24h/7d/30d/90d-day span for those windows (plus the legacy 7/30-day
    span), the explicit ``span`` for a custom date range, otherwise the
    earliest-to-latest span.
    """
    dated = _dated(summaries)
    if not dated:
        return None
    if span is not None:
        return span
    days = WINDOW_DAYS.get(window)
    if days is not None:
        return _day_of(now - days * 86400), _day_of(now)
    stamps = sorted(s.started_ts for s in dated if s.started_ts is not None)
    return _day_of(stamps[0]), _day_of(stamps[-1])


def _each_day(start_day: str, end_day: str) -> list[str]:
    days: list[str] = []
    day = start_day
    while day <= end_day:
        days.append(day)
        stamp = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC).timestamp()
        day = _day_of(stamp + 86400 + 1)
    return days


def cumulative_cost_per_day(
    summaries: tuple[AttemptSummary, ...] | list[AttemptSummary],
    *,
    now: float,
    window: str = "all",
    span: tuple[str, str] | None = None,
) -> list[CumulativeDay]:
    """Running USD total per UTC day, oldest day first.

    Daily spend sums only attempts with cost data; gap days carry the running
    total forward. Empty when no dated attempt has cost data, so the template
    renders a "no cost data" note instead of a flat zero chart.
    """
    rows = tuple(summaries)
    span = _cost_day_range(rows, now=now, window=window, span=span)
    if span is None:
        return []
    daily: dict[str, float] = dict.fromkeys(_each_day(*span), 0.0)
    seen_cost = False
    for summary in rows:
        if summary.started_ts is None or summary.cost_value is None:
            continue
        bucket = _day_of(summary.started_ts)
        if bucket in daily:
            daily[bucket] += summary.cost_value
            seen_cost = True
    if not seen_cost:
        return []
    out: list[CumulativeDay] = []
    running = 0.0
    for day in daily:
        running += daily[day]
        out.append(
            CumulativeDay(
                day=day,
                daily=daily[day],
                cumulative=running,
                daily_text=format_cost(daily[day]),
                cumulative_text=format_cost(running),
            )
        )
    return out


def duration_vs_cost(
    summaries: tuple[AttemptSummary, ...] | list[AttemptSummary],
) -> tuple[DurationCostPoint, ...]:
    """One mark per dated attempt with both duration and cost, oldest first.

    ``x_frac`` scales linearly with cost and ``y_frac`` with duration; a lone
    mark sits at 0.5 on a degenerate axis. Attempts missing either value are
    excluded: cost-missing attempts are counted by :func:`missing_cost_count`,
    while an attempt with cost but no duration has no separate count.
    """
    rows = sorted(
        (
            s
            for s in tuple(summaries)
            if s.started_ts is not None
            and s.cost_value is not None
            and s.duration_value is not None
        ),
        key=lambda s: (s.started_ts, s.issue),
    )
    if not rows:
        return ()
    costs = [s.cost_value for s in rows if s.cost_value is not None]
    durations = [s.duration_value for s in rows if s.duration_value is not None]
    cost_peak = max(costs)
    duration_peak = max(durations)
    points: list[DurationCostPoint] = []
    for summary in rows:
        assert summary.cost_value is not None
        assert summary.duration_value is not None
        key = normalize_outcome(summary.outcome_text)
        points.append(
            DurationCostPoint(
                issue=summary.issue,
                duration_text=summary.duration_text,
                duration_value=summary.duration_value,
                outcome_text=summary.outcome_text,
                outcome_key=key,
                label=OUTCOME_LABELS[key],
                shape=OUTCOME_SHAPES[key],
                cost_text=summary.cost_text,
                cost_value=summary.cost_value,
                x_frac=(summary.cost_value / cost_peak) if cost_peak > 0 else 0.5,
                y_frac=(
                    (summary.duration_value / duration_peak)
                    if duration_peak > 0
                    else 0.5
                ),
                tooltip=_tooltip(summary),
            )
        )
    return tuple(points)


# --- token charts ------------------------------------------------------------

#: Token categories in stable stack/legend order, with the CSS class suffix
#: each stacked segment renders with (``tok-<suffix>``).
TOKEN_CATEGORIES = (
    ("input", "input", "input"),
    ("cache_read", "cache read", "cache-read"),
    ("cache_creation", "cache creation", "cache-creation"),
    ("output", "output", "output"),
)

#: The single definition of cache read share, reused by the calculation and
#: the history page so the two can never drift apart.
CACHE_READ_SHARE_DEFINITION = "cache read ÷ (input + cache read + cache creation)"


def format_tokens(value: int | float) -> str:
    """Format a token count for chart axis ticks (``120,000``)."""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"{value:,}"


def _token_values(summary: AttemptSummary) -> tuple[float | None, ...]:
    return (
        summary.input_value,
        summary.cache_read_value,
        summary.cache_creation_value,
        summary.output_value,
    )


def _has_tokens(summary: AttemptSummary) -> bool:
    """Whether an attempt reported any token counter (a real number, never zero-fill)."""
    return any(v is not None for v in _token_values(summary))


def missing_token_count(
    summaries: tuple[AttemptSummary, ...] | list[AttemptSummary],
) -> int:
    """Attempts left out of the token charts for lack of token data."""
    return sum(1 for s in summaries if not _has_tokens(s))


def cache_read_share(summary: AttemptSummary) -> float | None:
    """Share of input-side tokens served from cache, or None when undefined.

    Defined once as cache read tokens over total input-side tokens
    (input + cache read + cache creation); output tokens are not
    input-side and never count. Missing categories count as zero once an
    attempt reported some token data, but an attempt without token data
    and a zero input-side total (all zeros, or input-side only from
    missing categories) both mean undefined, never zero.
    """
    if not _has_tokens(summary):
        return None
    input_side = (
        summary.input_value or 0.0,
        summary.cache_read_value or 0.0,
        summary.cache_creation_value or 0.0,
    )
    total = sum(input_side)
    if total <= 0:
        return None
    return (summary.cache_read_value or 0.0) / total


def _token_tooltip(summary: AttemptSummary, share: float | None) -> str:
    if share is None:
        share_text = "n/a"
    else:
        share_text = f"{share:.0%}"
    return (
        f"Issue #{summary.issue} · Started {summary.started_at} · "
        f"Outcome {summary.outcome_text} · "
        f"Input {summary.input_text} · Cache read {summary.cache_read_text} · "
        f"Cache creation {summary.cache_creation_text} · "
        f"Output {summary.output_text} · Cache read share {share_text}"
    )


@dataclass(frozen=True)
class TokenPoint:
    """One stacked token bar: per-category counters plus time position.

    ``segments`` pairs each :data:`TOKEN_CATEGORIES` CSS suffix with its
    counter value, so the template stacks from one source of truth.
    """

    issue: int
    started_at: str
    started_ts: float
    outcome_text: str
    outcome_key: str
    label: str
    shape: str
    input_value: float
    cache_read_value: float
    cache_creation_value: float
    output_value: float
    total: float
    x_frac: float
    tooltip: str
    segments: tuple[tuple[str, float], ...] = ()


@dataclass(frozen=True)
class CacheSharePoint:
    """One cache-read-share mark: fractional share against time, plus tooltip."""

    issue: int
    started_at: str
    started_ts: float
    outcome_text: str
    outcome_key: str
    label: str
    shape: str
    share: float
    share_text: str
    x_frac: float
    tooltip: str


def _with_tokens(summaries: tuple[AttemptSummary, ...]) -> list[AttemptSummary]:
    """Dated attempts with token data, oldest first."""
    return sorted(
        (s for s in summaries if s.started_ts is not None and _has_tokens(s)),
        key=lambda s: (s.started_ts, s.issue),
    )


def tokens_per_attempt(
    summaries: tuple[AttemptSummary, ...] | list[AttemptSummary],
) -> tuple[TokenPoint, ...]:
    """One stacked bar per dated attempt with token data, oldest first.

    ``x_frac`` is the fractional time position (a lone attempt sits at
    0.5); vertical scaling is left to the template, which divides each
    category by the peak attempt total. Missing categories render as
    zero once an attempt reported some token data; attempts without any
    token data are excluded and counted by :func:`missing_token_count`,
    never plotted as zero.
    """
    dated = _with_tokens(tuple(summaries))
    if not dated:
        return ()
    stamps = [s.started_ts for s in dated if s.started_ts is not None]
    lo, hi = min(stamps), max(stamps)
    span = hi - lo
    points: list[TokenPoint] = []
    for summary in dated:
        assert summary.started_ts is not None
        if span <= 0:
            x_frac = 0.5
        else:
            x_frac = (summary.started_ts - lo) / span
        key = normalize_outcome(summary.outcome_text)
        values = {
            "input": summary.input_value or 0.0,
            "cache_read": summary.cache_read_value or 0.0,
            "cache_creation": summary.cache_creation_value or 0.0,
            "output": summary.output_value or 0.0,
        }
        total = sum(values.values())
        segments = tuple(
            (suffix, values[cat_key]) for cat_key, _, suffix in TOKEN_CATEGORIES
        )
        points.append(
            TokenPoint(
                issue=summary.issue,
                started_at=summary.started_at,
                started_ts=summary.started_ts,
                outcome_text=summary.outcome_text,
                outcome_key=key,
                label=OUTCOME_LABELS[key],
                shape=OUTCOME_SHAPES[key],
                input_value=values["input"],
                cache_read_value=values["cache_read"],
                cache_creation_value=values["cache_creation"],
                output_value=values["output"],
                total=total,
                x_frac=x_frac,
                tooltip=_token_tooltip(summary, cache_read_share(summary)),
                segments=segments,
            )
        )
    return tuple(points)


def cache_share_per_attempt(
    summaries: tuple[AttemptSummary, ...] | list[AttemptSummary],
) -> tuple[CacheSharePoint, ...]:
    """One mark per dated attempt with a defined cache read share, oldest first.

    ``x_frac`` is the fractional time position (a lone attempt sits at
    0.5); the vertical axis always spans 0-100%. Attempts without token
    data or with a zero input-side total have no defined share and are
    excluded: the former are counted by :func:`missing_token_count`,
    the latter are measured zeros with no share to plot.
    """
    dated = _with_tokens(tuple(summaries))
    if not dated:
        return ()
    stamps = [s.started_ts for s in dated if s.started_ts is not None]
    lo, hi = min(stamps), max(stamps)
    span = hi - lo
    points: list[CacheSharePoint] = []
    for summary in dated:
        assert summary.started_ts is not None
        share = cache_read_share(summary)
        if share is None:
            continue
        if span <= 0:
            x_frac = 0.5
        else:
            x_frac = (summary.started_ts - lo) / span
        key = normalize_outcome(summary.outcome_text)
        points.append(
            CacheSharePoint(
                issue=summary.issue,
                started_at=summary.started_at,
                started_ts=summary.started_ts,
                outcome_text=summary.outcome_text,
                outcome_key=key,
                label=OUTCOME_LABELS[key],
                shape=OUTCOME_SHAPES[key],
                share=share,
                share_text=f"{share:.0%}",
                x_frac=x_frac,
                tooltip=_token_tooltip(summary, share),
            )
        )
    return tuple(points)
