"""Tolerant reader for ``agentctl`` control replies (spec section 4).

Turns one finished control exec (:class:`ControlExec`: raw stdout, raw
stderr plus the exit code) into a :class:`ControlReply` with a ``kind``
and the parsed fields. Pure: no Docker, no I/O, never raises on agent
output (garbage bytes, empty output and invalid UTF-8 all classify).

Parsing rules (spec section 4):

- stdout on exit 0: one ``key: value`` line per row
  (``^([a-z_]+):[ \\t]?(.*)$``). The first occurrence of a key wins.
  Lines that do not match are ignored, and so are unknown keys (they
  stay in :attr:`ControlReply.raw` for tests, never rendered). Key order
  is not relied on.
- Keys used: ``repository``, ``request_id``, ``sequence`` (an integer,
  else none), ``acknowledgement``, ``effect``. Each may be missing.
- stderr on exit 1/2: an optional first ``repository: <owner/name>``
  line, taken when present; the rest, stripped and capped at 2,000
  characters, is the reason. The reason is display text only: nothing
  branches on it.

Kinds (spec section 4):

- 0 ``accepted``; 1 ``rejected``; 2 with a stderr line starting
  ``usage:`` is ``usage_error`` (a dashboard bug or version skew, never
  retried), 2 otherwise ``unknown``; 125, 126, 127 ``failed`` (Docker
  could not run ``agentctl``, nothing was sent); any other code
  ``unknown``; a dashboard timeout is ``unknown``; a missing Docker CLI
  is ``failed``.
- ``agentctl command <id>`` shares the parser. Exit 0 yields the
  record (``record``), exit 1 is ``not_recorded`` (never committed, so
  retrying with the same request ID is safe), exit 2 is ``unknown``
  ("Could not check") unless it is a ``usage:`` refusal.

Repository handling: on exit 0 the reply ``repository:`` is compared
with the expected repository case-insensitively. A difference is a
repository mismatch (the command was applied, nothing is undone). A
missing ``repository:`` line is no mismatch (the flag is none) with a
note. On a rejection a present ``repository:`` line is kept for the
Control audit; the card still says Rejected.

Version gating (spec section 5.4): :data:`MINIMUM_VERSIONS` maps
agent-side flags to the first agent version supporting them. The
``--expect-repo`` entry is pending, so it never enables the flag. The
version is parsed from a leading ``v?<major>.<minor>.<patch>``; an
unreadable or missing version counts as old.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .gateway import ControlBusyError, ControlExec, ControlTimeoutError, DockerNotAvailableError

#: Display-text cap for the stderr-derived reason (spec section 4).
REASON_MAX_LENGTH = 2000

#: One stdout ``key: value`` row: lowercase/underscore key, one optional
#: space or tab, then the value (which may itself hold colons).
STDOUT_LINE_RE = re.compile(r"([a-z_]+):[ \t]?(.*)")

#: First-class stdout keys (spec section 4). Anything else lands in
#: :attr:`ControlReply.raw` only.
KNOWN_STDOUT_KEYS = frozenset(
    {"repository", "request_id", "sequence", "acknowledgement", "effect"}
)

#: Note shown when an exit-0 reply names no repository.
MISSING_REPOSITORY_NOTE = "the reply did not name a repository"

#: Reply kinds for mutating Control actions.
ACCEPTED = "accepted"
REJECTED = "rejected"
USAGE_ERROR = "usage_error"
UNKNOWN = "unknown"
FAILED = "failed"

#: Extra kinds for ``agentctl command <id>`` status replies.
RECORD = "record"
NOT_RECORDED = "not_recorded"

#: Agent-side flags gated by agent version (spec section 5.4). A value of
#: ``None`` means pending: the release that ships the flag is not known
#: yet, so the flag is never passed.
MINIMUM_VERSIONS: dict[str, tuple[int, int, int] | None] = {
    "expect_repo": None,
}

#: Leading ``v?<major>.<minor>.<patch>`` (spec section 5.4). Anything
#: after the patch (``-rc1``, ``+build``, ...) is not part of the version.
_AGENT_VERSION_RE = re.compile(r"\s*v?(\d+)\.(\d+)\.(\d+)")


@dataclass(frozen=True)
class ControlReply:
    """One parsed control exec: its kind plus the tolerant fields.

    ``repository`` is the ``repository:`` line wherever it was found
    (stdout on exit 0, stderr on exit 1/2), else none. ``reason`` is the
    stderr-derived display text ("" when there is none). Nothing branches
    on ``reason``. ``repository_mismatch`` is true/false when both the
    reply and the expected repository named one, else none (including a
    missing reply line on exit 0, which sets ``repository_note``).
    ``raw`` keeps every first-occurrence stdout ``key: value`` pair,
    including unknown keys, for tests; templates never render it.
    ``exit_code`` is the exec's exit code, or none for exception-mapped
    replies (timeout, Docker missing).
    """

    kind: str = UNKNOWN
    repository: str | None = None
    request_id: str | None = None
    sequence: int | None = None
    acknowledgement: str | None = None
    effect: str | None = None
    reason: str = ""
    repository_mismatch: bool | None = None
    repository_note: str | None = None
    raw: dict[str, str] = field(default_factory=dict)
    exit_code: int | None = None


def _decode(data: Any) -> str:
    """Agent bytes as text, never raising (invalid UTF-8 is replaced)."""
    if isinstance(data, str):
        return data
    if isinstance(data, (bytes, bytearray, memoryview)):
        return bytes(data).decode("utf-8", errors="replace")
    return ""


def _parse_stdout(stdout: Any) -> dict[str, str]:
    """First-occurrence ``key: value`` mapping for raw stdout bytes."""
    text = _decode(stdout)
    parsed: dict[str, str] = {}
    for line in text.splitlines():
        match = STDOUT_LINE_RE.match(line)
        if match is None:
            continue
        key, value = match.group(1), match.group(2)
        if key not in parsed:
            parsed[key] = value
    return parsed


def _present(value: Any) -> str | None:
    """A stripped text field, or none when missing or blank."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _parse_sequence(value: Any) -> int | None:
    """An integer sequence, else none (never raises).

    Only ASCII decimal digits count: ``+12``, ``-3`` or ``1_0`` are not
    sequence numbers, so they read as none rather than as integers.
    """
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped or any(char not in "0123456789" for char in stripped):
        return None
    try:
        return int(stripped, 10)
    except ValueError:
        return None


def _parse_stderr(stderr: Any) -> tuple[str | None, str]:
    """Split raw stderr into ``(repository, reason)``.

    An optional first ``repository: <owner/name>`` line is taken when
    present; the rest, stripped and capped at 2,000 characters, is the
    display-only reason.
    """
    text = _decode(stderr)
    lines = text.splitlines()
    repository: str | None = None
    rest = lines
    if lines:
        match = STDOUT_LINE_RE.match(lines[0])
        if match is not None and match.group(1) == "repository":
            candidate = match.group(2).strip()
            repository = candidate or None
            rest = lines[1:]
    reason = "\n".join(rest).strip()
    if len(reason) > REASON_MAX_LENGTH:
        reason = reason[:REASON_MAX_LENGTH]
    return repository, reason


def _is_usage_error(stderr: Any) -> bool:
    """Whether any stderr line starts with ``usage:`` (a dashboard bug)."""
    for line in _decode(stderr).splitlines():
        if line.lstrip().startswith("usage:"):
            return True
    return False


def _exit_code(exec_result: Any) -> int | None:
    """The exec's exit code, or none when it is not a plain int."""
    code = getattr(exec_result, "returncode", None)
    if isinstance(code, bool) or not isinstance(code, int):
        return None
    return code


def _mismatch(
    reply_repository: str | None, expected_repository: Any
) -> bool | None:
    """Case-insensitive repository comparison, or none when either is missing."""
    if not isinstance(expected_repository, str) or not expected_repository:
        return None
    if not isinstance(reply_repository, str) or not reply_repository:
        return None
    return reply_repository.lower() != expected_repository.lower()


def _shared_kind(returncode: int | None, stderr: Any) -> str | None:
    """The kind shared by both exec shapes, or none when they differ.

    Exit 2 with a ``usage:`` stderr line is ``usage_error`` (a dashboard
    bug or version skew, never retried), 125/126/127 is ``failed``
    (Docker could not run ``agentctl``, nothing was sent), and anything
    else is ``unknown``. Exits 0 and 1 classify per exec shape, so they
    return none here.
    """
    if returncode in (0, 1):
        return None
    if returncode == 2:
        return USAGE_ERROR if _is_usage_error(stderr) else UNKNOWN
    if returncode in (125, 126, 127):
        return FAILED
    return UNKNOWN


def _control_kind(returncode: int | None, stderr: Any) -> str:
    """Kind for a mutating Control action from its exit code."""
    if returncode == 0:
        return ACCEPTED
    if returncode == 1:
        return REJECTED
    shared = _shared_kind(returncode, stderr)
    assert shared is not None
    return shared


def _command_kind(returncode: int | None, stderr: Any) -> str:
    """Kind for ``agentctl command <id>`` from its exit code."""
    if returncode == 0:
        return RECORD
    if returncode == 1:
        return NOT_RECORDED
    shared = _shared_kind(returncode, stderr)
    assert shared is not None
    return shared


@dataclass(frozen=True)
class _StdoutFields:
    """First-class stdout fields plus the full first-occurrence mapping."""

    raw: dict[str, str]
    repository: str | None = None
    request_id: str | None = None
    sequence: int | None = None
    acknowledgement: str | None = None
    effect: str | None = None


def _stdout_fields(exec_result: Any, returncode: int | None) -> _StdoutFields:
    """Parsed stdout fields (exit 0 only; anything else reads as absent)."""
    if returncode != 0:
        return _StdoutFields(raw={})
    try:
        stdout = exec_result.stdout
    except AttributeError:
        stdout = b""
    raw = _parse_stdout(stdout)
    return _StdoutFields(
        raw=raw,
        repository=_present(raw.get("repository")),
        request_id=_present(raw.get("request_id")),
        sequence=_parse_sequence(raw["sequence"]) if "sequence" in raw else None,
        acknowledgement=_present(raw.get("acknowledgement")),
        effect=_present(raw.get("effect")),
    )


def _build_reply(
    exec_result: Any,
    *,
    kind: str,
    returncode: int | None,
    expected_repository: Any = None,
) -> ControlReply:
    """Assemble a reply from parsed stdout/stderr plus the mismatch flag."""
    try:
        stderr = exec_result.stderr
    except AttributeError:
        stderr = b""
    fields = _stdout_fields(exec_result, returncode)
    repository = fields.repository
    reason = ""
    if returncode != 0:
        stderr_repository, reason = _parse_stderr(stderr)
        repository = stderr_repository
    mismatch = _mismatch(repository, expected_repository)
    note: str | None = None
    if returncode == 0 and repository is None:
        note = MISSING_REPOSITORY_NOTE
    return ControlReply(
        kind=kind,
        repository=repository,
        request_id=fields.request_id,
        sequence=fields.sequence,
        acknowledgement=fields.acknowledgement,
        effect=fields.effect,
        reason=reason,
        repository_mismatch=mismatch,
        repository_note=note,
        raw=dict(fields.raw),
        exit_code=returncode,
    )


def parse_control_reply(
    exec_result: ControlExec, *, expected_repository: str | None = None
) -> ControlReply:
    """Parse one mutating Control action exec into a reply (never raises).

    Kinds follow the spec kinds table: 0 ``accepted``, 1 ``rejected``,
    2 with a ``usage:`` stderr line ``usage_error`` else ``unknown``,
    125/126/127 ``failed``, anything else ``unknown``. A bad ``exec``
    shape (missing stdout/stderr, non-integer exit) classifies as
    ``unknown`` rather than raising.
    """
    try:
        stderr: Any = exec_result.stderr
    except AttributeError:
        return ControlReply(kind=UNKNOWN, exit_code=_exit_code(exec_result))
    returncode = _exit_code(exec_result)
    return _build_reply(
        exec_result,
        kind=_control_kind(returncode, stderr),
        returncode=returncode,
        expected_repository=expected_repository,
    )


def parse_command_reply(
    exec_result: ControlExec, *, expected_repository: str | None = None
) -> ControlReply:
    """Parse one ``agentctl command <id>`` exec into a reply (never raises).

    Shares the tolerant parser with :func:`parse_control_reply`. Exit 0
    yields the record (``record``), exit 1 is ``not_recorded`` (never
    committed, so retrying with the same request ID is safe), exit 2 is
    ``unknown`` ("Could not check") unless it is a ``usage:`` refusal,
    125/126/127 is ``failed``, anything else ``unknown``.
    """
    try:
        stderr: Any = exec_result.stderr
    except AttributeError:
        return ControlReply(kind=UNKNOWN, exit_code=_exit_code(exec_result))
    returncode = _exit_code(exec_result)
    return _build_reply(
        exec_result,
        kind=_command_kind(returncode, stderr),
        returncode=returncode,
        expected_repository=expected_repository,
    )


def timeout_reply(message: str = "control exec timed out") -> ControlReply:
    """The reply for a dashboard-side control timeout: ``unknown``.

    Killing the ``docker exec`` client does not stop ``agentctl`` inside
    the container, so a timeout means "outcome unknown". (A ``command
    <id>`` check renders the same kind as "Could not check".)
    """
    reason = message if isinstance(message, str) else "control exec timed out"
    reason = reason.strip()[:REASON_MAX_LENGTH]
    return ControlReply(
        kind=UNKNOWN,
        reason=reason,
        exit_code=None,
    )


def docker_unavailable_reply(message: str = "docker CLI not found") -> ControlReply:
    """The reply when Docker never started: ``failed`` (nothing was sent)."""
    reason = message if isinstance(message, str) else "docker CLI not found"
    reason = reason.strip()[:REASON_MAX_LENGTH]
    return ControlReply(
        kind=FAILED,
        reason=reason,
        exit_code=None,
    )


def reply_for_exception(exc: BaseException) -> ControlReply:
    """Map a control-exec exception to its reply kind (spec kinds table).

    :class:`ControlTimeoutError` is ``unknown``; a missing Docker CLI
    (:class:`DockerNotAvailableError`) is ``failed``. Anything else is
    re-raised: only these two exceptions mean a kind without an exit.
    (:class:`ControlBusyError` is handled by the caller as a 409 refusal,
    never as a reply.)
    """
    if isinstance(exc, ControlTimeoutError):
        return timeout_reply(str(exc) or "control exec timed out")
    if isinstance(exc, DockerNotAvailableError):
        return docker_unavailable_reply(str(exc) or "docker CLI not found")
    if isinstance(exc, ControlBusyError):
        raise exc
    raise exc


def parse_agent_version(text: str | None) -> tuple[int, int, int] | None:
    """Parse ``v?<major>.<minor>.<patch>`` from the version's start.

    Returns the ``(major, minor, patch)`` triple, or none when the text
    is missing or unreadable. An unreadable or missing version counts as
    old. Never raises.
    """
    if not isinstance(text, str) or not text:
        return None
    match = _AGENT_VERSION_RE.match(text)
    if match is None:
        return None
    try:
        return (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def agent_supports(feature: str, agent_version: str | None) -> bool:
    """Whether ``agent_version`` first supports ``feature`` (never raises).

    An unknown feature, a pending minimum (none), or an unreadable or
    missing version counts as unsupported (old).
    """
    minimum = MINIMUM_VERSIONS.get(feature)
    if minimum is None:
        return False
    parsed = parse_agent_version(agent_version)
    if parsed is None:
        return False
    return parsed >= minimum


def supports_expect_repo(agent_version: str | None) -> bool:
    """Whether the agent supports ``--expect-repo`` (pending: never yet).

    Until the agent release that ships the guard is known, the
    ``--expect-repo`` entry stays pending and this is always false, so
    the dashboard never passes the flag.
    """
    return agent_supports("expect_repo", agent_version)
