"""Docker gateway: the only module that builds Docker argv.

All Docker access uses argv lists (no shell). The gateway exposes discovery
(``list_instances``), one telemetry exec per running container
(``telemetry``), and one Attempt log history exec per running container
(``attempt_history``). Exit codes are never raised as errors here: the
collector classifies each telemetry exec into a poll outcome, and the
history reader maps its exec output into attempt summaries.
"""

from __future__ import annotations

import asyncio
import re
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
#: ``/data/logs/<issue>/<started_at>/attempt.json`` with the verified name
#: shapes, reads the active checkpoint for the in-progress attempt, and
#: prints one JSON document. ``/data/logs`` itself holds the paths, so no
#: raw path ever appears in the argv.
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
        path = os.path.join(issue_dir, started, "attempt.json")
        try:
            record = json.loads(read_capped(path, MAX_ATTEMPT_BYTES).decode("utf-8"))
        except (OSError, ValueError):
            out["unreadable"] += 1
            continue
        if not isinstance(record, dict):
            out["unreadable"] += 1
            continue
        out["attempts"].append({"issue": int(issue), "started": started, "attempt": record})
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

#: Cap on the ``agent_output.json`` bytes the full-log collector reads: the
#: page shows the first events with a truncation notice past this size.
LOG_STREAM_CAP = 4 * 1024 * 1024

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
