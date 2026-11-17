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
  previews apply unchanged. A backend without those events reads as the
  model working, never as an error.
- the Evidence files themselves are archive members (``tool_call``,
  ``tool_result``, ``model_output`` kinds), read in one
  ``attempts export --path ...`` exec.

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
