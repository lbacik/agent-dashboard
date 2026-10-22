"""Attempt progress: turns and token budget over elapsed time in one attempt.

The agent writes one periodic ``limits_checked`` reading per settled model
response into the Attempt log event stream (``agent_output.json``); the
reading's ``detail`` carries the attempt's turn count and token budget
against elapsed seconds plus the configured limits (maximum turns,
timeout, soft threshold, maximum budget). This module parses those
readings (tolerant: unknown keys ignored, missing or non-numeric fields
are ``None``, never zero) so the progress page can chart how close one
implementation attempt came to its limits and when a handoff was
triggered.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .gateway import ATTEMPT_EVENTS_LAYOUT
from .history import LAYOUT_NOT_RECOGNISED, format_duration


@dataclass(frozen=True)
class LimitReading:
    """One parsed ``limits_checked`` reading: turns and budget at an elapsed time."""

    thread: str | None = None
    elapsed: float | None = None
    turns: int | None = None
    budget_tokens: float | None = None
    soft_threshold_tokens: float | None = None
    max_budget_tokens: float | None = None
    max_turns: int | None = None
    timeout_seconds: float | None = None
    timestamp: float | None = None


@dataclass(frozen=True)
class ProgressLimits:
    """The configured limits: reference lines, each omitted when unknown."""

    max_turns: int | None = None
    timeout_seconds: float | None = None
    max_budget_tokens: float | None = None
    soft_threshold_tokens: float | None = None


@dataclass(frozen=True)
class HandoffMark:
    """One handoff-triggering event; ``elapsed`` is None when unplaceable."""

    name: str = ""
    timestamp_text: str = ""
    elapsed: float | None = None


@dataclass(frozen=True)
class ProgressResult:
    """The parsed progress payload: readings oldest first with limits."""

    ok: bool
    error: str | None = None
    issue: int = 0
    started_at: str = ""
    outcome_text: str = "interrupted"
    active: bool = False
    readings: tuple[LimitReading, ...] = ()
    limits: ProgressLimits = field(default_factory=ProgressLimits)
    handoffs: tuple[HandoffMark, ...] = ()
    total_events: int = 0
    unreadable: int = 0
    truncated: bool = False


def _num(raw: Any) -> float | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return float(raw)


def _parse_number(text: str) -> float | None:
    try:
        return float(text.strip())
    except ValueError:
        return None


def _parse_time(raw: Any) -> float | None:
    """Parse an event timestamp; None when missing or unparseable.

    Mirrors :func:`history._parse_time`: ISO-8601 with an optional ``Z``
    suffix, naive timestamps read as UTC.
    """
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


def _restore_started(raw: Any) -> str:
    """Restore ``2026-09-27T16:33:43.385090Z`` from its dash-form directory name."""
    if not isinstance(raw, str) or "T" not in raw:
        return ""
    date, clock = raw.split("T", 1)
    return f"{date}T{clock.replace('-', ':')}"


def parse_limit_reading(detail: Any) -> LimitReading | None:
    """Parse one ``limits_checked`` event detail into a reading.

    The agent formats the detail as ``key=value`` pairs separated by
    ``;`` (see ``_token_limits_detail`` upstream). Unknown keys are
    ignored; a missing or non-numeric numeric field stays ``None``.
    Returns ``None`` when the detail carries no recognised key at all.
    """
    if not isinstance(detail, str) or not detail.strip():
        return None
    fields: dict[str, str] = {}
    for chunk in detail.split(";"):
        key, sep, value = chunk.partition("=")
        if not sep:
            continue
        fields[key.strip()] = value.strip()
    if not fields:
        return None
    recognised = {
        "thread",
        "budget_tokens",
        "soft_threshold_tokens",
        "max_budget_tokens",
        "max_turns",
        "timeout_seconds",
        "elapsed_seconds",
        "turns",
    }
    if not any(key in recognised for key in fields):
        return None
    thread = fields.get("thread") or None
    turns_raw = _parse_number(fields["turns"]) if "turns" in fields else None
    max_turns_raw = _parse_number(fields["max_turns"]) if "max_turns" in fields else None
    return LimitReading(
        thread=thread,
        elapsed=_parse_number(fields["elapsed_seconds"]) if "elapsed_seconds" in fields else None,
        turns=int(turns_raw) if turns_raw is not None else None,
        budget_tokens=_parse_number(fields["budget_tokens"]) if "budget_tokens" in fields else None,
        soft_threshold_tokens=(
            _parse_number(fields["soft_threshold_tokens"])
            if "soft_threshold_tokens" in fields
            else None
        ),
        max_budget_tokens=(
            _parse_number(fields["max_budget_tokens"]) if "max_budget_tokens" in fields else None
        ),
        max_turns=int(max_turns_raw) if max_turns_raw is not None else None,
        timeout_seconds=(
            _parse_number(fields["timeout_seconds"]) if "timeout_seconds" in fields else None
        ),
    )


def _is_handoff(name: str) -> bool:
    """Whether an event name marks a triggered handoff (tolerant to new names)."""
    return "handoff" in name.lower()


def _elapsed_at(stamps: list[tuple[float, float]], moment: float) -> float:
    """Place an event timestamp on the readings' elapsed clock.

    ``stamps`` are ``(timestamp, elapsed)`` pairs in file order; the result
    interpolates linearly between the bracketing readings and clamps to the
    observed span outside it.
    """
    first_ts, first_elapsed = stamps[0]
    last_ts, last_elapsed = stamps[-1]
    if moment <= first_ts:
        return first_elapsed
    if moment >= last_ts:
        return last_elapsed
    prev_ts, prev_elapsed = first_ts, first_elapsed
    for stamp_ts, stamp_elapsed in stamps[1:]:
        if moment <= stamp_ts:
            span = stamp_ts - prev_ts
            if span <= 0:
                return stamp_elapsed
            frac = (moment - prev_ts) / span
            return prev_elapsed + frac * (stamp_elapsed - prev_elapsed)
        prev_ts, prev_elapsed = stamp_ts, stamp_elapsed
    return last_elapsed


def _read_limits_from_token_budget(attempt: dict[str, Any]) -> ProgressLimits:
    """Configured budget limits from an ``attempt.json`` token budget, if any."""
    budget = attempt.get("token_budget")
    if not isinstance(budget, dict):
        return ProgressLimits()
    soft = _num(budget.get("effective_soft_threshold_tokens"))
    if soft is None:
        soft = _num(budget.get("soft_threshold_tokens"))
    return ProgressLimits(
        max_budget_tokens=_num(budget.get("max_budget_tokens")),
        soft_threshold_tokens=soft,
    )


def _assemble_progress(
    payload: dict[str, Any], *, issue: int, started: str
) -> ProgressResult:
    """Assemble readings from an attempt-events-layout payload, oldest first.

    Shared by the running-instance exec reader and the stopped-instance
    tree-copy reader: both build the same layout, so the tolerance rules
    apply identically to either source.
    """
    if (
        not isinstance(payload, dict)
        or payload.get("layout") != ATTEMPT_EVENTS_LAYOUT
        or not isinstance(payload.get("events"), list)
    ):
        return ProgressResult(
            ok=False, error=LAYOUT_NOT_RECOGNISED, issue=issue, started_at=_restore_started(started)
        )
    raw_attempt = payload.get("attempt")
    attempt = raw_attempt if isinstance(raw_attempt, dict) else None
    if attempt is None:
        outcome_text, active = "interrupted", False
    else:
        outcome = attempt.get("outcome")
        if isinstance(outcome, str) and outcome:
            outcome_text, active = outcome, False
        else:
            outcome_text, active = "active", True
    readings: list[LimitReading] = []
    handoff_events: list[tuple[str, Any]] = []
    for entry in payload["events"]:
        if not isinstance(entry, dict):
            continue
        name = entry.get("event")
        if not isinstance(name, str) or not name:
            continue
        if name == "limits_checked":
            reading = parse_limit_reading(entry.get("detail"))
            if reading is None:
                continue
            if reading.thread is not None and reading.thread != "main":
                continue
            readings.append(
                LimitReading(
                    thread=reading.thread,
                    elapsed=reading.elapsed,
                    turns=reading.turns,
                    budget_tokens=reading.budget_tokens,
                    soft_threshold_tokens=reading.soft_threshold_tokens,
                    max_budget_tokens=reading.max_budget_tokens,
                    max_turns=reading.max_turns,
                    timeout_seconds=reading.timeout_seconds,
                    timestamp=_parse_time(entry.get("timestamp")),
                )
            )
        elif _is_handoff(name):
            handoff_events.append((name, entry.get("timestamp")))
    readings.sort(
        key=lambda r: (r.timestamp is None, r.timestamp or 0.0, r.elapsed or 0.0)
    )
    stamps = [
        (r.timestamp, r.elapsed)
        for r in readings
        if r.timestamp is not None and r.elapsed is not None
    ]
    stamps.sort(key=lambda pair: pair[0])
    handoffs: list[HandoffMark] = []
    for name, raw_timestamp in handoff_events:
        moment = _parse_time(raw_timestamp)
        elapsed = _elapsed_at(stamps, moment) if moment is not None and stamps else None
        text = raw_timestamp if isinstance(raw_timestamp, str) and raw_timestamp else ""
        handoffs.append(HandoffMark(name=name, timestamp_text=text, elapsed=elapsed))
    limits = _read_limits_from_token_budget(attempt or {})
    max_turns = limits.max_turns
    timeout: float | None = limits.timeout_seconds
    max_budget = limits.max_budget_tokens
    soft = limits.soft_threshold_tokens
    for reading in readings:
        if reading.max_turns is not None:
            max_turns = reading.max_turns
        if reading.timeout_seconds is not None:
            timeout = reading.timeout_seconds
        if reading.max_budget_tokens is not None:
            max_budget = reading.max_budget_tokens
        if reading.soft_threshold_tokens is not None:
            soft = reading.soft_threshold_tokens
    unreadable = payload.get("unreadable")
    if isinstance(unreadable, bool) or not isinstance(unreadable, int) or unreadable < 0:
        unreadable = 0
    total = payload.get("total_events")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        total = 0
    return ProgressResult(
        ok=True,
        issue=issue,
        started_at=_restore_started(started),
        outcome_text=outcome_text,
        active=active,
        readings=tuple(readings),
        limits=ProgressLimits(
            max_turns=max_turns,
            timeout_seconds=timeout,
            max_budget_tokens=max_budget,
            soft_threshold_tokens=soft,
        ),
        handoffs=tuple(handoffs),
        total_events=total,
        unreadable=unreadable,
        truncated=payload.get("truncated") is True,
    )


def read_progress(
    stdout: bytes, returncode: int, *, issue: int, started: str
) -> ProgressResult:
    """Parse one attempt-events exec's stdout into readings, oldest first.

    ``issue``/``started`` (the validated URL reference) stay the result's
    identity; the payload carries the data. ``returncode`` is accepted for
    signature parity with the other Attempt log readers but does not decide
    the outcome: a payload with the expected layout is trusted, anything
    else is ``layout not recognised``. Only main-thread ``limits_checked``
    readings chart the attempt; subagent-thread readings are dropped. An
    attempt without an outcome is ``active``; a missing attempt record
    reads as ``interrupted``.
    """
    del returncode
    try:
        payload = json.loads(stdout.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return ProgressResult(
            ok=False, error=LAYOUT_NOT_RECOGNISED, issue=issue, started_at=_restore_started(started)
        )
    return _assemble_progress(payload, issue=issue, started=started)


def read_stopped_progress(
    data: bytes, *, issue: int, started: str
) -> ProgressResult:
    """Parse one stopped-instance run-directory copy into readings.

    The tar archive is shaped into the attempt-events layout (see
    :func:`stopped.events_payload_from_run_tree`) and assembled with the
    same tolerance rules as the running-instance exec reader. A non-tar
    stream reads as ``layout not recognised``, never an error.
    """
    from .stopped import events_payload_from_run_tree

    try:
        payload = events_payload_from_run_tree(data, issue=issue, started=started)
    except (ValueError, TypeError, AttributeError):
        return ProgressResult(
            ok=False, error=LAYOUT_NOT_RECOGNISED, issue=issue, started_at=_restore_started(started)
        )
    return _assemble_progress(payload, issue=issue, started=started)


@dataclass(frozen=True)
class ProgressPoint:
    """One chart mark: fractional time against a fractional value."""

    x_frac: float
    value_frac: float
    elapsed: float
    value: float


@dataclass(frozen=True)
class ProgressSeries:
    """Turns and budget over elapsed time, scaled for inline SVG.

    ``turns``/``budgets`` hold only readings with both coordinates known;
    ``skipped`` counts the readings left out. ``x_max`` stretches to the
    timeout when known so a finished attempt never fills the plot. Peaks
    include the configured limits for the same reason. ``handoff_fracs``
    holds the placed handoffs on the time axis; ``unplaced_handoffs``
    counts the handoff events without a timestamp.
    """

    x_max: float
    turns: tuple[ProgressPoint, ...] = ()
    budgets: tuple[ProgressPoint, ...] = ()
    turns_peak: float = 0.0
    budget_peak: float = 0.0
    timeout_frac: float | None = None
    max_turns_frac: float | None = None
    max_budget_frac: float | None = None
    soft_threshold_frac: float | None = None
    x_ticks: list[tuple[float, str]] = field(default_factory=list)
    handoff_fracs: list[float] = field(default_factory=list)
    unplaced_handoffs: int = 0
    skipped: int = 0


def _positive(raw: Any) -> float | None:
    """A usable limit value: a real number greater than zero, else None."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    value = float(raw)
    return value if value > 0 else None


def progress_series(result: ProgressResult) -> ProgressSeries | None:
    """Build the turns/budget-over-elapsed series, or None with no data."""
    if not result.ok:
        return None
    placeable = [
        r
        for r in result.readings
        if r.elapsed is not None and (r.turns is not None or r.budget_tokens is not None)
    ]
    if not placeable:
        return None
    skipped = len(result.readings) - len(placeable)
    timeout = _positive(result.limits.timeout_seconds)
    elapsed_values = [r.elapsed for r in placeable if r.elapsed is not None]
    assert elapsed_values
    x_max = max(elapsed_values)
    if timeout is not None:
        x_max = max(x_max, timeout)
    if x_max <= 0:
        return None
    max_turns = _positive(result.limits.max_turns)
    max_budget = _positive(result.limits.max_budget_tokens)
    soft = _positive(result.limits.soft_threshold_tokens)
    turn_values = [float(r.turns) for r in placeable if r.turns is not None]
    budget_values = [float(r.budget_tokens) for r in placeable if r.budget_tokens is not None]
    turns_peak = max(turn_values + ([max_turns] if max_turns is not None else []), default=0.0)
    budget_peak = max(
        budget_values
        + ([max_budget] if max_budget is not None else [])
        + ([soft] if soft is not None else []),
        default=0.0,
    )
    turns: list[ProgressPoint] = []
    for reading in placeable:
        if reading.turns is None or reading.elapsed is None:
            continue
        assert turns_peak >= 0
        value_frac = (float(reading.turns) / turns_peak) if turns_peak > 0 else 0.5
        turns.append(
            ProgressPoint(
                x_frac=reading.elapsed / x_max,
                value_frac=value_frac,
                elapsed=reading.elapsed,
                value=float(reading.turns),
            )
        )
    budgets: list[ProgressPoint] = []
    for reading in placeable:
        if reading.budget_tokens is None or reading.elapsed is None:
            continue
        value_frac = (
            (float(reading.budget_tokens) / budget_peak) if budget_peak > 0 else 0.5
        )
        budgets.append(
            ProgressPoint(
                x_frac=reading.elapsed / x_max,
                value_frac=value_frac,
                elapsed=reading.elapsed,
                value=float(reading.budget_tokens),
            )
        )
    handoff_fracs: list[float] = []
    unplaced = 0
    for mark in result.handoffs:
        if mark.elapsed is None:
            unplaced += 1
        else:
            handoff_fracs.append(mark.elapsed / x_max)
    return ProgressSeries(
        x_max=x_max,
        turns=tuple(turns),
        budgets=tuple(budgets),
        turns_peak=turns_peak,
        budget_peak=budget_peak,
        timeout_frac=(timeout / x_max) if timeout is not None else None,
        max_turns_frac=(
            (max_turns / turns_peak)
            if max_turns is not None and turns_peak > 0
            else None
        ),
        max_budget_frac=(
            (max_budget / budget_peak)
            if max_budget is not None and budget_peak > 0
            else None
        ),
        soft_threshold_frac=(
            (soft / budget_peak) if soft is not None and budget_peak > 0 else None
        ),
        x_ticks=[
            (0.0, format_duration(0)),
            (0.5, format_duration(x_max / 2)),
            (1.0, format_duration(x_max)),
        ],
        handoff_fracs=handoff_fracs,
        unplaced_handoffs=unplaced,
        skipped=skipped,
    )
