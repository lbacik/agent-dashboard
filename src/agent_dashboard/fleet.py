"""In-memory fleet state and the background poll loop.

HTTP handlers only read :class:`FleetState`; the gateway is reached solely
from the background asyncio task, through :func:`collector.run_cycle`.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from .collector import GOOD, InstancePoll, run_cycle
from .drawer import DrawerDetail, read_drawer
from .gateway import ManagedInstance, validate_attempt_ref
from .presentation import (
    attention_rank,
    build_row_headline,
    format_age,
    reach_for_snapshot,
)
from .snapshot import SnapshotDetail, read_snapshot


@dataclass(frozen=True)
class FleetRow:
    """One rendered fleet row: reach plus the snapshot view model.

    Templates consume only these view models; there is no public JSON API.
    ``live_url`` links the running row's Live button at the current
    attempt's log (``None`` while no attempt is active or its reference is
    not a valid log reference); the history link needs no view model since
    the template builds it from the instance name.
    """

    name: str
    state: str  # "running" or "not running"
    container_id: str
    reach: str
    headline: str
    snapshot_age: float | None
    age_text: str | None
    has_snapshot: bool
    rank: int = 6
    detail: SnapshotDetail | None = None
    drawer: DrawerDetail | None = None
    live_url: str | None = None
    live_issue: int | None = None
    # Raw control section for the Control console availability: the value
    # plus whether the snapshot key was absent (missing vs null matters).
    control_raw: Any = None
    control_raw_missing: bool = False
    control_age_text: str | None = None


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
    poll_interval: float = 5.0
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

    async def snapshot(
        self, now: float | None = None, *, poll_interval: float | None = None
    ) -> tuple[list[FleetRow], str | None]:
        at = time.time() if now is None else now
        interval = self.poll_interval if poll_interval is None else poll_interval
        async with self._lock:
            rows = [self._render(entry, at, interval) for entry in self._entries.values()]
            return sorted(rows, key=lambda r: (r.rank, r.name)), self.last_error

    @staticmethod
    def _live_link(
        name: str, drawer: DrawerDetail | None
    ) -> tuple[str | None, int | None]:
        """The current attempt's log link: ``(url, issue)`` or ``(None, None)``.

        The issue prefers the current attempt's number and falls back to the
        control plane's active attempt; the start comes from the control
        plane. A missing attempt on either side, or a reference the log
        routes would reject, means no Live button.
        """
        if drawer is None:
            return None, None
        issue = (
            drawer.issue_number if drawer.issue_number is not None else drawer.active_issue
        )
        started = drawer.active_started
        if issue is None or not started:
            return None, None
        try:
            number, dash = validate_attempt_ref(issue, started)
        except ValueError:
            return None, None
        return (
            f"/instances/{quote(name, safe='')}/attempts/{number}/{quote(dash, safe='')}",
            number,
        )

    @staticmethod
    def _render(entry: _InstanceEntry, now: float, poll_interval: float) -> FleetRow:
        reach = reach_for_snapshot(entry.container_state, entry.outcome_kind, entry.snapshot)
        has_snapshot = entry.snapshot is not None
        detail = read_snapshot(
            entry.snapshot,
            snapshot_at=entry.snapshot_at,
            now=now,
            poll_interval=poll_interval,
        )
        age: float | None = None
        age_text: str | None = None
        if has_snapshot and entry.snapshot_at is not None:
            age = max(0.0, now - entry.snapshot_at)
            age_text = format_age(age)
        headline = build_row_headline(
            reach,
            reason=entry.outcome_reason,
            age_text=age_text,
            has_snapshot=has_snapshot,
            detail=detail,
        )
        rank = attention_rank(reach, detail, has_snapshot=has_snapshot)
        drawer = read_drawer(entry.snapshot, control_stale=detail.control_stale)
        live_url, live_issue = FleetState._live_link(entry.name, drawer)
        control_raw: Any = None
        control_raw_missing = False
        control_age_text: str | None = None
        if isinstance(entry.snapshot, dict):
            if "control" not in entry.snapshot:
                control_raw_missing = True
            else:
                control_raw = entry.snapshot.get("control")
            section = entry.snapshot.get("control")
            if isinstance(section, dict):
                observed = section.get("control_observed_at")
                if isinstance(observed, str) and observed.strip():
                    control_age_text = format_age(
                        max(0.0, now - _parse_control_time(observed, now))
                    )
        return FleetRow(
            name=entry.name,
            state=entry.container_state,
            container_id=entry.container_id,
            reach=reach,
            headline=headline,
            snapshot_age=age,
            age_text=age_text,
            has_snapshot=has_snapshot,
            rank=rank,
            detail=detail,
            drawer=drawer,
            live_url=live_url,
            live_issue=live_issue,
            control_raw=control_raw,
            control_raw_missing=control_raw_missing,
            control_age_text=control_age_text,
        )


def _parse_control_time(raw: str, now: float) -> float:
    """Parse ``control_observed_at``; unparseable reads as now (age zero)."""
    try:
        text = raw.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        moment = datetime.fromisoformat(text)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return moment.timestamp()
    except ValueError:
        return now


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
