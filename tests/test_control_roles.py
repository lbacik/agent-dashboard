"""Roles in Control actions and the Control audit (LAN spec section 7).

In LAN mode an Operator's Role decides whether they may issue Control
actions, and every Control audit entry records who issued it. The startup
dashboard control mode comes first, then the Role; a ``viewer`` sees the
"Controls unavailable" line and gets 403 with a ``not_permitted`` refusal
on review/submit (nothing on status). Every entry carries ``operator``: the
account name in LAN mode, null outside it. The session ID and the client
address are never recorded.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time

import pytest
from fake_gateway import FakeGateway
from fastapi.testclient import TestClient
from test_passwords import CheapHasher
from test_signin import Clock, add_account, add_session
from test_web import fresh_snapshot, load_snapshot

from agent_dashboard import account_store
from agent_dashboard.app import create_app
from agent_dashboard.collector import run_cycle
from agent_dashboard.config import Settings
from agent_dashboard.control import (
    VIEWER_REASON,
    compute_control_mode,
    request_control_mode,
)
from agent_dashboard.control_audit import audit_path
from agent_dashboard.fleet import FleetState
from agent_dashboard.gateway import ManagedInstance


@pytest.fixture(autouse=True)
def _isolated_audit(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))


HOST = "example.test"
BASE = f"http://{HOST}"
GOOD_HEADERS = {"Origin": BASE, "HX-Request": "true"}

RID = "dash-0123456789abcdef"
REPO = "example-owner/example-repo"


def make_lan_client(monkeypatch, tmp_path, snapshots, *, now=None):
    """A LAN mode app with a fleet, a clock and a cheap hasher."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    moment = time.time() if now is None else now
    state = FleetState()
    cids = {name: f"{0xABC000 + index:x}" for index, name in enumerate(snapshots)}
    gateway = FakeGateway(
        [ManagedInstance(name, cid, "running") for name, cid in cids.items()]
    )
    for name, snap in snapshots.items():
        gateway.set_snapshot(cids[name], fresh_snapshot(copy.deepcopy(snap), moment))
    asyncio.run(run_cycle(state, gateway, max_concurrent_execs=4, now=moment))
    clock = Clock(moment)
    settings = Settings(
        lan_mode=True, allowed_hosts=frozenset({HOST}), poll_interval=5.0
    )
    app = create_app(
        state=state,
        settings=settings,
        gateway=gateway,
        start_poller=False,
        clock=clock,
        password_hasher=CheapHasher(),
    )
    return TestClient(app, base_url=BASE), state, gateway, cids, clock, moment


def lan_client(monkeypatch, tmp_path, name="work"):
    return make_lan_client(
        monkeypatch, tmp_path, {name: load_snapshot("snapshot-attempt-in-progress.json")}
    )


def cookie_headers(token, extra=None):
    headers = dict(GOOD_HEADERS)
    headers["Cookie"] = f"agent-dashboard-sign-in={token}"
    if extra:
        headers.update(extra)
    return headers


def audit_entries() -> list[dict]:
    path = audit_path()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def accepted_reply_body() -> bytes:
    return (
        f"repository: {REPO}\nrequest_id: {RID}\nsequence: 8\n"
        "acknowledgement: accepted\neffect: ok\n"
    ).encode()


# --- availability per request (spec 7.1) --------------------------------------


def test_request_mode_control_off_first():
    mode = compute_control_mode(control_enabled=False, host="127.0.0.1")
    out = request_control_mode(mode, role="viewer", lan_mode=True)
    assert out.available is False
    assert out.reason == "control is off (AGENT_DASHBOARD_CONTROL=off, read-only mode)"


def test_request_mode_non_loopback_bind_second():
    mode = compute_control_mode(
        control_enabled=True, host="192.168.1.5", lan_mode=False
    )
    assert mode.available is False
    out = request_control_mode(mode, role="controller", lan_mode=False)
    assert out.available is False
    assert "non-loopback address (192.168.1.5)" in (out.reason or "")


def test_request_mode_viewer_in_lan_mode():
    mode = compute_control_mode(
        control_enabled=True, host="0.0.0.0", lan_mode=True
    )
    assert mode.available is True
    out = request_control_mode(mode, role="viewer", lan_mode=True)
    assert out.available is False
    assert out.reason == VIEWER_REASON
    assert out.reason == "your account can view but not issue Control actions"


def test_request_mode_controller_in_lan_mode_stays_available():
    mode = compute_control_mode(
        control_enabled=True, host="0.0.0.0", lan_mode=True
    )
    out = request_control_mode(mode, role="controller", lan_mode=True)
    assert out.available is True
    assert out.reason is None


def test_request_mode_viewer_outside_lan_mode_is_ignored():
    mode = compute_control_mode(control_enabled=True, host="127.0.0.1")
    out = request_control_mode(mode, role="viewer", lan_mode=False)
    assert out.available is True


def test_request_mode_missing_role_in_lan_mode_is_available():
    mode = compute_control_mode(
        control_enabled=True, host="0.0.0.0", lan_mode=True
    )
    assert request_control_mode(mode, role=None, lan_mode=True).available is True


# --- the viewer's console (spec 7.1, 8.4) --------------------------------------


def test_viewer_console_shows_only_the_line(monkeypatch, tmp_path):
    client, _, _, _, _, moment = lan_client(monkeypatch, tmp_path)
    add_account("vera", role="viewer", now=moment)
    token = "viewer-console-token"
    add_session("vera", token, now=moment)
    response = client.get("/instances/work/control", headers=cookie_headers(token))
    assert response.status_code == 200
    assert (
        "Controls unavailable: your account can view but not issue Control actions"
        in response.text
    )
    # As for every other unavailable reason: the line only, no timeline.
    assert "Commands (last 10" not in response.text
    assert "ctl-composer" not in response.text


def test_controller_console_keeps_the_composer(monkeypatch, tmp_path):
    client, _, _, _, _, moment = lan_client(monkeypatch, tmp_path)
    add_account("carl", role="controller", now=moment)
    token = "controller-console-token"
    add_session("carl", token, now=moment)
    response = client.get("/instances/work/control", headers=cookie_headers(token))
    assert response.status_code == 200
    assert "Controls unavailable" not in response.text
    assert "ctl-composer" in response.text


def test_top_bar_chip_shows_dashboard_mode_not_role(monkeypatch, tmp_path):
    client, _, _, _, _, moment = lan_client(monkeypatch, tmp_path)
    add_account("vera", role="viewer", now=moment)
    token = "viewer-chip-token"
    add_session("vera", token, now=moment)
    response = client.get("/", headers=cookie_headers(token))
    assert response.status_code == 200
    assert "Control <strong>on</strong>" in response.text


# --- a viewer cannot issue Control actions (spec 7.2) ----------------------------


def test_viewer_review_is_refused_with_not_permitted(monkeypatch, tmp_path):
    client, _, gateway, _, _, moment = lan_client(monkeypatch, tmp_path)
    add_account("vera", role="viewer", now=moment)
    token = "viewer-review-token"
    add_session("vera", token, now=moment)
    response = client.post(
        "/instances/work/control/review",
        data={"action": "stop"},
        headers=cookie_headers(token),
    )
    assert response.status_code == 403
    assert (
        "Controls unavailable: your account can view but not issue Control actions"
        in response.text
    )
    assert gateway.control_calls == []
    entries = audit_entries()
    assert len(entries) == 1
    assert entries[0]["outcome"] == "refused"
    assert entries[0]["reason"] == "not_permitted"
    assert entries[0]["operator"] == "vera"


def test_viewer_submit_is_refused_with_not_permitted(monkeypatch, tmp_path):
    client, _, gateway, cids, _, moment = lan_client(monkeypatch, tmp_path)
    add_account("vera", role="viewer", now=moment)
    token = "viewer-submit-token"
    add_session("vera", token, now=moment)
    response = client.post(
        "/instances/work/control/submit",
        data={
            "action": "stop",
            "container_id": cids["work"],
            "repository": REPO,
            "request_id": RID,
        },
        headers=cookie_headers(token),
    )
    assert response.status_code == 403
    assert (
        "Controls unavailable: your account can view but not issue Control actions"
        in response.text
    )
    assert gateway.control_calls == []
    entries = audit_entries()
    assert len(entries) == 1
    assert entries[0]["outcome"] == "refused"
    assert entries[0]["reason"] == "not_permitted"
    # The Role is checked with request protection, before the form fields
    # are parsed, like every other protection refusal.
    assert entries[0]["request_id"] is None
    assert entries[0]["operator"] == "vera"


def test_viewer_status_is_refused_and_writes_nothing(monkeypatch, tmp_path):
    client, _, gateway, cids, _, moment = lan_client(monkeypatch, tmp_path)
    add_account("vera", role="viewer", now=moment)
    token = "viewer-status-token"
    add_session("vera", token, now=moment)
    response = client.post(
        "/instances/work/control/status",
        data={
            "action": "stop",
            "container_id": cids["work"],
            "repository": REPO,
            "request_id": RID,
        },
        headers=cookie_headers(token),
    )
    assert response.status_code == 403
    assert (
        "Controls unavailable: your account can view but not issue Control actions"
        in response.text
    )
    assert gateway.control_calls == []
    assert audit_entries() == []


def test_downgrade_between_review_and_submit_is_refused(monkeypatch, tmp_path):
    client, _, gateway, cids, _, moment = lan_client(monkeypatch, tmp_path)
    add_account("carl", role="controller", now=moment)
    token = "downgrade-token"
    add_session("carl", token, now=moment)
    headers = cookie_headers(token)
    review = client.post(
        "/instances/work/control/review", data={"action": "stop"}, headers=headers
    )
    assert review.status_code == 200
    # The Role is read again at submit: a downgrade in between is refused.
    conn = account_store.open_store(now=moment)
    try:
        account_store.set_role(conn, "carl", "viewer")
    finally:
        conn.close()
    response = client.post(
        "/instances/work/control/submit",
        data={
            "action": "stop",
            "container_id": cids["work"],
            "repository": REPO,
            "request_id": RID,
        },
        headers=headers,
    )
    assert response.status_code == 403
    assert (
        "Controls unavailable: your account can view but not issue Control actions"
        in response.text
    )
    assert gateway.control_calls == []
    entries = audit_entries()
    assert len(entries) == 1
    assert entries[0]["outcome"] == "refused"
    assert entries[0]["reason"] == "not_permitted"
    assert entries[0]["operator"] == "carl"


# --- the Control audit carries the operator (spec 7.3) --------------------------


def test_controller_entries_carry_operator(monkeypatch, tmp_path):
    client, _, gateway, cids, _, moment = lan_client(monkeypatch, tmp_path)
    add_account("carl", role="controller", now=moment)
    token = "controller-submit-token"
    add_session("carl", token, now=moment)
    gateway.set_control(cids["work"], accepted_reply_body(), b"", 0)
    response = client.post(
        "/instances/work/control/submit",
        data={
            "action": "stop",
            "container_id": cids["work"],
            "repository": REPO,
            "request_id": RID,
        },
        headers=cookie_headers(token),
    )
    assert response.status_code == 200
    entries = audit_entries()
    assert [entry["outcome"] for entry in entries] == ["submitted", "accepted"]
    for entry in entries:
        assert entry["operator"] == "carl"


def test_rejected_outcome_carries_operator(monkeypatch, tmp_path):
    client, _, gateway, cids, _, moment = lan_client(monkeypatch, tmp_path)
    add_account("carl", role="controller", now=moment)
    token = "controller-rejected-token"
    add_session("carl", token, now=moment)
    gateway.set_control(cids["work"], b"", b"rejected: nope", 1)
    response = client.post(
        "/instances/work/control/submit",
        data={
            "action": "stop",
            "container_id": cids["work"],
            "repository": REPO,
            "request_id": RID,
        },
        headers=cookie_headers(token),
    )
    assert response.status_code == 200
    entries = audit_entries()
    assert [entry["outcome"] for entry in entries] == ["submitted", "rejected"]
    for entry in entries:
        assert entry["operator"] == "carl"


def test_controller_refusal_carries_operator(monkeypatch, tmp_path):
    client, _, _, _, _, moment = lan_client(monkeypatch, tmp_path)
    add_account("carl", role="controller", now=moment)
    token = "controller-refusal-token"
    add_session("carl", token, now=moment)
    response = client.post(
        "/instances/work/control/review",
        data={"action": "explode"},
        headers=cookie_headers(token),
    )
    assert response.status_code == 422
    entries = audit_entries()
    assert len(entries) == 1
    assert entries[0]["reason"] == "validation"
    assert entries[0]["operator"] == "carl"


def test_entries_carry_null_operator_outside_lan_mode(monkeypatch, tmp_path):
    from test_control_submit import GOOD_HEADERS as PLAIN_HEADERS
    from test_control_submit import live_client

    client, _, gateway, cids = live_client()
    gateway.set_control(cids["work"], accepted_reply_body(), b"", 0)
    response = client.post(
        "/instances/work/control/submit",
        data={
            "action": "stop",
            "container_id": cids["work"],
            "repository": REPO,
            "request_id": RID,
        },
        headers=dict(PLAIN_HEADERS),
    )
    assert response.status_code == 200
    entries = audit_entries()
    assert [entry["outcome"] for entry in entries] == ["submitted", "accepted"]
    for entry in entries:
        assert entry["operator"] is None


def test_viewer_keeps_commands_and_track_gets(monkeypatch, tmp_path):
    client, _, _, cids, _, moment = lan_client(monkeypatch, tmp_path)
    add_account("vera", role="viewer", now=moment)
    token = "viewer-gets-token"
    add_session("vera", token, now=moment)
    headers = cookie_headers(token)
    commands = client.get("/instances/work/control/commands", headers=headers)
    assert commands.status_code == 200
    tracked = client.get(
        "/instances/work/control/track",
        params={
            "action": "stop",
            "container_id": cids["work"],
            "repository": REPO,
            "request_id": RID,
            "sequence": "12",
        },
        headers=headers,
    )
    assert tracked.status_code == 200
    # The GETs read the snapshot only: no Control audit entry.
    assert audit_entries() == []


@pytest.mark.parametrize("role", ["viewer", "controller"])
def test_entries_never_record_session_or_client(monkeypatch, tmp_path, role):
    client, _, gateway, cids, _, moment = lan_client(monkeypatch, tmp_path)
    add_account("ollie", role=role, now=moment)
    token = "leak-check-token"
    add_session("ollie", token, now=moment)
    headers = cookie_headers(token)
    client.post("/instances/work/control/review", data={"action": "stop"}, headers=headers)
    gateway.set_control(cids["work"], accepted_reply_body(), b"", 0)
    client.post(
        "/instances/work/control/submit",
        data={
            "action": "stop",
            "container_id": cids["work"],
            "repository": REPO,
            "request_id": RID,
        },
        headers=headers,
    )
    entries = audit_entries()
    assert entries, "expected refused/submitted/outcome entries"
    for entry in entries:
        for key in entry:
            lowered = key.lower()
            assert "session" not in lowered, key
            assert "client" not in lowered, key
            assert "address" not in lowered, key
        blob = json.dumps(entry)
        assert token not in blob
        assert hashlib.sha256(token.encode()).hexdigest() not in blob
