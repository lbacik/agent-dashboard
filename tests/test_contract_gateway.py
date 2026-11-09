"""Contract operations run through the injectable process runner."""

import asyncio

import pytest
from contract_replay import ReplayProcessRunner, UnexpectedArgvError

from agent_dashboard.gateway import (
    AGENTCTL_PATH,
    AsyncSubprocessRunner,
    ControlExec,
    ControlTimeoutError,
    DockerGateway,
    DockerNotAvailableError,
    argv_contract_exec,
)


def test_contract_argv_pinned():
    assert argv_contract_exec("abc123", ["status"]) == [
        "docker",
        "exec",
        "--user",
        "agent",
        "abc123",
        AGENTCTL_PATH,
        "--contract",
        "1",
        "status",
    ]


def test_contract_argv_mutation_pinned():
    assert argv_contract_exec(
        "abc123",
        ["stop", "--request-id=req-3f1c9a2b7d10", "--expect-repo=a/b"],
    ) == [
        "docker",
        "exec",
        "--user",
        "agent",
        "abc123",
        AGENTCTL_PATH,
        "--contract",
        "1",
        "stop",
        "--request-id=req-3f1c9a2b7d10",
        "--expect-repo=a/b",
    ]


def test_contract_argv_major_tracks_the_pinned_library():
    from agent_operator_contract import CONTRACT_MAJOR

    assert CONTRACT_MAJOR == 1
    assert argv_contract_exec("abc123", ["status"])[7] == str(CONTRACT_MAJOR)


def test_contract_argv_rejects_bad_values():
    with pytest.raises(ValueError):
        argv_contract_exec("not a container id!", ["status"])
    with pytest.raises(ValueError):
        argv_contract_exec("abc123", [])
    with pytest.raises(ValueError):
        argv_contract_exec("abc123", "--contract")
    with pytest.raises(ValueError):
        argv_contract_exec("abc123", ["--contract", "1", "status"])
    with pytest.raises(ValueError):
        argv_contract_exec("abc123", ["status", "--contract", "1"])
    with pytest.raises(ValueError):
        argv_contract_exec("abc123", ["status", "--contract=1"])
    with pytest.raises(ValueError):
        argv_contract_exec("abc123", ["status", ""])
    with pytest.raises(ValueError):
        argv_contract_exec("abc123", ["status"], exec_user="has space")
    with pytest.raises(ValueError):
        argv_contract_exec("abc123", ["status"], exec_user="")


def test_default_runner_is_the_async_subprocess_path():
    assert isinstance(DockerGateway()._process_runner, AsyncSubprocessRunner)


def test_contract_exec_replays_a_fixture():
    runner = ReplayProcessRunner.from_package(["status-active"])
    gateway = DockerGateway(process_runner=runner)
    exec_result = asyncio.run(gateway.contract_exec("abc123", ["status"]))
    assert isinstance(exec_result, ControlExec)
    assert exec_result.returncode == 0
    assert b'"ok":true' in exec_result.stdout.replace(b" ", b"")
    assert runner.remaining == 0
    assert runner.calls == [argv_contract_exec("abc123", ["status"])]


def test_contract_exec_replays_error_envelopes_verbatim():
    runner = ReplayProcessRunner.from_package(["rejected"])
    gateway = DockerGateway(process_runner=runner)
    exec_result = asyncio.run(
        gateway.contract_exec(
            "abc123",
            [
                "resume",
                "--request-id",
                "req-7c8d9e0f1a2b",
                "--expect-repo",
                "example-owner/example-repo",
            ],
        )
    )
    assert exec_result.returncode == 1
    assert b"rejected" in exec_result.stdout


def test_contract_exec_fails_on_unexpected_argv():
    runner = ReplayProcessRunner.from_package(["status-active"])
    gateway = DockerGateway(process_runner=runner)
    with pytest.raises(UnexpectedArgvError):
        asyncio.run(gateway.contract_exec("abc123", ["identity"]))
    assert runner.remaining == 1


def test_contract_exec_rejects_a_bad_timeout_before_running():
    runner = ReplayProcessRunner.from_package(["status-active"])
    gateway = DockerGateway(process_runner=runner)
    for bad in (0, -1.0, float("nan")):
        with pytest.raises(ValueError):
            asyncio.run(gateway.contract_exec("abc123", ["status"], timeout=bad))
    assert runner.calls == []


def test_contract_exec_uses_the_contract_timeout_policy():
    seen = {}

    class CapturingRunner:
        async def run(self, argv, *, timeout, timeout_error):
            seen["timeout"] = timeout
            seen["argv"] = argv
            return ControlExec(stdout=b"{}", stderr=b"", returncode=0)

    gateway = DockerGateway(process_runner=CapturingRunner())
    asyncio.run(gateway.contract_exec("abc123", ["status"], operation="status"))
    assert seen["timeout"] == 10.0
    asyncio.run(gateway.contract_exec("abc123", ["stop"], operation="stop"))
    assert seen["timeout"] == 15.0
    asyncio.run(
        gateway.contract_exec("abc123", ["status"], operation="status", timeout=2.5)
    )
    assert seen["timeout"] == 2.5


def test_control_still_uses_the_default_runner(monkeypatch):
    from test_gateway import _FakeProcess, _patch_create_subprocess

    proc = _FakeProcess(stdout=b"repository: a/b\nrequest_id: x\n", returncode=0)
    seen = _patch_create_subprocess(monkeypatch, proc)
    gateway = DockerGateway()
    exec_result = asyncio.run(
        gateway.control("abc123", "stop", request_id="dash-0123456789abcdef")
    )
    assert exec_result.returncode == 0
    assert seen["argv"][:2] == ["docker", "exec"]


def test_default_runner_timeout_kills_and_raises(monkeypatch):
    class SlowProcess:
        returncode = None
        killed = False

        async def communicate(self):
            await asyncio.sleep(10)
            return b"", b""

        def kill(self):
            self.killed = True

        async def wait(self):
            self.returncode = -9
            return self.returncode

    proc = SlowProcess()

    async def fake_create(*argv, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    gateway = DockerGateway()
    with pytest.raises(ControlTimeoutError):
        asyncio.run(gateway.contract_exec("abc123", ["status"], timeout=0.01))
    assert proc.killed


def test_default_runner_missing_cli(monkeypatch):
    async def fake_create(*argv, **kwargs):
        raise FileNotFoundError("no docker")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    gateway = DockerGateway()
    with pytest.raises(DockerNotAvailableError):
        asyncio.run(gateway.contract_exec("abc123", ["status"]))


def test_replay_runner_rejects_invalid_fixtures():
    with pytest.raises(ValueError):
        ReplayProcessRunner([{"argv": ["--contract"], "exit_code": 0}])
