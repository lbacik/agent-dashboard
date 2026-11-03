"""Sign-in, sign-out and the gate in LAN mode (spec 3, 4.3, 5.1-5.4, 5.6, 8, 9).

LAN mode apps are built directly with LAN mode ``Settings``; the CLI still
refuses LAN mode. The growing delay (5.5) lives in ``test_signin_delay.py``.
The stale-cookie box (5.6) is a separate ticket: ``GET /login`` with a stale
cookie clears it and shows the plain form.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest
from argon2.exceptions import VerifyMismatchError
from fastapi.testclient import TestClient
from test_passwords import CheapHasher

from agent_dashboard import account_store
from agent_dashboard.app import create_app, fleet_event_stream
from agent_dashboard.auth import (
    cookie_for_origin,
    is_secure_origin,
    sanitize_next,
    try_sanitize_next,
)
from agent_dashboard.config import Settings
from agent_dashboard.control_audit import audit_path
from agent_dashboard.passwords import normalize_password

T0 = 1_700_000_000.0  # 2023-11-14 22:13:33 UTC; the chip reads "22:13".
PASSWORD = "correct horse battery staple"


class Clock:
    """An injectable clock (spec 12): tests advance ``now`` by hand."""

    def __init__(self, now: float = T0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def make_lan(tmp_path, monkeypatch, *, clock=None, hasher=None, **settings_kwargs):
    """A LAN mode app with an isolated store, clock and cheap hasher."""
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


def add_account(name, role="controller", password=PASSWORD, now=T0):
    conn = account_store.open_store(now=now)
    try:
        account_store.create_account(conn, name, role, now=now)
        if password is not None:
            conn.execute(
                "UPDATE accounts SET password_hash = ? WHERE name = ?",
                (CheapHasher().hash(normalize_password(password)), name),
            )
            conn.commit()
    finally:
        conn.close()


def add_session(account, token, now=T0, created=None, last_seen=None):
    digest = hashlib.sha256(token.encode()).digest()
    conn = account_store.open_store(now=now)
    try:
        account_store.create_sign_in_session(conn, account, digest, now=now)
        if created is not None or last_seen is not None:
            conn.execute(
                "UPDATE sign_in_sessions SET created_at = ?, last_seen_at = ?"
                " WHERE id_hash = ?",
                (created if created is not None else now,
                 last_seen if last_seen is not None else now,
                 digest),
            )
            conn.commit()
    finally:
        conn.close()


def session_count(account, now=T0):
    conn = account_store.open_store(now=now)
    try:
        return account_store.count_account_sessions(conn, account)
    finally:
        conn.close()


def last_seen_of(account, token, now=T0):
    conn = account_store.open_store(now=now)
    try:
        row = conn.execute(
            "SELECT last_seen_at FROM sign_in_sessions WHERE id_hash = ?",
            (hashlib.sha256(token.encode()).digest(),),
        ).fetchone()
        return None if row is None else row["last_seen_at"]
    finally:
        conn.close()


def post_login(client, account="alice", password=PASSWORD,
               origin="http://localhost", **kwargs):
    data = {"account": account, "password": password}
    data.update(kwargs.pop("data", {}))
    headers = {"Origin": origin}
    headers.update(kwargs.pop("headers", {}))
    return client.post("/login", data=data, headers=headers,
                       follow_redirects=False, **kwargs)


def set_cookies(response):
    """The response's ``Set-Cookie`` values keyed by cookie name."""
    out = {}
    for header in response.headers.get_list("set-cookie"):
        pair = header.split(";", 1)[0]
        name, _, value = pair.partition("=")
        out[name.strip()] = value.strip().strip('"')
    return out


def cookie_header(name, value):
    return {"Cookie": f"{name}={value}"}


def auth_lines(capsys):
    """The ``auth`` stdout lines as parsed JSON (spec 9)."""
    lines = []
    for line in capsys.readouterr().out.splitlines():
        if line.startswith("auth "):
            lines.append(json.loads(line.removeprefix("auth ")))
    return lines


@pytest.fixture()
def lan(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    add_account("alice", now=T0)
    return make_lan(tmp_path, monkeypatch)


# --- the next rule (spec 5.1) ----------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "/",
        "/instances/work/history",
        "/instances/work/history?preset=7d",
        "/loginx",
        "/Login",
        "/logout-all",
        "/" + "a" * 2047,  # exactly 2048 characters
    ],
)
def test_next_rule_accepts(value):
    assert sanitize_next(value) == value
    assert try_sanitize_next(value) == value


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "//evil",
        "/\\evil",
        "/login",
        "/login?next=/",
        "/logout",
        "/logout/",
        "/set-password",
        "/set-password?token=x",
        "http://localhost/",
        "instances/work",
        " /",
        "/" + "a" * 2048,  # 2049 characters
        "/page\x00",
        "/page\x1f",
        "/page\x7f",
        "/page\x9f",
        "/pag\ne",
    ],
)
def test_next_rule_rejects(value):
    assert sanitize_next(value) == "/"
    assert try_sanitize_next(value) is None


def test_clean_next_round_trips_through_login(lan):
    response = post_login(lan.client, origin="http://localhost",
                          data={"next": "/instances/work/history?preset=7d"})
    assert response.status_code == 303
    assert response.headers["location"] == "/instances/work/history?preset=7d"


def test_rejected_next_falls_back_to_root(lan):
    for bad in ("//evil", "/login", "/logout", "/set-password",
                "http://localhost/", "/" + "a" * 2048):
        response = post_login(lan.client, origin="http://localhost",
                              data={"next": bad})
        assert response.status_code == 303, bad
        assert response.headers["location"] == "/", bad


# --- the gate table (spec 5.1) ---------------------------------------------------


def test_gate_htmx_gets_redirect(lan):
    response = lan.client.get(
        "/",
        headers={
            "HX-Request": "true",
            "HX-Current-URL": "http://localhost/instances/work/history?preset=7d",
        },
        follow_redirects=False,
    )
    assert response.status_code == 401
    assert response.text == ""
    assert response.headers["hx-redirect"] == (
        "/login?next=%2Finstances%2Fwork%2Fhistory%3Fpreset%3D7d"
    )


def test_gate_htmx_without_current_url_has_no_next(lan):
    response = lan.client.get("/", headers={"HX-Request": "true"},
                               follow_redirects=False)
    assert response.status_code == 401
    assert response.headers["hx-redirect"] == "/login"


@pytest.mark.parametrize("page", ["//evil", "/login", "/logout",
                                  "/set-password?token=x", "/page\x7f"])
def test_gate_htmx_with_bad_current_url_has_no_next(lan, page):
    response = lan.client.get(
        "/",
        headers={"HX-Request": "true", "HX-Current-URL": f"http://localhost{page}"},
        follow_redirects=False,
    )
    assert response.status_code == 401
    assert response.headers["hx-redirect"] == "/login"


def test_gate_htmx_overlong_current_url_has_no_next(lan):
    response = lan.client.get(
        "/",
        headers={"HX-Request": "true",
                 "HX-Current-URL": "http://localhost/" + "a" * 2048},
        follow_redirects=False,
    )
    assert response.status_code == 401
    assert response.headers["hx-redirect"] == "/login"


def test_gate_stream_gets_plain_401(lan):
    response = lan.client.get("/stream", follow_redirects=False)
    assert response.status_code == 401
    assert response.text == "Sign-in required."
    assert "text/plain" in response.headers["content-type"]


def test_gate_plain_get_redirects_with_next(lan):
    response = lan.client.get("/instances/work/history?preset=7d",
                               follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == (
        "/login?next=%2Finstances%2Fwork%2Fhistory%3Fpreset%3D7d"
    )


def test_gate_root_redirects_with_root_next(lan):
    response = lan.client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2F"


def test_gate_head_redirects(lan):
    response = lan.client.head("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2F"


def test_gate_other_methods_get_plain_401(lan):
    response = lan.client.post(
        "/instances/work/control/review",
        data={"action": "stop"},
        headers={"Origin": "http://localhost"},
        follow_redirects=False,
    )
    assert response.status_code == 401
    assert response.text == "Sign-in required."
    assert "text/plain" in response.headers["content-type"]


def test_gate_htmx_post_gets_redirect(lan):
    response = lan.client.post(
        "/instances/work/control/review",
        data={"action": "stop"},
        headers={"Origin": "http://localhost", "HX-Request": "true",
                 "HX-Current-URL": "http://localhost/instances/work"},
        follow_redirects=False,
    )
    assert response.status_code == 401
    assert response.text == ""
    assert response.headers["hx-redirect"] == "/login?next=%2Finstances%2Fwork"


def test_gate_refusal_writes_no_control_audit_entry(lan):
    response = lan.client.post(
        "/instances/work/control/review",
        data={"action": "stop"},
        headers={"Origin": "http://localhost", "HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 401
    assert not audit_path().exists()


def test_gate_refusal_writes_no_auth_line(lan, capsys):
    lan.client.get("/", follow_redirects=False)
    assert auth_lines(capsys) == []


# --- anonymous routes (spec 3.1, 3.2) ---------------------------------------------


def test_static_is_anonymous(lan):
    response = lan.client.get("/static/style.css", follow_redirects=False)
    assert response.status_code == 200


def test_whoami_signed_in_is_204(lan):
    add_session("alice", "tok-whoami")
    response = lan.client.get("/whoami", headers=cookie_header(
        "agent-dashboard-sign-in", "tok-whoami"))
    assert response.status_code == 204
    assert response.text == ""


def test_whoami_unsigned_gets_gate_answer(lan):
    assert lan.client.get("/whoami", follow_redirects=False).status_code == 303


def test_whoami_unsigned_htmx_gets_redirect(lan):
    response = lan.client.get(
        "/whoami",
        headers={"HX-Request": "true",
                 "HX-Current-URL": "http://localhost/instances/work"},
        follow_redirects=False,
    )
    assert response.status_code == 401
    assert response.headers["hx-redirect"] == "/login?next=%2Finstances%2Fwork"


# --- outside LAN mode: nothing new (spec 3, 8.2) -----------------------------------


def test_outside_lan_mode_login_and_logout_are_404(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    app = create_app(settings=Settings(), start_poller=False)
    client = TestClient(app, base_url="http://localhost")
    assert client.get("/login", follow_redirects=False).status_code == 404
    assert client.post("/login", data={}, follow_redirects=False).status_code == 404
    assert client.post("/logout", follow_redirects=False).status_code == 404
    assert client.get("/whoami", follow_redirects=False).status_code == 404


def test_outside_lan_mode_no_gate_and_topbar_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    app = create_app(settings=Settings(), start_poller=False)
    client = TestClient(app, base_url="http://localhost")
    response = client.get("/")
    assert response.status_code == 200
    assert "Operator <strong>" not in response.text
    assert "topbar-signout" not in response.text
    assert "Sign out" not in response.text
    assert "Stream <strong>5s</strong>" in response.text
    assert "sign-in.js" not in response.text
    assert "x-frame-options" not in response.headers
    assert "referrer-policy" not in response.headers


# --- POST /login (spec 5.3, 4.3) -----------------------------------------------------


def test_login_success_redirects_and_sets_loopback_cookie(lan):
    response = post_login(lan.client)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    cookies = set_cookies(response)
    assert "agent-dashboard-sign-in" in cookies
    assert "__Host-agent-dashboard-sign-in" not in cookies
    raw = response.headers.get_list("set-cookie")
    loopback = next(h for h in raw if h.startswith("agent-dashboard-sign-in="))
    assert "Secure" not in loopback
    assert "HttpOnly" in loopback
    assert "SameSite=strict" in loopback
    assert "Path=/" in loopback
    assert "Max-Age=43200" in loopback


def test_login_success_over_https_sets_host_cookie(lan):
    response = post_login(lan.https, origin="https://localhost")
    assert response.status_code == 303
    cookies = set_cookies(response)
    assert "__Host-agent-dashboard-sign-in" in cookies
    raw = response.headers.get_list("set-cookie")
    host = next(h for h in raw if h.startswith("__Host-agent-dashboard-sign-in="))
    assert "Secure" in host
    assert "HttpOnly" in host
    assert "SameSite=strict" in host
    assert "Path=/" in host
    assert "Max-Age=43200" in host


def test_login_max_age_equals_remaining_lifetime(tmp_path, monkeypatch):
    env = make_lan(tmp_path, monkeypatch, sign_in_idle_timeout=300.0,
                   sign_in_max_age=3600.0)
    add_account("alice", now=T0)
    response = post_login(env.client)
    assert response.status_code == 303
    raw = response.headers.get_list("set-cookie")
    assert any("Max-Age=3600" in header for header in raw)


def test_login_mints_a_new_session_id_every_time(lan):
    first = set_cookies(post_login(lan.client))["agent-dashboard-sign-in"]
    second = set_cookies(post_login(lan.client))["agent-dashboard-sign-in"]
    assert first != second
    for token in (first, second):
        response = lan.client.get("/", headers=cookie_header(
            "agent-dashboard-sign-in", token), follow_redirects=False)
        assert response.status_code == 200


def test_login_reads_host_cookie_first(lan):
    good = set_cookies(post_login(lan.https,
                                  origin="https://localhost"))[
        "__Host-agent-dashboard-sign-in"]
    headers = {"Cookie": f"__Host-agent-dashboard-sign-in={good}; "
                         "agent-dashboard-sign-in=bogus"}
    assert lan.client.get("/", headers=headers,
                           follow_redirects=False).status_code == 200


def test_unknown_name_fails_like_wrong_password(lan):
    response = post_login(lan.client, account="mallory")
    assert response.status_code == 401
    assert "Account name or password is incorrect." in response.text
    assert response.text.count("Account name or password is incorrect.") == 1
    assert "mallory" in response.text


def test_wrong_password_fails(lan):
    response = post_login(lan.client, password="wrong password here!!")
    assert response.status_code == 401
    assert "Account name or password is incorrect." in response.text
    assert "alice" in response.text


def test_login_error_never_says_whether_account_exists(lan):
    unknown = post_login(lan.client, account="nosuchone")
    wrong = post_login(lan.client, account="alice", password="wrong password here!!")
    assert "exist" not in unknown.text.lower()
    assert "exist" not in wrong.text.lower()


def test_login_names_are_normalised(lan):
    response = post_login(lan.client, account="  ALICE  ")
    assert response.status_code == 303
    cookies = set_cookies(response)
    authed = lan.client.get("/", headers=cookie_header(
        "agent-dashboard-sign-in", cookies["agent-dashboard-sign-in"]),
        follow_redirects=False)
    assert authed.status_code == 200
    assert "Operator <strong>alice · controller</strong>" in authed.text


def test_login_password_is_nfkc_normalised(tmp_path, monkeypatch):
    env = make_lan(tmp_path, monkeypatch)
    ligature = "ﬁsh and ships make dinner elongated"
    add_account("bob", password=ligature, now=T0)
    response = post_login(env.client, account="bob", password=ligature)
    assert response.status_code == 303


def test_login_overlong_password_fails_without_checking(lan):
    response = post_login(lan.client, password="x" * 1025)
    assert response.status_code == 401
    assert "Account name or password is incorrect." in response.text


@pytest.mark.parametrize("account", ["", "@@@", "a" * 100])
def test_login_strange_names_fail_safely(lan, account):
    response = post_login(lan.client, account=account)
    assert response.status_code == 401
    assert "Account name or password is incorrect." in response.text


def test_login_refused_origin(lan):
    response = post_login(lan.client, origin="http://evil.test")
    assert response.status_code == 403
    assert "Sign-in was refused: the request did not come from this page." in response.text


def test_login_refused_fetch_site(lan):
    response = post_login(lan.client, headers={"Sec-Fetch-Site": "cross-site"})
    assert response.status_code == 403
    assert "Sign-in was refused: the request did not come from this page." in response.text


def test_login_refused_without_https(lan):
    response = lan.lan_http.post(
        "/login",
        data={"account": "alice", "password": PASSWORD},
        headers={"Origin": "http://example.test"},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert ("Sign-in was refused: this page was not opened over HTTPS."
            in response.text)


def test_login_over_https_on_lan_host_succeeds(tmp_path, monkeypatch):
    env = make_lan(tmp_path, monkeypatch)
    add_account("alice", now=T0)
    client = TestClient(env.app, base_url="http://example.test")
    response = client.post(
        "/login",
        data={"account": "alice", "password": PASSWORD},
        headers={"Origin": "https://example.test"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "__Host-agent-dashboard-sign-in" in set_cookies(response)


def test_login_form_over_8kib_is_413(lan, capsys):
    response = post_login(lan.client, data={"account": "a" * 9000})
    assert response.status_code == 413
    assert auth_lines(capsys) == []


def test_rehash_updates_stale_hash(tmp_path, monkeypatch):
    class RehashHasher(CheapHasher):
        def verify(self, hash, password):
            if hash in ("old-params-hash", "cheap$" + password):
                return True
            raise VerifyMismatchError("mismatch")

        def check_needs_rehash(self, hash):
            return True

    env = make_lan(tmp_path, monkeypatch, hasher=RehashHasher())
    conn = account_store.open_store(now=T0)
    try:
        account_store.create_account(conn, "alice", "controller", now=T0)
        conn.execute("UPDATE accounts SET password_hash = ? WHERE name = ?",
                     ("old-params-hash", "alice"))
        conn.commit()
    finally:
        conn.close()
    assert post_login(env.client).status_code == 303
    conn = account_store.open_store(now=T0)
    try:
        row = conn.execute(
            "SELECT password_hash FROM accounts WHERE name = ?", ("alice",)
        ).fetchone()
        assert row["password_hash"] == "cheap$" + PASSWORD
    finally:
        conn.close()


# --- GET /login (spec 8.1) --------------------------------------------------------------


def test_login_page_marks_and_boxes(lan):
    response = lan.client.get("/login")
    assert response.status_code == 200
    assert "LAN mode · sign-in required" in response.text
    assert "Stream <strong>" not in response.text
    assert "Control <strong>" not in response.text
    assert "Operator" not in response.text
    assert 'form method="post" action="/login" data-secure-form' in response.text
    assert 'data-secure-warn' in response.text
    assert '<script src="/static/sign-in.js" defer></script>' in response.text
    assert 'name="next" value=""' in response.text
    assert 'autocomplete="username"' in response.text
    assert 'autocapitalize="none"' in response.text
    assert 'spellcheck="false"' in response.text
    assert 'autocomplete="current-password"' in response.text
    assert "Set-password link." in response.text


def test_login_page_next_is_kept(lan):
    response = lan.client.get("/login?next=/instances/work/history")
    assert response.status_code == 200
    assert 'name="next" value="/instances/work/history"' in response.text


def test_login_page_signed_out_box(lan):
    response = lan.client.get("/login?signed_out=1")
    assert response.status_code == 200
    assert 'data-tone="ok"' in response.text
    assert "You have signed out." in response.text


def test_login_page_password_set_box(lan):
    response = lan.client.get("/login?password_set=1&account=alice")
    assert response.status_code == 200
    assert "Your password is set. Sign in with it now." in response.text
    assert 'name="account" type="text" value="alice"' in response.text
    assert 'name="password" type="password"' in response.text
    assert ('autocomplete="current-password" autofocus' in response.text)


def test_login_page_password_set_with_bad_name_prefills_nothing(lan):
    response = lan.client.get("/login?password_set=1&account=BAD+NAME!")
    assert response.status_code == 200
    assert "Your password is set. Sign in with it now." in response.text
    assert 'name="account" type="text" value=""' in response.text


def test_login_page_with_valid_session_redirects(lan):
    token = set_cookies(post_login(lan.client))["agent-dashboard-sign-in"]
    response = lan.client.get(
        "/login?next=/instances/work/history",
        headers=cookie_header("agent-dashboard-sign-in", token),
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/instances/work/history"


def test_login_page_with_stale_cookie_clears_and_shows_form(lan):
    response = lan.client.get(
        "/login",
        headers=cookie_header("agent-dashboard-sign-in", "stale"),
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert "Sign in" in response.text
    cleared = response.headers.get_list("set-cookie")
    assert any(h.startswith("__Host-agent-dashboard-sign-in=") for h in cleared)
    assert any(h.startswith("agent-dashboard-sign-in=") for h in cleared)


# --- POST /logout (spec 5.4) ---------------------------------------------------------------


def test_logout_ends_session_and_redirects(lan):
    token = set_cookies(post_login(lan.client))["agent-dashboard-sign-in"]
    headers = dict(cookie_header("agent-dashboard-sign-in", token))
    headers["Origin"] = "http://localhost"
    response = lan.client.post("/logout", headers=headers, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?signed_out=1"
    cleared = response.headers.get_list("set-cookie")
    assert any(h.startswith("__Host-agent-dashboard-sign-in=") for h in cleared)
    assert any(h.startswith("agent-dashboard-sign-in=") for h in cleared)
    assert any("Max-Age=0" in h for h in cleared)
    refused = lan.client.get("/", headers=cookie_header(
        "agent-dashboard-sign-in", token), follow_redirects=False)
    assert refused.status_code == 303


def test_logout_ends_only_the_current_session(lan):
    first = set_cookies(post_login(lan.client))["agent-dashboard-sign-in"]
    second = set_cookies(post_login(lan.client))["agent-dashboard-sign-in"]
    assert session_count("alice") == 2
    headers = dict(cookie_header("agent-dashboard-sign-in", first))
    headers["Origin"] = "http://localhost"
    assert lan.client.post("/logout", headers=headers,
                           follow_redirects=False).status_code == 303
    assert session_count("alice") == 1
    response = lan.client.get("/", headers=cookie_header(
        "agent-dashboard-sign-in", second), follow_redirects=False)
    assert response.status_code == 200


def test_logout_refused_origin_keeps_session(lan):
    token = set_cookies(post_login(lan.client))["agent-dashboard-sign-in"]
    headers = dict(cookie_header("agent-dashboard-sign-in", token))
    headers["Origin"] = "http://evil.test"
    response = lan.client.post("/logout", headers=headers, follow_redirects=False)
    assert response.status_code == 403
    assert response.text == "Sign-out was refused: the request did not come from this page."
    assert lan.client.get("/", headers=cookie_header(
        "agent-dashboard-sign-in", token),
        follow_redirects=False).status_code == 200


def test_logout_refused_fetch_site_keeps_session(lan):
    token = set_cookies(post_login(lan.client))["agent-dashboard-sign-in"]
    headers = dict(cookie_header("agent-dashboard-sign-in", token))
    headers["Origin"] = "http://localhost"
    headers["Sec-Fetch-Site"] = "cross-site"
    response = lan.client.post("/logout", headers=headers, follow_redirects=False)
    assert response.status_code == 403
    assert lan.client.get("/", headers=cookie_header(
        "agent-dashboard-sign-in", token),
        follow_redirects=False).status_code == 200


def test_logout_without_session_gets_gate_answer(lan):
    response = lan.client.post(
        "/logout", headers={"Origin": "http://localhost"},
        follow_redirects=False)
    assert response.status_code == 401
    assert response.text == "Sign-in required."


def test_logout_without_session_htmx_gets_redirect(lan):
    response = lan.client.post(
        "/logout",
        headers={"Origin": "http://localhost", "HX-Request": "true",
                 "HX-Current-URL": "http://localhost/instances/work"},
        follow_redirects=False,
    )
    assert response.status_code == 401
    assert response.headers["hx-redirect"] == "/login?next=%2Finstances%2Fwork"


# --- sessions: expiry, activity, revocation (spec 5.6, 6.2) ----------------------------------


def test_idle_expiry_refuses_and_deletes_row(lan):
    add_session("alice", "old-idle", created=T0 - 100, last_seen=T0 - 3700)
    response = lan.client.get(
        "/", headers=cookie_header("agent-dashboard-sign-in", "old-idle"),
        follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2F"
    assert session_count("alice") == 0


def test_absolute_expiry_refuses_and_deletes_row(tmp_path, monkeypatch):
    env = make_lan(tmp_path, monkeypatch, sign_in_idle_timeout=300.0,
                   sign_in_max_age=600.0)
    add_account("alice", now=T0)
    add_session("alice", "old-absolute", now=T0, created=T0 - 700,
                last_seen=T0 - 10)
    response = env.client.get(
        "/", headers=cookie_header("agent-dashboard-sign-in", "old-absolute"),
        follow_redirects=False)
    assert response.status_code == 303
    assert session_count("alice") == 0


def test_activity_refreshes_at_most_once_a_minute(lan):
    token = set_cookies(post_login(lan.client))["agent-dashboard-sign-in"]
    headers = cookie_header("agent-dashboard-sign-in", token)
    lan.clock.now = T0 + 30
    lan.client.get("/", headers=headers, follow_redirects=False)
    assert last_seen_of("alice", token) == T0
    lan.clock.now = T0 + 91
    lan.client.get("/", headers=headers, follow_redirects=False)
    assert last_seen_of("alice", token) == T0 + 91


def test_reset_account_refuses_at_once(tmp_path, monkeypatch):
    env = make_lan(tmp_path, monkeypatch)
    add_account("alice", now=T0)
    token = set_cookies(post_login(env.client))["agent-dashboard-sign-in"]
    conn = account_store.open_store(now=T0)
    try:
        account_store.reset_password(conn, "alice")
    finally:
        conn.close()
    headers = dict(cookie_header("agent-dashboard-sign-in", token))
    assert env.client.get("/", headers=headers,
                           follow_redirects=False).status_code == 303
    htmx = dict(headers)
    htmx["HX-Request"] = "true"
    htmx["HX-Current-URL"] = "http://localhost/instances/work"
    response = env.client.get("/", headers=htmx, follow_redirects=False)
    assert response.status_code == 401
    assert response.headers["hx-redirect"] == "/login?next=%2Finstances%2Fwork"


def test_removed_account_session_refused_at_once(lan):
    add_session("alice", "removed-token")
    conn = account_store.open_store(now=T0)
    try:
        account_store.remove_account(conn, "alice", now=T0)
    finally:
        conn.close()
    headers = dict(cookie_header("agent-dashboard-sign-in", "removed-token"))
    assert lan.client.get("/", headers=headers,
                           follow_redirects=False).status_code == 303
    headers["HX-Request"] = "true"
    headers["HX-Current-URL"] = "http://localhost/"
    response = lan.client.get("/", headers=headers, follow_redirects=False)
    assert response.status_code == 401
    assert response.headers["hx-redirect"] == "/login?next=%2F"


def test_passwordless_account_session_refused_at_once(tmp_path, monkeypatch):
    env = make_lan(tmp_path, monkeypatch)
    add_account("carol", password=None, now=T0)
    add_session("carol", "nopass-token")
    response = env.client.get(
        "/", headers=cookie_header("agent-dashboard-sign-in", "nopass-token"),
        follow_redirects=False)
    assert response.status_code == 303


def test_role_is_read_on_every_request(lan):
    token = set_cookies(post_login(lan.client))["agent-dashboard-sign-in"]
    headers = cookie_header("agent-dashboard-sign-in", token)
    assert "Operator <strong>alice · controller</strong>" in lan.client.get(
        "/", headers=headers).text
    conn = account_store.open_store(now=T0)
    try:
        account_store.set_role(conn, "alice", "viewer")
    finally:
        conn.close()
    response = lan.client.get("/", headers=headers)
    assert "Operator <strong>alice · viewer</strong>" in response.text


# --- /stream re-check (spec 5.6) ------------------------------------------------------------------


def test_stream_ends_when_the_session_ends():
    async def main():
        queue: asyncio.Queue = asyncio.Queue()

        class FakeFleet:
            def subscribe(self):
                return queue

            def unsubscribe(self, _queue):
                pass

            async def snapshot(self, poll_interval=None):
                return ([], None)

        seen = 0

        async def is_signed_in():
            nonlocal seen
            seen += 1
            return seen == 1

        async def feeder():
            await asyncio.sleep(0.05)
            await queue.put(1)

        async def connected():
            return False

        task = asyncio.create_task(feeder())
        chunks = []
        async for chunk in fleet_event_stream(
            FakeFleet(), lambda rows, error: "TABLE",
            connected, None, is_signed_in=is_signed_in,
        ):
            chunks.append(chunk)
        await task
        return chunks

    chunks = asyncio.run(main())
    assert len(chunks) == 2
    assert chunks[0].startswith("retry:")
    assert chunks[1].startswith("event: fleet")


def test_stream_with_ended_session_refuses_at_connect(lan):
    add_session("alice", "gone", created=T0 - 100, last_seen=T0 - 3700)
    response = lan.client.get(
        "/stream", headers=cookie_header("agent-dashboard-sign-in", "gone"),
        follow_redirects=False)
    assert response.status_code == 401


# --- UI: headers, chip, sign out (spec 8, 8.1, 8.2) ---


def test_html_responses_carry_security_headers(lan):
    token = set_cookies(post_login(lan.client))["agent-dashboard-sign-in"]
    headers = cookie_header("agent-dashboard-sign-in", token)
    for path in ("/", "/instances/nope/history",
                 "/instances/nope/attempts/3/abc",
                 "/instances/nope/attempts/3/abc/progress"):
        response = lan.client.get(path, headers=headers)
        assert response.status_code == 200, path
        assert response.headers["x-frame-options"] == "DENY", path
        assert response.headers["referrer-policy"] == "same-origin", path
        assert '<script src="/static/sign-in.js" defer></script>' in response.text, path

def test_operator_chip_and_sign_out_on_every_dashboard_page(lan):
    token = set_cookies(post_login(lan.client))["agent-dashboard-sign-in"]
    headers = cookie_header("agent-dashboard-sign-in", token)
    chip = (
        '<span class="topbar-chip" '
        'title="Signed in since 22:13 UTC">'
        "Operator <strong>alice · controller</strong></span>"
    )
    sign_out = (
        '<form class="topbar-signout" action="/logout" '
        'method="post">'
    )
    for path in ("/", "/instances/nope/history",
                 "/instances/nope/attempts/3/abc",
                 "/instances/nope/attempts/3/abc/progress"):
        response = lan.client.get(path, headers=headers)
        assert response.status_code == 200, path
        assert chip in response.text, path
        assert sign_out in response.text, path
        assert "Sign out" in response.text, path
        assert "data-secure-form" not in response.text, path


def test_login_page_sends_no_store(lan):
    response = lan.client.get("/login")
    assert response.status_code == 200
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "same-origin"
    assert response.headers["cache-control"] == "no-store"


def test_login_failure_box_sends_security_headers(lan):
    response = post_login(lan.client, password="wrong password here!!")
    assert response.status_code == 401
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "same-origin"
    assert response.headers["cache-control"] == "no-store"


def test_sign_in_js_marks_secure_form(lan):
    response = lan.client.get("/static/sign-in.js")
    assert response.status_code == 200
    assert "window.isSecureContext" in response.text
    assert "data-secure-form" in response.text
    assert "data-secure-warn" in response.text


# --- secure context and cookie choice, unit level (spec 4.3, 5.2) ---


@pytest.mark.parametrize("origin", [
    "https://localhost",
    "https://example.test",
    "https://example.test:8443",
    "HTTPS://EXAMPLE.TEST/",
])
def test_is_secure_origin_accepts_https(origin):
    assert is_secure_origin(origin) is True


@pytest.mark.parametrize("origin", [
    "http://localhost",
    "http://localhost:8000",
    "http://127.0.0.1",
    "http://127.0.0.5",
    "http://[::1]",
])
def test_is_secure_origin_accepts_loopback_http(origin):
    assert is_secure_origin(origin) is True


@pytest.mark.parametrize("origin", [
    "http://example.test",
    "http://192.168.1.2",
    "http://[::2]",
    "ftp://localhost/file",
    "not an origin",
    "",
])
def test_is_secure_origin_refuses(origin):
    assert is_secure_origin(origin) is False


def test_cookie_for_origin_https():
    name, secure = cookie_for_origin("https://example.test")
    assert name == "__Host-agent-dashboard-sign-in"
    assert secure is True


def test_cookie_for_origin_loopback_http():
    name, secure = cookie_for_origin("http://localhost")
    assert name == "agent-dashboard-sign-in"
    assert secure is False


# --- auth lines (spec 9) ---


def test_sign_in_logs_auth_line(lan, capsys):
    post_login(lan.client)
    (line,) = auth_lines(capsys)
    assert line["event"] == "sign_in"
    assert line["account"] == "alice"
    assert line["client"] == "testclient"
    assert line["at"].endswith("Z")
    assert set(line) == {"at", "event", "account", "client"}


def test_sign_in_failed_logs_auth_line(lan, capsys):
    response = post_login(lan.client, password="wrong password here!!")
    assert response.status_code == 401
    (line,) = auth_lines(capsys)
    assert line["event"] == "sign_in_failed"
    assert line["account"] == "alice"
    assert line["client"] == "testclient"
    assert line["delay_s"] == 1
    assert set(line) == {"at", "event", "account", "client", "delay_s"}


def test_sign_out_logs_auth_line(lan, capsys):
    token = set_cookies(post_login(lan.client))["agent-dashboard-sign-in"]
    capsys.readouterr()
    headers = dict(cookie_header("agent-dashboard-sign-in", token))
    headers["Origin"] = "http://localhost"
    response = lan.client.post("/logout", headers=headers,
                               follow_redirects=False)
    assert response.status_code == 303
    (line,) = auth_lines(capsys)
    assert line["event"] == "sign_out"
    assert line["account"] == "alice"
    assert line["client"] == "testclient"
    assert set(line) == {"at", "event", "account", "client"}


def test_login_origin_refused_logs_auth_line(lan, capsys):
    response = post_login(lan.client, origin="http://evil.test")
    assert response.status_code == 403
    (line,) = auth_lines(capsys)
    assert line["event"] == "origin_refused"
    assert line["path"] == "/login"
    assert line["origin"] == "http://evil.test"
    assert line["host"] == "localhost"
    assert line["fetch_site"] == ""
    assert line["reason"] == "origin"
    assert set(line) == {"at", "event", "path", "origin", "host",
                         "fetch_site", "reason"}


def test_logout_origin_refused_logs_auth_line(lan, capsys):
    token = set_cookies(post_login(lan.client))["agent-dashboard-sign-in"]
    capsys.readouterr()
    headers = dict(cookie_header("agent-dashboard-sign-in", token))
    headers["Origin"] = "http://evil.test"
    response = lan.client.post("/logout", headers=headers,
                               follow_redirects=False)
    assert response.status_code == 403
    (line,) = auth_lines(capsys)
    assert line["event"] == "origin_refused"
    assert line["path"] == "/logout"
    assert line["reason"] == "origin"


def test_login_insecure_context_logs_auth_line(lan, capsys):
    response = lan.lan_http.post(
        "/login",
        data={"account": "alice", "password": PASSWORD},
        headers={"Origin": "http://example.test"},
        follow_redirects=False,
    )
    assert response.status_code == 403
    (line,) = auth_lines(capsys)
    assert line["event"] == "origin_refused"
    assert line["path"] == "/login"
    assert line["reason"] == "secure_context"


def test_auth_lines_cut_typed_names_to_64_chars(lan, capsys):
    response = post_login(lan.client, account="a" * 100)
    assert response.status_code == 401
    (line,) = auth_lines(capsys)
    assert line["event"] == "sign_in_failed"
    assert line["account"] == "a" * 64


def test_auth_lines_are_single_line_json(lan, capsys):
    post_login(lan.client, account='mallory"test')
    raw = [line for line in capsys.readouterr().out.splitlines()
           if line.startswith("auth ")]
    assert len(raw) == 1
    parsed = json.loads(raw[0].removeprefix("auth "))
    assert parsed["account"] == 'mallory"test'


# --- stale cookie info box (spec 5.6, 8.1) ---------------------------------------


def test_stale_cookie_without_next_shows_short_info_box(lan):
    response = lan.client.get(
        "/login",
        headers=cookie_header("agent-dashboard-sign-in", "stale"),
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert 'data-tone="info"' in response.text
    assert "Your sign-in session has ended. Sign in again." in response.text
    assert "to continue where you were" not in response.text
    cleared = response.headers.get_list("set-cookie")
    assert any(h.startswith("__Host-agent-dashboard-sign-in=") for h in cleared)
    assert any(h.startswith("agent-dashboard-sign-in=") for h in cleared)
    assert any("Max-Age=0" in h for h in cleared)


def test_stale_cookie_with_next_shows_full_info_box_and_keeps_next(lan):
    response = lan.client.get(
        "/login?next=/instances/work/history",
        headers=cookie_header("agent-dashboard-sign-in", "stale"),
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert 'data-tone="info"' in response.text
    assert (
        "Your sign-in session has ended. "
        "Sign in again to continue where you were." in response.text
    )
    assert 'name="next" value="/instances/work/history"' in response.text
    cleared = response.headers.get_list("set-cookie")
    assert any(h.startswith("__Host-agent-dashboard-sign-in=") for h in cleared)
    assert any(h.startswith("agent-dashboard-sign-in=") for h in cleared)


def test_stale_host_cookie_clears_both_names(lan):
    response = lan.client.get(
        "/login",
        headers=cookie_header("__Host-agent-dashboard-sign-in", "stale"),
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert 'data-tone="info"' in response.text
    cleared = response.headers.get_list("set-cookie")
    assert any(h.startswith("__Host-agent-dashboard-sign-in=") for h in cleared)
    assert any(h.startswith("agent-dashboard-sign-in=") for h in cleared)


def test_fresh_login_page_has_no_stale_box(lan):
    response = lan.client.get("/login", follow_redirects=False)
    assert response.status_code == 200
    assert "Your sign-in session has ended" not in response.text


def test_stale_cookie_never_names_reason(lan):
    # Expiry, reset and remove all read as the same box.
    add_session("alice", "gone-idle", created=T0 - 100, last_seen=T0 - 3700)
    for token in ("gone-idle", "no-such-session"):
        response = lan.client.get(
            "/login",
            headers=cookie_header("agent-dashboard-sign-in", token),
            follow_redirects=False,
        )
        assert response.status_code == 200
        assert "Your sign-in session has ended. Sign in again." in response.text
        assert "expir" not in response.text.lower()
        assert "reset" not in response.text.lower()
        assert "remov" not in response.text.lower()


# --- /stream ends with the session (spec 5.6, 5.7, 12) --------------------------


def test_stream_refreshes_activity_while_session_lives(lan):
    token = set_cookies(post_login(lan.client))["agent-dashboard-sign-in"]
    headers = cookie_header("agent-dashboard-sign-in", token)
    assert last_seen_of("alice", token) == T0
    lan.clock.now = T0 + 91
    response = lan.client.get("/", headers=headers, follow_redirects=False)
    assert response.status_code == 200
    assert last_seen_of("alice", token) == T0 + 91


def test_stream_ends_after_cli_reset(tmp_path, monkeypatch):
    import asyncio

    from agent_dashboard import cli as cli_module

    env = make_lan(tmp_path, monkeypatch)
    add_account("alice", now=T0)
    token = set_cookies(post_login(env.client))["agent-dashboard-sign-in"]

    async def is_signed_in():
        def work():
            conn = account_store.open_store()
            try:
                return (
                    account_store.find_sign_in_session(
                        conn,
                        hashlib.sha256(token.encode()).digest(),
                        now=env.clock.now,
                        idle_timeout=env.settings.sign_in_idle_timeout,
                        max_age=env.settings.sign_in_max_age,
                    )
                    is not None
                )
            finally:
                conn.close()

        return await asyncio.to_thread(work)

    async def scenario():
        queue: asyncio.Queue = asyncio.Queue()

        class FakeFleet:
            def subscribe(self):
                return queue

            def unsubscribe(self, _queue):
                pass

            async def snapshot(self, poll_interval=None):
                return ([], None)

        async def connected():
            return False

        stream = fleet_event_stream(
            FakeFleet(), lambda rows, error: "TABLE",
            connected, None, is_signed_in=is_signed_in,
        )
        it = stream.__aiter__()
        assert (await it.__anext__()).startswith("retry:")
        assert (await it.__anext__()).startswith("event: fleet")
        # The CLI reset (a separate process in production) ends the session.
        cli_module.main(["account", "reset", "alice"])
        await queue.put(1)
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(it.__anext__(), timeout=2)
        await stream.aclose()

    asyncio.run(scenario())
    # The browser's reconnect then gets the gate's 401.
    refused = env.client.get(
        "/stream",
        headers=cookie_header("agent-dashboard-sign-in", token),
        follow_redirects=False,
    )
    assert refused.status_code == 401


def test_stream_ends_after_expiry_with_injected_clock(tmp_path, monkeypatch):
    import asyncio

    env = make_lan(tmp_path, monkeypatch)
    add_account("alice", now=T0)
    token = set_cookies(post_login(env.client))["agent-dashboard-sign-in"]

    async def is_signed_in():
        def work():
            conn = account_store.open_store()
            try:
                return (
                    account_store.find_sign_in_session(
                        conn,
                        hashlib.sha256(token.encode()).digest(),
                        now=env.clock.now,
                        idle_timeout=env.settings.sign_in_idle_timeout,
                        max_age=env.settings.sign_in_max_age,
                    )
                    is not None
                )
            finally:
                conn.close()

        return await asyncio.to_thread(work)

    async def scenario():
        queue: asyncio.Queue = asyncio.Queue()

        class FakeFleet:
            def subscribe(self):
                return queue

            def unsubscribe(self, _queue):
                pass

            async def snapshot(self, poll_interval=None):
                return ([], None)

        async def connected():
            return False

        stream = fleet_event_stream(
            FakeFleet(), lambda rows, error: "TABLE",
            connected, None, is_signed_in=is_signed_in,
        )
        it = stream.__aiter__()
        assert (await it.__anext__()).startswith("retry:")
        assert (await it.__anext__()).startswith("event: fleet")
        # Past the idle timeout the next fleet event ends the stream.
        env.clock.now = T0 + 1800.0 + 1
        await queue.put(1)
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(it.__anext__(), timeout=2)
        await stream.aclose()

    asyncio.run(scenario())


def test_cli_reset_while_app_holds_store_ends_session(tmp_path, monkeypatch):
    from agent_dashboard import cli as cli_module

    env = make_lan(tmp_path, monkeypatch)
    add_account("alice", now=T0)
    token = set_cookies(post_login(env.client))["agent-dashboard-sign-in"]
    headers = cookie_header("agent-dashboard-sign-in", token)
    assert env.client.get("/", headers=headers,
                           follow_redirects=False).status_code == 200
    # The app holds the store open while the CLI resets the account.
    held = account_store.open_store(now=T0)
    try:
        cli_module.main(["account", "reset", "alice"])
        refused = env.client.get("/", headers=headers, follow_redirects=False)
        assert refused.status_code == 303
        htmx = dict(headers)
        htmx["HX-Request"] = "true"
        htmx["HX-Current-URL"] = "http://localhost/instances/work"
        response = env.client.get("/", headers=htmx, follow_redirects=False)
        assert response.status_code == 401
        assert response.headers["hx-redirect"] == "/login?next=%2Finstances%2Fwork"
    finally:
        held.close()


def test_startup_cleanup_deletes_ended_sessions(tmp_path, monkeypatch):
    from agent_dashboard import account_store as store

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    conn = store.open_store(now=T0)
    try:
        store.create_account(conn, "alice", "controller", now=T0)
        digest = hashlib.sha256(b"ended-token").digest()
        store.create_sign_in_session(conn, "alice", digest, now=T0 - 3700)
        conn.execute(
            "UPDATE sign_in_sessions SET created_at = ?, last_seen_at = ?"
            " WHERE id_hash = ?",
            (T0 - 3700, T0 - 3700, digest),
        )
        conn.commit()
    finally:
        conn.close()
    # Opening the store in LAN mode deletes the ended row.
    conn = store.open_store(
        now=T0, idle_timeout=1800.0, max_age=43200.0
    )
    try:
        assert conn.execute("SELECT COUNT(*) FROM sign_in_sessions").fetchone()[0] == 0
    finally:
        conn.close()


# --- sign-in.js SSE close (spec 5.6, static asset) --------------------------------


def test_sign_in_js_checks_whoami_on_sse_close():
    from pathlib import Path

    text = (Path("src/agent_dashboard/static/sign-in.js")).read_text()
    assert "htmx:sseError" in text
    assert "htmx:sseClose" in text
    assert 'htmx.ajax("GET", "/whoami", {swap: "none"})' in text
    # At most one check at a time: a single request site behind a guard.
    assert text.count('htmx.ajax("GET"') == 1
    assert "checking" in text


def test_sign_in_js_included_only_in_lan_mode(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from agent_dashboard.app import create_app
    from agent_dashboard.config import Settings

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    lan_app = create_app(
        settings=Settings(lan_mode=True,
                          allowed_hosts=frozenset({"example.test"})),
        start_poller=False,
        clock=Clock(),
        password_hasher=CheapHasher(),
    )
    add_account("alice", now=T0)
    token = set_cookies(
        TestClient(lan_app, base_url="http://localhost").post(
            "/login",
            data={"account": "alice", "password": PASSWORD},
            headers={"Origin": "http://localhost"},
            follow_redirects=False,
        )
    )["agent-dashboard-sign-in"]
    body = TestClient(lan_app, base_url="http://localhost").get(
        "/", headers=cookie_header("agent-dashboard-sign-in", token)
    ).text
    assert '<script src="/static/sign-in.js" defer></script>' in body
    plain_app = create_app(settings=Settings(), start_poller=False)
    plain_body = TestClient(plain_app, base_url="http://localhost").get("/").text
    assert "sign-in.js" not in plain_body


# --- templates consume view models only (spec 8) ---


def test_templates_consume_view_models_only():
    import dataclasses
    from pathlib import Path

    from agent_dashboard.auth import Operator, SignInView

    assert dataclasses.is_dataclass(Operator)
    assert dataclasses.is_dataclass(SignInView)
    assert Operator.__dataclass_params__.frozen
    assert SignInView.__dataclass_params__.frozen
    templates = Path("src/agent_dashboard/templates")
    base = (templates / "base.html").read_text()
    login = (templates / "login.html").read_text()
    assert "request.state" not in base
    assert "request.state" not in login
    assert "operator.since" in base
    assert "operator.name" in base
    assert "operator.role" in base
    assert "view.notice_tone" in login
    assert "view.notice_text" in login
    assert "view.next" in login
    assert "view.account" in login
