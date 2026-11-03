"""``agent-dashboard account …``: manage Operator accounts (spec section 6.4).

The host operator runs these in either mode, against the store the server
will later read. ``agent-dashboard`` without a subcommand still serves; the
account subcommands take no serve flags. Every failure prints
``agent-dashboard: error: <message>`` to stderr and exits 1; usage errors
exit 2 through argparse.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
import urllib.parse
from datetime import UTC, datetime

from . import account_store
from .account_store import IssuedLink, StoreError, UnknownAccountError

LAST_CONTROLLER_WARNING = (
    "Warning: no active account has the controller Role now, so Control "
    "actions are unavailable to everyone. Grant it again with: "
    "agent-dashboard account role {name} controller"
)

EMPTY_LIST_LINE = (
    "No Operator accounts. Create one with: "
    "agent-dashboard account add <name> --role controller"
)


def _now() -> float:
    """Current time; a seam tests monkeypatch for a fixed clock."""
    return time.time()


def normalize_base_url(value: str) -> str:
    """Validate ``--url`` and drop a trailing ``/``.

    Accepts ``http(s)://host[:port]`` with an optional path prefix; raises
    ``ValueError`` for anything else.
    """
    if not value or any(ch.isspace() for ch in value):
        raise ValueError(
            f"invalid --url {value!r}: "
            "expected http(s)://host[:port] with an optional path prefix"
        )
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        raise ValueError(
            f"invalid --url {value!r}: "
            "expected http(s)://host[:port] with an optional path prefix"
        ) from None
    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"invalid --url {value!r}: "
            "expected http(s)://host[:port] with an optional path prefix"
        )
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            f"invalid --url {value!r}: "
            "expected http(s)://host[:port] with an optional path prefix"
        )
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host
    try:
        port = parsed.port
    except ValueError:
        raise ValueError(
            f"invalid --url {value!r}: "
            "expected http(s)://host[:port] with an optional path prefix"
        ) from None
    if port is not None:
        netloc = f"{netloc}:{port}"
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{netloc}{path}"


def format_moment(timestamp: float) -> str:
    """A store time as ``YYYY-MM-DD HH:MM`` UTC (spec 6.4)."""
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%d %H:%M")


def format_link_block(name: str, link: IssuedLink, base: str | None) -> str:
    """The Set-password link block printed by ``add`` and ``reset``."""
    target = (
        f"{base}/set-password?token={link.token}"
        if base is not None
        else f"/set-password?token={link.token}"
    )
    return (
        f"Set-password link for {name} (works once, "
        f"until {format_moment(link.expires_at)} UTC):\n"
        f"  {target}\n"
        f"Send it to {name} only. Open it through the dashboard's https:// "
        "address, or an SSH tunnel to localhost."
    )


def account_state(
    conn: sqlite3.Connection, account: account_store.Account, now: float
) -> str:
    """The ``STATE`` column value for ``account list`` (spec 6.4)."""
    if account.removed:
        return "removed"
    if account.password_hash is not None:
        return "active"
    pending = conn.execute(
        "SELECT 1 FROM set_password_links WHERE account = ? "
        "AND used_at IS NULL AND superseded_at IS NULL AND expires_at >= ?",
        (account.name, now),
    ).fetchone()
    return "link pending" if pending is not None else "no password"


def _fail(message: str) -> int:
    print(f"agent-dashboard: error: {message}", file=sys.stderr)
    return 1


def _warn_last_controller(name: str) -> None:
    print(LAST_CONTROLLER_WARNING.format(name=name), file=sys.stderr)


def cmd_add(
    conn: sqlite3.Connection,
    name: str,
    role: str,
    base: str | None,
    now: float,
) -> int:
    try:
        account = account_store.create_account(conn, name, role, now=now)
    except ValueError as exc:
        return _fail(str(exc))
    link = account_store.issue_set_password_link(conn, name, now=now)
    print(f"Created Operator account {account.name} ({account.role}).")
    print(format_link_block(name, link, base))
    return 0


def cmd_reset(
    conn: sqlite3.Connection, name: str, base: str | None, now: float
) -> int:
    try:
        ended, link = account_store.reset_account(conn, name, now=now)
    except (ValueError, UnknownAccountError) as exc:
        return _fail(str(exc))
    print(f"Reset {name}: password deleted, {ended} sign-in sessions ended.")
    print(format_link_block(name, link, base))
    return 0


def cmd_role(conn: sqlite3.Connection, name: str, role: str) -> int:
    try:
        account, changed = account_store.set_role(conn, name, role)
    except (ValueError, UnknownAccountError) as exc:
        return _fail(str(exc))
    if changed:
        print(f"{name} is now {role}.")
    else:
        print(f"{name} is already {role}.")
    if role == "viewer" and not account_store.active_controllers(conn):
        _warn_last_controller(name)
    return 0


def cmd_remove(conn: sqlite3.Connection, name: str, now: float) -> int:
    try:
        ended = account_store.remove_account(conn, name, now=now)
    except (ValueError, UnknownAccountError) as exc:
        return _fail(str(exc))
    print(f"Removed {name}: {ended} sign-in sessions ended. The name stays reserved.")
    if not account_store.active_controllers(conn):
        _warn_last_controller(name)
    return 0


def cmd_list(conn: sqlite3.Connection, now: float) -> int:
    accounts = account_store.list_accounts(conn)
    if not accounts:
        print(EMPTY_LIST_LINE)
        return 0
    headers = (
        "NAME",
        "ROLE",
        "STATE",
        "CREATED",
        "PASSWORD SET",
        "REMOVED",
        "SIGN-IN SESSIONS",
    )
    rows = [
        (
            account.name,
            account.role,
            account_state(conn, account, now),
            format_moment(account.created_at),
            format_moment(account.password_set_at)
            if account.password_set_at is not None
            else "-",
            format_moment(account.removed_at)
            if account.removed_at is not None
            else "-",
            str(account_store.count_account_sessions(conn, account.name)),
        )
        for account in accounts
    ]
    widths = [
        max(len(headers[i]), max(len(row[i]) for row in rows)) for i in range(len(headers))
    ]
    print("  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip())
    for row in rows:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    return 0


def build_account_parser() -> argparse.ArgumentParser:
    """Argument parser for ``agent-dashboard account …`` (no serve flags)."""
    parser = argparse.ArgumentParser(prog="agent-dashboard account")
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="Create an Operator account.")
    add.add_argument("name")
    add.add_argument("--role", choices=("viewer", "controller"), default="viewer")
    add.add_argument("--url", default=None)

    reset = sub.add_parser("reset", help="Delete a password, issue a new link.")
    reset.add_argument("name")
    reset.add_argument("--url", default=None)

    role = sub.add_parser("role", help="Set an account's Role.")
    role.add_argument("name")
    role.add_argument("role", choices=("viewer", "controller"))

    remove = sub.add_parser("remove", help="Tombstone an account.")
    remove.add_argument("name")

    sub.add_parser("list", help="List all accounts, removed ones last.")
    return parser


def run_account(argv: list[str]) -> int:
    """Run ``agent-dashboard account …``; 0 on success, 1 on error.

    Usage errors exit 2 through argparse. ``argv`` is the words after
    ``account``.
    """
    args = build_account_parser().parse_args(argv)
    base: str | None = None
    if getattr(args, "url", None) is not None:
        try:
            base = normalize_base_url(args.url)
        except ValueError as exc:
            return _fail(str(exc))
    now = _now()
    try:
        conn = account_store.open_store(now=now)
    except StoreError as exc:
        return _fail(str(exc))
    try:
        if args.command == "add":
            return cmd_add(conn, args.name, args.role, base, now)
        if args.command == "reset":
            return cmd_reset(conn, args.name, base, now)
        if args.command == "role":
            return cmd_role(conn, args.name, args.role)
        if args.command == "remove":
            return cmd_remove(conn, args.name, now)
        if args.command == "list":
            return cmd_list(conn, now)
    finally:
        conn.close()
    return _fail(f"unknown account command {args.command!r}")


__all__ = [
    "EMPTY_LIST_LINE",
    "LAST_CONTROLLER_WARNING",
    "account_state",
    "build_account_parser",
    "cmd_add",
    "cmd_list",
    "cmd_remove",
    "cmd_reset",
    "cmd_role",
    "format_link_block",
    "format_moment",
    "normalize_base_url",
    "run_account",
]
