"""Contract Attempt page: header, archive events, manifest and artifact
previews/downloads (contract major 1).

One contract Attempt has its own page, reached from the Attempt history
row and addressed by its opaque Attempt ID (URL-encoded). The header comes
from ``attempts show <attempt-id>`` (with the sealed state, marked "still
changing" while the Attempt is not sealed); the Attempt's Contract Events
come from the ``events.jsonl`` member (kind ``events``) of its archive,
fetched with ``attempts export <attempt-id> --path <path>``; the archive
manifest is listed by kind with size and media type.

Every manifest entry carries the member's ``sha256``: an export is verified
(size and ``sha256``) before it is trusted, and a torn read of an unsealed
archive is fetched once more before the page reports "changing, try
again". Artifact previews fetch one member with ``attempts export
--path`` and render it by its ``media_type`` (JSON with a formatted/raw
toggle, NDJSON as rows, ``text/*`` as text, anything else download only);
previews stop after 256 KiB and name the manifest's true size, while
downloads serve the whole verified file.

Tolerance: malformed or unknown replies become a clear message, never an
exception; unknown fields are ignored; archive event lines that do not
parse are skipped and counted as unreadable.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from dataclasses import dataclass
from typing import Any

from agent_operator_contract import documents

from .attempt_log import KNOWN_LEVELS
from .contract_history import ContractAttempt, read_attempt_summary
from .contract_reply import (
    AMBIGUOUS,
    CONTRACT_MISMATCH,
    NOT_FOUND,
    REJECTION,
    SUCCESS,
    UNSUPPORTED,
    classify_contract_reply,
)

#: Bound for an opaque Attempt ID: longer values are refused before any
#: argv exists, so no unbounded path segment ever reaches Docker.
ATTEMPT_ID_MAX_LENGTH = 256

#: Archive member carrying the Attempt's Contract Events (kind ``events``).
EVENTS_KIND = "events"

#: First member of every ``attempts export`` tar stream.
EXPORT_MANIFEST_NAME = "manifest.json"


def validate_attempt_id_shape(attempt_id: Any) -> str:
    """Return the Attempt ID when its shape is safe for an argv, else raise.

    Anything else raises :class:`ValueError` before any argv exists: an
    empty or non-string value, a value over the length bound, a leading
    ``-`` (an option smuggle), or control characters (including DEL).
    Membership (whether the instance listed the ID) is checked by the
    caller, which knows the instance name.
    """
    if not isinstance(attempt_id, str) or not attempt_id:
        raise ValueError(f"invalid attempt id: {attempt_id!r}")
    if len(attempt_id) > ATTEMPT_ID_MAX_LENGTH:
        raise ValueError(
            f"invalid attempt id: too long "
            f"({len(attempt_id)} characters, "
            f"at most {ATTEMPT_ID_MAX_LENGTH})"
        )
    if attempt_id.startswith("-"):
        raise ValueError(f"invalid attempt id: {attempt_id!r}")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in attempt_id):
        raise ValueError(f"invalid attempt id: {attempt_id!r}")
    return attempt_id


def validate_contract_attempt_id(attempt_id: Any, known_ids: Any) -> str:
    """Return the Attempt ID when the instance listed it, else raise.

    The shape is validated first (see :func:`validate_attempt_id_shape`);
    then the ID must be one the instance listed (the dashboard remembers
    listed IDs while reading the Attempt history). Anything else raises
    :class:`ValueError` before any argv exists.
    """
    validate_attempt_id_shape(attempt_id)
    try:
        listed = set(known_ids)
    except TypeError:
        listed = set()
    assert isinstance(attempt_id, str)
    if attempt_id not in listed:
        raise ValueError(f"unknown attempt id: {attempt_id!r}")
    return attempt_id


def validate_export_path(path: Any) -> str:
    """Return the archive member path for ``--path``, else raise.

    The path must be a relative manifest path: non-empty, no leading
    ``/``, and no ``..`` segment (the contract's manifest path pattern).
    """
    if (
        not isinstance(path, str)
        or not path
        or path.startswith("/")
        or path in (".", "..")
        or "/../" in f"/{path}/"
        or path.startswith("../")
        or path.endswith("/..")
    ):
        raise ValueError(f"invalid export path: {path!r}")
    return path


@dataclass(frozen=True)
class ContractManifestEntry:
    """One archive manifest entry: kind, path, size, media type and sha256."""

    kind: str
    path: str
    size: int
    media_type: str
    sha256: str
    size_text: str


#: Preview bound for one artifact member: previews stop reading after this
#: many bytes and name the manifest's true size (mirrors the legacy
#: Evidence preview cap).
ARTIFACT_PREVIEW_CAP = 256 * 1024

#: Shown when two consecutive reads of an unsealed archive both disagree
#: with the manifest: the archive is still moving, so nothing is final.
CHANGING_MESSAGE = (
    "The archive is still changing, try again: "
    "the artifact did not match its manifest on two reads."
)


@dataclass(frozen=True)
class ContractAttemptShow:
    """One parsed ``attempts show`` reply: header plus manifest."""

    ok: bool
    error: str | None = None
    not_found: bool = False
    summary: ContractAttempt | None = None
    manifest: tuple[ContractManifestEntry, ...] = ()
    still_changing: bool = False


def format_manifest_size(size: int) -> str:
    """Display text for one manifest size, e.g. ``412 B`` or ``2.1 MB``."""
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        return "—"
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _envelope_error(stdout: bytes) -> tuple[str | None, str]:
    try:
        envelope = json.loads(stdout.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
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


def _read_manifest_entry(entry: Any) -> ContractManifestEntry | None:
    if documents.validate_manifest_entry(entry, "manifest") != []:
        return None
    assert isinstance(entry, dict)
    kind = entry.get("kind")
    path = entry.get("path")
    size = entry.get("size")
    media_type = entry.get("media_type")
    sha256 = entry.get("sha256")
    if (
        not isinstance(kind, str)
        or not isinstance(path, str)
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size < 0
        or not isinstance(media_type, str)
        or not media_type
        or not isinstance(sha256, str)
        or not sha256
    ):
        return None
    return ContractManifestEntry(
        kind=kind,
        path=path,
        size=size,
        media_type=media_type,
        sha256=sha256,
        size_text=format_manifest_size(size),
    )


def parse_contract_attempts_show(
    stdout: bytes, returncode: int | None, *, attempt_id: str = "?"
) -> ContractAttemptShow:
    """Parse one ``attempts show`` exec into the header plus the manifest.

    Never raises: malformed or unknown replies become a clear message.
    ``not_found`` is set for a missing Attempt so the page can answer 404.
    """
    cls = classify_contract_reply(returncode, stdout)
    if cls not in (SUCCESS, "replay"):
        code, message = _envelope_error(stdout if isinstance(stdout, bytes) else b"")
        if cls == CONTRACT_MISMATCH:
            detail = message or code or "the instance speaks another contract major"
            return ContractAttemptShow(ok=False, error=f"Contract mismatch: {detail}")
        if cls == UNSUPPORTED:
            detail = message or code or "unsupported"
            return ContractAttemptShow(
                ok=False,
                error=f"Attempt not supported by this instance: {detail}",
            )
        if cls == NOT_FOUND:
            return ContractAttemptShow(
                ok=False,
                error=(
                    f"Attempt {attempt_id!r} was not found on this instance: "
                    f"{message or code or 'not found'}"
                ),
                not_found=True,
            )
        if cls == REJECTION:
            return ContractAttemptShow(
                ok=False,
                error=f"Attempt {attempt_id!r} rejected: {message or code or 'rejected'}",
            )
        if cls == AMBIGUOUS:
            if code or message:
                return ContractAttemptShow(
                    ok=False,
                    error=f"Could not read the Attempt: {message or code}",
                )
            return ContractAttemptShow(ok=False, error="Attempt not recognised")
        return ContractAttemptShow(
            ok=False, error=f"Unexpected Attempt reply: {cls}"
        )
    try:
        payload = json.loads(
            stdout.decode("utf-8") if isinstance(stdout, bytes) else str(stdout)
        )
    except (ValueError, UnicodeDecodeError):
        return ContractAttemptShow(ok=False, error="Attempt not recognised")
    if not isinstance(payload, dict):
        return ContractAttemptShow(ok=False, error="Attempt not recognised")
    result = payload.get("result")
    if not isinstance(result, dict):
        return ContractAttemptShow(ok=False, error="Attempt not recognised")
    problems = documents.validate_attempts_show(result)
    if problems != []:
        if "manifest" in " ".join(problems).lower():
            return ContractAttemptShow(
                ok=False, error="Attempt manifest not recognised"
            )
        return ContractAttemptShow(ok=False, error="Attempt not recognised")
    summary = read_attempt_summary(result.get("attempt"))
    if summary is None:
        return ContractAttemptShow(ok=False, error="Attempt not recognised")
    manifest: list[ContractManifestEntry] = []
    raw_manifest = result.get("manifest")
    assert isinstance(raw_manifest, list)
    for entry in raw_manifest:
        parsed = _read_manifest_entry(entry)
        if parsed is None:
            return ContractAttemptShow(
                ok=False, error="Attempt manifest not recognised"
            )
        manifest.append(parsed)
    if not any(
        entry.kind == EVENTS_KIND and entry.path == "events.jsonl"
        for entry in manifest
    ):
        return ContractAttemptShow(ok=False, error="Attempt manifest not recognised")
    return ContractAttemptShow(
        ok=True,
        summary=summary,
        manifest=tuple(manifest),
        still_changing=not summary.sealed,
    )


@dataclass(frozen=True)
class ContractExportResult:
    """One parsed ``attempts export`` reply: the requested member's bytes."""

    ok: bool
    error: str | None = None
    not_found: bool = False
    data: bytes | None = None


def extract_export_member(tar_bytes: bytes, wanted: str) -> tuple[bytes, dict[str, Any]]:
    """Return ``(member bytes, export manifest)`` for one archive member.

    The tar stream must start with ``manifest.json``; every member name is
    checked against the export manifest's file list, and exactly the
    requested member is returned. Anything else raises :class:`ValueError`.
    """
    validate_export_path(wanted)
    if not isinstance(tar_bytes, bytes) or not tar_bytes:
        raise ValueError("export stream is empty")
    try:
        stream = io.BytesIO(tar_bytes)
        with tarfile.open(fileobj=stream, mode="r:*") as tar:
            members = tar.getmembers()
            if not members or members[0].name != EXPORT_MANIFEST_NAME:
                raise ValueError("export stream must start with manifest.json")
            first = tar.extractfile(members[0])
            if first is None:
                raise ValueError("export stream must start with manifest.json")
            raw_manifest = first.read()
            try:
                manifest = json.loads(raw_manifest.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise ValueError(f"export manifest is not JSON: {exc}") from exc
            problems = documents.validate_export_manifest(manifest)
            if problems != []:
                raise ValueError(f"export manifest not recognised: {problems[0]}")
            assert isinstance(manifest, dict)
            files = manifest.get("files")
            assert isinstance(files, list)
            allowed = {EXPORT_MANIFEST_NAME}
            for entry in files:
                if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                    allowed.add(entry["path"])
            if wanted not in allowed:
                raise ValueError(
                    f"export member {wanted!r} is not in the archive manifest"
                )
            for member in members[1:]:
                if member.isdir():
                    continue
                if not member.isfile():
                    raise ValueError(
                        f"export member {member.name!r} is not a regular file"
                    )
                if member.name not in allowed:
                    raise ValueError(
                        f"export member {member.name!r} "
                        "is not in the archive manifest"
                    )
            for member in members[1:]:
                if member.isfile() and member.name == wanted:
                    handle = tar.extractfile(member)
                    if handle is None:
                        raise ValueError(
                            f"export member {wanted!r} could not be read"
                        )
                    return handle.read(), manifest
            raise ValueError(f"export member {wanted!r} is missing from the archive")
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise ValueError(f"export stream is not a tar archive: {exc}") from exc


def parse_contract_attempt_export(
    stdout: bytes, returncode: int | None, *, attempt_id: str = "?", wanted: str
) -> ContractExportResult:
    """Parse one ``attempts export`` exec into the requested member's bytes.

    A non-zero exit after streaming started (a non-empty stdout) discards
    the output. Never raises; ``not_found`` marks a missing Attempt or a
    missing archive path so the page can answer 404.
    """
    streaming = isinstance(stdout, bytes) and len(stdout) > 0
    if (
        isinstance(returncode, bool)
        or not isinstance(returncode, int)
        or returncode != 0
    ):
        if streaming and not _looks_like_envelope(stdout):
            return ContractExportResult(
                ok=False,
                error=(
                    f"Attempt export for {attempt_id!r} failed "
                    "after streaming started: the output was discarded "
                    "and the export may be retried."
                ),
            )
        cls = classify_contract_reply(returncode, stdout)
        code, message = _envelope_error(stdout if isinstance(stdout, bytes) else b"")
        if cls == NOT_FOUND:
            return ContractExportResult(
                ok=False,
                error=(
                    f"Attempt archive for {attempt_id!r} was not found "
                    f"on this instance: {message or code or 'not found'}"
                ),
                not_found=True,
            )
        if cls == CONTRACT_MISMATCH:
            detail = message or code or "the instance speaks another contract major"
            return ContractExportResult(
                ok=False, error=f"Contract mismatch: {detail}"
            )
        if cls == UNSUPPORTED:
            detail = message or code or "unsupported"
            return ContractExportResult(
                ok=False,
                error=f"Attempt export not supported by this instance: {detail}",
            )
        if cls == REJECTION:
            return ContractExportResult(
                ok=False,
                error=(
                    f"Attempt export for {attempt_id!r} rejected: "
                    f"{message or code or 'rejected'}"
                ),
            )
        if code or message:
            return ContractExportResult(
                ok=False, error=f"Could not export the Attempt archive: {message or code}"
            )
        return ContractExportResult(
            ok=False, error="Attempt export not recognised"
        )
    try:
        data, _ = extract_export_member(stdout, wanted)
    except ValueError as exc:
        return ContractExportResult(ok=False, error=f"Could not read the archive: {exc}")
    return ContractExportResult(ok=True, data=data)


def _looks_like_envelope(stdout: Any) -> bool:
    """True when the bytes parse as a producer envelope (an error, not a stream)."""
    try:
        doc = json.loads(stdout.decode("utf-8") if isinstance(stdout, bytes) else "")
    except (ValueError, UnicodeDecodeError):
        return False
    return isinstance(doc, dict) and "ok" in doc


@dataclass(frozen=True)
class ContractArchiveEvent:
    """One Contract Event from the archive: filter and paging fields included.

    ``detail`` mirrors ``text`` so the shared level/event/text filters apply
    unchanged; ``is_native`` marks ``native.`` events.
    """

    seq: int
    timestamp_text: str = ""
    time_text: str = ""
    level: str = ""
    level_known: bool = False
    event: str = ""
    detail: str = ""
    is_native: bool = False


@dataclass(frozen=True)
class ContractEventsResult:
    """Parsed ``events.jsonl``: ordered rows plus the unreadable count."""

    ok: bool
    error: str | None = None
    events: tuple[ContractArchiveEvent, ...] = ()
    unreadable: int = 0


def _short_time(text: str) -> str:
    """Extract ``HH:MM:SS`` from an ISO-8601 timestamp, else the full text."""
    clock = text[11:19] if len(text) >= 19 and text[10] == "T" else ""
    if (
        len(clock) == 8
        and clock[2] == ":"
        and clock[5] == ":"
        and clock.replace(":", "").isdigit()
    ):
        return clock
    return text


def _is_native_name(name: str) -> bool:
    return name.startswith("native.") and len(name) > len("native.")


def _read_archive_event(entry: Any, seq: int) -> ContractArchiveEvent | None:
    if not isinstance(entry, dict):
        return None
    if documents.validate_contract_event(entry) != []:
        return None
    at = entry.get("at")
    level = entry.get("level")
    event = entry.get("event")
    text = entry.get("text")
    if (
        not isinstance(at, str)
        or not isinstance(level, str)
        or not isinstance(event, str)
        or not isinstance(text, str)
    ):
        return None
    return ContractArchiveEvent(
        seq=seq,
        timestamp_text=at,
        time_text=_short_time(at),
        level=level,
        level_known=level.upper() in KNOWN_LEVELS,
        event=event,
        detail=text,
        is_native=_is_native_name(event),
    )


def parse_contract_events(data: bytes) -> ContractEventsResult:
    """Parse one ``events.jsonl`` member into ordered rows.

    Lines that do not parse are skipped and counted as unreadable; an empty
    member (or one with no readable row) reads as not recognised, never as
    an empty stream. Never raises.
    """
    if not isinstance(data, bytes):
        return ContractEventsResult(ok=False, error="Contract events not recognised")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return ContractEventsResult(ok=False, error="Contract events not recognised")
    events: list[ContractArchiveEvent] = []
    unreadable = 0
    seq = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        seq += 1
        try:
            entry = json.loads(line)
        except ValueError:
            unreadable += 1
            continue
        parsed = _read_archive_event(entry, seq)
        if parsed is None:
            unreadable += 1
        else:
            events.append(parsed)
    if not events:
        return ContractEventsResult(ok=False, error="Contract events not recognised")
    return ContractEventsResult(ok=True, events=tuple(events), unreadable=unreadable)


def artifact_render_kind(media_type: Any) -> str:
    """Rendering for one artifact member: ``json``, ``ndjson``, ``text`` or ``binary``.

    ``application/json`` gets the formatted/raw toggle, ``application/x-ndjson``
    renders as rows, ``text/*`` renders as text, and anything else is download
    only. Never raises: an unknown value reads as ``binary``.
    """
    if not isinstance(media_type, str):
        return "binary"
    lowered = media_type.strip().lower()
    if lowered in ("application/json", "application/problem+json"):
        return "json"
    if lowered == "application/x-ndjson":
        return "ndjson"
    if lowered == "text/plain" or lowered.startswith("text/"):
        return "text"
    return "binary"


def format_artifact_json(data: bytes, truncated: bool) -> str:
    """Display text for one ``application/json`` artifact: pretty when possible.

    Formatted means pretty-printed JSON; a truncated member or invalid JSON
    falls back to the raw text, like the legacy Evidence preview. Never
    raises on garbage input.
    """
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        return ""
    if truncated:
        return text
    try:
        parsed = json.loads(text)
    except ValueError:
        return text
    try:
        return json.dumps(parsed, indent=2)
    except (ValueError, TypeError):
        return text


def split_ndjson_rows(data: bytes) -> list[str]:
    """One row per line of an ``application/x-ndjson`` artifact, as read.

    Never raises: undecodable bytes read with replacement characters.
    """
    if not isinstance(data, bytes):
        return []
    return data.decode("utf-8", errors="replace").splitlines()


def verify_artifact_member(data: bytes, entry: ContractManifestEntry) -> str | None:
    """Check one exported member against its manifest entry, else describe why.

    Both the size and the ``sha256`` must match: a mismatch is a torn read
    of an archive that is still moving, never a final answer. Returns
    ``None`` when the member verifies.
    """
    if not isinstance(data, bytes) or len(data) != entry.size:
        return (
            f"export file {entry.path!r} does not match its manifest size "
            f"({len(data) if isinstance(data, bytes) else 'no'} B read, "
            f"{entry.size} B listed)"
        )
    if hashlib.sha256(data).hexdigest() != entry.sha256:
        return f"export file {entry.path!r} does not match its manifest sha256"
    return None


@dataclass(frozen=True)
class VerifiedArtifact:
    """One verified ``attempts export`` member: its bytes or why they wait."""

    ok: bool
    error: str | None = None
    changing: bool = False
    data: bytes | None = None


async def fetch_verified_artifact(
    fetch: Any,
    entry: ContractManifestEntry,
    *,
    sealed: bool,
    attempt_id: str = "?",
) -> VerifiedArtifact:
    """Fetch one member through ``attempts export`` and verify it, with one retry.

    ``fetch`` is awaited for one ``attempts export --path`` exec and returns
    its stdout and return code; gateway failures propagate to the caller.
    The member is verified (size and ``sha256``) against its manifest entry.
    A mismatch on an unsealed archive is fetched once more: a second mismatch
    reports the archive as still changing (``changing`` is set), never as
    final. A mismatch on a sealed archive is an error. Never raises except
    for the gateway failures ``fetch`` itself raises.
    """
    wanted = entry.path
    first = await fetch()
    export = parse_contract_attempt_export(
        first.stdout, first.returncode, attempt_id=attempt_id, wanted=wanted
    )
    if not export.ok:
        return VerifiedArtifact(ok=False, error=export.error)
    assert export.data is not None
    if verify_artifact_member(export.data, entry) is None:
        return VerifiedArtifact(ok=True, data=export.data)
    if sealed:
        return VerifiedArtifact(
            ok=False,
            error=(
                f"Could not verify the artifact against its manifest sha256: "
                f"export file {wanted!r} does not match the sealed archive."
            ),
        )
    second = await fetch()
    retry = parse_contract_attempt_export(
        second.stdout, second.returncode, attempt_id=attempt_id, wanted=wanted
    )
    if not retry.ok:
        return VerifiedArtifact(ok=False, error=retry.error)
    assert retry.data is not None
    if verify_artifact_member(retry.data, entry) is None:
        return VerifiedArtifact(ok=True, data=retry.data)
    return VerifiedArtifact(ok=False, changing=True, error=CHANGING_MESSAGE)


def slice_artifact_preview(data: bytes) -> tuple[bytes, bool]:
    """Preview bytes for one verified member: at most the preview cap.

    Previews stop reading after 256 KiB; the caller names the manifest's true
    size beside the truncation marker.
    """
    if not isinstance(data, bytes):
        return b"", False
    if len(data) <= ARTIFACT_PREVIEW_CAP:
        return data, False
    return data[:ARTIFACT_PREVIEW_CAP], True
