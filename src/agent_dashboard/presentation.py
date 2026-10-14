"""Reach, status headline, attention rank and age formatting.

Reach is how current the dashboard's view of a managed instance is; the
status headline is the one sentence derived from the reach and the latest
telemetry snapshot; the attention rank orders rows so the instances needing
the operator first come first. Snapshot contents are interpreted through the
tolerant snapshot reader (``snapshot.py``): version skew never breaks the
table.
"""

from __future__ import annotations

from typing import Any

from .collector import (
    AGENT_UNREACHABLE,
    GOOD,
    NOT_SUPPORTED,
    NOT_SUPPORTED_REASON,
    SNAPSHOT_FAILED,
    TIMEOUT,
    UNSUPPORTED_OPERATION,
)
from .snapshot import SnapshotDetail, is_unsupported_schema

LIVE = "live"
STALE = "stale"
UNREACHABLE = "unreachable"
NOT_RUNNING = "not running"
UNSUPPORTED = "unsupported"

# Attention rank, lowest first: unreachable, recovery hold, consecutive
# errors and handoff in progress; then the soft threshold zone; then
# working, idle and offline.
RANK_UNREACHABLE = 0
RANK_RECOVERY = 1
RANK_ERRORS = 2
RANK_HANDOFF = 3
RANK_THRESHOLD = 4
RANK_WORKING = 5
RANK_IDLE = 6
RANK_OFFLINE = 7


def derive_reach(container_state: str, outcome_kind: str | None) -> str:
    """Map container state plus the last poll outcome kind to a reach."""
    if container_state != "running":
        return NOT_RUNNING
    if outcome_kind == GOOD:
        return LIVE
    if outcome_kind in (AGENT_UNREACHABLE, TIMEOUT):
        return UNREACHABLE
    if outcome_kind in (UNSUPPORTED_OPERATION, NOT_SUPPORTED):
        return UNSUPPORTED
    if outcome_kind == SNAPSHOT_FAILED:
        return STALE
    # Never polled, or an outcome kind this version does not know: keep the
    # last good snapshot visible rather than crying wolf.
    return STALE


def format_age(seconds: float) -> str:
    """Format a snapshot age for the table: '12 s', '3 min', '2 h'."""
    total = max(0, int(seconds))
    if total < 60:
        return f"{total} s"
    minutes = total // 60
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h"


def build_headline(
    reach: str,
    *,
    reason: str,
    age_text: str | None,
    has_snapshot: bool,
) -> str:
    """One sentence saying what the instance is doing or needs."""
    if reach == LIVE:
        return "Live: reporting telemetry"
    if reach == UNREACHABLE:
        return f"Unreachable: {reason}"
    if reach == NOT_RUNNING:
        return "Not running"
    if reach == UNSUPPORTED:
        return NOT_SUPPORTED_REASON
    # Stale: the agent is up but the snapshot failed.
    if has_snapshot and age_text is not None:
        return f"Stale: last good snapshot {age_text} ago — {reason}"
    if reason:
        return f"Stale: {reason}"
    return "Waiting for the first telemetry poll"


def reach_for_snapshot(
    container_state: str, outcome_kind: str | None, snapshot: dict[str, Any] | None
) -> str:
    """Reach for one instance, treating a too-new schema as unsupported.

    A ``schema_version`` higher than this dashboard reads keeps only the
    stable fields, so the instance is shown as unsupported rather than live.
    """
    if container_state == "running" and is_unsupported_schema(snapshot) is not None:
        return UNSUPPORTED
    return derive_reach(container_state, outcome_kind)


def _snapshot_headline(detail: SnapshotDetail) -> str | None:
    """One sentence derived from the snapshot; None when nothing is known.

    Returns None when both the control and current-attempt sections are
    missing, so the caller falls back to the generic live/stale headline
    instead of claiming idleness it cannot see.
    """
    if detail.unsupported_schema is not None:
        return f"unsupported schema v{detail.unsupported_schema}"
    if detail.recovery_reason is not None or detail.attempt_kind == "recovery":
        issue = f" on #{detail.attempt_issue}" if detail.attempt_issue is not None else ""
        if detail.recovery_reason:
            return f"Recovery hold{issue}: {detail.recovery_reason}"
        if detail.attempt_phase:
            return f"Recovery hold{issue} · {detail.attempt_phase}"
        return "Recovery hold" + issue
    if detail.handoff_active and detail.soft_crossed:
        if detail.attempt_issue is not None:
            return f"Soft threshold crossed, handoff delivering on #{detail.attempt_issue}"
        return "Soft threshold crossed, handoff delivering"
    if detail.handoff_active:
        if detail.attempt_issue is not None:
            return f"Handoff delivering on #{detail.attempt_issue}"
        return "Handoff delivering"
    if detail.soft_crossed:
        if detail.attempt_issue is not None:
            return f"Soft threshold crossed on #{detail.attempt_issue}"
        return "Soft threshold crossed"
    if detail.errors_count is not None and detail.errors_count >= 1:
        if detail.errors_max is not None:
            return f"{detail.errors_count}/{detail.errors_max} consecutive infrastructure errors"
        return f"{detail.errors_count} consecutive infrastructure errors"
    if detail.attempt_kind == "active" and detail.attempt_issue is not None:
        if detail.attempt_phase:
            return f"Working on #{detail.attempt_issue} · {detail.attempt_phase}"
        return f"Working on #{detail.attempt_issue}"
    if detail.attempt_kind == "idle":
        return "Waiting for a ready issue"
    return None


def build_row_headline(
    reach: str,
    *,
    reason: str,
    age_text: str | None,
    has_snapshot: bool,
    detail: SnapshotDetail | None = None,
) -> str:
    """Status headline for one fleet row, derived from reach and snapshot."""
    if reach == UNREACHABLE:
        return f"Unreachable: {reason}"
    if reach == NOT_RUNNING:
        return "Not running"
    if reach == UNSUPPORTED:
        if detail is not None and detail.unsupported_schema is not None:
            return f"unsupported schema v{detail.unsupported_schema}"
        return NOT_SUPPORTED_REASON
    if detail is not None and detail.has_snapshot:
        derived = _snapshot_headline(detail)
        if derived is not None:
            return derived
    return build_headline(reach, reason=reason, age_text=age_text, has_snapshot=has_snapshot)


def attention_rank(reach: str, detail: SnapshotDetail | None, *, has_snapshot: bool) -> int:
    """Order rows so the instances needing the operator come first."""
    if reach == UNREACHABLE:
        return RANK_UNREACHABLE
    if detail is not None and detail.has_snapshot and detail.unsupported_schema is None:
        if detail.attempt_kind == "recovery":
            return RANK_RECOVERY
        if detail.errors_count is not None and detail.errors_count >= 1:
            return RANK_ERRORS
        if detail.handoff_active:
            return RANK_HANDOFF
        if detail.soft_crossed or detail.approaching_soft:
            return RANK_THRESHOLD
        if detail.attempt_kind == "active":
            return RANK_WORKING
        if detail.attempt_kind == "idle":
            return RANK_IDLE
    if reach == LIVE:
        return RANK_WORKING
    if reach == STALE:
        return RANK_WORKING if has_snapshot else RANK_IDLE
    if reach in (NOT_RUNNING, UNSUPPORTED):
        # Offline: visible, but last.
        return RANK_OFFLINE
    # Unknown reach kinds stay last rather than jumping the queue.
    return RANK_OFFLINE
