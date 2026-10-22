"""Docker gateway: the only module that builds Docker argv.

All Docker access uses argv lists (no shell). The gateway exposes discovery
(``list_instances``), one telemetry exec per running container
(``telemetry``), one Attempt log history exec per running container
(``attempt_history``), the per-attempt event stream reads, and one Evidence
file read per file (``read_file``). Stopped containers cannot run ``docker
exec``, so their Attempt logs arrive through ``docker cp`` tree copies
(``copy_logs`` for the whole list, ``copy_run`` for one attempt), parsed on
the dashboard side with the same validation and caps. Exit codes are never
raised as errors here, except a missing Evidence file (``docker cp`` exit 1
with ``Could not find the file`` on stderr) or a missing copied tree (``no
such`` path on stderr), which map to ``FileNotFoundError``: the collector
classifies each telemetry exec into a poll outcome, and the history reader
maps its exec output into attempt summaries.
"""

from __future__ import annotations

import asyncio
import io
import re
import tarfile
from dataclasses import dataclass

AGENT_ROLE_LABEL = "io.github.lbacik.simple-coding-agent.role=agent"
ONEOFF_LABEL = "com.docker.compose.oneoff"

# Tab-separated columns: id, name, state, oneoff label value.
PS_FORMAT = '{{.ID}}\t{{.Names}}\t{{.State}}\t{{.Label "com.docker.compose.oneoff"}}'

AGENT_USER = "agent"
AGENTCTL_PATH = "/usr/local/bin/agentctl"

#: Docker container IDs are hex (short or full length); the gateway addresses
#: containers by ID only, never by name, so anything else is rejected before
#: an argv is built.
CONTAINER_ID_RE = re.compile(r"[0-9a-fA-F]{1,64}\Z")

#: Collector run inside the managed instance by ``argv_attempt_history``.
#: A fixed script (never interpolated with operator input): it walks
#: ``/data/logs/<issue>/<started_at>/`` with the verified name shapes, keeps
#: one record per run directory (even when ``attempt.json`` is missing or
#: unparsable, so the Attempt log stays reachable), flags whether
#: ``agent_output.json`` exists, reads the active checkpoint for the
#: in-progress attempt, and prints one JSON document. ``/data/logs`` itself
#: holds the paths, so no raw path ever appears in the argv.
HISTORY_SCRIPT = """\
import json
import os
import re

BASE = "/data/logs"
STATE = "/data/state/attempt.json"
ISSUE_RE = re.compile(r"[0-9]+\\Z")
STARTED_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}-[0-9]{2}-[0-9]{2}\\.[0-9]+Z\\Z")
MAX_ATTEMPT_BYTES = 1000000
MAX_STATE_BYTES = 65536


def read_capped(path, limit):
    with open(path, "rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError("file too large")
    return data


out = {"layout": "attempt-logs/v1", "attempts": [], "active_started_at": None, "unreadable": 0}
try:
    issues = sorted(os.listdir(BASE))
except OSError:
    print(json.dumps({"layout": "unknown", "reason": "missing /data/logs", "attempts": []}))
    raise SystemExit(0)
for issue in issues:
    if not ISSUE_RE.match(issue):
        continue
    issue_dir = os.path.join(BASE, issue)
    if not os.path.isdir(issue_dir):
        continue
    try:
        started_names = sorted(os.listdir(issue_dir))
    except OSError:
        out["unreadable"] += 1
        continue
    for started in started_names:
        if not STARTED_RE.match(started):
            continue
        run_dir = os.path.join(issue_dir, started)
        path = os.path.join(run_dir, "attempt.json")
        try:
            record = json.loads(read_capped(path, MAX_ATTEMPT_BYTES).decode("utf-8"))
        except (OSError, ValueError):
            out["unreadable"] += 1
            record = None
        if record is not None and not isinstance(record, dict):
            out["unreadable"] += 1
            record = None
        has_events = os.path.isfile(os.path.join(run_dir, "agent_output.json"))
        out["attempts"].append(
            {
                "issue": int(issue),
                "started": started,
                "attempt": record,
                "has_events": has_events,
            }
        )
try:
    state = json.loads(read_capped(STATE, MAX_STATE_BYTES).decode("utf-8"))
    if isinstance(state, dict) and isinstance(state.get("started_at"), str):
        out["active_started_at"] = state["started_at"]
except (OSError, ValueError):
    pass
print(json.dumps(out))
"""

#: Layout marker the per-attempt events collector stamps on its output.
ATTEMPT_EVENTS_LAYOUT = "attempt-events/v1"

#: Layout marker the per-attempt full-log collector stamps on its output.
ATTEMPT_LOG_LAYOUT = "attempt-log/v1"

#: Layout marker the per-attempt tail collector stamps on its output: the
#: newest events of the same stream with the full count, so ``seq`` stays
#: correct past the head cap.
ATTEMPT_LOG_TAIL_LAYOUT = "attempt-log-tail/v1"

#: Cap on the ``agent_output.json`` bytes the full-log collector reads: the
#: page shows the first events with a truncation notice past this size.
LOG_STREAM_CAP = 4 * 1024 * 1024

#: Cap on the ``agent_output.json`` tail bytes the tail collector reads: the
#: last bytes aligned to whole lines, so the Activity widget sees the newest
#: events even past the head cap. Counting the full stream's non-blank lines
#: is a chunked binary scan (no extra memory); the parsed tail stays bounded
#: by this cap and the read by ``log_timeout`` like the other collectors.
LOG_TAIL_CAP = 512 * 1024

#: Default bound for one Attempt log read (bigger payloads than telemetry).
LOG_EXEC_TIMEOUT = 15.0

#: At most this many Attempt log operations run at once; the semaphore lives
#: on the gateway, separate from the poll cycle's ``max_concurrent_execs``.
LOG_MAX_CONCURRENT = 4

#: Collector run inside the managed instance by ``argv_attempt_log``.
#: A fixed script (never interpolated with operator input): the attempt
#: reference travels as two argv arguments that the script re-validates
#: with the verified name shapes before joining any path, so the daemon's
#: ``..`` resolution cannot escape ``/data/logs``. It reads the attempt's
#: ``attempt.json`` summary (``None`` when missing: in progress or crashed)
#: and the full ordered ``agent_output.json`` event stream up to
#: :data:`LOG_STREAM_CAP` bytes. Lines that do not parse become synthetic
#: ``unparsable_line`` WARNING rows inline, so row numbers keep matching the
#: file; past the cap the head is kept with ``truncated`` set and the true
#: ``file_size`` reported for the notice.
ATTEMPT_LOG_SCRIPT = """\
import json
import os
import re
import sys

BASE = "/data/logs"
ISSUE_RE = re.compile(r"[0-9]+\\Z")
STARTED_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}-[0-9]{2}-[0-9]{2}\\.[0-9]+Z\\Z")
MAX_ATTEMPT_BYTES = 1000000
MAX_EVENTS_BYTES = 4194304


def read_capped(path, limit):
    with open(path, "rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError("file too large")
    return data


def unknown(reason):
    print(json.dumps({"layout": "unknown", "reason": reason}))
    raise SystemExit(0)


args = sys.argv[1:]
if len(args) != 2:
    unknown("expected issue and started arguments")
issue, started = args
if not ISSUE_RE.match(issue) or not STARTED_RE.match(started):
    unknown("invalid attempt reference")
if not os.path.isdir(BASE):
    unknown("missing /data/logs")
attempt_dir = os.path.join(BASE, issue, started)
attempt = None
unreadable = 0
try:
    record = json.loads(
        read_capped(os.path.join(attempt_dir, "attempt.json"), MAX_ATTEMPT_BYTES).decode(
            "utf-8"
        )
    )
except OSError:
    pass
except ValueError:
    unreadable += 1
else:
    if isinstance(record, dict):
        attempt = record
    else:
        unreadable += 1
events = []
total_events = 0
truncated = False
file_size = 0
events_path = os.path.join(attempt_dir, "agent_output.json")
try:
    file_size = os.path.getsize(events_path)
except OSError:
    pass
else:
    try:
        raw = read_capped(events_path, MAX_EVENTS_BYTES).decode("utf-8")
    except ValueError:
        truncated = True
        with open(events_path, "rb") as handle:
            raw = handle.read(MAX_EVENTS_BYTES).decode("utf-8", errors="replace")
    except OSError:
        unreadable += 1
        raw = None
    if raw is not None:
        for line in raw.splitlines():
            if not line.strip():
                continue
            total_events += 1
            try:
                entry = json.loads(line)
            except ValueError:
                events.append(
                    {
                        "timestamp": None,
                        "level": "WARNING",
                        "event": "unparsable_line",
                        "detail": line[:200],
                    }
                )
                continue
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
print(
    json.dumps(
        {
            "layout": "attempt-log/v1",
            "issue": int(issue),
            "started": started,
            "attempt": attempt,
            "events": events,
            "total_events": total_events,
            "unreadable": unreadable,
            "truncated": truncated,
            "file_size": file_size,
        }
    )
)
"""


#: Collector run inside the managed instance by ``argv_attempt_tail_log``.
#: A fixed script (never interpolated with operator input): the attempt
#: reference travels as two argv arguments that the script re-validates
#: with the verified name shapes before joining any path, so the daemon's
#: ``..`` resolution cannot escape ``/data/logs``. It reads the attempt's
#: ``attempt.json`` summary (``None`` when missing: in progress or crashed)
#: and the last :data:`LOG_TAIL_CAP` bytes of the ``agent_output.json``
#: event stream, aligned to whole lines. The full stream's non-blank line
#: count is scanned in binary chunks (bounded memory) so the returned rows
#: carry the file's line numbers in ``total_events``; lines that do not
#: parse become synthetic ``unparsable_line`` WARNING rows inline. Past the
#: cap the tail is kept with ``truncated`` set and the true ``file_size``
#: reported; a small stream returns the whole file untruncated.
ATTEMPT_LOG_TAIL_SCRIPT = """\
import json
import os
import re
import sys

BASE = "/data/logs"
ISSUE_RE = re.compile(r"[0-9]+\\Z")
STARTED_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}-[0-9]{2}-[0-9]{2}\\.[0-9]+Z\\Z")
MAX_ATTEMPT_BYTES = 1000000
MAX_TAIL_BYTES = 524288


def read_capped(path, limit):
    with open(path, "rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError("file too large")
    return data


def unknown(reason):
    print(json.dumps({"layout": "unknown", "reason": reason}))
    raise SystemExit(0)


def parse_line(line):
    if not line.strip():
        return None
    try:
        entry = json.loads(line)
    except ValueError:
        return {
            "timestamp": None,
            "level": "WARNING",
            "event": "unparsable_line",
            "detail": line[:200],
        }
    if not isinstance(entry, dict):
        return {
            "timestamp": None,
            "level": "WARNING",
            "event": "unparsable_line",
            "detail": line[:200],
        }
    return {
        "timestamp": entry.get("timestamp"),
        "level": entry.get("level"),
        "event": entry.get("event"),
        "detail": entry.get("detail"),
    }


args = sys.argv[1:]
if len(args) != 2:
    unknown("expected issue and started arguments")
issue, started = args
if not ISSUE_RE.match(issue) or not STARTED_RE.match(started):
    unknown("invalid attempt reference")
if not os.path.isdir(BASE):
    unknown("missing /data/logs")
attempt_dir = os.path.join(BASE, issue, started)
attempt = None
unreadable = 0
try:
    record = json.loads(
        read_capped(os.path.join(attempt_dir, "attempt.json"), MAX_ATTEMPT_BYTES).decode(
            "utf-8"
        )
    )
except OSError:
    pass
except ValueError:
    unreadable += 1
else:
    if isinstance(record, dict):
        attempt = record
    else:
        unreadable += 1
events = []
total_events = 0
truncated = False
file_size = 0
events_path = os.path.join(attempt_dir, "agent_output.json")
try:
    file_size = os.path.getsize(events_path)
except OSError:
    pass
else:
    try:
        with open(events_path, "rb") as handle:
            count = 0
            for raw_line in handle:
                if raw_line.strip():
                    count += 1
        total_events = count
    except OSError:
        unreadable += 1
        total_events = 0
    else:
        if total_events == 0:
            truncated = False
        elif file_size <= MAX_TAIL_BYTES:
            try:
                with open(events_path, "rb") as handle:
                    raw = handle.read().decode("utf-8", errors="replace")
            except OSError:
                unreadable += 1
                raw = None
            if raw is not None:
                for line in raw.splitlines():
                    parsed = parse_line(line)
                    if parsed is not None:
                        events.append(parsed)
            truncated = False
        else:
            truncated = True
            try:
                with open(events_path, "rb") as handle:
                    handle.seek(file_size - MAX_TAIL_BYTES)
                    tail = handle.read()
                with open(events_path, "rb") as handle:
                    handle.seek(file_size - MAX_TAIL_BYTES - 1)
                    prev = handle.read(1)
            except OSError:
                unreadable += 1
                tail = None
                prev = b"\\n"
            if tail is not None:
                lines = tail.decode("utf-8", errors="replace").splitlines()
                if prev != b"\\n" and lines:
                    lines = lines[1:]
                for line in lines:
                    parsed = parse_line(line)
                    if parsed is not None:
                        events.append(parsed)
print(
    json.dumps(
        {
            "layout": "attempt-log-tail/v1",
            "issue": int(issue),
            "started": started,
            "attempt": attempt,
            "events": events,
            "total_events": total_events,
            "unreadable": unreadable,
            "truncated": truncated,
            "file_size": file_size,
        }
    )
)
"""


#: Collector run inside the managed instance by ``argv_attempt_events``.
#: A fixed script (never interpolated with operator input): the attempt
#: reference travels as two argv arguments that the script re-validates
#: with the verified name shapes before joining any path, so the daemon's
#: ``..`` resolution cannot escape ``/data/logs``. It reads the attempt's
#: ``attempt.json`` summary and keeps only the ``agent_output.json`` events
#: the progress chart needs (periodic limit readings plus threshold and
#: handoff events); the full stream never crosses the exec boundary.
ATTEMPT_EVENTS_SCRIPT = """\
import json
import os
import re
import sys

BASE = "/data/logs"
ISSUE_RE = re.compile(r"[0-9]+\\Z")
STARTED_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}-[0-9]{2}-[0-9]{2}\\.[0-9]+Z\\Z")
MAX_ATTEMPT_BYTES = 1000000
MAX_EVENTS_BYTES = 2000000
KEEP_EVENTS = frozenset([
    "limits_checked",
    "token_soft_threshold_crossed",
    "token_soft_threshold_subagents_stopped",
    "token_soft_threshold_handoff_note_committed",
])


def read_capped(path, limit):
    with open(path, "rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError("file too large")
    return data


def unknown(reason):
    print(json.dumps({"layout": "unknown", "reason": reason}))
    raise SystemExit(0)


args = sys.argv[1:]
if len(args) != 2:
    unknown("expected issue and started arguments")
issue, started = args
if not ISSUE_RE.match(issue) or not STARTED_RE.match(started):
    unknown("invalid attempt reference")
if not os.path.isdir(BASE):
    unknown("missing /data/logs")
attempt_dir = os.path.join(BASE, issue, started)
attempt = None
unreadable = 0
try:
    record = json.loads(
        read_capped(os.path.join(attempt_dir, "attempt.json"), MAX_ATTEMPT_BYTES).decode(
            "utf-8"
        )
    )
except (OSError, ValueError):
    unreadable += 1
else:
    if isinstance(record, dict):
        attempt = record
    else:
        unreadable += 1
events = []
total_events = 0
truncated = False
try:
    raw = read_capped(
        os.path.join(attempt_dir, "agent_output.json"), MAX_EVENTS_BYTES
    ).decode("utf-8")
except ValueError:
    truncated = True
except OSError:
    unreadable += 1
else:
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
        events.append(
            {
                "timestamp": entry.get("timestamp"),
                "level": entry.get("level"),
                "event": name,
                "detail": entry.get("detail"),
            }
        )
print(
    json.dumps(
        {
            "layout": "attempt-events/v1",
            "issue": int(issue),
            "started": started,
            "attempt": attempt,
            "events": events,
            "total_events": total_events,
            "unreadable": unreadable,
            "truncated": truncated,
        }
    )
)
"""

#: One attempt reference: digits-only issue plus the UTC start with ``:``
#: (as in ``attempt.json``) or ``-`` (as in the log directory name) and
#: mandatory fractional seconds.
ATTEMPT_ISSUE_RE = re.compile(r"[0-9]+\Z")
ATTEMPT_STARTED_DASH_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}-[0-9]{2}-[0-9]{2}\.[0-9]+Z\Z"
)
ATTEMPT_STARTED_COLON_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]+Z\Z"
)


class GatewayError(Exception):
    """A Docker failure with a human-readable reason."""


class DockerNotAvailableError(GatewayError):
    pass


class DiscoveryTimeoutError(GatewayError):
    pass


class TelemetryTimeoutError(GatewayError):
    pass


class AttemptLogTimeoutError(GatewayError):
    pass


@dataclass(frozen=True)
class TelemetryExec:
    """One finished ``docker exec``: raw stdout plus the exit code."""

    stdout: bytes
    returncode: int


@dataclass(frozen=True)
class ManagedInstance:
    """One managed instance (agent container) seen by discovery."""

    name: str
    container_id: str
    raw_state: str

    @property
    def state(self) -> str:
        """Container state as shown in the fleet table: running / not running."""
        return "running" if self.raw_state.strip().lower() == "running" else "not running"


def argv_list_instances() -> list[str]:
    """Argv for discovery: list agent containers, running or exited."""
    return [
        "docker",
        "ps",
        "-a",
        "--filter",
        f"label={AGENT_ROLE_LABEL}",
        "--format",
        PS_FORMAT,
    ]


def argv_telemetry(container_id: str) -> list[str]:
    """Argv for one telemetry exec, addressed by container ID, never by name."""
    return [
        "docker",
        "exec",
        "--user",
        AGENT_USER,
        container_id,
        AGENTCTL_PATH,
        "telemetry",
        "--json",
    ]


def validate_container_id(container_id: str) -> str:
    """Return the container ID when it is a hex Docker ID, else raise.

    The gateway never addresses containers by name, so the argv carries only
    this validated value plus fixed constants: no shell, no raw paths.
    """
    if not isinstance(container_id, str) or not CONTAINER_ID_RE.fullmatch(container_id):
        raise ValueError(f"invalid container id: {container_id!r}")
    return container_id


def argv_attempt_history(container_id: str) -> list[str]:
    """Argv for one Attempt log history exec: every attempt summary at once.

    A single ``docker exec`` runs the fixed :data:`HISTORY_SCRIPT` with the
    instance's Python; the only variable part is the validated container ID.
    """
    validate_container_id(container_id)
    return [
        "docker",
        "exec",
        "--user",
        AGENT_USER,
        container_id,
        "python",
        "-c",
        HISTORY_SCRIPT,
    ]


def validate_attempt_ref(issue: int | str, started: str) -> tuple[int, str]:
    """Validate one attempt reference; return ``(issue, dash-form started)``.

    ``issue`` is digits (or a non-negative int); ``started`` is the UTC
    start either as in ``attempt.json`` (``:``) or as in the log directory
    name (``-``). Anything else raises :class:`ValueError` before an argv
    is built, so no raw path ever reaches Docker.
    """
    if isinstance(issue, bool):
        raise ValueError(f"invalid issue: {issue!r}")
    if isinstance(issue, int):
        if issue < 0:
            raise ValueError(f"invalid issue: {issue!r}")
        number = issue
    elif isinstance(issue, str) and ATTEMPT_ISSUE_RE.fullmatch(issue):
        number = int(issue)
    else:
        raise ValueError(f"invalid issue: {issue!r}")
    if not isinstance(started, str):
        raise ValueError(f"invalid attempt start: {started!r}")
    if ATTEMPT_STARTED_COLON_RE.fullmatch(started):
        date, clock = started.split("T", 1)
        dash = f"{date}T{clock.replace(':', '-')}"
    elif ATTEMPT_STARTED_DASH_RE.fullmatch(started):
        dash = started
    else:
        raise ValueError(f"invalid attempt start: {started!r}")
    return number, dash


def argv_attempt_events(container_id: str, issue: int | str, started: str) -> list[str]:
    """Argv for one Attempt log events exec: a single attempt's kept events.

    A single ``docker exec`` runs the fixed :data:`ATTEMPT_EVENTS_SCRIPT`
    with the instance's Python; besides the validated container ID the only
    variable parts are the validated attempt reference (issue digits plus
    the dash-form start), passed as plain argv arguments, never a path.
    """
    validate_container_id(container_id)
    number, dash_started = validate_attempt_ref(issue, started)
    return [
        "docker",
        "exec",
        "--user",
        AGENT_USER,
        container_id,
        "python",
        "-c",
        ATTEMPT_EVENTS_SCRIPT,
        str(number),
        dash_started,
    ]


def parse_ps_output(output: str) -> list[ManagedInstance]:
    """Parse ``docker ps --format`` rows, dropping compose one-off containers."""
    instances: list[ManagedInstance] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        container_id, name, raw_state, oneoff = (p.strip() for p in parts)
        if oneoff == "True":
            continue
        name = name.removeprefix("/")
        if not name:
            continue
        instances.append(
            ManagedInstance(name=name, container_id=container_id, raw_state=raw_state)
        )
    return instances


def argv_attempt_log(container_id: str, issue: int | str, started: str) -> list[str]:
    """Argv for one Attempt log read: a single attempt's full event stream.

    A single ``docker exec`` runs the fixed :data:`ATTEMPT_LOG_SCRIPT`
    with the instance's Python; besides the validated container ID the only
    variable parts are the validated attempt reference (issue digits plus
    the dash-form start), passed as plain argv arguments, never a path.
    """
    validate_container_id(container_id)
    number, dash_started = validate_attempt_ref(issue, started)
    return [
        "docker",
        "exec",
        "--user",
        AGENT_USER,
        container_id,
        "python",
        "-c",
        ATTEMPT_LOG_SCRIPT,
        str(number),
        dash_started,
    ]


def argv_attempt_tail_log(
    container_id: str, issue: int | str, started: str
) -> list[str]:
    """Argv for one Attempt log tail read: a single attempt's newest events.

    A single ``docker exec`` runs the fixed :data:`ATTEMPT_LOG_TAIL_SCRIPT`
    with the instance's Python; besides the validated container ID the only
    variable parts are the validated attempt reference (issue digits plus
    the dash-form start), passed as plain argv arguments, never a path.
    """
    validate_container_id(container_id)
    number, dash_started = validate_attempt_ref(issue, started)
    return [
        "docker",
        "exec",
        "--user",
        AGENT_USER,
        container_id,
        "python",
        "-c",
        ATTEMPT_LOG_TAIL_SCRIPT,
        str(number),
        dash_started,
    ]


def argv_list_run_files(
    container_id: str, issue: int | str, started: str
) -> list[str]:
    """Argv for one Attempt log run listing: the file names in a single run.

    A single ``docker exec`` runs ``ls -1`` on the run directory; besides the
    validated container ID the only variable parts are the validated attempt
    reference (issue digits plus the dash-form start), joined into the path
    inside the gateway, never from the request. No shell.
    """
    validate_container_id(container_id)
    number, dash_started = validate_attempt_ref(issue, started)
    return [
        "docker",
        "exec",
        "--user",
        AGENT_USER,
        container_id,
        "ls",
        "-1",
        f"/data/logs/{number}/{dash_started}",
    ]


#: Evidence files an Attempt log event points at: numbered tool call, tool
#: result and model response files. ``agent_output.json`` and
#: ``attempt.json`` are readable too (the stream download and the header),
#: as are the setup/baseline/check command logs.
EVIDENCE_FILE_RE = re.compile(
    r"\d{4,}_(tool_call\.json|tool_result\.json|model_response\.txt)\Z"
)
COMMAND_LOG_RE = re.compile(r"(setup|baseline_check|check)_(stdout|stderr)\.log\Z")

#: Single files readable by name, besides the numbered Evidence files.
SINGLE_LOG_FILES = frozenset({"agent_output.json", "attempt.json"})

#: Cap on an Evidence file or command log preview: the fragment shows the
#: first bytes with a truncation notice past this size.
EVIDENCE_PREVIEW_CAP = 256 * 1024

#: One tar header block; the member header gives the file's total size.
_TAR_BLOCK = 512

#: A tar end-of-archive marker: two zero blocks, one suffices to reject.
_ZERO_BLOCK = b"\0" * _TAR_BLOCK


def validate_log_file(filename: str) -> str:
    """Return the file name when it is on the read allow-list, else raise.

    The check runs in the route layer before the gateway is called and again
    here (defence in depth: the gateway must not trust its caller, because
    ``docker cp`` resolves ``..`` and there is no other barrier). A name that
    fails gives 404 in the route or :class:`ValueError` here; it never
    reaches argv. ``transcripts/`` and everything under ``/data/state`` stay
    unreachable, deliberately. Uses :func:`re.fullmatch`, never ``match``
    with ``$``.
    """
    if (
        isinstance(filename, str)
        and filename
        and (
            EVIDENCE_FILE_RE.fullmatch(filename) is not None
            or filename in SINGLE_LOG_FILES
            or COMMAND_LOG_RE.fullmatch(filename) is not None
        )
    ):
        return filename
    raise ValueError(f"invalid log file: {filename!r}")


def argv_read_file(
    container_id: str, issue: int | str, started: str, filename: str
) -> list[str]:
    """Argv for one Evidence file read: ``docker cp`` streams a tar archive.

    The validated attempt reference and file name are joined into the source
    path inside the gateway; no raw path ever comes from the request. Never
    ``-L``, never a directory.
    """
    validate_container_id(container_id)
    number, dash_started = validate_attempt_ref(issue, started)
    validate_log_file(filename)
    return [
        "docker",
        "cp",
        f"{container_id}:/data/logs/{number}/{dash_started}/{filename}",
        "-",
    ]


def argv_copy_logs(container_id: str) -> list[str]:
    """Argv for one stopped-instance Attempt log list: ``docker cp`` of ``/data/logs``.

    The copy streams a tar archive of the whole logs tree; the dashboard
    parses it without executing anything inside the container, so this read
    works on stopped containers where ``docker exec`` fails. Besides the
    validated container ID the argv carries only the fixed source path and
    ``-``: no shell, no raw path, and the tar member names are re-validated
    during parsing, never used in argv.
    """
    validate_container_id(container_id)
    return [
        "docker",
        "cp",
        f"{container_id}:/data/logs",
        "-",
    ]


def argv_copy_run(container_id: str, issue: int | str, started: str) -> list[str]:
    """Argv for one stopped-instance attempt read: ``docker cp`` of a run directory.

    The copy streams a tar archive of the single run directory; the dashboard
    parses ``attempt.json`` and ``agent_output.json`` out of it with the same
    caps and tolerance as the running-instance exec collectors, so this read
    works on stopped containers where ``docker exec`` fails. Besides the
    validated container ID the only variable parts are the validated attempt
    reference (issue digits plus the dash-form start), joined into the source
    path inside the gateway, never from the request. Never ``-L``.
    """
    validate_container_id(container_id)
    number, dash_started = validate_attempt_ref(issue, started)
    return [
        "docker",
        "cp",
        f"{container_id}:/data/logs/{number}/{dash_started}",
        "-",
    ]


@dataclass(frozen=True)
class FileResult:
    """One Evidence file read: ``content`` (up to the cap), its header size,
    and whether the preview was truncated."""

    content: bytes
    total_size: int
    truncated: bool


class _NeedMoreTar(Exception):
    """Internal: fewer than one tar header block read so far."""


def _first_member_offset(data: bytes) -> tuple[int, int, str]:
    """``(size, content offset, typeflag)`` of the tar stream's first member.

    A byte-level header walk (pax extended headers are skipped) that tells
    the streaming read exactly where the content starts, so at most the cap
    is read before the ``docker cp`` process is killed. Raises
    :class:`_NeedMoreTar` when fewer than one header block is available and
    :class:`GatewayError` (``layout not recognised``) when the bytes are not
    a tar stream or hold no file member.
    """
    pos = 0
    while True:
        if len(data) < pos + _TAR_BLOCK:
            raise _NeedMoreTar()
        header = data[pos : pos + _TAR_BLOCK]
        if header == _ZERO_BLOCK:
            raise GatewayError(
                "layout not recognised: evidence archive holds no file"
            )
        try:
            size = int(header[124:136].rstrip(b"\0 ").strip() or b"0", 8)
        except ValueError as exc:
            raise GatewayError(
                "layout not recognised: evidence archive is not a tar stream"
            ) from exc
        if size < 0:
            raise GatewayError(
                "layout not recognised: evidence archive is not a tar stream"
            )
        typeflag = chr(header[156])
        if typeflag in ("x", "X", "g"):
            pos += _TAR_BLOCK + -(-size // _TAR_BLOCK) * _TAR_BLOCK
            continue
        return size, pos + _TAR_BLOCK, typeflag


def _tar_first_member(data: bytes) -> tarfile.TarInfo | None:
    """The tar stream's first member, parsed with ``tarfile`` streaming mode.

    Returns ``None`` when the stream holds no member at all; raises
    :class:`GatewayError` (``layout not recognised``) on anything else that
    is not exactly one leading regular-file member's header.
    """
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r|") as tar:
            return tar.next()
    except GatewayError:
        raise
    except (tarfile.TarError, EOFError, OSError, ValueError) as exc:
        raise GatewayError(
            "layout not recognised: evidence archive is not a tar stream"
        ) from exc


def _extract_single_member(data: bytes) -> bytes:
    """The content of a tar stream holding exactly one regular-file member.

    Anything else (no member, a directory or symlink member, several
    members) is ``layout not recognised``.
    """
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r|") as tar:
            first = tar.next()
            if first is None:
                raise GatewayError(
                    "layout not recognised: evidence archive holds no file"
                )
            if not first.isfile():
                raise GatewayError(
                    "layout not recognised: evidence archive holds no regular file"
                )
            extracted = tar.extractfile(first)
            content = extracted.read() if extracted is not None else b""
            if tar.next() is not None:
                raise GatewayError(
                    "layout not recognised: evidence archive holds several files"
                )
            return content
    except GatewayError:
        raise
    except (tarfile.TarError, EOFError, OSError, ValueError) as exc:
        raise GatewayError(
            "layout not recognised: evidence archive is not a tar stream"
        ) from exc


class DockerGateway:
    """Runs discovery and telemetry execs through the Docker CLI.

    ``timeout`` bounds discovery, telemetry, history and events execs;
    ``log_timeout`` bounds the Attempt log full-stream read, which moves
    more data. At most :data:`LOG_MAX_CONCURRENT` Attempt log reads run at
    once on ``_log_semaphore``, separate from the poll cycle's
    ``max_concurrent_execs``.
    """

    def __init__(
        self, timeout: float = 5.0, log_timeout: float = LOG_EXEC_TIMEOUT
    ) -> None:
        if timeout <= 0:
            raise ValueError(f"timeout must be positive: {timeout}")
        if log_timeout <= 0:
            raise ValueError(f"log timeout must be positive: {log_timeout}")
        self.timeout = timeout
        self.log_timeout = log_timeout
        self._log_semaphore = asyncio.Semaphore(LOG_MAX_CONCURRENT)

    def argv(self) -> list[str]:
        return argv_list_instances()

    async def _run(self, argv: list[str]):
        """Spawn the Docker CLI; raise when it cannot even start."""
        try:
            return await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise DockerNotAvailableError(
                "docker CLI not found: install Docker and ensure it is on PATH"
            ) from exc
        except OSError as exc:
            raise DockerNotAvailableError(f"cannot run docker CLI: {exc}") from exc

    async def list_instances(self) -> list[ManagedInstance]:
        proc = await self._run(self.argv())
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), self.timeout)
        except TimeoutError as exc:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            raise DiscoveryTimeoutError(
                f"docker ps timed out after {self.timeout:g} s"
            ) from exc
        if proc.returncode != 0:
            detail = stderr.decode(errors="replace").strip()
            if "permission denied" in detail.lower():
                raise GatewayError(f"permission denied running docker ps: {detail}")
            reason = detail or f"docker ps exited with status {proc.returncode}"
            raise GatewayError(reason)
        return parse_ps_output(stdout.decode(errors="replace"))

    async def telemetry(self, container_id: str) -> TelemetryExec:
        """Run one telemetry exec against a container ID (never a name).

        The exit code is reported, not raised: the collector classifies each
        exec into a poll outcome. Only a dashboard-side timeout raises
        (:class:`TelemetryTimeoutError`), after killing and reaping the
        client process.
        """
        proc = await self._run(argv_telemetry(container_id))
        try:
            stdout, _stderr = await asyncio.wait_for(proc.communicate(), self.timeout)
        except TimeoutError as exc:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            raise TelemetryTimeoutError(
                f"docker exec timed out after {self.timeout:g} s"
            ) from exc
        return TelemetryExec(stdout=stdout, returncode=proc.returncode)

    async def attempt_events(
        self, container_id: str, issue: int | str, started: str
    ) -> TelemetryExec:
        """Run one Attempt log events exec for a single attempt.

        Like :meth:`telemetry` the exit code is reported, not raised: the
        progress reader maps the stdout into limit readings. Only a
        dashboard-side timeout raises (:class:`TelemetryTimeoutError`),
        after killing and reaping the client process. An invalid container
        ID or attempt reference raises :class:`ValueError` before anything
        is spawned.
        """
        proc = await self._run(argv_attempt_events(container_id, issue, started))
        try:
            stdout, _stderr = await asyncio.wait_for(proc.communicate(), self.timeout)
        except TimeoutError as exc:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            raise TelemetryTimeoutError(
                f"docker exec timed out after {self.timeout:g} s"
            ) from exc
        return TelemetryExec(stdout=stdout, returncode=proc.returncode)

    async def attempt_history(self, container_id: str) -> TelemetryExec:
        """Run one Attempt log history exec against a container ID (never a name).

        Like :meth:`telemetry` the exit code is reported, not raised: the
        history reader maps the stdout into attempt summaries. Only a
        dashboard-side timeout raises (:class:`TelemetryTimeoutError`), after
        killing and reaping the client process. An invalid container ID
        raises :class:`ValueError` before anything is spawned.
        """
        proc = await self._run(argv_attempt_history(container_id))
        try:
            stdout, _stderr = await asyncio.wait_for(proc.communicate(), self.timeout)
        except TimeoutError as exc:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            raise TelemetryTimeoutError(
                f"docker exec timed out after {self.timeout:g} s"
            ) from exc
        return TelemetryExec(stdout=stdout, returncode=proc.returncode)

    async def attempt_log(
        self, container_id: str, issue: int | str, started: str
    ) -> TelemetryExec:
        """Run one Attempt log read for a single attempt's full event stream.

        Like :meth:`telemetry` the exit code is reported, not raised: the
        Attempt log reader maps the stdout into the header and the ordered
        event rows. The read is bounded by ``log_timeout`` (not ``timeout``)
        and at most :data:`LOG_MAX_CONCURRENT` such reads run at once on the
        log semaphore, separate from the poll cycle's ``max_concurrent_execs``.
        An invalid container ID or attempt reference raises
        :class:`ValueError` before anything is spawned.
        """
        argv = argv_attempt_log(container_id, issue, started)
        async with self._log_semaphore:
            proc = await self._run(argv)
            try:
                stdout, _stderr = await asyncio.wait_for(
                    proc.communicate(), self.log_timeout
                )
            except TimeoutError as exc:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()
                raise AttemptLogTimeoutError(
                    f"docker exec timed out after {self.log_timeout:g} s"
                ) from exc
            return TelemetryExec(stdout=stdout, returncode=proc.returncode)

    async def attempt_tail_log(
        self, container_id: str, issue: int | str, started: str
    ) -> TelemetryExec:
        """Run one Attempt log tail read for a single attempt's newest events.

        Like :meth:`attempt_log` the exit code is reported, not raised: the
        tail reader maps the stdout into the header and the newest ordered
        event rows with file line numbers (``seq``). The read is bounded by
        ``log_timeout`` (at most :data:`LOG_TAIL_CAP` parsed bytes plus a
        chunked line count) and shares the log semaphore with the other
        Attempt log reads. An invalid container ID or attempt reference raises
        :class:`ValueError` before anything is spawned.
        """
        argv = argv_attempt_tail_log(container_id, issue, started)
        async with self._log_semaphore:
            proc = await self._run(argv)
            try:
                stdout, _stderr = await asyncio.wait_for(
                    proc.communicate(), self.log_timeout
                )
            except TimeoutError as exc:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()
                raise AttemptLogTimeoutError(
                    f"docker exec timed out after {self.log_timeout:g} s"
                ) from exc
            return TelemetryExec(stdout=stdout, returncode=proc.returncode)

    async def list_run_files(
        self, container_id: str, issue: int | str, started: str
    ) -> list[str]:
        """List the file names in one attempt's run directory with ``ls -1``.

        Like :meth:`telemetry` the exit code is reported, not raised, except
        that a non-zero exit means the run is not there and maps to
        :class:`FileNotFoundError`. The read is bounded by ``log_timeout``
        and shares the log semaphore with the other Attempt log reads. An
        invalid container ID or attempt reference raises :class:`ValueError`
        before anything is spawned.
        """
        argv = argv_list_run_files(container_id, issue, started)
        number, dash_started = validate_attempt_ref(issue, started)
        async with self._log_semaphore:
            proc = await self._run(argv)
            try:
                stdout, _stderr = await asyncio.wait_for(
                    proc.communicate(), self.log_timeout
                )
            except TimeoutError as exc:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()
                raise AttemptLogTimeoutError(
                    f"docker exec timed out after {self.log_timeout:g} s"
                ) from exc
            if proc.returncode != 0:
                raise FileNotFoundError(
                    f"attempt not found: {number}/{dash_started}"
                )
            return [
                line
                for line in stdout.decode(errors="replace").splitlines()
                if line
            ]

    async def _cp_bytes(self, argv: list[str], what: str) -> bytes:
        """Drain one ``docker cp`` directory-tree copy into raw tar bytes.

        Unlike :meth:`read_file` (exactly one regular-file member, capped),
        a tree copy holds many members and is parsed by the caller, so the
        whole stream is returned. The read shares the log semaphore with the
        other Attempt log reads and is bounded by ``log_timeout``. A non-zero
        exit naming a missing path (a gone volume, or logs never written)
        maps to :class:`FileNotFoundError`; any other failure is a
        :class:`GatewayError`.
        """
        async with self._log_semaphore:
            proc = await self._run(argv)
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), self.log_timeout
                )
            except TimeoutError as exc:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()
                raise AttemptLogTimeoutError(
                    f"docker cp timed out after {self.log_timeout:g} s"
                ) from exc
            if proc.returncode != 0:
                detail = stderr.decode(errors="replace").strip()
                lowered = detail.lower()
                if "could not find" in lowered or "no such" in lowered:
                    raise FileNotFoundError(f"{what} not found: {detail}")
                reason = detail or f"docker cp exited with status {proc.returncode}"
                raise GatewayError(reason)
            return stdout

    async def copy_logs(self, container_id: str) -> bytes:
        """Copy ``/data/logs`` out of a managed instance as raw tar bytes.

        The stopped-instance read path for the attempt list: ``docker cp``
        works on stopped containers where ``docker exec`` fails. The argv
        carries only the validated container ID plus the fixed source path
        (see :func:`argv_copy_logs`); member names are re-validated while
        parsing, never used in argv. A missing tree (a gone volume, or an
        instance that never wrote logs) maps to :class:`FileNotFoundError`.
        An invalid container ID raises :class:`ValueError` before anything
        is spawned.
        """
        return await self._cp_bytes(argv_copy_logs(container_id), "attempt logs")

    async def copy_run(
        self, container_id: str, issue: int | str, started: str
    ) -> bytes:
        """Copy one attempt's run directory out as raw tar bytes.

        The stopped-instance read path for a single attempt's header, event
        stream and file listing: ``docker cp`` works on stopped containers
        where ``docker exec`` fails. The argv carries only the validated
        container ID plus the validated attempt reference joined into the
        fixed source path (see :func:`argv_copy_run`). A missing run
        directory maps to :class:`FileNotFoundError`. An invalid container
        ID or attempt reference raises :class:`ValueError` before anything
        is spawned.
        """
        argv = argv_copy_run(container_id, issue, started)
        number, dash_started = validate_attempt_ref(issue, started)
        return await self._cp_bytes(argv, f"attempt {number}/{dash_started}")

    async def read_file(
        self,
        container_id: str,
        issue: int | str,
        started: str,
        filename: str,
        cap: int | None = EVIDENCE_PREVIEW_CAP,
    ) -> FileResult:
        """Read one Evidence file out of a managed instance with ``docker cp``.

        The argv carries the validated container ID plus the validated
        attempt reference and file name joined into the source path (never
        ``-L``, never a directory, no shell); the file name passes the
        allow-list here as well as in the route. ``docker cp`` streams a tar
        archive to stdout, parsed in streaming mode (``r|``): exactly one
        regular-file member is expected, anything else (a directory or
        symlink member, zero or several members, a non-tar stream) is
        ``layout not recognised``. The member header gives ``total_size``;
        at most ``cap`` content bytes are read before the ``docker cp``
        process is killed and reaped (``cap=None`` reads the whole file).
        The exit status of ``docker cp`` itself is honoured: a missing file
        is exit 1 with ``Could not find the file`` on stderr and maps to
        :class:`FileNotFoundError`. The read is bounded by ``log_timeout``
        and shares the log semaphore with the other Attempt log reads.
        """
        argv = argv_read_file(container_id, issue, started, filename)
        if cap is not None and (
            isinstance(cap, bool) or not isinstance(cap, int) or cap < 1
        ):
            raise ValueError(f"invalid cap: {cap!r}")
        async with self._log_semaphore:
            proc = await self._run(argv)
            try:
                return await asyncio.wait_for(
                    self._read_cp_result(proc, filename, cap), self.log_timeout
                )
            except TimeoutError as exc:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()
                raise AttemptLogTimeoutError(
                    f"docker cp timed out after {self.log_timeout:g} s"
                ) from exc

    async def _read_cp_result(
        self, proc, filename: str, cap: int | None
    ) -> FileResult:
        """Drain one ``docker cp`` tar stream into a capped :class:`FileResult`.

        Reads stdout chunk by chunk so a file over the cap stops the daemon
        side early (kill and reap); the tar membership decision itself always
        goes through ``tarfile`` in streaming mode. The byte-level header
        walk (:func:`_first_member_offset`) only tells the loop where the
        content starts, so at most the cap is read before the kill.
        """
        buf = bytearray()
        size: int | None = None
        offset = 0
        regular = False
        saw_non_regular = False
        while True:
            if (
                size is not None
                and regular
                and cap is not None
                and size > cap
                and len(buf) >= offset + cap
            ):
                break
            chunk = await proc.stdout.read(65536)
            if not chunk:
                break
            buf += chunk
            if size is None and not saw_non_regular and len(buf) >= _TAR_BLOCK:
                try:
                    size, offset, typeflag = _first_member_offset(bytes(buf))
                except _NeedMoreTar:
                    pass
                except GatewayError:
                    pass  # Ruled precisely at EOF over the full stream.
                else:
                    regular = typeflag in ("0", "\0")
                    if not regular:
                        saw_non_regular = True
                        break
        data = bytes(buf)
        killed = bool(
            size is not None
            and regular
            and cap is not None
            and size > cap
            and len(data) >= offset + cap
        )
        if killed:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        else:
            # The loop left early only for a non-regular first member; drain
            # the rest so stderr can be read without deadlocking the pipes.
            while await proc.stdout.read(65536):
                pass
        stderr = await proc.stderr.read()
        returncode = await proc.wait()
        if returncode != 0 and b"could not find the file" in stderr.lower():
            raise FileNotFoundError(f"evidence file not found: {filename!r}")
        if killed:
            # The preview stops at the cap, so a trailing second member
            # cannot be checked; the buffered head always holds the full
            # header blocks, which tarfile confirms as one regular file.
            # This is deliberate: the spec's kill-after-cap rule takes
            # precedence over its exactly-one-member rule on the truncated
            # path, because confirming a trailer would mean reading past
            # the cap. Full (unkilled) reads still enforce exactly one.
            first = _tar_first_member(data)
            if first is None or not first.isfile():
                raise GatewayError(
                    "layout not recognised: "
                    "evidence archive holds no regular file"
                )
            content = data[offset : offset + (cap if cap is not None else 0)]
            total = first.size
        elif saw_non_regular:
            raise GatewayError(
                "layout not recognised: "
                "evidence archive holds no regular file"
            )
        else:
            content = _extract_single_member(data)
            total = size if size is not None else len(content)
        return FileResult(
            content=content,
            total_size=total,
            truncated=total > len(content),
        )
