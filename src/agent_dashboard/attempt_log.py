"""Attempt log page: header plus the full ordered event stream of one attempt.

The gateway reads one implementation attempt's ``attempt.json`` summary and
the whole ``agent_output.json`` event stream in a single exec call (see
``gateway.ATTEMPT_LOG_SCRIPT``) and returns the raw stdout. This module
parses that payload into framework-free view models for the
``/instances/{name}/attempts/{issue}/{started}`` page: a tolerant header
(every ``attempt.json`` key except ``started_at`` may be missing) and the
full ordered event rows, server-paged at :data:`LOG_PAGE_SIZE` rows.

Tolerance rules (mirroring the history and progress readers):

- a missing ``attempt.json`` reads as ``attempt_missing`` with the header
  notice ``no attempt.json: in progress or crashed``, never an error;
- a missing ``agent_output.json`` or zero parsed events reads as
  ``layout not recognised`` with whatever header data was readable;
- an unparsable stream line is already a synthetic ``unparsable_line``
  WARNING row from the collector, so row numbers keep matching the file;
  anything still unparseable here becomes the same synthetic row;
- unknown event names render generically (name plus detail text);
  unknown levels render unstyled (``level_known`` is False);
- unknown fields are ignored; no function raises on garbage input.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any

from .gateway import (
    ATTEMPT_LOG_LAYOUT,
    ATTEMPT_LOG_TAIL_LAYOUT,
    validate_log_file,
)
from .history import LAYOUT_NOT_RECOGNISED, NOT_APPLICABLE, format_cost, format_duration

#: Rows per page on the Attempt log page.
LOG_PAGE_SIZE = 200

#: Levels the agent writes; anything else renders unstyled.
KNOWN_LEVELS = ("INFO", "WARNING", "ERROR")

#: Header notice when the attempt summary file is missing.
NO_ATTEMPT_JSON = "no attempt.json: in progress or crashed"

#: ``token_budget`` cost keys accepted for the header cost, in preference order.
BUDGET_COST_KEYS = (
    "total_cost_usd",
    "totalCostUsd",
    "cost_usd",
    "costUSD",
    "cost",
    "total_cost",
)

#: Per-model cost keys accepted for the header cost, in preference order.
#: The agent writes camelCase ``costUSD``; snake_case stays accepted for
#: older or divergent payloads.
MODEL_COST_KEYS = (
    "costUSD",
    "cost_usd",
    "total_cost_usd",
    "cost",
    "total_cost",
)


#: Events whose detail may name an Evidence file. Skill events carry their
#: payload inline, have no file, and stay plain rows.
EVIDENCE_EVENTS = ("tool_call", "tool_result", "model_response")

#: Evidence reference in an event detail: the tool name, the optional
#: ``[main]``/``[subagent:id]`` thread tag (absent on old agent versions),
#: and the numbered Evidence file. The detail may carry a suffix after the
#: file name (e.g. `` (chars=85)`` on model responses). Model responses
#: carry no tool name at all (``-> <file> (chars=<N>)`` per the layout
#: research), so a bare ``->`` prefix links those too.
EVIDENCE_REF_RE = re.compile(
    r"^(?:(?P<tool>.*?)(?: \[(?P<thread>main|subagent:[^\]]+)\])? -> |-> )"
    r"(?P<file>\d{4,}_(?:tool_call\.json|tool_result\.json|model_response\.txt))"
)


@dataclass(frozen=True)
class EvidenceRef:
    """One Evidence reference parsed from an event detail: tool name, the
    optional thread tag, and the allow-listed file name."""

    tool: str
    thread: str | None
    file: str


def parse_evidence_ref(event: Any, detail: Any) -> EvidenceRef | None:
    """Parse an Evidence reference from one event detail, or ``None``.

    Only ``tool_call``, ``tool_result`` and ``model_response`` events link
    to a file. A detail without ``->`` stays a plain row, and a file name
    that fails the read allow-list never becomes a link. Never raises.
    """
    try:
        if event not in EVIDENCE_EVENTS or not isinstance(detail, str):
            return None
        match = EVIDENCE_REF_RE.match(detail)
        if match is None:
            return None
        candidate = detail[match.start("file") :].split()[0]
        if candidate != match.group("file"):
            return None
        validate_log_file(candidate)
    except (ValueError, IndexError):
        return None
    return EvidenceRef(
        tool=match.group("tool") or "",
        thread=match.group("thread"),
        file=candidate,
    )


@dataclass(frozen=True)
class LogEvent:
    """One ordered event row: ``seq`` is the 1-based line number in the file."""

    seq: int
    timestamp_text: str = ""
    time_text: str = ""
    level: str = ""
    level_known: bool = False
    event: str = "unknown"
    detail: str = ""
    evidence: EvidenceRef | None = None


@dataclass(frozen=True)
class AttemptLogResult:
    """The parsed Attempt log payload: header plus the full ordered stream."""

    ok: bool
    error: str | None = None
    issue: int = 0
    started_at: str = NOT_APPLICABLE
    issue_title: str | None = None
    outcome_text: str = "interrupted"
    active: bool = False
    attempt_missing: bool = False
    duration_text: str = NOT_APPLICABLE
    cost_text: str = NOT_APPLICABLE
    agent_version: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    events: tuple[LogEvent, ...] = ()
    total_events: int = 0
    unreadable: int = 0
    truncated: bool = False
    file_size: int = 0


def _num(raw: Any) -> int | float | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return raw


def _str(raw: Any) -> str | None:
    return raw if isinstance(raw, str) and raw else None


def _restore_started(raw: Any) -> str:
    """Restore ``2026-09-27T16:33:43.385090Z`` from its dash-form directory name."""
    if not isinstance(raw, str) or "T" not in raw:
        return NOT_APPLICABLE
    date, clock = raw.split("T", 1)
    return f"{date}T{clock.replace('-', ':')}"


def _hms(raw: Any) -> str:
    """``HH:MM:SS`` of an event timestamp, or ``""`` when unknown."""
    if not isinstance(raw, str) or len(raw) < 19:
        return ""
    return raw[11:19]


def _read_cost(attempt: dict[str, Any]) -> str:
    """Header cost: ``token_budget`` total first, else summed model costs."""
    budget = attempt.get("token_budget")
    if isinstance(budget, dict):
        for key in BUDGET_COST_KEYS:
            value = _num(budget.get(key))
            if value is not None:
                return format_cost(value)
    usage = attempt.get("model_usage")
    if isinstance(usage, dict):
        total: int | float | None = None
        for model in usage.values():
            if not isinstance(model, dict):
                continue
            for key in MODEL_COST_KEYS:
                value = _num(model.get(key))
                if value is not None:
                    total = (total or 0) + value
                    break
        if total is not None:
            return format_cost(total)
    return NOT_APPLICABLE


def _read_header(
    attempt: Any, started: str, *, attempt_missing: bool = False
) -> dict[str, Any]:
    """Tolerant header fields from one ``attempt.json`` record (or its absence)."""
    base: dict[str, Any] = {
        "started_at": _restore_started(started),
        "attempt_missing": attempt_missing,
    }
    if not isinstance(attempt, dict):
        return {
            **base,
            "issue_title": None,
            "outcome_text": "interrupted",
            "active": False,
            "duration_text": NOT_APPLICABLE,
            "cost_text": NOT_APPLICABLE,
            "agent_version": None,
            "pr_number": None,
            "pr_url": None,
        }
    outcome = _str(attempt.get("outcome"))
    if outcome is not None:
        outcome_text, active = outcome, False
    else:
        outcome_text, active = "active", True
    duration = _num(attempt.get("duration_seconds"))
    pr = attempt.get("pull_request")
    pr_number: int | None = None
    pr_url: str | None = None
    if isinstance(pr, dict):
        number = pr.get("number")
        if isinstance(number, int) and not isinstance(number, bool):
            pr_number = number
        pr_url = _str(pr.get("url"))
    return {
        **base,
        "issue_title": _str(attempt.get("issue_title")),
        "outcome_text": outcome_text,
        "active": active,
        "started_at": _str(attempt.get("started_at")) or _restore_started(started),
        "duration_text": (
            format_duration(duration) if duration is not None else NOT_APPLICABLE
        ),
        "cost_text": _read_cost(attempt),
        "agent_version": _str(attempt.get("agent_version")),
        "pr_number": pr_number,
        "pr_url": pr_url,
    }


def _read_event(entry: Any, seq: int) -> LogEvent:
    """One ordered row; never raises: garbage becomes a synthetic row."""
    if not isinstance(entry, dict):
        text = entry if isinstance(entry, str) else ""
        return LogEvent(
            seq=seq,
            level="WARNING",
            level_known=True,
            event="unparsable_line",
            detail=text[:200],
        )
    timestamp = entry.get("timestamp")
    timestamp_text = timestamp if isinstance(timestamp, str) and timestamp else ""
    level_raw = entry.get("level")
    level = level_raw if isinstance(level_raw, str) and level_raw else ""
    name = entry.get("event")
    event = name if isinstance(name, str) and name else "unknown"
    detail_raw = entry.get("detail")
    if isinstance(detail_raw, str):
        detail = detail_raw
    elif detail_raw is None:
        detail = ""
    else:
        try:
            detail = str(detail_raw)
        except Exception:
            detail = ""
    return LogEvent(
        seq=seq,
        timestamp_text=timestamp_text,
        time_text=_hms(timestamp),
        level=level,
        level_known=level in KNOWN_LEVELS,
        event=event,
        detail=detail,
        evidence=parse_evidence_ref(event, detail),
    )


def _counts(payload: dict[str, Any], events: tuple[LogEvent, ...]) -> dict[str, Any]:
    """Validated stream counters, falling back to what was actually parsed."""

    def _count(raw: Any, fallback: int) -> int:
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            return fallback
        return raw

    total = _count(payload.get("total_events"), len(events))
    return {
        "total_events": total,
        "unreadable": _count(payload.get("unreadable"), 0),
        "truncated": payload.get("truncated") is True,
        "file_size": _count(payload.get("file_size"), 0),
    }


def _assemble_attempt_log(
    payload: dict[str, Any], *, issue: int, started: str
) -> AttemptLogResult:
    """Assemble a header plus ordered rows from an attempt-log-layout payload.

    Shared by the running-instance exec reader and the stopped-instance
    tree-copy reader: both build the same layout, so the tolerance rules
    apply identically to either source.
    """
    if (
        not isinstance(payload, dict)
        or payload.get("layout") != ATTEMPT_LOG_LAYOUT
        or not isinstance(payload.get("events"), list)
    ):
        raw_attempt = payload.get("attempt") if isinstance(payload, dict) else None
        header = _read_header(
            raw_attempt, started, attempt_missing=raw_attempt is None
        )
        return AttemptLogResult(
            ok=False,
            error=LAYOUT_NOT_RECOGNISED,
            issue=issue,
            **header,  # type: ignore[arg-type]
        )
    raw_attempt = payload.get("attempt")
    header = _read_header(
        raw_attempt, started, attempt_missing=raw_attempt is None
    )
    events = tuple(
        _read_event(entry, seq) for seq, entry in enumerate(payload["events"], start=1)
    )
    if not events:
        return AttemptLogResult(
            ok=False,
            error=LAYOUT_NOT_RECOGNISED,
            issue=issue,
            **header,  # type: ignore[arg-type]
            **_counts(payload, events),  # type: ignore[arg-type]
        )
    return AttemptLogResult(
        ok=True,
        issue=issue,
        **header,  # type: ignore[arg-type]
        events=events,
        **_counts(payload, events),  # type: ignore[arg-type]
    )


def read_attempt_log(
    stdout: bytes, returncode: int, *, issue: int, started: str
) -> AttemptLogResult:
    """Parse one Attempt log exec's stdout into a header plus ordered rows.

    ``issue``/``started`` (the validated URL reference) stay the result's
    identity; the payload carries the data. ``returncode`` is accepted for
    signature parity with the other Attempt log readers but does not decide
    the outcome: a payload with the expected layout is trusted, anything
    else is ``layout not recognised``. A payload with the expected layout
    but zero parsed events is ``layout not recognised`` as well, keeping
    whatever header data was readable. A missing ``attempt`` record reads
    as ``attempt_missing`` with the in-progress-or-crashed notice.
    """
    del returncode
    try:
        payload = json.loads(stdout.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return AttemptLogResult(
            ok=False,
            error=LAYOUT_NOT_RECOGNISED,
            issue=issue,
            started_at=_restore_started(started),
            attempt_missing=True,
        )
    return _assemble_attempt_log(payload, issue=issue, started=started)


def read_stopped_attempt_log(
    data: bytes, *, issue: int, started: str
) -> tuple[AttemptLogResult, tuple[str, ...]]:
    """Parse one stopped-instance run-directory copy into a log plus file names.

    Returns the parsed header plus ordered rows (with the same tolerance
    rules as the running-instance exec reader) and every regular file's
    base name in the run, used for the command-log links. A non-tar stream
    reads as ``layout not recognised``, never an error.
    """
    from .stopped import parse_run_tree

    try:
        payload, names = parse_run_tree(data, issue=issue, started=started)
    except (ValueError, TypeError, AttributeError):
        return (
            AttemptLogResult(
                ok=False,
                error=LAYOUT_NOT_RECOGNISED,
                issue=issue,
                started_at=_restore_started(started),
                attempt_missing=True,
            ),
            (),
        )
    return _assemble_attempt_log(payload, issue=issue, started=started), tuple(names)


def _assemble_attempt_tail_log(
    payload: dict[str, Any], *, issue: int, started: str
) -> AttemptLogResult:
    """Assemble a header plus newest rows from a tail-layout payload.

    Shared by the running-instance tail exec reader and the stopped-instance
    tail tree-copy reader. ``total_events`` is the full stream's non-blank
    line count, so rows keep the file's line numbers (``seq``): a truncated
    tail starts at ``total - len(events) + 1``, a whole-file tail starts at
    1. Tolerance mirrors the head reader: a missing layout or zero rows is
    ``layout not recognised`` with whatever header was readable.
    """
    if (
        not isinstance(payload, dict)
        or payload.get("layout") != ATTEMPT_LOG_TAIL_LAYOUT
        or not isinstance(payload.get("events"), list)
    ):
        raw_attempt = payload.get("attempt") if isinstance(payload, dict) else None
        header = _read_header(
            raw_attempt, started, attempt_missing=raw_attempt is None
        )
        return AttemptLogResult(
            ok=False,
            error=LAYOUT_NOT_RECOGNISED,
            issue=issue,
            **header,  # type: ignore[arg-type]
        )
    raw_attempt = payload.get("attempt")
    header = _read_header(
        raw_attempt, started, attempt_missing=raw_attempt is None
    )
    raw_events = payload["events"]
    counts = _counts(payload, ())
    total = counts["total_events"]
    if not raw_events:
        return AttemptLogResult(
            ok=False,
            error=LAYOUT_NOT_RECOGNISED,
            issue=issue,
            **header,  # type: ignore[arg-type]
            **counts,  # type: ignore[arg-type]
        )
    if counts["truncated"] and total >= len(raw_events):
        start = total - len(raw_events) + 1
    else:
        start = 1
        if total < len(raw_events):
            counts = {**counts, "total_events": len(raw_events)}
    events = tuple(
        _read_event(entry, seq)
        for seq, entry in zip(range(start, start + len(raw_events)), raw_events)
    )
    return AttemptLogResult(
        ok=True,
        issue=issue,
        **header,  # type: ignore[arg-type]
        events=events,
        **counts,  # type: ignore[arg-type]
    )


def read_attempt_tail_log(
    stdout: bytes, returncode: int, *, issue: int, started: str
) -> AttemptLogResult:
    """Parse one Attempt log tail exec's stdout into a header plus newest rows.

    ``issue``/``started`` stay the result's identity; ``returncode`` is
    accepted for signature parity but does not decide the outcome. Rows keep
    the file's line numbers (``seq``), so the Activity widget can link into
    the Attempt log page. A tail-layout payload with zero rows is ``layout
    not recognised``, like the head reader.
    """
    del returncode
    try:
        payload = json.loads(stdout.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return AttemptLogResult(
            ok=False,
            error=LAYOUT_NOT_RECOGNISED,
            issue=issue,
            started_at=_restore_started(started),
            attempt_missing=True,
        )
    return _assemble_attempt_tail_log(payload, issue=issue, started=started)


def read_stopped_attempt_tail_log(
    data: bytes, *, issue: int, started: str
) -> tuple[AttemptLogResult, tuple[str, ...]]:
    """Parse one stopped-instance run-directory copy into a tail plus names.

    Returns the parsed header plus newest rows (same tolerance as the
    running-instance tail exec reader) and every regular file's base name.
    A non-tar stream reads as ``layout not recognised``, never an error.
    """
    from .stopped import parse_run_tree_tail

    try:
        payload, names = parse_run_tree_tail(data, issue=issue, started=started)
    except (ValueError, TypeError, AttributeError):
        return (
            AttemptLogResult(
                ok=False,
                error=LAYOUT_NOT_RECOGNISED,
                issue=issue,
                started_at=_restore_started(started),
                attempt_missing=True,
            ),
            (),
        )
    return _assemble_attempt_tail_log(
        payload, issue=issue, started=started
    ), tuple(names)


def _text(value: Any) -> str:
    """Filter input as text; garbage (None, numbers, lists) reads as blank."""
    return value if isinstance(value, str) else ""


def _active(value: Any) -> str:
    """One stripped filter value; blank or ``all`` means no filtering."""
    text = _text(value).strip()
    return "" if text.lower() == "all" else text


def event_names(events: tuple[LogEvent, ...]) -> tuple[str, ...]:
    """Event names observed in this stream, sorted for a stable select."""
    return tuple(sorted({ev.event for ev in events if ev.event}))


def level_options(events: tuple[LogEvent, ...]) -> tuple[str, ...]:
    """Level select options: blank (All) first, then known levels, then extras.

    Known levels stay selectable even when the stream has no such row (the
    result is then simply empty); levels the agent wrote outside
    :data:`KNOWN_LEVELS` (e.g. ``DEBUG``) are appended sorted so they can
    still be picked.
    """
    extras = sorted(
        {ev.level for ev in events if ev.level and ev.level not in KNOWN_LEVELS}
    )
    return ("", *KNOWN_LEVELS, *extras)


def filter_events(
    events: tuple[LogEvent, ...],
    level: Any = "",
    event: Any = "",
    q: Any = "",
) -> tuple[LogEvent, ...]:
    """Narrow the full stream before paging; never raises on garbage input.

    ``level`` matches exactly (case-insensitively), ``event`` matches
    exactly, ``q`` is a case-insensitive substring of the detail. Blank
    (or ``all`` for the two selects) means no filtering for that field;
    unknown values simply match nothing, so the page renders an empty
    result. ``q`` keeps a literal ``all``: only a blank query matches
    everything. Rows keep their file line numbers (``seq``), never
    positions in the filtered list.
    """
    wanted_level = _active(level)
    wanted_event = _active(event)
    needle = _text(q).strip().lower()

    def _keep(ev: LogEvent) -> bool:
        if wanted_level and ev.level.upper() != wanted_level.upper():
            return False
        if wanted_event and ev.event != wanted_event:
            return False
        return not needle or needle in ev.detail.lower()

    return tuple(ev for ev in events if _keep(ev))


def pair_tool_calls(events: tuple[LogEvent, ...]) -> dict[int, int]:
    """FIFO pair tool calls with their results, both directions.

    Results carry no call id, so a ``tool_result`` takes the oldest
    unmatched ``tool_call`` with the same ``(tool, thread)`` key in event
    order, as the prototype does. Runs over the full stream before
    filtering and paging, so a pair link works across pages and filters.
    Calls without a result, and results without a call, stay unpaired.
    Model responses never pair. Never raises.
    """
    try:
        rows = tuple(events)
    except TypeError:
        return {}
    pending: dict[tuple[str, str | None], deque[int]] = defaultdict(deque)
    by_seq: dict[int, LogEvent] = {}
    for ev in rows:
        if isinstance(ev, LogEvent):
            by_seq.setdefault(ev.seq, ev)
    pairs: dict[int, int] = {}
    for ev in rows:
        if not isinstance(ev, LogEvent) or ev.evidence is None:
            continue
        try:
            key = (ev.evidence.tool, ev.evidence.thread)
            if ev.event == "tool_call":
                pending[key].append(ev.seq)
            elif ev.event == "tool_result" and pending[key]:
                call_seq = pending[key].popleft()
                pairs[call_seq] = ev.seq
                pairs[ev.seq] = call_seq
        except (AttributeError, TypeError):
            continue
    # Drop pairs whose counterpart lost its evidence (defensive: the map
    # above only pairs evidence rows, but callers may pass stale seqs).
    return {
        seq: other for seq, other in pairs.items() if other in by_seq and seq in by_seq
    }


def paired_files(
    events: tuple[LogEvent, ...], pairs: dict[int, int] | None = None
) -> dict[int, str]:
    """Map each paired row ``seq`` to its counterpart's Evidence file name.

    Computed from the full stream (see :func:`pair_tool_calls`); rows
    without a pair, or whose counterpart has no Evidence file, are absent.
    Never raises.
    """
    try:
        rows = tuple(events)
    except TypeError:
        return {}
    try:
        pair_map = dict(pairs) if pairs is not None else pair_tool_calls(rows)
    except (TypeError, ValueError):
        return {}
    by_seq: dict[int, LogEvent] = {}
    for ev in rows:
        if isinstance(ev, LogEvent):
            by_seq.setdefault(ev.seq, ev)
    out: dict[int, str] = {}
    for seq, other in pair_map.items():
        counterpart = by_seq.get(other)
        if (
            counterpart is not None
            and counterpart.evidence is not None
            and counterpart.evidence.file
        ):
            out[seq] = counterpart.evidence.file
    return out


def parse_page(raw: Any, pages: int) -> int:
    """Clamp a ``?page=`` value into ``1..pages``; garbage means page 1."""
    try:
        number = int(str(raw).strip())
    except (ValueError, TypeError, AttributeError):
        return 1
    return min(max(number, 1), max(pages, 1))


def paginate(
    events: tuple[LogEvent, ...], page: Any, *, per_page: int = LOG_PAGE_SIZE
) -> tuple[tuple[LogEvent, ...], int, int]:
    """Slice the ordered stream into one page: ``(rows, page, pages)``."""
    pages = max(1, -(-len(events) // per_page))
    number = parse_page(page, pages)
    start = (number - 1) * per_page
    return events[start : start + per_page], number, pages


def format_file_mb(size_bytes: int) -> str:
    """``M B`` display for the truncation notice, e.g. ``4.2 MB``."""
    return f"{size_bytes / (1024 * 1024):.1f} MB"


def format_preview(filename: str, content: bytes, truncated: bool) -> str:
    """Display text for one Evidence file fragment: decoded as read.

    Formatted means pretty-printed JSON for ``*.json``; it falls back to the
    raw text when the content is truncated or is not valid JSON. ``raw=1``
    callers pass the result through unchanged by rendering without calling
    this function. Never raises on garbage input.
    """
    try:
        text = content.decode("utf-8", errors="replace")
    except Exception:
        return ""
    if truncated or not filename.endswith(".json"):
        return text
    try:
        parsed = json.loads(text)
    except ValueError:
        return text
    try:
        return json.dumps(parsed, indent=2)
    except (ValueError, TypeError):
        return text
