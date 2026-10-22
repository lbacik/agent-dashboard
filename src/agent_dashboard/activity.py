"""Instance drawer Activity widget: what the agent is doing right now.

Reads the active attempt's Attempt log event stream and derives two views:
``Now`` (the tool call running after the latest ``model_response``, or the
model working otherwise) and ``Last call`` (the latest paired call and its
response, side by side).

Pairing runs within one model turn: the stream is split at
``model_response`` and each turn pairs FIFO per ``(tool, thread)`` (see
:func:`attempt_log.pair_tool_calls`). The Attempt log page pairs over the
whole stream, so a new result would match an older abandoned call of the
same tool and the current call would read as running forever; splitting
into turns keeps an abandoned call in its own turn.

Previews clip to a few lines so the widget keeps its fixed height; Evidence
files never change once written, so the route caches them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .attempt_log import LogEvent, pair_tool_calls

#: Lines/chars kept of one Evidence file preview; the widget has a fixed
#: height with inner scrolling, so previews stay short.
PREVIEW_LINES = 14
PREVIEW_CHARS = 1500


@dataclass(frozen=True)
class Activity:
    """Derived widget state: ``state`` is ``tool``, ``model`` or ``empty``."""

    state: str  # "tool" | "model" | "empty"
    current: tuple[LogEvent, ...] = ()
    since: LogEvent | None = None
    last_call: LogEvent | None = None
    last_result: LogEvent | None = None
    last_message: LogEvent | None = None


def turn_pairs(events: tuple[LogEvent, ...]) -> dict[int, int]:
    """Pair calls with results within each model turn.

    The stream splits at ``model_response``; each turn pairs FIFO per
    ``(tool, thread)``. An abandoned call in an earlier turn can never
    swallow a later turn's result. Never raises.
    """
    try:
        rows = tuple(events)
    except TypeError:
        return {}
    pairs: dict[int, int] = {}
    turn: list[LogEvent] = []
    for ev in (*rows, None):
        if ev is None or (isinstance(ev, LogEvent) and ev.event == "model_response"):
            try:
                pairs.update(pair_tool_calls(tuple(turn)))
            except Exception:
                pass
            turn = []
        elif isinstance(ev, LogEvent):
            turn.append(ev)
    return pairs


def find_activity(events: tuple[LogEvent, ...]) -> Activity:
    """Derive the widget state from the full ordered event stream.

    The current call is a ``tool_call`` with Evidence after the latest
    ``model_response`` that has no paired result in its own turn; otherwise
    the model is working (timed from the last event). The last call is the
    latest paired call with Evidence. Never raises.
    """
    try:
        rows = tuple(events)
    except TypeError:
        return Activity("empty")
    rows = tuple(ev for ev in rows if isinstance(ev, LogEvent))
    if not rows:
        return Activity("empty")
    try:
        pairs = turn_pairs(rows)
    except Exception:
        pairs = {}
    by_seq = {ev.seq: ev for ev in rows}
    last_response = max(
        (ev.seq for ev in rows if ev.event == "model_response"), default=0
    )
    current = tuple(
        ev
        for ev in rows
        if ev.event == "tool_call"
        and ev.seq > last_response
        and ev.seq not in pairs
        and ev.evidence is not None
    )
    paired_calls = [
        ev
        for ev in rows
        if ev.event == "tool_call" and ev.seq in pairs and ev.evidence is not None
    ]
    last_call = paired_calls[-1] if paired_calls else None
    last_result = by_seq.get(pairs[last_call.seq]) if last_call is not None else None
    if last_result is not None and not isinstance(last_result, LogEvent):
        last_result = None
    last_message = next(
        (
            ev
            for ev in reversed(rows)
            if ev.event == "model_response" and ev.evidence is not None
        ),
        None,
    )
    if current:
        return Activity("tool", current, current[0], last_call, last_result, last_message)
    return Activity("model", (), rows[-1], last_call, last_result, last_message)


def parse_ts(text: str | None) -> datetime | None:
    """Parse an event timestamp, or ``None`` when missing/unparseable."""
    if not isinstance(text, str) or not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def elapsed_text(
    start: str | None, end: str | None = None, *, now: datetime | None = None
) -> str:
    """Human elapsed time between two event timestamps (or ``now``).

    ``""`` when either end is missing or unparseable. Renders ``12.0 s``
    under ten seconds, ``12 s`` under two minutes, then ``3 min 04 s`` and
    ``2 h 05 min``.
    """
    begin = parse_ts(start)
    finish = parse_ts(end) if end else now
    if begin is None or finish is None:
        return ""
    seconds = max(0.0, (finish - begin).total_seconds())
    if seconds < 10:
        return f"{seconds:.1f} s"
    if seconds < 120:
        return f"{seconds:.0f} s"
    minutes, sec = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes} min {sec:02d} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min"


def clip_text(text: str) -> str:
    """Clip file text to the preview budget, marking the cut with ``…``."""
    lines = text.strip("\n").splitlines()
    clipped = len(lines) > PREVIEW_LINES
    out = "\n".join(lines[:PREVIEW_LINES])
    if len(out) > PREVIEW_CHARS:
        out, clipped = out[:PREVIEW_CHARS], True
    return out + ("\n…" if clipped else "")


def _load(content: bytes) -> Any:
    try:
        return json.loads(content.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def _compact(data: Any) -> str:
    if isinstance(data, dict):
        data = {key: value for key, value in data.items() if key != "agent_id"}
    return json.dumps(data, indent=1, ensure_ascii=False)


def _edit_text(data: dict[str, Any]) -> str:
    """One ``-/+`` diff of a Write/Edit call payload."""
    if "content" in data:
        return str(data["content"])
    edits = data.get("edits") if isinstance(data.get("edits"), list) else [data]
    out: list[str] = []
    for edit in edits:
        if not isinstance(edit, dict):
            continue
        for line in str(edit.get("old_string", "")).splitlines():
            out.append("- " + line)
        for line in str(edit.get("new_string", "")).splitlines():
            out.append("+ " + line)
    return "\n".join(out)


def call_preview(tool: str, content: bytes) -> tuple[str, str]:
    """``(headline, body)`` preview of one tool_call Evidence file.

    Bash shows the description plus the command; Read/Edit/Write show the
    path plus the ``-/+`` lines; a subagent prompt shows the description
    plus the prompt; anything else is compact JSON without ``agent_id``.
    Never raises.
    """
    try:
        raw = content.decode("utf-8", errors="replace")
    except Exception:
        return "", ""
    data = _load(content)
    if not isinstance(data, dict):
        return "", clip_text(raw)
    try:
        if "command" in data:
            return str(data.get("description") or ""), clip_text(str(data["command"]))
        if "file_path" in data and (
            "content" in data or "old_string" in data or "edits" in data
        ):
            return str(data["file_path"]), clip_text(_edit_text(data))
        for key in ("file_path", "path", "pattern", "url", "query"):
            if key in data:
                rest = {k: v for k, v in data.items() if k not in (key, "agent_id")}
                return str(data[key]), clip_text(_compact(rest)) if rest else ""
        if "prompt" in data:
            return str(data.get("description") or ""), clip_text(str(data["prompt"]))
        return "", clip_text(_compact(data))
    except Exception:
        return "", clip_text(raw)


def result_preview(
    tool: str, content: bytes, truncated: bool
) -> tuple[str, str, bool]:
    """``(headline, body, is_error)`` preview of one tool_result file.

    Bash shows stdout then stderr; Read shows the path, line count and
    content; Edit/Write show the path, type and structured-patch lines;
    anything else is compact JSON without ``agent_id``. Failed or
    interrupted results set ``is_error``. Never raises.
    """
    del tool
    try:
        raw = content.decode("utf-8", errors="replace")
    except Exception:
        return "", "", False
    data = _load(content)
    if not isinstance(data, dict):
        return ("first bytes" if truncated else ""), clip_text(raw), False
    try:
        is_error = bool(data.get("is_error") or data.get("interrupted"))
        if "stdout" in data or "stderr" in data:
            out = str(data.get("stdout") or "")
            err = str(data.get("stderr") or "")
            body = out + (("\n[stderr]\n" + err) if err else "")
            return "", clip_text(body) or "(no output)", is_error
        file = data.get("file")
        if isinstance(file, dict) and "content" in file:
            head = f"{file.get('filePath', '')} · {file.get('numLines', '?')} lines"
            return head, clip_text(str(file["content"])), is_error
        if "filePath" in data:
            head = str(data["filePath"]) + (
                f" · {data['type']}" if data.get("type") else ""
            )
            patch = data.get("structuredPatch")
            if isinstance(patch, list) and patch:
                lines = [
                    line
                    for hunk in patch
                    if isinstance(hunk, dict)
                    for line in hunk.get("lines", [])
                    if isinstance(line, str)
                ]
                return head, clip_text("\n".join(lines)), is_error
            if data.get("type") == "create":
                return head, "(written)", is_error
            return head, "(ok)", is_error
        return "", clip_text(_compact(data)), is_error
    except Exception:
        return "", clip_text(raw), False
