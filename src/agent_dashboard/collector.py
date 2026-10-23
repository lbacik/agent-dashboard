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


def _not_supported(now: float) -> tuple[PollOutcome, None]:
    return _outcome(NOT_SUPPORTED, NOT_SUPPORTED_REASON, now), None


# Error codes the agent can report; a JSON error object is handled by its
# code whatever the exit status (the contract gives them exits 1 and 2).
ERROR_CODE_KINDS = frozenset(
    {AGENT_UNREACHABLE, UNSUPPORTED_OPERATION, SNAPSHOT_FAILED}
)


def classify_telemetry(
    stdout: bytes, returncode: int, *, now: float
) -> tuple[PollOutcome, dict[str, Any] | None]:
    """Classify one finished exec into a poll outcome plus snapshot (if good).

    Only exit 0 with a JSON snapshot is good; a nonzero exit with a payload
    that is not a recognised error object means the agent answered but the
    snapshot failed, so the last good snapshot stays visible as stale.
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
        if code in ERROR_CODE_KINDS:
            return _outcome(str(code), str(message or code), now), None
        if message:
            reason = f"{message} (code: {code})"
        else:
            reason = f"unknown error code: {code!r}"
        return _outcome(SNAPSHOT_FAILED, reason, now), None
    if returncode != 0:
        return (
            _outcome(SNAPSHOT_FAILED, f"agentctl exited with status {returncode}", now),
            None,
        )
    return _outcome(GOOD, "", now), payload


async def _poll_one(
    gateway,
    instance: ManagedInstance,
    semaphore: asyncio.Semaphore,
    now: float,
) -> tuple[str, InstancePoll] | None:
    """One exec for one running instance, bounded by the shared semaphore.

    A running instance with a control exec in flight gets no telemetry exec
    this cycle: ``None`` stands for "skipped", so ``apply_cycle`` keeps its
    previous poll outcome and snapshot. The skip is not counted as a failure.
    The check runs right before the exec, after taking the poll semaphore.
    """
    async with semaphore:
        in_flight = getattr(gateway, "control_in_flight", None)
        if in_flight is not None and in_flight(instance.container_id):
            return None
        try:
            exec_result: TelemetryExec = await gateway.telemetry(instance.container_id)
        except TelemetryTimeoutError as exc:
            # The gateway's message carries the budget that actually fired.
            return instance.name, InstancePoll(_outcome(TIMEOUT, str(exc), now), None)
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
    max_concurrent_execs: int = 4,
    now: float | None = None,
) -> None:
    """Run one poll cycle: discovery, then telemetry for running instances.

    A discovery failure keeps the previous rows and records the reason; a
    removed container disappears; exited containers get no exec. The exec
    timeout lives on the gateway; ``now`` stamps every outcome and snapshot
    of the cycle as its receive time (exec-level skew inside one cycle is
    milliseconds against second-granularity ages).
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
        *(_poll_one(gateway, inst, semaphore, at) for inst in running)
    )
    polls = {name: poll for item in results if item is not None for name, poll in [item]}
    await state.apply_cycle(discovered, polls, at)
