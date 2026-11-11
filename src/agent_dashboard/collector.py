"""Poll cycle: fetch one telemetry snapshot per running instance.

Each cycle discovers the fleet, then runs one exec against every running
container (at most one exec in flight per instance, at most
``max_concurrent_execs`` overall): legacy instances keep ``telemetry``
v1, while contract-path instances poll with ``agentctl --contract 1
status`` as their labelled exec user (ADR 0014). Stopped contract-path
instances are read offline instead (``docker exec`` fails on stopped
containers): one Offline Read ``status`` per stopped instance per stop,
through the same shared semaphore. Misconfigured and duplicated rows are
never polled. Each exec is classified into a poll outcome:

- exit 0 with a JSON snapshot -> good snapshot;
- a JSON error object -> its ``code`` (``agent_unreachable``,
  ``unsupported_operation``, ``snapshot_failed``);
- stdout that is not a JSON object -> telemetry not supported by this
  agent version (older images reject the subcommand with exit 2);
- dashboard-side timeout -> unreachable (timeout).

Contract ``status`` replies map by reply class: success to a good
snapshot; ``agent_unreachable``, Docker exits 125-127 and timeouts to
agent unreachable; ``internal`` and malformed stdout to snapshot failed;
``contract_mismatch`` to the contract-mismatch outcome (whose reason
names ``details.supported``); ``unsupported`` to unsupported operation.

An Offline Read whose helper is refused (``rejected`` with
``details.reason`` ``instance_running``: the container started between
inspection and the run) maps to the instance-started outcome: the row
renders "instance started, refresh" and waits for the next discovery.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

from .contract_reply import (
    AMBIGUOUS,
    classify_contract_reply,
)
from .contract_reply import (
    CONTRACT_MISMATCH as REPLY_CONTRACT_MISMATCH,
)
from .contract_reply import (
    SUCCESS as REPLY_SUCCESS,
)
from .contract_reply import (
    UNSUPPORTED as REPLY_UNSUPPORTED,
)
from .contract_status import contract_mismatch_headline
from .gateway import (
    CONTRACT_PATH,
    ControlTimeoutError,
    DockerNotAvailableError,
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
CONTRACT_MISMATCH = "contract_mismatch"
INSTANCE_STARTED = "instance_started"

#: Headline for the instance-started outcome: the stopped container started
#: between inspection and its Offline Read, so the row waits for the next
#: discovery (when the running poll takes over).
INSTANCE_STARTED_REASON = "instance started, refresh"

#: Headline prefix when the Offline Read cannot run: the image digest, a
#: volume name or a mount destination failed validation before any helper
#: was started, and the suffix names the offending value.
OFFLINE_UNAVAILABLE_PREFIX = "offline read unavailable: "

POLL_OUTCOME_KINDS = frozenset(
    {
        GOOD,
        AGENT_UNREACHABLE,
        UNSUPPORTED_OPERATION,
        SNAPSHOT_FAILED,
        NOT_SUPPORTED,
        TIMEOUT,
        CONTRACT_MISMATCH,
        INSTANCE_STARTED,
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
    """Everything one cycle learned about one managed instance.

    ``offline_key`` is the finish time of the stop an Offline Read belongs
    to (``None`` for every other poll): the fleet records it so the loop
    runs one Offline Read ``status`` per stopped instance per stop.
    """

    outcome: PollOutcome
    snapshot: dict[str, Any] | None
    offline_key: str | None = None


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


def classify_contract_status(
    stdout: bytes, returncode: int | None, *, now: float
) -> tuple[PollOutcome, dict[str, Any] | None]:
    """Classify one finished contract ``status`` exec.

    A coherent ``ok:true`` envelope is a good snapshot (its ``result``); a
    ``contract_mismatch`` refusal is the contract-mismatch outcome (its
    reason names ``details.supported``); an ``unsupported`` refusal is the
    unsupported-operation outcome. Of the ambiguous answers, an
    ``agent_unreachable`` envelope, a Docker transport failure (exits
    125-127) and a dashboard-side timeout mean the agent is unreachable,
    while ``internal`` and malformed stdout mean the snapshot failed.
    Anything else definitive-but-unexpected is a snapshot failure too: an
    unknown reply class never breaks the row.
    """
    cls = classify_contract_reply(returncode, stdout)
    if cls == REPLY_SUCCESS:
        try:
            result = json.loads(stdout)
        except ValueError:
            return _outcome(SNAPSHOT_FAILED, "malformed stdout", now), None
        if not isinstance(result, dict):
            return _outcome(SNAPSHOT_FAILED, "malformed stdout", now), None
        snapshot = result.get("result")
        if not isinstance(snapshot, dict):
            return _outcome(SNAPSHOT_FAILED, "malformed stdout", now), None
        return _outcome(GOOD, "", now), snapshot
    if cls == REPLY_CONTRACT_MISMATCH:
        supported: Any = None
        try:
            envelope = json.loads(stdout)
            error = envelope.get("error", {})
            details = error.get("details", {}) if isinstance(error, dict) else {}
            supported = details.get("supported") if isinstance(details, dict) else None
        except ValueError:
            supported = None
        # The reason is the headline: it names details.supported.
        return _outcome(CONTRACT_MISMATCH, contract_mismatch_headline(supported), now), None
    if cls == REPLY_UNSUPPORTED:
        return _outcome(UNSUPPORTED_OPERATION, _contract_message(stdout), now), None
    if cls == AMBIGUOUS:
        code, message = _contract_error(stdout)
        if code == "agent_unreachable":
            return _outcome(AGENT_UNREACHABLE, message or code, now), None
        if isinstance(returncode, int) and 125 <= returncode <= 127:
            return (
                _outcome(
                    AGENT_UNREACHABLE,
                    f"docker exec exited with status {returncode}",
                    now,
                ),
                None,
            )
        if code == "internal":
            return _outcome(SNAPSHOT_FAILED, message or code, now), None
        return _outcome(SNAPSHOT_FAILED, message or code or "malformed stdout", now), None
    return (
        _outcome(SNAPSHOT_FAILED, f"unexpected contract reply: {cls}", now),
        None,
    )


def _contract_error(stdout: bytes) -> tuple[str | None, str]:
    """The ``(code, message)`` of a contract error envelope, or unknowns."""
    try:
        envelope = json.loads(stdout)
    except ValueError:
        return None, ""
    if not isinstance(envelope, dict):
        return None, ""
    error = envelope.get("error")
    if not isinstance(error, dict):
        return None, ""
    code = error.get("code")
    message = error.get("message")
    return (
        code if isinstance(code, str) else None,
        message if isinstance(message, str) else "",
    )


def _contract_message(stdout: bytes) -> str:
    """The message of a contract error envelope, or its code, or a fallback."""
    code, message = _contract_error(stdout)
    return message or code or "unsupported operation"


def classify_offline_status(
    stdout: bytes, returncode: int | None, *, now: float
) -> tuple[PollOutcome, dict[str, Any] | None]:
    """Classify one finished Offline Read ``status``.

    A ``rejected`` helper (the container started between inspection and the
    run, so the live engine refused the read) is the instance-started
    outcome; everything else classifies exactly like an online ``status``.
    """
    try:
        envelope = json.loads(stdout)
    except ValueError:
        envelope = None
    if isinstance(envelope, dict):
        error = envelope.get("error")
        if isinstance(error, dict) and error.get("code") == "rejected":
            return _outcome(INSTANCE_STARTED, INSTANCE_STARTED_REASON, now), None
    return classify_contract_status(stdout, returncode, now=now)


async def _poll_one(
    gateway,
    instance: ManagedInstance,
    semaphore: asyncio.Semaphore,
    now: float,
) -> tuple[str, InstancePoll] | None:
    """One exec for one running instance, bounded by the shared semaphore.

    A running legacy instance with a control exec in flight gets no exec
    this cycle; a misconfigured or duplicated row never gets one either:
    ``None`` stands for "skipped", so ``apply_cycle`` keeps its previous
    poll outcome and snapshot. The skip is not counted as a failure. The
    control-slot check runs right before the exec, after taking the poll
    semaphore. Contract-path instances keep being polled while a Control
    action is in flight (their ``status`` poll never takes the control
    slot); legacy ones keep telemetry v1.
    """
    if instance.note is not None:
        return None
    async with semaphore:
        if instance.path == CONTRACT_PATH:
            return await _poll_contract_one(gateway, instance, now)
        in_flight = getattr(gateway, "control_in_flight", None)
        if in_flight is not None and in_flight(instance.container_id):
            return None
        try:
            exec_result: TelemetryExec = await gateway.telemetry(instance.container_id)
        except TelemetryTimeoutError as exc:
            # The gateway's message carries the budget that actually fired.
            return instance.container_id, InstancePoll(
                _outcome(TIMEOUT, str(exc), now), None
            )
        except GatewayError as exc:
            return instance.container_id, InstancePoll(
                _outcome(AGENT_UNREACHABLE, str(exc), now), None
            )
        outcome, snapshot = classify_telemetry(
            exec_result.stdout, exec_result.returncode, now=now
        )
        return instance.container_id, InstancePoll(outcome, snapshot)


async def _poll_contract_one(
    gateway, instance: ManagedInstance, now: float
) -> tuple[str, InstancePoll] | None:
    """One contract ``status`` exec; exit codes are classified, not raised."""
    assert instance.exec_user is not None
    try:
        exec_result = await gateway.contract_status(
            instance.container_id, instance.exec_user
        )
    except ControlTimeoutError as exc:
        # The gateway's message carries the budget that actually fired.
        return instance.container_id, InstancePoll(
            _outcome(TIMEOUT, str(exc), now), None
        )
    except (GatewayError, DockerNotAvailableError) as exc:
        return instance.container_id, InstancePoll(
            _outcome(AGENT_UNREACHABLE, str(exc), now), None
        )
    outcome, snapshot = classify_contract_status(
        exec_result.stdout, exec_result.returncode, now=now
    )
    return instance.container_id, InstancePoll(outcome, snapshot)


async def _poll_offline_one(
    gateway,
    state,
    instance: ManagedInstance,
    semaphore: asyncio.Semaphore,
    now: float,
) -> tuple[str, InstancePoll] | None:
    """One Offline Read ``status`` for a stopped contract instance.

    The instance is never started: it is re-inspected (spec section 12) and
    its image runs as a throwaway helper with no network and read-only
    volumes. The read runs at most once per stop, keyed by container ID
    plus the inspect finish time, through the poll cycle's shared semaphore
    (counted against the global exec limit like every other exec). A stale
    key (the container restarted with a new finish time) reads again; a
    running container, a misconfigured or duplicated row, an unparsable
    inspect, and any transient failure (including the helper timeout, whose
    cleanup already ran) skip the cycle and are retried next time. An
    invalid image, volume or destination never starts a helper: it is the
    unavailable outcome with its reason. A refused helper (the container
    started mid-read) is the instance-started outcome with no key, so the
    row waits for the next discovery instead of caching the refusal.
    """
    if (
        instance.note is not None
        or instance.path != CONTRACT_PATH
        or not instance.exec_user
    ):
        return None
    async with semaphore:
        get_lock = getattr(gateway, "offline_lock", None)
        lock = None
        if callable(get_lock):
            try:
                lock = get_lock(instance.container_id)
            except ValueError:
                return None
        if lock is None:
            return await _poll_offline_guarded(
                gateway, state, instance, now
            )
        async with lock:
            return await _poll_offline_guarded(
                gateway, state, instance, now
            )


async def _poll_offline_guarded(
    gateway,
    state,
    instance: ManagedInstance,
    now: float,
) -> tuple[str, InstancePoll] | None:
    """Inspect plus one Offline Read ``status``; caller holds the lock."""
    try:
        info = await gateway.inspect_offline(instance.container_id)
    except GatewayError:
        return None
    if info.running:
        return instance.container_id, InstancePoll(
            _outcome(INSTANCE_STARTED, INSTANCE_STARTED_REASON, now), None
        )
    if await state.is_offline_done(instance.container_id, info.finished_at):
        return None
    key = info.finished_at
    try:
        exec_result = await gateway.offline_status(
            info.image, info.mounts, instance.exec_user
        )
    except ValueError as exc:
        return instance.container_id, InstancePoll(
            _outcome(SNAPSHOT_FAILED, f"{OFFLINE_UNAVAILABLE_PREFIX}{exc}", now),
            None,
            offline_key=key,
        )
    except GatewayError:
        return None
    outcome, snapshot = classify_offline_status(
        exec_result.stdout, exec_result.returncode, now=now
    )
    if outcome.kind == INSTANCE_STARTED:
        return instance.container_id, InstancePoll(outcome, None)
    return instance.container_id, InstancePoll(outcome, snapshot, offline_key=key)


async def run_cycle(
    state,
    gateway,
    *,
    max_concurrent_execs: int = 4,
    now: float | None = None,
) -> None:
    """Run one poll cycle: discovery, then telemetry for running instances.

    A discovery failure keeps the previous rows and records the reason; a
    removed container disappears; running containers get one exec each,
    while stopped contract-path instances get one Offline Read ``status``
    per stop (keyed by container ID plus finish time) through the same
    shared semaphore, so every exec of the cycle counts against
    ``max_concurrent_execs``. The exec timeout lives on the gateway;
    ``now`` stamps every outcome and snapshot of the cycle as its receive
    time (exec-level skew inside one cycle is milliseconds against
    second-granularity ages).
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
    stopped = [
        inst
        for inst in discovered
        if inst.state != "running"
        and inst.path == CONTRACT_PATH
        and inst.note is None
        and inst.exec_user
    ]
    results = await asyncio.gather(
        *(_poll_one(gateway, inst, semaphore, at) for inst in running),
        *(_poll_offline_one(gateway, state, inst, semaphore, at) for inst in stopped),
    )
    polls = {name: poll for item in results if item is not None for name, poll in [item]}
    await state.apply_cycle(discovered, polls, at)
