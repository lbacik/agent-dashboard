import pytest

from agent_dashboard.config import (
    DEFAULT_EXEC_TIMEOUT,
    DEFAULT_MAX_CONCURRENT_EXECS,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_PORT,
    ensure_loopback,
    is_loopback,
    load_settings,
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
