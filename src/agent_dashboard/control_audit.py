"""Control audit writer (spec section 7).

Append-only JSONL at ``$XDG_STATE_HOME/agent-dashboard/control-audit.jsonl``;
when ``XDG_STATE_HOME`` is unset, ``~/.local/state/agent-dashboard/`` (the
XDG default). The directory is created on first write with mode ``0700``,
the file with ``0600``. Every entry is also written to stdout as one line,
``control-audit <json>``, for journald or ``docker logs``. There is no
rotation.

One process-wide lock serialises writes. Each entry is one ``write`` of one
line, then ``flush`` and ``os.fsync``. A failed write is reported to the
caller (the submit route fails closed on ``submitted``); a failed refusal
write still returns the refusal and the caller logs to stderr.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

AUDIT_DIR_NAME = "agent-dashboard"
AUDIT_FILE_NAME = "control-audit.jsonl"

_lock = threading.Lock()


def audit_dir() -> Path:
    """The audit directory: ``$XDG_STATE_HOME/agent-dashboard/`` or the fallback."""
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg is not None and xdg.strip():
        base = Path(xdg.strip())
    else:
        base = Path.home() / ".local" / "state"
    return base / AUDIT_DIR_NAME


def audit_path() -> Path:
    """The audit file path (directory plus ``control-audit.jsonl``)."""
    return audit_dir() / AUDIT_FILE_NAME


def utc_now_ms() -> str:
    """Current UTC time as ISO 8601 with milliseconds (``...Z``)."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def refused_entry(
    *,
    instance: str,
    reason: str,
    error: str,
    request_id: str | None = None,
    container_id: str | None = None,
    expected_repository: str | None = None,
    action: str | None = None,
    params: dict[str, Any] | None = None,
    command: str = "",
    agent_version: str | None = None,
    expect_repo_guard: bool = False,
) -> dict[str, Any]:
    """Build a ``refused`` audit entry (spec section 7).

    ``request_id`` is null for a refusal before one exists (review, or a
    malformed submit). ``container_id`` is the form's value, even when the
    binding check refused it; ``expected_repository`` is the repository the
    form carried. ``retry`` is always false on review refusals.
    """
    return {
        "at": utc_now_ms(),
        "outcome": "refused",
        "request_id": request_id,
        "instance": instance,
        "container_id": container_id,
        "expected_repository": expected_repository,
        "action": action,
        "params": dict(params) if params is not None else {},
        "command": command,
        "agent_version": agent_version,
        "expect_repo_guard": expect_repo_guard,
        "retry": False,
        "exit_code": None,
        "sequence": None,
        "acknowledgement": None,
        "reply_repository": None,
        "repository_mismatch": None,
        "reason": reason,
        "error": error,
        "duration_ms": None,
    }


def append_entry(entry: dict[str, Any]) -> Path:
    """Append one entry to the audit file and mirror it to stdout.

    Creates the directory (mode ``0700``) and the file (mode ``0600``) on
    first write. One ``write`` of one line, then ``flush`` and ``fsync``,
    under one process-wide lock. The same JSON line is printed to stdout
    as ``control-audit <json>``. Any I/O failure is raised to the caller.
    """
    line = json.dumps(entry, separators=(",", ":"))
    path = audit_path()
    with _lock:
        directory = path.parent
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.chmod(str(path), 0o600)
            data = (line + "\n").encode("utf-8")
            written = 0
            # Entries are small so this is one write in practice; loop on a
            # partial write so the file never holds a torn non-JSON line.
            while written < len(data):
                chunk = os.write(fd, data[written:])
                if chunk == 0:  # pragma: no cover - defensive
                    raise OSError("short write to the Control audit file")
                written += chunk
            os.fsync(fd)
        finally:
            os.close(fd)
        print(f"control-audit {line}", flush=True)
    return path


def log_refusal_failure(reason: str, error: Exception) -> None:
    """Log a failed refusal write to stderr (the refusal itself still stands)."""
    print(f"control-audit refusal write failed ({reason}): {error}", file=sys.stderr)


__all__ = [
    "AUDIT_DIR_NAME",
    "AUDIT_FILE_NAME",
    "append_entry",
    "audit_dir",
    "audit_path",
    "log_refusal_failure",
    "refused_entry",
    "utc_now_ms",
]
