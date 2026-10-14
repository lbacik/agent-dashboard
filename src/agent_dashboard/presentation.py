"""Reach, status headline and age formatting for the fleet table.

Reach is how current the dashboard's view of a managed instance is; the
status headline is the one sentence derived from the reach and the latest
poll outcome. Snapshot contents are not interpreted here (that is the next
slice's snapshot reader): a live instance reports telemetry, full stop.
"""

from __future__ import annotations

from .collector import (
    AGENT_UNREACHABLE,
    GOOD,
    NOT_SUPPORTED,
    NOT_SUPPORTED_REASON,
    SNAPSHOT_FAILED,
    TIMEOUT,
    UNSUPPORTED_OPERATION,
)

LIVE = "live"
STALE = "stale"
UNREACHABLE = "unreachable"
NOT_RUNNING = "not running"
UNSUPPORTED = "unsupported"


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
