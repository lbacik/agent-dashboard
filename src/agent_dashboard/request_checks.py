"""Shared request checks: the Origin rule and the client address (spec 4.2, 4.4).

The Origin rule is used by every state-changing POST: the Control routes
and (once sign-in lands) ``POST /login``, ``POST /logout`` and
``POST /set-password``. ``X-Forwarded-Host`` and ``X-Forwarded-Proto`` are
never read.
"""

from __future__ import annotations

import ipaddress
import json
import sys
from urllib.parse import urlsplit

#: Default ports per Origin scheme (spec 4.2).
DEFAULT_PORTS = {"http": 80, "https": 443}


def is_origin_allowed(origin: str | None, host_header: str | None) -> bool:
    """Whether an ``Origin`` header value matches the ``Host`` header.

    ``Origin`` must be present and parse as ``scheme://host[:port]`` with
    scheme ``http`` or ``https``; ``null`` or anything else is refused. The
    host compares case-insensitively (IPv6 in brackets compared without
    them); a missing port on either side means the Origin scheme's default
    (80 for ``http``, 443 for ``https``).
    """
    if origin is None:
        return False
    text = origin.strip()
    if not text or text.lower() == "null":
        return False
    try:
        parts = urlsplit(text)
    except ValueError:
        return False
    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS:
        return False
    if parts.username or parts.password:
        return False
    if parts.query or parts.fragment:
        return False
    if parts.path not in ("", "/"):
        return False
    origin_host = parts.hostname
    if not origin_host:
        return False
    try:
        origin_port = parts.port
    except ValueError:
        return False
    default = DEFAULT_PORTS[scheme]
    if origin_port is None:
        origin_port = default
    elif not 1 <= origin_port <= 65535:
        return False

    if host_header is None:
        return False
    raw = host_header.strip()
    if not raw:
        return False
    if raw.startswith("["):
        end = raw.find("]")
        if end == -1:
            return False
        host_name = raw[1:end].lower()
        if not host_name:
            return False
        rest = raw[end + 1 :]
        if rest == "":
            host_port: int | None = None
        elif rest.startswith(":"):
            port_text = rest[1:]
            if not port_text.isdigit():
                return False
            host_port = int(port_text)
            if not 1 <= host_port <= 65535:
                return False
        else:
            return False
    elif raw.count(":") > 1:
        # Unbracketed IPv6: cannot carry a port, refuse.
        return False
    elif ":" in raw:
        name, _, port_text = raw.rpartition(":")
        host_name = name.strip().lower()
        if not host_name or "[" in host_name or "]" in host_name:
            return False
        if not port_text.isdigit():
            return False
        host_port = int(port_text)
        if not 1 <= host_port <= 65535:
            return False
    else:
        host_name = raw.lower()
        if not host_name or "[" in host_name or "]" in host_name:
            return False
        host_port = None
    if origin_host.lower() != host_name:
        return False
    if host_port is None:
        # The Host header's missing port takes the Origin scheme's default.
        host_port = default
    return origin_port == host_port


def log_origin_refused(request, *, reason: str = "origin") -> None:
    """Write one stdout ``auth`` line for a refused Origin (spec 9)."""
    from .control_audit import utc_now_ms

    payload = {
        "at": utc_now_ms(),
        "event": "origin_refused",
        "path": request.url.path,
        "origin": request.headers.get("origin") or "",
        "host": request.headers.get("host") or "",
        "fetch_site": request.headers.get("sec-fetch-site") or "",
        "reason": reason,
    }
    print(f"auth {json.dumps(payload, separators=(',', ':'))}", flush=True)
    sys.stdout.flush()


def client_address(
    peer: str, x_forwarded_for: str | None, trusted_proxies
) -> str:
    """Resolve the client address from the peer and ``X-Forwarded-For``.

    When the peer address is in ``trusted_proxies`` and ``X-Forwarded-For``
    is present, walk its comma-separated entries from the right and take
    the first that is not in ``trusted_proxies``. An entry that is not an
    IP address ends the walk and the peer address is used. If every entry
    is trusted, the left-most one is used. Otherwise the peer address is
    used and ``X-Forwarded-For`` is ignored.
    """
    if not x_forwarded_for:
        return peer
    try:
        peer_ip = ipaddress.ip_address(peer.strip())
    except ValueError:
        return peer
    try:
        trusted = tuple(trusted_proxies) if trusted_proxies else ()
    except TypeError:
        return peer
    if not any(peer_ip in net for net in trusted):
        return peer
    entries = [entry.strip() for entry in x_forwarded_for.split(",")]
    for entry in reversed(entries):
        if not entry:
            return peer
        try:
            address = ipaddress.ip_address(entry)
        except ValueError:
            return peer
        if any(address in net for net in trusted):
            continue
        return entry
    return entries[0]


__all__ = [
    "DEFAULT_PORTS",
    "client_address",
    "is_origin_allowed",
    "log_origin_refused",
]
