"""Control console availability: the dashboard control mode plus per-action rules.

Pure view-model functions (spec sections 2, 5.1, 5.2) for the read-only
Control console. Templates receive only these results; there is no public
JSON API. A missing field leaves the action enabled: the agent is the
authority, and its rejection is harmless and shown with its reason.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .config import is_loopback

#: Sentinel for a missing ``control`` key (distinct from null/unreadable).
MISSING = object()

#: Control actions in composer order (spec section 8.2).
ACTION_ORDER = ("stop", "stop_after", "resume", "handoff", "next_issue")

#: Composer labels in the same order.
ACTION_LABELS = {
    "stop": "Stop",
    "stop_after": "Stop after N",
    "resume": "Resume",
    "handoff": "Handoff now",
    "next_issue": "Next issue",
}

#: Exact ``agentctl`` preview per action (``{n}`` / ``{issue}`` filled live).
ACTION_COMMANDS = {
    "stop": "stop",
    "stop_after": "stop --after {n}",
    "resume": "resume",
    "handoff": "handoff now",
    "next_issue": "next issue {issue}",
}

#: "Controls unavailable" reasons for a non-live Reach (spec section 5.1).
REACH_REASONS = {
    "stale": "the snapshot is stale (newer polls failed)",
    "unreachable": "the agent is unreachable",
    "not running": "the container is not running",
    "unsupported": "this agent's telemetry is not supported",
}


@dataclass(frozen=True)
class ControlMode:
    """Dashboard-wide Control availability, computed once at startup."""

    available: bool = True
    reason: str | None = None


def compute_control_mode(
    *,
    control_enabled: bool = True,
    host: str = "127.0.0.1",
    host_guard_disabled: bool = False,
) -> ControlMode:
    """First matching unavailable reason (spec section 2), else available."""
    if not control_enabled:
        return ControlMode(
            available=False,
            reason="control is off (AGENT_DASHBOARD_CONTROL=off, read-only mode)",
        )
    if not is_loopback(host or ""):
        return ControlMode(
            available=False,
            reason=f"the dashboard is bound to a non-loopback address ({host})",
        )
    if host_guard_disabled:
        return ControlMode(
            available=False,
            reason="the Host guard is disabled (AGENT_DASHBOARD_DISABLE_HOST_GUARD=1)",
        )
    return ControlMode(available=True, reason=None)


def host_guard_disabled_from_env(env: dict[str, str] | None = None) -> bool:
    """Whether the Host-guard escape hatch is set (test deployments only)."""
    source = os.environ if env is None else env
    return source.get("AGENT_DASHBOARD_DISABLE_HOST_GUARD") == "1"


@dataclass(frozen=True)
class ControlAction:
    """One Control action: enabled with an optional note, or disabled with its reason."""

    key: str
    label: str
    enabled: bool
    reason: str | None = None
    note: str | None = None
    checked: bool = False
    command: str = ""


@dataclass(frozen=True)
class ControlAvailability:
    """The console body: an unavailable reason, or the per-action composer."""

    unavailable: str | None = None
    actions: tuple[ControlAction, ...] = ()
    stale_note: str | None = None

    @property
    def any_enabled(self) -> bool:
        return any(a.enabled for a in self.actions)


def control_slug(name: str) -> str:
    """Stable per-instance slug for the console element ID."""
    return re.sub(r"[^A-Za-z0-9-]", "-", name)


def _preview(action: str, *, n: Any = None, issue: Any = None) -> str:
    template = ACTION_COMMANDS[action]
    n_text = str(n) if n is not None and str(n) != "" else "N"
    issue_text = str(issue) if issue is not None and str(issue) != "" else "<n>"
    return template.replace("{n}", n_text).replace("{issue}", issue_text)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def control_availability(
    *,
    reach: str,
    control: Any,
    mode: ControlMode | None = None,
    control_stale: bool = False,
    control_age_text: str | None = None,
) -> ControlAvailability:
    """Build the console view model from a fleet row's reach and control section.

    ``control`` is the raw ``control`` section value, or :data:`MISSING`
    when the snapshot has no ``control`` key. A missing field leaves the
    action enabled. Reasons are checked in spec table order; the first one
    that holds is shown.
    """
    active_mode = mode if mode is not None else ControlMode(available=True)
    if not active_mode.available:
        return ControlAvailability(unavailable=active_mode.reason or "unavailable")
    if reach != "live":
        return ControlAvailability(
            unavailable=REACH_REASONS.get(reach, f"the snapshot is {reach}")
        )
    if control is MISSING or control is None:
        return ControlAvailability(
            unavailable="the snapshot has no control section"
        )
    if not isinstance(control, dict):
        return ControlAvailability(
            unavailable="the snapshot's control section is unreadable"
        )

    stale_note: str | None = None
    if control_stale:
        age = control_age_text or "some time"
        stale_note = (
            f"The agent's control state is {age} old; it may be busy "
            "(for example claiming an issue), so acceptance can take up to 10 s."
        )

    states = _action_states(control)
    first_enabled = next((key for key, enabled, _, _ in states if enabled), None)
    actions: list[ControlAction] = []
    for key, enabled, reason, note in states:
        checked = first_enabled is not None and key == first_enabled
        command = _preview(key, n=1) if key == "stop_after" else _preview(key)
        actions.append(
            ControlAction(
                key=key,
                label=ACTION_LABELS[key],
                enabled=enabled,
                reason=reason,
                note=note,
                checked=checked,
                command=command,
            )
        )
    return ControlAvailability(
        unavailable=None, actions=tuple(actions), stale_note=stale_note
    )


def _action_states(control: dict[str, Any]) -> list[tuple[str, bool, str | None, str | None]]:
    """(key, enabled, reason, note) per action in composer order."""
    intake = control.get("intake")
    pending = control.get("pending_command")
    pending = pending if isinstance(pending, dict) else None
    pending_kind = pending.get("kind") if pending else None
    has_pending = bool(pending)
    stop_plan = control.get("stop_plan") if "stop_plan" in control else None
    handoff = control.get("handoff")
    handoff = handoff if isinstance(handoff, dict) else None
    handoff_ack = handoff.get("acknowledgement") if handoff else None
    handoff_accepted = handoff_ack == "accepted"
    handoff_begun = handoff_accepted and bool(handoff.get("begun")) if handoff else False
    recovering = bool(control.get("recovering"))
    recovery = control.get("recovery") if "recovery" in control else None
    has_recovery = recovery is not None
    active_null = "active_attempt" in control and control["active_attempt"] is None
    next_issue = control.get("pending_next_issue")
    next_issue = next_issue if isinstance(next_issue, dict) else None

    out: list[tuple[str, bool, str | None, str | None]] = []

    # Stop.
    if intake == "stopped":
        out.append(("stop", False, "already stopped", None))
    elif pending_kind == "stop":
        out.append(("stop", False, "a stop is pending", None))
    else:
        out.append(("stop", True, None, None))

    # Stop after N.
    if intake == "stopped":
        out.append(("stop_after", False, "already stopped", None))
    elif handoff_begun:
        out.append(("stop_after", False, "a handoff is in progress", None))
    else:
        note: str | None = None
        if isinstance(stop_plan, dict):
            remaining = stop_plan.get("remaining")
            if _is_int(remaining):
                note = f"replaces the current plan ({remaining} left)"
        out.append(("stop_after", True, None, note))

    # Resume.
    accepted_not_begun = handoff_accepted and not handoff_begun
    nothing_to_resume = (
        intake == "running"
        and not has_pending
        and stop_plan is None
        and not accepted_not_begun
    )
    if recovering:
        out.append(("resume", False, "the instance is recovering", None))
    elif has_recovery:
        out.append(
            ("resume", False, "recovery hold: resolve it with agentctl recovery", None)
        )
    elif nothing_to_resume:
        out.append(("resume", False, "already running", None))
    else:
        out.append(("resume", True, None, None))

    # Handoff now. A missing field leaves the action enabled (tolerant
    # reader); only an explicit null reads as no active attempt.
    if active_null:
        out.append(("handoff", False, "no active attempt", None))
    elif recovering:
        out.append(("handoff", False, "the instance is recovering", None))
    elif has_recovery:
        out.append(("handoff", False, "recovery hold", None))
    elif handoff_accepted:
        out.append(("handoff", False, "a handoff is in progress", None))
    else:
        out.append(("handoff", True, None, None))

    # Next issue: never disabled.
    note_next: str | None = None
    if next_issue is not None:
        number = next_issue.get("issue_number")
        if _is_int(number):
            note_next = f"currently #{number} is next"
    out.append(("next_issue", True, None, note_next))

    return out


def format_control_age(observed_at: str | None, *, now: float) -> str | None:
    """Format the control section's age for the stale note (None when unknown)."""
    if not isinstance(observed_at, str) or not observed_at.strip():
        return None
    try:
        text = observed_at.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        moment = datetime.fromisoformat(text)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        age = max(0.0, now - moment.timestamp())
    except ValueError:
        return None
    total = int(age)
    if total < 60:
        return f"{total} s"
    minutes = total // 60
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h"


__all__ = [
    "ACTION_COMMANDS",
    "ACTION_LABELS",
    "ACTION_ORDER",
    "MISSING",
    "REACH_REASONS",
    "ControlAction",
    "ControlAvailability",
    "ControlMode",
    "compute_control_mode",
    "control_availability",
    "control_slug",
    "format_control_age",
    "host_guard_disabled_from_env",
]
