"""Contract Attempt history: paged ``attempts list`` reader (contract major 1).

A running contract instance lists its Attempts through
``agentctl --contract 1 attempts list --limit 100 [--before <cursor>]``
(newest first). This module parses each page into framework-free view
models, pages until an Attempt starts before the selected range (or the
configurable ``All`` cap), and shares the result with the drawer
statistics card and the Fleet overview panel.

Contract major 1 carries no per-Attempt usage, so contract Attempts never
count as zero cost: cost and token figures always read as "not reported"
and cost aggregations count them as missing.

Tolerance: malformed or unknown replies become a clear message, never an
exception; unknown fields are ignored; entries that do not parse are
skipped and counted as unreadable.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from .charts import LegendEntry
from .contract_reply import (
    AMBIGUOUS,
    CONTRACT_MISMATCH,
    NOT_FOUND,
    REJECTION,
    SUCCESS,
    UNSUPPORTED,
    classify_contract_reply,
)
from .history import format_duration

#: Page size for every ``attempts list`` exec.
HISTORY_PAGE_LIMIT = 100

#: Default cap for the ``All`` range: paging stops with a visible note.
HISTORY_CAP_DEFAULT = 2000

#: Display text for cost and token figures: contract major 1 reports none.
NOT_REPORTED = "not reported"

#: Display for a missing outcome label.
NO_OUTCOME_LABEL = "—"

_UNKNOWN_KEY = "unknown"


def _slug(label: str) -> str:
    """CSS-safe grouping key for an outcome label (display keeps the label)."""
    text = (label or "").strip().lower().replace("_", "-").replace(" ", "-")
    kept = "".join(ch for ch in text if ch.isalnum() or ch == "-")
    kept = re.sub(r"-+", "-", kept).strip("-")
    return kept or _UNKNOWN_KEY


def _str(raw: Any) -> str | None:
    return raw if isinstance(raw, str) and raw else None


def _parse_time(raw: Any) -> float | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        text = raw.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        moment = datetime.fromisoformat(text)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return moment.timestamp()
    except ValueError:
        return None


def _parse_int(raw: Any) -> int | None:
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw


@dataclass(frozen=True)
class ContractAttempt:
    """One contract Attempt: final display text, so templates add no formatting.

    ``cost_value`` stays ``None`` (and token values stay ``None``) so cost
    aggregations count every contract Attempt as missing, never as zero.
    ``outcome_text`` mirrors ``outcome_label`` for the generic helpers;
    label-grouped charts use :func:`counts_by_label` instead.
    """

    attempt_id: str
    issue: int
    issue_title: str | None = None
    started_at: str = "—"
    started_ts: float | None = None
    finished_at: str | None = None
    finished_ts: float | None = None
    duration_text: str = "—"
    duration_value: float | None = None
    lifecycle_state: str = "unknown"
    outcome_label: str = NO_OUTCOME_LABEL
    outcome_native: str | None = None
    outcome_reason: str | None = None
    outcome_text: str = NO_OUTCOME_LABEL
    delivery_number: int | None = None
    delivery_url: str | None = None
    attention_required: bool = False
    sealed: bool = False
    sealed_at: str | None = None
    sealed_text: str = "open"
    # Compatibility with the shared cost/token helpers (always missing).
    cost_text: str = NOT_REPORTED
    cost_value: float | None = None
    cost_threshold_value: float | None = None
    input_text: str = NOT_REPORTED
    cache_read_text: str = NOT_REPORTED
    cache_creation_text: str = NOT_REPORTED
    output_text: str = NOT_REPORTED
    has_usage: bool = False
    input_value: float | None = None
    cache_read_value: float | None = None
    cache_creation_value: float | None = None
    output_value: float | None = None
    has_summary: bool = True
    active: bool = False


@dataclass(frozen=True)
class ContractHistoryResult:
    """Paged ``attempts list`` result, newest first with the range applied."""

    ok: bool
    error: str | None = None
    summaries: tuple[ContractAttempt, ...] = ()
    total: int = 0
    unreadable: int = 0
    capped: bool = False
    cap: int = HISTORY_CAP_DEFAULT
    next_cursor: str | None = None


@dataclass(frozen=True)
class ContractPage:
    """One parsed ``attempts list`` page."""

    ok: bool
    error: str | None = None
    attempts: tuple[ContractAttempt, ...] = ()
    unreadable: int = 0
    next_cursor: str | None = None


def _read_attempt(entry: Any, now: float) -> ContractAttempt | None:
    if not isinstance(entry, dict):
        return None
    attempt_id = _str(entry.get("attempt_id"))
    if attempt_id is None:
        return None
    issue_raw = entry.get("issue")
    if not isinstance(issue_raw, dict):
        return None
    issue = _parse_int(issue_raw.get("number"))
    if issue is None:
        return None
    issue_title = _str(issue_raw.get("title"))
    started_at = _str(entry.get("started_at")) or "—"
    started_ts = _parse_time(entry.get("started_at"))
    finished_at = _str(entry.get("finished_at"))
    finished_ts = _parse_time(entry.get("finished_at"))
    if started_ts is not None and finished_ts is not None:
        duration_value: float | None = max(0.0, finished_ts - started_ts)
        duration_text = format_duration(duration_value)
    elif started_ts is not None and finished_at is None:
        duration_value = max(0.0, now - started_ts)
        duration_text = format_duration(duration_value)
    else:
        duration_value = None
        duration_text = "—"
    lifecycle_state = _str(entry.get("lifecycle_state")) or "unknown"
    outcome_raw = entry.get("outcome")
    if isinstance(outcome_raw, dict):
        outcome_label = _str(outcome_raw.get("label")) or NO_OUTCOME_LABEL
        outcome_native = _str(outcome_raw.get("native"))
        reason_raw = outcome_raw.get("reason")
        outcome_reason = (
            reason_raw if isinstance(reason_raw, str) and reason_raw else None
        )
    elif outcome_raw is None:
        outcome_label = NO_OUTCOME_LABEL
        outcome_native = None
        outcome_reason = None
    else:
        return None
    delivery_number: int | None = None
    delivery_url: str | None = None
    delivery_raw = entry.get("delivery")
    if isinstance(delivery_raw, dict):
        pr_raw = delivery_raw.get("pull_request")
        if isinstance(pr_raw, dict):
            delivery_number = _parse_int(pr_raw.get("number"))
            delivery_url = _str(pr_raw.get("url"))
        elif pr_raw is not None:
            return None
    elif delivery_raw is not None:
        return None
    attention = entry.get("attention_required")
    attention_required = attention is True
    if not isinstance(entry.get("attention_required"), bool):
        # Missing or malformed attention flags read as not required.
        attention_required = False
    sealed_raw = entry.get("sealed")
    sealed = sealed_raw is True
    if not isinstance(sealed_raw, bool):
        return None
    sealed_at = _str(entry.get("sealed_at"))
    if sealed and sealed_at is None:
        return None
    if not sealed and entry.get("sealed_at") is not None:
        return None
    return ContractAttempt(
        attempt_id=attempt_id,
        issue=issue,
        issue_title=issue_title,
        started_at=started_at,
        started_ts=started_ts,
        finished_at=finished_at,
        finished_ts=finished_ts,
        duration_text=duration_text,
        duration_value=duration_value,
        lifecycle_state=lifecycle_state,
        outcome_label=outcome_label,
        outcome_native=outcome_native,
        outcome_reason=outcome_reason,
        outcome_text=outcome_label,
        delivery_number=delivery_number,
        delivery_url=delivery_url,
        attention_required=attention_required,
        sealed=sealed,
        sealed_at=sealed_at,
        sealed_text="sealed" if sealed else "open",
    )


def _envelope_error(stdout: bytes) -> tuple[str | None, str]:
    try:
        envelope = json.loads(stdout.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None, ""
    if not isinstance(envelope, dict):
        return None, ""
    error = envelope.get("error")
    if not isinstance(error, dict):
        return None, ""
    code = error.get("code")
    message = error.get("message")
    return (
        code if isinstance(code, str) else None,
        message if isinstance(message, str) else "",
    )


def parse_contract_attempts_page(
    stdout: bytes, returncode: int | None, *, now: float | None = None
) -> ContractPage:
    """Parse one ``attempts list`` exec into attempts plus the next cursor.

    Never raises: malformed or unknown replies become a clear error message.
    """
    at = time.time() if now is None else now
    cls = classify_contract_reply(returncode, stdout)
    if cls not in (SUCCESS, "replay"):
        code, message = _envelope_error(stdout if isinstance(stdout, bytes) else b"")
        if cls == CONTRACT_MISMATCH:
            detail = message or code or "the instance speaks another contract major"
            return ContractPage(ok=False, error=f"Contract mismatch: {detail}")
        if cls == UNSUPPORTED:
            detail = message or code or "unsupported"
            return ContractPage(
                ok=False,
                error=f"Attempt list not supported by this instance: {detail}",
            )
        if cls == NOT_FOUND:
            return ContractPage(
                ok=False, error=f"Attempt not found: {message or code or 'not found'}"
            )
        if cls == REJECTION:
            return ContractPage(
                ok=False,
                error=f"Attempt list rejected: {message or code or 'rejected'}",
            )
        if cls == AMBIGUOUS:
            if code or message:
                return ContractPage(
                    ok=False,
                    error=f"Could not read the Attempt list: {message or code}",
                )
            return ContractPage(ok=False, error="Attempt list not recognised")
        return ContractPage(ok=False, error=f"Unexpected Attempt list reply: {cls}")
    try:
        payload = json.loads(
            stdout.decode("utf-8") if isinstance(stdout, bytes) else str(stdout)
        )
    except (ValueError, UnicodeDecodeError):
        return ContractPage(ok=False, error="Attempt list not recognised")
    if not isinstance(payload, dict):
        return ContractPage(ok=False, error="Attempt list not recognised")
    result = payload.get("result")
    if not isinstance(result, dict):
        return ContractPage(ok=False, error="Attempt list not recognised")
    raw_attempts = result.get("attempts")
    if not isinstance(raw_attempts, list):
        return ContractPage(ok=False, error="Attempt list not recognised")
    raw_cursor = result.get("next_cursor")
    if raw_cursor is not None and not isinstance(raw_cursor, str):
        return ContractPage(ok=False, error="Attempt list not recognised")
    if isinstance(raw_cursor, str) and not raw_cursor:
        return ContractPage(ok=False, error="Attempt list not recognised")
    attempts: list[ContractAttempt] = []
    unreadable = 0
    for entry in raw_attempts:
        parsed = _read_attempt(entry, at)
        if parsed is None:
            unreadable += 1
        else:
            attempts.append(parsed)
    # Newest first by start time; entries without a start stay at the end.
    attempts.sort(
        key=lambda s: (s.started_ts is not None, s.started_ts or 0.0), reverse=True
    )
    return ContractPage(
        ok=True, attempts=tuple(attempts), unreadable=unreadable,
        next_cursor=raw_cursor if isinstance(raw_cursor, str) else None,
    )


# --- addressable-ID memory -----------------------------------------------------


_KNOWN_IDS: dict[str, set[str]] = {}


def remember_attempt_ids(instance_name: str, attempt_ids: Any) -> None:
    """Remember Attempt IDs from a listing as addressable IDs for the instance."""
    if not isinstance(instance_name, str) or not instance_name:
        return
    bucket = _KNOWN_IDS.setdefault(instance_name, set())
    try:
        ids = list(attempt_ids)
    except TypeError:
        return
    for attempt_id in ids:
        if isinstance(attempt_id, str) and attempt_id:
            bucket.add(attempt_id)


def known_attempt_ids(instance_name: str) -> frozenset[str]:
    """Attempt IDs remembered for one instance (empty when none)."""
    bucket = _KNOWN_IDS.get(instance_name)
    return frozenset(bucket) if bucket else frozenset()


def clear_known_attempt_ids() -> None:
    """Forget every remembered Attempt ID (tests only)."""
    _KNOWN_IDS.clear()


# --- range filtering and label grouping ----------------------------------------


def _cutoff_for_spec(spec: Any, now: float) -> float | None:
    """Lower-bound timestamp for a range spec, or None for ``all``."""
    from .range_stats import PRESET_DAYS

    if getattr(spec, "is_custom", False):
        assert spec.start is not None
        start_day: date = spec.start
        midnight = datetime(
            start_day.year, start_day.month, start_day.day, tzinfo=UTC
        ).timestamp()
        return midnight
    preset = getattr(spec, "preset", "all")
    if preset == "all":
        return None
    days = PRESET_DAYS.get(preset)
    if days is None:
        return None
    return now - days * 86400


def apply_contract_range(
    attempts: tuple[ContractAttempt, ...] | list[ContractAttempt],
    spec: Any,
    now: float,
) -> tuple[ContractAttempt, ...]:
    """Cut newest-first attempts down to a drawer/overview range."""
    rows = tuple(attempts)
    if getattr(spec, "is_custom", False):
        assert spec.start is not None and spec.end is not None
        out = []
        for summary in rows:
            if summary.started_ts is None:
                continue
            day = datetime.fromtimestamp(summary.started_ts, tz=UTC).date()
            if spec.start <= day <= spec.end:
                out.append(summary)
        return tuple(out)
    preset = getattr(spec, "preset", "all")
    if preset == "all":
        return rows
    from .range_stats import PRESET_DAYS

    days = PRESET_DAYS.get(preset)
    if days is None:
        return rows
    cutoff = now - days * 86400
    return tuple(
        s for s in rows if s.started_ts is not None and s.started_ts >= cutoff
    )


def counts_by_label(
    attempts: tuple[ContractAttempt, ...] | list[ContractAttempt],
) -> tuple[dict[str, int], dict[str, str]]:
    """Group attempts by outcome label: ``(counts, display labels)`` by slug key."""
    counts: dict[str, int] = {}
    labels: dict[str, str] = {}
    for summary in attempts:
        key = _slug(summary.outcome_label)
        counts[key] = counts.get(key, 0) + 1
        labels.setdefault(key, summary.outcome_label)
    return counts, labels


def contract_outcome_text(counts: dict[str, int], labels: dict[str, str]) -> str:
    """Counts in label order, e.g. ``Delivered: 2`` (display only)."""
    return ", ".join(
        f"{labels[key]}: {counts[key]}" for key in sorted(counts, key=lambda k: labels[k])
    )


def contract_legend(
    attempts: tuple[ContractAttempt, ...] | list[ContractAttempt],
) -> list[LegendEntry]:
    """Legend entries for the outcome labels present, sorted by label."""
    from .charts import OUTCOME_SHAPES

    shapes = (
        OUTCOME_SHAPES["complete"],
        OUTCOME_SHAPES["incomplete"],
        OUTCOME_SHAPES["handoff"],
        OUTCOME_SHAPES["infrastructure_error"],
        OUTCOME_SHAPES["no_changes"],
        OUTCOME_SHAPES["other"],
    )
    counts, labels = counts_by_label(attempts)
    ordered = sorted(counts, key=lambda k: labels[k])
    out: list[LegendEntry] = []
    for index, key in enumerate(ordered):
        out.append(
            LegendEntry(key=key, label=labels[key], shape=shapes[index % len(shapes)])
        )
    return out


@dataclass(frozen=True)
class ContractStripDay:
    """One activity-strip column for contract attempts: per-label counts."""

    day: date
    iso: str
    label: str
    counts: dict[str, int]
    total: int
    selected: bool
    tooltip: str


def contract_day_strip(
    attempts: tuple[ContractAttempt, ...] | list[ContractAttempt],
    spec: Any,
    now: float,
) -> list[ContractStripDay]:
    """Attempts per UTC day over the last 90 days, oldest day first.

    Mirrors :func:`range_stats.day_strip` but groups by outcome label.
    """
    from .range_stats import PRESET_DAYS, STRIP_DAYS

    rows = tuple(attempts)
    today = datetime.fromtimestamp(now, tz=UTC).date()
    first = today - timedelta(days=STRIP_DAYS - 1)
    per_day: dict[date, dict[str, int]] = {}
    display: dict[str, str] = {}
    for summary in rows:
        if summary.started_ts is None:
            continue
        day = datetime.fromtimestamp(summary.started_ts, tz=UTC).date()
        if day >= first:
            key = _slug(summary.outcome_label)
            display.setdefault(key, summary.outcome_label)
            bucket = per_day.setdefault(day, {})
            bucket[key] = bucket.get(key, 0) + 1

    def is_selected(day: date) -> bool:
        if getattr(spec, "is_custom", False):
            assert spec.start is not None and spec.end is not None
            return spec.start <= day <= spec.end
        preset = getattr(spec, "preset", "all")
        if preset == "all":
            return True
        span = int(PRESET_DAYS.get(preset, 30))
        return day > today - timedelta(days=span)

    def tooltip(label: str, total: int, counts: dict[str, int]) -> str:
        if not total:
            return f"{label}: no attempts"
        bits = "".join(
            f" · {display.get(key, key)} {counts[key]}"
            for key in sorted(counts, key=lambda k: display.get(k, k))
        )
        return f"{label}: {total} attempt(s){bits}"

    out: list[ContractStripDay] = []
    for offset in range(STRIP_DAYS):
        day = first + timedelta(days=offset)
        bucket = per_day.get(day, {})
        label = f"{day.day} {day:%b}"
        total = sum(bucket.values())
        out.append(
            ContractStripDay(
                day=day,
                iso=day.isoformat(),
                label=label,
                counts=dict(bucket),
                total=total,
                selected=is_selected(day),
                tooltip=tooltip(label, total, bucket),
            )
        )
    return out


async def collect_contract_history(
    gateway: Any,
    container_id: str,
    exec_user: str,
    spec: Any,
    *,
    now: float | None = None,
    cap: int = HISTORY_CAP_DEFAULT,
    instance_name: str | None = None,
) -> ContractHistoryResult:
    """Page ``attempts list`` newest-first until the range cutoff or the cap.

    Every page runs online with the contract's ``attempts-list`` timeout
    (15 s). Paging stops at the first page whose oldest Attempt starts
    before the selected range; ``All`` stops at ``cap`` with ``capped``
    set so the page shows a "showing newest N" note.
    """
    at = time.time() if now is None else now
    if not isinstance(cap, int) or isinstance(cap, bool) or cap < 1:
        cap = HISTORY_CAP_DEFAULT
    cutoff = _cutoff_for_spec(spec, at)
    fetched: list[ContractAttempt] = []
    unreadable = 0
    cursor: str | None = None
    capped = False
    last_cursor: str | None = None
    while len(fetched) < cap:
        try:
            exec_result = await gateway.contract_attempts_list(
                container_id, exec_user, limit=HISTORY_PAGE_LIMIT, before=cursor
            )
            page = parse_contract_attempts_page(
                exec_result.stdout, exec_result.returncode, now=at
            )
        except FileNotFoundError:
            return ContractHistoryResult(
                ok=False,
                error=(
                    "Attempt logs are unavailable for this instance: "
                    "its volume is gone or it holds no logs."
                ),
                cap=cap,
            )
        except Exception as exc:
            return ContractHistoryResult(
                ok=False, error=f"Could not read the Attempt list: {exc}", cap=cap
            )
        if not page.ok:
            return ContractHistoryResult(ok=False, error=page.error, cap=cap)
        fetched.extend(page.attempts)
        unreadable += page.unreadable
        if instance_name is not None:
            remember_attempt_ids(instance_name, [a.attempt_id for a in page.attempts])
        if page.next_cursor is None:
            last_cursor = None
            break
        last_cursor = page.next_cursor
        if len(fetched) >= cap:
            capped = True
            fetched = fetched[:cap]
            break
        oldest = fetched[-1] if fetched else None
        if (
            cutoff is not None
            and oldest is not None
            and oldest.started_ts is not None
            and oldest.started_ts < cutoff
        ):
            break
        cursor = page.next_cursor
    else:
        capped = len(fetched) >= cap
    cut = apply_contract_range(tuple(fetched), spec, at)
    return ContractHistoryResult(
        ok=True,
        summaries=tuple(cut),
        total=len(fetched),
        unreadable=unreadable,
        capped=capped,
        cap=cap,
        next_cursor=last_cursor,
    )
