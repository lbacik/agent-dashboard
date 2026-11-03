"""Set-password page in LAN mode (spec 8.3, 6.5, 6.3)."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_passwords import CheapHasher

from agent_dashboard import account_store
from agent_dashboard.app import create_app
from agent_dashboard.config import Settings
from agent_dashboard.passwords import normalize_password

T0 = 1_700_000_000.0
PASSWORD = "correct horse battery staple"
NEW_PASSWORD = "a fresh horse phrase for tests"


class Clock:
    def __init__(self, now: float = T0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def make_lan(tmp_path, monkeypatch, *, clock=None, hasher=None, **settings_kwargs):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    clock = clock if clock is not None else Clock()
    kwargs = {"lan_mode": True, "allowed_hosts": frozenset({"example.test"})}
    kwargs.update(settings_kwargs)
    settings = Settings(**kwargs)
    app = create_app(
        settings=settings,
        start_poller=False,
        clock=clock,
        password_hasher=hasher if hasher is not None else CheapHasher(),
    )
    return SimpleNamespace(
        app=app,
        client=TestClient(app, base_url="http://localhost"),
        https=TestClient(app, base_url="https://localhost"),
        lan_http=TestClient(app, base_url="http://example.test"),
        clock=clock,
        settings=settings,
    )


@pytest.fixture()
def lan(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    env = make_lan(tmp_path, monkeypatch)
    conn = account_store.open_store(now=T0)
    try:
        account_store.create_account(conn, "alice", "controller", now=T0)
    finally:
        conn.close()
    return env


def issue(name, token, now=T0):
    conn = account_store.open_store(now=now)
    try:
        return account_store.issue_set_password_link(
            conn, name, now=now, token=token
        )
    finally:
        conn.close()


def link_state(token, now=T0):
    conn = account_store.open_store(now=now)
    try:
        return account_store.set_password_link_state(conn, token, now=now)
    finally:
        conn.close()


def add_session(account, token, now=T0):
    digest = hashlib.sha256(token.encode()).digest()
    conn = account_store.open_store(now=now)
    try:
        account_store.create_sign_in_session(conn, account, digest, now=now)
    finally:
        conn.close()


def session_count(account, now=T0):
    conn = account_store.open_store(now=now)
    try:
        return account_store.count_account_sessions(conn, account)
    finally:
        conn.close()


def get_hash(account, now=T0):
    conn = account_store.open_store(now=now)
    try:
        row = account_store.get_account(conn, account)
        return None if row is None else row.password_hash
    finally:
        conn.close()


def auth_lines(capsys):
    lines = []
    for line in capsys.readouterr().out.splitlines():
        if line.startswith("auth "):
            lines.append(json.loads(line.removeprefix("auth ")))
    return lines


def post_set_password(client, token="tok", password=NEW_PASSWORD,
                      repeat=NEW_PASSWORD, origin="http://localhost",
                      extra_headers=None, extra_data=None):
    data = {"token": token, "password": password, "password_repeat": repeat}
    if extra_data:
        data.update(extra_data)
    headers = {"Origin": origin}
    if extra_headers:
        headers.update(extra_headers)
    return client.post("/set-password", data=data, headers=headers,
                       follow_redirects=False)


# --- GET valid link (spec 8.3) -------------------------------------------------


def test_get_valid_link_shows_form(lan):
    issue("alice", "good-tok")
    response = lan.client.get("/set-password?token=good-tok")
    assert response.status_code == 200
    text = response.text
    assert "Set your password" in text
    assert "Account <strong>alice</strong>" in text
    assert 'form method="post" action="/set-password" data-secure-form' in text
    assert 'data-secure-warn' in text
    assert '<script src="/static/sign-in.js" defer></script>' in text
    assert 'name="token" value="good-tok"' in text
    assert text.count("good-tok") == 1
    assert 'name="username"' in text
    assert 'value="alice"' in text
    assert 'autocomplete="username"' in text
    assert 'autocomplete="new-password"' in text
    assert 'minlength="15"' in text
    assert 'maxlength="128"' in text
    assert "Set password" in text
    assert "15 to 128 characters, anything allowed, spaces included." in text
    assert "data-password-count" in text
    assert "The link works once, until" in text
    assert "UTC (" in text and "h left)" in text


def test_get_valid_link_expiry_line(lan):
    link = issue("alice", "exp-tok")
    assert link.expires_at == T0 + 24 * 3600
    response = lan.client.get("/set-password?token=exp-tok")
    assert response.status_code == 200
    # 2023-11-14 22:13 UTC + 24h = 22:13 next day.
    assert "until 22:13 UTC (24 h left)" in response.text


def test_get_valid_link_sends_lan_headers_and_no_store(lan):
    issue("alice", "head-tok")
    response = lan.client.get("/set-password?token=head-tok")
    assert response.status_code == 200
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "same-origin"
    assert response.headers["cache-control"] == "no-store"


def test_token_appears_only_in_hidden_field(lan):
    issue("alice", "secret-token-xyz")
    response = lan.client.get("/set-password?token=secret-token-xyz")
    assert response.status_code == 200
    assert response.text.count("secret-token-xyz") == 1
    assert 'name="token" value="secret-token-xyz"' in response.text


# --- GET unusable links: 410 table (spec 8.3) ----------------------------------


def test_get_expired_link_is_410(lan):
    issue("alice", "old-tok", now=T0)
    lan.clock.now = T0 + 24 * 3600 + 1
    response = lan.client.get("/set-password?token=old-tok")
    assert response.status_code == 410
    assert link_state("old-tok", now=lan.clock.now) == "expired"
    assert 'data-tone="danger"' in response.text
    assert "has expired: links work for 24 hours" in response.text
    assert "Account <strong>alice</strong>" in response.text
    assert "<code>agent-dashboard account reset alice</code>" in response.text
    assert 'href="/login"' in response.text
    assert "<form" not in response.text


def test_get_used_link_is_410(lan):
    issue("alice", "used-tok")
    assert post_set_password(lan.client, token="used-tok").status_code == 303
    response = lan.client.get("/set-password?token=used-tok")
    assert response.status_code == 410
    assert link_state("used-tok", now=T0) == "used"
    assert "has already been used" in response.text
    assert "Account <strong>alice</strong>" in response.text
    assert "<code>agent-dashboard account reset alice</code>" in response.text
    assert 'href="/login"' in response.text


def test_get_superseded_link_is_410(lan):
    issue("alice", "first-tok", now=T0)
    issue("alice", "second-tok", now=T0 + 10)
    assert link_state("first-tok", now=T0 + 10) == "superseded"
    response = lan.client.get("/set-password?token=first-tok")
    assert response.status_code == 410
    assert "A newer Set-password link was issued" in response.text
    assert "Account <strong>alice</strong>" in response.text
    assert "<code>agent-dashboard account reset alice</code>" in response.text
    assert 'href="/login"' in response.text
    # The newer link still works.
    assert lan.client.get("/set-password?token=second-tok").status_code == 200


def test_get_unknown_link_is_410_without_account_name(lan):
    response = lan.client.get("/set-password?token=no-such-token")
    assert response.status_code == 410
    assert "not valid. Check that you copied the whole link" in response.text
    assert "Account <strong>" not in response.text
    assert "account reset" not in response.text
    assert 'href="/login"' in response.text
    assert 'data-tone="danger"' in response.text


def test_get_missing_token_is_410_unknown(lan):
    response = lan.client.get("/set-password")
    assert response.status_code == 410
    assert "not valid. Check that you copied the whole link" in response.text
    assert "Account <strong>" not in response.text


def test_get_removed_account_reads_unknown_without_name(lan):
    issue("alice", "gone-tok")
    conn = account_store.open_store(now=T0)
    try:
        account_store.remove_account(conn, "alice", now=T0)
        # Removal supersedes the live link; clear it to exercise the
        # defensive removed-as-unknown branch (spec 6.3 order).
        conn.execute(
            "UPDATE set_password_links SET superseded_at = NULL WHERE token_hash = ?",
            (account_store.hash_token("gone-tok"),),
        )
        conn.commit()
        assert (
            account_store.set_password_link_state(conn, "gone-tok", now=T0)
            == "unknown"
        )
    finally:
        conn.close()
    response = lan.client.get("/set-password?token=gone-tok")
    assert response.status_code == 410
    assert "not valid. Check that you copied the whole link" in response.text
    assert "Account <strong>" not in response.text
    assert "alice" not in response.text
    assert "account reset" not in response.text


def test_410_sends_lan_headers_and_no_store(lan):
    response = lan.client.get("/set-password?token=nope")
    assert response.status_code == 410
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "same-origin"
    assert response.headers["cache-control"] == "no-store"


# --- POST validation: 422 cases (spec 8.3) ------------------------------------


def test_post_too_short_is_422(lan):
    issue("alice", "v-tok")
    response = post_set_password(lan.client, token="v-tok",
                                 password="too short here", repeat="too short here")
    assert response.status_code == 422
    assert "Use at least 15 characters." in response.text
    assert 'name="token" value="v-tok"' in response.text
    assert response.text.count("v-tok") == 1
    assert get_hash("alice") is None
    assert link_state("v-tok") == "valid"


def test_post_too_long_is_422(lan):
    issue("alice", "v-tok")
    long_password = "x" * 129
    response = post_set_password(lan.client, token="v-tok",
                                 password=long_password, repeat=long_password)
    assert response.status_code == 422
    assert "Use at most 128 characters." in response.text
    assert get_hash("alice") is None


def test_post_mismatch_is_422(lan):
    issue("alice", "v-tok")
    response = post_set_password(
        lan.client, token="v-tok",
        password="a fresh horse phrase for tests",
        repeat="a different horse phrase here!",
    )
    assert response.status_code == 422
    assert "The two passwords differ." in response.text
    assert get_hash("alice") is None


def test_post_length_counted_after_nfkc_short(lan):
    # 13 ascii + decomposed e-acute (2 code points) is 15 raw, 14 normalised.
    issue("alice", "v-tok")
    raw = "a" * 13 + "é"
    assert len(raw) == 15
    assert len(normalize_password(raw)) == 14
    response = post_set_password(lan.client, token="v-tok",
                                 password=raw, repeat=raw)
    assert response.status_code == 422
    assert "Use at least 15 characters." in response.text


def test_post_length_counted_after_nfkc_expansion_passes(lan):
    # 13 ascii + ligature (1 code point) is 14 raw, 15 normalised: accepted.
    issue("alice", "v-tok")
    raw = "a" * 13 + "ﬁ"
    assert len(raw) == 14
    assert len(normalize_password(raw)) == 15
    response = post_set_password(lan.client, token="v-tok",
                                 password=raw, repeat=raw)
    assert response.status_code == 303


def test_post_422_sends_headers(lan):
    issue("alice", "v-tok")
    response = post_set_password(lan.client, token="v-tok",
                                 password="short", repeat="short")
    assert response.status_code == 422
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "same-origin"
    assert response.headers["cache-control"] == "no-store"


# --- POST token states: 410 (spec 8.3) ------------------------------------------


@pytest.mark.parametrize("state", ["expired", "used", "superseded", "unknown"])
def test_post_unusable_token_is_410(lan, state):
    lan.clock.now = T0
    if state == "expired":
        issue("alice", "s-tok", now=T0)
        lan.clock.now = T0 + 24 * 3600 + 1
        token = "s-tok"
    elif state == "used":
        issue("alice", "s-tok")
        assert post_set_password(lan.client, token="s-tok").status_code == 303
        token = "s-tok"
    elif state == "superseded":
        issue("alice", "s-tok", now=T0)
        issue("alice", "s-tok-2", now=T0 + 5)
        token = "s-tok"
    else:
        token = "no-such"
    response = post_set_password(lan.client, token=token)
    assert response.status_code == 410
    assert 'data-tone="danger"' in response.text
    if state == "unknown":
        assert "Account <strong>" not in response.text
    else:
        assert "Account <strong>alice</strong>" in response.text


def test_post_unknown_token_shows_no_account_name(lan):
    response = post_set_password(lan.client, token="ghost")
    assert response.status_code == 410
    assert "not valid. Check that you copied the whole link" in response.text
    assert "Account <strong>" not in response.text
    assert "ghost" not in response.text or 'value="ghost"' not in response.text


# --- POST success: transaction, redirect, sessions (spec 6.5) --------------------


def test_post_success_sets_password_and_redirects(lan, capsys):
    issue("alice", "win-tok")
    add_session("alice", "sess-one")
    add_session("alice", "sess-two")
    assert session_count("alice") == 2
    response = post_set_password(lan.client, token="win-tok")
    assert response.status_code == 303
    assert response.headers["location"] == (
        "/login?password_set=1&account=alice"
    )
    assert get_hash("alice") == f"cheap${NEW_PASSWORD}"
    assert link_state("win-tok") == "used"
    assert session_count("alice") == 0
    (line,) = auth_lines(capsys)
    assert line["event"] == "password_set"
    assert line["account"] == "alice"
    assert line["client"] == "testclient"
    assert set(line) == {"at", "event", "account", "client"}


def test_post_success_signs_nobody_in(lan):
    issue("alice", "win-tok")
    response = post_set_password(lan.client, token="win-tok")
    assert response.status_code == 303
    assert response.headers.get_list("set-cookie") == []
    assert lan.client.get("/", follow_redirects=False).status_code == 303


def test_used_link_cannot_be_used_again(lan):
    issue("alice", "once-tok")
    assert post_set_password(lan.client, token="once-tok").status_code == 303
    first_hash = get_hash("alice")
    response = post_set_password(
        lan.client, token="once-tok",
        password="another fresh horse phrase!!",
        repeat="another fresh horse phrase!!",
    )
    assert response.status_code == 410
    assert "has already been used" in response.text
    assert get_hash("alice") == first_hash


def test_post_success_ends_existing_sessions(lan):
    issue("alice", "win-tok")
    add_session("alice", "sess-one")
    assert post_set_password(lan.client, token="win-tok").status_code == 303
    from test_signin import cookie_header

    response = lan.client.get(
        "/", headers=cookie_header("agent-dashboard-sign-in", "sess-one"),
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2F"


def test_newer_link_supersedes_older(lan):
    issue("alice", "older-tok", now=T0)
    issue("alice", "newer-tok", now=T0 + 5)
    old = post_set_password(lan.client, token="older-tok")
    assert old.status_code == 410
    assert "newer Set-password link was issued" in old.text
    assert post_set_password(lan.client, token="newer-tok").status_code == 303


def test_login_prefills_name_after_password_set(lan):
    issue("alice", "win-tok")
    response = post_set_password(lan.client, token="win-tok")
    assert response.headers["location"] == "/login?password_set=1&account=alice"
    page = lan.client.get("/login?password_set=1&account=alice")
    assert page.status_code == 200
    assert "Your password is set. Sign in with it now." in page.text
    assert 'name="account" type="text" value="alice"' in page.text


# --- POST refusals: Origin and secure context (spec 4.2, 4.3) -------------------


def test_post_refused_origin(lan, capsys):
    issue("alice", "v-tok")
    response = post_set_password(lan.client, token="v-tok",
                                 origin="http://evil.test")
    assert response.status_code == 403
    assert "Sign-in was refused: the request did not come from this page." in response.text
    assert link_state("v-tok") == "valid"
    assert get_hash("alice") is None
    (line,) = auth_lines(capsys)
    assert line["event"] == "origin_refused"
    assert line["path"] == "/set-password"
    assert line["origin"] == "http://evil.test"
    assert line["reason"] == "origin"


def test_post_refused_fetch_site(lan, capsys):
    issue("alice", "v-tok")
    response = post_set_password(
        lan.client, token="v-tok",
        extra_headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert response.status_code == 403
    assert "Sign-in was refused: the request did not come from this page." in response.text
    (line,) = auth_lines(capsys)
    assert line["reason"] == "fetch_site"
    assert line["path"] == "/set-password"


def test_post_refused_without_https(lan, capsys):
    issue("alice", "v-tok")
    response = lan.lan_http.post(
        "/set-password",
        data={"token": "v-tok", "password": NEW_PASSWORD,
              "password_repeat": NEW_PASSWORD},
        headers={"Origin": "http://example.test"},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert ("Sign-in was refused: this page was not opened over HTTPS."
            in response.text)
    assert link_state("v-tok") == "valid"
    assert get_hash("alice") is None
    (line,) = auth_lines(capsys)
    assert line["event"] == "origin_refused"
    assert line["reason"] == "secure_context"


def test_post_form_over_8kib_is_413(lan, capsys):
    response = post_set_password(lan.client, token="x" * 9000)
    assert response.status_code == 413
    assert auth_lines(capsys) == []


# --- outside LAN mode: 404 (spec 3.1) -------------------------------------------


def test_outside_lan_mode_set_password_is_404(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    app = create_app(settings=Settings(), start_poller=False)
    client = TestClient(app, base_url="http://localhost")
    assert client.get("/set-password?token=x",
                      follow_redirects=False).status_code == 404
    assert client.post("/set-password", data={},
                       follow_redirects=False).status_code == 404


# --- view model only (spec 8) ----------------------------------------------------


def test_templates_consume_view_models_only():
    import dataclasses
    from pathlib import Path

    from agent_dashboard.auth import SetPasswordView

    assert dataclasses.is_dataclass(SetPasswordView)
    assert SetPasswordView.__dataclass_params__.frozen
    text = (Path("src/agent_dashboard/templates") / "set-password.html").read_text()
    assert "request.state" not in text
    assert "view.account" in text
    assert "view.token" in text
    assert "view.expires_text" in text
    assert "view.notice_tone" in text
    assert "view.show_form" in text
