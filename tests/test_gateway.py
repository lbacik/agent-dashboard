"""The gateway is the only code that builds Docker argv; pin every argv."""

import asyncio

import pytest

from agent_dashboard import gateway
from agent_dashboard.gateway import (
    AGENT_ROLE_LABEL,
    AGENT_USER,
    DiscoveryTimeoutError,
    DockerGateway,
    DockerNotAvailableError,
    GatewayError,
    ManagedInstance,
    TelemetryTimeoutError,
    argv_attempt_events,
    argv_attempt_history,
    argv_list_instances,
    argv_telemetry,
    parse_ps_output,
)

EXPECTED_ARGV = [
    "docker",
    "ps",
    "-a",
    "--filter",
    f"label={AGENT_ROLE_LABEL}",
    "--format",
    gateway.PS_FORMAT,
]


def test_agent_role_label():
    assert AGENT_ROLE_LABEL == "io.github.lbacik.simple-coding-agent.role=agent"


def test_list_instances_argv_pinned():
    assert argv_list_instances() == EXPECTED_ARGV


def test_gateway_argv_matches_module_function():
    assert DockerGateway(timeout=5.0).argv() == argv_list_instances() == EXPECTED_ARGV


def test_every_argv_producer_is_pinned():
    """Every public argv builder on the gateway must be enumerated here."""
    producers = {
        "argv_list_instances": argv_list_instances(),
        "argv_telemetry": argv_telemetry("abc123"),
        "argv_attempt_history": argv_attempt_history("abc123"),
        "argv_attempt_events": argv_attempt_events(
            "abc123", 98, "2026-09-27T16:33:43.385090Z"
        ),
    }
    assert set(producers) == {
        "argv_list_instances",
        "argv_telemetry",
        "argv_attempt_history",
        "argv_attempt_events",
    }
    for argv in producers.values():
        assert isinstance(argv, list)
        assert all(isinstance(part, str) for part in argv)
        assert argv[0] == "docker"


def test_no_shell_in_gateway_source():
    import pathlib

    source = pathlib.Path(gateway.__file__).read_text()
    assert "shell=True" not in source
    assert "os.system" not in source
    assert "subprocess.run" not in source or "shell" not in source


def test_parse_ps_output_states_and_fields():
    output = "abc123\tworker-1\trunning\t\ndef456\tworker-2\texited\t\n"
    rows = parse_ps_output(output)
    assert [(r.name, r.state, r.container_id) for r in rows] == [
        ("worker-1", "running", "abc123"),
        ("worker-2", "not running", "def456"),
    ]


def test_parse_ps_output_drops_oneoff_and_blank_or_malformed():
    output = "\n".join(
        [
            "aaa\toneoff-run\trunning\tTrue",
            "bbb\tworker-3\trunning\tFalse",
            "ccc\t/worker-4\texited\t",
            "malformed-line",
            "",
        ]
    )
    rows = parse_ps_output(output)
    assert [r.name for r in rows] == ["worker-3", "worker-4"]
    assert all(r.state in ("running", "not running") for r in rows)


def test_state_mapping():
    assert ManagedInstance("a", "id", "running").state == "running"
    assert ManagedInstance("a", "id", "Running").state == "running"
    for raw in ("exited", "created", "paused", "dead", "removing", ""):
        assert ManagedInstance("a", "id", raw).state == "not running"


def test_gateway_rejects_bad_timeout():
    with pytest.raises(ValueError):
        DockerGateway(timeout=0)


class _FakeProcess:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self):
        return self._stdout, self._stderr

    def kill(self):
        pass

    async def wait(self):
        return self.returncode


def _patch_create_subprocess(monkeypatch, process=None, *, exc=None):
    seen = {}

    async def fake_create(*argv, **kwargs):
        seen["argv"] = list(argv)
        seen["kwargs"] = kwargs
        if exc is not None:
            raise exc
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    return seen


def test_list_instances_uses_pinned_argv(monkeypatch):
    proc = _FakeProcess(stdout=b"abc\tworker-1\trunning\t\n")
    seen = _patch_create_subprocess(monkeypatch, proc)
    rows = asyncio.run(DockerGateway(timeout=5.0).list_instances())
    assert seen["argv"] == EXPECTED_ARGV
    assert "shell" not in seen["kwargs"]
    assert [r.name for r in rows] == ["worker-1"]


def test_list_instances_missing_cli(monkeypatch):
    _patch_create_subprocess(monkeypatch, exc=FileNotFoundError("no docker"))
    with pytest.raises(DockerNotAvailableError):
        asyncio.run(DockerGateway(timeout=5.0).list_instances())


def test_list_instances_timeout(monkeypatch):
    class SlowProcess(_FakeProcess):
        async def communicate(self):
            await asyncio.sleep(10)
            return b"", b""

    async def fake_create(*argv, **kwargs):
        return SlowProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    with pytest.raises(DiscoveryTimeoutError):
        asyncio.run(DockerGateway(timeout=0.05).list_instances())


def test_list_instances_nonzero_exit(monkeypatch):
    proc = _FakeProcess(returncode=1, stderr=b"boom")
    _patch_create_subprocess(monkeypatch, proc)
    with pytest.raises(GatewayError, match="boom"):
        asyncio.run(DockerGateway(timeout=5.0).list_instances())


def test_list_instances_permission_denied(monkeypatch):
    proc = _FakeProcess(
        returncode=1, stderr=b"permission denied while trying to connect"
    )
    _patch_create_subprocess(monkeypatch, proc)
    with pytest.raises(GatewayError, match="permission denied"):
        asyncio.run(DockerGateway(timeout=5.0).list_instances())


EXPECTED_TELEMETRY_ARGV = [
    "docker",
    "exec",
    "--user",
    "agent",
    "abc123",
    "/usr/local/bin/agentctl",
    "telemetry",
    "--json",
]


def test_telemetry_argv_pinned():
    assert argv_telemetry("abc123") == EXPECTED_TELEMETRY_ARGV


def test_telemetry_argv_has_no_tty_or_stdin_flags():
    argv = argv_telemetry("abc123")
    assert "-t" not in argv
    assert "-i" not in argv
    assert "--tty" not in argv
    assert "--interactive" not in argv


def test_telemetry_targets_container_id_not_name(monkeypatch):
    proc = _FakeProcess(stdout=b"{}", returncode=0)
    seen = _patch_create_subprocess(monkeypatch, proc)
    result = asyncio.run(DockerGateway(timeout=5.0).telemetry("abc123"))
    assert seen["argv"] == EXPECTED_TELEMETRY_ARGV
    assert "shell" not in seen["kwargs"]
    assert result.stdout == b"{}"
    assert result.returncode == 0


def test_telemetry_returns_stdout_and_exit_without_raising(monkeypatch):
    """Exit codes are the collector's business; the gateway reports them."""
    proc = _FakeProcess(stdout=b'{"ok": false}', returncode=2)
    _patch_create_subprocess(monkeypatch, proc)
    result = asyncio.run(DockerGateway(timeout=5.0).telemetry("abc123"))
    assert result.stdout == b'{"ok": false}'
    assert result.returncode == 2


def test_telemetry_timeout_kills_client_process(monkeypatch):
    killed = []
    reaped = []

    class SlowProcess(_FakeProcess):
        async def communicate(self):
            await asyncio.sleep(10)
            return b"", b""

        def kill(self):
            killed.append(True)

        async def wait(self):
            reaped.append(True)
            return -9

    async def fake_create(*argv, **kwargs):
        return SlowProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    with pytest.raises(TelemetryTimeoutError, match="timed out"):
        asyncio.run(DockerGateway(timeout=0.05).telemetry("abc123"))
    assert killed == [True]
    assert reaped == [True]


def test_telemetry_missing_cli(monkeypatch):
    _patch_create_subprocess(monkeypatch, exc=FileNotFoundError("no docker"))
    with pytest.raises(DockerNotAvailableError):
        asyncio.run(DockerGateway(timeout=5.0).telemetry("abc123"))


def test_gateway_rejects_bad_telemetry_timeout():
    with pytest.raises(ValueError):
        DockerGateway(timeout=-1)


# --- attempt events (progress chart) -----------------------------------------


def test_attempt_events_argv_pinned():
    argv = argv_attempt_events("abc123", 98, "2026-09-27T16:33:43.385090Z")
    assert argv == [
        "docker",
        "exec",
        "--user",
        AGENT_USER,
        "abc123",
        "python",
        "-c",
        gateway.ATTEMPT_EVENTS_SCRIPT,
        "98",
        "2026-09-27T16-33-43.385090Z",
    ]


def test_attempt_events_argv_accepts_dash_started():
    argv = argv_attempt_events("abc123", "98", "2026-09-27T16-33-43.385090Z")
    assert argv[-2:] == ["98", "2026-09-27T16-33-43.385090Z"]


def test_attempt_events_argv_rejects_bad_refs():
    with pytest.raises(ValueError):
        argv_attempt_events("abc123", "ninety-eight", "2026-09-27T16:33:43.385090Z")
    with pytest.raises(ValueError):
        argv_attempt_events("abc123", 98, "../state/attempt.json")
    with pytest.raises(ValueError):
        argv_attempt_events("abc123", 98, "not-a-timestamp")
    with pytest.raises(ValueError):
        argv_attempt_events("not a container!", 98, "2026-09-27T16:33:43.385090Z")


def test_attempt_events_uses_pinned_argv(monkeypatch):
    proc = _FakeProcess(stdout=b"{}", returncode=0)
    seen = _patch_create_subprocess(monkeypatch, proc)
    result = asyncio.run(
        DockerGateway(timeout=5.0).attempt_events(
            "abc123", 98, "2026-09-27T16:33:43.385090Z"
        )
    )
    assert seen["argv"] == argv_attempt_events("abc123", 98, "2026-09-27T16:33:43.385090Z")
    assert "shell" not in seen["kwargs"]
    assert result.stdout == b"{}"
    assert result.returncode == 0


def test_attempt_events_timeout_kills_client_process(monkeypatch):
    killed = []
    reaped = []

    class SlowProcess(_FakeProcess):
        async def communicate(self):
            await asyncio.sleep(10)
            return b"", b""

        def kill(self):
            killed.append(True)

        async def wait(self):
            reaped.append(True)
            return -9

    async def fake_create(*argv, **kwargs):
        return SlowProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    with pytest.raises(TelemetryTimeoutError, match="timed out"):
        asyncio.run(
            DockerGateway(timeout=0.05).attempt_events(
                "abc123", 98, "2026-09-27T16:33:43.385090Z"
            )
        )
    assert killed == [True]
    assert reaped == [True]


def test_attempt_events_missing_cli(monkeypatch):
    _patch_create_subprocess(monkeypatch, exc=FileNotFoundError("no docker"))
    with pytest.raises(DockerNotAvailableError):
        asyncio.run(
            DockerGateway(timeout=5.0).attempt_events(
                "abc123", 98, "2026-09-27T16:33:43.385090Z"
            )
        )
