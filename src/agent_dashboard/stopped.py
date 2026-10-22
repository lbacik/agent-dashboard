"""Attempt log reads for stopped managed instances.

``docker exec`` fails on stopped containers, so the attempt list and the
event stream arrive through ``docker cp`` tree copies instead (see
:func:`gateway.argv_copy_logs` and :func:`gateway.argv_copy_run`), parsed
here on the dashboard side. The parsers mirror the running-instance exec
collectors (:data:`gateway.HISTORY_SCRIPT`,
:data:`gateway.ATTEMPT_LOG_SCRIPT`, :data:`gateway.ATTEMPT_EVENTS_SCRIPT`)
entry for entry: the same name shapes are accepted, the same byte caps
apply, and the counters move the same way, so a stopped instance renders
exactly what the same exec would have rendered while it was running.

Safety mirrors the gateway contract: the copy argv carries only the
validated container ID plus fixed paths, so tar member names never reach
argv. They are re-validated here before use (digits-only issue,
timestamp-shaped run directory, recognised file names); anything else is
ignored. Only regular-file members are read; symlinks, directories and
anything outside the expected shapes never become data.
"""

from __future__ import annotations

import io
import json
import tarfile
from typing import Any

from .gateway import (
    ATTEMPT_EVENTS_LAYOUT,
    ATTEMPT_ISSUE_RE,
    ATTEMPT_LOG_LAYOUT,
    ATTEMPT_LOG_TAIL_LAYOUT,
    ATTEMPT_STARTED_DASH_RE,
    LOG_STREAM_CAP,
    LOG_TAIL_CAP,
    validate_attempt_ref,
)

#: Layout marker the stopped-instance list parser stamps on its output.
#: Identical to the exec collector's marker so the history reader runs
#: unchanged on either source.
HISTORY_LAYOUT = "attempt-logs/v1"

#: Cap on one ``attempt.json`` inside a copied tree, mirroring the exec
#: collectors: a larger file reads as missing (list) or unreadable.
MAX_ATTEMPT_BYTES = 1000000

#: Cap on the ``agent_output.json`` bytes kept for the progress chart,
#: mirroring the events exec collector: past this size the payload is
#: marked truncated and carries no events.
MAX_PROGRESS_EVENTS_BYTES = 2000000

#: Events the progress chart needs, mirroring the events exec collector's
#: keep set (periodic limit readings plus threshold and handoff events).
KEEP_EVENTS = frozenset(
    [
        "limits_checked",
        "token_soft_threshold_crossed",
        "token_soft_threshold_subagents_stopped",
        "token_soft_threshold_handoff_note_committed",
    ]
)

#: Run-level files the list and run parsers recognise by exact name.
ATTEMPT_FILE = "attempt.json"
EVENTS_FILE = "agent_output.json"


def _parts(name: Any) -> list[str] | None:
    """Split a tar member name into path parts, or ``None`` when unsafe.

    Empty parts and ``.`` are dropped; any ``..`` (the daemon resolves it,
    and there is no other barrier) or absolute path rejects the member.
    """
    if not isinstance(name, str) or not name:
        return None
    if name.startswith("/"):
        return None
    parts = [chunk for chunk in name.split("/") if chunk not in ("", ".")]
    if not parts or any(chunk == ".." for chunk in parts):
        return None
    return parts


def _members(data: bytes):
    """Yield ``(parts, size, read)`` for each regular-file member.

    ``read`` returns the member's content bytes when called; other members
    (directories, symlinks, devices, pax headers) are skipped, as are
    members with unsafe names. Never raises on garbage input: a non-tar
    stream simply yields nothing.
    """
    try:
        tar = tarfile.open(fileobj=io.BytesIO(data), mode="r:")
    except (tarfile.TarError, EOFError, OSError, ValueError):
        return
    with tar:
        while True:
            try:
                member = tar.next()
            except (tarfile.TarError, EOFError, OSError, ValueError):
                return
            if member is None:
                return
            if not member.isfile():
                continue
            parts = _parts(member.name)
            if parts is None:
                continue
            try:
                handle = tar.extractfile(member)
                content = handle.read() if handle is not None else b""
            except (tarfile.TarError, EOFError, OSError, ValueError):
                continue
            yield parts, member.size, content


def _read_attempt(content: bytes) -> tuple[dict[str, Any] | None, bool]:
    """Parse one ``attempt.json`` content: ``(record, counted)``.

    ``counted`` tells the caller to bump ``unreadable``: an oversized or
    unparsable file, or a JSON value that is not an object, counts, while a
    simply-missing file does not (the caller never calls this for one).
    """
    if len(content) > MAX_ATTEMPT_BYTES:
        return None, True
    try:
        record = json.loads(content.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None, True
    if not isinstance(record, dict):
        return None, True
    return record, False


def parse_logs_tree(data: bytes) -> dict[str, Any]:
    """Build a history-layout payload from a ``/data/logs`` tree copy.

    One entry per run directory (even when ``attempt.json`` is missing or
    unparsable, so the Attempt log stays reachable), with ``has_events``
    set when ``agent_output.json`` is present. ``active_started_at`` is
    always ``None``: a stopped instance has no active attempt, so runs
    without an outcome read as ``interrupted``.
    """
    attempts: dict[tuple[int, str], dict[str, Any] | None] = {}
    has_events: set[tuple[int, str]] = set()
    unreadable = 0
    for parts, _size, content in _members(data):
        if len(parts) < 3:
            continue
        issue_raw, started_raw, filename = parts[-3], parts[-2], parts[-1]
        if ATTEMPT_ISSUE_RE.fullmatch(issue_raw) is None:
            continue
        if ATTEMPT_STARTED_DASH_RE.fullmatch(started_raw) is None:
            continue
        key = (int(issue_raw), started_raw)
        if filename == EVENTS_FILE:
            has_events.add(key)
            attempts.setdefault(key, None)
        elif filename == ATTEMPT_FILE:
            if key in attempts and attempts[key] is not None:
                continue
            record, counted = _read_attempt(content)
            if counted:
                unreadable += 1
            attempts[key] = record
    entries = [
        {
            "issue": issue,
            "started": started,
            "attempt": attempts[(issue, started)],
            "has_events": (issue, started) in has_events,
        }
        for issue, started in sorted(attempts)
    ]
    return {
        "layout": HISTORY_LAYOUT,
        "attempts": entries,
        "active_started_at": None,
        "unreadable": unreadable,
    }


def _stream_events(
    content: bytes, cap: int
) -> tuple[list[dict[str, Any]], int, int, bool]:
    """Parse one ``agent_output.json`` content into kept event entries.

    Returns ``(events, total_events, unreadable, truncated)`` with the exec
    log collector's semantics: blank lines are skipped, a line that does not
    parse (or is not an object) becomes a synthetic ``unparsable_line``
    WARNING row so row numbers keep matching the file, and past ``cap``
    bytes the head is kept with ``truncated`` set and the true ``size``
    reported. Only the ``timestamp``/``level``/``event``/``detail`` keys are
    kept, as over the exec boundary.
    """
    if len(content) > cap:
        return [], 0, 0, True
    try:
        raw = content.decode("utf-8")
    except UnicodeDecodeError:
        return [], 0, 0, True
    events: list[dict[str, Any]] = []
    total_events = 0
    for line in raw.splitlines():
        if not line.strip():
            continue
        total_events += 1
        try:
            entry = json.loads(line)
        except ValueError:
            entry = None
        if not isinstance(entry, dict):
            events.append(
                {
                    "timestamp": None,
                    "level": "WARNING",
                    "event": "unparsable_line",
                    "detail": line[:200],
                }
            )
            continue
        events.append(
            {
                "timestamp": entry.get("timestamp"),
                "level": entry.get("level"),
                "event": entry.get("event"),
                "detail": entry.get("detail"),
            }
        )
    return events, total_events, 0, False


def _tail_events(
    content: bytes, cap: int
) -> tuple[list[dict[str, Any]], int, bool]:
    """Parse the last ``cap`` bytes of one ``agent_output.json`` content.

    Returns ``(events, total_events, truncated)`` with the tail exec
    collector's semantics: blank lines are skipped, a line that does not
    parse (or is not an object) becomes a synthetic ``unparsable_line``
    WARNING row, and past ``cap`` bytes the tail is kept with ``truncated``
    set. ``total_events`` is the full stream's non-blank line count, so the
    caller can number rows with the file's line numbers. Only the
    ``timestamp``/``level``/``event``/``detail`` keys are kept, as over the
    exec boundary. Never raises on garbage input.
    """
    try:
        total = sum(1 for line in content.splitlines() if line.strip())
    except Exception:
        return [], 0, True
    if total == 0:
        return [], 0, False
    if len(content) <= cap:
        truncated = False
        tail = content
        aligned = True
    else:
        truncated = True
        tail = content[-cap:]
        start = len(content) - cap
        aligned = start <= 0 or content[start - 1 : start] == b"\n"
    try:
        raw = tail.decode("utf-8", errors="replace")
    except Exception:
        return [], total, True
    lines = raw.splitlines()
    if not aligned and lines:
        lines = lines[1:]
    events: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            entry = None
        if not isinstance(entry, dict):
            events.append(
                {
                    "timestamp": None,
                    "level": "WARNING",
                    "event": "unparsable_line",
                    "detail": line[:200],
                }
            )
            continue
        events.append(
            {
                "timestamp": entry.get("timestamp"),
                "level": entry.get("level"),
                "event": entry.get("event"),
                "detail": entry.get("detail"),
            }
        )
    return events, total, truncated


def parse_run_tree_tail(
    data: bytes, *, issue: int, started: str
) -> tuple[dict[str, Any], list[str]]:
    """Build a tail-layout payload from one run-directory copy.

    Returns the payload plus every regular file's base name in the run.
    Semantics mirror the tail exec collector: the newest
    :data:`LOG_TAIL_CAP` bytes aligned to whole lines, the full stream's
    count in ``total_events`` for file line numbers, and the true member
    size in ``file_size``.
    """
    _number, dash_started = validate_attempt_ref(issue, started)
    attempt: dict[str, Any] | None = None
    attempt_seen = False
    unreadable = 0
    events: list[dict[str, Any]] = []
    total_events = 0
    truncated = False
    file_size = 0
    names: list[str] = []
    for parts, size, content in _members(data):
        if len(parts) < 2:
            continue
        run_raw, filename = parts[-2], parts[-1]
        if ATTEMPT_STARTED_DASH_RE.fullmatch(run_raw) is None:
            continue
        if run_raw != dash_started:
            continue
        if not filename:
            continue
        names.append(filename)
        if filename == ATTEMPT_FILE and not attempt_seen:
            attempt_seen = True
            if len(content) > MAX_ATTEMPT_BYTES:
                unreadable += 1
            else:
                try:
                    record = json.loads(content.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    unreadable += 1
                else:
                    if isinstance(record, dict):
                        attempt = record
                    else:
                        unreadable += 1
        elif filename == EVENTS_FILE and not events and not truncated:
            file_size = size
            events, total_events, truncated = _tail_events(content, LOG_TAIL_CAP)
    return (
        {
            "layout": ATTEMPT_LOG_TAIL_LAYOUT,
            "issue": _number,
            "started": dash_started,
            "attempt": attempt,
            "events": events,
            "total_events": total_events,
            "unreadable": unreadable,
            "truncated": truncated,
            "file_size": file_size,
        },
        sorted(set(names)),
    )


def parse_run_tree(
    data: bytes, *, issue: int, started: str
) -> tuple[dict[str, Any], list[str]]:
    """Build an attempt-log-layout payload from one run-directory copy.

    Returns the payload plus every regular file's base name in the run
    (used for the command-log links; names are validated again before any
    file is read). Only members under the validated ``started`` directory
    are accepted. Semantics mirror the log exec collector: a missing
    ``attempt.json`` reads as ``None`` without counting, an unparsable one
    counts once, and a missing event stream yields an empty event list.
    """
    _number, dash_started = validate_attempt_ref(issue, started)
    attempt: dict[str, Any] | None = None
    attempt_seen = False
    unreadable = 0
    events: list[dict[str, Any]] = []
    total_events = 0
    truncated = False
    file_size = 0
    names: list[str] = []
    for parts, size, content in _members(data):
        if len(parts) < 2:
            continue
        run_raw, filename = parts[-2], parts[-1]
        if ATTEMPT_STARTED_DASH_RE.fullmatch(run_raw) is None:
            continue
        if run_raw != dash_started:
            continue
        if not filename:
            continue
        names.append(filename)
        if filename == ATTEMPT_FILE and not attempt_seen:
            attempt_seen = True
            if len(content) > MAX_ATTEMPT_BYTES:
                unreadable += 1
            else:
                try:
                    record = json.loads(content.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    unreadable += 1
                else:
                    if isinstance(record, dict):
                        attempt = record
                    else:
                        unreadable += 1
        elif filename == EVENTS_FILE and not events and not truncated:
            file_size = size
            events, total_events, _unread, truncated = _stream_events(
                content, LOG_STREAM_CAP
            )
    return (
        {
            "layout": ATTEMPT_LOG_LAYOUT,
            "issue": _number,
            "started": dash_started,
            "attempt": attempt,
            "events": events,
            "total_events": total_events,
            "unreadable": unreadable,
            "truncated": truncated,
            "file_size": file_size,
        },
        sorted(set(names)),
    )


def events_payload_from_run_tree(
    data: bytes, *, issue: int, started: str
) -> dict[str, Any]:
    """Build an attempt-events-layout payload from one run-directory copy.

    Semantics mirror the events exec collector: the summary counts a
    missing or unparsable ``attempt.json`` once, and only the progress
    chart's kept events cross (periodic limit readings plus threshold and
    handoff events); the full stream never leaves the archive. Past
    :data:`MAX_PROGRESS_EVENTS_BYTES` the payload is marked truncated and
    carries no events.
    """
    _number, dash_started = validate_attempt_ref(issue, started)
    attempt: dict[str, Any] | None = None
    attempt_seen = False
    unreadable = 0
    kept: list[dict[str, Any]] = []
    total_events = 0
    truncated = False
    for parts, _size, content in _members(data):
        if len(parts) < 2:
            continue
        run_raw, filename = parts[-2], parts[-1]
        if ATTEMPT_STARTED_DASH_RE.fullmatch(run_raw) is None:
            continue
        if run_raw != dash_started:
            continue
        if filename == ATTEMPT_FILE and not attempt_seen:
            attempt_seen = True
            record, counted = _read_attempt(content)
            if counted:
                unreadable += 1
            else:
                attempt = record
        elif filename == EVENTS_FILE and not kept and not truncated:
            if len(content) > MAX_PROGRESS_EVENTS_BYTES:
                truncated = True
                continue
            try:
                raw = content.decode("utf-8")
            except UnicodeDecodeError:
                truncated = True
                continue
            for line in raw.splitlines():
                if not line.strip():
                    continue
                total_events += 1
                try:
                    entry = json.loads(line)
                except ValueError:
                    unreadable += 1
                    continue
                if not isinstance(entry, dict):
                    unreadable += 1
                    continue
                name = entry.get("event")
                if not isinstance(name, str):
                    continue
                if name not in KEEP_EVENTS and "handoff" not in name.lower():
                    continue
                kept.append(
                    {
                        "timestamp": entry.get("timestamp"),
                        "level": entry.get("level"),
                        "event": name,
                        "detail": entry.get("detail"),
                    }
                )
    return {
        "layout": ATTEMPT_EVENTS_LAYOUT,
        "issue": _number,
        "started": dash_started,
        "attempt": attempt,
        "events": kept,
        "total_events": total_events,
        "unreadable": unreadable,
        "truncated": truncated,
    }
