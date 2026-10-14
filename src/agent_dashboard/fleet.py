"""In-memory fleet state and the background discovery loop.

HTTP handlers only read :class:`FleetState`; the gateway is reached solely
from the background asyncio task.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from .gateway import GatewayError, ManagedInstance


@dataclass(frozen=True)
class FleetRow:
    name: str
    state: str  # "running" or "not running"


@dataclass
class FleetState:
    """The dashboard's current view of the fleet (one row per instance)."""

    _instances: dict[str, FleetRow] = field(default_factory=dict)
    last_error: str | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def refresh(self, gateway) -> None:
        """Run one discovery cycle: replace rows, or record the failure reason."""
        try:
            found: list[ManagedInstance] = await gateway.list_instances()
        except GatewayError as exc:
            async with self._lock:
                self.last_error = str(exc)
            return
        async with self._lock:
            self._instances = {
                inst.name: FleetRow(name=inst.name, state=inst.state) for inst in found
            }
            self.last_error = None

    async def snapshot(self) -> tuple[list[FleetRow], str | None]:
        async with self._lock:
            rows = sorted(self._instances.values(), key=lambda r: r.name)
            return rows, self.last_error


async def poll_forever(
    state: FleetState,
    gateway,
    poll_interval: float,
    stop: asyncio.Event | None = None,
) -> None:
    """Discovery loop: refresh immediately, then every poll interval."""
    while True:
        await state.refresh(gateway)
        if stop is not None and stop.is_set():
            return
        await asyncio.sleep(poll_interval)
        if stop is not None and stop.is_set():
            return
