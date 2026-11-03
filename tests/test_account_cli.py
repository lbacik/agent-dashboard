"""`agent-dashboard account …` through main(argv) (spec 6.4).

Every command and every error runs through ``cli.main`` with captured
stdout/stderr and exit codes, against a temporary ``XDG_STATE_HOME`` and a
fixed clock.
"""

from __future__ import annotations

import re

import pytest

from agent_dashboard import account_cli, account_store, cli
from agent_dashboard.account_store import (
    LINK_VALIDITY_SECONDS,
    StoreError,
    set_password_link_state,
)

T0 = 1_700_000_000.0  # 2023-11-14 22:13 UTC; links expire 2023-11-15 22:13 UTC.


@pytest.fixture()
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(account_cli, "_now", lambda: T0)
    return tmp_path


def main_code(argv) -> int:
    """Run cli.main like a process: SystemExit code, or 0 on success."""
    try:
        cli.main(argv)
    except SystemExit as exc:
        assert isinstance(exc.code, int)
        return exc.code
    return 0


def token_of(out: str) -> str:
    match = re.search(r"token=(\S+)", out)
    assert match is not None
    return match.group(1)


def open_store():
    return account_store.open_store(now=T0)


def add_session(account: str, ident: bytes) -> None:
    conn = open_store()
    try:
        conn.execute(
            "INSERT INTO sign_in_sessions (id_hash, account, created_at, last_seen_at)"
            " VALUES (?, ?, ?, ?)",
            (ident, account, T0, T0),
        )
        conn.commit()
    finally:
        conn.close()


def give_password(account: str) -> None:
    conn = open_store()
    try:
        conn.execute(
            "UPDATE accounts SET password_hash = ?, password_set_at = ?"
            " WHERE name = ?",
            ("hashed", T0, account),
        )
        conn.commit()
    finally:
        conn.close()


# --- add -----------------------------------------------------------------------


def test_add_default_role_and_link_block(state, capsys):
    assert main_code(["account", "add", "alice"]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[0] == "Created Operator account alice (viewer)."
    assert lines[1] == (
        "Set-password link for alice (works once, until 2023-11-15 22:13 UTC):"
    )
    assert lines[2] == f"  /set-password?token={token_of(out)}"
    assert lines[3] == (
        "Send it to alice only. Open it through the dashboard's https:// "
        "address, or an SSH tunnel to localhost."
    )
    conn = open_store()
    try:
        assert set_password_link_state(conn, token_of(out), now=T0) == "valid"
    finally:
        conn.close()


def test_add_controller_role(state, capsys):
    assert main_code(["account", "add", "alice", "--role", "controller"]) == 0
    assert "Created Operator account alice (controller)." in capsys.readouterr().out


def test_add_url_builds_full_link_and_drops_trailing_slash(state, capsys):
    assert (
        main_code(
            ["account", "add", "alice", "--url", "https://dash.example.test/"]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert f"  https://dash.example.test/set-password?token={token_of(out)}" in out


def test_add_url_keeps_path_prefix(state, capsys):
    assert (
        main_code(["account", "add", "alice", "--url", "http://h:8000/prefix/"])
        == 0
    )
    out = capsys.readouterr().out
    assert f"  http://h:8000/prefix/set-password?token={token_of(out)}" in out


@pytest.mark.parametrize(
    "url",
    ["not-a-url", "ftp://dash.example.test", "http://", "//missing-scheme",
     "https://dash.example.test/path?x=1", "https://dash.example.test#frag",
     "https://user@dash.example.test", "https://dash.example.test:abc",
     "https://dash example.test"],
)
def test_add_bad_url_is_exit_1(state, capsys, url):
    assert main_code(["account", "add", "alice", "--url", url]) == 1
    err = capsys.readouterr().err
    assert err.startswith("agent-dashboard: error: ")
    assert "invalid --url" in err


@pytest.mark.parametrize("name", ["Alice", "ALICE", "0abc", "a_b", "a b"])
def test_add_bad_name_is_exit_1(state, capsys, name):
    assert main_code(["account", "add", name]) == 1
    err = capsys.readouterr().err
    assert err.startswith("agent-dashboard: error: ")
    assert "invalid account name" in err


def test_add_dash_name_is_usage_error(state, capsys):
    # A name starting with "-" never reaches validation: argparse owns it.
    assert main_code(["account", "add", "-abc"]) == 2


def test_add_name_too_long_is_exit_1(state, capsys):
    assert main_code(["account", "add", "x" * 33]) == 1
    assert "invalid account name" in capsys.readouterr().err


def test_add_duplicate_name_is_exit_1(state, capsys):
    assert main_code(["account", "add", "alice"]) == 0
    capsys.readouterr()
    assert main_code(["account", "add", "alice"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("agent-dashboard: error: ")
    assert "account name alice was used before and cannot be reused" in err


def test_add_bad_role_is_usage_error(state, capsys):
    assert main_code(["account", "add", "alice", "--role", "admin"]) == 2


# --- reset ----------------------------------------------------------------------


def test_reset_deletes_password_and_sessions_and_links(state, capsys):
    assert main_code(["account", "add", "alice"]) == 0
    first = token_of(capsys.readouterr().out)
    give_password("alice")
    add_session("alice", b"\x01" * 32)
    add_session("alice", b"\x02" * 32)
    assert main_code(["account", "reset", "alice"]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[0] == "Reset alice: password deleted, 2 sign-in sessions ended."
    assert lines[1].startswith("Set-password link for alice (works once, until ")
    second = token_of(out)
    assert second != first
    conn = open_store()
    try:
        assert set_password_link_state(conn, first, now=T0) == "superseded"
        assert set_password_link_state(conn, second, now=T0) == "valid"
        row = conn.execute(
            "SELECT password_hash FROM accounts WHERE name = 'alice'"
        ).fetchone()
        assert row["password_hash"] is None
        assert (
            conn.execute("SELECT COUNT(*) FROM sign_in_sessions").fetchone()[0] == 0
        )
    finally:
        conn.close()


def test_reset_with_url(state, capsys):
    assert main_code(["account", "add", "alice"]) == 0
    capsys.readouterr()
    assert (
        main_code(["account", "reset", "alice", "--url", "https://h.example/"]) == 0
    )
    out = capsys.readouterr().out
    assert "Reset alice: password deleted, 0 sign-in sessions ended." in out
    assert f"  https://h.example/set-password?token={token_of(out)}" in out


def test_reset_unknown_is_exit_1(state, capsys):
    assert main_code(["account", "reset", "ghost"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("agent-dashboard: error: ")
    assert "unknown Operator account" in err


def test_reset_invalid_name_is_exit_1(state, capsys):
    assert main_code(["account", "reset", "Alice"]) == 1
    assert "invalid account name" in capsys.readouterr().err


def test_reset_removed_is_exit_1(state, capsys):
    assert main_code(["account", "add", "alice"]) == 0
    capsys.readouterr()
    assert main_code(["account", "remove", "alice"]) == 0
    capsys.readouterr()
    assert main_code(["account", "reset", "alice"]) == 1
    assert "unknown Operator account" in capsys.readouterr().err


# --- role ------------------------------------------------------------------------


def test_role_now_and_already(state, capsys):
    assert main_code(["account", "add", "alice"]) == 0
    capsys.readouterr()
    assert main_code(["account", "role", "alice", "controller"]) == 0
    assert capsys.readouterr().out == "alice is now controller.\n"
    assert main_code(["account", "role", "alice", "controller"]) == 0
    assert capsys.readouterr().out == "alice is already controller.\n"


def test_role_bad_choice_is_usage_error(state, capsys):
    assert main_code(["account", "add", "alice"]) == 0
    capsys.readouterr()
    assert main_code(["account", "role", "alice", "admin"]) == 2


def test_role_unknown_is_exit_1(state, capsys):
    assert main_code(["account", "role", "ghost", "viewer"]) == 1
    assert "unknown Operator account" in capsys.readouterr().err


def test_role_invalid_name_is_exit_1(state, capsys):
    assert main_code(["account", "role", "Alice", "viewer"]) == 1
    assert "invalid account name" in capsys.readouterr().err


def test_role_removed_is_exit_1(state, capsys):
    assert main_code(["account", "add", "alice"]) == 0
    capsys.readouterr()
    assert main_code(["account", "remove", "alice"]) == 0
    capsys.readouterr()
    assert main_code(["account", "role", "alice", "viewer"]) == 1
    assert "unknown Operator account" in capsys.readouterr().err


# --- remove -----------------------------------------------------------------------


def test_remove_tombstones_and_keeps_name_reserved(state, capsys):
    assert main_code(["account", "add", "alice"]) == 0
    capsys.readouterr()
    add_session("alice", b"\x01" * 32)
    assert main_code(["account", "remove", "alice"]) == 0
    out = capsys.readouterr()
    assert (
        out.out == "Removed alice: 1 sign-in sessions ended. The name stays reserved.\n"
    )
    assert main_code(["account", "add", "alice"]) == 1
    assert "was used before and cannot be reused" in capsys.readouterr().err
    assert main_code(["account", "reset", "alice"]) == 1
    assert "unknown Operator account" in capsys.readouterr().err
    assert main_code(["account", "role", "alice", "viewer"]) == 1
    assert "unknown Operator account" in capsys.readouterr().err
    assert main_code(["account", "remove", "alice"]) == 1
    assert "unknown Operator account" in capsys.readouterr().err


def test_remove_unknown_is_exit_1(state, capsys):
    assert main_code(["account", "remove", "ghost"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("agent-dashboard: error: ")
    assert "unknown Operator account" in err


# --- list --------------------------------------------------------------------------


def test_list_empty(state, capsys):
    assert main_code(["account", "list"]) == 0
    assert capsys.readouterr().out == (
        "No Operator accounts. Create one with: "
        "agent-dashboard account add <name> --role controller\n"
    )


def test_list_table_columns_and_states(state, capsys, monkeypatch):
    assert main_code(["account", "add", "alice", "--role", "controller"]) == 0
    capsys.readouterr()
    give_password("alice")
    add_session("alice", b"\x01" * 32)
    assert main_code(["account", "add", "bob"]) == 0
    capsys.readouterr()
    assert main_code(["account", "add", "zed"]) == 0
    capsys.readouterr()
    assert main_code(["account", "remove", "zed"]) == 0
    capsys.readouterr()

    # Past the 24 h link life, bob's pending link reads as no password.
    monkeypatch.setattr(account_cli, "_now", lambda: T0 + LINK_VALIDITY_SECONDS + 1)
    assert main_code(["account", "list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    header = re.split(r" {2,}", lines[0].strip())
    assert header == [
        "NAME",
        "ROLE",
        "STATE",
        "CREATED",
        "PASSWORD SET",
        "REMOVED",
        "SIGN-IN SESSIONS",
    ]
    by_name = {}
    for line in lines[1:]:
        cells = re.split(r" {2,}", line.strip())
        by_name[cells[0]] = cells
    assert by_name["alice"][1:4] == ["controller", "active", "2023-11-14 22:13"]
    assert by_name["alice"][4] == "2023-11-14 22:13"
    assert by_name["alice"][5] == "-"
    assert by_name["alice"][6] == "1"
    assert by_name["bob"][2] == "no password"
    assert by_name["bob"][4] == "-"
    assert by_name["zed"][2] == "removed"
    assert by_name["zed"][5] == "2023-11-14 22:13"
    # Removed accounts sort last.
    assert [line.split()[0] for line in lines[1:]] == ["alice", "bob", "zed"]


def test_list_link_pending_state(state, capsys):
    assert main_code(["account", "add", "alice"]) == 0
    capsys.readouterr()
    assert main_code(["account", "list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "link pending" in lines[1]


# --- last controller warning ---------------------------------------------------------


def test_role_viewer_warns_when_no_controller_left(state, capsys):
    assert main_code(["account", "add", "alice", "--role", "controller"]) == 0
    capsys.readouterr()
    give_password("alice")
    assert main_code(["account", "role", "alice", "viewer"]) == 0
    out = capsys.readouterr()
    assert out.out == "alice is now viewer.\n"
    assert out.err == (
        "Warning: no active account has the controller Role now, so Control "
        "actions are unavailable to everyone. Grant it again with: "
        "agent-dashboard account role alice controller\n"
    )


def test_role_viewer_already_warns_when_no_controller_left(state, capsys):
    assert main_code(["account", "add", "alice"]) == 0
    capsys.readouterr()
    assert main_code(["account", "role", "alice", "viewer"]) == 0
    out = capsys.readouterr()
    assert out.out == "alice is already viewer.\n"
    assert "Warning: no active account has the controller Role now" in out.err


def test_remove_warns_when_no_controller_left(state, capsys):
    assert main_code(["account", "add", "alice", "--role", "controller"]) == 0
    capsys.readouterr()
    give_password("alice")
    assert main_code(["account", "remove", "alice"]) == 0
    out = capsys.readouterr()
    assert "The name stays reserved." in out.out
    assert "Grant it again with: agent-dashboard account role alice controller" in (
        out.err
    )


def test_no_warning_while_a_controller_remains(state, capsys):
    assert main_code(["account", "add", "alice", "--role", "controller"]) == 0
    capsys.readouterr()
    assert main_code(["account", "add", "bob", "--role", "controller"]) == 0
    capsys.readouterr()
    give_password("alice")
    give_password("bob")
    assert main_code(["account", "role", "alice", "viewer"]) == 0
    assert capsys.readouterr().err == ""
    assert main_code(["account", "remove", "alice"]) == 0
    assert capsys.readouterr().err == ""


def test_no_warning_for_passwordless_controller(state, capsys):
    # A controller without a password cannot sign in, so removing a viewer
    # still leaves no active controller: the warning fires.
    assert main_code(["account", "add", "alice"]) == 0
    capsys.readouterr()
    assert main_code(["account", "remove", "alice"]) == 0
    assert "Warning" in capsys.readouterr().err


# --- usage and store errors --------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [["account"], ["account", "add"], ["account", "frobnicate"],
     ["account", "role", "alice"], ["account", "reset"], ["account", "remove"]],
)
def test_usage_errors_exit_2(state, capsys, argv):
    assert main_code(argv) == 2


def test_store_failure_names_path_and_exits_1(state, capsys, monkeypatch):
    def boom(*args, **kwargs):
        raise StoreError("cannot open accounts store at /x/state: boom")

    monkeypatch.setattr(account_store, "open_store", boom)
    assert main_code(["account", "list"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("agent-dashboard: error: ")
    assert "/x/state" in err


def test_account_commands_ignore_serve_flags(state, capsys, monkeypatch):
    # The account parser takes no serve flags: one is a usage error, not a
    # serve startup, and LAN mode never blocks account management.
    assert main_code(["account", "list", "--host", "0.0.0.0"]) == 2
    monkeypatch.setenv("AGENT_DASHBOARD_LAN_MODE", "on")
    assert main_code(["account", "list"]) == 0
