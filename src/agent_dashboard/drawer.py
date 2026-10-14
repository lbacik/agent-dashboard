"""Instance drawer view model: current attempt, control, recent events.

Reads the raw telemetry snapshot dict into framework-free view models for
the ``/instances/{name}`` fragment. Like the fleet table reader
(``snapshot.py``) it is tolerant: unknown fields are ignored, an unknown
``basis`` renders as ``estimated``, a missing field means unavailable, and a
``null`` section means not applicable. A ``schema_version`` higher than
supported keeps only the stable fields.

Every numeric figure is a :class:`Figure` pairing the final display text with
its basis marker, so templates add no formatting of their own. No USD, no
charts: token counts only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .snapshot import (
    NOT_APPLICABLE,
    UNAVAILABLE,
    is_unsupported_schema,
    normalize_basis,
)

_MISSING = object()


@dataclass(frozen=True)
class Figure:
    """One numeric figure: final display text plus its basis marker."""

    text: str = UNAVAILABLE
    basis: str = UNAVAILABLE


@dataclass(frozen=True)
class TokenCategory:
    """One row of the tokens-by-category breakdown."""

    name: str
    figure: Figure


@dataclass(frozen=True)
class SubagentView:
    """One subagent context row."""

    slot: str
    kind: str
    figure: Figure
    running: bool


@dataclass(frozen=True)
class EventView:
    """One recent-events row: timestamp, level, event name, one-line text."""

    timestamp: str
    level: str
    name: str
    text: str


def _fig(raw: Any, *, missing: str = UNAVAILABLE) -> Figure:
    """Extract a figure from ``{"value": n, "basis": b}``.

    A missing object, a non-numeric value, or an explicit ``null`` renders as
    ``missing`` (``unavailable``, or ``n/a`` inside a null section); only a
    real number is ever shown, so unavailable figures never render as zero.
    """
    if not isinstance(raw, dict):
        return Figure(missing, "" if missing == NOT_APPLICABLE else UNAVAILABLE)
    value = raw.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return Figure(missing, "" if missing == NOT_APPLICABLE else UNAVAILABLE)
    return Figure(_fmt_tokens(value), normalize_basis(raw.get("basis")))


def _fmt_tokens(value: int | float) -> str:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"{value:,}"


def _str(raw: Any) -> str | None:
    return raw if isinstance(raw, str) and raw else None


def _int(raw: Any) -> int | None:
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else None


def _section(snapshot: dict[str, Any], key: str) -> tuple[dict[str, Any] | None, str]:
    """Return ``(section, state)``: ``"present"``, ``"na"`` (null) or ``"missing"``."""
    raw = snapshot.get(key, _MISSING)
    if raw is None:
        return None, "na"
    if isinstance(raw, dict):
        return raw, "present"
    return {}, "missing"


def _missing_text(state: str) -> str:
    return NOT_APPLICABLE if state == "na" else UNAVAILABLE


@dataclass(frozen=True)
class DrawerDetail:
    """One snapshot's view model for the instance drawer fragment."""

    has_snapshot: bool = False
    unsupported_schema: int | None = None
    repository: str = UNAVAILABLE
    agent_version: str = UNAVAILABLE
    truncated: bool = False
    # Current attempt.
    current_state: str = "missing"  # active | na | missing
    attempt_id: str = UNAVAILABLE
    issue_number: int | None = None
    issue_title: str = UNAVAILABLE
    observed_at: str = UNAVAILABLE
    budget_used: Figure = field(default_factory=Figure)
    budget_measured: Figure = field(default_factory=Figure)
    budget_estimated: Figure = field(default_factory=Figure)
    hard_ceiling: Figure = field(default_factory=Figure)
    hard_reached: bool | None = None
    soft_threshold: Figure = field(default_factory=Figure)
    soft_rule: str = UNAVAILABLE
    soft_crossed: bool = False
    crossed_at: str = UNAVAILABLE
    crossed_at_tokens: Figure = field(default_factory=Figure)
    budget_used_pct: float | None = None
    budget_soft_pct: float | None = None
    budget_has_bar: bool = False
    budget_has_tick: bool = False
    budget_hatched: bool = False
    turns: Figure = field(default_factory=Figure)
    turns_max: Figure = field(default_factory=Figure)
    continuation_index: Figure = field(default_factory=Figure)
    continuation_max: Figure = field(default_factory=Figure)
    cache_hit: Figure = field(default_factory=Figure)
    cache_counted: int | None = None
    tokens: tuple[TokenCategory, ...] = ()
    ctx_main: Figure = field(default_factory=Figure)
    ctx_peak: Figure = field(default_factory=Figure)
    ctx_window: Figure = field(default_factory=Figure)
    ctx_auto_compact: Figure = field(default_factory=Figure)
    compactions_count: str = UNAVAILABLE
    compactions_last_at: str | None = None
    compactions_last_trigger: str | None = None
    compactions_last_pre: Figure = field(default_factory=Figure)
    compactions_last_post: Figure = field(default_factory=Figure)
    subagents: tuple[SubagentView, ...] = ()
    rate_limit: Figure = field(default_factory=Figure)
    rate_limit_reason: str | None = None
    # Control.
    control_state: str = "missing"  # present | na | missing
    intake: str = UNAVAILABLE
    intake_stale: bool = False
    active_issue: int | None = None
    active_title: str | None = None
    active_branch: str | None = None
    active_phase: str | None = None
    active_started: str | None = None
    active_text: str = UNAVAILABLE
    stop_plan: str = UNAVAILABLE
    handoff_present: bool = False
    handoff_source: str | None = None  # handoff | pending_command
    handoff_phase: str | None = None
    handoff_model_deadline: str | None = None
    handoff_publication_deadline: str | None = None
    handoff_detail: str | None = None
    handoff_begun: bool | None = None
    handoff_text: str = UNAVAILABLE
    recovery_present: bool = False
    recovery_issue: int | None = None
    recovery_phase: str | None = None
    recovery_branch: str | None = None
    recovery_reason: str | None = None
    recovery_next_action: str | None = None
    errors_text: str = UNAVAILABLE
    # Last finished attempt (shown even while an attempt is active).
    last_present: bool = False
    last_issue: int | None = None
    last_title: str | None = None
    last_outcome: str | None = None
    last_tokens: Figure = field(default_factory=Figure)
    last_turns: Figure = field(default_factory=Figure)
    last_finished_at: str | None = None
    last_pr_number: int | None = None
    last_pr_url: str | None = None
    # Recent events.
    events: tuple[EventView, ...] = ()


def _read_budget(current: dict[str, Any] | None, state: str) -> dict[str, Any]:
    missing = _missing_text(state)
    out: dict[str, Any] = {
        "used": Figure(missing, "" if state == "na" else UNAVAILABLE),
        "measured": Figure(missing, "" if state == "na" else UNAVAILABLE),
        "estimated": Figure(missing, "" if state == "na" else UNAVAILABLE),
        "hard": Figure(missing, "" if state == "na" else UNAVAILABLE),
        "reached": None,
        "soft": Figure(missing, "" if state == "na" else UNAVAILABLE),
        "rule": missing,
        "crossed": False,
        "crossed_at": missing,
        "crossed_tokens": Figure(missing, "" if state == "na" else UNAVAILABLE),
        "used_pct": None,
        "soft_pct": None,
        "has_bar": False,
        "has_tick": False,
        "hatched": False,
    }
    if not current:
        return out
    budget = current.get("budget")
    budget = budget if isinstance(budget, dict) else None
    if budget is None:
        return out
    used_value = _num(budget.get("used"))
    out["used"] = _fig(budget.get("used"), missing=missing)
    out["measured"] = _fig(budget.get("measured"), missing=missing)
    out["estimated"] = _fig(budget.get("estimated"), missing=missing)
    out["hard"] = _fig(budget.get("hard_ceiling"), missing=missing)
    reached = budget.get("hard_ceiling_reached")
    out["reached"] = reached if isinstance(reached, bool) else None
    soft = budget.get("soft_threshold")
    soft = soft if isinstance(soft, dict) else {}
    out["soft"] = _fig(soft, missing=missing)
    rule = _str(soft.get("rule"))
    out["rule"] = rule or missing
    out["crossed"] = soft.get("crossed") is True
    crossed_at = _str(soft.get("crossed_at"))
    out["crossed_at"] = crossed_at or missing
    crossed_tokens = soft.get("crossed_at_tokens")
    if isinstance(crossed_tokens, bool) or not isinstance(crossed_tokens, (int, float)):
        out["crossed_tokens"] = Figure(missing, "" if state == "na" else UNAVAILABLE)
    else:
        # A recorded crossing point: a plain token count with no basis of its
        # own, so it carries the soft threshold figure's measured reading.
        out["crossed_tokens"] = Figure(_fmt_tokens(crossed_tokens), "measured")
    hard_value = _num(budget.get("hard_ceiling"))
    soft_value = _num(soft)
    estimated_value = _num(budget.get("estimated"))
    if out["used"].basis == "estimated" or (estimated_value or 0) > 0:
        out["hatched"] = True
    if used_value is not None and hard_value is not None and hard_value > 0:
        out["used_pct"] = round(min(100.0, max(0.0, used_value / hard_value * 100.0)), 1)
        out["has_bar"] = True
        if soft_value is not None and soft_value > 0:
            out["soft_pct"] = round(
                min(100.0, max(0.0, soft_value / hard_value * 100.0)), 1
            )
            out["has_tick"] = True
    return out


def _num(raw: Any) -> int | float | None:
    if not isinstance(raw, dict):
        return None
    value = raw.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _read_context(current: dict[str, Any], missing: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "main": Figure(missing, UNAVAILABLE if missing != NOT_APPLICABLE else ""),
        "peak": Figure(missing, UNAVAILABLE if missing != NOT_APPLICABLE else ""),
        "window": Figure(missing, UNAVAILABLE if missing != NOT_APPLICABLE else ""),
        "auto": Figure(missing, UNAVAILABLE if missing != NOT_APPLICABLE else ""),
        "comp_count": missing,
        "last_at": None,
        "last_trigger": None,
        "last_pre": Figure(missing, UNAVAILABLE if missing != NOT_APPLICABLE else ""),
        "last_post": Figure(missing, UNAVAILABLE if missing != NOT_APPLICABLE else ""),
        "subagents": (),
    }
    context = current.get("context")
    context = context if isinstance(context, dict) else None
    if context is None:
        return out
    out["main"] = _fig(context.get("main"), missing=missing)
    out["peak"] = _fig(context.get("main_peak"), missing=missing)
    out["window"] = _fig(context.get("window"), missing=missing)
    out["auto"] = _fig(context.get("auto_compact_threshold"), missing=missing)
    compactions = context.get("compactions")
    compactions = compactions if isinstance(compactions, dict) else {}
    count = _int(compactions.get("count"))
    out["comp_count"] = str(count) if count is not None else missing
    last = compactions.get("last")
    last = last if isinstance(last, dict) else None
    if last is not None:
        out["last_at"] = _str(last.get("at"))
        out["last_trigger"] = _str(last.get("trigger"))
        out["last_pre"] = _fig(last.get("pre_tokens"), missing=missing)
        out["last_post"] = _fig(last.get("post_tokens"), missing=missing)
    subs = context.get("subagents")
    rows: list[SubagentView] = []
    if isinstance(subs, list):
        for index, sub in enumerate(subs):
            if not isinstance(sub, dict):
                continue
            slot = _str(sub.get("id")) or f"sub-{index}"
            kind = _str(sub.get("type")) or UNAVAILABLE
            rows.append(
                SubagentView(
                    slot=slot,
                    kind=kind,
                    figure=_fig(sub.get("context"), missing=missing),
                    running=sub.get("running") is True,
                )
            )
    out["subagents"] = tuple(rows)
    return out


def _stop_plan_text(control: dict[str, Any] | None, state: str) -> str:
    if control is None:
        return _missing_text(state)
    if "stop_plan" not in control:
        return UNAVAILABLE
    raw = control["stop_plan"]
    if raw is None:
        return "none"
    if isinstance(raw, str):
        return raw or "none"
    if isinstance(raw, dict):
        if not raw:
            return "none"
        parts = []
        for key in sorted(raw):
            value = raw[key]
            if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                parts.append(f"{key}: {value}")
            elif value is None:
                parts.append(f"{key}: —")
        return "; ".join(parts) if parts else "none"
    return UNAVAILABLE


def _handoff_text(
    phase: str | None,
    model_deadline: str | None,
    publication_deadline: str | None,
    detail: str | None,
) -> str:
    """One line for an in-progress handoff; never renders a bare None."""
    bits = []
    if phase:
        bits.append(f"phase {phase}")
    if model_deadline:
        bits.append(f"model deadline {model_deadline}")
    if publication_deadline:
        bits.append(f"publication deadline {publication_deadline}")
    if detail:
        bits.append(detail)
    return " · ".join(bits) if bits else "in progress"


def read_drawer(snapshot: dict[str, Any] | None, *, control_stale: bool = False) -> DrawerDetail:
    """Read one raw snapshot into the drawer view model.

    ``control_stale`` is the wall-clock staleness the caller derived (the
    control section's own timestamp against the poll interval); control
    figures keep their stale tag from it.
    """
    if not isinstance(snapshot, dict):
        return DrawerDetail()
    identity = snapshot.get("identity")
    identity = identity if isinstance(identity, dict) else {}
    repository = _str(identity.get("repository")) or UNAVAILABLE
    agent_version = _str(identity.get("agent_version")) or UNAVAILABLE
    unsupported = is_unsupported_schema(snapshot)
    if unsupported is not None:
        return DrawerDetail(
            has_snapshot=True,
            unsupported_schema=unsupported,
            repository=repository,
            agent_version=agent_version,
        )

    current, current_state = _section(snapshot, "current_attempt")
    control, control_state = _section(snapshot, "control")
    missing_current = _missing_text(current_state)

    budget = _read_budget(current, current_state)
    ctx = (
        _read_context(current, missing_current)
        if current is not None
        else _read_context({}, missing_current)
    )

    turns = Figure(missing_current, "" if current_state == "na" else UNAVAILABLE)
    turns_max = Figure(missing_current, "" if current_state == "na" else UNAVAILABLE)
    cont_index = Figure(missing_current, "" if current_state == "na" else UNAVAILABLE)
    cont_max = Figure(missing_current, "" if current_state == "na" else UNAVAILABLE)
    cache_hit = Figure(missing_current, "" if current_state == "na" else UNAVAILABLE)
    cache_counted: int | None = None
    token_rows: tuple[TokenCategory, ...] = ()
    rate_limit = Figure(missing_current, "" if current_state == "na" else UNAVAILABLE)
    rate_reason: str | None = None
    issue_number: int | None = None
    if current is not None:
        issue_number = _int(current.get("issue_number"))
        turns_raw = current.get("turns")
        turns = _fig(turns_raw, missing=missing_current)
        turns_max = _fig(
            turns_raw.get("max") if isinstance(turns_raw, dict) else None,
            missing=missing_current,
        )
        cont = current.get("continuation")
        cont = cont if isinstance(cont, dict) else {}
        cont_index = _fig(cont.get("index"), missing=missing_current)
        cont_max = _fig(cont.get("max"), missing=missing_current)
        cache = current.get("cache")
        cache = cache if isinstance(cache, dict) else {}
        hit_raw = cache.get("main_hit_rate")
        if isinstance(hit_raw, dict):
            raw_value = hit_raw.get("value")
            if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
                cache_hit = Figure(
                    missing_current, "" if current_state == "na" else UNAVAILABLE
                )
            else:
                cache_hit = Figure(
                    f"{raw_value * 100:.0f}%", normalize_basis(hit_raw.get("basis"))
                )
            counted = _int(hit_raw.get("counted"))
            cache_counted = counted
        else:
            cache_hit = Figure(
                missing_current, "" if current_state == "na" else UNAVAILABLE
            )
        cats = current.get("tokens_by_category")
        cats = cats if isinstance(cats, dict) else None
        if cats is not None:
            rows = []
            for key in (
                "input_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
                "output_tokens",
            ):
                rows.append(
                    TokenCategory(name=key, figure=_fig(cats.get(key), missing=missing_current))
                )
            token_rows = tuple(rows)
        rate_raw = current.get("provider_rate_limit")
        rate_limit = _fig(rate_raw, missing=missing_current)
        if isinstance(rate_raw, dict):
            rate_reason = _str(rate_raw.get("reason"))

    # Control.
    missing_control = _missing_text(control_state)
    intake = missing_control
    intake_stale = False
    active_issue = active_title = active_branch = active_phase = active_started = None
    active_text = "none" if control is not None else missing_control
    handoff_text = "none" if control is not None else missing_control
    handoff_present = False
    handoff_source = handoff_phase = None
    handoff_model_deadline = handoff_pub_deadline = handoff_detail = None
    handoff_begun: bool | None = None
    recovery_present = False
    recovery_issue = recovery_phase = recovery_branch = None
    recovery_reason = recovery_next_action = None
    errors_text = missing_control
    if control is not None:
        intake = _str(control.get("intake")) or missing_control
        intake_stale = control_stale
        active = control.get("active_attempt")
        active = active if isinstance(active, dict) else None
        if active is not None:
            active_issue = _int(active.get("issue_number"))
            active_title = _str(active.get("issue_title"))
            active_branch = _str(active.get("branch"))
            active_phase = _str(active.get("phase"))
            active_started = _str(active.get("started_at"))
            bits = []
            if active_issue is not None:
                bits.append(f"#{active_issue}")
            for bit in (active_title, active_branch, active_phase):
                if bit:
                    bits.append(bit)
            if active_started:
                bits.append(f"started {active_started}")
            active_text = " · ".join(bits) if bits else "none"
        handoff = control.get("handoff")
        if isinstance(handoff, dict):
            handoff_present = True
            handoff_source = "handoff"
            handoff_phase = _str(handoff.get("phase"))
            handoff_model_deadline = _str(handoff.get("model_deadline_at"))
            handoff_pub_deadline = _str(handoff.get("publication_deadline_at"))
            handoff_detail = _str(handoff.get("detail"))
            begun = handoff.get("begun")
            handoff_begun = begun if isinstance(begun, bool) else None
            handoff_text = _handoff_text(
                handoff_phase, handoff_model_deadline, handoff_pub_deadline, handoff_detail
            )
        else:
            pending = control.get("pending_command")
            if isinstance(pending, dict) and pending.get("kind") == "handoff":
                handoff_present = True
                handoff_source = "pending_command"
                handoff_detail = _str(pending.get("detail"))
                handoff_text = _handoff_text(None, None, None, handoff_detail)
        recovery = control.get("recovery")
        recovery = recovery if isinstance(recovery, dict) else None
        if recovery is not None:
            recovery_present = True
            recovery_issue = _int(recovery.get("issue_number"))
            recovery_phase = _str(recovery.get("phase"))
            recovery_branch = _str(recovery.get("branch"))
            recovery_reason = _str(recovery.get("hold_reason"))
            recovery_next_action = _str(recovery.get("next_action"))
        errors = control.get("consecutive_errors")
        errors = errors if isinstance(errors, dict) else None
        if errors is None:
            errors_text = missing_control
        else:
            count = _int(errors.get("count"))
            max_errors = _int(errors.get("max_consecutive_errors"))
            if count is None and max_errors is None:
                errors_text = UNAVAILABLE
            elif max_errors is None:
                errors_text = str(count)
            elif count is None:
                errors_text = UNAVAILABLE
            else:
                errors_text = f"{count} / {max_errors}"
    stop_plan = _stop_plan_text(control, control_state)

    # Last finished attempt, even while an attempt is active.
    last_present = False
    last_issue = last_title = last_outcome = None
    last_tokens = Figure(UNAVAILABLE, UNAVAILABLE)
    last_turns = Figure(UNAVAILABLE, UNAVAILABLE)
    last_finished_at = last_pr_url = None
    last_pr_number: int | None = None
    last = snapshot.get("last_finished")
    last = last if isinstance(last, dict) else None
    if last is not None:
        last_present = True
        last_issue = _int(last.get("issue_number"))
        last_title = _str(last.get("issue_title"))
        last_outcome = _str(last.get("outcome"))
        last_tokens = _fig(last.get("budget_used"))
        last_turns = _fig(last.get("turns"))
        last_finished_at = _str(last.get("finished_at"))
        pr = last.get("pull_request")
        pr = pr if isinstance(pr, dict) else None
        if pr is not None:
            last_pr_number = _int(pr.get("number"))
            last_pr_url = _str(pr.get("url"))

    # Recent events.
    events: list[EventView] = []
    recent = snapshot.get("recent_events")
    if isinstance(recent, list):
        for item in recent:
            if not isinstance(item, dict):
                continue
            events.append(
                EventView(
                    timestamp=_str(item.get("timestamp")) or "—",
                    level=_str(item.get("level")) or "—",
                    name=_str(item.get("event")) or "—",
                    text=_str(item.get("text")) or "—",
                )
            )

    truncated = snapshot.get("truncated") is True

    # An empty-but-present current section still counts as active: its
    # fields render unavailable rather than n/a.
    current_state_out = "active" if current is not None else current_state
    return DrawerDetail(
        has_snapshot=True,
        repository=repository,
        agent_version=agent_version,
        truncated=truncated,
        current_state=current_state_out,
        attempt_id=_str(current.get("attempt_id")) if current else missing_current,
        issue_number=issue_number,
        issue_title=(_str(current.get("issue_title")) if current else None)
        or missing_current,
        observed_at=(_str(current.get("observed_at")) if current else None)
        or missing_current,
        budget_used=budget["used"],
        budget_measured=budget["measured"],
        budget_estimated=budget["estimated"],
        hard_ceiling=budget["hard"],
        hard_reached=budget["reached"],
        soft_threshold=budget["soft"],
        soft_rule=budget["rule"],
        soft_crossed=budget["crossed"],
        crossed_at=budget["crossed_at"],
        crossed_at_tokens=budget["crossed_tokens"],
        budget_used_pct=budget["used_pct"],
        budget_soft_pct=budget["soft_pct"],
        budget_has_bar=budget["has_bar"],
        budget_has_tick=budget["has_tick"],
        budget_hatched=budget["hatched"],
        turns=turns,
        turns_max=turns_max,
        continuation_index=cont_index,
        continuation_max=cont_max,
        cache_hit=cache_hit,
        cache_counted=cache_counted,
        tokens=token_rows,
        ctx_main=ctx["main"],
        ctx_peak=ctx["peak"],
        ctx_window=ctx["window"],
        ctx_auto_compact=ctx["auto"],
        compactions_count=ctx["comp_count"],
        compactions_last_at=ctx["last_at"],
        compactions_last_trigger=ctx["last_trigger"],
        compactions_last_pre=ctx["last_pre"],
        compactions_last_post=ctx["last_post"],
        subagents=ctx["subagents"],
        rate_limit=rate_limit,
        rate_limit_reason=rate_reason,
        control_state=control_state,
        intake=intake,
        intake_stale=intake_stale,
        active_issue=active_issue,
        active_title=active_title,
        active_branch=active_branch,
        active_phase=active_phase,
        active_started=active_started,
        active_text=active_text,
        stop_plan=stop_plan,
        handoff_present=handoff_present,
        handoff_source=handoff_source,
        handoff_phase=handoff_phase,
        handoff_model_deadline=handoff_model_deadline,
        handoff_publication_deadline=handoff_pub_deadline,
        handoff_detail=handoff_detail,
        handoff_begun=handoff_begun,
        handoff_text=handoff_text,
        recovery_present=recovery_present,
        recovery_issue=recovery_issue,
        recovery_phase=recovery_phase,
        recovery_branch=recovery_branch,
        recovery_reason=recovery_reason,
        recovery_next_action=recovery_next_action,
        errors_text=errors_text,
        last_present=last_present,
        last_issue=last_issue,
        last_title=last_title,
        last_outcome=last_outcome,
        last_tokens=last_tokens,
        last_turns=last_turns,
        last_finished_at=last_finished_at,
        last_pr_number=last_pr_number,
        last_pr_url=last_pr_url,
        events=tuple(events),
    )
