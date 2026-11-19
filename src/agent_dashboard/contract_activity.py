"""Contract Activity: ``Now`` and ``Last call`` from the Attempt Archive.

The drawer's Activity widget shows the same panes for a contract instance
as for a legacy one (ADR 0017): the tool call running now (or the model
working), and the last paired call with its response. The legacy widget
reads the Attempt log tail; a contract instance exposes the same material
through its Attempt Archive instead:

- ``events.jsonl`` (the contract requires it in every archive) holds the
  attempt's events. A backend that reports its tool calls as
  ``native.tool_call``/``native.tool_result``/``native.model_response``
  events whose text names the Evidence file (``Bash [main] ->
  0244_tool_call.json``, simple-coding-agent's shape) maps one-to-one onto
  the legacy event rows, so :func:`activity.find_activity` and the legacy
  previews apply unchanged.
- the Evidence files themselves are archive members (``tool_call``,
  ``tool_result``, ``model_output`` kinds), read in one
  ``attempts export --path ...`` exec.
- a backend whose ``events.jsonl`` names no Evidence (coding-agent's
  carries only lifecycle and step events) keeps its model turns in a
  ``transcript.jsonl`` member instead: one ``request``/``response`` record
  per model call, tool calls and results inline. :func:`read_transcript`
  maps it onto the same legacy rows plus in-memory Evidence files, so the
  same panes render. A backend with neither reads as the model working,
  never as an error.

Every member is verified (size and ``sha256``) against the export's own
manifest before it is used; nothing about the unsealed archive is kept
between reads (ADR 0014's Sealed-only cache stays intact).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .attempt_log import LogEvent, read_log_event
from .contract_attempt import extract_export_member, parse_contract_attempt_export

#: The archive member holding the attempt's events (contract-required name).
EVENTS_PATH = "events.jsonl"

#: Native event names that map onto the legacy Evidence events.
NATIVE_EVIDENCE_EVENTS = {
    "native.tool_call": "tool_call",
    "native.tool_result": "tool_result",
    "native.model_response": "model_response",
}


def read_archive_events(data: bytes) -> tuple[LogEvent, ...]:
    """Read ``events.jsonl`` bytes into ordered legacy event rows.

    Each Contract Event becomes one row: ``at`` is the timestamp, ``text``
    the detail, and the native Evidence events take their legacy names so
    the Evidence reference parses. ``seq`` is the 1-based line number.
    Undecodable lines become synthetic rows; never raises.
    """
    if not isinstance(data, bytes):
        return ()
    rows: list[LogEvent] = []
    for seq, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
        if not line.strip():
            continue
        try:
            entry: Any = json.loads(line)
        except ValueError:
            entry = line
        if isinstance(entry, dict):
            name = entry.get("event")
            level = entry.get("level")
            entry = {
                "timestamp": entry.get("at"),
                "level": level.upper() if isinstance(level, str) else level,
                "event": NATIVE_EVIDENCE_EVENTS.get(name, name)
                if isinstance(name, str)
                else name,
                "detail": entry.get("text"),
            }
        rows.append(read_log_event(entry, seq))
    return tuple(rows)


#: The archive member holding the model transcript (coding-agent's name).
TRANSCRIPT_PATH = "transcript.jsonl"


def has_evidence(rows: Sequence[LogEvent]) -> bool:
    """Whether any row names an Evidence file (the events carry tool calls)."""
    return any(row.evidence is not None for row in rows)


@dataclass(frozen=True)
class TranscriptActivity:
    """A transcript as legacy event rows plus the Evidence files they name."""

    events: tuple[LogEvent, ...] = ()
    files: dict[str, bytes] | None = None


def _thread(conversation: Any) -> str:
    """The legacy thread tag for a transcript conversation label."""
    if (
        isinstance(conversation, str)
        and conversation.startswith("subagent:")
        and "]" not in conversation
        and len(conversation) > len("subagent:")
    ):
        return conversation
    return "main"


def _message_text(content: Any) -> str:
    """The model's text from a response's content (thinking when no text)."""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    texts: list[str] = []
    thinking: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            texts.append(block["text"])
        elif block.get("type") == "thinking" and isinstance(block.get("thinking"), str):
            thinking.append(block["thinking"])
    return "\n".join(texts or thinking).strip()


def _tool_output(content: Any) -> str:
    """A tool message's content as text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block.get("text") if isinstance(block, dict) else block
            for block in content
        ]
        if all(isinstance(part, str) for part in parts):
            return "\n".join(parts)
    return json.dumps(content, indent=1, ensure_ascii=False, default=str)


def read_transcript(data: bytes) -> TranscriptActivity:
    """Map ``transcript.jsonl`` bytes onto legacy rows and Evidence files.

    Every ``response`` record becomes a ``model_response`` row (linking its
    text, when it has any) followed by one ``tool_call`` row per call;
    every ``request`` record becomes a plain ``model_request`` row (so the
    model-working time runs from the request) after one ``tool_result`` row
    per tool message it carries, matched to its call by id. The Evidence
    files are synthesized in memory under numbered names, so
    :func:`activity.find_activity` and the previews apply unchanged. A
    conversation's last call (an accepted exit) has no result in the
    transcript. Undecodable lines are skipped; never raises.
    """
    if not isinstance(data, bytes):
        return TranscriptActivity()
    rows: list[LogEvent] = []
    files: dict[str, bytes] = {}
    calls: dict[str, tuple[str, str]] = {}  # call id -> (tool, thread)

    def add(at: Any, event: str, detail: str) -> None:
        rows.append(
            read_log_event(
                {"timestamp": at, "level": "INFO", "event": event, "detail": detail},
                len(rows) + 1,
            )
        )

    def evidence(suffix: str, content: bytes) -> str:
        name = f"{len(files) + 1:04d}_{suffix}"
        files[name] = content
        return name

    for line in data.decode("utf-8", errors="replace").splitlines():
        try:
            record: Any = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        at = record.get("at")
        thread = _thread(record.get("conversation"))
        if record.get("kind") == "response":
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            text = _message_text(message.get("content"))
            if text:
                file = evidence("model_response.txt", text.encode("utf-8"))
                add(at, "model_response", f"-> {file} (chars={len(text)})")
            else:
                add(at, "model_response", "")
            tool_calls = message.get("tool_calls")
            for call in tool_calls if isinstance(tool_calls, list) else ():
                if not isinstance(call, dict) or not isinstance(call.get("name"), str):
                    continue
                tool = call["name"]
                args = call.get("args")
                file = evidence(
                    "tool_call.json",
                    json.dumps(
                        args if isinstance(args, dict) else {"args": args},
                        ensure_ascii=False,
                        default=str,
                    ).encode("utf-8"),
                )
                if isinstance(call.get("id"), str):
                    calls[call["id"]] = (tool, thread)
                add(at, "tool_call", f"{tool} [{thread}] -> {file}")
        elif record.get("kind") == "request":
            new_messages = record.get("new_messages")
            for message in new_messages if isinstance(new_messages, list) else ():
                if not isinstance(message, dict) or message.get("type") != "tool":
                    continue
                call = calls.pop(str(message.get("tool_call_id")), None)
                if call is None:
                    continue
                tool, call_thread = call
                output = _tool_output(message.get("content"))
                file = evidence("tool_result.json", output.encode("utf-8"))
                add(at, "tool_result", f"{tool} [{call_thread}] -> {file}")
            add(at, "model_request", "")
    return TranscriptActivity(tuple(rows), files)


@dataclass(frozen=True)
class MembersResult:
    """One verified multi-member export: the members' bytes, or why not."""

    ok: bool
    error: str | None = None
    members: dict[str, bytes] | None = None


def _verify(data: bytes, manifest: dict[str, Any], path: str) -> str | None:
    """Check one member against its export-manifest entry; ``None`` when good."""
    files = manifest.get("files") if isinstance(manifest, dict) else None
    entry = next(
        (
            item
            for item in files or ()
            if isinstance(item, dict) and item.get("path") == path
        ),
        None,
    )
    if entry is None:
        return f"export file {path!r} is not in the archive manifest"
    if entry.get("size") != len(data):
        return f"export file {path!r} does not match its manifest size"
    if entry.get("sha256") != hashlib.sha256(data).hexdigest():
        return f"export file {path!r} does not match its manifest sha256"
    return None


def parse_export_members(
    stdout: bytes, returncode: int | None, paths: Sequence[str], *, attempt_id: str = "?"
) -> MembersResult:
    """Parse one ``attempts export --path ...`` exec into verified members.

    A failed exec reads like a single-member export (the same messages); a
    member missing from the stream or failing its manifest size or
    ``sha256`` (a torn read of a moving archive) fails the whole read.
    Never raises.
    """
    wanted = list(paths)
    if not wanted:
        return MembersResult(ok=False, error="no archive member requested")
    if returncode != 0 or isinstance(returncode, bool):
        failed = parse_contract_attempt_export(
            stdout, returncode, attempt_id=attempt_id, wanted=wanted[0]
        )
        return MembersResult(ok=False, error=failed.error)
    members: dict[str, bytes] = {}
    for path in wanted:
        try:
            data, manifest = extract_export_member(stdout, path)
        except ValueError as exc:
            return MembersResult(ok=False, error=f"Could not read the archive: {exc}")
        problem = _verify(data, manifest, path)
        if problem is not None:
            return MembersResult(ok=False, error=f"Could not read the archive: {problem}")
        members[path] = data
    return MembersResult(ok=True, members=members)


async def fetch_members(
    fetch: Any, paths: Sequence[str], *, attempt_id: str = "?"
) -> MembersResult:
    """Run ``fetch`` (one export exec) and verify; one retry on a bad read.

    The archive of a running attempt moves (``events.jsonl`` grows), so a
    read whose member disagrees with its manifest is fetched once more
    before it reads as an error. Gateway failures propagate to the caller.
    """
    first = await fetch()
    result = parse_export_members(
        first.stdout, first.returncode, paths, attempt_id=attempt_id
    )
    if result.ok:
        return result
    second = await fetch()
    return parse_export_members(
        second.stdout, second.returncode, paths, attempt_id=attempt_id
    )
