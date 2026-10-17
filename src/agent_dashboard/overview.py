"""Fleet overview: per-instance outcomes and cost over a time window.

The fleet page's overview panel is built from every managed instance's
attempt summaries (see ``history.read_history``). Instances are read in
parallel with a bounded timeout; an instance whose summaries cannot be read
(not running, unreachable, layout not recognised) keeps its reach and is
excluded from the totals instead of failing the panel.

Charts reuse the history charts' approach and vocabulary (``charts`` outcome
order, labels, shapes and ``outcome-*`` CSS classes); instance order follows
the fleet's attention rank. Instances with no attempts in the window are
shown as such, never as zero cost.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from .charts import (
    OUTCOME_LABELS,
    OUTCOME_ORDER,
    OUTCOME_SHAPES,
    LegendEntry,
    cost_totals,
    normalize_outcome,
)
from .fleet import FleetRow
from .gateway import GatewayError
from .history import (
    DEFAULT_WINDOW,
    NOT_APPLICABLE,
    VALID_WINDOWS,
    HistoryResult,
    format_cost,
    read_history,
)

#: Fallback per-instance read budget when the caller passes no timeout.
OVERVIEW_TIMEOUT = 5.0

NOT_RUNNING_MESSAGE = "Attempt history is available for running managed instances only."

NO_ATTEMPTS_YET = "No attempts recorded yet"
NO_ATTEMPTS_IN_WINDOW = "No attempts in this window"


@dataclass(frozen=True)
class InstanceOverview:
    """One panel row: window-filtered outcome counts and cost for one instance.

    ``counts`` maps canonical outcome keys (``charts.normalize_outcome``) to
    attempt counts over the window. ``cost_total`` is the summed USD cost
    (``None`` when the window has no cost data, so the template renders a
    note instead of zero). ``cost_text`` stays the display: a formatted total,
    ``n/a`` for attempts without usage data or unreadable instances, and an
    explicit sentence when the window holds no attempts.
    """

    name: str
    state: str
    container_id: str
    reach: str
    rank: int
    ok: bool
    error: str | None = None
    window: str = DEFAULT_WINDOW
    total: int = 0
    shown: int = 0
    unreadable: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    outcome_text: str = ""
    cost_total: float | None = None
    cost_with: int = 0
    cost_missing: int = 0
    cost_text: str = NOT_APPLICABLE
    tip: str = ""


@dataclass(frozen=True)
class OverviewResult:
    """The panel: one row per instance in attention-rank order, plus totals.

    Totals cover only readable instances; unreadable ones are counted in
    ``unreadable`` and left out.
    """

    window: str
    rows: tuple[InstanceOverview, ...] = ()
    readable: int = 0
    unreadable: int = 0
    total_cost: float = 0.0
    total_with: int = 0
    total_missing: int = 0
    total_attempts: int = 0


def _outcome_text(counts: dict[str, int]) -> str:
    """Counts in stable legend order, e.g. ``complete: 2, incomplete: 1``."""
    return ", ".join(
        f"{OUTCOME_LABELS[key]}: {counts[key]}" for key in OUTCOME_ORDER if key in counts
    )


def summarize_instance(
    *,
    name: str,
    state: str,
    container_id: str,
    reach: str,
    rank: int,
    window: str,
    result: HistoryResult,
) -> InstanceOverview:
    """Aggregate one parsed history payload into a panel row.

    ``result`` is already window-filtered by :func:`history.read_history`.
    """
    counts: dict[str, int] = {}
    for summary in result.summaries:
        key = normalize_outcome(summary.outcome_text)
        counts[key] = counts.get(key, 0) + 1
    totals = cost_totals(result.summaries)
    cost_total = totals.total if totals.with_cost else None
    if not result.summaries:
        if result.total == 0:
            cost_text = NO_ATTEMPTS_YET
        else:
            cost_text = NO_ATTEMPTS_IN_WINDOW
    elif totals.with_cost:
        cost_text = format_cost(totals.total)
    else:
        cost_text = NOT_APPLICABLE
    outcome_text = _outcome_text(counts)
    if result.summaries:
        tip = f"{name} · {len(result.summaries)} of {result.total} attempts"
        if outcome_text:
            tip += f" · {outcome_text}"
        tip += f" · Cost {cost_text}"
    else:
        tip = f"{name} · {cost_text}"
    return InstanceOverview(
        name=name,
        state=state,
        container_id=container_id,
        reach=reach,
        rank=rank,
        ok=True,
        window=window,
        total=result.total,
        shown=len(result.summaries),
        unreadable=result.unreadable,
        counts=counts,
        outcome_text=outcome_text,
        cost_total=cost_total,
        cost_with=totals.with_cost,
        cost_missing=totals.missing,
        cost_text=cost_text,
        tip=tip,
    )


def error_instance(
    *,
    name: str,
    state: str,
    container_id: str,
    reach: str,
    rank: int,
    window: str,
    error: str,
) -> InstanceOverview:
    """A panel row for an instance whose summaries could not be read."""
    return InstanceOverview(
        name=name,
        state=state,
        container_id=container_id,
        reach=reach,
        rank=rank,
        ok=False,
        error=error,
        window=window,
        tip=f"{name} · {reach} · {error}",
    )


def overview_legend(result: OverviewResult) -> list[LegendEntry]:
    """Legend entries for the outcomes present, in stable order."""
    present = {key for row in result.rows if row.ok for key in row.counts}
    return [
        LegendEntry(key=key, label=OUTCOME_LABELS[key], shape=OUTCOME_SHAPES[key])
        for key in OUTCOME_ORDER
        if key in present
    ]


async def _read_one(
    row: FleetRow,
    gateway,
    semaphore: asyncio.Semaphore,
    *,
    window: str,
    now: float,
    timeout: float,
) -> InstanceOverview:
    """Read and aggregate one instance; any failure becomes an error row."""
    base = {
        "name": row.name,
        "state": row.state,
        "container_id": row.container_id,
        "reach": row.reach,
        "rank": row.rank,
        "window": window,
    }
    if row.state != "running":
        return error_instance(error=NOT_RUNNING_MESSAGE, **base)
    try:
        async with semaphore:
            exec_result = await asyncio.wait_for(
                gateway.attempt_history(row.container_id), timeout
            )
    except TimeoutError:
        return error_instance(
            error=f"Could not read the Attempt log: history read timed out after "
            f"{timeout:g} s",
            **base,
        )
    except (GatewayError, ValueError) as exc:
        return error_instance(
            error=f"Could not read the Attempt log: {exc}", **base
        )
    except Exception as exc:  # one bad instance never fails the panel
        return error_instance(
            error=f"Could not read the Attempt log: {exc}", **base
        )
    result = read_history(
        exec_result.stdout, exec_result.returncode, now=now, window=window
    )
    if not result.ok:
        return error_instance(error=result.error or "layout not recognised", **base)
    return summarize_instance(result=result, **base)


async def collect_overview(
    rows: list[FleetRow],
    gateway,
    *,
    window: str = DEFAULT_WINDOW,
    now: float | None = None,
    timeout: float = OVERVIEW_TIMEOUT,
    max_concurrent_execs: int = 4,
) -> OverviewResult:
    """Aggregate the panel rows for fleet rows (already in attention-rank order).

    Every running instance is read in parallel (bounded by
    ``max_concurrent_execs`` in flight) with ``timeout`` seconds per read, so
    one slow or failing instance neither delays nor breaks the panel. Row
    order follows the input order.
    """
    if max_concurrent_execs < 1:
        raise ValueError(f"max concurrent execs must be >= 1: {max_concurrent_execs}")
    if timeout <= 0:
        raise ValueError(f"timeout must be positive: {timeout}")
    at = time.time() if now is None else now
    if window not in VALID_WINDOWS:
        window = "all"
    semaphore = asyncio.Semaphore(max_concurrent_execs)
    reads = (
        _read_one(row, gateway, semaphore, window=window, now=at, timeout=timeout)
        for row in rows
    )
    parts = await asyncio.gather(*reads)
    total_cost = sum(row.cost_total for row in parts if row.cost_total is not None)
    return OverviewResult(
        window=window,
        rows=tuple(parts),
        readable=sum(1 for row in parts if row.ok),
        unreadable=sum(1 for row in parts if not row.ok),
        total_cost=total_cost,
        total_with=sum(row.cost_with for row in parts if row.ok),
        total_missing=sum(row.cost_missing for row in parts if row.ok),
        total_attempts=sum(row.shown for row in parts if row.ok),
    )
