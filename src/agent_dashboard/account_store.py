"""Operator account store (spec section 6.1).

One SQLite file, ``accounts.sqlite3``, next to the Control audit
(``$XDG_STATE_HOME/agent-dashboard/``, else ``~/.local/state/agent-dashboard/``).
It holds Operator accounts, sign-in sessions and Set-password links. The
directory has mode ``0700`` and the file ``0600``. Standard-library
``sqlite3`` only, no ORM. Every connection sets ``journal_mode=WAL``,
``foreign_keys=ON`` and ``busy_timeout=5000``, so the CLI and the server write
safely at the same time. ``user_version`` carries the schema version (1);
opening a newer version is an error naming the path.

Times are Unix epoch seconds (``REAL``, UTC). Every function that reads the
clock takes an optional ``now`` so tests can inject one.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .control_audit import audit_dir

STORE_FILE_NAME = "accounts.sqlite3"
SCHEMA_VERSION = 1

#: Account names (ADR 0013): lowercase start, then lowercase/digits/dashes.
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")

ROLES = ("viewer", "controller")

#: A Set-password link works for 24 hours (spec 6.3).
LINK_VALIDITY_SECONDS = 24 * 60 * 60

#: Link rows are kept for 30 days after they expire, so an old link reads as
#: expired, used or superseded rather than unknown (spec 6.1).
LINK_RETENTION_SECONDS = 30 * 24 * 60 * 60

_SCHEMA = """
CREATE TABLE accounts (
  name            TEXT PRIMARY KEY,           -- ^[a-z][a-z0-9-]{0,31}$
  role            TEXT NOT NULL CHECK (role IN ('viewer', 'controller')),
  password_hash   TEXT,                       -- argon2 PHC string; NULL until set
  created_at      REAL NOT NULL,
  password_set_at REAL,
  removed_at      REAL                        -- tombstone when set
);
CREATE TABLE sign_in_sessions (
  id_hash      BLOB PRIMARY KEY,              -- SHA-256 of the cookie value
  account      TEXT NOT NULL REFERENCES accounts(name),
  created_at   REAL NOT NULL,
  last_seen_at REAL NOT NULL
);
CREATE INDEX sign_in_sessions_account ON sign_in_sessions(account);
CREATE TABLE set_password_links (
  token_hash    BLOB PRIMARY KEY,             -- SHA-256 of the token
  account       TEXT NOT NULL REFERENCES accounts(name),
  issued_at     REAL NOT NULL,
  expires_at    REAL NOT NULL,                -- issued_at + 24 h
  used_at       REAL,
  superseded_at REAL
);
CREATE INDEX set_password_links_account ON set_password_links(account);
"""


class StoreError(Exception):
    """The accounts store cannot be opened (message names its path)."""


class UnknownAccountError(ValueError):
    """An unknown or removed account (removed accounts read as unknown)."""


class AccountExistsError(ValueError):
    """An account name that was used before; names are never reused."""


@dataclass(frozen=True)
class Account:
    """One row of the ``accounts`` table."""

    name: str
    role: str
    password_hash: str | None
    created_at: float
    password_set_at: float | None
    removed_at: float | None

    @property
    def removed(self) -> bool:
        """True once ``remove`` set the tombstone."""
        return self.removed_at is not None

    @property
    def can_sign_in(self) -> bool:
        """True when the account has a password and is not removed."""
        return self.removed_at is None and self.password_hash is not None


@dataclass(frozen=True)
class IssuedLink:
    """A freshly issued Set-password link: the plain token plus its expiry."""

    token: str
    expires_at: float


@dataclass(frozen=True)
class SetPasswordLinkDetail:
    """A link's state plus the account/expiry the page shows (spec 8.3).

    ``account`` and ``expires_at`` are set when the row names an account
    (every state except unknown); unknown links -- no row, or a removed
    account with no other state -- carry neither.
    """

    state: str
    account: str | None
    expires_at: float | None


@dataclass(frozen=True)
class SignInSession:
    """One joined sign-in session row: the session plus its account's Role.

    Returned by :func:`find_sign_in_session`; the gate turns it into an
    ``Operator`` view model. The Role is read from the account row on every
    request, never cached in the session.
    """

    account: str
    role: str
    created_at: float
    last_seen_at: float


def store_path() -> Path:
    """The store file path (the Control audit's directory plus the file name)."""
    return audit_dir() / STORE_FILE_NAME


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def open_store(
    now: float | None = None,
    *,
    idle_timeout: float | None = None,
    max_age: float | None = None,
) -> sqlite3.Connection:
    """Open the store, creating it (directory and file) when missing.

    Applies the pragmas, migrates a fresh file to schema version 1, refuses a
    newer ``user_version`` with an error naming the path, deletes
    Set-password link rows more than 30 days past expiry, and — when both
    ``idle_timeout`` and ``max_age`` are given — deletes ended sign-in
    sessions (spec 5.6). Raises ``StoreError`` when the store cannot be
    opened.
    """
    path = store_path()
    try:
        directory = path.parent
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.chmod(str(path), 0o600)
        finally:
            os.close(fd)
    except OSError as exc:
        raise StoreError(f"cannot open accounts store at {path}: {exc}") from exc
    conn = _connect(path)
    try:
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        except sqlite3.Error as exc:
            raise StoreError(f"cannot open accounts store at {path}: {exc}") from exc
        if version == 0:
            try:
                with conn:
                    conn.executescript(_SCHEMA)
                    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            except sqlite3.Error as exc:
                raise StoreError(
                    f"cannot open accounts store at {path}: {exc}"
                ) from exc
        elif version != SCHEMA_VERSION:
            raise StoreError(
                f"accounts store at {path} has schema version {version}, "
                f"newer than supported version {SCHEMA_VERSION}"
            )
        cleanup_links(conn, now)
        if idle_timeout is not None and max_age is not None:
            cleanup_ended_sessions(conn, now, idle_timeout, max_age)
    except BaseException:
        conn.close()
        raise
    return conn


def _now(now: float | None) -> float:
    return time.time() if now is None else now


def check_name(name: str) -> str:
    """Raise ``ValueError`` unless the name matches the account-name rule."""
    if not NAME_RE.match(name):
        raise ValueError(
            f"invalid account name {name!r}: must match ^[a-z][a-z0-9-]{{0,31}}$"
        )
    return name


def hash_token(token: str) -> bytes:
    """SHA-256 of a Set-password link token (what the store keeps)."""
    return hashlib.sha256(token.encode("utf-8")).digest()


def _row_to_account(row: sqlite3.Row) -> Account:
    return Account(
        name=row["name"],
        role=row["role"],
        password_hash=row["password_hash"],
        created_at=row["created_at"],
        password_set_at=row["password_set_at"],
        removed_at=row["removed_at"],
    )


def get_account(conn: sqlite3.Connection, name: str) -> Account | None:
    """The account row, or None when no account carries the name."""
    row = conn.execute("SELECT * FROM accounts WHERE name = ?", (name,)).fetchone()
    return _row_to_account(row) if row is not None else None


def _active_account(conn: sqlite3.Connection, name: str) -> Account:
    """The account, raising ``UnknownAccountError`` when unknown or removed."""
    account = get_account(conn, name)
    if account is None or account.removed:
        raise UnknownAccountError(f"unknown Operator account {name!r}")
    return account


def create_account(
    conn: sqlite3.Connection, name: str, role: str, *, now: float | None = None
) -> Account:
    """Insert a password-less account; names (even removed ones) never recur."""
    check_name(name)
    if role not in ROLES:
        raise ValueError(f"invalid role {role!r}: expected viewer or controller")
    timestamp = _now(now)
    try:
        with conn:
            conn.execute(
                "INSERT INTO accounts (name, role, created_at) VALUES (?, ?, ?)",
                (name, role, timestamp),
            )
    except sqlite3.IntegrityError:
        raise AccountExistsError(
            f"account name {name} was used before and cannot be reused"
        ) from None
    account = get_account(conn, name)
    assert account is not None
    return account


def set_role(
    conn: sqlite3.Connection, name: str, role: str
) -> tuple[Account, bool]:
    """Set an account's Role; returns the account and whether it changed.

    No sign-in session ends; the change applies from the next request.
    """
    check_name(name)
    if role not in ROLES:
        raise ValueError(f"invalid role {role!r}: expected viewer or controller")
    account = _active_account(conn, name)
    if account.role == role:
        return account, False
    with conn:
        conn.execute("UPDATE accounts SET role = ? WHERE name = ?", (role, name))
    updated = get_account(conn, name)
    assert updated is not None
    return updated, True


def delete_account_sessions(conn: sqlite3.Connection, name: str) -> int:
    """Delete all of an account's sign-in sessions, returning how many ended."""
    cursor = conn.execute("DELETE FROM sign_in_sessions WHERE account = ?", (name,))
    return cursor.rowcount


def remove_account(
    conn: sqlite3.Connection, name: str, *, now: float | None = None
) -> int:
    """Tombstone an account: no password, no sessions, name kept reserved."""
    check_name(name)
    _active_account(conn, name)
    timestamp = _now(now)
    with conn:
        ended = delete_account_sessions(conn, name)
        conn.execute(
            "UPDATE set_password_links SET superseded_at = ? "
            "WHERE account = ? AND used_at IS NULL AND superseded_at IS NULL "
            "AND expires_at > ?",
            (timestamp, name, timestamp),
        )
        conn.execute(
            "UPDATE accounts SET password_hash = NULL, removed_at = ? WHERE name = ?",
            (timestamp, name),
        )
    return ended


def _clear_password_and_sessions(conn: sqlite3.Connection, name: str) -> int:
    ended = delete_account_sessions(conn, name)
    conn.execute("UPDATE accounts SET password_hash = NULL WHERE name = ?", (name,))
    return ended


def reset_password(conn: sqlite3.Connection, name: str) -> int:
    """Delete an account's password and all its sign-in sessions."""
    check_name(name)
    _active_account(conn, name)
    with conn:
        return _clear_password_and_sessions(conn, name)


def issue_set_password_link(
    conn: sqlite3.Connection,
    name: str,
    *,
    now: float | None = None,
    token: str | None = None,
) -> IssuedLink:
    """Issue a Set-password link, superseding the account's unused ones.

    The plain token is returned once for printing; only its SHA-256 is
    stored. Supersession and insert happen in one transaction. ``token`` is
    an injection seam for tests; production passes None for a fresh
    ``secrets.token_urlsafe(32)`` (256 random bits).
    """
    check_name(name)
    _active_account(conn, name)
    timestamp = _now(now)
    plain = token if token is not None else secrets.token_urlsafe(32)
    with conn:
        _supersede_and_insert_link(conn, name, timestamp, plain)
    return IssuedLink(token=plain, expires_at=timestamp + LINK_VALIDITY_SECONDS)


def _supersede_and_insert_link(
    conn: sqlite3.Connection, name: str, timestamp: float, plain: str
) -> None:
    # Only live links are superseded: an already-expired link keeps reading
    # as expired for its 30-day trail (spec 6.1, 6.3).
    conn.execute(
        "UPDATE set_password_links SET superseded_at = ? "
        "WHERE account = ? AND used_at IS NULL AND superseded_at IS NULL "
        "AND expires_at > ?",
        (timestamp, name, timestamp),
    )
    conn.execute(
        "INSERT INTO set_password_links "
        "(token_hash, account, issued_at, expires_at) VALUES (?, ?, ?, ?)",
        (hash_token(plain), name, timestamp, timestamp + LINK_VALIDITY_SECONDS),
    )


def reset_account(
    conn: sqlite3.Connection,
    name: str,
    *,
    now: float | None = None,
    token: str | None = None,
) -> tuple[int, IssuedLink]:
    """Delete an account's password and sessions and issue a new link.

    One transaction (spec 6.4): the password, the sign-in sessions and the
    supersession of the old links change together.
    """
    check_name(name)
    _active_account(conn, name)
    timestamp = _now(now)
    plain = token if token is not None else secrets.token_urlsafe(32)
    with conn:
        ended = _clear_password_and_sessions(conn, name)
        _supersede_and_insert_link(conn, name, timestamp, plain)
    return ended, IssuedLink(
        token=plain, expires_at=timestamp + LINK_VALIDITY_SECONDS
    )


def set_password_link_state(
    conn: sqlite3.Connection, token: str, *, now: float | None = None
) -> str:
    """A token's state, checked in spec order (spec 6.3).

    ``unknown`` (no row), ``used``, ``superseded``, ``expired``, a removed
    account (read as ``unknown``), then ``valid``.
    """
    timestamp = _now(now)
    row = conn.execute(
        "SELECT * FROM set_password_links WHERE token_hash = ?",
        (hash_token(token),),
    ).fetchone()
    if row is None:
        return "unknown"
    if row["used_at"] is not None:
        return "used"
    if row["superseded_at"] is not None:
        return "superseded"
    if timestamp > row["expires_at"]:
        return "expired"
    account = get_account(conn, row["account"])
    if account is None or account.removed:
        return "unknown"
    return "valid"


def describe_set_password_link(
    conn: sqlite3.Connection, token: str, *, now: float | None = None
) -> SetPasswordLinkDetail:
    """A link's state plus the account/expiry the ``/set-password`` page shows.

    The state follows the spec order of :func:`set_password_link_state`
    (unknown, used, superseded, expired, removed-as-unknown, valid).
    ``account``/``expires_at`` are set for every state with a row naming a
    live account detail -- that is, everything except unknown (no row, or a
    removed account with no other state, shows no account name, spec 8.3).
    """
    timestamp = _now(now)
    row = conn.execute(
        "SELECT * FROM set_password_links WHERE token_hash = ?",
        (hash_token(token),),
    ).fetchone()
    if row is None:
        return SetPasswordLinkDetail(state="unknown", account=None, expires_at=None)
    if row["used_at"] is not None:
        return SetPasswordLinkDetail(
            state="used", account=row["account"], expires_at=row["expires_at"]
        )
    if row["superseded_at"] is not None:
        return SetPasswordLinkDetail(
            state="superseded", account=row["account"], expires_at=row["expires_at"]
        )
    if timestamp > row["expires_at"]:
        return SetPasswordLinkDetail(
            state="expired", account=row["account"], expires_at=row["expires_at"]
        )
    account = get_account(conn, row["account"])
    if account is None or account.removed:
        return SetPasswordLinkDetail(state="unknown", account=None, expires_at=None)
    return SetPasswordLinkDetail(
        state="valid", account=row["account"], expires_at=row["expires_at"]
    )


def consume_set_password_link(
    conn: sqlite3.Connection,
    token: str,
    password_hash: str,
    *,
    now: float | None = None,
) -> SetPasswordLinkDetail:
    """Set a password through a link in one transaction (spec 6.5).

    The link is still valid, ``password_hash`` and ``password_set_at`` are
    set, ``used_at`` is set, and all of the account's sign-in sessions are
    deleted. Returns the detail (state ``valid`` with the account); when the
    link is no longer valid nothing changes and the current detail is
    returned instead, so the caller re-renders the 410 page.
    """
    timestamp = _now(now)
    with conn:
        row = conn.execute(
            "SELECT * FROM set_password_links WHERE token_hash = ?",
            (hash_token(token),),
        ).fetchone()
        if row is None:
            return SetPasswordLinkDetail(
                state="unknown", account=None, expires_at=None
            )
        if row["used_at"] is not None:
            return SetPasswordLinkDetail(
                state="used", account=row["account"], expires_at=row["expires_at"]
            )
        if row["superseded_at"] is not None:
            return SetPasswordLinkDetail(
                state="superseded",
                account=row["account"],
                expires_at=row["expires_at"],
            )
        if timestamp > row["expires_at"]:
            return SetPasswordLinkDetail(
                state="expired",
                account=row["account"],
                expires_at=row["expires_at"],
            )
        account = get_account(conn, row["account"])
        if account is None or account.removed:
            return SetPasswordLinkDetail(
                state="unknown", account=None, expires_at=None
            )
        conn.execute(
            "UPDATE accounts SET password_hash = ?, password_set_at = ?"
            " WHERE name = ?",
            (password_hash, timestamp, row["account"]),
        )
        conn.execute(
            "UPDATE set_password_links SET used_at = ? WHERE token_hash = ?",
            (timestamp, hash_token(token)),
        )
        conn.execute(
            "DELETE FROM sign_in_sessions WHERE account = ?", (row["account"],)
        )
        return SetPasswordLinkDetail(
            state="valid", account=row["account"], expires_at=row["expires_at"]
        )


def any_account_can_sign_in(conn: sqlite3.Connection) -> bool:
    """True when at least one account can sign in (spec 2.3).

    An account can sign in when it is not removed and has a password set.
    """
    row = conn.execute(
        "SELECT 1 FROM accounts "
        "WHERE removed_at IS NULL AND password_hash IS NOT NULL LIMIT 1"
    ).fetchone()
    return row is not None


def active_controllers(conn: sqlite3.Connection) -> list[Account]:
    """Accounts that can sign in with the ``controller`` Role."""
    return [
        _row_to_account(row)
        for row in conn.execute(
            "SELECT * FROM accounts WHERE role = 'controller' "
            "AND removed_at IS NULL AND password_hash IS NOT NULL"
        ).fetchall()
    ]


def list_accounts(conn: sqlite3.Connection) -> list[Account]:
    """All accounts, removed ones last, then by name."""
    rows = conn.execute("SELECT * FROM accounts").fetchall()
    accounts = [_row_to_account(row) for row in rows]
    accounts.sort(key=lambda a: (a.removed, a.name))
    return accounts


def count_account_sessions(conn: sqlite3.Connection, name: str) -> int:
    """The account's open sign-in session rows (ended ones are deleted)."""
    row = conn.execute(
        "SELECT COUNT(*) FROM sign_in_sessions WHERE account = ?", (name,)
    ).fetchone()
    return int(row[0])


def create_sign_in_session(
    conn: sqlite3.Connection,
    account: str,
    id_hash: bytes,
    *,
    now: float | None = None,
) -> None:
    """Insert one sign-in session row for an account (spec 5.3).

    The caller holds the session ID; only its SHA-256 (``id_hash``) is
    stored. ``created_at`` and ``last_seen_at`` both start at ``now``.
    """
    timestamp = _now(now)
    with conn:
        conn.execute(
            "INSERT INTO sign_in_sessions "
            "(id_hash, account, created_at, last_seen_at) VALUES (?, ?, ?, ?)",
            (id_hash, account, timestamp, timestamp),
        )


def delete_sign_in_session(conn: sqlite3.Connection, id_hash: bytes) -> int:
    """Delete one sign-in session row; returns how many rows ended."""
    with conn:
        cursor = conn.execute(
            "DELETE FROM sign_in_sessions WHERE id_hash = ?", (id_hash,)
        )
    return cursor.rowcount


def find_sign_in_session(
    conn: sqlite3.Connection,
    id_hash: bytes,
    *,
    now: float | None = None,
    idle_timeout: float,
    max_age: float,
) -> SignInSession | None:
    """Look one sign-in session up by the cookie's SHA-256 (spec 6.2).

    One query joins ``sign_in_sessions`` to ``accounts``. The row is
    accepted only when the account can sign in (a password set, not
    removed) and the session has not ended (spec 5.6): past
    ``last_seen_at + idle_timeout`` or past ``created_at + max_age``.
    Ended rows are deleted when found. Activity refreshes ``last_seen_at``
    at most once a minute per session.
    """
    timestamp = _now(now)
    row = conn.execute(
        "SELECT s.account, s.created_at, s.last_seen_at,"
        " a.role, a.password_hash, a.removed_at"
        " FROM sign_in_sessions s JOIN accounts a ON s.account = a.name"
        " WHERE s.id_hash = ?",
        (id_hash,),
    ).fetchone()
    if row is None:
        return None
    if row["removed_at"] is not None or row["password_hash"] is None:
        # A reset, removed or password-less account's session is refused at
        # once; the Role is read from the account row on every request.
        return None
    if (
        timestamp > row["created_at"] + max_age
        or timestamp > row["last_seen_at"] + idle_timeout
    ):
        with conn:
            conn.execute(
                "DELETE FROM sign_in_sessions WHERE id_hash = ?", (id_hash,)
            )
        return None
    if timestamp - row["last_seen_at"] >= 60:
        with conn:
            conn.execute(
                "UPDATE sign_in_sessions SET last_seen_at = ? WHERE id_hash = ?",
                (timestamp, id_hash),
            )
    return SignInSession(
        account=row["account"],
        role=row["role"],
        created_at=row["created_at"],
        last_seen_at=row["last_seen_at"],
    )


def cleanup_links(conn: sqlite3.Connection, now: float | None = None) -> int:
    """Delete link rows more than 30 days past expiry; returns rows deleted."""
    cutoff = _now(now) - LINK_RETENTION_SECONDS
    with conn:
        cursor = conn.execute(
            "DELETE FROM set_password_links WHERE expires_at < ?", (cutoff,)
        )
    return cursor.rowcount


def cleanup_ended_sessions(
    conn: sqlite3.Connection,
    now: float | None,
    idle_timeout: float,
    max_age: float,
) -> int:
    """Delete ended sign-in sessions (spec 5.6); returns rows deleted.

    A session has ended when ``now`` is past ``last_seen_at + idle_timeout``
    or past ``created_at + max_age`` — the same rule
    :func:`find_sign_in_session` uses for a single row. Run when the store
    opens in LAN mode, so a restart never keeps an ended session.
    """
    timestamp = _now(now)
    with conn:
        cursor = conn.execute(
            "DELETE FROM sign_in_sessions "
            "WHERE ? > last_seen_at + ? OR ? > created_at + ?",
            (timestamp, idle_timeout, timestamp, max_age),
        )
    return cursor.rowcount


__all__ = [
    "LINK_RETENTION_SECONDS",
    "LINK_VALIDITY_SECONDS",
    "ROLES",
    "SCHEMA_VERSION",
    "STORE_FILE_NAME",
    "Account",
    "AccountExistsError",
    "IssuedLink",
    "SetPasswordLinkDetail",
    "SignInSession",
    "StoreError",
    "UnknownAccountError",
    "active_controllers",
    "any_account_can_sign_in",
    "check_name",
    "cleanup_ended_sessions",
    "cleanup_links",
    "consume_set_password_link",
    "count_account_sessions",
    "create_account",
    "create_sign_in_session",
    "delete_account_sessions",
    "delete_sign_in_session",
    "describe_set_password_link",
    "find_sign_in_session",
    "get_account",
    "hash_token",
    "issue_set_password_link",
    "list_accounts",
    "open_store",
    "remove_account",
    "reset_account",
    "reset_password",
    "set_password_link_state",
    "set_role",
    "store_path",
]
