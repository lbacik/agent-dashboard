"""Tolerant reader for schema-v1 telemetry snapshots.

Turns the raw snapshot dicts ``agentctl telemetry`` returned into
framework-free view models for the fleet table. Unknown fields are ignored,
so additive contract changes never break the dashboard. Templates consume
only these view models; there is no public JSON API.

Version-skew rules (from the telemetry snapshot contract):

- unknown fields are ignored;
- an unknown ``basis`` renders as ``estimated``;
- a missing field means unavailable;
- a ``null`` section (``control``, ``current_attempt``) means not applicable;
- a ``schema_version`` higher than supported keeps only the stable fields
  (``schema_version``, ``generated_at``, ``identity.repository``,
  ``identity.agent_version``) and every other cell renders as unavailable.

Unavailable figures render as "unavailable", never as zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

SUPPORTED_SCHEMA_VERSION = 1

KNOWN_BASES = frozenset({"measured", "estimated", "configured", "sdk_assumed", "unavailable"})

UNAVAILABLE = "unavailable"
NOT_APPLICABLE = "n/a"
NO_ATTEMPT = "—"

# Used / soft-threshold ratio at or above which an attempt counts as
# approaching the soft threshold (attention rank, below handoff/crossed).
APPROACH_RATIO = 0.75


def normalize_basis(basis: Any) -> str:
    """Map a figure basis to a known marker; anything unknown is estimated."""
    if isinstance(basis, str) and basis in KNOWN_BASES:
        return basis
    return "estimated"


def _figure(raw: Any) -> tuple[int | float | None, str]:
    """Extract ``(value, basis)`` from a figure object.

    A missing object, a non-numeric value, or an explicit ``null`` all mean
    unavailable; only a real number is ever shown, so unavailable figures can
    never render as zero.
    """
    if not isinstance(raw, dict):
        return None, UNAVAILABLE
    value = raw.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, UNAVAILABLE
    return value, normalize_basis(raw.get("basis"))


def _as_int(raw: Any) -> int | None:
    """An integer field value; booleans and non-integers mean absent."""
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else None


_MISSING = object()


def _section(snapshot: dict[str, Any], key: str) -> tuple[dict[str, Any] | None, bool]:
    """Return ``(section, was_null)`` for a snapshot section.

    A ``null`` section means not applicable; a missing (or malformed) section
    means unavailable, and reads as an empty dict so every field inside it is
    unavailable rather than not applicable.
    """
    raw = snapshot.get(key, _MISSING)
    if raw is None:
        return None, True
    if isinstance(raw, dict):
        return raw, False
    return {}, False


def _absent_text(was_null: bool) -> str:
    """Display text for a section that carries no usable value."""
    return NOT_APPLICABLE if was_null else UNAVAILABLE


def is_unsupported_schema(snapshot: dict[str, Any] | None) -> int | None:
    """The schema version when it is newer than supported, else None."""
    if not isinstance(snapshot, dict):
        return None
    version = _schema_version(snapshot.get("schema_version"))
    if version is not None and version > SUPPORTED_SCHEMA_VERSION:
        return version
    return None


def _schema_version(raw: Any) -> int | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


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


def _fmt_tokens(value: int | float) -> str:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"{value:,}"


@dataclass(frozen=True)
class SnapshotDetail:
    """One snapshot's view model for the fleet table (framework-free).

    Every ``*_text`` field is a final display string for the template:
    a formatted figure, "unavailable", "n/a", or "—". Templates add no
    formatting of their own beyond the budget bar widths and stale tags.
    """

    has_snapshot: bool = False
    unsupported_schema: int | None = None
    repository: str | None = None
    repository_display: str = UNAVAILABLE
    agent_version: str | None = None
    # Retained stable field: the snapshot's own timestamp. The table measures
    # age dashboard-side, but the field stays on the view model so a
    # too-new schema keeps every stable field, not just the displayed ones.
    generated_at: str | None = None
    control_present: bool = False
    current_present: bool = False
    control_stale: bool = False
    intake_text: str = UNAVAILABLE
    intake_stale: bool = False
    attempt_kind: str = UNAVAILABLE  # active | recovery | idle | unavailable | n/a
    attempt_text: str = UNAVAILABLE
    attempt_stale: bool = False
    attempt_issue: int | None = None
    attempt_phase: str | None = None
    recovery_reason: str | None = None
    handoff_active: bool = False
    soft_crossed: bool = False
    approaching_soft: bool = False
    budget_text: str = UNAVAILABLE
    budget_basis: str = ""
    budget_hatched: bool = False
    budget_used_pct: float | None = None
    budget_soft_pct: float | None = None
    budget_has_bar: bool = False
    budget_has_tick: bool = False
    context_text: str = UNAVAILABLE
    context_basis: str = ""
    turns_text: str = UNAVAILABLE
    turns_basis: str = ""
    cache_text: str = UNAVAILABLE
    cache_basis: str = ""
    errors_text: str = UNAVAILABLE
    errors_stale: bool = False
    errors_count: int | None = None
    errors_max: int | None = None
    # Raw figures kept for headline and attention-rank derivation.
    budget_used_basis: str = UNAVAILABLE
    turns_basis_raw: str = UNAVAILABLE


def _stable_fields(snapshot: dict[str, Any]) -> tuple[str | None, str | None]:
    identity = snapshot.get("identity")
    identity = identity if isinstance(identity, dict) else {}
    repository = identity.get("repository")
    agent_version = identity.get("agent_version")
    return (
        repository if isinstance(repository, str) else None,
        agent_version if isinstance(agent_version, str) else None,
    )


def read_snapshot(
    snapshot: dict[str, Any] | None,
    *,
    snapshot_at: float | None,
    now: float,
    poll_interval: float = 5.0,
) -> SnapshotDetail:
    """Read one raw snapshot into a view model, tolerating version skew."""
    if not isinstance(snapshot, dict):
        return SnapshotDetail()
    repository, agent_version = _stable_fields(snapshot)
    generated_raw = snapshot.get("generated_at")
    generated_at = generated_raw if isinstance(generated_raw, str) else None
    unsupported = is_unsupported_schema(snapshot)
    if unsupported is not None:
        return SnapshotDetail(
            has_snapshot=True,
            unsupported_schema=unsupported,
            repository=repository,
            repository_display=repository or UNAVAILABLE,
            agent_version=agent_version,
            generated_at=generated_at,
        )

    control, control_was_null = _section(snapshot, "control")
    current, current_was_null = _section(snapshot, "current_attempt")
    control_present = control is not None
    current_present = current is not None

    # Control staleness is wall-clock: the control section is stale when its
    # own timestamp is older than one poll interval while the snapshot itself
    # just arrived. Agent containers share the host clock, so no agent-side
    # timestamp is compared against another.
    observed = _parse_time(control.get("control_observed_at")) if control else None
    snapshot_fresh = snapshot_at is not None and (now - snapshot_at) <= poll_interval
    control_old = observed is not None and (now - observed) > poll_interval
    control_stale = snapshot_fresh and control_old

    # Intake (control-derived).
    if control is None:
        intake_text = NOT_APPLICABLE
    elif not control:
        intake_text = UNAVAILABLE
    else:
        intake = control.get("intake")
        intake_text = intake if isinstance(intake, str) and intake else UNAVAILABLE

    # Consecutive errors (control-derived).
    errors = control.get("consecutive_errors") if control else None
    errors = errors if isinstance(errors, dict) else None
    if control is None:
        errors_text = NOT_APPLICABLE
        errors_count, errors_max = None, None
    elif errors is None:
        errors_text, errors_count, errors_max = UNAVAILABLE, None, None
    else:
        errors_count = _as_int(errors.get("count"))
        errors_max = _as_int(errors.get("max_consecutive_errors"))
        if errors_count is None and errors_max is None:
            errors_text = UNAVAILABLE
        elif errors_max is None:
            errors_text = str(errors_count)
        elif errors_count is None:
            errors_text = UNAVAILABLE
        else:
            errors_text = f"{errors_count} / {errors_max}"

    # Attempt: the recovery hold, the active attempt, or idle.
    recovery = control.get("recovery") if control else None
    recovery = recovery if isinstance(recovery, dict) else None
    active = control.get("active_attempt") if control else None
    active = active if isinstance(active, dict) else None
    current_issue = _as_int(current.get("issue_number")) if current else None
    attempt_kind, attempt_text = UNAVAILABLE, UNAVAILABLE
    attempt_from_control = False
    attempt_issue, attempt_phase, recovery_reason = None, None, None
    if recovery is not None:
        attempt_issue = _as_int(recovery.get("issue_number"))
        phase = recovery.get("phase")
        attempt_phase = phase if isinstance(phase, str) and phase else None
        reason = recovery.get("hold_reason")
        recovery_reason = reason if isinstance(reason, str) and reason else None
        if attempt_issue is not None and attempt_phase:
            attempt_text = f"Recovery hold #{attempt_issue} · {attempt_phase}"
        elif attempt_issue is not None:
            attempt_text = f"Recovery hold #{attempt_issue}"
        else:
            attempt_text = "Recovery hold"
        attempt_kind, attempt_from_control = "recovery", True
    elif active is not None:
        attempt_issue = _as_int(active.get("issue_number"))
        phase = active.get("phase")
        attempt_phase = phase if isinstance(phase, str) and phase else None
        if attempt_issue is not None and attempt_phase:
            attempt_text = f"#{attempt_issue} · {attempt_phase}"
        elif attempt_issue is not None:
            attempt_text = f"#{attempt_issue}"
        else:
            attempt_text = UNAVAILABLE
        attempt_kind, attempt_from_control = "active", True
    elif current_issue is not None:
        attempt_issue = current_issue
        attempt_text = f"#{current_issue}"
        attempt_kind, attempt_from_control = "active", False
    elif "control" not in snapshot and "current_attempt" not in snapshot:
        attempt_kind, attempt_text = UNAVAILABLE, UNAVAILABLE
    elif control_was_null:
        # A null control section means not applicable, and control is the
        # authority on attempt state.
        attempt_kind, attempt_text = NOT_APPLICABLE, NOT_APPLICABLE
    else:
        # No attempt anywhere: a null current section says so explicitly,
        # and present-but-empty sections carry no attempt either.
        attempt_kind, attempt_text = "idle", NO_ATTEMPT
        attempt_from_control = control_present

    # Handoff in progress: the handoff object or an accepted handoff command.
    handoff_active = False
    if control:
        handoff = control.get("handoff")
        handoff_active = isinstance(handoff, dict)
        if not handoff_active:
            pending = control.get("pending_command")
            handoff_active = (
                isinstance(pending, dict) and pending.get("kind") == "handoff"
            )

    # Budget bar (current-attempt figures, never control-stale).
    budget_text, budget_basis = UNAVAILABLE, ""
    budget_hatched = False
    used_pct, soft_pct = None, None
    has_bar, has_tick = False, False
    soft_crossed, approaching = False, False
    used_basis = UNAVAILABLE
    budget = current.get("budget") if current else None
    budget = budget if isinstance(budget, dict) else None
    if current is None:
        budget_text = _absent_text(current_was_null)
    elif budget is not None:
        used_value, used_basis = _figure(budget.get("used"))
        hard_value, _ = _figure(budget.get("hard_ceiling"))
        soft = budget.get("soft_threshold")
        soft = soft if isinstance(soft, dict) else {}
        soft_value, _ = _figure(soft)
        crossed = soft.get("crossed")
        soft_crossed = crossed is True
        measured_value, _ = _figure(budget.get("measured"))
        estimated_value, _ = _figure(budget.get("estimated"))
        # Partly estimated: a positive estimated remainder, or a used figure
        # whose basis is estimated while no breakdown is given.
        budget_hatched = (estimated_value or 0) > 0 or used_basis == "estimated"
        if used_value is not None and hard_value is not None and hard_value > 0:
            budget_text = f"{_fmt_tokens(used_value)} / {_fmt_tokens(hard_value)}"
            budget_basis = used_basis if used_basis != UNAVAILABLE else ""
            used_pct = round(min(100.0, max(0.0, used_value / hard_value * 100.0)), 1)
            has_bar = True
            if soft_value is not None and soft_value > 0:
                soft_pct = round(
                    min(100.0, max(0.0, soft_value / hard_value * 100.0)), 1
                )
                has_tick = True
        denominator = soft_value if soft_value else hard_value
        if (
            not soft_crossed
            and used_value is not None
            and denominator is not None
            and denominator > 0
            and used_value / denominator >= APPROACH_RATIO
        ):
            approaching = True

    # Main context plus the number of running subagents.
    context_text, context_basis = UNAVAILABLE, ""
    if current is None:
        context_text = _absent_text(current_was_null)
    elif current is not None:
        context = current.get("context")
        context = context if isinstance(context, dict) else None
        if context is not None:
            main_value, main_basis = _figure(context.get("main"))
            subagents = context.get("subagents")
            running: int | None = None
            if isinstance(subagents, list):
                running = sum(
                    1 for s in subagents if isinstance(s, dict) and s.get("running") is True
                )
            if main_value is not None:
                context_text = _fmt_tokens(main_value)
                if running:
                    context_text += f" (+{running})"
                context_basis = main_basis if main_basis != UNAVAILABLE else ""

    # Turns.
    turns_text, turns_basis = UNAVAILABLE, ""
    if current is None:
        turns_text = _absent_text(current_was_null)
    elif current is not None:
        turns = current.get("turns")
        turns_value, turns_basis_raw = _figure(turns if isinstance(turns, dict) else None)
        if turns_value is not None:
            turns_text = _fmt_tokens(turns_value)
            turns_basis = turns_basis_raw if turns_basis_raw != UNAVAILABLE else ""
        else:
            turns_basis = ""

    # Cache hit rate.
    cache_text, cache_basis = UNAVAILABLE, ""
    if current is None:
        cache_text = _absent_text(current_was_null)
    elif current is not None:
        cache = current.get("cache")
        cache = cache if isinstance(cache, dict) else {}
        hit = cache.get("main_hit_rate")
        hit_value, hit_basis = _figure(hit if isinstance(hit, dict) else None)
        if hit_value is not None:
            cache_text = f"{hit_value * 100:.0f}%"
            cache_basis = hit_basis if hit_basis != UNAVAILABLE else ""

    return SnapshotDetail(
        has_snapshot=True,
        repository=repository,
        repository_display=repository or UNAVAILABLE,
        agent_version=agent_version,
        generated_at=generated_at,
        control_present=control_present,
        current_present=current_present,
        control_stale=control_stale,
        intake_text=intake_text,
        intake_stale=control_stale and control_present,
        attempt_kind=attempt_kind,
        attempt_text=attempt_text,
        attempt_stale=control_stale and attempt_from_control,
        attempt_issue=attempt_issue,
        attempt_phase=attempt_phase,
        recovery_reason=recovery_reason,
        handoff_active=handoff_active,
        soft_crossed=soft_crossed,
        approaching_soft=approaching,
        budget_text=budget_text,
        budget_basis=budget_basis,
        budget_hatched=budget_hatched,
        budget_used_pct=used_pct,
        budget_soft_pct=soft_pct,
        budget_has_bar=has_bar,
        budget_has_tick=has_tick,
        context_text=context_text,
        context_basis=context_basis,
        turns_text=turns_text,
        turns_basis=turns_basis,
        cache_text=cache_text,
        cache_basis=cache_basis,
        errors_text=errors_text,
        errors_stale=control_stale and control_present,
        errors_count=errors_count,
        errors_max=errors_max,
        budget_used_basis=used_basis,
        turns_basis_raw=turns_basis or UNAVAILABLE,
    )
