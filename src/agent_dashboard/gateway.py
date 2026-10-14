"""Docker gateway: the only module that builds Docker argv.

All Docker access uses argv lists (no shell). In this slice the gateway
exposes only discovery (``list_instances``).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

AGENT_ROLE_LABEL = "io.github.lbacik.simple-coding-agent.role=agent"
ONEOFF_LABEL = "com.docker.compose.oneoff"

# Tab-separated columns: id, name, state, oneoff label value.
PS_FORMAT = '{{.ID}}\t{{.Names}}\t{{.State}}\t{{.Label "com.docker.compose.oneoff"}}'


class GatewayError(Exception):
    """A discovery failure with a human-readable reason for the banner."""


class DockerNotAvailableError(GatewayError):
    pass


class DiscoveryTimeoutError(GatewayError):
    pass


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
    """Runs discovery through the Docker CLI."""

    def __init__(self, timeout: float = 5.0) -> None:
        if timeout <= 0:
            raise ValueError(f"timeout must be positive: {timeout}")
        self.timeout = timeout

    def argv(self) -> list[str]:
        return argv_list_instances()

    async def list_instances(self) -> list[ManagedInstance]:
        argv = self.argv()
        try:
            proc = await asyncio.create_subprocess_exec(
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
