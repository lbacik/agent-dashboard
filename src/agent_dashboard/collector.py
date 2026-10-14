"""Poll cycle: fetch one telemetry snapshot per running instance.

Each cycle discovers the fleet, then runs ``telemetry`` against every
running container (at most one exec in flight per instance, at most
``max_concurrent_execs`` overall). Each exec is classified into a poll
outcome:

- exit 0 with a JSON snapshot -> good snapshot;
- a JSON error object -> its ``code`` (``agent_unreachable``,
  ``unsupported_operation``, ``snapshot_failed``);
- stdout that is not a JSON object -> telemetry not supported by this
  agent version (older images reject the subcommand with exit 2);
- dashboard-side timeout -> unreachable (timeout).
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

from .gateway import (
    GatewayError,
    ManagedInstance,
    TelemetryExec,
    TelemetryTimeoutError,
)

GOOD = "good"
AGENT_UNREACHABLE = "agent_unreachable"
UNSUPPORTED_OPERATION = "unsupported_operation"
SNAPSHOT_FAILED = "snapshot_failed"
NOT_SUPPORTED = "not_supported"
TIMEOUT = "timeout"

POLL_OUTCOME_KINDS = frozenset(
    {
        GOOD,
        AGENT_UNREACHABLE,
        UNSUPPORTED_OPERATION,
        SNAPSHOT_FAILED,
        NOT_SUPPORTED,
        TIMEOUT,
    }
)

NOT_SUPPORTED_REASON = "telemetry not supported by this agent version"


@dataclass(frozen=True)
class PollOutcome:
    """The classification of one telemetry attempt."""

    kind: str
    reason: str
    at: float


@dataclass(frozen=True)
class InstancePoll:
    """Everything one cycle learned about one managed instance."""

    outcome: PollOutcome
    snapshot: dict[str, Any] | None


def _outcome(kind: str, reason: str, now: float) -> PollOutcome:
    return PollOutcome(kind=kind, reason=reason, at=now)


def timeout_outcome(exec_timeout: float, now: float) -> PollOutcome:
    """The dashboard-side timeout outcome: unreachable, with the budget."""
    return _outcome(TIMEOUT, f"docker exec timed out after {exec_timeout:g} s", now)


def _not_supported(now: float) -> tuple[PollOutcome, None]:
    return _outcome(NOT_SUPPORTED, NOT_SUPPORTED_REASON, now), None


def classify_telemetry(
    stdout: bytes, returncode: int, *, now: float
) -> tuple[PollOutcome, dict[str, Any] | None]:
    """Classify one finished exec into a poll outcome plus snapshot (if good).

    ``returncode`` only matters through the payload: a JSON error object is
    handled by its ``code`` whatever the exit status, and any other JSON
    object is the snapshot.
    """
    try:
        payload = json.loads(stdout)
    except ValueError:
        return _not_supported(now)
    if not isinstance(payload, dict):
        return _not_supported(now)
    if payload.get("ok") is False:
        error = payload.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        message = error.get("message") if isinstance(error, dict) else None
        if code == AGENT_UNREACHABLE:
            return _outcome(AGENT_UNREACHABLE, str(message or code), now), None
        if code == UNSUPPORTED_OPERATION:
            return _outcome(UNSUPPORTED_OPERATION, str(message or code), now), None
        if code == SNAPSHOT_FAILED:
            return _outcome(SNAPSHOT_FAILED, str(message or code), now), None
        if message:
            reason = f"{message} (code: {code})"
        else:
            reason = f"unknown error code: {code!r}"
        return _outcome(SNAPSHOT_FAILED, reason, now), None
    _ = returncode
    return _outcome(GOOD, "", now), payload


async def _poll_one(
    gateway,
    instance: ManagedInstance,
    semaphore: asyncio.Semaphore,
    exec_timeout: float,
    now: float,
) -> tuple[str, InstancePoll]:
    """One exec for one running instance, bounded by the shared semaphore."""
    async with semaphore:
        try:
            exec_result: TelemetryExec = await gateway.telemetry(instance.container_id)
        except TelemetryTimeoutError:
            return instance.name, InstancePoll(timeout_outcome(exec_timeout, now), None)
        except GatewayError as exc:
            return instance.name, InstancePoll(_outcome(AGENT_UNREACHABLE, str(exc), now), None)
        outcome, snapshot = classify_telemetry(
            exec_result.stdout, exec_result.returncode, now=now
        )
        return instance.name, InstancePoll(outcome, snapshot)


async def run_cycle(
    state,
    gateway,
    *,
    exec_timeout: float = 5.0,
    max_concurrent_execs: int = 4,
    now: float | None = None,
) -> None:
    """Run one poll cycle: discovery, then telemetry for running instances.

    A discovery failure keeps the previous rows and records the reason; a
    removed container disappears; exited containers get no exec.
    """
    if max_concurrent_execs < 1:
        raise ValueError(f"max concurrent execs must be >= 1: {max_concurrent_execs}")
    at = time.time() if now is None else now
    try:
        discovered: list[ManagedInstance] = await gateway.list_instances()
    except GatewayError as exc:
        await state.set_discovery_error(str(exc))
        return
    semaphore = asyncio.Semaphore(max_concurrent_execs)
    running = [inst for inst in discovered if inst.state == "running"]
    results = await asyncio.gather(
        *(_poll_one(gateway, inst, semaphore, exec_timeout, at) for inst in running)
    )
    await state.apply_cycle(discovered, dict(results), at)
