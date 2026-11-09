"""Fleet overview: per-instance outcomes and cost over a range.

Aggregates every managed instance's attempt summaries for the fleet page's
lazily loaded overview panel (``/overview``). The panel takes the instance
drawer's range model (``range_stats``: presets plus a strip-picked UTC date
span), and both read through :func:`read_full_history`, so the panel and
the drawer card count the same attempts. Instances are read in parallel,
each bounded only by the gateway's own timeouts; an instance whose
summaries cannot be read (gone volume, unreachable, layout not recognised)
keeps its reach and is excluded from the totals instead of failing the
aggregation.

Charts reuse the history charts' approach and vocabulary (``charts`` outcome
order, labels, shapes and ``outcome-*`` CSS classes); instance order follows
the fleet's attention rank. Instances with no attempts in the range are
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
from .contract_history import (
    HISTORY_CAP_DEFAULT,
    ContractAttempt,
    ContractHistoryResult,
    apply_contract_range,
    collect_contract_history,
    contract_day_strip,
    contract_outcome_text,
    counts_by_label,
)
from .fleet import FleetRow
from .history import (
    DEFAULT_WINDOW,
    NOT_APPLICABLE,
    AttemptSummary,
    HistoryResult,
    format_cost,
    read_history,
    read_stopped_history,
)
from .range_stats import RangeSpec, apply_range, day_strip

LOGS_GONE_MESSAGE = (
    "Attempt logs are unavailable for this instance: "
    "its volume is gone or it holds no logs."
)

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
    #: True for contract-path rows: ``counts`` is keyed by outcome-label
    #: slug (display only) and cost always reads as no cost data.
    is_contract: bool = False


@dataclass(frozen=True)
class OverviewResult:
    """The panel: one row per instance in attention-rank order, plus totals.

    Totals cover only readable instances; unreadable ones are counted in
    ``unreadable`` and left out. ``summaries`` holds every readable
    instance's full (uncut) history, which feeds the fleet activity strip.
    """

    spec: RangeSpec = field(default_factory=RangeSpec)
    rows: tuple[InstanceOverview, ...] = ()
    summaries: tuple[AttemptSummary, ...] = ()
    #: Full (uncut) contract Attempt summaries behind the contract rows.
    contract_summaries: tuple[ContractAttempt, ...] = ()
    #: Outcome-label display names by slug, merged over the contract rows.
    contract_labels: dict[str, str] = field(default_factory=dict)
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


def summarize_contract_instance(
    *,
    name: str,
    state: str,
    container_id: str,
    reach: str,
    rank: int,
    window: str,
    summaries: tuple[ContractAttempt, ...] | list[ContractAttempt],
    total: int,
    unreadable: int,
) -> InstanceOverview:
    """Aggregate one parsed contract Attempt list into a panel row.

    ``summaries`` is already range-filtered; ``total`` stays the full
    listing's so the card reads "X of Y attempts". Outcomes group by
    ``outcome.label`` (display only); contract major 1 carries no
    per-Attempt usage, so every Attempt counts as missing cost data
    (never zero) and the cost figure reads as no cost data.
    """
    rows = tuple(summaries)
    counts, labels = counts_by_label(rows)
    if not rows:
        if total == 0:
            cost_text = NO_ATTEMPTS_YET
        else:
            cost_text = NO_ATTEMPTS_IN_WINDOW
    else:
        # Contract major 1 carries no per-Attempt usage: the Attempts
        # count as missing cost data, never as zero cost.
        cost_text = "No cost data"
    outcome_text = contract_outcome_text(counts, labels)
    if rows:
        tip = f"{name} · {len(rows)} of {total} attempts"
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
        total=total,
        shown=len(rows),
        unreadable=unreadable,
        counts=counts,
        outcome_text=outcome_text,
        cost_total=None,
        cost_with=0,
        cost_missing=len(rows),
        cost_text=cost_text,
        tip=tip,
        is_contract=True,
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
    """Legend entries for the outcomes present, in stable order.

    Legacy rows group by canonical outcome; contract rows group by
    ``outcome.label`` (display only), so their slugs follow in label
    order with cycled shapes.
    """
    present = {
        key
        for row in result.rows
        if row.ok and not row.is_contract
        for key in row.counts
    }
    entries = [
        LegendEntry(key=key, label=OUTCOME_LABELS[key], shape=OUTCOME_SHAPES[key])
        for key in OUTCOME_ORDER
        if key in present
    ]
    contract_counts: dict[str, int] = {}
    for row in result.rows:
        if row.ok and row.is_contract:
            for key, count in row.counts.items():
                contract_counts[key] = contract_counts.get(key, 0) + count
    if contract_counts:
        labels = dict(result.contract_labels)
        ordered = sorted(contract_counts, key=lambda k: labels.get(k, k))
        shapes = (
            OUTCOME_SHAPES["complete"],
            OUTCOME_SHAPES["incomplete"],
            OUTCOME_SHAPES["handoff"],
            OUTCOME_SHAPES["infrastructure_error"],
            OUTCOME_SHAPES["no_changes"],
            OUTCOME_SHAPES["other"],
        )
        for index, key in enumerate(ordered):
            entries.append(
                LegendEntry(
                    key=key,
                    label=labels.get(key, key),
                    shape=shapes[index % len(shapes)],
                )
            )
    return entries


def overview_chart_keys(result: OverviewResult) -> list[str]:
    """Bar keys for the overview charts: canonical outcomes plus label slugs."""
    keys = list(OUTCOME_ORDER)
    slugs = sorted(
        result.contract_labels, key=lambda k: result.contract_labels[k]
    )
    return keys + [slug for slug in slugs if slug not in keys]


def overview_chart_labels(result: OverviewResult) -> dict[str, str]:
    """Display labels for :func:`overview_chart_keys` (slugs stay display)."""
    labels = dict(OUTCOME_LABELS)
    labels.update(result.contract_labels)
    return labels


def overview_strip(
    result: OverviewResult, spec: RangeSpec, now: float
) -> list:
    """Fleet-wide 90-day activity strip, oldest day first, both paths merged.

    Legacy summaries group by canonical outcome; contract summaries group
    by outcome-label slug. Buckets carry both key spaces, so the template
    renders every Attempt under the extended :func:`overview_chart_keys`.
    """
    from .range_stats import StripDay

    legacy = day_strip(result.summaries, spec, now) if result.summaries else None
    contract = (
        contract_day_strip(result.contract_summaries, spec, now)
        if result.contract_summaries
        else None
    )
    if legacy is None and contract is None:
        return []
    if legacy is None:
        assert contract is not None
        return [
            StripDay(
                day=d.day,
                iso=d.iso,
                label=d.label,
                counts=dict(d.counts),
                total=d.total,
                selected=d.selected,
                tooltip=d.tooltip,
            )
            for d in contract
        ]
    if contract is None:
        return list(legacy)
    out: list = []
    for old, new in zip(legacy, contract):
        counts = dict(old.counts)
        for key, count in new.counts.items():
            counts[key] = counts.get(key, 0) + count
        total = sum(counts.values())
        if not total:
            tooltip = f"{old.label}: no attempts"
        else:
            bits = "".join(
                f" · {overview_chart_labels(result).get(key, key)} {counts[key]}"
                for key in overview_chart_keys(result)
                if key in counts
            )
            tooltip = f"{old.label}: {total} attempt(s){bits}"
        out.append(
            StripDay(
                day=old.day,
                iso=old.iso,
                label=old.label,
                counts=counts,
                total=total,
                selected=old.selected or new.selected,
                tooltip=tooltip,
            )
        )
    return out


#: Stopped contract instances run no exec: they read as error rows, like
#: legacy instances whose logs cannot be read.
CONTRACT_STOPPED_MESSAGE = (
    "Attempt list is unavailable for this instance: it is not running."
)


async def read_full_history(
    row: FleetRow,
    gateway,
    *,
    now: float,
    contract_cap: int = HISTORY_CAP_DEFAULT,
) -> HistoryResult | ContractHistoryResult | str:
    """Every attempt summary of one instance (window ``all``), or why not.

    Running instances read through one gateway exec; stopped ones through a
    ``docker cp`` tree copy (ADR 0005), with the same tolerance rules. Each
    read is bounded only by the gateway's own timeouts: no outer timeout
    wraps the calls, so a slow read can never cancel the gateway mid-exec
    and orphan a ``docker`` child. Any failure comes back as a display
    message, never an exception. Contract-path rows page the online
    ``attempts list`` (window ``all``, so paging stops at ``contract_cap``);
    stopped contract instances cannot run an exec and read as error rows.
    Every Attempt ID from a contract listing is remembered per instance as
    an addressable ID for the Attempt page.
    """
    if getattr(row, "path", "legacy") == "contract":
        if row.state != "running":
            return CONTRACT_STOPPED_MESSAGE
        if not getattr(row, "exec_user", None):
            return CONTRACT_STOPPED_MESSAGE
        try:
            result = await collect_contract_history(
                gateway,
                row.container_id,
                row.exec_user,
                RangeSpec(preset="all"),
                now=now,
                cap=contract_cap,
                instance_name=row.name,
            )
        except FileNotFoundError:
            return LOGS_GONE_MESSAGE
        except Exception as exc:  # one bad instance never fails the caller
            return f"Could not read the Attempt list: {exc}"
        if not result.ok:
            return result.error or "Attempt list not recognised"
        return result
    try:
        if row.state == "running":
            exec_result = await gateway.attempt_history(row.container_id)
            result = read_history(
                exec_result.stdout, exec_result.returncode, now=now, window="all"
            )
        else:
            tree = await gateway.copy_logs(row.container_id)
            result = read_stopped_history(tree, now=now, window="all")
    except FileNotFoundError:
        return LOGS_GONE_MESSAGE
    except Exception as exc:  # one bad instance never fails the caller
        return f"Could not read the Attempt log: {exc}"
    if not result.ok:
        return result.error or "layout not recognised"
    return result


async def _read_one(
    row: FleetRow,
    gateway,
    semaphore: asyncio.Semaphore,
    *,
    spec: RangeSpec,
    now: float,
    contract_cap: int = HISTORY_CAP_DEFAULT,
) -> tuple[InstanceOverview, HistoryResult | None, ContractHistoryResult | None]:
    """Read and aggregate one instance; any failure becomes an error row."""
    base = {
        "name": row.name,
        "state": row.state,
        "container_id": row.container_id,
        "reach": row.reach,
        "rank": row.rank,
        "window": "all",
    }
    async with semaphore:
        full = await read_full_history(row, gateway, now=now, contract_cap=contract_cap)
    if isinstance(full, str):
        return error_instance(error=full, **base), None, None
    if isinstance(full, ContractHistoryResult):
        cut = apply_contract_range(tuple(full.summaries), spec, now)
        inst = summarize_contract_instance(
            summaries=cut,
            total=full.total,
            unreadable=full.unreadable,
            **base,
        )
        return inst, None, full
    cut = apply_range(full, spec, now)
    return summarize_instance(result=cut, **base), full, None


async def collect_overview(
    rows: list[FleetRow],
    gateway,
    *,
    spec: RangeSpec | None = None,
    now: float | None = None,
    max_concurrent_execs: int = 4,
    contract_cap: int = HISTORY_CAP_DEFAULT,
) -> OverviewResult:
    """Aggregate the panel rows for fleet rows (already in attention-rank order).

    Every instance is read in parallel (bounded by ``max_concurrent_execs``
    in flight), each read bounded only by the gateway's own timeouts, so one
    slow or failing instance neither delays nor breaks the panel, then cut
    to ``spec`` (the default range when omitted). Contract rows page the
    online ``attempts list`` up to ``contract_cap`` and group outcomes by
    ``outcome.label``; their Attempts count as missing cost data, never
    zero. Row order follows the input order.
    """
    if max_concurrent_execs < 1:
        raise ValueError(f"max concurrent execs must be >= 1: {max_concurrent_execs}")
    at = time.time() if now is None else now
    spec = RangeSpec() if spec is None else spec
    semaphore = asyncio.Semaphore(max_concurrent_execs)
    reads = (
        _read_one(row, gateway, semaphore, spec=spec, now=at, contract_cap=contract_cap)
        for row in rows
    )
    results = await asyncio.gather(*reads)
    parts = [inst for inst, _, _ in results]
    total_cost = sum(row.cost_total for row in parts if row.cost_total is not None)
    contract_labels: dict[str, str] = {}
    for inst in parts:
        if inst.ok and inst.is_contract:
            for key in inst.counts:
                contract_labels.setdefault(key, key)
    for _, _, full in results:
        if isinstance(full, ContractHistoryResult):
            _, labels = counts_by_label(full.summaries)
            for key, label in labels.items():
                contract_labels.setdefault(key, label)
    return OverviewResult(
        spec=spec,
        rows=tuple(parts),
        summaries=tuple(
            s for _, full, _ in results if full for s in full.summaries
        ),
        contract_summaries=tuple(
            s for _, _, full in results if full for s in full.summaries
        ),
        contract_labels=contract_labels,
        readable=sum(1 for row in parts if row.ok),
        unreadable=sum(1 for row in parts if not row.ok),
        total_cost=total_cost,
        total_with=sum(row.cost_with for row in parts if row.ok),
        total_missing=sum(row.cost_missing for row in parts if row.ok),
        total_attempts=sum(row.shown for row in parts if row.ok),
    )
