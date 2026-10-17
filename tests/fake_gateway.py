"""Shared fake Docker gateway for tests.

Discovery returns scripted rows (or raises a scripted error). Telemetry
returns recorded stdout/exit pairs per container ID, mirroring the real
``DockerGateway.telemetry`` return type. The default script is an old agent
whose ``agentctl`` rejects the ``telemetry`` subcommand (exit 2, empty
stdout), which classifies as "telemetry not supported by this agent
version".
"""

from __future__ import annotations

import asyncio
import json

from agent_dashboard.gateway import (
    ManagedInstance,
    TelemetryExec,
    TelemetryTimeoutError,
)

DEFAULT_SCRIPT = TelemetryExec(stdout=b"", returncode=2)


class FakeGateway:
    def __init__(self, rows=None, error=None):
        self._rows = list(rows or [])
        self._error = error
        self._scripts: dict[str, TelemetryExec | Exception] = {}
        self._histories: dict[str, TelemetryExec | Exception] = {}
        self.delay = 0.0
        self.history_delay = 0.0
        self.history_delays: dict[str, float] = {}
        self.list_calls = 0
        self.telemetry_calls: list[str] = []
        self.history_calls: list[str] = []
        self.in_flight = 0
        self.peak_in_flight = 0
        self.per_id_in_flight: dict[str, int] = {}
        self.peak_per_id: dict[str, int] = {}

    def set_rows(self, rows: list[ManagedInstance]) -> None:
        self._rows = list(rows)

    def set_script(self, container_id: str, stdout: bytes, returncode: int) -> None:
        self._scripts[container_id] = TelemetryExec(stdout=stdout, returncode=returncode)

    def set_snapshot(self, container_id: str, snapshot: dict) -> None:
        self.set_script(container_id, json.dumps(snapshot).encode(), 0)

    def set_error_code(self, container_id: str, code: str, message: str) -> None:
        self.set_script(
            container_id,
            json.dumps({"ok": False, "error": {"code": code, "message": message}}).encode(),
            1,
        )

    def set_timeout(
        self, container_id: str, message: str = "docker exec timed out after 5 s"
    ) -> None:
        self._scripts[container_id] = TelemetryTimeoutError(message)

    def set_history(
        self, container_id: str, payload: dict, returncode: int = 0
    ) -> None:
        self._histories[container_id] = TelemetryExec(
            stdout=json.dumps(payload).encode(), returncode=returncode
        )

    def set_history_error(self, container_id: str, error: Exception) -> None:
        self._histories[container_id] = error

    async def list_instances(self) -> list[ManagedInstance]:
        self.list_calls += 1
        if self._error is not None:
            raise self._error
        return list(self._rows)

    async def telemetry(self, container_id: str) -> TelemetryExec:
        self.telemetry_calls.append(container_id)
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        current = self.per_id_in_flight.get(container_id, 0) + 1
        self.per_id_in_flight[container_id] = current
        self.peak_per_id[container_id] = max(self.peak_per_id.get(container_id, 0), current)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            scripted = self._scripts.get(container_id, DEFAULT_SCRIPT)
            if isinstance(scripted, Exception):
                raise scripted
            return scripted
        finally:
            self.in_flight -= 1
            self.per_id_in_flight[container_id] -= 1

    async def attempt_history(self, container_id: str) -> TelemetryExec:
        """One scripted history exec; unscripted IDs report an empty log."""
        self.history_calls.append(container_id)
        delay = self.history_delays.get(container_id, self.history_delay)
        if delay:
            await asyncio.sleep(delay)
        scripted = self._histories.get(
            container_id,
            TelemetryExec(
                stdout=json.dumps(
                    {
                        "layout": "attempt-logs/v1",
                        "attempts": [],
                        "active_started_at": None,
                        "unreadable": 0,
                    }
                ).encode(),
                returncode=0,
            ),
        )
        if isinstance(scripted, Exception):
            raise scripted
        return scripted
