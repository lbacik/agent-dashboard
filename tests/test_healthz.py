"""Process liveness probe: GET /healthz in both modes, behind the Host check.

``/healthz`` answers 200 ``ok`` (``text/plain``) without a sign-in session
in either mode. In LAN mode the gate exempts exactly ``/healthz``;
look-alikes stay gated. The ``Host`` allow-list still applies, whatever
the middleware order. The probe makes no gateway call and writes no
Control audit entry and no ``auth`` line.
"""

from __future__ import annotations

import json

from fake_gateway import FakeGateway
from fastapi.testclient import TestClient

from agent_dashboard.app import create_app
from agent_dashboard.config import Settings
from agent_dashboard.control_audit import audit_path
from agent_dashboard.fleet import FleetState


def make_plain_app():
    gateway = FakeGateway()
    app = create_app(
        state=FleetState(),
        settings=Settings(),
        gateway=gateway,
        start_poller=False,
    )
    return app, gateway


def make_lan_app(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    gateway = FakeGateway()
    app = create_app(
        state=FleetState(),
        settings=Settings(lan_mode=True, allowed_hosts=frozenset({"example.test"})),
        gateway=gateway,
        start_poller=False,
    )
    return app, gateway


def auth_lines(capsys):
    return [
        json.loads(line.removeprefix("auth "))
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("auth ")
    ]


def test_healthz_outside_lan_mode_returns_ok():
    app, _ = make_plain_app()
    response = TestClient(app, base_url="http://localhost").get("/healthz")
    assert response.status_code == 200
    assert response.text == "ok"
    assert response.headers["content-type"].startswith("text/plain")


def test_healthz_in_lan_mode_returns_ok_without_session(tmp_path, monkeypatch):
    app, _ = make_lan_app(tmp_path, monkeypatch)
    for client in (
        TestClient(app, base_url="http://localhost"),
        TestClient(app, base_url="http://example.test"),
    ):
        response = client.get("/healthz", follow_redirects=False)
        assert response.status_code == 200
        assert response.text == "ok"
        assert response.headers["content-type"].startswith("text/plain")


def test_healthz_head_answers_200_without_session(tmp_path, monkeypatch):
    app, _ = make_lan_app(tmp_path, monkeypatch)
    response = TestClient(app, base_url="http://localhost").head(
        "/healthz", follow_redirects=False
    )
    assert response.status_code == 200


def test_healthz_in_lan_mode_other_paths_stay_gated(tmp_path, monkeypatch):
    app, _ = make_lan_app(tmp_path, monkeypatch)
    client = TestClient(app, base_url="http://localhost")
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2F"
    htmx = client.get(
        "/",
        headers={"HX-Request": "true", "HX-Current-URL": "http://localhost/"},
        follow_redirects=False,
    )
    assert htmx.status_code == 401
    assert htmx.headers["hx-redirect"] == "/login?next=%2F"


def test_healthz_in_lan_mode_lookalikes_stay_gated(tmp_path, monkeypatch):
    app, _ = make_lan_app(tmp_path, monkeypatch)
    client = TestClient(app, base_url="http://localhost")
    for path in ("/healthz/", "/healthz/x", "/healthzx", "/HEALTHZ"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"].startswith("/login?next="), path


def test_healthz_disallowed_host_is_403_outside_lan_mode():
    app, _ = make_plain_app()
    response = TestClient(app, base_url="http://evil.test").get(
        "/healthz", follow_redirects=False
    )
    assert response.status_code == 403
    assert response.text == "forbidden: unexpected Host header"


def test_healthz_disallowed_host_is_403_in_lan_mode(tmp_path, monkeypatch):
    # The gate lets /healthz through without a session, so the Host check
    # answers 403 whatever the middleware order (see #205).
    app, _ = make_lan_app(tmp_path, monkeypatch)
    response = TestClient(app, base_url="http://evil.test").get(
        "/healthz", follow_redirects=False
    )
    assert response.status_code == 403
    assert response.text == "forbidden: unexpected Host header"


def test_healthz_disallowed_host_is_403_with_reversed_middleware(
    tmp_path, monkeypatch
):
    # Pins the 403 structurally: with the gate inside the Host check (the
    # #205 order) the refusal still comes from the Host check, because the
    # exemption delegates down the stack instead of answering early.
    app, _ = make_lan_app(tmp_path, monkeypatch)
    app.user_middleware.reverse()
    assert TestClient(app, base_url="http://evil.test").get(
        "/healthz", follow_redirects=False
    ).status_code == 403
    assert TestClient(app, base_url="http://localhost").get(
        "/healthz", follow_redirects=False
    ).status_code == 200


def test_healthz_with_unknown_session_cookie_needs_no_session(
    tmp_path, monkeypatch, capsys
):
    # A cookie matching no session is never resolved: the exemption comes
    # before any store read, so the probe writes no `auth` line either.
    app, _ = make_lan_app(tmp_path, monkeypatch)
    response = TestClient(app, base_url="http://localhost").get(
        "/healthz",
        headers={"Cookie": "agent-dashboard-sign-in=stale-unknown-session"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert response.text == "ok"
    assert auth_lines(capsys) == []


def test_healthz_makes_no_gateway_call(tmp_path, monkeypatch):
    plain_app, plain_gateway = make_plain_app()
    TestClient(plain_app, base_url="http://localhost").get("/healthz")
    assert plain_gateway.list_calls == 0
    assert plain_gateway.telemetry_calls == []
    lan_app, lan_gateway = make_lan_app(tmp_path, monkeypatch)
    TestClient(lan_app, base_url="http://localhost").get("/healthz")
    assert lan_gateway.list_calls == 0
    assert lan_gateway.telemetry_calls == []


def test_healthz_writes_no_audit_entry_and_no_auth_line(tmp_path, monkeypatch, capsys):
    app, _ = make_lan_app(tmp_path, monkeypatch)
    response = TestClient(app, base_url="http://localhost").get("/healthz")
    assert response.status_code == 200
    assert not audit_path().exists()
    assert auth_lines(capsys) == []
