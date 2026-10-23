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

#: Display labels for ``control.commands`` kinds (action keys use
#: underscores; the timeline reads "stop after" and "next issue").
KIND_LABELS = {
    "stop": "stop",
    "stop_after": "stop after",
    "resume": "resume",
    "handoff": "handoff",
    "next_issue": "next issue",
}

#: Card titles per terminal acknowledgement (spec section 8.4).
TERMINAL_TITLES = {
    "completed": "Completed",
    "superseded": "Superseded",
    "not fulfilled": "Not fulfilled",
}

#: Card tones per terminal acknowledgement (spec section 8.4): Completed
#: ok, Superseded muted, Not fulfilled warn. Every card also has a text
#: title, so its state never relies on colour alone.
TERMINAL_TONES = {
    "completed": "ok",
    "superseded": "muted",
    "not fulfilled": "warn",
}

#: Body when the tracked container is gone or was recreated (spec 8.4).
GONE_BODY = (
    "Instance replaced or unavailable; the agent's record of this command "
    "is on its /data volume."
)

#: Body when the command fell out of the last 10 recorded commands.
EVICTED_BODY = "No longer in the last 10 commands"

#: Body while the command is not yet in the latest good snapshot.
WAITING_BODY = "waiting for the next snapshot"

#: Progress while a next-issue command waits for its claim.
NEXT_ISSUE_WAITING = "waiting for the next permitted claim"

#: Terminal acknowledgements for the lifecycle stepper (spec section 8.5).
TERMINAL_ACKS = ("completed", "superseded", "not fulfilled")

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


def fleet_chips(control: Any) -> tuple[str, ...]:
    """Command-state chips for the fleet table's Intake cell (spec 8.1).

    Derived from the latest good snapshot's ``control`` section only, in
    this order: ``handoff · <phase>`` when a handoff is accepted,
    ``stop pending``, ``stops after <remaining> more`` and ``next #<n>``.
    A missing or unreadable section yields no chips; a missing field
    inside it simply yields no chip for that slot (tolerant reader).
    """
    if not isinstance(control, dict):
        return ()
    chips: list[str] = []
    handoff = control.get("handoff")
    if isinstance(handoff, dict) and handoff.get("acknowledgement") == "accepted":
        phase = handoff.get("phase")
        chips.append(
            f"handoff · {phase}" if isinstance(phase, str) and phase else "handoff"
        )
    pending = control.get("pending_command")
    if isinstance(pending, dict) and pending.get("kind") == "stop":
        chips.append("stop pending")
    stop_plan = control.get("stop_plan") if "stop_plan" in control else None
    if isinstance(stop_plan, dict):
        remaining = stop_plan.get("remaining")
        if _is_int(remaining):
            chips.append(f"stops after {remaining} more")
        else:
            chips.append("stops after more")
    upcoming = control.get("pending_next_issue")
    if isinstance(upcoming, dict):
        number = upcoming.get("issue_number")
        if _is_int(number):
            chips.append(f"next #{number}")
        else:
            chips.append("next")
    return tuple(chips)


def _short_clock(value: Any) -> str:
    """Extract ``HH:MM:SS`` from an ISO-8601 timestamp, else the raw text."""
    if not isinstance(value, str) or not value:
        return "—"
    clock = value[11:19] if len(value) >= 19 and value[10] == "T" else ""
    if (
        len(clock) == 8
        and clock[2] == ":"
        and clock[5] == ":"
        and clock.replace(":", "").isdigit()
    ):
        return clock
    return value


@dataclass(frozen=True)
class CommandStep:
    """One lifecycle stepper stage: its label and done/current/todo state."""

    label: str
    state: str  # "done" | "current" | "todo"


@dataclass(frozen=True)
class CommandEntry:
    """One ``control.commands`` record for the timeline (spec 8.5)."""

    request_id: str
    sequence: int | None
    kind: str
    kind_label: str
    acknowledgement: str | None
    detail: str
    updated_at: str | None
    short_time: str
    origin: str  # "dashboard" | "CLI"
    steps: tuple[CommandStep, ...] = ()
    current_step: str = "submitted"


def _terminal_label(acknowledgement: str | None) -> str:
    """Terminal stepper label: a known acknowledgement, else ``completed``."""
    if isinstance(acknowledgement, str) and acknowledgement in TERMINAL_ACKS:
        return acknowledgement
    return "completed"


def _make_steps(labels: list[str], current: int) -> tuple[CommandStep, ...]:
    """Build the stepper stages around the current step's index."""
    return tuple(
        CommandStep(
            label=label,
            state=(
                "done" if index < current else "current" if index == current else "todo"
            ),
        )
        for index, label in enumerate(labels)
    )


def _stepper_labels(acknowledgement: str | None) -> list[str]:
    """Base stepper: submitted → accepted → terminal acknowledgement."""
    return ["submitted", "accepted", _terminal_label(acknowledgement)]


def _current_index(labels: list[str], acknowledgement: str | None) -> int:
    """Index of the current step for an acknowledgement (tolerant reader)."""
    if isinstance(acknowledgement, str) and acknowledgement in TERMINAL_ACKS:
        return len(labels) - 1
    if acknowledgement == "accepted":
        return 1
    return 0


def _build_steps(
    acknowledgement: str | None, *, handoff_phase: str | None = None
) -> tuple[tuple[CommandStep, ...], str]:
    """Lifecycle stepper plus the current step's label.

    A handoff whose request ID matches ``handoff.request_id`` inserts
    ``delivering → model work`` and takes its current step from
    ``handoff.phase``; every other command uses the base stepper.
    """
    if handoff_phase is not None:
        labels = [
            "submitted",
            "accepted",
            "delivering",
            "model work",
            _terminal_label(acknowledgement),
        ]
        if isinstance(acknowledgement, str) and acknowledgement in TERMINAL_ACKS:
            # A terminal acknowledgement wins over a stale handoff phase:
            # a finished handoff reads as finished, not delivering/model work.
            current = len(labels) - 1
        else:
            normalized = handoff_phase.replace("_", " ").strip().lower()
            if normalized == "model work":
                current = 3
            elif normalized == "delivering":
                current = 2
            else:
                current = _current_index(labels, acknowledgement)
        return _make_steps(labels, current), labels[current]
    labels = _stepper_labels(acknowledgement)
    current = _current_index(labels, acknowledgement)
    return _make_steps(labels, current), labels[current]


def command_timeline(control: Any) -> tuple[CommandEntry, ...]:
    """The ``control.commands`` timeline, newest first by sequence (spec 8.5).

    Reads the latest good snapshot's ``control`` section only. At most the
    last 10 commands are returned; records without an integer sequence sort
    last. A missing or unreadable section yields no entries.
    """
    if not isinstance(control, dict):
        return ()
    raw = control.get("commands")
    if isinstance(raw, dict):
        records = list(raw.values())
    elif isinstance(raw, list):
        records = list(raw)
    else:
        return ()
    handoff = control.get("handoff")
    handoff = handoff if isinstance(handoff, dict) else {}
    handoff_request = handoff.get("request_id")
    parsed: list[tuple[int, int, CommandEntry]] = []
    for order, record in enumerate(records):
        if not isinstance(record, dict):
            continue
        request_id = record.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            continue
        sequence = record.get("sequence")
        sequence = sequence if _is_int(sequence) else None
        kind = record.get("kind")
        kind = kind if isinstance(kind, str) and kind else "unknown"
        acknowledgement = record.get("acknowledgement")
        acknowledgement = (
            acknowledgement
            if isinstance(acknowledgement, str) and acknowledgement
            else None
        )
        detail = record.get("detail")
        detail = detail if isinstance(detail, str) else ""
        updated = record.get("updated_at")
        updated = updated if isinstance(updated, str) and updated else None
        created = record.get("created_at")
        created = created if isinstance(created, str) and created else None
        clock_source = updated or created
        origin = "dashboard" if request_id.startswith("dash-") else "CLI"
        follows_handoff = (
            kind == "handoff"
            and isinstance(handoff_request, str)
            and handoff_request
            and request_id == handoff_request
        )
        phase = handoff.get("phase") if follows_handoff else None
        phase_arg = (
            phase if follows_handoff and isinstance(phase, str) and phase else None
        )
        steps, current = _build_steps(acknowledgement, handoff_phase=phase_arg)
        entry = CommandEntry(
            request_id=request_id,
            sequence=sequence,
            kind=kind,
            kind_label=KIND_LABELS.get(kind, kind),
            acknowledgement=acknowledgement,
            detail=detail,
            updated_at=updated,
            short_time=_short_clock(clock_source) if clock_source else "—",
            origin=origin,
            steps=steps,
            current_step=current,
        )
        parsed.append((sequence if sequence is not None else -1, order, entry))
    parsed.sort(key=lambda item: (item[0], -item[1]), reverse=True)
    return tuple(entry for _, _, entry in parsed[:10])


def timeline_summary(entries: tuple[CommandEntry, ...] | list[CommandEntry]) -> str:
    """Collapsed-summary text: the count plus the latest command and its step."""
    items = list(entries)
    if not items:
        return "0 commands"
    latest = items[0]
    if latest.sequence is not None:
        head = f"#{latest.sequence} {latest.kind_label}"
    else:
        head = f"{latest.kind_label} {latest.request_id}"
    return f"{len(items)} commands · latest {head} ({latest.current_step})"


def _command_records(control: Any) -> list[dict[str, Any]]:
    """The ``control.commands`` records as a list of mappings (tolerant)."""
    if not isinstance(control, dict):
        return []
    raw = control.get("commands")
    if isinstance(raw, dict):
        candidates = list(raw.values())
    elif isinstance(raw, list):
        candidates = list(raw)
    else:
        return []
    return [record for record in candidates if isinstance(record, dict)]


def _find_record(control: Any, request_id: str) -> dict[str, Any] | None:
    """The ``control.commands`` record for one request ID, or none."""
    for record in _command_records(control):
        if record.get("request_id") == request_id:
            return record
    return None


def _record_sequences(control: Any) -> list[int]:
    """Every integer sequence in ``control.commands`` (tolerant reader)."""
    sequences: list[int] = []
    for record in _command_records(control):
        sequence = record.get("sequence")
        if _is_int(sequence):
            sequences.append(sequence)
    return sequences


def _follow_progress(
    *, action: str, request_id: str, control: Any
) -> str | None:
    """The progress line for a tracked command (spec section 8.4).

    From the related section when its ``request_id`` matches the card's:
    handoff ``phase <phase>``, stop after ``<remaining> of <requested>
    attempt(s) left``, next issue "waiting for the next permitted claim".
    A section without a ``request_id`` field matches by action (an older
    agent); a mismatched ID or a missing section means no progress line.
    """
    if not isinstance(control, dict):
        return None
    if action == "handoff":
        handoff = control.get("handoff")
        if not isinstance(handoff, dict):
            return None
        if "request_id" in handoff and handoff.get("request_id") != request_id:
            return None
        phase = handoff.get("phase")
        if isinstance(phase, str) and phase.strip():
            return f"phase {phase.strip()}"
        return None
    if action == "stop_after":
        stop_plan = control.get("stop_plan")
        if not isinstance(stop_plan, dict):
            return None
        if "request_id" in stop_plan and stop_plan.get("request_id") != request_id:
            return None
        remaining = stop_plan.get("remaining")
        if not _is_int(remaining):
            return None
        requested = stop_plan.get("requested")
        if not _is_int(requested):
            requested = stop_plan.get("total")
        if _is_int(requested):
            return f"{remaining} of {requested} attempt(s) left"
        return f"{remaining} attempt(s) left"
    if action == "next_issue":
        pending = control.get("pending_next_issue")
        if not isinstance(pending, dict):
            return None
        if "request_id" in pending and pending.get("request_id") != request_id:
            return None
        return NEXT_ISSUE_WAITING
    return None


@dataclass(frozen=True)
class FollowResult:
    """Where one tracked command stands (spec section 8.4 "Following").

    ``outcome`` is ``following`` (an accepted card that keeps tracking),
    ``terminal`` (completed, superseded or not fulfilled: tracking stops),
    ``paused`` (Reach is not live: the message names the reach reason and
    the card keeps following), ``waiting`` (the ID is not in the latest
    good snapshot yet: the card keeps following), ``evicted`` (the ID
    fell out of the last 10: the card stops and offers Check status) or
    ``gone`` (the container was replaced or the instance is gone: the
    card stops). ``following`` mirrors whether the card keeps its
    ``sse:fleet`` track trigger; ``check_status`` whether it offers the
    Check status button.
    """

    outcome: str
    acknowledgement: str | None = None
    title: str = "Accepted"
    tone: str = "info"
    body: str = ""
    progress: str | None = None
    following: bool = False
    check_status: bool = False


def follow_command(
    *,
    action: str,
    request_id: str,
    sequence: int | None,
    card_container_id: str,
    control: Any,
    reach: str,
    row_container_id: str | None,
    row_exists: bool,
) -> FollowResult:
    """Locate one accepted command in the latest good snapshot (pure).

    Reads only the given ``control`` section and the row's Reach and
    container ID: never Docker, never the Control audit. A missing
    ``request_id`` field match, a non-integer card sequence and an
    unreadable section all read as "waiting for the next snapshot"
    (tolerant reader); only an integer card sequence below every recorded
    sequence reads as evicted.
    """
    if not row_exists or row_container_id != card_container_id:
        return FollowResult(
            outcome="gone",
            title="Accepted",
            tone="info",
            body=GONE_BODY,
            following=False,
        )
    if reach != "live":
        reason = REACH_REASONS.get(reach, f"the snapshot is {reach}")
        record = _find_record(control, request_id)
        detail = ""
        acknowledgement: str | None = None
        progress: str | None = None
        if record is not None:
            raw_ack = record.get("acknowledgement")
            acknowledgement = (
                raw_ack if isinstance(raw_ack, str) and raw_ack else None
            )
            raw_detail = record.get("detail")
            detail = raw_detail if isinstance(raw_detail, str) and raw_detail else ""
            progress = _follow_progress(
                action=action, request_id=request_id, control=control
            )
        paused = f"following paused: {reason}"
        progress = paused if progress is None else f"{progress} · {paused}"
        return FollowResult(
            outcome="paused",
            acknowledgement=acknowledgement,
            title="Accepted",
            tone="info",
            body=detail or WAITING_BODY,
            progress=progress,
            following=True,
        )
    record = _find_record(control, request_id)
    if record is not None:
        raw_ack = record.get("acknowledgement")
        acknowledgement = raw_ack if isinstance(raw_ack, str) and raw_ack else None
        raw_detail = record.get("detail")
        detail = raw_detail if isinstance(raw_detail, str) and raw_detail else ""
        body = detail or "—"
        progress = _follow_progress(
            action=action, request_id=request_id, control=control
        )
        if isinstance(acknowledgement, str) and acknowledgement in TERMINAL_ACKS:
            return FollowResult(
                outcome="terminal",
                acknowledgement=acknowledgement,
                title=TERMINAL_TITLES[acknowledgement],
                tone=TERMINAL_TONES[acknowledgement],
                body=body,
                progress=None,
                following=False,
            )
        return FollowResult(
            outcome="following",
            acknowledgement=acknowledgement,
            title="Accepted",
            tone="info",
            body=body,
            progress=progress,
            following=True,
        )
    sequences = _record_sequences(control)
    if (
        sequence is not None
        and sequences
        and all(card_seq > sequence for card_seq in sequences)
    ):
        return FollowResult(
            outcome="evicted",
            title="Accepted",
            tone="info",
            body=EVICTED_BODY,
            following=False,
            check_status=True,
        )
    return FollowResult(
        outcome="waiting",
        title="Accepted",
        tone="info",
        body=WAITING_BODY,
        following=True,
    )


__all__ = [
    "ACTION_COMMANDS",
    "ACTION_LABELS",
    "ACTION_ORDER",
    "EVICTED_BODY",
    "GONE_BODY",
    "KIND_LABELS",
    "MISSING",
    "NEXT_ISSUE_WAITING",
    "REACH_REASONS",
    "TERMINAL_ACKS",
    "TERMINAL_TITLES",
    "TERMINAL_TONES",
    "WAITING_BODY",
    "CommandEntry",
    "CommandStep",
    "ControlAction",
    "ControlAvailability",
    "ControlMode",
    "FollowResult",
    "command_timeline",
    "compute_control_mode",
    "control_availability",
    "control_slug",
    "fleet_chips",
    "follow_command",
    "format_control_age",
    "host_guard_disabled_from_env",
    "timeline_summary",
]
