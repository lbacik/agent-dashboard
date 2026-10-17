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

from .charts import format_tokens
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
    stamps = [(r.timestamp, r.elapsed) for r in readings if r.timestamp is not None and r.elapsed is not None]
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
