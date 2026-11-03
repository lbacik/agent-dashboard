"""LAN mode settings, startup checks, the Origin rule and the migration.

Slice 1 of the LAN access map (spec sections 2, 4.1, 4.2 and 9, plus the
ADR 0008 migration). LAN mode never serves yet: ``--lan-mode on`` parses
but is a startup error until the "Turn on LAN mode" ticket removes it.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import time

import pytest
from fake_gateway import FakeGateway
from fastapi.testclient import TestClient
from test_web import fresh_snapshot, load_snapshot

from agent_dashboard import cli as cli_module
from agent_dashboard.app import create_app
from agent_dashboard.collector import run_cycle
from agent_dashboard.config import (
    Settings,
    build_parser,
    load_settings,
    parse_lan_mode,
    parse_seconds,
    parse_trusted_proxies,
)
from agent_dashboard.control import compute_control_mode
from agent_dashboard.fleet import FleetState
from agent_dashboard.gateway import ManagedInstance
from agent_dashboard.request_checks import client_address, is_origin_allowed

LAN_ENV_VARS = (
    "AGENT_DASHBOARD_LAN_MODE",
    "AGENT_DASHBOARD_TRUSTED_PROXIES",
    "AGENT_DASHBOARD_SIGN_IN_IDLE_TIMEOUT",
    "AGENT_DASHBOARD_SIGN_IN_MAX_AGE",
    "AGENT_DASHBOARD_DISABLE_HOST_GUARD",
    "AGENT_DASHBOARD_ALLOWED_HOSTS",
    "AGENT_DASHBOARD_HOST",
    "AGENT_DASHBOARD_ALLOW_NON_LOOPBACK",
    "AGENT_DASHBOARD_CONTROL",
)


@pytest.fixture(autouse=True)
def _clean_lan_env(monkeypatch):
    for var in LAN_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _isolated_audit(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))


# --- settings defaults (spec 2.1) -------------------------------------------


def test_lan_defaults():
    settings = load_settings([])
    assert settings.lan_mode is False
    assert settings.trusted_proxies == ()
    assert settings.sign_in_idle_timeout == 1800.0
    assert settings.sign_in_max_age == 43200.0
    assert settings.host_check is True


# --- lan_mode (spec 2.1) -----------------------------------------------------


@pytest.mark.parametrize("value", ["on", "ON", " on ", "1", "true", "TRUE", "yes"])
def test_lan_mode_truthy_parses_on(value):
    assert parse_lan_mode(value) is True


@pytest.mark.parametrize("value", ["off", "OFF", " off ", "0", "false", "no"])
def test_lan_mode_falsy_parses_off(value):
    assert parse_lan_mode(value) is False


@pytest.mark.parametrize("value", [None, "", "   "])
def test_lan_mode_blank_is_default_off(value):
    assert parse_lan_mode(value) is False


@pytest.mark.parametrize("value", ["maybe", "lan", "2", "onoff", "enabled"])
def test_lan_mode_typo_is_startup_error(value):
    with pytest.raises(ValueError, match="invalid lan-mode value"):
        parse_lan_mode(value)


def test_lan_mode_typo_names_the_value():
    with pytest.raises(ValueError) as exc:
        load_settings(["--lan-mode", "maybe"])
    assert "maybe" in str(exc.value)


def test_lan_mode_flag_beats_env(monkeypatch):
    monkeypatch.setenv("AGENT_DASHBOARD_LAN_MODE", "off")
    monkeypatch.setenv("AGENT_DASHBOARD_ALLOWED_HOSTS", "example.test")
    with pytest.raises(ValueError, match="not available in this build"):
        load_settings(["--lan-mode", "on"])


def test_lan_mode_env_beats_default(monkeypatch):
    monkeypatch.setenv("AGENT_DASHBOARD_LAN_MODE", "on")
    monkeypatch.setenv("AGENT_DASHBOARD_ALLOWED_HOSTS", "example.test")
    with pytest.raises(ValueError, match="not available in this build"):
        load_settings([])


@pytest.mark.parametrize("value", ["off", "0", "false", "no"])
def test_lan_mode_off_values_serve(monkeypatch, value):
    monkeypatch.setenv("AGENT_DASHBOARD_LAN_MODE", value)
    assert load_settings([]).lan_mode is False


# --- trusted_proxies (spec 2.1) ----------------------------------------------


def test_trusted_proxies_default_empty():
    assert load_settings([]).trusted_proxies == ()


def test_trusted_proxies_parses_addresses_and_cidrs():
    parsed = parse_trusted_proxies("10.0.0.1, 192.168.0.0/16, ::1, fd00::/8")
    assert parsed == (
        ipaddress.ip_network("10.0.0.1", strict=False),
        ipaddress.ip_network("192.168.0.0/16", strict=False),
        ipaddress.ip_network("::1", strict=False),
        ipaddress.ip_network("fd00::/8", strict=False),
    )


@pytest.mark.parametrize("value", ["", "   ", "*, , *", "*", " , "])
def test_trusted_proxies_blank_and_star_dropped(value):
    assert parse_trusted_proxies(value) == ()


def test_trusted_proxies_star_dropped_among_entries():
    parsed = parse_trusted_proxies("*, 10.0.0.1, , *")
    assert parsed == (ipaddress.ip_network("10.0.0.1", strict=False),)


def test_trusted_proxies_invalid_names_the_entry():
    with pytest.raises(ValueError) as exc:
        parse_trusted_proxies("10.0.0.1, not-a-proxy")
    assert "not-a-proxy" in str(exc.value)


def test_trusted_proxies_invalid_from_env_names_it(monkeypatch):
    monkeypatch.setenv("AGENT_DASHBOARD_TRUSTED_PROXIES", "bogus-entry")
    with pytest.raises(ValueError) as exc:
        load_settings([])
    assert "bogus-entry" in str(exc.value)


def test_trusted_proxies_flag_beats_env(monkeypatch):
    monkeypatch.setenv("AGENT_DASHBOARD_TRUSTED_PROXIES", "10.9.9.9")
    settings = load_settings(["--trusted-proxies", "10.1.2.3"])
    assert settings.trusted_proxies == (
        ipaddress.ip_network("10.1.2.3", strict=False),
    )


def test_trusted_proxies_env_beats_default(monkeypatch):
    monkeypatch.setenv("AGENT_DASHBOARD_TRUSTED_PROXIES", "10.1.2.3")
    settings = load_settings([])
    assert settings.trusted_proxies == (
        ipaddress.ip_network("10.1.2.3", strict=False),
    )


def test_trusted_proxies_read_outside_lan_mode():
    # Read in both modes, used only in LAN mode.
    settings = load_settings(["--trusted-proxies", "10.0.0.0/8"])
    assert settings.lan_mode is False
    assert settings.trusted_proxies == (
        ipaddress.ip_network("10.0.0.0/8", strict=False),
    )


# --- sign-in timeouts (spec 2.1) ----------------------------------------------


def test_sign_in_idle_timeout_default():
    assert load_settings([]).sign_in_idle_timeout == 1800.0


def test_sign_in_max_age_default():
    assert load_settings([]).sign_in_max_age == 43200.0


def test_sign_in_idle_timeout_flag_beats_env(monkeypatch):
    monkeypatch.setenv("AGENT_DASHBOARD_SIGN_IN_IDLE_TIMEOUT", "600")
    assert load_settings(["--sign-in-idle-timeout", "900"]).sign_in_idle_timeout == 900


def test_sign_in_idle_timeout_env_beats_default(monkeypatch):
    monkeypatch.setenv("AGENT_DASHBOARD_SIGN_IN_IDLE_TIMEOUT", "600")
    assert load_settings([]).sign_in_idle_timeout == 600


def test_sign_in_max_age_flag_beats_env(monkeypatch):
    monkeypatch.setenv("AGENT_DASHBOARD_SIGN_IN_MAX_AGE", "3600")
    assert load_settings(["--sign-in-max-age", "7200"]).sign_in_max_age == 7200


def test_sign_in_max_age_env_beats_default(monkeypatch):
    monkeypatch.setenv("AGENT_DASHBOARD_SIGN_IN_MAX_AGE", "3600")
    assert load_settings([]).sign_in_max_age == 3600


def test_sign_in_idle_timeout_minimum_is_300():
    assert load_settings(["--sign-in-idle-timeout", "300"]).sign_in_idle_timeout == 300


@pytest.mark.parametrize("value", ["299", "299.9", "0", "-5"])
def test_sign_in_idle_timeout_below_300_is_error(value):
    with pytest.raises(ValueError, match="sign-in idle timeout must be at least"):
        load_settings(["--sign-in-idle-timeout", value])


def test_sign_in_idle_timeout_nan_is_error():
    with pytest.raises(ValueError, match="sign-in idle timeout must be at least"):
        load_settings(["--sign-in-idle-timeout", "nan"])


def test_sign_in_idle_timeout_not_a_number_is_error():
    with pytest.raises(ValueError, match="invalid sign-in idle timeout"):
        load_settings(["--sign-in-idle-timeout", "soon"])


def test_sign_in_max_age_may_equal_idle_timeout():
    settings = load_settings(
        ["--sign-in-idle-timeout", "600", "--sign-in-max-age", "600"]
    )
    assert settings.sign_in_max_age == 600


def test_sign_in_max_age_at_7_days():
    settings = load_settings(["--sign-in-max-age", "604800"])
    assert settings.sign_in_max_age == 604800


@pytest.mark.parametrize("idle,max_age", [("600", "599"), ("1800", "1799.9")])
def test_sign_in_max_age_below_idle_is_error(idle, max_age):
    with pytest.raises(ValueError, match="sign-in max age must be between"):
        load_settings(["--sign-in-idle-timeout", idle, "--sign-in-max-age", max_age])


def test_sign_in_max_age_above_7_days_is_error():
    with pytest.raises(ValueError, match="sign-in max age must be between"):
        load_settings(["--sign-in-max-age", "604801"])


def test_sign_in_max_age_nan_is_error():
    with pytest.raises(ValueError, match="sign-in max age must be between"):
        load_settings(["--sign-in-max-age", "nan"])


def test_sign_in_max_age_not_a_number_is_error():
    with pytest.raises(ValueError, match="invalid sign-in max age"):
        load_settings(["--sign-in-max-age", "forever"])


def test_sign_in_max_age_env_below_idle_is_error(monkeypatch):
    monkeypatch.setenv("AGENT_DASHBOARD_SIGN_IN_MAX_AGE", "600")
    with pytest.raises(ValueError, match="sign-in max age must be between"):
        load_settings([])


def test_parse_seconds_blank_is_default():
    assert parse_seconds(None, name="thing", default=7.0) == 7.0
    assert parse_seconds("  ", name="thing", default=7.0) == 7.0


# --- host_check (spec 2.1) ------------------------------------------------------


def test_host_check_defaults_on():
    assert load_settings([]).host_check is True


def test_host_check_off_only_for_exact_1(monkeypatch):
    monkeypatch.setenv("AGENT_DASHBOARD_DISABLE_HOST_GUARD", "1")
    assert load_settings([]).host_check is False


@pytest.mark.parametrize("value", ["0", "true", "TRUE", "yes", "", "2", " 1 "])
def test_host_check_stays_on_for_other_values(monkeypatch, value):
    monkeypatch.setenv("AGENT_DASHBOARD_DISABLE_HOST_GUARD", value)
    assert load_settings([]).host_check is True


def test_host_help_names_lan_mode():
    parser = build_parser()
    action = next(a for a in parser._actions if "--host" in a.option_strings)
    assert action.help is not None
    assert "LAN mode" in action.help
    assert "--allow-non-loopback" in action.help


def test_allow_non_loopback_ignored_in_lan_mode(monkeypatch):
    # A non-loopback bind without the flag is accepted in LAN mode: the
    # bind is never refused there (the temporary unavailable error proves
    # parsing got past the bind check).
    monkeypatch.setenv("AGENT_DASHBOARD_ALLOWED_HOSTS", "example.test")
    with pytest.raises(ValueError, match="not available in this build"):
        load_settings(["--lan-mode", "on", "--host", "192.168.1.10"])


# --- startup checks (spec 2.2) ---------------------------------------------------


def test_loopback_refusal_outside_lan_mode():
    with pytest.raises(ValueError, match="refusing to bind"):
        load_settings(["--host", "0.0.0.0"])


def test_loopback_refusal_outside_lan_mode_env(monkeypatch):
    monkeypatch.setenv("AGENT_DASHBOARD_HOST", "192.168.1.10")
    with pytest.raises(ValueError, match="refusing to bind"):
        load_settings([])


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "10.0.0.5", "::"])
def test_any_bind_accepted_in_lan_mode(monkeypatch, host):
    monkeypatch.setenv("AGENT_DASHBOARD_ALLOWED_HOSTS", "example.test")
    with pytest.raises(ValueError) as exc:
        load_settings(["--lan-mode", "on", "--host", host])
    assert "not available in this build" in str(exc.value)
    assert "refusing to bind" not in str(exc.value)


def test_lan_mode_on_is_startup_error(monkeypatch):
    monkeypatch.setenv("AGENT_DASHBOARD_ALLOWED_HOSTS", "example.test")
    with pytest.raises(ValueError, match="LAN mode is not available in this build"):
        load_settings(["--lan-mode", "on"])


def test_lan_mode_needs_allowed_hosts(monkeypatch):
    with pytest.raises(ValueError) as exc:
        load_settings(["--lan-mode", "on"])
    assert "LAN mode needs AGENT_DASHBOARD_ALLOWED_HOSTS" in str(exc.value)


def test_lan_mode_allowed_hosts_message_names_escape(monkeypatch):
    with pytest.raises(ValueError) as exc:
        load_settings(["--lan-mode", "on"])
    message = str(exc.value)
    assert "comma-separated" in message
    assert "AGENT_DASHBOARD_DISABLE_HOST_GUARD=1" in message


def test_lan_mode_no_allowed_hosts_needed_when_host_check_off(monkeypatch):
    monkeypatch.setenv("AGENT_DASHBOARD_DISABLE_HOST_GUARD", "1")
    with pytest.raises(ValueError, match="not available in this build"):
        load_settings(["--lan-mode", "on"])


def test_lan_mode_allowed_hosts_extra_parsing(monkeypatch):
    # A lone "*" parses to an empty allow-list, so it still fails the check.
    monkeypatch.setenv("AGENT_DASHBOARD_ALLOWED_HOSTS", "*")
    with pytest.raises(ValueError, match="LAN mode needs"):
        load_settings(["--lan-mode", "on"])


def _stderr_of(monkeypatch, capsys, argv):
    with pytest.raises(SystemExit) as exc:
        cli_module.main(argv)
    assert exc.value.code == 2
    return capsys.readouterr().err


def test_startup_errors_exit_2_with_prefix(monkeypatch, capsys):
    err = _stderr_of(monkeypatch, capsys, ["--lan-mode", "maybe"])
    assert err.startswith("agent-dashboard: error: ")
    assert "invalid lan-mode value" in err


def test_lan_mode_unavailable_exits_2(monkeypatch, capsys):
    monkeypatch.setenv("AGENT_DASHBOARD_ALLOWED_HOSTS", "example.test")
    err = _stderr_of(monkeypatch, capsys, ["--lan-mode", "on"])
    assert err.startswith("agent-dashboard: error: ")
    assert "LAN mode is not available in this build" in err


def test_trusted_proxy_error_exits_2(monkeypatch, capsys):
    err = _stderr_of(
        monkeypatch, capsys, ["--trusted-proxies", "bogus-entry"]
    )
    assert err.startswith("agent-dashboard: error: ")
    assert "bogus-entry" in err


def test_idle_timeout_error_exits_2(monkeypatch, capsys):
    err = _stderr_of(monkeypatch, capsys, ["--sign-in-idle-timeout", "10"])
    assert err.startswith("agent-dashboard: error: ")
    assert "sign-in idle timeout" in err


def test_max_age_error_exits_2(monkeypatch, capsys):
    err = _stderr_of(monkeypatch, capsys, ["--sign-in-max-age", "5"])
    assert err.startswith("agent-dashboard: error: ")
    assert "sign-in max age" in err


def test_loopback_refusal_exits_2(monkeypatch, capsys):
    err = _stderr_of(monkeypatch, capsys, ["--host", "0.0.0.0"])
    assert err.startswith("agent-dashboard: error: ")
    assert "non-loopback" in err


def test_allowed_hosts_check_exits_2(monkeypatch, capsys):
    err = _stderr_of(monkeypatch, capsys, ["--lan-mode", "on"])
    assert err.startswith("agent-dashboard: error: ")
    assert "LAN mode needs AGENT_DASHBOARD_ALLOWED_HOSTS" in err


# --- dashboard control mode and migration (spec 2.4) ------------------------------


def test_control_off_reason_unchanged():
    mode = compute_control_mode(control_enabled=False, host="127.0.0.1")
    assert mode.available is False
    assert mode.reason == "control is off (AGENT_DASHBOARD_CONTROL=off, read-only mode)"


def test_control_off_wins_over_non_loopback():
    mode = compute_control_mode(control_enabled=False, host="192.168.1.5")
    assert mode.available is False
    assert mode.reason.startswith("control is off")


def test_non_loopback_reason_points_to_lan_mode():
    mode = compute_control_mode(control_enabled=True, host="192.168.1.5")
    assert mode.available is False
    assert mode.reason == (
        "the dashboard is bound to a non-loopback address (192.168.1.5); "
        "Control actions beyond loopback need LAN mode "
        "(AGENT_DASHBOARD_LAN_MODE=on)"
    )


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "127.0.0.2"])
def test_loopback_stays_available(host):
    assert compute_control_mode(host=host).available is True


def test_lan_mode_bind_never_a_reason():
    assert compute_control_mode(host="0.0.0.0", lan_mode=True).available is True
    assert compute_control_mode(host="192.168.1.5", lan_mode=True).available is True


def test_host_check_does_not_enable_controls():
    # The escape hatch only turns off the Host check, not this function.
    mode = compute_control_mode(host="0.0.0.0", lan_mode=False)
    assert mode.available is False
    assert "LAN mode" in (mode.reason or "")


def test_disable_host_guard_non_loopback_is_read_only_with_migration(
    monkeypatch, capsys
):
    seen: dict = {}

    def fake_run(app, **kwargs):
        seen.update(kwargs)
        assert app.state.control_mode.available is False
        assert "LAN mode" in (app.state.control_mode.reason or "")

    monkeypatch.setattr("uvicorn.run", fake_run)
    monkeypatch.setenv("AGENT_DASHBOARD_DISABLE_HOST_GUARD", "1")
    monkeypatch.setenv("AGENT_DASHBOARD_ALLOW_NON_LOOPBACK", "1")
    cli_module.main(["--host", "0.0.0.0"])
    assert seen["proxy_headers"] is False
    out = capsys.readouterr().out
    assert (
        "agent-dashboard: Control actions are unavailable: "
        "AGENT_DASHBOARD_DISABLE_HOST_GUARD=1 no longer enables them on a "
        "non-loopback bind. Turn on LAN mode instead "
        '(README: "Migrating from AGENT_DASHBOARD_DISABLE_HOST_GUARD")'
    ) in out


def test_no_migration_line_on_loopback(monkeypatch, capsys):
    seen: dict = {}

    def fake_run(app, **kwargs):
        seen.update(kwargs)

    monkeypatch.setattr("uvicorn.run", fake_run)
    monkeypatch.setenv("AGENT_DASHBOARD_DISABLE_HOST_GUARD", "1")
    cli_module.main([])
    assert seen["proxy_headers"] is False
    assert "DISABLE_HOST_GUARD" not in capsys.readouterr().out


def test_no_migration_line_when_host_check_on(monkeypatch, capsys):
    seen: dict = {}

    def fake_run(app, **kwargs):
        seen.update(kwargs)

    monkeypatch.setattr("uvicorn.run", fake_run)
    cli_module.main(["--host", "0.0.0.0", "--allow-non-loopback"])
    assert seen["proxy_headers"] is False
    assert "DISABLE_HOST_GUARD" not in capsys.readouterr().out


def test_lan_mode_startup_line_wired(monkeypatch, capsys):
    seen: dict = {}
    settings = Settings(
        host="192.168.1.5",
        port=8001,
        lan_mode=True,
        host_check=True,
        trusted_proxies=(
            ipaddress.ip_network("10.0.0.0/8", strict=False),
        ),
    )

    def fake_run(app, **kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(cli_module, "load_settings", lambda argv=None: settings)
    monkeypatch.setattr("uvicorn.run", fake_run)
    cli_module.main([])
    out = capsys.readouterr().out
    assert (
        "agent-dashboard: LAN mode is on: every request needs a sign-in session "
        "(bind 192.168.1.5:8001, Host check on, trusted proxies 10.0.0.0/8)"
    ) in out
    assert seen["proxy_headers"] is False


def test_lan_mode_startup_line_no_proxies_and_check_off(monkeypatch, capsys):
    settings = Settings(lan_mode=True, host_check=False)

    def fake_run(app, **kwargs):
        pass

    monkeypatch.setattr(cli_module, "load_settings", lambda argv=None: settings)
    monkeypatch.setattr("uvicorn.run", fake_run)
    cli_module.main([])
    out = capsys.readouterr().out
    assert "Host check off" in out
    assert "trusted proxies none" in out


def test_uvicorn_proxy_headers_false(monkeypatch):
    seen: dict = {}

    def fake_run(app, **kwargs):
        seen.update(kwargs)

    monkeypatch.setattr("uvicorn.run", fake_run)
    cli_module.main([])
    assert seen["proxy_headers"] is False
    assert seen["workers"] == 1
    assert seen["host"] == "127.0.0.1"
    assert seen["loop"] == "asyncio"


# --- Origin rule (spec 4.2) -------------------------------------------------------


@pytest.mark.parametrize(
    "origin,host",
    [
        ("http://localhost", "localhost"),
        ("https://localhost", "localhost"),
        ("http://localhost:8000", "localhost:8000"),
        ("https://localhost:8000", "localhost:8000"),
        ("HTTP://LOCALHOST", "localhost"),
        ("http://LOCALHOST:8000", "localhost:8000"),
        ("http://localhost:80", "localhost"),
        ("http://localhost", "localhost:80"),
        ("https://localhost:443", "localhost"),
        ("https://localhost", "localhost:443"),
        ("http://127.0.0.1:8000", "127.0.0.1:8000"),
        ("http://[::1]:8000", "[::1]:8000"),
        ("http://[::1]", "[::1]"),
        ("https://[::1]", "[::1]"),
        ("http://[::1]:80", "[::1]"),
        ("http://example.test", "example.test"),
        ("https://example.test:8443", "example.test:8443"),
    ],
)
def test_origin_allowed(origin, host):
    assert is_origin_allowed(origin, host) is True


@pytest.mark.parametrize(
    "origin,host",
    [
        (None, "localhost"),
        ("", "localhost"),
        ("   ", "localhost"),
        ("null", "localhost"),
        ("NULL", "localhost"),
        ("Null", "localhost"),
        ("ftp://localhost", "localhost"),
        ("file://localhost", "localhost"),
        ("ws://localhost", "localhost"),
        ("http://localhost:9999", "localhost"),
        ("http://localhost", "localhost:9999"),
        ("http://localhost:8000", "localhost:8001"),
        ("http://example.test", "other.test"),
        ("http://EXAMPLE.test", "other.test"),
        ("http://localhost", "127.0.0.1"),
        ("http://localhost:80", "localhost:443"),
        ("https://localhost", "localhost:80"),
        ("http://[::1]:8000", "[::1]:8001"),
        ("http://[::1]", "127.0.0.1"),
        ("http://localhost", ""),
        ("http://localhost", None),
        ("http://localhost/extra", "localhost"),
        ("http://localhost?x=1", "localhost"),
        ("http://localhost#frag", "localhost"),
        ("http://user@localhost", "localhost"),
        ("http://localhost:notaport", "localhost"),
        ("http://localhost:0", "localhost"),
        ("http://localhost", "localhost:notaport"),
        ("http://", "localhost"),
        ("http://:8000", ":8000"),
        ("localhost:8000", "localhost:8000"),
        ("http://localhost:8000", "localhost:"),
        ("http://::1", "[::1]"),
    ],
)
def test_origin_refused(origin, host):
    assert is_origin_allowed(origin, host) is False


def test_origin_ignores_forwarded_headers():
    assert (
        is_origin_allowed("http://localhost", "localhost") is True
    )


def test_host_without_port_uses_origin_default():
    # Explicit default ports match a bare Host; a wrong port does not.
    assert is_origin_allowed("http://localhost:80", "localhost") is True
    assert is_origin_allowed("https://localhost:443", "localhost") is True
    assert is_origin_allowed("http://localhost:443", "localhost") is False
    assert is_origin_allowed("https://localhost:80", "localhost") is False


def make_origin_client():
    now = time.time()
    state = FleetState()
    cids = {"work": "abc000"}
    gateway = FakeGateway(
        [ManagedInstance("work", "abc000", "running")]
    )
    gateway.set_snapshot(
        "abc000", fresh_snapshot(load_snapshot("snapshot-attempt-in-progress.json"), now)
    )
    asyncio.run(run_cycle(state, gateway, max_concurrent_execs=4, now=now))
    app = create_app(
        state=state,
        settings=Settings(poll_interval=5.0),
        gateway=gateway,
        start_poller=False,
    )
    return TestClient(app, base_url="http://localhost"), gateway, cids


def review_post(client, headers):
    return client.post(
        "/instances/work/control/review", data={"action": "stop"}, headers=headers
    )


def test_control_routes_accept_https_origin():
    client, gateway, _ = make_origin_client()
    response = review_post(
        client, {"Origin": "https://localhost", "HX-Request": "true"}
    )
    assert response.status_code == 200
    assert gateway.control_calls == []


def test_control_routes_accept_https_origin_explicit_port():
    # Unit-level: an explicit https port matches a Host with the same port.
    assert is_origin_allowed("https://localhost:8443", "localhost:8443") is True
    assert is_origin_allowed("https://localhost:8443", "localhost") is False


def test_control_routes_accept_explicit_default_port():
    client, gateway, _ = make_origin_client()
    response = review_post(
        client, {"Origin": "http://localhost:80", "HX-Request": "true"}
    )
    assert response.status_code == 200
    assert gateway.control_calls == []


@pytest.mark.parametrize(
    "origin",
    ["http://localhost:9999", "null", "ftp://localhost", "https://evil.test"],
)
def test_control_refused_origin_writes_one_auth_line(origin, capsys):
    client, gateway, _ = make_origin_client()
    response = review_post(client, {"Origin": origin, "HX-Request": "true"})
    assert response.status_code == 403
    assert "missing or mismatched Origin header" in response.text
    assert gateway.control_calls == []
    lines = [
        line for line in capsys.readouterr().out.splitlines() if line.startswith("auth ")
    ]
    assert len(lines) == 1
    payload = json.loads(lines[0].removeprefix("auth "))
    assert payload["event"] == "origin_refused"
    for field in ("at", "path", "origin", "host", "fetch_site", "reason"):
        assert field in payload, field
    assert payload["path"] == "/instances/work/control/review"
    assert payload["origin"] == origin
    assert payload["host"] == "localhost"
    assert payload["reason"] == "origin"


def test_control_missing_origin_writes_auth_line(capsys):
    client, gateway, _ = make_origin_client()
    response = review_post(client, {"HX-Request": "true"})
    assert response.status_code == 403
    lines = [
        line for line in capsys.readouterr().out.splitlines() if line.startswith("auth ")
    ]
    assert len(lines) == 1
    payload = json.loads(lines[0].removeprefix("auth "))
    assert payload["event"] == "origin_refused"
    assert payload["origin"] == ""
    assert gateway.control_calls == []


def test_control_ipv6_origin_accepted():
    now = time.time()
    state = FleetState()
    gateway = FakeGateway([ManagedInstance("work", "abc000", "running")])
    gateway.set_snapshot(
        "abc000", fresh_snapshot(load_snapshot("snapshot-attempt-in-progress.json"), now)
    )
    asyncio.run(run_cycle(state, gateway, max_concurrent_execs=4, now=now))
    app = create_app(
        state=state,
        settings=Settings(poll_interval=5.0, allowed_hosts=frozenset({"::1"})),
        gateway=gateway,
        start_poller=False,
    )
    client = TestClient(app, base_url="http://[::1]:8000")
    response = client.post(
        "/instances/work/control/review",
        data={"action": "stop"},
        headers={
            "Origin": "http://[::1]:8000",
            "HX-Request": "true",
        },
    )
    assert response.status_code == 200
    assert gateway.control_calls == []


def test_control_forwarded_headers_never_read():
    client, gateway, _ = make_origin_client()
    # X-Forwarded-Host pointing elsewhere changes nothing: the real Host wins.
    response = review_post(
        client,
        {
            "Origin": "http://localhost",
            "HX-Request": "true",
            "X-Forwarded-Host": "evil.test",
            "X-Forwarded-Proto": "https",
        },
    )
    assert response.status_code == 200
    assert gateway.control_calls == []
    # And they cannot rescue a bad Origin either.
    bad = review_post(
        client,
        {
            "Origin": "http://evil.test",
            "HX-Request": "true",
            "X-Forwarded-Host": "localhost",
            "X-Forwarded-Proto": "http",
        },
    )
    assert bad.status_code == 403


def test_control_fetch_site_refusal_writes_no_auth_line(capsys):
    client, gateway, _ = make_origin_client()
    response = review_post(
        client,
        {
            "Origin": "http://localhost",
            "HX-Request": "true",
            "Sec-Fetch-Site": "cross-site",
        },
    )
    assert response.status_code == 403
    assert "Sec-Fetch-Site" in response.text
    assert gateway.control_calls == []
    lines = [
        line for line in capsys.readouterr().out.splitlines() if line.startswith("auth ")
    ]
    assert lines == []


def test_control_htmx_refusal_writes_no_auth_line(capsys):
    client, gateway, _ = make_origin_client()
    response = review_post(client, {"Origin": "http://localhost"})
    assert response.status_code == 403
    assert "HX-Request" in response.text
    lines = [
        line for line in capsys.readouterr().out.splitlines() if line.startswith("auth ")
    ]
    assert lines == []


def test_submit_refused_origin_writes_auth_line(capsys):
    client, gateway, cids = make_origin_client()
    response = client.post(
        "/instances/work/control/submit",
        data={
            "action": "stop",
            "container_id": cids["work"],
            "repository": "example-owner/example-repo",
            "request_id": "dash-0123456789abcdef",
        },
        headers={"Origin": "http://evil.test", "HX-Request": "true"},
    )
    assert response.status_code == 403
    lines = [
        line for line in capsys.readouterr().out.splitlines() if line.startswith("auth ")
    ]
    assert len(lines) == 1
    payload = json.loads(lines[0].removeprefix("auth "))
    assert payload["event"] == "origin_refused"
    assert payload["path"] == "/instances/work/control/submit"
    assert gateway.control_calls == []


def test_status_refused_origin_writes_auth_line(capsys):
    client, gateway, cids = make_origin_client()
    response = client.post(
        "/instances/work/control/status",
        data={
            "action": "stop",
            "container_id": cids["work"],
            "repository": "example-owner/example-repo",
            "request_id": "dash-0123456789abcdef",
        },
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 403
    lines = [
        line for line in capsys.readouterr().out.splitlines() if line.startswith("auth ")
    ]
    assert len(lines) == 1
    payload = json.loads(lines[0].removeprefix("auth "))
    assert payload["event"] == "origin_refused"
    assert payload["path"] == "/instances/work/control/status"


# --- client address (spec 4.4) ---------------------------------------------------


def _nets(*values):
    return tuple(ipaddress.ip_network(v, strict=False) for v in values)


def test_client_address_ignores_xff_when_peer_untrusted():
    assert (
        client_address("192.0.2.10", "10.0.0.5", _nets("10.0.0.0/8")) == "192.0.2.10"
    )


def test_client_address_ignores_xff_when_missing():
    assert client_address("10.0.0.5", None, _nets("10.0.0.0/8")) == "10.0.0.5"


def test_client_address_takes_first_untrusted_from_right():
    assert (
        client_address(
            "10.0.0.5", "203.0.113.7, 10.0.0.9, 10.0.0.8", _nets("10.0.0.0/8")
        )
        == "203.0.113.7"
    )


def test_client_address_skips_trusted_tail():
    assert (
        client_address("10.0.0.1", "203.0.113.7, 10.0.0.2", _nets("10.0.0.0/8"))
        == "203.0.113.7"
    )


def test_client_address_all_trusted_takes_leftmost():
    assert (
        client_address("10.0.0.1", "10.0.0.3, 10.0.0.2", _nets("10.0.0.0/8"))
        == "10.0.0.3"
    )


def test_client_address_invalid_entry_ends_walk_with_peer():
    assert (
        client_address("10.0.0.1", "203.0.113.7, nonsense", _nets("10.0.0.0/8"))
        == "10.0.0.1"
    )


def test_client_address_blank_entry_ends_walk_with_peer():
    assert (
        client_address("10.0.0.1", "203.0.113.7, ", _nets("10.0.0.0/8")) == "10.0.0.1"
    )


def test_client_address_ipv6():
    assert (
        client_address("fd00::1", "2001:db8::7, fd00::2", _nets("fd00::/8"))
        == "2001:db8::7"
    )
