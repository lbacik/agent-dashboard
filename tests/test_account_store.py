"""Operator account store: file, schema, links, tombstone (spec 6.1, 6.3)."""

from __future__ import annotations

import os
import sqlite3
import stat
import time

import pytest

from agent_dashboard import account_store
from agent_dashboard.account_store import (
    LINK_VALIDITY_SECONDS,
    SCHEMA_VERSION,
    STORE_FILE_NAME,
    AccountExistsError,
    StoreError,
    UnknownAccountError,
    active_controllers,
    cleanup_links,
    count_account_sessions,
    create_account,
    get_account,
    issue_set_password_link,
    list_accounts,
    open_store,
    remove_account,
    reset_account,
    reset_password,
    set_password_link_state,
    set_role,
    store_path,
)


@pytest.fixture()
def isolated_state(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture()
def conn(isolated_state):
    connection = open_store(now=1_000_000.0)
    yield connection
    connection.close()


def add_session(conn, account, ident=b"\x01" * 32, now=1_000_000.0):
    conn.execute(
        "INSERT INTO sign_in_sessions (id_hash, account, created_at, last_seen_at)"
        " VALUES (?, ?, ?, ?)",
        (ident, account, now, now),
    )
    conn.commit()


def give_password(conn, account, digest="hashed"):
    conn.execute(
        "UPDATE accounts SET password_hash = ?, password_set_at = ? WHERE name = ?",
        (digest, 1_000_100.0, account),
    )
    conn.commit()


# --- file, directory and pragmas (spec 6.1) ----------------------------------


def test_store_lives_next_to_the_control_audit(isolated_state):
    open_store(now=1_000_000.0).close()
    assert store_path() == isolated_state / "agent-dashboard" / STORE_FILE_NAME


def test_directory_is_0700_and_file_is_0600(isolated_state):
    open_store(now=1_000_000.0).close()
    assert stat.S_IMODE(os.stat(store_path().parent).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(store_path()).st_mode) == 0o600


def test_modes_repaired_on_reopen(isolated_state):
    open_store(now=1_000_000.0).close()
    os.chmod(store_path().parent, 0o755)
    os.chmod(store_path(), 0o644)
    open_store(now=1_000_000.0).close()
    assert stat.S_IMODE(os.stat(store_path().parent).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(store_path()).st_mode) == 0o600


def test_pragmas_on_every_connection(isolated_state):
    conn = open_store(now=1_000_000.0)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        conn.close()


def test_schema_version_is_1(isolated_state):
    conn = open_store(now=1_000_000.0)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 1
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {"accounts", "sign_in_sessions", "set_password_links"} <= tables
    finally:
        conn.close()


def test_newer_user_version_refuses_open_naming_path(isolated_state):
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(str(path))
    raw.execute("CREATE TABLE t (x TEXT)")
    raw.execute("PRAGMA user_version=2")
    raw.commit()
    raw.close()
    with pytest.raises(StoreError) as exc:
        open_store(now=1_000_000.0)
    assert str(path) in str(exc.value)


def test_unopenable_store_names_path(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "agent-dashboard").mkdir()
    (tmp_path / "state" / "agent-dashboard" / STORE_FILE_NAME).mkdir()
    with pytest.raises(StoreError) as exc:
        open_store(now=1_000_000.0)
    assert STORE_FILE_NAME in str(exc.value)


# --- accounts -----------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["alice", "a", "bob-1", "x" + "y" * 31, "a-b-c-0"]
)
def test_valid_names(conn, name):
    assert create_account(conn, name, "viewer", now=1_000_000.0).name == name


@pytest.mark.parametrize(
    "name",
    ["", "Alice", "ALICE", "0abc", "-abc", "a_b", "a b", "a.b", "a/b",
     "x" + "y" * 32, "éclair", "a" * 33],
)
def test_invalid_names_rejected(conn, name):
    with pytest.raises(ValueError, match="invalid account name"):
        create_account(conn, name, "viewer", now=1_000_000.0)


def test_default_role_and_passwordless(conn):
    account = create_account(conn, "alice", "viewer", now=1_000_000.0)
    assert account.role == "viewer"
    assert account.password_hash is None
    assert account.created_at == 1_000_000.0
    assert account.password_set_at is None
    assert account.removed_at is None
    assert account.can_sign_in is False


def test_invalid_role_rejected(conn):
    with pytest.raises(ValueError, match="invalid role"):
        create_account(conn, "alice", "admin", now=1_000_000.0)


def test_set_role_change_and_idempotent(conn):
    create_account(conn, "alice", "viewer", now=1_000_000.0)
    account, changed = set_role(conn, "alice", "controller")
    assert (account.role, changed) == ("controller", True)
    account, changed = set_role(conn, "alice", "controller")
    assert (account.role, changed) == ("controller", False)


def test_set_role_unknown_or_removed(conn):
    with pytest.raises(UnknownAccountError):
        set_role(conn, "ghost", "viewer")
    create_account(conn, "alice", "viewer", now=1_000_000.0)
    remove_account(conn, "alice", now=1_000_000.0)
    with pytest.raises(UnknownAccountError):
        set_role(conn, "alice", "viewer")


# --- tombstone (spec 6.3/6.4, ADR 0013) ----------------------------------------


def test_removed_name_cannot_be_reused(conn):
    create_account(conn, "alice", "viewer", now=1_000_000.0)
    remove_account(conn, "alice", now=1_000_100.0)
    with pytest.raises(AccountExistsError, match="was used before"):
        create_account(conn, "alice", "viewer", now=1_000_200.0)
    account = get_account(conn, "alice")
    assert account is not None and account.removed
    assert account.can_sign_in is False


def test_reset_role_remove_fail_on_removed(conn):
    create_account(conn, "alice", "viewer", now=1_000_000.0)
    remove_account(conn, "alice", now=1_000_100.0)
    with pytest.raises(UnknownAccountError):
        reset_password(conn, "alice")
    with pytest.raises(UnknownAccountError):
        set_role(conn, "alice", "controller")
    with pytest.raises(UnknownAccountError):
        remove_account(conn, "alice", now=1_000_200.0)


def test_reset_role_remove_fail_on_unknown(conn):
    with pytest.raises(UnknownAccountError):
        reset_password(conn, "ghost")
    with pytest.raises(UnknownAccountError):
        remove_account(conn, "ghost", now=1_000_000.0)


def test_remove_clears_password_and_ends_sessions(conn):
    create_account(conn, "alice", "viewer", now=1_000_000.0)
    give_password(conn, "alice")
    add_session(conn, "alice", b"\x01" * 32)
    add_session(conn, "alice", b"\x02" * 32)
    assert remove_account(conn, "alice", now=1_000_200.0) == 2
    account = get_account(conn, "alice")
    assert account is not None
    assert account.password_hash is None
    assert account.removed_at == 1_000_200.0
    assert count_account_sessions(conn, "alice") == 0


def test_reset_clears_password_and_ends_sessions(conn):
    create_account(conn, "alice", "viewer", now=1_000_000.0)
    give_password(conn, "alice")
    add_session(conn, "alice")
    assert reset_password(conn, "alice") == 1
    account = get_account(conn, "alice")
    assert account is not None and not account.removed
    assert account.password_hash is None
    assert count_account_sessions(conn, "alice") == 0


def test_reset_account_is_one_transaction_with_link(conn):
    create_account(conn, "alice", "viewer", now=1_000_000.0)
    give_password(conn, "alice")
    add_session(conn, "alice")
    ended, link = reset_account(conn, "alice", now=1_000_000.0, token="tok")
    assert ended == 1
    assert link.expires_at == 1_000_000.0 + LINK_VALIDITY_SECONDS
    assert set_password_link_state(conn, "tok", now=1_000_000.0) == "valid"


# --- Set-password links (spec 6.3) ----------------------------------------------


def test_issue_link_expiry_and_hash_only(conn):
    create_account(conn, "alice", "viewer", now=1_000_000.0)
    link = issue_set_password_link(conn, "alice", now=1_000_000.0, token="plain-token")
    assert link.token == "plain-token"
    assert link.expires_at == 1_000_000.0 + LINK_VALIDITY_SECONDS
    row = conn.execute("SELECT token_hash FROM set_password_links").fetchone()
    assert row["token_hash"] != b"plain-token"
    assert len(row["token_hash"]) == 32


def test_link_states_in_spec_order(conn):
    create_account(conn, "alice", "viewer", now=1_000_000.0)
    t0 = 1_000_000.0

    assert set_password_link_state(conn, "nope", now=t0) == "unknown"

    valid = issue_set_password_link(conn, "alice", now=t0, token="valid-tok")
    assert set_password_link_state(conn, valid.token, now=t0) == "valid"

    # A second link supersedes the first in the same transaction.
    newer = issue_set_password_link(conn, "alice", now=t0 + 10, token="newer-tok")
    assert set_password_link_state(conn, valid.token, now=t0 + 10) == "superseded"
    assert set_password_link_state(conn, newer.token, now=t0 + 10) == "valid"

    # Used beats superseded-or-not: mark the newer link used.
    conn.execute(
        "UPDATE set_password_links SET used_at = ? WHERE token_hash = ?",
        (t0 + 20, account_store.hash_token(newer.token)),
    )
    conn.commit()
    assert set_password_link_state(conn, newer.token, now=t0 + 20) == "used"

    # Expired: still unused and unsuperseded, past expires_at.
    expiring = issue_set_password_link(conn, "alice", now=t0 + 30, token="exp-tok")
    late = t0 + 30 + LINK_VALIDITY_SECONDS + 1
    assert set_password_link_state(conn, expiring.token, now=late) == "expired"

    # Removal supersedes the live link but leaves used and expired rows alone.
    live = issue_set_password_link(conn, "alice", now=t0 + 40, token="live-tok")
    assert set_password_link_state(conn, live.token, now=t0 + 40) == "valid"
    remove_account(conn, "alice", now=t0 + 50)
    assert set_password_link_state(conn, live.token, now=t0 + 60) == "superseded"
    assert set_password_link_state(conn, newer.token, now=t0 + 60) == "used"
    # Issuing the live link superseded the still-live expiring link, so it
    # now reads superseded (superseded beats expired in the spec order).
    assert set_password_link_state(conn, expiring.token, now=late + 30) == "superseded"

    # A removed account reads as unknown for a link with no other state:
    # simulate the defensive branch by clearing the removal supersession.
    conn.execute(
        "UPDATE set_password_links SET superseded_at = NULL WHERE token_hash = ?",
        (account_store.hash_token(live.token),),
    )
    conn.commit()
    assert set_password_link_state(conn, live.token, now=t0 + 60) == "unknown"


def test_expired_checked_before_removed(conn):
    # The link already expired when the account is removed, so removal leaves
    # it alone and it keeps reading expired, not unknown (spec order).
    create_account(conn, "alice", "viewer", now=1_000_000.0)
    link = issue_set_password_link(conn, "alice", now=1_000_000.0, token="e-tok")
    remove_account(conn, "alice", now=1_000_000.0 + LINK_VALIDITY_SECONDS + 100)
    assert (
        set_password_link_state(
            conn, link.token, now=1_000_000.0 + LINK_VALIDITY_SECONDS + 1
        )
        == "expired"
    )


def test_30_day_cleanup_keeps_then_deletes(isolated_state):
    t0 = 2_000_000.0
    conn = open_store(now=t0)
    create_account(conn, "alice", "viewer", now=t0)
    issue_set_password_link(conn, "alice", now=t0, token="old-tok")
    conn.close()

    # 29 days past expiry: the row survives and reads expired.
    conn = open_store(now=t0 + LINK_VALIDITY_SECONDS + 29 * 24 * 3600)
    try:
        assert (
            set_password_link_state(
                conn, "old-tok", now=t0 + LINK_VALIDITY_SECONDS + 29 * 24 * 3600
            )
            == "expired"
        )
    finally:
        conn.close()

    # Past 30 days: the row is deleted at open and reads unknown.
    conn = open_store(now=t0 + LINK_VALIDITY_SECONDS + 31 * 24 * 3600)
    try:
        assert (
            set_password_link_state(
                conn, "old-tok", now=t0 + LINK_VALIDITY_SECONDS + 31 * 24 * 3600
            )
            == "unknown"
        )
        assert cleanup_links(conn, now=t0 + LINK_VALIDITY_SECONDS + 31 * 24 * 3600) == 0
    finally:
        conn.close()


def test_used_link_kept_until_retention(conn):
    t0 = 3_000_000.0
    create_account(conn, "alice", "viewer", now=t0)
    issue_set_password_link(conn, "alice", now=t0, token="used-tok")
    conn.execute(
        "UPDATE set_password_links SET used_at = ? WHERE token_hash = ?",
        (t0 + 5, account_store.hash_token("used-tok")),
    )
    conn.commit()
    assert cleanup_links(conn, now=t0 + LINK_VALIDITY_SECONDS + 31 * 24 * 3600) == 1
    assert set_password_link_state(conn, "used-tok", now=time.time()) == "unknown"


# --- listing and controllers -----------------------------------------------------


def test_list_orders_removed_last(conn):
    create_account(conn, "zed", "viewer", now=1_000_000.0)
    create_account(conn, "amy", "viewer", now=1_000_000.0)
    create_account(conn, "mid", "viewer", now=1_000_000.0)
    remove_account(conn, "mid", now=1_000_100.0)
    assert [a.name for a in list_accounts(conn)] == ["amy", "zed", "mid"]


def test_active_controllers_need_password_and_no_tombstone(conn):
    create_account(conn, "nopass", "controller", now=1_000_000.0)
    create_account(conn, "viewer", "viewer", now=1_000_000.0)
    give_password(conn, "viewer")
    create_account(conn, "gone", "controller", now=1_000_000.0)
    give_password(conn, "gone")
    remove_account(conn, "gone", now=1_000_100.0)
    create_account(conn, "boss", "controller", now=1_000_000.0)
    give_password(conn, "boss")
    assert [a.name for a in active_controllers(conn)] == ["boss"]
