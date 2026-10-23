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
import io
import json
import tarfile

from agent_dashboard.gateway import (
    EVIDENCE_PREVIEW_CAP,
    ControlBusyError,
    ControlExec,
    ControlTimeoutError,
    FileResult,
    ManagedInstance,
    TelemetryExec,
    TelemetryTimeoutError,
    argv_command_status,
    argv_control,
)

DEFAULT_SCRIPT = TelemetryExec(stdout=b"", returncode=2)


def make_logs_tree(entries: dict[str, bytes]) -> bytes:
    """Build a ``docker cp /data/logs`` tar: names like ``logs/98/<started>/attempt.json``."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def make_run_tree(entries: dict[str, bytes]) -> bytes:
    """Build a ``docker cp`` run-directory tar: names like ``<started>/attempt.json``."""
    return make_logs_tree(entries)


class FakeGateway:
    def __init__(self, rows=None, error=None):
        self._rows = list(rows or [])
        self._error = error
        self._scripts: dict[str, TelemetryExec | Exception] = {}
        self._histories: dict[str, TelemetryExec | Exception] = {}
        self._events: dict[str, TelemetryExec | Exception] = {}
        self._logs: dict[str, TelemetryExec | Exception] = {}
        self._tails: dict[str, TelemetryExec | Exception] = {}
        self._run_files: dict[tuple[str, str, str], list[str] | Exception] = {}
        self._files: dict[tuple[str, str, str, str], FileResult | Exception] = {}
        self._logs_trees: dict[str, bytes | Exception] = {}
        self._run_trees: dict[tuple[str, str, str], bytes | Exception] = {}
        self.delay = 0.0
        self.history_delay = 0.0
        self.history_delays: dict[str, float] = {}
        self._control_scripts: dict[str, ControlExec | Exception] = {}
        self._status_scripts: dict[str, ControlExec | Exception] = {}
        self._control_active: set[str] = set()
        self._control_held: set[str] = set()
        self.control_delay = 0.0
        self.control_delays: dict[str, float] = {}
        self.list_calls = 0
        self.telemetry_calls: list[str] = []
        self.history_calls: list[str] = []
        self.events_calls: list[tuple[str, str, str]] = []
        self.log_calls: list[tuple[str, str, str]] = []
        self.tail_calls: list[tuple[str, str, str]] = []
        self.run_files_calls: list[tuple[str, str, str]] = []
        self.control_calls: list[dict] = []
        self.command_status_calls: list[tuple[str, str]] = []
        self.control_argvs: list[list[str]] = []
        self.file_calls: list[tuple[str, str, str, str]] = []
        self.logs_tree_calls: list[str] = []
        self.run_tree_calls: list[tuple[str, str, str]] = []
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

    def set_events(
        self, container_id: str, payload: dict, returncode: int = 0
    ) -> None:
        self._events[container_id] = TelemetryExec(
            stdout=json.dumps(payload).encode(), returncode=returncode
        )

    def set_events_error(self, container_id: str, error: Exception) -> None:
        self._events[container_id] = error

    def set_log(
        self, container_id: str, payload: dict, returncode: int = 0
    ) -> None:
        self._logs[container_id] = TelemetryExec(
            stdout=json.dumps(payload).encode(), returncode=returncode
        )

    def set_log_error(self, container_id: str, error: Exception) -> None:
        self._logs[container_id] = error

    def set_tail(
        self, container_id: str, payload: dict, returncode: int = 0
    ) -> None:
        self._tails[container_id] = TelemetryExec(
            stdout=json.dumps(payload).encode(), returncode=returncode
        )

    def set_tail_error(self, container_id: str, error: Exception) -> None:
        self._tails[container_id] = error

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

    def set_control(
        self, container_id: str, stdout: bytes, stderr: bytes = b"", returncode: int = 0
    ) -> None:
        """Script one control exec reply: reported, never raised."""
        self._control_scripts[container_id] = ControlExec(
            stdout=stdout, stderr=stderr, returncode=returncode
        )

    def set_control_error(self, container_id: str, error: Exception) -> None:
        """Script one control exec error (busy, timeout, ...)."""
        self._control_scripts[container_id] = error

    def set_control_busy(self, container_id: str) -> None:
        """Script a busy control slot: the next control call raises at once."""
        self.set_control_error(
            container_id,
            ControlBusyError(
                f"another control exec for container {container_id} is already in flight"
            ),
        )

    def set_control_timeout(
        self, container_id: str, message: str = "docker exec timed out after 15 s"
    ) -> None:
        """Script a control timeout: the next control call raises."""
        self.set_control_error(container_id, ControlTimeoutError(message))

    def set_command_status(
        self, container_id: str, stdout: bytes, stderr: bytes = b"", returncode: int = 0
    ) -> None:
        """Script one ``agentctl command <id>`` reply: reported, never raised."""
        self._status_scripts[container_id] = ControlExec(
            stdout=stdout, stderr=stderr, returncode=returncode
        )

    def set_command_status_error(self, container_id: str, error: Exception) -> None:
        """Script one command-status error (busy, timeout, ...)."""
        self._status_scripts[container_id] = error

    def hold_control(self, container_id: str) -> None:
        """Pin a container as control-in-flight (collector skip tests).

        A hold survives scripted control calls (only :meth:`release_control`
        clears it); a control call for a held container raises at once.
        """
        self._control_held.add(container_id)

    def release_control(self, container_id: str) -> None:
        """Clear a :meth:`hold_control` pin."""
        self._control_held.discard(container_id)

    def control_in_flight(self, container_id: str) -> bool:
        """True while a scripted control or command-status exec runs or is held."""
        return container_id in self._control_active or container_id in self._control_held

    async def _run_scripted_control(
        self, container_id: str, scripts: dict[str, ControlExec | Exception]
    ) -> ControlExec:
        """One scripted control-slot exec: busy when held or already running."""
        if self.control_in_flight(container_id):
            raise ControlBusyError(
                f"another control exec for container {container_id} is already in flight"
            )
        self._control_active.add(container_id)
        try:
            delay = self.control_delays.get(container_id, self.control_delay)
            if delay:
                await asyncio.sleep(delay)
            scripted = scripts.get(
                container_id, ControlExec(stdout=b"", stderr=b"", returncode=0)
            )
            if isinstance(scripted, Exception):
                raise scripted
            return scripted
        finally:
            self._control_active.discard(container_id)

    async def control(
        self,
        container_id: str,
        action: str,
        *,
        request_id: str,
        n: int | str | None = None,
        issue: int | str | None = None,
        expect_repo: str | None = None,
    ) -> ControlExec:
        """One scripted Control action exec, validated like the real gateway."""
        argv = argv_control(
            container_id,
            action,
            request_id=request_id,
            n=n,
            issue=issue,
            expect_repo=expect_repo,
        )
        if self.control_in_flight(container_id):
            raise ControlBusyError(
                f"another control exec for container {container_id} is already in flight"
            )
        self.control_argvs.append(argv)
        self.control_calls.append(
            {
                "container_id": container_id,
                "action": action,
                "request_id": request_id,
                "n": n,
                "issue": issue,
                "expect_repo": expect_repo,
            }
        )
        return await self._run_scripted_control(container_id, self._control_scripts)

    async def command_status(self, container_id: str, request_id: str) -> ControlExec:
        """One scripted ``agentctl command <id>`` exec, validated like the real one."""
        argv = argv_command_status(container_id, request_id)
        if self.control_in_flight(container_id):
            raise ControlBusyError(
                f"another control exec for container {container_id} is already in flight"
            )
        self.control_argvs.append(argv)
        self.command_status_calls.append((container_id, request_id))
        return await self._run_scripted_control(container_id, self._status_scripts)

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

    async def attempt_events(
        self, container_id: str, issue: int | str, started: str
    ) -> TelemetryExec:
        """One scripted events exec; unscripted IDs report an empty payload."""
        self.events_calls.append((container_id, str(issue), started))
        scripted = self._events.get(
            container_id,
            TelemetryExec(
                stdout=json.dumps(
                    {
                        "layout": "attempt-events/v1",
                        "issue": 98,
                        "started": "2026-09-27T16-33-43.385090Z",
                        "attempt": {"outcome": "complete"},
                        "events": [],
                        "total_events": 0,
                        "unreadable": 0,
                        "truncated": False,
                    }
                ).encode(),
                returncode=0,
            ),
        )
        if isinstance(scripted, Exception):
            raise scripted
        return scripted

    async def attempt_log(
        self, container_id: str, issue: int | str, started: str
    ) -> TelemetryExec:
        """One scripted full-log read; unscripted IDs report an empty stream."""
        self.log_calls.append((container_id, str(issue), started))
        scripted = self._logs.get(
            container_id,
            TelemetryExec(
                stdout=json.dumps(
                    {
                        "layout": "attempt-log/v1",
                        "issue": 98,
                        "started": "2026-09-27T16-33-43.385090Z",
                        "attempt": {"outcome": "complete"},
                        "events": [],
                        "total_events": 0,
                        "unreadable": 0,
                        "truncated": False,
                        "file_size": 0,
                    }
                ).encode(),
                returncode=0,
            ),
        )
        if isinstance(scripted, Exception):
            raise scripted
        return scripted

    async def attempt_tail_log(
        self, container_id: str, issue: int | str, started: str
    ) -> TelemetryExec:
        """One scripted tail read; unscripted IDs report an empty tail."""
        self.tail_calls.append((container_id, str(issue), started))
        scripted = self._tails.get(
            container_id,
            TelemetryExec(
                stdout=json.dumps(
                    {
                        "layout": "attempt-log-tail/v1",
                        "issue": 98,
                        "started": "2026-09-27T16-33-43.385090Z",
                        "attempt": {"outcome": "complete"},
                        "events": [],
                        "total_events": 0,
                        "unreadable": 0,
                        "truncated": False,
                        "file_size": 0,
                    }
                ).encode(),
                returncode=0,
            ),
        )
        if isinstance(scripted, Exception):
            raise scripted
        return scripted

    def set_run_files(
        self,
        container_id: str,
        issue: int | str,
        started: str,
        files: list[str] | Exception,
    ) -> None:
        self._run_files[(container_id, str(issue), started)] = files

    async def list_run_files(
        self, container_id: str, issue: int | str, started: str
    ) -> list[str]:
        """Scripted run listing; unscripted runs hold no files."""
        self.run_files_calls.append((container_id, str(issue), started))
        scripted = self._run_files.get((container_id, str(issue), started), [])
        if isinstance(scripted, Exception):
            raise scripted
        return list(scripted)

    def set_logs_tree(
        self, container_id: str, data: bytes | Exception
    ) -> None:
        self._logs_trees[container_id] = data

    def set_run_tree(
        self,
        container_id: str,
        issue: int | str,
        started: str,
        data: bytes | Exception,
    ) -> None:
        self._run_trees[(container_id, str(issue), started)] = data

    async def copy_logs(self, container_id: str) -> bytes:
        """One scripted logs-tree copy; unscripted trees report not found."""
        self.logs_tree_calls.append(container_id)
        scripted = self._logs_trees.get(
            container_id, FileNotFoundError("attempt logs not found")
        )
        if isinstance(scripted, Exception):
            raise scripted
        assert isinstance(scripted, bytes)
        return scripted

    async def copy_run(
        self, container_id: str, issue: int | str, started: str
    ) -> bytes:
        """One scripted run-directory copy; unscripted runs report not found."""
        self.run_tree_calls.append((container_id, str(issue), started))
        scripted = self._run_trees.get(
            (container_id, str(issue), started),
            FileNotFoundError(f"attempt not found: {issue}/{started}"),
        )
        if isinstance(scripted, Exception):
            raise scripted
        assert isinstance(scripted, bytes)
        return scripted

    def set_file(
        self,
        container_id: str,
        issue: int | str,
        started: str,
        filename: str,
        result: FileResult | Exception,
    ) -> None:
        self._files[(container_id, str(issue), started, filename)] = result

    async def read_file(
        self,
        container_id: str,
        issue: int | str,
        started: str,
        filename: str,
        cap: int | None = EVIDENCE_PREVIEW_CAP,
    ) -> FileResult:
        """One scripted file read; unscripted files report not found."""
        self.file_calls.append((container_id, str(issue), started, filename))
        scripted = self._files.get(
            (container_id, str(issue), started, filename),
            FileNotFoundError(f"evidence file not found: {filename!r}"),
        )
        if isinstance(scripted, Exception):
            raise scripted
        assert isinstance(scripted, FileResult)
        if cap is not None and len(scripted.content) > cap:
            return FileResult(
                content=scripted.content[:cap],
                total_size=scripted.total_size,
                truncated=True,
            )
        return scripted
