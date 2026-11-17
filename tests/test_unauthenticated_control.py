"""Control without sign-in on a non-loopback bind (ADR 0015).

Outside LAN mode a non-loopback bind is read-only unless the operator sets
``AGENT_DASHBOARD_UNAUTHENTICATED_CONTROL=on``. With it, Control actions
are offered to anyone who can reach the port: the top bar says so, startup
prints a warning, and every Control audit entry gains ``client``, the
direct TCP peer address. The setting cannot be combined with LAN mode,
does not replace ``--allow-non-loopback``, and does not need the ``Host``
check. On a loopback bind it changes nothing.
"""

from __future__ import annotations

import pytest
from test_control_console import control_body, make_control_client
from test_control_submit import (
    GOOD_HEADERS,
    audit_entries,
    base_form,
    make_submit_client,
    submit,
)
from test_web import load_snapshot

from agent_dashboard import cli as cli_module
from agent_dashboard.config import Settings, load_settings
from agent_dashboard.control import compute_control_mode
from agent_dashboard.control_audit import (
    outcome_entry,
    refused_entry,
    submitted_entry,
)

REPO = "example-owner/example-repo"
RID = "dash-0123456789abcdef"
FIXTURE = "snapshot-attempt-in-progress.json"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    for name in (
        "AGENT_DASHBOARD_UNAUTHENTICATED_CONTROL",
        "AGENT_DASHBOARD_LAN_MODE",
        "AGENT_DASHBOARD_ALLOW_NON_LOOPBACK",
        "AGENT_DASHBOARD_DISABLE_HOST_GUARD",
        "AGENT_DASHBOARD_ALLOWED_HOSTS",
        "AGENT_DASHBOARD_HOST",
    ):
        monkeypatch.delenv(name, raising=False)


def open_settings(**overrides) -> Settings:
    """The rpi5 shape: 0.0.0.0, Host check off, Control without sign-in."""
    values = {
        "poll_interval": 5.0,
        "host": "0.0.0.0",
        "allow_non_loopback": True,
        "host_check": False,
        "unauthenticated_control": True,
    }
    values.update(overrides)
    return Settings(**values)


# --- the setting (config) -----------------------------------------------------


def test_default_is_off():
    assert load_settings([]).unauthenticated_control is False


@pytest.mark.parametrize("value", ["on", "1", "true", "yes", "ON"])
def test_flag_turns_it_on(value):
    settings = load_settings(["--unauthenticated-control", value])
    assert settings.unauthenticated_control is True


@pytest.mark.parametrize("value", ["off", "0", "false", "no"])
def test_env_turns_it_off(monkeypatch, value):
    monkeypatch.setenv("AGENT_DASHBOARD_UNAUTHENTICATED_CONTROL", value)
    assert load_settings([]).unauthenticated_control is False


def test_env_turns_it_on(monkeypatch):
    monkeypatch.setenv("AGENT_DASHBOARD_UNAUTHENTICATED_CONTROL", "on")
    assert load_settings([]).unauthenticated_control is True


def test_flag_wins_over_env(monkeypatch):
    monkeypatch.setenv("AGENT_DASHBOARD_UNAUTHENTICATED_CONTROL", "on")
    settings = load_settings(["--unauthenticated-control", "off"])
    assert settings.unauthenticated_control is False


def test_invalid_value_is_a_startup_error():
    with pytest.raises(ValueError, match="invalid unauthenticated-control value"):
        load_settings(["--unauthenticated-control", "maybe"])


def test_lan_mode_and_the_setting_are_a_startup_error():
    with pytest.raises(ValueError) as excinfo:
        load_settings(
            [
                "--lan-mode",
                "on",
                "--allowed-hosts",
                "dashboard.example.lan",
                "--unauthenticated-control",
                "on",
            ]
        )
    message = str(excinfo.value)
    assert "AGENT_DASHBOARD_UNAUTHENTICATED_CONTROL" in message
    assert "LAN mode" in message


def test_the_setting_does_not_allow_a_non_loopback_bind():
    # --allow-non-loopback keeps its own meaning.
    with pytest.raises(ValueError, match="non-loopback"):
        load_settings(["--host", "0.0.0.0", "--unauthenticated-control", "on"])


def test_the_setting_does_not_need_the_host_check(monkeypatch):
    monkeypatch.setenv("AGENT_DASHBOARD_DISABLE_HOST_GUARD", "1")
    settings = load_settings(
        [
            "--host",
            "0.0.0.0",
            "--allow-non-loopback",
            "--unauthenticated-control",
            "on",
        ]
    )
    assert settings.unauthenticated_control is True
    assert settings.host_check is False


# --- dashboard control mode ---------------------------------------------------


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5"])
def test_non_loopback_bind_with_the_setting_is_available(host):
    mode = compute_control_mode(host=host, unauthenticated_control=True)
    assert mode.available is True
    assert mode.reason is None
    assert mode.unauthenticated is True


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_bind_with_the_setting_changes_nothing(host):
    mode = compute_control_mode(host=host, unauthenticated_control=True)
    assert mode.available is True
    assert mode.unauthenticated is False


def test_control_off_still_wins():
    mode = compute_control_mode(
        control_enabled=False, host="0.0.0.0", unauthenticated_control=True
    )
    assert mode.available is False
    assert mode.reason.startswith("control is off")
    assert mode.unauthenticated is False


def test_non_loopback_reason_names_both_ways_out():
    mode = compute_control_mode(host="192.168.1.5")
    assert mode.available is False
    assert mode.unauthenticated is False
    assert mode.reason == (
        "the dashboard is bound to a non-loopback address (192.168.1.5); "
        "Control actions beyond loopback need LAN mode "
        "(AGENT_DASHBOARD_LAN_MODE=on), or Control without sign-in "
        "(AGENT_DASHBOARD_UNAUTHENTICATED_CONTROL=on)"
    )


# --- UI -----------------------------------------------------------------------


def test_console_offers_the_composer():
    client, _, _ = make_control_client({"work": load_snapshot(FIXTURE)}, open_settings())
    body = control_body(client).text
    assert "Controls unavailable" not in body
    assert "ctl-composer" in body


def test_top_bar_says_no_sign_in():
    client, _, _ = make_control_client({"work": load_snapshot(FIXTURE)}, open_settings())
    page = client.get("/").text
    assert "Control <strong>on · no sign-in</strong>" in page


def test_top_bar_unchanged_on_loopback():
    client, _, _ = make_control_client(
        {"work": load_snapshot(FIXTURE)},
        open_settings(host="127.0.0.1", allow_non_loopback=False),
    )
    page = client.get("/").text
    assert "Control <strong>on</strong>" in page
    assert "no sign-in" not in page


# --- Control audit ------------------------------------------------------------


def test_entry_builders_add_client_only_when_given():
    common = {
        "instance": "work",
        "request_id": RID,
        "container_id": "abc000",
        "expected_repository": REPO,
        "action": "stop",
    }
    assert "client" not in submitted_entry(**common)
    assert "client" not in outcome_entry(**common, outcome="accepted")
    assert "client" not in refused_entry(instance="work", reason="validation", error="x")
    assert submitted_entry(**common, client="192.168.1.20")["client"] == "192.168.1.20"
    assert (
        outcome_entry(**common, outcome="accepted", client="192.168.1.20")["client"]
        == "192.168.1.20"
    )
    assert (
        refused_entry(
            instance="work", reason="validation", error="x", client="192.168.1.20"
        )["client"]
        == "192.168.1.20"
    )


def accepted_reply() -> bytes:
    return (
        f"repository: {REPO}\nrequest_id: {RID}\nsequence: 3\n"
        "acknowledgement: accepted\neffect: ok\n"
    ).encode()


def test_submit_records_the_client_address():
    client, _, gateway, cids = make_submit_client(
        {"work": load_snapshot(FIXTURE)}, open_settings()
    )
    gateway.set_control(cids["work"], accepted_reply(), b"", 0)
    response = submit(client, "work", base_form(cids))
    assert response.status_code == 200
    entries = audit_entries()
    assert [entry["outcome"] for entry in entries] == ["submitted", "accepted"]
    for entry in entries:
        assert entry["operator"] is None
        # TestClient's peer address.
        assert entry["client"] == "testclient"


def test_refusal_records_the_client_address():
    client, _, _, cids = make_submit_client(
        {"work": load_snapshot(FIXTURE)}, open_settings()
    )
    response = submit(client, "work", base_form(cids, action="explode"))
    assert response.status_code == 422
    entries = audit_entries()
    assert [entry["outcome"] for entry in entries] == ["refused"]
    assert entries[0]["client"] == "testclient"


def test_loopback_audit_has_no_client():
    client, _, gateway, cids = make_submit_client(
        {"work": load_snapshot(FIXTURE)},
        open_settings(host="127.0.0.1", allow_non_loopback=False),
    )
    gateway.set_control(cids["work"], accepted_reply(), b"", 0)
    response = submit(client, "work", base_form(cids), headers=dict(GOOD_HEADERS))
    assert response.status_code == 200
    entries = audit_entries()
    assert entries
    for entry in entries:
        assert "client" not in entry


# --- startup (cli) ------------------------------------------------------------


def test_startup_warns_and_skips_the_migration_line(monkeypatch, capsys):
    seen: dict = {}

    def fake_run(app, **kwargs):
        seen.update(kwargs)
        assert app.state.control_mode.available is True
        assert app.state.control_mode.unauthenticated is True

    monkeypatch.setattr("uvicorn.run", fake_run)
    monkeypatch.setenv("AGENT_DASHBOARD_DISABLE_HOST_GUARD", "1")
    monkeypatch.setenv("AGENT_DASHBOARD_ALLOW_NON_LOOPBACK", "1")
    monkeypatch.setenv("AGENT_DASHBOARD_UNAUTHENTICATED_CONTROL", "on")
    cli_module.main(["--host", "0.0.0.0", "--port", "8000"])
    out = capsys.readouterr().out
    assert (
        "agent-dashboard: Control actions are on without sign-in: anyone who "
        "can reach 0.0.0.0:8000 can issue them "
        "(AGENT_DASHBOARD_UNAUTHENTICATED_CONTROL=on, Host check off)"
    ) in out
    assert "no longer enables them" not in out


def test_no_warning_on_loopback(monkeypatch, capsys):
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: None)
    monkeypatch.setenv("AGENT_DASHBOARD_UNAUTHENTICATED_CONTROL", "on")
    cli_module.main([])
    assert "without sign-in" not in capsys.readouterr().out


def test_lan_mode_with_the_setting_exits_2(monkeypatch, capsys):
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: None)
    monkeypatch.setenv("AGENT_DASHBOARD_UNAUTHENTICATED_CONTROL", "on")
    with pytest.raises(SystemExit) as excinfo:
        cli_module.main(["--lan-mode", "on", "--allowed-hosts", "dash.example.lan"])
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert err.startswith("agent-dashboard: error: ")
    assert "AGENT_DASHBOARD_UNAUTHENTICATED_CONTROL" in err
