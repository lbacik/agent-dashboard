"""Offline Read for stopped contract instances (gateway argv and parsing).

A stopped contract instance is never started: the dashboard re-inspects it
(``docker inspect``) and runs one throwaway helper (``docker run --rm``)
with no network, the instance's volumes mounted read-only and its exec
user. This module pins the three argv the helper path can produce
(inspect, run, cleanup), the validation that runs before the run argv is
built, and the inspect parser that yields the running state, the finish
time, the image ID and the ``type=volume`` mounts (bind mounts skipped).
"""

import asyncio
import json

import pytest

from agent_dashboard.gateway import (
    AGENTCTL_PATH,
    OFFLINE_READ_MARKER_LABEL,
    ControlTimeoutError,
    DockerGateway,
    GatewayError,
    OfflineInspect,
    OfflineMount,
    argv_inspect_offline,
    argv_offline_cleanup,
    argv_offline_status,
    make_offline_helper_name,
    parse_inspect_offline,
    validate_offline_destination,
    validate_offline_helper_name,
    validate_offline_image,
    validate_offline_volume_name,
)

IMAGE = "sha256:" + "ab" * 32
OTHER_IMAGE = "sha256:" + "cd" * 32
MOUNTS = (
    OfflineMount("data-vol", "/data"),
    OfflineMount("state-vol", "/state"),
)


def _inspect_entry(**overrides):
    entry = {
        "Id": "abc123",
        "Image": IMAGE,
        "State": {
            "Status": "exited",
            "Running": False,
            "Paused": False,
            "FinishedAt": "2026-11-06T09:58:12.123456789Z",
        },
        "Mounts": [
            {"Type": "volume", "Name": "data-vol", "Destination": "/data"},
            {"Type": "volume", "Name": "state-vol", "Destination": "/state"},
            {"Type": "bind", "Source": "/host/logs", "Destination": "/logs"},
            {"Type": "tmpfs", "Destination": "/run"},
        ],
    }
    entry.update(overrides)
    return json.dumps([entry]).encode()


# --- inspect argv ------------------------------------------------------------


def test_inspect_argv_pinned():
    assert argv_inspect_offline("abc123") == ["docker", "inspect", "abc123"]


def test_inspect_argv_rejects_non_hex_ids():
    for bad in ("", "web-1", "../x", "abc123; rm -rf /", "z" * 65):
        with pytest.raises(ValueError):
            argv_inspect_offline(bad)


# --- inspect parsing ---------------------------------------------------------


def test_parse_inspect_yields_state_finish_image_and_volume_mounts():
    info = parse_inspect_offline(_inspect_entry())
    assert info == OfflineInspect(
        running=False,
        finished_at="2026-11-06T09:58:12.123456789Z",
        image=IMAGE,
        mounts=MOUNTS,
    )


def test_parse_inspect_skips_bind_and_ephemeral_mounts():
    info = parse_inspect_offline(_inspect_entry())
    assert [m.destination for m in info.mounts] == ["/data", "/state"]


def test_parse_inspect_detects_running_containers():
    running_state = {"Status": "running", "Running": True, "FinishedAt": ""}
    info = parse_inspect_offline(_inspect_entry(State=running_state))
    assert info.running is True
    paused_state = {"Status": "paused", "Running": False, "Paused": True}
    assert parse_inspect_offline(_inspect_entry(State=paused_state)).running is True
    restarting = {"Status": "restarting", "Running": False}
    assert parse_inspect_offline(_inspect_entry(State=restarting)).running is True


def test_parse_inspect_unknown_state_means_running():
    assert parse_inspect_offline(_inspect_entry(State={})).running is True
    no_state = json.dumps([{"Id": "abc123", "Image": IMAGE}]).encode()
    assert parse_inspect_offline(no_state).running is True
    created = {"Status": "created", "Running": False}
    assert parse_inspect_offline(_inspect_entry(State=created)).running is False
    dead = {"Status": "dead", "Running": False}
    assert parse_inspect_offline(_inspect_entry(State=dead)).running is False


def test_parse_inspect_missing_finish_time_reads_empty():
    state = {"Status": "exited", "Running": False}
    info = parse_inspect_offline(_inspect_entry(State=state))
    assert info.running is False
    assert info.finished_at == ""


def test_parse_inspect_rejects_non_array_output():
    for bad in (b"not json", b"{}", b"[]", b"[42]", b""):
        with pytest.raises(GatewayError):
            parse_inspect_offline(bad)


# --- validation --------------------------------------------------------------


def test_valid_image_volume_and_destination_pass():
    assert validate_offline_image(IMAGE) == IMAGE
    assert validate_offline_volume_name("data-vol") == "data-vol"
    assert validate_offline_destination("/data") == "/data"


def test_image_must_be_a_sha256_digest():
    for bad in ("", "ubuntu:22.04", "sha256:xyz", "sha256:" + "ab" * 31, "ab" * 64, 42, None):
        with pytest.raises(ValueError):
            validate_offline_image(bad)


def test_volume_names_must_be_valid():
    for bad in ("", "has space", "has,comma", "/abs/path", "a/b", "-lead", ".", "..", 42, None):
        with pytest.raises(ValueError):
            validate_offline_volume_name(bad)


def test_destinations_must_be_safe_for_mount_csv():
    for bad in ("", "relative/path", "has,comma", "has space", "has\ttab", "/trailing ", 42, None):
        with pytest.raises(ValueError):
            validate_offline_destination(bad)


def test_helper_names_must_be_valid_container_names():
    assert validate_offline_helper_name("offline-helper-01") == "offline-helper-01"
    for bad in ("", "has space", "has,comma", "/slash", "semi;colon", 42, None):
        with pytest.raises(ValueError):
            validate_offline_helper_name(bad)


def test_generated_helper_names_are_valid():
    seen = {make_offline_helper_name() for _ in range(10)}
    assert len(seen) == 10
    for name in seen:
        assert validate_offline_helper_name(name) == name
        assert name.startswith("agent-dashboard-offline-")


# --- offline run argv --------------------------------------------------------


EXPECTED_OFFLINE_ARGV = [
    "docker",
    "run",
    "--rm",
    "--network",
    "none",
    "--user",
    "agent",
    "--name",
    "offline-helper-01",
    "--label",
    f"{OFFLINE_READ_MARKER_LABEL}=1",
    "--mount",
    "type=volume,src=data-vol,dst=/data,readonly",
    "--mount",
    "type=volume,src=state-vol,dst=/state,readonly",
    "--entrypoint",
    AGENTCTL_PATH,
    IMAGE,
    "--contract",
    "1",
    "--offline",
    "status",
]


def test_offline_run_argv_pinned_with_several_volumes():
    argv = argv_offline_status(IMAGE, MOUNTS, "agent", "offline-helper-01")
    assert argv == EXPECTED_OFFLINE_ARGV


def test_offline_run_marker_label_is_the_discovery_skip_label():
    assert OFFLINE_READ_MARKER_LABEL == "io.github.lbacik.agent-dashboard.offline-read"
    assert f"--label={OFFLINE_READ_MARKER_LABEL}=1" not in EXPECTED_OFFLINE_ARGV
    assert f"{OFFLINE_READ_MARKER_LABEL}=1" in EXPECTED_OFFLINE_ARGV


def test_offline_run_has_no_tty_or_stdin_flags():
    assert "-t" not in EXPECTED_OFFLINE_ARGV
    assert "-i" not in EXPECTED_OFFLINE_ARGV


def test_offline_run_mounts_are_read_only_volumes():
    mounts = [part for part in EXPECTED_OFFLINE_ARGV if part.startswith("type=")]
    assert len(mounts) == 2
    for mount in mounts:
        assert mount.startswith("type=volume,")
        assert mount.endswith(",readonly")
        assert "type=bind" not in mount


def test_offline_run_without_volumes_has_no_mount_flags():
    argv = argv_offline_status(IMAGE, (), "agent", "offline-helper-01")
    assert "--mount" not in argv
    assert argv[-4:] == ["--contract", "1", "--offline", "status"]


def test_offline_run_validates_before_building_argv():
    with pytest.raises(ValueError):
        argv_offline_status("ubuntu:22.04", MOUNTS, "agent", "offline-helper-01")
    with pytest.raises(ValueError):
        argv_offline_status(
            IMAGE, (OfflineMount("bad,name", "/data"),), "agent", "offline-helper-01"
        )
    with pytest.raises(ValueError):
        argv_offline_status(
            IMAGE, (OfflineMount("data-vol", "relative"),), "agent", "offline-helper-01"
        )
    with pytest.raises(ValueError):
        argv_offline_status(IMAGE, MOUNTS, "root", "offline-helper-01")
    with pytest.raises(ValueError):
        argv_offline_status(IMAGE, MOUNTS, "agent", "bad name")


def test_offline_cleanup_argv_pinned():
    assert argv_offline_cleanup("offline-helper-01") == [
        "docker",
        "rm",
        "-f",
        "offline-helper-01",
    ]


def test_offline_cleanup_validates_the_helper_name():
    with pytest.raises(ValueError):
        argv_offline_cleanup("bad name")


# --- offline run through the gateway -----------------------------------------


class _ScriptedRunner:
    """An injectable process runner with a scripted reply and call log."""

    def __init__(self, reply=None, error=None):
        self.reply = reply
        self.error = error
        self.calls: list[tuple[list[str], float]] = []

    async def run(self, argv, *, timeout, timeout_error):
        self.calls.append((list(argv), timeout))
        if isinstance(self.error, Exception):
            if isinstance(self.error, type):
                raise self.error("timed out")
            raise self.error
        return self.reply


def _ok_exec():
    from agent_dashboard.gateway import ControlExec

    return ControlExec(stdout=b'{"ok": true}', stderr=b"", returncode=0)


def test_offline_status_runs_as_the_exec_user_with_the_read_bound():
    from agent_operator_contract import client

    runner = _ScriptedRunner(reply=_ok_exec())
    gateway_obj = DockerGateway(process_runner=runner)
    result = asyncio.run(
        gateway_obj.offline_status(IMAGE, MOUNTS, "someone", helper_name="offline-helper-01")
    )
    assert result.returncode == 0
    (argv, timeout) = runner.calls[0]
    expected = [("someone" if part == "agent" else part) for part in EXPECTED_OFFLINE_ARGV]
    assert argv == expected
    assert timeout == client.timeout_for("status") == 10.0


def test_offline_status_timeout_removes_the_helper_then_raises():
    runner = _ScriptedRunner(error=ControlTimeoutError("docker run timed out after 10 s"))
    gateway_obj = DockerGateway(process_runner=runner)
    with pytest.raises(ControlTimeoutError):
        asyncio.run(
            gateway_obj.offline_status(
                IMAGE, MOUNTS, "agent", helper_name="offline-helper-01"
            )
        )
    assert [call[0] for call in runner.calls] == [
        EXPECTED_OFFLINE_ARGV,
        ["docker", "rm", "-f", "offline-helper-01"],
    ]


def test_offline_status_timeout_cleanup_failure_still_raises_the_timeout():
    class _FailingCleanupRunner(_ScriptedRunner):
        async def run(self, argv, *, timeout, timeout_error):
            self.calls.append((list(argv), timeout))
            raise timeout_error

    runner = _FailingCleanupRunner()
    gateway_obj = DockerGateway(process_runner=runner)
    with pytest.raises(ControlTimeoutError, match="docker run timed out"):
        asyncio.run(
            gateway_obj.offline_status(
                IMAGE, MOUNTS, "agent", helper_name="offline-helper-01"
            )
        )
    assert len(runner.calls) == 2


def test_offline_status_validates_before_spawning():
    runner = _ScriptedRunner(reply=_ok_exec())
    gateway_obj = DockerGateway(process_runner=runner)
    with pytest.raises(ValueError):
        asyncio.run(gateway_obj.offline_status("not-an-image", MOUNTS, "agent"))
    assert runner.calls == []


def test_inspect_offline_uses_the_pinned_argv(monkeypatch):
    seen: dict = {}

    async def _spawn(self, argv):
        seen["argv"] = argv

        class _Proc:
            returncode = 0

            async def communicate(self):
                return _inspect_entry(), b""

        return _Proc()

    monkeypatch.setattr(DockerGateway, "_run", _spawn)
    gateway_obj = DockerGateway()
    info = asyncio.run(gateway_obj.inspect_offline("abc123"))
    assert seen["argv"] == ["docker", "inspect", "abc123"]
    assert info == OfflineInspect(
        running=False,
        finished_at="2026-11-06T09:58:12.123456789Z",
        image=IMAGE,
        mounts=MOUNTS,
    )


def test_inspect_offline_failure_raises_gateway_error(monkeypatch):
    async def _spawn(self, argv):
        class _Proc:
            returncode = 1

            async def communicate(self):
                return b"", b"Error: no such container"

        return _Proc()

    monkeypatch.setattr(DockerGateway, "_run", _spawn)
    gateway_obj = DockerGateway()
    with pytest.raises(GatewayError, match="no such container"):
        asyncio.run(gateway_obj.inspect_offline("abc123"))
