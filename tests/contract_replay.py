"""Replaying process runner for contract-path tests.

Replays ``{argv, exit_code, stdout, stderr}`` recordings (the consumer suite's
fixture format) at the gateway's process-runner boundary and fails the test
on an unexpected argv. Recordings always come from the pinned
agent-operator-contract package (see :func:`from_package`); they are never
copied into this repository.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from agent_dashboard.gateway import ControlExec

AGENTCTL_PATH = "/usr/local/bin/agentctl"


class UnexpectedArgvError(AssertionError):
    """A contract exec ran with an argv no recording answers."""


def _check_fixture(fixture: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the recording, validating its consumer-suite shape first."""
    from agent_operator_contract import documents

    problems = documents.validate_fixture(fixture)
    if problems:
        raise ValueError(f"invalid contract fixture: {problems!r}")
    return fixture


class ReplayProcessRunner:
    """Replay canned contract replies, failing on any unexpected argv.

    Each recording answers once: the first recording whose ``argv`` equals
    the agentctl args (everything after ``/usr/local/bin/agentctl`` in the
    ``docker exec`` argv) is consumed and returned as a finished exec. A run
    with no matching recording raises :class:`UnexpectedArgvError`, so the
    test fails at the exact unexpected call. Every run is recorded on
    :attr:`calls` (full Docker argv) for further assertions.
    """

    def __init__(self, fixtures: Iterable[Mapping[str, Any]]) -> None:
        self._fixtures = [_check_fixture(fixture) for fixture in fixtures]
        self.calls: list[list[str]] = []

    @classmethod
    def from_package(cls, names: Iterable[str]) -> ReplayProcessRunner:
        """Replay the named consumer fixtures from the pinned package data."""
        from agent_operator_contract import documents

        return cls([documents.load_fixture(name) for name in names])

    @property
    def remaining(self) -> int:
        """Recordings not yet consumed."""
        return len(self._fixtures)

    def _agentctl_argv(self, argv: list[str]) -> list[str] | None:
        """The agentctl args of a ``docker exec`` argv, or none without one."""
        if AGENTCTL_PATH not in argv:
            return None
        return argv[argv.index(AGENTCTL_PATH) + 1 :]

    async def run(
        self, argv: list[str], *, timeout: float, timeout_error: Exception
    ) -> ControlExec:
        """Replay the first recording matching the agentctl args.

        The timeout is accepted and ignored: replays answer at once. An argv
        with no ``agentctl`` call or no matching recording raises
        :class:`UnexpectedArgvError`.
        """
        _ = (timeout, timeout_error)
        self.calls.append(list(argv))
        wanted = self._agentctl_argv(list(argv))
        for index, fixture in enumerate(self._fixtures):
            if wanted is not None and list(fixture["argv"]) == wanted:
                recording = self._fixtures.pop(index)
                stdout = recording["stdout"]
                stderr = recording["stderr"]
                return ControlExec(
                    stdout=stdout.encode("utf-8"),
                    stderr=stderr.encode("utf-8"),
                    returncode=recording["exit_code"],
                )
        raise UnexpectedArgvError(f"no contract recording answers argv: {argv!r}")
