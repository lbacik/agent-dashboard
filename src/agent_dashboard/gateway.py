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


class GatewayError(Exception):
    """A Docker failure with a human-readable reason."""


class DockerNotAvailableError(GatewayError):
    pass


class DiscoveryTimeoutError(GatewayError):
    pass


class TelemetryTimeoutError(GatewayError):
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


class DockerGateway:
    """Runs discovery and telemetry execs through the Docker CLI.

    ``timeout`` bounds every Docker call: discovery and each telemetry exec.
    """

    def __init__(self, timeout: float = 5.0) -> None:
        if timeout <= 0:
            raise ValueError(f"timeout must be positive: {timeout}")
        self.timeout = timeout

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
