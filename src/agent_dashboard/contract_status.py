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


#: Usage Origins in display order (spec section 7.2): the known names
#: first, then any unknown origin by its raw name, sorted. The set is
#: open; a name outside this tuple renders raw, never as another origin.
ORIGIN_ORDER = ("main", "subagent", "handoff")


@dataclass(frozen=True)
class OriginSpendView:
    """One Usage Origin's spend cell (contract 1.1 ``budget.by_origin``).

    Every figure keeps its basis and reason, so an ``estimated``
    (worst-case, upper-bound) cell and an ``unavailable`` ("not recorded")
    cell are distinguishable from a present one and never read as zero.
    """

    name: str = UNKNOWN
    known: bool = False
    input_tokens: FigureView = field(default_factory=FigureView)
    cache_read_tokens: FigureView = field(default_factory=FigureView)
    cache_write_tokens: FigureView = field(default_factory=FigureView)
    output_tokens: FigureView = field(default_factory=FigureView)
    effective_tokens: FigureView = field(default_factory=FigureView)
    cost_usd: FigureView = field(default_factory=FigureView)


@dataclass(frozen=True)
class BudgetView:
    """The attempt budget the way the backend enforces it."""

    raw: FigureView = field(default_factory=FigureView)
    effective: FigureView = field(default_factory=FigureView)
    enforced: str = UNKNOWN  # "raw" | "effective" | "none" | "unknown"
    limits: tuple[BudgetLimitView, ...] = ()
    #: Spend per Usage Origin: none when the backend does not report
    #: ``by_origin`` at all (contract 1.0, or a 1.1 backend without it),
    #: distinct from reported cells whose figures are ``unavailable``.
    by_origin: tuple[OriginSpendView, ...] | None = None


@dataclass(frozen=True)
class OptionalFigureView:
    """One optional figure the backend reported (absent ones are omitted)."""

    name: str = UNKNOWN
    label: str = UNKNOWN
    is_native: bool = False
    figure: FigureView = field(default_factory=FigureView)


#: ``native.*`` figure names the Current attempt layout reads in the
#: telemetry v1 places (simple-coding-agent's names, ADR 0017). A backend
#: without them shows those rows as unknown; nothing is invented.
NATIVE_SOFT_THRESHOLD = "native.soft_threshold_tokens"
NATIVE_AUTO_COMPACT = "native.auto_compact_threshold"
NATIVE_COMPACTIONS = "native.compactions"
NATIVE_CONTINUATION_MAX = "native.continuation_max"
#: The configured Handoff Threshold (contract 1.1, coding-agent mapping).
NATIVE_HANDOFF_THRESHOLD = "native.handoff_threshold_tokens"
NATIVE_TOKEN_CATEGORIES = (
    "native.input_tokens",
    "native.cache_read_input_tokens",
    "native.cache_creation_input_tokens",
    "native.output_tokens",
)

#: Optional figures the Current attempt layout shows in a named row; every
#: other reported figure is listed under "Other figures". The token figures
#: with a named slot in the Main context block never sprout generic "Other
#: figures" rows; Compactions has no named row and lists under Other
#: figures when the backend reports it.
_GAUGE_FIGURES = frozenset(
    {
        "turns",
        "continuations",
        "cache_hit_rate",
        "context_used",
        "context_peak",
        "context_window",
        "context_threshold",
        "subagents_running",
        "subagents_ended",
        NATIVE_SOFT_THRESHOLD,
        NATIVE_AUTO_COMPACT,
        NATIVE_HANDOFF_THRESHOLD,
        NATIVE_CONTINUATION_MAX,
        *NATIVE_TOKEN_CATEGORIES,
    }
)


@dataclass(frozen=True)
class AttemptGauge:
    """The current attempt in the cockpit's Current attempt layout.

    The same rows, bar and matrix cells the telemetry v1 path shows
    (``DrawerDetail``/``SnapshotDetail``), read from the Status Snapshot
    budget and figures, so a contract instance looks like a legacy one
    (ADR 0017). Token figures omit the unit word (the row names the
    quantity); every other figure keeps its unit. Rows the snapshot cannot
    fill read ``unknown``; nothing is derived that the backend did not
    report.
    """

    enforced: str = UNKNOWN
    budget_used: FigureView = field(default_factory=FigureView)
    raw: FigureView = field(default_factory=FigureView)
    effective: FigureView = field(default_factory=FigureView)
    hard_ceiling: FigureView = field(default_factory=FigureView)
    soft_threshold: FigureView = field(default_factory=FigureView)
    soft_crossed: bool | None = None
    budget_used_pct: float | None = None
    budget_soft_pct: float | None = None
    budget_has_bar: bool = False
    budget_has_tick: bool = False
    budget_hatched: bool = False
    turns: FigureView = field(default_factory=FigureView)
    turns_max: FigureView = field(default_factory=FigureView)
    continuation_index: FigureView = field(default_factory=FigureView)
    continuation_max: FigureView = field(default_factory=FigureView)
    cache_hit: FigureView = field(default_factory=FigureView)
    ctx_main: FigureView = field(default_factory=FigureView)
    ctx_window: FigureView = field(default_factory=FigureView)
    ctx_peak: FigureView = field(default_factory=FigureView)
    ctx_auto_compact: FigureView = field(default_factory=FigureView)
    compactions: FigureView = field(default_factory=FigureView)
    #: Contract 1.1 token figures: the Usable Threshold ``context_used`` is
    #: compared with, the Handoff Threshold, and the Sub-agent counts.
    #: Absent figures read unknown.
    context_threshold: FigureView = field(default_factory=FigureView)
    handoff_threshold: FigureView = field(default_factory=FigureView)
    subagents_running: FigureView = field(default_factory=FigureView)
    subagents_ended: FigureView = field(default_factory=FigureView)
    #: The compared threshold (``context_threshold``, else the native
    #: auto-compact threshold) and the bar scale (the Handoff Threshold, else
    #: the Model Context Window). Token figures omit the unit word.
    context_compared: FigureView = field(default_factory=FigureView)
    context_scale: FigureView = field(default_factory=FigureView)
    context_scale_is_handoff: bool = False
    context_left: FigureView = field(default_factory=FigureView)
    #: Fleet matrix bar: ``context_used`` against the compared threshold.
    context_used_pct: float | None = None
    context_has_bar: bool = False
    context_hatched: bool = False
    context_hot: bool = False
    #: Cockpit bar: 0 to the Handoff Threshold (or the Context Window).
    context_scale_has_bar: bool = False
    context_scale_used_pct: float | None = None
    context_scale_compared_pct: float | None = None
    context_scale_peak_pct: float | None = None
    #: Handoffs for the ``↻N`` chip: the continuations count when above 0.
    context_continuations: int | None = None
    #: Spend by origin totals: bucket sums of the reported cells, while
    #: Effective and Cost are the budget totals. Token totals omit the unit
    #: word; unknown when nothing usable was reported.
    spend_input_total: FigureView = field(default_factory=FigureView)
    spend_cache_read_total: FigureView = field(default_factory=FigureView)
    spend_cache_write_total: FigureView = field(default_factory=FigureView)
    spend_output_total: FigureView = field(default_factory=FigureView)
    spend_effective_total: FigureView = field(default_factory=FigureView)
    spend_cost_total: FigureView = field(default_factory=FigureView)
    spend_has_unavailable: bool = False
    spend_cost_used: FigureView = field(default_factory=FigureView)
    spend_cost_limit: FigureView = field(default_factory=FigureView)
    spend_cost_pct: float | None = None
    # Enforced limits other than the enforced token budget and turns.
    other_limits: tuple[BudgetLimitView, ...] = ()
    tokens: tuple[OptionalFigureView, ...] = ()
    other_figures: tuple[OptionalFigureView, ...] = ()
    # Fleet matrix cells (legacy ``SnapshotDetail`` shapes).
    budget_text: str = UNAVAILABLE
    budget_basis: str = ""
    context_text: str = UNAVAILABLE
    context_basis: str = ""
    context_compared_text: str = UNKNOWN
    context_reason: str | None = None
    turns_text: str = UNAVAILABLE
    turns_basis: str = ""
    cache_text: str = UNAVAILABLE
    cache_basis: str = ""


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
    gauge: AttemptGauge = field(default_factory=AttemptGauge)
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
    # ``identity.process_started_at``: null offline, RFC 3339 UTC online.
    # Decides whether ``last_halt`` is from the current process or history.
    process_started_at: str | None = None
    pending_stop: bool = False
    stop_plan_remaining: int | None = None
    stop_plan_counted: int | None = None
    stop_plan_request_id: str | None = None
    priority_issue: int | None = None
    priority_request_id: str | None = None
    # Operator Capabilities from ``identity.capabilities`` in the latest good
    # snapshot: ``stop-after`` offers ``stop --after``, ``next-issue`` offers
    # ``next issue`` and ``operator-handoff`` offers ``handoff now``.
    capabilities: tuple[str, ...] = ()
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

    @property
    def halt_is_current(self) -> bool:
        """Whether the recorded Halt describes the current process.

        The single halt-is-current check (see :func:`halt_is_current`):
        the headline, the attention rank and both Last cells all read
        this, so changing the rule there changes them too. The drawer's
        :attr:`halt` view stays available as history either way.
        """
        return halt_is_current(self)


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


def _contract_follow_progress(
    *, action: str, request_id: str, stop_plan: Any, priority: Any
) -> str | None:
    """The progress line for a tracked contract command (spec: contract path).

    From the intake section whose ``request_id`` matches the card's: the
    stop plan reads ``"<remaining> left and <counted> counted"`` and the
    priority reads ``"issue #<n> pending"``. A section without a
    ``request_id`` field never matches (newer agents always stamp one); a
    mismatched ID or a missing section means no progress line.
    """
    if action == "stop_after":
        section = stop_plan
    elif action == "next_issue":
        section = priority
    else:
        return None
    if section is None:
        return None
    if isinstance(section, dict):
        entry = dict(section)
    else:
        entry = {
            key: getattr(section, key, None)
            for key in ("request_id", "remaining", "counted", "issue")
        }
    if entry.get("request_id") != request_id:
        return None
    if action == "stop_after":
        remaining = entry.get("remaining")
        if isinstance(remaining, bool) or not isinstance(remaining, int):
            return None
        counted = entry.get("counted")
        if isinstance(counted, bool) or not isinstance(counted, int):
            return f"{remaining} left"
        return f"{remaining} left and {counted} counted"
    number = entry.get("issue")
    if isinstance(number, bool) or not isinstance(number, int):
        return None
    return f"issue #{number} pending"


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
    stop_plan: Any = None,
    priority: Any = None,
) -> Any:
    """Locate one accepted contract command in the latest good snapshot (pure).

    Reads only the snapshot's ``recent_commands`` and intake sections plus
    the row's Reach and container ID: never Docker, never the Control audit.
    The outcome set mirrors :func:`control.follow_command` (``following``,
    ``terminal``, ``paused``, ``waiting``, ``evicted``, ``gone``); the
    terminal set is :data:`CONTRACT_TERMINAL_ACKS`. ``stop_plan`` and
    ``priority`` are the intake sections (mappings or parsed records) that
    feed the following cards' progress lines when their ``request_id``
    matches the card's.
    """
    from .control import EVICTED_BODY as LEGACY_EVICTED_BODY
    from .control import GONE_BODY, REACH_REASONS, WAITING_BODY, FollowResult

    progress = _contract_follow_progress(
        action=action, request_id=request_id, stop_plan=stop_plan, priority=priority
    )
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
        paused = f"following paused: {reason}"
        paused_progress = paused if progress is None else f"{progress} · {paused}"
        return FollowResult(
            outcome="paused",
            acknowledgement=record.acknowledgement if record is not None else None,
            title="Accepted",
            tone="info",
            body=body,
            progress=paused_progress,
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
            progress=progress,
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


def _read_origin_cell(name: str, cell: Any) -> OriginSpendView:
    """One reported Usage Origin cell; unknown names keep their raw name."""
    figures = cell if cell is not None else None

    def _cell_figure(field: str) -> FigureView:
        return figure_view(getattr(figures, field, None))

    return OriginSpendView(
        name=name,
        known=name in ORIGIN_ORDER,
        input_tokens=_cell_figure("input_tokens"),
        cache_read_tokens=_cell_figure("cache_read_tokens"),
        cache_write_tokens=_cell_figure("cache_write_tokens"),
        output_tokens=_cell_figure("output_tokens"),
        effective_tokens=_cell_figure("effective_tokens"),
        cost_usd=_cell_figure("cost_usd"),
    )


def _read_by_origin(budget: contract_status_lib.Budget) -> tuple[OriginSpendView, ...] | None:
    """Spend per Usage Origin: none when the backend does not report it.

    Known origins come first in ``ORIGIN_ORDER``, then unknown origins by
    raw name; a backend that stays silent reads as none, distinct from
    reported cells whose figures are ``unavailable``.
    """
    reported = budget.by_origin
    if reported is None:
        return None
    known = [name for name in ORIGIN_ORDER if name in reported]
    unknown = sorted(name for name in reported if name not in ORIGIN_ORDER)
    return tuple(_read_origin_cell(name, reported[name]) for name in (*known, *unknown))


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
        by_origin=_read_by_origin(budget),
    )


def _gauge_view(fig: Any) -> FigureView:
    """A figure for the Current attempt layout: tokens without the unit word."""
    view = figure_view(fig)
    if view.state == "value" and view.unit == "tokens":
        return FigureView(
            view.text.removesuffix(" tokens"), view.basis, view.unit, view.state, view.reason
        )
    return view


def _fig_number(fig: Any) -> int | float | None:
    """The numeric value of a usable figure (a known basis and unit), else none."""
    if figure_view(fig).state != "value":
        return None
    return fig.value


def _pct(part: int | float, whole: int | float) -> float:
    return round(min(100.0, max(0.0, part / whole * 100.0)), 1)


def _cell(view: FigureView) -> tuple[str, str]:
    """One fleet-matrix cell: the bare text and its basis (``""`` when none)."""
    if view.state != "value":
        return UNAVAILABLE, ""
    return view.text, view.basis


def _dto_number(fig: Any) -> int | float | None:
    """The numeric value of a usable figure DTO, else none."""
    if figure_view(fig).state != "value":
        return None
    value = getattr(fig, "value", None)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _dto_reason(fig: Any) -> str | None:
    """The backend note on a figure DTO, if it carries one."""
    reason = getattr(fig, "reason", None)
    return reason if isinstance(reason, str) and reason else None


def _is_local_estimate(fig: Any) -> bool:
    """Whether a context figure is a local estimate (no measured response).

    An anchored estimate (computed from provider-reported input) reads as a
    solid bar; a local estimate reads hatched, like an estimated budget bar.
    The backend only marks the difference in the reason text, so a missing
    reason reads as local (hatched) rather than as anchored.
    """
    if figure_view(fig).basis != "estimated":
        return False
    reason = _dto_reason(fig)
    if reason is None:
        return True
    return "anchor" not in reason.lower()


#: Spend table buckets in column order (contract 1.1 ``by_origin`` names).
SPEND_BUCKETS = (
    "input_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "output_tokens",
)


def _spend_sum(cells: list[Any], field: str) -> FigureView:
    """One bucket total: the sum of the usable reported cells.

    Unavailable cells contribute nothing and never read as zero; when no
    cell reports a usable value the total reads unknown.
    """
    values = [
        number
        for number in (_dto_number(getattr(cell, field, None)) for cell in cells)
        if number is not None
    ]
    if not values:
        return FigureView()
    total = sum(values)
    estimated = any(
        figure_view(getattr(cell, field, None)).basis == "estimated" for cell in cells
    )
    return FigureView(
        _fmt_number(total), "estimated" if estimated else "measured", "tokens", "value"
    )


def read_attempt_gauge(
    budget: contract_status_lib.Budget, figures: dict[str, Any]
) -> AttemptGauge:
    """Read one current attempt into the cockpit's Current attempt layout.

    The bar fills the enforced token budget (``raw_tokens`` or
    ``effective_tokens`` limit) against its ceiling, with the soft
    threshold tick from ``native.soft_threshold_tokens`` when reported; it
    is hatched when the used figure is estimated. The Main context cell
    compares ``context_used`` with the Usable Threshold (else the native
    auto-compact threshold); its bar is hatched while the figure is a local
    estimate and turns danger colour at 95%. Never raises.
    """
    enforced = budget.enforced_tokens
    if enforced not in ("raw", "effective", "none"):
        enforced = UNKNOWN
    token_limit_name = {"raw": "raw_tokens", "effective": "effective_tokens"}.get(enforced)
    token_limit = None
    turns_limit = None
    other_limits: list[BudgetLimitView] = []
    for entry in budget.limits:
        name = entry.name if isinstance(entry.name, str) and entry.name else UNKNOWN
        if token_limit_name is not None and name == token_limit_name and token_limit is None:
            token_limit = entry
        elif name == "turns" and turns_limit is None:
            turns_limit = entry
        else:
            other_limits.append(
                BudgetLimitView(
                    name=name,
                    is_native=_is_native_name(name),
                    used=figure_view(entry.used),
                    limit=figure_view(entry.limit),
                )
            )

    if token_limit is not None:
        used_fig = token_limit.used
        hard_fig = token_limit.limit
    else:
        used_fig = {"raw": budget.raw_tokens, "effective": budget.effective_tokens}.get(
            enforced
        )
        hard_fig = None
    soft_fig = figures.get(NATIVE_SOFT_THRESHOLD)

    used_value = _fig_number(used_fig)
    hard_value = _fig_number(hard_fig)
    soft_value = _fig_number(soft_fig)
    used_pct = soft_pct = None
    has_bar = has_tick = False
    if used_value is not None and hard_value is not None and hard_value > 0:
        used_pct = _pct(used_value, hard_value)
        has_bar = True
        if soft_value is not None and soft_value > 0:
            soft_pct = _pct(soft_value, hard_value)
            has_tick = True
    soft_crossed = (
        used_value >= soft_value
        if used_value is not None and soft_value is not None and soft_value > 0
        else None
    )
    used_view = _gauge_view(used_fig)
    hatched = used_view.state == "value" and used_view.basis == "estimated"

    turns_fig = turns_limit.used if turns_limit is not None else figures.get("turns")
    turns_view = _gauge_view(turns_fig)
    turns_max = _gauge_view(turns_limit.limit if turns_limit is not None else None)
    cont_fig = figures.get("continuations")
    cont_view = (
        FigureView(NOT_APPLICABLE, "", "count", "na")
        if cont_fig is None
        else _gauge_view(cont_fig)
    )
    ctx_main = _gauge_view(figures.get("context_used"))
    ctx_threshold_view = _gauge_view(figures.get("context_threshold"))
    ctx_auto_view = _gauge_view(figures.get(NATIVE_AUTO_COMPACT))
    ctx_window_view = _gauge_view(figures.get("context_window"))
    ctx_peak_view = _gauge_view(figures.get("context_peak"))
    handoff_view = _gauge_view(figures.get(NATIVE_HANDOFF_THRESHOLD))

    # The compared threshold: the Usable Threshold, else the native
    # auto-compact threshold when the backend reports one.
    ctx_used_fig = figures.get("context_used")
    compared_fig = (
        figures.get("context_threshold")
        if _dto_number(figures.get("context_threshold")) is not None
        else figures.get(NATIVE_AUTO_COMPACT)
    )
    compared_view = (
        ctx_threshold_view
        if _dto_number(figures.get("context_threshold")) is not None
        else ctx_auto_view
    )
    used_num = _dto_number(ctx_used_fig)
    compared_num = _dto_number(compared_fig)
    context_used_pct = context_hot = None
    context_has_bar = False
    if used_num is not None and compared_num is not None and compared_num > 0:
        context_used_pct = _pct(used_num, compared_num)
        context_has_bar = True
        context_hot = context_used_pct >= 95
    context_hatched = _is_local_estimate(ctx_used_fig)
    if context_hatched and ctx_main.state != "value":
        context_hatched = False
    context_left = FigureView()
    threshold_num = _dto_number(figures.get("context_threshold"))
    if used_num is not None and threshold_num is not None:
        context_left = FigureView(
            _fmt_number(threshold_num - used_num),
            ctx_threshold_view.basis,
            "tokens",
            "value",
        )
    # The cockpit bar: 0 to the Handoff Threshold, else the Context Window.
    handoff_num = _dto_number(figures.get(NATIVE_HANDOFF_THRESHOLD))
    window_num = _dto_number(figures.get("context_window"))
    peak_num = _dto_number(figures.get("context_peak"))
    scale_num = handoff_num if handoff_num is not None else window_num
    context_scale = (
        handoff_view if handoff_num is not None else ctx_window_view
    )
    context_scale_has_bar = False
    scale_used_pct = scale_compared_pct = scale_peak_pct = None
    if used_num is not None and scale_num is not None and scale_num > 0:
        context_scale_has_bar = True
        scale_used_pct = _pct(used_num, scale_num)
        if compared_num is not None and compared_num > 0:
            scale_compared_pct = _pct(compared_num, scale_num)
        if peak_num is not None and peak_num > 0:
            scale_peak_pct = _pct(peak_num, scale_num)
    cont_num = _dto_number(figures.get("continuations"))
    context_continuations = (
        cont_num
        if isinstance(cont_num, int) and not isinstance(cont_num, bool) and cont_num > 0
        else None
    )
    if context_continuations is None and (
        isinstance(cont_num, float) and cont_num > 0
    ):
        context_continuations = int(cont_num)

    # Cache hit rate is main-only: from the main origin cell when the
    # backend reports per-origin spend, else the reported figure (which the
    # backend already scopes to the main conversation).
    cache_view = _gauge_view(figures.get("cache_hit_rate"))
    by_origin_raw = budget.by_origin
    if isinstance(by_origin_raw, dict) and "main" in by_origin_raw:
        main = by_origin_raw["main"]
        main_inputs = [_dto_number(getattr(main, field, None)) for field in (
            "input_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
        )]
        if all(value is not None for value in main_inputs):
            denom = sum(main_inputs)  # type: ignore[arg-type]
            if denom > 0:
                main_rate = main_inputs[1] / denom  # type: ignore[operator]
                main_basis = figure_view(getattr(main, "cache_read_tokens", None)).basis
                cache_view = FigureView(
                    f"{main_rate * 100:.0f}%",
                    main_basis or "measured",
                    "ratio",
                    "value",
                )

    # Spend by origin: bucket sums of the usable cells; Effective and Cost
    # are the budget totals, never a sum that could drift from them.
    by_origin_cells = _read_by_origin(budget)
    origin_dtos = (
        [by_origin_raw[name] for name in by_origin_raw if by_origin_raw[name] is not None]
        if isinstance(by_origin_raw, dict)
        else []
    )
    spend_buckets = (
        {field: _spend_sum(origin_dtos, field) for field in SPEND_BUCKETS}
        if by_origin_cells is not None
        else {field: FigureView() for field in SPEND_BUCKETS}
    )
    spend_effective_total = FigureView()
    spend_cost_total = FigureView()
    spend_cost_used = spend_cost_limit = FigureView()
    spend_cost_pct = None
    spend_has_unavailable = False
    if by_origin_cells is not None:
        spend_effective_total = _gauge_view(budget.effective_tokens)
        cost_fig = (
            figures.get("cost_usd")
            if _dto_number(figures.get("cost_usd")) is not None
            else None
        )
        for entry in budget.limits:
            name = entry.name if isinstance(entry.name, str) and entry.name else UNKNOWN
            if name == "cost_usd":
                spend_cost_used = _gauge_view(entry.used)
                spend_cost_limit = _gauge_view(entry.limit)
                limit_cost = _dto_number(entry.limit)
                total_cost = _dto_number(cost_fig)
                if total_cost is None:
                    total_cost = _dto_number(entry.used)
                    cost_fig = entry.used
                if total_cost is not None and limit_cost is not None and limit_cost > 0:
                    spend_cost_pct = _pct(total_cost, limit_cost)
                break
        if _dto_number(cost_fig) is not None:
            spend_cost_total = _gauge_view(cost_fig)
        for cell in by_origin_cells:
            for figure in (
                cell.input_tokens,
                cell.cache_read_tokens,
                cell.cache_write_tokens,
                cell.output_tokens,
                cell.effective_tokens,
                cell.cost_usd,
            ):
                if figure.state == "unknown":
                    spend_has_unavailable = True
                    break
            if spend_has_unavailable:
                break

    if has_bar:
        hard_view = _gauge_view(hard_fig)
        budget_text = f"{used_view.text} / {hard_view.text}"
        budget_basis = used_view.basis
    else:
        budget_text, budget_basis = _cell(used_view)
    context_text, context_basis = _cell(ctx_main)
    if compared_view.state == "value":
        context_compared_text = compared_view.text
    else:
        context_compared_text = UNKNOWN
    turns_text, turns_basis = _cell(turns_view)
    cache_text, cache_basis = _cell(cache_view)

    tokens = tuple(
        OptionalFigureView(
            name=name,
            label=name.removeprefix("native."),
            is_native=True,
            figure=_gauge_view(figures[name]),
        )
        for name in NATIVE_TOKEN_CATEGORIES
        if name in figures
    )
    other_figures = tuple(
        row for row in _optional_figures(figures) if row.name not in _GAUGE_FIGURES
    )
    return AttemptGauge(
        enforced=enforced,
        budget_used=used_view,
        raw=_gauge_view(budget.raw_tokens),
        effective=_gauge_view(budget.effective_tokens),
        hard_ceiling=_gauge_view(hard_fig),
        context_threshold=_gauge_view(figures.get("context_threshold")),
        handoff_threshold=_gauge_view(figures.get(NATIVE_HANDOFF_THRESHOLD)),
        subagents_running=_gauge_view(figures.get("subagents_running")),
        subagents_ended=_gauge_view(figures.get("subagents_ended")),
        soft_threshold=_gauge_view(soft_fig) if soft_fig is not None else FigureView(),
        soft_crossed=soft_crossed,
        budget_used_pct=used_pct,
        budget_soft_pct=soft_pct,
        budget_has_bar=has_bar,
        budget_has_tick=has_tick,
        budget_hatched=hatched,
        turns=turns_view,
        turns_max=turns_max,
        continuation_index=cont_view,
        continuation_max=_gauge_view(figures.get(NATIVE_CONTINUATION_MAX)),
        cache_hit=cache_view,
        ctx_main=ctx_main,
        ctx_window=ctx_window_view,
        ctx_peak=ctx_peak_view,
        ctx_auto_compact=ctx_auto_view,
        compactions=_gauge_view(figures.get(NATIVE_COMPACTIONS)),
        context_compared=compared_view,
        context_scale=context_scale,
        context_scale_is_handoff=handoff_num is not None,
        context_left=context_left,
        context_used_pct=context_used_pct,
        context_has_bar=context_has_bar,
        context_hatched=context_hatched,
        context_hot=bool(context_hot),
        context_scale_has_bar=context_scale_has_bar,
        context_scale_used_pct=scale_used_pct,
        context_scale_compared_pct=scale_compared_pct,
        context_scale_peak_pct=scale_peak_pct,
        context_continuations=context_continuations,
        spend_input_total=spend_buckets["input_tokens"],
        spend_cache_read_total=spend_buckets["cache_read_tokens"],
        spend_cache_write_total=spend_buckets["cache_write_tokens"],
        spend_output_total=spend_buckets["output_tokens"],
        spend_effective_total=spend_effective_total,
        spend_cost_total=spend_cost_total,
        spend_has_unavailable=spend_has_unavailable,
        spend_cost_used=spend_cost_used,
        spend_cost_limit=spend_cost_limit,
        spend_cost_pct=spend_cost_pct,
        other_limits=tuple(other_limits),
        tokens=tokens,
        other_figures=other_figures,
        budget_text=budget_text,
        budget_basis=budget_basis,
        context_text=context_text,
        context_basis=context_basis,
        context_compared_text=context_compared_text,
        context_reason=_dto_reason(ctx_used_fig),
        turns_text=turns_text,
        turns_basis=turns_basis,
        cache_text=cache_text,
        cache_basis=cache_basis,
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
            gauge=read_attempt_gauge(current.budget, dict(current.figures)),
            events=attempt_events,
            events_total=attempt_total,
        )

    raw_capabilities = getattr(snapshot.identity, "capabilities", ())
    if isinstance(raw_capabilities, (list, tuple)):
        capabilities = tuple(
            cap for cap in raw_capabilities if isinstance(cap, str) and cap
        )
    else:
        capabilities = ()

    stop_plan = snapshot.intake.stop_plan
    if stop_plan is not None:
        stop_plan_remaining = stop_plan.remaining
        stop_plan_counted = stop_plan.counted
        stop_plan_request_id = stop_plan.request_id
    else:
        stop_plan_remaining = None
        stop_plan_counted = None
        stop_plan_request_id = None

    intake_priority = snapshot.intake.priority
    if intake_priority is not None:
        priority_issue = intake_priority.issue
        priority_request_id = intake_priority.request_id
    else:
        priority_issue = None
        priority_request_id = None

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
        process_started_at=snapshot.identity.process_started_at,
        pending_stop=snapshot.intake.pending_stop is not None,
        stop_plan_remaining=stop_plan_remaining,
        stop_plan_counted=stop_plan_counted,
        stop_plan_request_id=stop_plan_request_id,
        priority_issue=priority_issue,
        priority_request_id=priority_request_id,
        capabilities=capabilities,
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


def _parse_halt_time(text: Any) -> float | None:
    """Parse an RFC 3339 timestamp; none when missing or unparseable."""
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        stamped = text.strip()
        if stamped.endswith("Z"):
            stamped = stamped[:-1] + "+00:00"
        moment = datetime.fromisoformat(stamped)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return moment.timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def halt_is_current(detail: ContractDetail) -> bool:
    """True when the snapshot's Halt still describes the current process.

    A Halt is current when ``instance.state`` is ``stopped`` (Offline Read,
    where ``process_started_at`` is null) or when the Halt's ``at`` is at or
    after ``identity.process_started_at``. Otherwise it is history from an
    earlier process. Fail safe: a missing or unparseable timestamp on an
    online snapshot reads as current. No Halt recorded reads as not current.
    """
    if detail.last_halt_reason is None:
        return False
    if detail.instance_state == "stopped":
        return True
    halt_at = _parse_halt_time(detail.last_halt_at)
    started_at = _parse_halt_time(detail.process_started_at)
    if halt_at is None or started_at is None:
        return True
    return halt_at >= started_at


def contract_headline(detail: ContractDetail) -> str | None:
    """The snapshot-derived headline; none when the state says nothing.

    Precedence: a pending stop or stop plan (operator intent in flight),
    then the current Halt, then a last finished attempt needing the
    operator, then ``instance.state``. A Halt older than the current
    process is history and is skipped. An ``"unknown"`` state reads as
    nothing so the caller falls back to the generic live headline.
    """
    if detail.pending_stop:
        return "Stop requested"
    if detail.stop_plan_remaining is not None:
        return f"Stop planned ({detail.stop_plan_remaining} remaining)"
    if detail.last_halt_reason is not None and halt_is_current(detail):
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
    """True when the snapshot itself needs the operator (halt or attention).

    A Halt older than the current process is history and does not count;
    only a current Halt raises the attention tier.
    """
    return halt_is_current(detail) or detail.last_finished_attention


def contract_state_rank(instance_state: str) -> int | None:
    """The attention tier for an ``instance.state``; none when unknown."""
    if instance_state in ("starting", "recovering"):
        return 1
    if instance_state in ("active", "draining"):
        return 5
    if instance_state in ("idle", "paused"):
        return 6
    return None
