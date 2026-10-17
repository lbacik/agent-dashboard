"""Attempt history: summaries of one managed instance's implementation attempts.

The gateway collects every ``attempt.json`` under ``/data/logs`` in a single
exec call (see ``gateway.HISTORY_SCRIPT``) and returns the raw stdout. This
module parses that payload into framework-free view models for the
``/instances/{name}/history`` page: one row per attempt with issue, start
time, duration, outcome, model stop reason, cost, and tokens by category
(input, cache read, cache creation, output), newest first.

Tolerance rules (mirroring the fleet readers):

- attempts that failed before the model ran carry no usage data: cost,
  tokens and stop reason render as ``n/a``, never as zero;
- an attempt without an outcome is ``active`` when its start matches the
  instance's active checkpoint, else ``interrupted``;
- an unrecognised or missing layout renders as ``layout not recognised``,
  never an error;
- unknown fields are ignored; a missing ``started_at`` falls back to the
  attempt directory name; entries that still do not parse are skipped and
  counted.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

#: Layout marker the collector script stamps on its output.
HISTORY_LAYOUT = "attempt-logs/v1"

LAYOUT_NOT_RECOGNISED = "layout not recognised"

NOT_APPLICABLE = "n/a"

VALID_WINDOWS = ("7", "30", "all")
DEFAULT_WINDOW = "30"
WINDOW_DAYS = {"7": 7.0, "30": 30.0}

#: Token counters summed across the per-model usage map.
TOKEN_KEYS = (
    ("input_text", "input_tokens"),
    ("cache_read_text", "cache_read_input_tokens"),
    ("cache_creation_text", "cache_creation_input_tokens"),
    ("output_text", "output_tokens"),
)

#: Cost keys accepted inside one model's usage counters, in preference order.
COST_KEYS = ("cost_usd", "total_cost_usd", "cost", "total_cost")

#: Cost soft-threshold keys accepted at the top level of one attempt record,
#: in preference order. The agent reports the threshold only when it knows
#: it, so a missing (or non-numeric, or non-positive) value means unknown and
#: the cost chart omits its reference line instead of guessing.
COST_THRESHOLD_KEYS = (
    "cost_soft_threshold_usd",
    "cost_soft_threshold",
    "soft_threshold_cost_usd",
    "soft_threshold_usd",
    "cost_threshold_usd",
    "cost_threshold",
)


def format_duration(seconds: float) -> str:
    """Format a duration in seconds as ``45s``, ``28m 32s``, ``2h 02m`` or ``1d 1h``."""
    total = int(round(seconds))
    if total < 0:
        total = 0
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def format_cost(value: int | float) -> str:
    """Format a USD cost: cents for ordinary amounts, four decimals for dust."""
    amount = float(value)
    if abs(amount) < 0.01 and amount != 0:
        return f"${amount:,.4f}"
    return f"${amount:,.2f}"


def _format_tokens(value: int | float) -> str:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"{value:,}"


def _num(raw: Any) -> int | float | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return raw


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


def _restore_started(raw: Any) -> str | None:
    """Restore ``2026-09-27T16:33:43Z`` from a directory name with ``-`` for ``:``.

    Only the time part (after ``T``) has its dashes restored; the date part
    keeps its own dashes.
    """
    if not isinstance(raw, str) or "T" not in raw:
        return None
    date, clock = raw.split("T", 1)
    return f"{date}T{clock.replace('-', ':')}"


@dataclass(frozen=True)
class AttemptSummary:
    """One Attempt log row: final display text, so templates add no formatting.

    ``cost_value`` is the numeric USD cost (``None`` when unknown) used only
    for the cost charts and to scale the attempts timeline marks;
    ``cost_text`` stays the display. ``duration_value`` is the numeric
    duration in seconds (``None`` when unknown) for the duration-vs-cost
    scatter; ``duration_text`` stays the display. ``cost_threshold_value``
    is the attempt's reported USD soft threshold (``None`` when the agent
    did not report one) for the cost chart's reference line.
    ``input_value``, ``cache_read_value``, ``cache_creation_value`` and
    ``output_value`` are the numeric token counters (``None`` when the
    category was not reported) for the token charts; the ``*_text``
    fields stay the display.
    """

    issue: int
    issue_title: str | None = None
    started_at: str = NOT_APPLICABLE
    started_ts: float | None = None
    duration_text: str = NOT_APPLICABLE
    duration_value: float | None = None
    outcome_text: str = "interrupted"
    active: bool = False
    stop_reason_text: str = NOT_APPLICABLE
    cost_text: str = NOT_APPLICABLE
    cost_value: float | None = None
    cost_threshold_value: float | None = None
    input_text: str = NOT_APPLICABLE
    cache_read_text: str = NOT_APPLICABLE
    cache_creation_text: str = NOT_APPLICABLE
    output_text: str = NOT_APPLICABLE
    has_usage: bool = False
    input_value: float | None = None
    cache_read_value: float | None = None
    cache_creation_value: float | None = None
    output_value: float | None = None


@dataclass(frozen=True)
class HistoryResult:
    """The parsed history payload: rows newest first with the window applied."""

    ok: bool
    error: str | None = None
    summaries: tuple[AttemptSummary, ...] = ()
    total: int = 0
    unreadable: int = 0
    window: str = DEFAULT_WINDOW


def _read_usage(attempt: dict[str, Any]) -> dict[str, Any]:
    """Sum token and cost counters across the per-model usage map.

    Returns display-ready texts plus ``has_usage``: only a real number ever
    counts as usage, so a missing map never renders as zero.
    """
    out: dict[str, Any] = {name: NOT_APPLICABLE for name, _ in TOKEN_KEYS}
    for name, _ in TOKEN_KEYS:
        out[name.replace("_text", "_value")] = None
    out["cost"] = NOT_APPLICABLE
    out["cost_value"] = None
    out["has_usage"] = False
    usage = attempt.get("model_usage")
    if not isinstance(usage, dict):
        return out
    totals: dict[str, int | float] = {}
    cost: int | float | None = None
    for model in usage.values():
        if not isinstance(model, dict):
            continue
        for _, key in TOKEN_KEYS:
            value = _num(model.get(key))
            if value is not None:
                totals[key] = totals.get(key, 0) + value
        for key in COST_KEYS:
            value = _num(model.get(key))
            if value is not None:
                cost = (cost or 0) + value
                break
    if totals or cost is not None:
        out["has_usage"] = True
    for name, key in TOKEN_KEYS:
        if key in totals:
            out[name] = _format_tokens(totals[key])
            out[name.replace("_text", "_value")] = float(totals[key])
    if cost is not None:
        out["cost"] = format_cost(cost)
        out["cost_value"] = float(cost)
    return out


def _read_cost_threshold(attempt: dict[str, Any]) -> float | None:
    """The attempt's reported USD soft threshold, or None when unknown.

    Only a real positive number counts: a missing key, a non-numeric value
    and zero/negative readings all mean the instance did not report a usable
    threshold, so the cost chart omits its reference line.
    """
    for key in COST_THRESHOLD_KEYS:
        value = _num(attempt.get(key))
        if value is not None and value > 0:
            return float(value)
    return None


def _read_stop_reason(attempt: dict[str, Any], has_usage: bool) -> str:
    if not has_usage:
        return NOT_APPLICABLE
    raw = attempt.get("model_stop_reason")
    if isinstance(raw, str) and raw:
        return raw
    if isinstance(raw, dict):
        reasons = sorted({v for v in raw.values() if isinstance(v, str) and v})
        if reasons:
            return ", ".join(reasons)
    return NOT_APPLICABLE


def _read_entry(entry: Any, active_started_at: str | None, now: float) -> AttemptSummary | None:
    if not isinstance(entry, dict):
        return None
    issue = entry.get("issue")
    if isinstance(issue, bool) or not isinstance(issue, int):
        return None
    attempt = entry.get("attempt")
    if not isinstance(attempt, dict):
        return None
    started_at = _str(attempt.get("started_at")) or _restore_started(entry.get("started"))
    started_ts = _parse_time(started_at)
    outcome = _str(attempt.get("outcome"))
    if outcome is not None:
        outcome_text = outcome
        active = False
    elif active_started_at and started_at == active_started_at:
        outcome_text = "active"
        active = True
    else:
        outcome_text = "interrupted"
        active = False
    duration = _num(attempt.get("duration_seconds"))
    if duration is not None:
        duration_value: float | None = float(duration)
        duration_text = format_duration(duration)
    elif active and started_ts is not None:
        duration_value = max(0.0, now - started_ts)
        duration_text = format_duration(duration_value)
    else:
        duration_value = None
        duration_text = NOT_APPLICABLE
    usage = _read_usage(attempt)
    return AttemptSummary(
        issue=issue,
        issue_title=_str(attempt.get("issue_title")),
        started_at=started_at or NOT_APPLICABLE,
        started_ts=started_ts,
        duration_text=duration_text,
        duration_value=duration_value,
        outcome_text=outcome_text,
        active=active,
        stop_reason_text=_read_stop_reason(attempt, usage["has_usage"]),
        cost_text=usage["cost"],
        cost_value=usage["cost_value"],
        cost_threshold_value=_read_cost_threshold(attempt),
        input_text=usage["input_text"],
        cache_read_text=usage["cache_read_text"],
        cache_creation_text=usage["cache_creation_text"],
        output_text=usage["output_text"],
        has_usage=usage["has_usage"],
        input_value=usage["input_value"],
        cache_read_value=usage["cache_read_value"],
        cache_creation_value=usage["cache_creation_value"],
        output_value=usage["output_value"],
    )


def read_history(
    stdout: bytes, returncode: int, *, now: float | None = None, window: str = "all"
) -> HistoryResult:
    """Parse one history exec's stdout into rows, newest first.

    ``returncode`` is accepted for signature parity with the telemetry
    classifier but does not decide the outcome: a payload with the expected
    layout is trusted, anything else is ``layout not recognised``. ``window``
    is one of ``7``/``30``/``all`` (anything else means ``all``); attempts
    without a parseable start are always shown.
    """
    del returncode
    at = time.time() if now is None else now
    if window not in VALID_WINDOWS:
        window = "all"
    try:
        payload = json.loads(stdout.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return HistoryResult(ok=False, error=LAYOUT_NOT_RECOGNISED, window=window)
    if not isinstance(payload, dict) or payload.get("layout") != HISTORY_LAYOUT:
        return HistoryResult(ok=False, error=LAYOUT_NOT_RECOGNISED, window=window)
    raw_attempts = payload.get("attempts")
    if not isinstance(raw_attempts, list):
        return HistoryResult(ok=False, error=LAYOUT_NOT_RECOGNISED, window=window)
    active_started_at = _str(payload.get("active_started_at"))
    unreadable = payload.get("unreadable")
    if isinstance(unreadable, bool) or not isinstance(unreadable, int) or unreadable < 0:
        unreadable = 0
    summaries: list[AttemptSummary] = []
    for entry in raw_attempts:
        summary = _read_entry(entry, active_started_at, at)
        if summary is None:
            unreadable += 1
        else:
            summaries.append(summary)
    summaries.sort(
        key=lambda s: (s.started_ts is not None, s.started_ts or 0.0), reverse=True
    )
    total = len(summaries)
    days = WINDOW_DAYS.get(window)
    if days is not None:
        cutoff = at - days * 86400
        summaries = [s for s in summaries if s.started_ts is None or s.started_ts >= cutoff]
    return HistoryResult(
        ok=True,
        summaries=tuple(summaries),
        total=total,
        unreadable=unreadable,
        window=window,
    )
