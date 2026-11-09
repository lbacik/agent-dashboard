"""Classification of contract ``agentctl`` replies (contract spec sections 4, 8.4).

A pure entry point for the contract path: any finished exec (an exit code plus
raw stdout) maps to one consumer class. The decision follows the contract:
``error.code`` decides whenever stdout is a coherent producer envelope, and
the exit code is only the fallback for incoherent stdout (including the
Docker 125-127 transport failures, which carry no envelope and are ambiguous
for a mutation: the same request ID is safe to retry).

Only the library's pure parts are used here (envelope validation, the
error-code table, the contract major, request-ID generation and validation,
capability mapping). Execution, timeouts, kill-and-reap and concurrency stay
in the gateway's async runner; this module never spawns anything.

Kinds:

- ``success``: a coherent ``ok:true`` envelope with exit 0.
- ``replay``: ``ok:true`` with ``result.replayed`` true (same request ID and
  payload seen before; nothing new happened).
- ``rejection``: a definitive refusal (``rejected``, ``repository_mismatch``,
  ``payload_mismatch``, ``invalid_argument``, ``usage``, or ``internal`` with
  ``details.stored: false``). Nothing was stored.
- ``unsupported``: the instance does not advertise the operation/capability.
- ``contract_mismatch``: the consumer's ``--contract`` major is not the
  producer's. Nothing executed.
- ``not_found``: unknown request ID, Attempt or archive path.
- ``ambiguous``: the outcome is unknown and the same request ID may be
  retried (``agent_unreachable``, ``internal`` without ``stored: false``,
  malformed stdout, exit-code contradictions, transport failures, timeouts).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from agent_operator_contract import CONTRACT_MAJOR, client, documents, shared

SUCCESS = "success"
"""A coherent ``ok:true`` producer answer."""

REPLAY = "replay"
"""``ok:true`` with ``result.replayed`` true: the same request ID and payload."""

REJECTION = "rejection"
"""A definitive refusal; nothing was stored."""

UNSUPPORTED = "unsupported"
"""The operation or capability is not advertised by this instance."""

CONTRACT_MISMATCH = "contract_mismatch"
"""The consumer's contract major is not the producer's; nothing executed."""

NOT_FOUND = "not_found"
"""Unknown request ID, Attempt or archive path."""

AMBIGUOUS = "ambiguous"
"""The outcome is unknown; retrying with the same request ID is safe."""

CONTRACT_CLASSES = frozenset(
    {
        SUCCESS,
        REPLAY,
        REJECTION,
        UNSUPPORTED,
        CONTRACT_MISMATCH,
        NOT_FOUND,
        AMBIGUOUS,
    }
)
"""Every class :func:`classify_contract_reply` can return."""

_REQUEST_ID_RE = re.compile(documents.REQUEST_ID_PATTERN)
"""Contract request IDs use the schema's ``pattern`` semantics (``re.search``)."""


def _decode(data: Any) -> str:
    """Producer stdout as text; anything undecodable reads as unparsable."""
    if isinstance(data, str):
        return data
    if isinstance(data, (bytes, bytearray, memoryview)):
        return bytes(data).decode("utf-8")
    return ""


def _classify(exit_code: Any, stdout: Any) -> str:
    """Map one finished exec to its class; raises on anything incoherent."""
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        raise ValueError(f"no exit code: {exit_code!r}")
    doc = json.loads(_decode(stdout))
    if not isinstance(doc, dict) or documents.validate_envelope(doc) != []:
        raise ValueError("stdout is not a valid envelope")
    if documents.contract_major_of(doc.get("contract")) != str(CONTRACT_MAJOR):
        raise ValueError(f"envelope contract {doc.get('contract')!r} is not major 1")
    if doc.get("ok") is True:
        if exit_code != 0:
            raise ValueError(f"ok:true with exit code {exit_code}")
        result = doc.get("result")
        if not isinstance(result, dict):
            raise ValueError("ok:true envelope holds no result object")
        return REPLAY if result.get("replayed") is True else SUCCESS
    error = doc.get("error")
    if not isinstance(error, dict):
        raise ValueError("ok:false envelope holds no error object")
    code = error.get("code")
    entry = documents.error_table().get(code)
    if not isinstance(entry, dict) or exit_code != entry.get("exit_code"):
        raise ValueError(f"error.code {code!r} contradicts exit code {exit_code}")
    if code == "unsupported":
        return UNSUPPORTED
    if code == "contract_mismatch":
        return CONTRACT_MISMATCH
    if code == "not_found":
        return NOT_FOUND
    if code == "agent_unreachable":
        return AMBIGUOUS
    if code == "internal":
        details = error.get("details")
        if isinstance(details, dict) and details.get("stored") is False:
            return REJECTION
        return AMBIGUOUS
    return REJECTION


def classify_contract_reply(exit_code: Any, stdout: Any) -> str:
    """Map any contract reply to its consumer class; never raises.

    ``error.code`` decides for coherent envelopes; the exit code is only the
    fallback for malformed stdout and Docker transport failures (125-127),
    which read as ``ambiguous``. A missing exit code (a timeout, a missing
    Docker CLI) is likewise ``ambiguous``: the outcome is unknown and the
    same request ID may be retried.
    """
    try:
        return _classify(exit_code, stdout)
    except Exception:
        return AMBIGUOUS


def new_contract_request_id() -> str:
    """Generate one consumer request ID for a contract mutation (spec 6.2)."""
    return client.new_request_id()


def validate_contract_request_id(request_id: str) -> str:
    """Return the request ID when it fits the contract pattern, else raise.

    Anything else (empty, wrong alphabet, too long, sign, whitespace, a flag
    shape) raises :class:`ValueError` before any argv exists.
    """
    if not isinstance(request_id, str) or _REQUEST_ID_RE.search(request_id) is None:
        raise ValueError(f"invalid contract request id: {request_id!r}")
    return request_id


def required_capability(
    operation: str, arguments: Mapping[str, Any] | None = None
) -> str | None:
    """Return the optional capability an operation needs, or none for core.

    Delegates to the library's argv-grammar mapping (``stop`` needs
    ``stop-after`` only with ``--after``; anything else maps to its fixed
    capability). A non-string operation reads as core (none) rather than
    raising; the producer remains the authority on unknown operations.
    """
    if not isinstance(operation, str):
        return None
    try:
        return shared.required_capability(operation, dict(arguments or {}))
    except Exception:
        return None


def _as_text(value: Any) -> str | None:
    """A stripped non-empty string, else none (never raises)."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _as_sequence(value: Any) -> int | None:
    """An integer sequence >= 0, else none (never raises)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    return None


def describe_contract_reply(exit_code: Any, stdout: Any) -> dict[str, Any]:
    """Parsed fields of one contract reply plus its consumer class.

    Never raises: anything incoherent reads as ``stored: None`` with empty
    fields and the class from :func:`classify_contract_reply` (usually
    ``ambiguous``). Keys: ``class``, ``error_code``, ``acknowledgement``,
    ``replayed``, ``sequence``, ``detail``, ``repository``, ``request_id``,
    ``stored`` and ``message``. ``stored`` is the ``details.stored`` flag
    for ``internal`` errors (none otherwise); ``message`` is the error
    message or the command detail for success.
    """
    cls = classify_contract_reply(exit_code, stdout)
    detail: dict[str, Any] = {
        "class": cls,
        "error_code": None,
        "acknowledgement": None,
        "replayed": None,
        "sequence": None,
        "detail": None,
        "repository": None,
        "request_id": None,
        "stored": None,
        "message": "",
    }
    try:
        text = _decode(stdout)
        doc = json.loads(text)
    except Exception:
        return detail
    if not isinstance(doc, dict):
        return detail
    if doc.get("ok") is True:
        result = doc.get("result")
        if not isinstance(result, dict):
            return detail
        replayed = result.get("replayed")
        detail["replayed"] = replayed if isinstance(replayed, bool) else None
        command = result.get("command")
        if not isinstance(command, dict):
            return detail
        acknowledgement = command.get("acknowledgement")
        detail["acknowledgement"] = (
            acknowledgement if isinstance(acknowledgement, str) else None
        )
        detail["sequence"] = _as_sequence(command.get("sequence"))
        detail["detail"] = _as_text(command.get("detail"))
        detail["repository"] = _as_text(command.get("repository"))
        detail["request_id"] = _as_text(command.get("request_id"))
        detail["message"] = detail["detail"] or ""
        return detail
    error = doc.get("error")
    if not isinstance(error, dict):
        return detail
    code = error.get("code")
    detail["error_code"] = code if isinstance(code, str) else None
    detail["message"] = _as_text(error.get("message")) or ""
    detail["request_id"] = _as_text(error.get("request_id"))
    details = error.get("details")
    if isinstance(details, dict):
        stored = details.get("stored")
        detail["stored"] = stored if isinstance(stored, bool) else None
        if detail["error_code"] in ("repository_mismatch",):
            detail["repository"] = _as_text(details.get("repository"))
    return detail
