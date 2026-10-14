"""Docker gateway: the only module that builds Docker argv.

All Docker access uses argv lists (no shell). The gateway exposes discovery
(``list_instances``) and one telemetry exec per running container
(``telemetry``). Exit codes are never raised as errors here: the collector
classifies each exec result into a poll outcome.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

AGENT_ROLE_LABEL = "io.github.lbacik.simple-coding-agent.role=agent"
ONEOFF_LABEL = "com.docker.compose.oneoff"

# Tab-separated columns: id, name, state, oneoff label value.
PS_FORMAT = '{{.ID}}\t{{.Names}}\t{{.State}}\t{{.Label "com.docker.compose.oneoff"}}'

AGENT_USER = "agent"
AGENTCTL_PATH = "/usr/local/bin/agentctl"


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
        (:class:`TelemetryTimeoutError`), after killing the client process.
        """
        proc = await self._run(argv_telemetry(container_id))
        try:
            stdout, _stderr = await asyncio.wait_for(proc.communicate(), self.timeout)
        except TimeoutError as exc:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            raise TelemetryTimeoutError(
                f"docker exec timed out after {self.timeout:g} s"
            ) from exc
        return TelemetryExec(stdout=stdout, returncode=proc.returncode)
