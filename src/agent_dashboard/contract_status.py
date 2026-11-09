"""Status Snapshot view models for the contract path (ADR 0014).

Turns the ``result`` of a good contract ``status`` reply into
framework-free view models for the fleet table and the instance drawer:
the Reach inputs, the Status headline and the Attention rank, plus the
full Status Snapshot for the drawer (every figure with its unit and
basis, the budget the way the backend enforces it, every enforced limit,
the optional figures, per-section freshness, the last Halt, the last
finished attempt and the recent Contract Events). Parsing is lenient
(spec section 5): unknown fields are ignored and unknown enum values
read as ``"unknown"``; anything structurally incoherent reads as no
snapshot rather than breaking the row. Templates consume only these
view models; there is no public JSON API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from agent_operator_contract import status as contract_status_lib

UNKNOWN = "unknown"

UNAVAILABLE = "unavailable"

#: Figure units the Status Snapshot reports (spec section 7.3).
FIGURE_UNITS = ("tokens", "usd", "seconds", "count", "ratio")

#: Figure bases with a basis marker (per DESIGN.md). ``unavailable`` reads
#: as ``"unknown"`` and ``not_applicable`` as ``"not applicable"``.
FIGURE_BASES = ("measured", "estimated", "configured", "assumed")

NOT_APPLICABLE = "not applicable"

#: A section older than this many poll intervals reads as stale. Reach is
#: unchanged by section staleness; the Status headline names it instead.
STALE_AFTER_INTERVALS = 3

#: Recent Contract Events shown in the drawer, newest first (the snapshot
#: allows up to 50; the drawer keeps the latest 20 like the legacy path).
EVENTS_SHOWN = 20

#: Command timeline entries shown, newest first (like the legacy path).
COMMANDS_SHOWN = 10

#: Terminal acknowledgements for a contract CommandRecord (spec 7.5).
CONTRACT_TERMINAL_ACKS = ("completed", "superseded", "not_fulfilled")

#: Display labels for contract ``recent_commands`` kinds.
CONTRACT_KIND_LABELS = {
    "stop": "stop",
    "stop_after": "stop after",
    "resume": "resume",
    "handoff": "handoff",
    "next_issue": "next issue",
    "recovery_retry": "recovery retry",
    "recovery_release": "recovery release",
    "errors_reset": "errors reset",
}

#: Optional figures in display order (spec section 7.3 name set): cost,
#: cache hit rate, context sizes, turns, tool calls, continuations, review
#: seats, elapsed time. ``native.*`` and unknown names follow, sorted.
OPTIONAL_FIGURE_LABELS = (
    ("cost_usd", "Cost"),
    ("cache_hit_rate", "Cache hit rate"),
    ("context_used", "Context used"),
    ("context_peak", "Context peak"),
    ("context_window", "Context window"),
    ("turns", "Turns"),
    ("tool_calls", "Tool calls"),
    ("continuations", "Continuations"),
    ("review_seats_running", "Review seats"),
    ("elapsed_seconds", "Elapsed time"),
)


@dataclass(frozen=True)
class FigureView:
    """One figure's final display strings (framework-free).

    ``text`` is the formatted value with its unit (``"1,834,221 tokens"``,
    ``"$3.41"``, ``"86%"``), ``"unknown"`` or ``"not applicable"``.
    ``basis`` is the basis marker (``"measured"`` and friends) or ``""``
    when the text already says unknown/not applicable. ``state`` is
    ``"value"``, ``"unknown"`` or ``"na"`` so the template can style
    "not applicable" distinctly from "unknown". ``reason`` is the
    backend's note (e.g. why a figure is unavailable), for the tooltip.
    """

    text: str = UNKNOWN
    basis: str = ""
    unit: str = UNKNOWN
    state: str = "unknown"  # "value" | "unknown" | "na"
    reason: str | None = None


@dataclass(frozen=True)
class BudgetLimitView:
    """One enforced ceiling: used against limit, with both bases."""

    name: str = UNKNOWN
    is_native: bool = False
    used: FigureView = field(default_factory=FigureView)
    limit: FigureView = field(default_factory=FigureView)


@dataclass(frozen=True)
class BudgetView:
    """The attempt budget the way the backend enforces it."""

    raw: FigureView = field(default_factory=FigureView)
    effective: FigureView = field(default_factory=FigureView)
    enforced: str = UNKNOWN  # "raw" | "effective" | "none" | "unknown"
    limits: tuple[BudgetLimitView, ...] = ()


@dataclass(frozen=True)
class OptionalFigureView:
    """One optional figure the backend reported (absent ones are omitted)."""

    name: str = UNKNOWN
    label: str = UNKNOWN
    is_native: bool = False
    figure: FigureView = field(default_factory=FigureView)


@dataclass(frozen=True)
class SectionFreshness:
    """One section's ``observed_at`` age and whether it reads as stale."""

    observed_at: str = UNKNOWN
    age_text: str = UNKNOWN  # e.g. "12 s ago"
    stale: bool = False


@dataclass(frozen=True)
class AttemptView:
    """The current attempt: facts, budget, optional figures, its events."""

    attempt_id: str = UNKNOWN
    issue_number: int = 0
    issue_title: str | None = None
    native_phase: str = UNKNOWN
    started_at: str = UNKNOWN
    last_activity_at: str | None = None
    freshness: SectionFreshness = field(default_factory=SectionFreshness)
    budget: BudgetView = field(default_factory=BudgetView)
    figures: tuple[OptionalFigureView, ...] = ()
    events: tuple[ContractEventView, ...] = ()
    events_total: int = 0


@dataclass(frozen=True)
class HaltView:
    """The last durably recorded engine halt."""

    reason: str = UNKNOWN
    detail: str = ""
    at: str = UNKNOWN


@dataclass(frozen=True)
class LastFinishedView:
    """The last finished attempt: outcome label, delivery link, attention."""

    attempt_id: str = UNKNOWN
    issue_number: int | None = None
    issue_title: str | None = None
    outcome_label: str | None = None
    delivery_number: int | None = None
    delivery_url: str | None = None
    attention_required: bool = False
    finished_at: str | None = None


@dataclass(frozen=True)
class ContractEventView:
    """One recent Contract Event: level, event, attempt, phase, text.

    ``short_time`` is the ``HH:MM:SS`` clock reading for the glanceable
    column; the full timestamp stays in ``at`` for the tooltip. ``is_native``
    marks ``native.`` events; unknown event names are shown raw.
    """

    at: str = UNKNOWN
    short_time: str = UNKNOWN
    level: str = UNKNOWN
    event: str = UNKNOWN
    is_native: bool = False
    attempt_id: str | None = None
    issue: int | None = None
    phase: str | None = None
    text: str = ""


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
    # Status Snapshot detail for the drawer.
    truncated: bool = False
    instance_observed: SectionFreshness | None = None
    intake_observed: SectionFreshness | None = None
    # The oldest stale section's ``observed_at``; none when nothing is stale.
    stale_since: str | None = None
    current: AttemptView | None = None
    halt: HaltView | None = None
    last_finished: LastFinishedView | None = None
    # Recent Contract Events, newest first, at most EVENTS_SHOWN;
    # ``events_total`` is the full snapshot count for the count line.
    events: tuple[ContractEventView, ...] = ()
    events_total: int = 0
    # Command timeline entries, newest first, at most COMMANDS_SHOWN.
    commands: tuple[ContractCommandEntry, ...] = ()


@dataclass(frozen=True)
class ContractCommandStep:
    """One lifecycle stepper stage: its label and done/current/todo state."""

    label: str
    state: str  # "done" | "current" | "todo"


@dataclass(frozen=True)
class ContractCommandEntry:
    """One contract ``recent_commands`` record for the timeline.

    Mirrors :class:`control.CommandEntry` with the contract acknowledgement
    set (``accepted`` plus ``completed``/``superseded``/``not_fulfilled``):
    the terminal stepper stage reads the acknowledgement verbatim, so an
    unknown value renders as its own stage rather than a wrong one.
    """

    request_id: str
    sequence: int | None
    kind: str
    kind_label: str
    acknowledgement: str | None
    detail: str
    updated_at: str | None
    short_time: str
    origin: str  # "dashboard" | "CLI"
    steps: tuple[ContractCommandStep, ...] = ()
    current_step: str = "submitted"

    @property
    def ack_class(self) -> str:
        """Tone class for the stepper: ``ack-<acknowledgement>``, else ``""``."""
        if isinstance(self.acknowledgement, str) and self.acknowledgement in (
            "accepted",
            *CONTRACT_TERMINAL_ACKS,
        ):
            return "ack-" + self.acknowledgement.replace(" ", "-")
        return ""


def _contract_steps(
    acknowledgement: str | None,
) -> tuple[tuple[ContractCommandStep, ...], str]:
    """Lifecycle stepper plus the current step's label for one record.

    Base stepper: submitted → accepted → the Command Record's
    Acknowledgement (terminal or otherwise). A terminal acknowledgement
    marks every stage done; ``accepted`` sits on the middle stage; anything
    else (including none) sits on "submitted".
    """
    if isinstance(acknowledgement, str) and acknowledgement:
        terminal_label = acknowledgement
    else:
        terminal_label = "completed"
    labels = ["submitted", "accepted", terminal_label]
    if isinstance(acknowledgement, str) and acknowledgement in CONTRACT_TERMINAL_ACKS:
        current = len(labels) - 1
    elif acknowledgement == "accepted":
        current = 1
    else:
        current = 0
    steps = tuple(
        ContractCommandStep(
            label=label,
            state=(
                "done"
                if index < current
                else "current"
                if index == current
                else "todo"
            ),
        )
        for index, label in enumerate(labels)
    )
    return steps, labels[current]


def contract_command_timeline(recent_commands: Any) -> tuple[ContractCommandEntry, ...]:
    """The ``recent_commands`` timeline, newest first by sequence.

    Reads the parsed snapshot's ``recent_commands`` (or the raw list);
    records without an integer sequence sort last. At most
    ``COMMANDS_SHOWN`` entries are returned; anything unreadable yields no
    entries. The origin chip reads ``dashboard`` for a ``dash-`` request ID
    and ``CLI`` otherwise.
    """
    if isinstance(recent_commands, dict):
        records = list(recent_commands.values())
    elif isinstance(recent_commands, (list, tuple)):
        records = list(recent_commands)
    else:
        return ()
    parsed: list[tuple[int, int, ContractCommandEntry]] = []
    for order, record in enumerate(records):
        if isinstance(record, dict):
            request_id = record.get("request_id")
            sequence = record.get("sequence")
            kind = record.get("kind")
            acknowledgement = record.get("acknowledgement")
            detail = record.get("detail")
            updated = record.get("updated_at")
            created = record.get("created_at")
        else:
            request_id = getattr(record, "request_id", None)
            sequence = getattr(record, "sequence", None)
            kind = getattr(record, "kind", None)
            acknowledgement = getattr(record, "acknowledgement", None)
            detail = getattr(record, "detail", None)
            updated = getattr(record, "updated_at", None)
            created = getattr(record, "created_at", None)
        if not isinstance(request_id, str) or not request_id:
            continue
        if isinstance(sequence, bool) or not isinstance(sequence, int):
            sequence = None
        kind = kind if isinstance(kind, str) and kind else "unknown"
        acknowledgement = (
            acknowledgement if isinstance(acknowledgement, str) and acknowledgement else None
        )
        detail = detail if isinstance(detail, str) else ""
        updated = updated if isinstance(updated, str) and updated else None
        created = created if isinstance(created, str) and created else None
        clock_source = updated or created
        steps, current_step = _contract_steps(acknowledgement)
        parsed.append(
            (
                sequence if sequence is not None else -1,
                order,
                ContractCommandEntry(
                    request_id=request_id,
                    sequence=sequence,
                    kind=kind,
                    kind_label=CONTRACT_KIND_LABELS.get(kind, kind),
                    acknowledgement=acknowledgement,
                    detail=detail,
                    updated_at=updated,
                    short_time=_short_time(clock_source) if clock_source else "—",
                    origin="dashboard" if request_id.startswith("dash-") else "CLI",
                    steps=steps,
                    current_step=current_step,
                ),
            )
        )
    parsed.sort(key=lambda item: (item[0], -item[1]), reverse=True)
    return tuple(entry for _, _, entry in parsed[:COMMANDS_SHOWN])


def contract_timeline_summary(
    entries: tuple[ContractCommandEntry, ...] | list[ContractCommandEntry],
) -> str:
    """Collapsed-summary text: the count plus the latest command and its ack."""
    items = list(entries)
    if not items:
        return "0 commands"
    latest = items[0]
    if latest.sequence is not None:
        head = f"#{latest.sequence} {latest.kind_label}"
    else:
        head = f"{latest.kind_label} {latest.request_id}"
    ack = latest.acknowledgement or "submitted"
    return f"{len(items)} commands · latest {head} ({ack})"


def follow_contract_command(
    *,
    action: str,
    request_id: str,
    sequence: int | None,
    card_container_id: str,
    recent_commands: Any,
    reach: str,
    row_container_id: str | None,
    row_exists: bool,
) -> Any:
    """Locate one accepted contract command in the latest good snapshot (pure).

    Reads only the snapshot's ``recent_commands`` and the row's Reach and
    container ID: never Docker, never the Control audit. The outcome set
    mirrors :func:`control.follow_command` (``following``, ``terminal``,
    ``paused``, ``waiting``, ``evicted``, ``gone``); the terminal set is
    :data:`CONTRACT_TERMINAL_ACKS`. The ``action`` argument is accepted for
    call-shape parity with the legacy path and is otherwise unused: a
    contract snapshot has no per-action progress section.
    """
    from .control import EVICTED_BODY as LEGACY_EVICTED_BODY
    from .control import GONE_BODY, REACH_REASONS, WAITING_BODY, FollowResult

    _ = action
    if not row_exists or row_container_id != card_container_id:
        return FollowResult(
            outcome="gone",
            title="Accepted",
            tone="info",
            body=GONE_BODY,
            following=False,
        )
    records = contract_command_timeline(recent_commands)
    record = next((entry for entry in records if entry.request_id == request_id), None)
    if reach != "live":
        reason = REACH_REASONS.get(reach, f"the snapshot is {reach}")
        body = record.detail if record is not None and record.detail else WAITING_BODY
        return FollowResult(
            outcome="paused",
            acknowledgement=record.acknowledgement if record is not None else None,
            title="Accepted",
            tone="info",
            body=body,
            progress=f"following paused: {reason}",
            following=True,
        )
    if record is not None:
        body = record.detail or "—"
        if (
            isinstance(record.acknowledgement, str)
            and record.acknowledgement in CONTRACT_TERMINAL_ACKS
        ):
            titles = {
                "completed": "Completed",
                "superseded": "Superseded",
                "not_fulfilled": "Not fulfilled",
            }
            tones = {
                "completed": "ok",
                "superseded": "muted",
                "not_fulfilled": "warn",
            }
            return FollowResult(
                outcome="terminal",
                acknowledgement=record.acknowledgement,
                title=titles[record.acknowledgement],
                tone=tones[record.acknowledgement],
                body=body,
                progress=None,
                following=False,
            )
        return FollowResult(
            outcome="following",
            acknowledgement=record.acknowledgement,
            title="Accepted",
            tone="info",
            body=body,
            progress=None,
            following=True,
        )
    sequences = [entry.sequence for entry in records if entry.sequence is not None]
    if sequence is not None and sequences and all(card_seq > sequence for card_seq in sequences):
        return FollowResult(
            outcome="evicted",
            title="Accepted",
            tone="info",
            body=LEGACY_EVICTED_BODY,
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


def _fmt_number(value: int | float) -> str:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"{value:,}"


def _format_value(value: int | float, unit: str) -> str:
    """The value with its unit: ``"1,834,221 tokens"``, ``"$3.41"``."""
    if unit == "tokens":
        return f"{_fmt_number(value)} tokens"
    if unit == "usd":
        return f"${value:,.2f}"
    if unit == "seconds":
        return f"{_fmt_number(value)} s"
    if unit == "count":
        return _fmt_number(value)
    if unit == "ratio":
        return f"{value * 100:.0f}%"
    return UNKNOWN


def _norm_unit(unit: Any) -> str:
    return unit if isinstance(unit, str) and unit in FIGURE_UNITS else UNKNOWN


def figure_view(fig: Any) -> FigureView:
    """Render one parsed figure: value with unit and basis marker.

    ``unavailable`` (and any unknown unit or basis, or missing value)
    renders ``"unknown"``; ``not_applicable`` renders ``"not applicable"``.
    Never raises; never renders a bare number for an absent figure.
    """
    if not isinstance(fig, contract_status_lib.Figure):
        return FigureView()
    unit = _norm_unit(fig.unit)
    basis = fig.basis
    value = fig.value
    reason = fig.reason if isinstance(fig.reason, str) and fig.reason else None
    if basis == "not_applicable":
        if value is None:
            return FigureView(NOT_APPLICABLE, "", unit, "na", reason)
        return FigureView(UNKNOWN, "", unit, "unknown", reason)
    if (
        basis not in FIGURE_BASES
        or unit == UNKNOWN
        or value is None
        or isinstance(value, bool)
        or not isinstance(value, (int, float))
    ):
        return FigureView(UNKNOWN, "", unit, "unknown", reason)
    return FigureView(_format_value(value, unit), basis, unit, "value", reason)


def _is_native_name(name: str) -> bool:
    return name.startswith("native.") and len(name) > len("native.")


def _optional_figures(figures: dict[str, Any]) -> tuple[OptionalFigureView, ...]:
    """The reported optional figures in display order; absent ones omitted."""
    rows: list[OptionalFigureView] = []
    for name, label in OPTIONAL_FIGURE_LABELS:
        if name in figures:
            rows.append(
                OptionalFigureView(
                    name=name,
                    label=label,
                    is_native=False,
                    figure=figure_view(figures[name]),
                )
            )
    extra = sorted(name for name in figures if name not in dict(OPTIONAL_FIGURE_LABELS))
    for name in extra:
        rows.append(
            OptionalFigureView(
                name=name,
                label=name,
                is_native=_is_native_name(name),
                figure=figure_view(figures[name]),
            )
        )
    return tuple(rows)


def _format_age(seconds: float) -> str:
    """Format a section age: '12 s', '3 min', '2 h' (as the fleet table)."""
    total = max(0, int(seconds))
    if total < 60:
        return f"{total} s"
    minutes = total // 60
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h"


def _parse_observed(text: Any, now: float) -> float:
    """Parse an ``observed_at`` timestamp; unparseable reads as now."""
    if not isinstance(text, str) or not text.strip():
        return now
    try:
        stamped = text.strip()
        if stamped.endswith("Z"):
            stamped = stamped[:-1] + "+00:00"
        moment = datetime.fromisoformat(stamped)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return moment.timestamp()
    except ValueError:
        return now


def section_freshness(
    observed_at: Any, *, now: float, poll_interval: float
) -> SectionFreshness:
    """One section's age and whether it is older than three poll intervals."""
    if not isinstance(observed_at, str) or not observed_at:
        return SectionFreshness(UNKNOWN, UNKNOWN, False)
    age = max(0.0, now - _parse_observed(observed_at, now))
    return SectionFreshness(
        observed_at=observed_at,
        age_text=f"{_format_age(age)} ago",
        stale=age > STALE_AFTER_INTERVALS * poll_interval,
    )


def _short_time(at: str) -> str:
    """Extract ``HH:MM:SS`` from an ISO-8601 timestamp, else the full text."""
    clock = at[11:19] if len(at) >= 19 and at[10] == "T" else ""
    if (
        len(clock) == 8
        and clock[2] == ":"
        and clock[5] == ":"
        and clock.replace(":", "").isdigit()
    ):
        return clock
    return at


def _event_view(event: contract_status_lib.ContractEvent) -> ContractEventView:
    """Render one parsed Contract Event; unknown names are shown raw."""
    name = event.event if isinstance(event.event, str) and event.event else UNKNOWN
    return ContractEventView(
        at=event.at,
        short_time=_short_time(event.at),
        level=event.level if isinstance(event.level, str) and event.level else UNKNOWN,
        event=name,
        is_native=_is_native_name(name),
        attempt_id=event.attempt_id,
        issue=event.issue,
        phase=event.phase,
        text=event.text if isinstance(event.text, str) else "",
    )


def _read_delivery(result: Any) -> tuple[int | None, str | None]:
    """The last finished attempt's delivery link, or ``(None, None)``.

    The library DTO drops ``delivery`` (it carries no verdict), so this
    reads it leniently from the raw result: a pull-request number and URL,
    never a bare or broken link.
    """
    try:
        last = result.get("last_finished") if isinstance(result, dict) else None
        delivery = last.get("delivery") if isinstance(last, dict) else None
        pull = delivery.get("pull_request") if isinstance(delivery, dict) else None
        if not isinstance(pull, dict):
            return None, None
        number = pull.get("number")
        url = pull.get("url")
        number = number if isinstance(number, int) and not isinstance(number, bool) else None
        url = url if isinstance(url, str) and url else None
        if number is None and url is None:
            return None, None
        return number, url
    except Exception:
        return None, None


def _read_budget(
    budget: contract_status_lib.Budget,
) -> BudgetView:
    """Render the budget the way the backend enforces it."""
    enforced = budget.enforced_tokens
    if enforced not in ("raw", "effective", "none"):
        enforced = UNKNOWN
    limits: list[BudgetLimitView] = []
    for entry in budget.limits:
        name = entry.name if isinstance(entry.name, str) and entry.name else UNKNOWN
        limits.append(
            BudgetLimitView(
                name=name,
                is_native=_is_native_name(name),
                used=figure_view(entry.used),
                limit=figure_view(entry.limit),
            )
        )
    return BudgetView(
        raw=figure_view(budget.raw_tokens),
        effective=figure_view(budget.effective_tokens),
        enforced=enforced,
        limits=tuple(limits),
    )


def read_contract_snapshot(
    result: Any, *, now: float | None = None, poll_interval: float = 5.0
) -> ContractDetail | None:
    """Read one contract ``status`` result into a view model; never raises.

    Returns none when the result is not a parsable Status Snapshot (not a
    dict, or structurally incoherent even leniently): the row keeps its
    poll outcome and renders the generic reach headline instead. ``now``
    stamps per-section ages; without it ages read as unknown and nothing
    reads as stale.
    """
    if not isinstance(result, dict):
        return None
    try:
        snapshot = contract_status_lib.parse_status_lenient(result)
    except Exception:
        return None

    def _fresh(observed_at: Any) -> SectionFreshness:
        if now is None:
            text = observed_at if isinstance(observed_at, str) and observed_at else UNKNOWN
            return SectionFreshness(text, UNKNOWN, False)
        return section_freshness(observed_at, now=now, poll_interval=poll_interval)

    halt = snapshot.instance.last_halt
    current = snapshot.current_attempt
    last = snapshot.last_finished
    if current is not None:
        attempt_text = f"#{current.issue_number} · {current.native_phase}"
    else:
        attempt_text = "—"

    instance_fresh = _fresh(snapshot.instance.observed_at)
    intake_fresh = _fresh(snapshot.intake.observed_at)

    current_view: AttemptView | None = None
    attempt_events: tuple[ContractEventView, ...] = ()
    ordered_events = [_event_view(event) for event in snapshot.recent_events]
    if current is not None:
        scoped = [
            view for view in ordered_events if view.attempt_id == current.attempt_id
        ]
        attempt_total = len(scoped)
        attempt_events = tuple(reversed(scoped[-EVENTS_SHOWN:]))
        current_view = AttemptView(
            attempt_id=current.attempt_id,
            issue_number=current.issue_number,
            issue_title=current.issue_title,
            native_phase=current.native_phase,
            started_at=current.started_at,
            last_activity_at=current.last_activity_at,
            freshness=_fresh(current.observed_at),
            budget=_read_budget(current.budget),
            figures=_optional_figures(dict(current.figures)),
            events=attempt_events,
            events_total=attempt_total,
        )

    halt_view: HaltView | None = None
    if halt is not None:
        halt_view = HaltView(reason=halt.reason, detail=halt.detail, at=halt.at)

    last_view: LastFinishedView | None = None
    if last is not None:
        delivery_number, delivery_url = _read_delivery(result)
        last_view = LastFinishedView(
            attempt_id=last.attempt_id,
            issue_number=last.issue_number,
            issue_title=last.issue_title,
            outcome_label=last.outcome_label,
            delivery_number=delivery_number,
            delivery_url=delivery_url,
            attention_required=bool(last.attention_required),
            finished_at=last.finished_at,
        )

    events_total = len(ordered_events)
    events = tuple(reversed(ordered_events[-EVENTS_SHOWN:]))

    recent_commands: Any = getattr(snapshot, "recent_commands", None)
    if recent_commands is None and isinstance(result, dict):
        recent_commands = result.get("recent_commands")
    commands = contract_command_timeline(recent_commands)

    stale_since: str | None = None
    if now is not None:
        candidates = [instance_fresh, intake_fresh]
        if current_view is not None:
            candidates.append(current_view.freshness)
        stale = [fresh for fresh in candidates if fresh.stale]
        if stale:
            oldest = min(stale, key=lambda item: _parse_observed(item.observed_at, now))
            stale_since = oldest.observed_at

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
        truncated=bool(snapshot.truncated),
        instance_observed=instance_fresh,
        intake_observed=intake_fresh,
        stale_since=stale_since,
        current=current_view,
        halt=halt_view,
        last_finished=last_view,
        events=events,
        events_total=events_total,
        commands=commands,
    )


def stale_suffix(detail: ContractDetail) -> str:
    """The headline suffix naming section staleness, or ``""`` when fresh."""
    if detail.stale_since is None:
        return ""
    return f" (data stale since {detail.stale_since})"


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
