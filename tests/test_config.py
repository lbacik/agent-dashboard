import pytest

from agent_dashboard.config import (
    DEFAULT_EXEC_TIMEOUT,
    DEFAULT_MAX_CONCURRENT_EXECS,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_PORT,
    ensure_loopback,
    is_loopback,
    load_settings,
    parse_allowed_hosts,
    parse_truthy,
)


def test_defaults(monkeypatch):
    for key in (
        "AGENT_DASHBOARD_HOST",
        "AGENT_DASHBOARD_PORT",
        "AGENT_DASHBOARD_POLL_INTERVAL",
        "AGENT_DASHBOARD_EXEC_TIMEOUT",
        "AGENT_DASHBOARD_MAX_CONCURRENT_EXECS",
    ):
        monkeypatch.delenv(key, raising=False)
    settings = load_settings([])
    assert settings.host == "127.0.0.1"
    assert settings.port == DEFAULT_PORT
    assert settings.poll_interval == DEFAULT_POLL_INTERVAL
    assert settings.exec_timeout == DEFAULT_EXEC_TIMEOUT
    assert settings.max_concurrent_execs == DEFAULT_MAX_CONCURRENT_EXECS


def test_env_configuration(monkeypatch):
    monkeypatch.setenv("AGENT_DASHBOARD_HOST", "localhost")
    monkeypatch.setenv("AGENT_DASHBOARD_PORT", "9001")
    monkeypatch.setenv("AGENT_DASHBOARD_POLL_INTERVAL", "7")
    monkeypatch.setenv("AGENT_DASHBOARD_EXEC_TIMEOUT", "9")
    monkeypatch.setenv("AGENT_DASHBOARD_MAX_CONCURRENT_EXECS", "8")
    settings = load_settings([])
    assert settings.host == "localhost"
    assert settings.port == 9001
    assert settings.poll_interval == 7
    assert settings.exec_timeout == 9
    assert settings.max_concurrent_execs == 8


def test_cli_flags_beat_env(monkeypatch):
    monkeypatch.setenv("AGENT_DASHBOARD_PORT", "9001")
    monkeypatch.setenv("AGENT_DASHBOARD_POLL_INTERVAL", "7")
    settings = load_settings(["--port", "9002", "--poll-interval", "6"])
    assert settings.port == 9002
    assert settings.poll_interval == 6


def test_poll_interval_floor(monkeypatch):
    monkeypatch.delenv("AGENT_DASHBOARD_POLL_INTERVAL", raising=False)
    assert load_settings(["--poll-interval", "0.5"]).poll_interval == 2.0
    monkeypatch.setenv("AGENT_DASHBOARD_POLL_INTERVAL", "1")
    assert load_settings([]).poll_interval == 2.0


@pytest.mark.parametrize(
    "host", ["127.0.0.1", "127.0.0.2", "localhost", "LOCALHOST", "::1"]
)
def test_loopback_accepted(host):
    assert is_loopback(host) is True
    ensure_loopback(host)


@pytest.mark.parametrize(
    "host", ["0.0.0.0", "192.168.1.10", "10.0.0.5", "example.com", "", "258.1.1.1"]
)
def test_non_loopback_refused(host):
    assert is_loopback(host) is False
    with pytest.raises(ValueError):
        ensure_loopback(host)


def test_load_settings_refuses_non_loopback_cli():
    with pytest.raises(ValueError):
        load_settings(["--host", "0.0.0.0"])


def test_load_settings_refuses_non_loopback_env(monkeypatch):
    monkeypatch.setenv("AGENT_DASHBOARD_HOST", "192.168.1.10")
    with pytest.raises(ValueError):
        load_settings([])


def test_opt_in_env_allows_non_loopback(monkeypatch):
    monkeypatch.setenv("AGENT_DASHBOARD_HOST", "0.0.0.0")
    monkeypatch.setenv("AGENT_DASHBOARD_ALLOW_NON_LOOPBACK", "1")
    settings = load_settings([])
    assert settings.host == "0.0.0.0"
    assert settings.allow_non_loopback is True


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " 1 "])
def test_opt_in_env_truthy_variants(monkeypatch, value):
    monkeypatch.setenv("AGENT_DASHBOARD_HOST", "192.168.1.10")
    monkeypatch.setenv("AGENT_DASHBOARD_ALLOW_NON_LOOPBACK", value)
    assert load_settings([]).allow_non_loopback is True


@pytest.mark.parametrize("value", ["0", "false", "no", "", "maybe"])
def test_opt_in_env_falsy_still_refuses(monkeypatch, value):
    monkeypatch.setenv("AGENT_DASHBOARD_HOST", "192.168.1.10")
    monkeypatch.setenv("AGENT_DASHBOARD_ALLOW_NON_LOOPBACK", value)
    with pytest.raises(ValueError):
        load_settings([])


def test_opt_in_cli_flag_allows_non_loopback(monkeypatch):
    monkeypatch.delenv("AGENT_DASHBOARD_ALLOW_NON_LOOPBACK", raising=False)
    monkeypatch.delenv("AGENT_DASHBOARD_HOST", raising=False)
    settings = load_settings(["--host", "0.0.0.0", "--allow-non-loopback"])
    assert settings.host == "0.0.0.0"
    assert settings.allow_non_loopback is True


def test_ensure_loopback_opt_in():
    ensure_loopback("0.0.0.0", allow_non_loopback=True)
    ensure_loopback("192.168.1.10", allow_non_loopback=True)
    with pytest.raises(ValueError):
        ensure_loopback("192.168.1.10", allow_non_loopback=False)


def test_allowed_hosts_env_parsed(monkeypatch):
    monkeypatch.setenv("AGENT_DASHBOARD_ALLOWED_HOSTS", "raspberrypi, 192.168.1.50")
    settings = load_settings([])
    assert settings.allowed_hosts == frozenset({"raspberrypi", "192.168.1.50"})


def test_allowed_hosts_cli_beats_env(monkeypatch):
    monkeypatch.setenv("AGENT_DASHBOARD_ALLOWED_HOSTS", "from-env")
    settings = load_settings(["--allowed-hosts", "from-cli"])
    assert settings.allowed_hosts == frozenset({"from-cli"})


def test_allowed_hosts_defaults_empty(monkeypatch):
    monkeypatch.delenv("AGENT_DASHBOARD_ALLOWED_HOSTS", raising=False)
    assert load_settings([]).allowed_hosts == frozenset()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("*", frozenset()),
        ("*, raspberrypi", frozenset({"raspberrypi"})),
        ("", frozenset()),
        (None, frozenset()),
        ("Host.Example , 10.0.0.5", frozenset({"host.example", "10.0.0.5"})),
    ],
)
def test_parse_allowed_hosts_rejects_wildcard(raw, expected):
    assert parse_allowed_hosts(raw) == expected


@pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
def test_parse_truthy(value):
    assert parse_truthy(value) is True
    assert parse_truthy(value.upper()) is True


@pytest.mark.parametrize("value", [None, "", "0", "false", "no", "maybe"])
def test_parse_falsy(value):
    assert parse_truthy(value) is False
