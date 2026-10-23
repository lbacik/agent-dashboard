"""Drawer Cost section: per-instance cost figures over the stats-card range.

The Cost section lives inside the attempt-statistics fragment (``_stats.html``),
so one range selector drives both: a preset or strip selection re-renders the
card and the section together, and the drawer's SSE refresh keeps the loaded
fragment via ``hx-preserve`` instead of re-reading attempt summaries on every
poll.

Figures come from the same window-``all`` history read as the card, cut to the
active range with :func:`range_stats.apply_range`:

- ``total`` sums only attempts with cost data (matching the history page for
  the same range); attempts without usage data never count as zero and are
  counted in ``missing``;
- ``average`` divides by the attempts with usage data only;
- ``most`` is the range attempt with the highest cost (newest wins ties, since
  summaries arrive newest first);
- ``last`` is the most recent finished attempt regardless of range (the first
  non-active summary, newest first), whose cost may itself be unknown.

The current attempt has no cost yet: its ``attempt.json`` is written only when
it finishes, and telemetry snapshot v1 carries no USD, so the section always
notes that the current attempt's cost appears once it finishes.
"""

from __future__ import annotations

from dataclasses import dataclass

from .history import AttemptSummary, HistoryResult, format_cost

#: Note shown in every Cost section state: the running attempt has no cost yet.
CURRENT_NOTE = "The current attempt's cost appears once it finishes."

#: Empty states, worded instead of zeros (mirroring the overview panel).
NO_ATTEMPTS_YET = "No attempts recorded yet"
NO_ATTEMPTS_IN_WINDOW = "No attempts in this window"
NO_COST_DATA = "No cost data in this window"


@dataclass(frozen=True)
class CostSummary:
    """Cost figures for the drawer's Cost section.

    ``shown``/``total_attempts`` describe the range cut (``len(cut)`` and the
    full history's ``total``); ``total``/``average`` are ``None`` when no range
    attempt has cost data, so the template renders words instead of zeros.
    ``most`` is the priciest range attempt (``None`` without cost data) and
    ``last`` the most recent finished attempt regardless of range (``None``
    when nothing finished yet).
    """

    shown: int = 0
    total_attempts: int = 0
    with_cost: int = 0
    missing: int = 0
    total: float | None = None
    total_text: str = "n/a"
    average: float | None = None
    average_text: str = "n/a"
    most: AttemptSummary | None = None
    last: AttemptSummary | None = None


def summarize_cost(cut: HistoryResult, full: HistoryResult) -> CostSummary:
    """Aggregate the Cost section figures from the cut and full histories.

    ``cut`` is already range-filtered; ``full`` is the window-``all`` history
    the card read (newest first). Only real numbers count as cost: attempts
    without usage data are excluded from the total, the average and the most
    expensive pick, never treated as zero.
    """
    with_cost = sum(1 for s in cut.summaries if s.cost_value is not None)
    total_value = sum(s.cost_value for s in cut.summaries if s.cost_value is not None)
    total: float | None = total_value if with_cost else None
    average: float | None = (total_value / with_cost) if with_cost else None
    most: AttemptSummary | None = None
    for summary in cut.summaries:
        if summary.cost_value is None:
            continue
        if most is None or summary.cost_value > most.cost_value:
            most = summary
    last: AttemptSummary | None = None
    for summary in full.summaries:
        if not summary.active:
            last = summary
            break
    return CostSummary(
        shown=len(cut.summaries),
        total_attempts=full.total,
        with_cost=with_cost,
        missing=len(cut.summaries) - with_cost,
        total=total,
        total_text=format_cost(total_value) if with_cost else "n/a",
        average=average,
        average_text=format_cost(average) if average is not None else "n/a",
        most=most,
        last=last,
    )
