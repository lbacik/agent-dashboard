"""Status Snapshot view models for the contract path (ADR 0014).

Turns the ``result`` of a good contract ``status`` reply into
framework-free view models for the fleet table: the Reach inputs, the
Status headline and the Attention rank. Parsing is lenient (spec section
5): unknown fields are ignored and unknown enum values read as
``"unknown"``; anything structurally incoherent reads as no snapshot
rather than breaking the row. Templates consume only these view models;
there is no public JSON API.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_operator_contract import status as contract_status_lib

UNKNOWN = "unknown"

UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ContractDetail:
    """One contract status snapshot's view model (framework-free).

    Every ``*_text`` field is a final display string for the template.
    ``has_snapshot`` is false when the last poll carried no parsable
    snapshot; the row then falls back to the generic reach headline.
    """

    has_snapshot: bool = False
    backend: str | None = None
    repository: str | None = None
    repository_display: str = UNAVAILABLE
    agent_version: str | None = None
    # ``instance.state`` as parsed (starting, recovering, idle, active,
    # draining, paused, stopped, or ``"unknown"`` for a future value).
    instance_state: str = UNKNOWN
    startup_check: str | None = None
    last_halt_reason: str | None = None
    last_halt_detail: str | None = None
    last_halt_at: str | None = None
    pending_stop: bool = False
    stop_plan_remaining: int | None = None
    current_issue: int | None = None
    current_phase: str | None = None
    last_finished_issue: int | None = None
    last_finished_label: str | None = None
    last_finished_attention: bool = False
    intake_state: str | None = None
    # Short attempt text for the rail (``#42 · run_step`` or ``—``).
    attempt_text: str = "—"


def read_contract_snapshot(result: Any) -> ContractDetail | None:
    """Read one contract ``status`` result into a view model; never raises.

    Returns none when the result is not a parsable Status Snapshot (not a
    dict, or structurally incoherent even leniently): the row keeps its
    poll outcome and renders the generic reach headline instead.
    """
    if not isinstance(result, dict):
        return None
    try:
        snapshot = contract_status_lib.parse_status_lenient(result)
    except Exception:
        return None
    halt = snapshot.instance.last_halt
    current = snapshot.current_attempt
    last = snapshot.last_finished
    if current is not None:
        attempt_text = f"#{current.issue_number} · {current.native_phase}"
    else:
        attempt_text = "—"
    return ContractDetail(
        has_snapshot=True,
        backend=snapshot.identity.backend,
        repository=snapshot.identity.repository,
        repository_display=snapshot.identity.repository or UNAVAILABLE,
        agent_version=snapshot.identity.agent_version,
        instance_state=snapshot.instance.state,
        startup_check=snapshot.instance.startup_check,
        last_halt_reason=halt.reason if halt is not None else None,
        last_halt_detail=halt.detail if halt is not None else None,
        last_halt_at=halt.at if halt is not None else None,
        pending_stop=snapshot.intake.pending_stop is not None,
        stop_plan_remaining=(
            snapshot.intake.stop_plan.remaining
            if snapshot.intake.stop_plan is not None
            else None
        ),
        current_issue=current.issue_number if current is not None else None,
        current_phase=current.native_phase if current is not None else None,
        last_finished_issue=last.issue_number if last is not None else None,
        last_finished_label=last.outcome_label if last is not None else None,
        last_finished_attention=(
            last.attention_required if last is not None else False
        ),
        intake_state=snapshot.intake.state,
        attempt_text=attempt_text,
    )


def contract_mismatch_headline(supported: Any) -> str:
    """Headline for a contract mismatch, naming what the instance supports."""
    return (
        "Contract mismatch: the instance supports "
        f"{supported!r} while this dashboard speaks contract 1"
    )


def contract_headline(detail: ContractDetail) -> str | None:
    """The snapshot-derived headline; none when the state says nothing.

    Precedence: a pending stop or stop plan (operator intent in flight),
    then the last Halt, then a last finished attempt needing the operator,
    then ``instance.state``. An ``"unknown"`` state reads as nothing so the
    caller falls back to the generic live headline.
    """
    if detail.pending_stop:
        return "Stop requested"
    if detail.stop_plan_remaining is not None:
        return f"Stop planned ({detail.stop_plan_remaining} remaining)"
    if detail.last_halt_reason is not None:
        halted = f"Halted ({detail.last_halt_reason})"
        if detail.last_halt_detail:
            halted += f": {detail.last_halt_detail}"
        return halted
    if detail.last_finished_attention:
        issue = (
            f" #{detail.last_finished_issue}"
            if detail.last_finished_issue is not None
            else ""
        )
        label = f" — {detail.last_finished_label}" if detail.last_finished_label else ""
        return f"Needs operator:{issue}{label}"
    state = detail.instance_state
    if state == "starting":
        if detail.startup_check:
            return f"Starting: {detail.startup_check}"
        return "Starting"
    if state == "recovering":
        return "Recovering"
    if state == "idle":
        return "Idle: waiting for a ready issue"
    if state == "active":
        if detail.current_issue is not None and detail.current_phase:
            return f"Working on #{detail.current_issue} · {detail.current_phase}"
        if detail.current_phase:
            return f"Working · {detail.current_phase}"
        return "Working"
    if state == "draining":
        if detail.current_issue is not None:
            return f"Draining · #{detail.current_issue}"
        return "Draining"
    if state == "paused":
        return "Paused"
    return None


def needs_operator(detail: ContractDetail) -> bool:
    """True when the snapshot itself needs the operator (halt or attention)."""
    return (
        detail.last_halt_reason is not None or detail.last_finished_attention
    )


def contract_state_rank(instance_state: str) -> int | None:
    """The attention tier for an ``instance.state``; none when unknown."""
    if instance_state in ("starting", "recovering"):
        return 1
    if instance_state in ("active", "draining"):
        return 5
    if instance_state in ("idle", "paused"):
        return 6
    return None
