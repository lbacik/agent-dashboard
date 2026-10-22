"""PROTOTYPE (throwaway, branch prototype/fleet-view-logs): drawer activity widget.

Derives "what is the agent doing now" and "last call + response" from one
attempt's event stream. A ``tool_call`` with no paired result after the
latest ``model_response`` is the current call; otherwise the model is
working. Earlier unpaired calls (interrupted, abandoned) are ignored.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .attempt_log import LogEvent, pair_tool_calls

PREVIEW_LINES = 14
PREVIEW_CHARS = 1500


@dataclass(frozen=True)
class Activity:
    state: str  # "tool" | "model" | "empty"
    current: tuple[LogEvent, ...] = ()
    since: LogEvent | None = None
    last_call: LogEvent | None = None
    last_result: LogEvent | None = None
    last_message: LogEvent | None = None


def turn_pairs(events: tuple[LogEvent, ...]) -> dict[int, int]:
    """FIFO pairing within each model turn (split at ``model_response``), so
    an abandoned call never swallows a later call's result."""
    pairs: dict[int, int] = {}
    turn: list[LogEvent] = []
    for ev in (*events, None):
        if ev is None or ev.event == "model_response":
            pairs.update(pair_tool_calls(tuple(turn)))
            turn = []
        else:
            turn.append(ev)
    return pairs


def find_activity(events: tuple[LogEvent, ...]) -> Activity:
    pairs = turn_pairs(events)
    by_seq = {ev.seq: ev for ev in events}
    last_response = max(
        (ev.seq for ev in events if ev.event == "model_response"), default=0
    )
    current = tuple(
        ev
        for ev in events
        if ev.event == "tool_call" and ev.seq > last_response and ev.seq not in pairs
    )
    paired_calls = [
        ev for ev in events if ev.event == "tool_call" and ev.seq in pairs and ev.evidence
    ]
    last_call = paired_calls[-1] if paired_calls else None
    last_result = by_seq.get(pairs[last_call.seq]) if last_call else None
    last_message = next(
        (ev for ev in reversed(events) if ev.event == "model_response" and ev.evidence),
        None,
    )
    if current:
        return Activity("tool", current, current[0], last_call, last_result, last_message)
    if not events:
        return Activity("empty")
    return Activity("model", (), events[-1], last_call, last_result, last_message)


def parse_ts(text: str) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def elapsed_text(start: str, end: str | None = None, *, now: datetime | None = None) -> str:
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


def _clip(text: str) -> str:
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
        data = {k: v for k, v in data.items() if k != "agent_id"}
    return json.dumps(data, indent=1, ensure_ascii=False)


def call_preview(tool: str, content: bytes) -> tuple[str, str]:
    """(headline, body) for one tool_call file."""
    data = _load(content)
    if not isinstance(data, dict):
        return "", _clip(content.decode("utf-8", errors="replace"))
    if "command" in data:
        return str(data.get("description") or ""), _clip(str(data["command"]))
    if "file_path" in data and ("content" in data or "old_string" in data or "edits" in data):
        return str(data["file_path"]), _clip(_edit_text(data))
    for key in ("file_path", "path", "pattern", "url", "query"):
        if key in data:
            rest = {k: v for k, v in data.items() if k not in (key, "agent_id")}
            return str(data[key]), _clip(_compact(rest)) if rest else ""
    if "prompt" in data:
        return str(data.get("description") or ""), _clip(str(data["prompt"]))
    return "", _clip(_compact(data))


def _edit_text(data: dict[str, Any]) -> str:
    """Write content as is; edits as -old / +new lines."""
    if "content" in data:
        return str(data["content"])
    edits = data.get("edits") if isinstance(data.get("edits"), list) else [data]
    out: list[str] = []
    for edit in edits:
        if not isinstance(edit, dict):
            continue
        out += ["- " + line for line in str(edit.get("old_string", "")).splitlines()]
        out += ["+ " + line for line in str(edit.get("new_string", "")).splitlines()]
    return "\n".join(out)


def result_preview(tool: str, content: bytes, truncated: bool) -> tuple[str, str, bool]:
    """(headline, body, is_error) for one tool_result file."""
    data = _load(content)
    if not isinstance(data, dict):
        text = content.decode("utf-8", errors="replace")
        return ("first bytes" if truncated else ""), _clip(text), False
    is_error = bool(data.get("is_error") or data.get("interrupted"))
    if "stdout" in data or "stderr" in data:
        out = str(data.get("stdout") or "")
        err = str(data.get("stderr") or "")
        body = out + (("\n[stderr]\n" + err) if err else "")
        return "", _clip(body) or "(no output)", is_error
    file = data.get("file")
    if isinstance(file, dict) and "content" in file:
        head = f"{file.get('filePath', '')} · {file.get('numLines', '?')} lines"
        return head, _clip(str(file["content"])), is_error
    if "filePath" in data:
        head = str(data["filePath"]) + (f" · {data['type']}" if data.get("type") else "")
        patch = data.get("structuredPatch")
        if isinstance(patch, list) and patch:
            lines = [
                line
                for hunk in patch
                if isinstance(hunk, dict)
                for line in hunk.get("lines", [])
                if isinstance(line, str)
            ]
            return head, _clip("\n".join(lines)), is_error
        return head, "(written)" if data.get("type") == "create" else "(ok)", is_error
    return "", _clip(_compact(data)), is_error
