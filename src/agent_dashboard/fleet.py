"""In-memory fleet state and the background poll loop.

HTTP handlers only read :class:`FleetState`; the gateway is reached solely
from the background asyncio task, through :func:`collector.run_cycle`.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from .collector import GOOD, InstancePoll, run_cycle
from .gateway import ManagedInstance
from .presentation import build_headline, derive_reach, format_age


@dataclass(frozen=True)
class FleetRow:
    name: str
    state: str  # "running" or "not running"
    container_id: str
    reach: str
    headline: str
    snapshot_age: float | None
    age_text: str | None
    has_snapshot: bool


@dataclass
class _InstanceEntry:
    """One managed instance: container identity plus the last telemetry."""

    name: str
    container_id: str
    container_state: str  # "running" or "not running"
    snapshot: dict[str, Any] | None = None
    snapshot_at: float | None = None
    outcome_kind: str | None = None
    outcome_reason: str = ""
    outcome_at: float | None = None


@dataclass
class FleetState:
    """The dashboard's current view of the fleet (one row per instance)."""

    _entries: dict[str, _InstanceEntry] = field(default_factory=dict)
    last_error: str | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _subscribers: set[asyncio.Queue[int]] = field(default_factory=set, repr=False)
    _version: int = 0

    def subscribe(self) -> asyncio.Queue[int]:
        """Register a stream listener; every cycle puts the new version."""
        queue: asyncio.Queue[int] = asyncio.Queue()
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[int]) -> None:
        """Remove a stream listener (client disconnect / shutdown)."""
        self._subscribers.discard(queue)

    def _publish(self) -> None:
        """Wake every stream listener. Called with the fleet lock held."""
        self._version += 1
        for queue in list(self._subscribers):
            queue.put_nowait(self._version)

    async def set_discovery_error(self, reason: str) -> None:
        """Record a discovery failure; previous rows stay visible."""
        async with self._lock:
            self.last_error = reason
            self._publish()

    async def apply_cycle(
        self,
        discovered: list[ManagedInstance],
        polls: dict[str, InstancePoll],
        now: float,
    ) -> None:
        """Replace rows from discovery plus this cycle's telemetry.

        A recreated container (changed container ID) drops its old snapshot.
        Instances the discovery no longer lists disappear.
        """
        async with self._lock:
            entries: dict[str, _InstanceEntry] = {}
            for inst in discovered:
                previous = self._entries.get(inst.name)
                if previous is not None and previous.container_id == inst.container_id:
                    entry = previous
                else:
                    entry = _InstanceEntry(
                        name=inst.name,
                        container_id=inst.container_id,
                        container_state=inst.state,
                    )
                entry.container_id = inst.container_id
                entry.container_state = inst.state
                poll = polls.get(inst.name)
                if poll is not None:
                    entry.outcome_kind = poll.outcome.kind
                    entry.outcome_reason = poll.outcome.reason
                    entry.outcome_at = poll.outcome.at
                    if poll.outcome.kind == GOOD and poll.snapshot is not None:
                        entry.snapshot = poll.snapshot
                        entry.snapshot_at = poll.outcome.at
                entries[inst.name] = entry
            self._entries = entries
            self.last_error = None
            self._publish()

    async def snapshot(self, now: float | None = None) -> tuple[list[FleetRow], str | None]:
        at = time.time() if now is None else now
        async with self._lock:
            rows = [self._render(entry, at) for entry in self._entries.values()]
            return sorted(rows, key=lambda r: r.name), self.last_error

    @staticmethod
    def _render(entry: _InstanceEntry, now: float) -> FleetRow:
        reach = derive_reach(entry.container_state, entry.outcome_kind)
        age: float | None = None
        age_text: str | None = None
        if entry.snapshot is not None and entry.snapshot_at is not None:
            age = max(0.0, now - entry.snapshot_at)
            age_text = format_age(age)
        headline = build_headline(
            reach,
            reason=entry.outcome_reason,
            age_text=age_text,
            has_snapshot=entry.snapshot is not None,
        )
        return FleetRow(
            name=entry.name,
            state=entry.container_state,
            container_id=entry.container_id,
            reach=reach,
            headline=headline,
            snapshot_age=age,
            age_text=age_text,
            has_snapshot=entry.snapshot is not None,
        )


async def poll_forever(
    state: FleetState,
    gateway,
    poll_interval: float,
    stop: asyncio.Event | None = None,
    *,
    max_concurrent_execs: int = 4,
) -> None:
    """Poll loop: refresh immediately, then repeat without overlapping.

    The next cycle starts one interval after the previous cycle started, or
    when the previous cycle finishes, whichever is later. The exec timeout
    lives on the gateway; the concurrency cap lives here.
    """
    loop = asyncio.get_running_loop()
    while True:
        start = loop.time()
        await run_cycle(state, gateway, max_concurrent_execs=max_concurrent_execs)
        if stop is not None and stop.is_set():
            return
        elapsed = loop.time() - start
        await asyncio.sleep(max(0.0, poll_interval - elapsed))
        if stop is not None and stop.is_set():
            return
