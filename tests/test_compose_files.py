"""The container install's Compose files, read through ``docker compose config``.

``config`` only parses and interpolates the files; it never talks to the
daemon, so these tests need the Docker CLI with the Compose plugin but no
running Docker. The developer's own ``.env`` and shell variables are kept
out: every run gets a scratch env file and a minimal environment.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BASE = ROOT / "docker-compose.yml"
LAN = ROOT / "docker-compose.lan.yml"
DOCKERFILE = ROOT / "Dockerfile"

PASS_THROUGH = (
    "AGENT_DASHBOARD_LAN_MODE",
    "AGENT_DASHBOARD_TRUSTED_PROXIES",
    "AGENT_DASHBOARD_SIGN_IN_IDLE_TIMEOUT",
    "AGENT_DASHBOARD_SIGN_IN_MAX_AGE",
    "AGENT_DASHBOARD_CONTROL",
    "AGENT_DASHBOARD_CONTROL_EXEC_TIMEOUT",
)

LAN_ENV = {"VIRTUAL_HOST": "dashboard.example.lan", "PROXY_NETWORK": "proxy-net"}


def compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        proc = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


pytestmark = pytest.mark.skipif(
    not compose_available(), reason="docker compose CLI not available"
)


def compose_config(
    tmp_path: Path, *files: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    env_file = tmp_path / "test.env"
    env_file.write_text(
        "".join(f"{key}={value}\n" for key, value in (env or {}).items())
    )
    argv = ["docker", "compose", "--project-directory", str(ROOT)]
    for path in files:
        argv += ["-f", str(path)]
    argv += ["--env-file", str(env_file), "config", "--format", "json"]
    clean_env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/")}
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=60, env=clean_env, check=False
    )


def resolved_service(tmp_path: Path, *files: Path, env: dict[str, str] | None = None):
    proc = compose_config(tmp_path, *files, env=env)
    assert proc.returncode == 0, proc.stderr
    config = json.loads(proc.stdout)
    return config, config["services"]["dashboard"]


def published_ports(service: dict) -> list[tuple[str, str, int]]:
    return [
        (port.get("host_ip", ""), str(port["published"]), port["target"])
        for port in service["ports"]
    ]


# --- base file -------------------------------------------------------------


def test_base_publishes_on_loopback_by_default(tmp_path):
    _, service = resolved_service(tmp_path, BASE)
    assert published_ports(service) == [("127.0.0.1", "8000", 8000)]


def test_base_image_is_tagged_agent_dashboard(tmp_path):
    _, service = resolved_service(tmp_path, BASE)
    assert service["image"] == "agent-dashboard"


def test_base_keeps_state_on_a_named_volume(tmp_path):
    config, service = resolved_service(tmp_path, BASE)
    mounts = [
        (m["type"], m["source"], m["target"]) for m in service.get("volumes", [])
    ]
    assert ("volume", "state", "/state") in mounts
    assert "state" in config["volumes"]
    assert service["environment"]["XDG_STATE_HOME"] == "/state"


def test_base_is_hardened(tmp_path):
    _, service = resolved_service(tmp_path, BASE)
    assert service["read_only"] is True
    assert service["tmpfs"] == ["/tmp"]
    assert service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges:true"]


def test_base_keeps_the_non_loopback_bind_and_lan_mode_off(tmp_path):
    _, service = resolved_service(tmp_path, BASE)
    environment = service["environment"]
    assert environment["AGENT_DASHBOARD_HOST"] == "0.0.0.0"
    assert environment["AGENT_DASHBOARD_ALLOW_NON_LOOPBACK"] == "1"
    # Empty means "not set" to the dashboard: LAN mode stays off.
    assert environment["AGENT_DASHBOARD_LAN_MODE"] == ""


@pytest.mark.parametrize("name", PASS_THROUGH)
def test_base_passes_through_settings_empty_by_default(tmp_path, name):
    _, service = resolved_service(tmp_path, BASE)
    assert service["environment"][name] == ""


@pytest.mark.parametrize("name", PASS_THROUGH)
def test_base_passes_through_settings_from_env_file(tmp_path, name):
    _, service = resolved_service(tmp_path, BASE, env={name: "x"})
    assert service["environment"][name] == "x"


# --- LAN overlay -------------------------------------------------------------


def test_overlay_publishes_only_on_loopback(tmp_path):
    _, service = resolved_service(
        tmp_path, BASE, LAN, env={**LAN_ENV, "DASHBOARD_BIND": "0.0.0.0"}
    )
    assert published_ports(service) == [("127.0.0.1", "8000", 8000)]


def test_overlay_keeps_the_configured_host_port(tmp_path):
    _, service = resolved_service(tmp_path, BASE, LAN, env={**LAN_ENV, "DASHBOARD_PORT": "8080"})
    assert published_ports(service) == [("127.0.0.1", "8080", 8000)]


def test_overlay_turns_lan_mode_on_for_the_proxy(tmp_path):
    _, service = resolved_service(
        tmp_path, BASE, LAN, env={**LAN_ENV, "LETSENCRYPT_HOST": "dashboard.example.lan"}
    )
    environment = service["environment"]
    assert environment["AGENT_DASHBOARD_LAN_MODE"] == "on"
    assert environment["VIRTUAL_HOST"] == "dashboard.example.lan"
    assert environment["VIRTUAL_PORT"] == "8000"
    assert environment["LETSENCRYPT_HOST"] == "dashboard.example.lan"
    # The base file's settings stay in place under the overlay.
    assert environment["XDG_STATE_HOME"] == "/state"
    assert service["read_only"] is True


def test_overlay_joins_the_external_proxy_network(tmp_path):
    config, service = resolved_service(tmp_path, BASE, LAN, env=LAN_ENV)
    assert set(service["networks"]) == {"default", "proxy"}
    proxy = config["networks"]["proxy"]
    assert proxy["external"] is True
    assert proxy["name"] == "proxy-net"


@pytest.mark.parametrize("missing", ["VIRTUAL_HOST", "PROXY_NETWORK"])
def test_overlay_fails_clearly_without_a_required_variable(tmp_path, missing):
    env = {key: value for key, value in LAN_ENV.items() if key != missing}
    proc = compose_config(tmp_path, BASE, LAN, env=env)
    assert proc.returncode != 0
    assert f"set {missing} to" in proc.stderr


def test_env_example_works_with_the_base_file(tmp_path):
    env = {}
    for line in (ROOT / ".env.example").read_text().splitlines():
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            env[key] = value
    _, service = resolved_service(tmp_path, BASE, env=env)
    assert published_ports(service) == [("127.0.0.1", "8000", 8000)]
    assert service["environment"]["AGENT_DASHBOARD_LAN_MODE"] == ""


# --- Dockerfile --------------------------------------------------------------


def test_dockerfile_creates_state_owned_by_appuser_before_dropping_root():
    lines = [line.strip() for line in DOCKERFILE.read_text().splitlines()]
    user_line = lines.index("USER appuser")
    before = "\n".join(lines[:user_line])
    assert "mkdir -p /state" in before
    assert "chown appuser:appuser /state" in before


def test_dockerfile_does_not_buffer_stdout():
    # Startup lines (LAN mode, no account yet, migration) must reach
    # `docker logs` when written, not when the buffer happens to fill.
    assert 'ENV PYTHONUNBUFFERED=1' in DOCKERFILE.read_text()
