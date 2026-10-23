"""Per-instance attempt statistics: the drawer's range model.

The fleet overview panel's per-instance figures live in the instance drawer
instead: a card whose range selector offers presets (``24h``/``7d``/``30d``/
``90d``/``All``) plus an activity strip of attempts per UTC day over the last
90 days. Clicking a day selects that day, dragging across days selects a
range. This module is the framework-free range model behind the card:

- :class:`RangeSpec`: the active range (a preset or an inclusive UTC date
  span), with its query encoding, human label and history-page query;
- :func:`parse_range` / :func:`parse_cookie`: explicit query params win,
  otherwise the remembered cookie value, otherwise the default ``30d``;
- :func:`apply_range`: cut a window-``all`` history result down to the range
  (attempts without a parseable start count only under ``all``);
- :func:`day_strip`: the 90-day activity strip, oldest day first.

Counting matches the overview panel (``overview.summarize_instance``): every
summary counts, including runs without a usable ``attempt.json`` (their
outcome normalises to ``other``), so the card and the panel can never drift
apart.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from urllib.parse import parse_qsl, urlencode

from .charts import OUTCOME_LABELS, OUTCOME_ORDER, normalize_outcome
from .history import AttemptSummary, HistoryResult

#: Range presets in selector order.
PRESETS = ("24h", "7d", "30d", "90d", "all")

#: The default range when neither the query nor the cookie names one.
DEFAULT_PRESET = "30d"

#: Cutoff in days per preset (``all`` means no cutoff).
PRESET_DAYS = {"24h": 1.0, "7d": 7.0, "30d": 30.0, "90d": 90.0}

#: Human labels for the presets.
PRESET_LABELS = {
    "24h": "last 24 hours",
    "7d": "last 7 days",
    "30d": "last 30 days",
    "90d": "last 90 days",
    "all": "all time",
}

#: Activity strip span in UTC days, ending today.
STRIP_DAYS = 90

#: Cookie remembering the chosen range across instances and drawer reopenings.
COOKIE_NAME = "attempt_range"

#: Cookie remembering the fleet overview panel's range, kept apart from the
#: drawer's so picking one never moves the other.
FLEET_COOKIE_NAME = "fleet_range"

#: How long the remembered range lives (one year).
COOKIE_MAX_AGE = 31536000


@dataclass(frozen=True)
class RangeSpec:
    """The card's active range: a preset or an inclusive UTC date span."""

    preset: str = DEFAULT_PRESET
    start: date | None = None
    end: date | None = None

    @property
    def is_custom(self) -> bool:
        """Whether this is a strip-picked date span rather than a preset."""
        return self.start is not None and self.end is not None

    @property
    def query(self) -> str:
        """The cookie/query encoding of this range."""
        if self.is_custom:
            assert self.start is not None and self.end is not None
            return urlencode({"from": self.start.isoformat(), "to": self.end.isoformat()})
        return urlencode({"preset": self.preset})

    @property
    def label(self) -> str:
        """The human label, e.g. ``last 7 days`` or ``1 Oct – 15 Oct 2026``."""
        if self.is_custom:
            assert self.start is not None and self.end is not None
            if self.start == self.end:
                return _fmt_day_year(self.start)
            if self.start.year == self.end.year:
                return (
                    f"{self.start.day} {self.start:%b}"
                    f" – {self.end.day} {self.end:%b %Y}"
                )
            return f"{_fmt_day_year(self.start)} – {_fmt_day_year(self.end)}"
        return PRESET_LABELS[self.preset]

    @property
    def history_window(self) -> str:
        """The history-page query string for exactly this range.

        The history page understands the same encoding as this card
        (``preset=…`` or ``from=…&to=…``), so the link keeps the exact
        range instead of falling back to the closest legacy window.
        """
        return self.query


def _fmt_day_year(day: date) -> str:
    return f"{day.day} {day:%b %Y}"


def _parse_day(raw: str | None) -> date | None:
    try:
        return date.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def parse_range(params: Mapping[str, str]) -> RangeSpec:
    """Parse an explicit range from query params (or a decoded cookie mapping).

    A valid ``from``/``to`` date pair wins over ``preset``; anything invalid
    falls back to the default preset, never to an empty range.
    """
    get = params.get
    start = _parse_day(get("from"))
    end = _parse_day(get("to"))
    if start is not None and end is not None:
        if start > end:
            start, end = end, start
        return RangeSpec(preset="", start=start, end=end)
    preset = get("preset", DEFAULT_PRESET)
    if preset not in PRESETS:
        preset = DEFAULT_PRESET
    return RangeSpec(preset=preset)


def parse_cookie(raw: str | None) -> RangeSpec:
    """Parse the remembered range cookie; anything invalid means the default."""
    if not raw:
        return RangeSpec()
    try:
        return parse_range(dict(parse_qsl(raw, keep_blank_values=True)))
    except (ValueError, AttributeError):
        return RangeSpec()


def _day_of(timestamp: float) -> date:
    return datetime.fromtimestamp(timestamp, tz=UTC).date()


def apply_range(result: HistoryResult, spec: RangeSpec, now: float) -> HistoryResult:
    """Cut a window-``all`` history result down to the range, newest first.

    Totals stay the full history's (so the card reads "X of Y attempts");
    only the summaries are cut. Attempts without a parseable start count
    only under ``all``: they cannot be placed on the strip or in a span.
    """
    rows = result.summaries
    if spec.is_custom:
        assert spec.start is not None and spec.end is not None
        rows = tuple(
            s
            for s in rows
            if s.started_ts is not None and spec.start <= _day_of(s.started_ts) <= spec.end
        )
    elif spec.preset != "all":
        cutoff = now - PRESET_DAYS[spec.preset] * 86400
        rows = tuple(
            s for s in rows if s.started_ts is not None and s.started_ts >= cutoff
        )
    return dataclasses.replace(result, summaries=tuple(rows))


@dataclass(frozen=True)
class StripDay:
    """One activity-strip column: per-outcome counts for a UTC day."""

    day: date
    iso: str
    label: str
    counts: dict[str, int]
    total: int
    selected: bool
    tooltip: str


def _day_tooltip(label: str, total: int, counts: dict[str, int]) -> str:
    if not total:
        return f"{label}: no attempts"
    bits = "".join(
        f" · {OUTCOME_LABELS[key]} {counts[key]}"
        for key in OUTCOME_ORDER
        if key in counts
    )
    return f"{label}: {total} attempt(s){bits}"


def day_strip(
    summaries: tuple[AttemptSummary, ...],
    spec: RangeSpec,
    now: float,
) -> list[StripDay]:
    """Attempts per UTC day over the last :data:`STRIP_DAYS` days, oldest first.

    Gaps render as empty (zero-total) days so the strip always spans the same
    90 days; the selected range highlights every covered day, including empty
    ones. ``summaries`` are the full history's (window ``all``); undated
    attempts have no day and never appear on the strip.
    """
    today = _day_of(now)
    first = today - timedelta(days=STRIP_DAYS - 1)
    per_day: dict[date, dict[str, int]] = {}
    for summary in summaries:
        if summary.started_ts is None:
            continue
        day = _day_of(summary.started_ts)
        if day >= first:
            bucket = per_day.setdefault(day, {})
            key = normalize_outcome(summary.outcome_text)
            bucket[key] = bucket.get(key, 0) + 1

    def is_selected(day: date) -> bool:
        if spec.is_custom:
            assert spec.start is not None and spec.end is not None
            return spec.start <= day <= spec.end
        if spec.preset == "all":
            return True
        span = int(PRESET_DAYS[spec.preset])
        return day > today - timedelta(days=span)

    out: list[StripDay] = []
    for offset in range(STRIP_DAYS):
        day = first + timedelta(days=offset)
        bucket = per_day.get(day, {})
        label = f"{day.day} {day:%b}"
        total = sum(bucket.values())
        out.append(
            StripDay(
                day=day,
                iso=day.isoformat(),
                label=label,
                counts=dict(bucket),
                total=total,
                selected=is_selected(day),
                tooltip=_day_tooltip(label, total, bucket),
            )
        )
    return out
