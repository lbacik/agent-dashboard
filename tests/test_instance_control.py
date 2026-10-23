"""Slice 1: gateway control operations, the collector's poll skip and settings.

Covers ``argv_control`` / ``argv_command_status`` (every action with and
without ``--expect-repo``, plus ``command <id>``), every validation case
from spec section 3.2, the timeout kill-and-reap, the per-container busy
slot, ``control_in_flight``, the collector skip that keeps the previous
poll outcome and snapshot, the ``control_exec_timeout`` setting, and the
fake gateway's scripted control replies, busy and timeout.
"""

import asyncio

import pytest
from fake_gateway import FakeGateway

from agent_dashboard import gateway
from agent_dashboard.collector import run_cycle
from agent_dashboard.config import load_settings
from agent_dashboard.fleet import FleetState
from agent_dashboard.gateway import (
    AGENT_USER,
    AGENTCTL_PATH,
    ControlBusyError,
    ControlExec,
    ControlTimeoutError,
    DockerGateway,
    DockerNotAvailableError,
    ManagedInstance,
    argv_command_status,
    argv_control,
    validate_control_action,
    validate_control_issue,
    validate_control_n,
    validate_expect_repo,
    validate_request_id,
)

CID = "abc123"
OTHER = "def456"
RID = "dash-0123456789abcdef"
REPO = "octo-org/hello-world"
BASE = ["docker", "exec", "--user", AGENT_USER, CID, AGENTCTL_PATH]


def run(cycle_state, cycle_gateway, **kwargs):
    kwargs.setdefault("now", 1000.0)
    asyncio.run(run_cycle(cycle_state, cycle_gateway, **kwargs))


# --- argv pinning ------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "kwargs", "tail"),
    [
        ("stop", {}, ["stop", f"--request-id={RID}"]),
        ("stop_after", {"n": 3}, ["stop", "--after", "3", f"--request-id={RID}"]),
        ("resume", {}, ["resume", f"--request-id={RID}"]),
        ("handoff", {}, ["handoff", "now", f"--request-id={RID}"]),
        ("next_issue", {"issue": 42}, ["next", "issue", "42", f"--request-id={RID}"]),
    ],
)
def test_argv_control_pinned(action, kwargs, tail):
    assert argv_control(CID, action, request_id=RID, **kwargs) == [*BASE, *tail]


@pytest.mark.parametrize(
    ("action", "kwargs", "tail"),
    [
        ("stop", {}, ["stop", f"--request-id={RID}"]),
        ("stop_after", {"n": 3}, ["stop", "--after", "3", f"--request-id={RID}"]),
        ("resume", {}, ["resume", f"--request-id={RID}"]),
        ("handoff", {}, ["handoff", "now", f"--request-id={RID}"]),
        ("next_issue", {"issue": 42}, ["next", "issue", "42", f"--request-id={RID}"]),
    ],
)
def test_argv_control_expect_repo_is_last_single_element(action, kwargs, tail):
    argv = argv_control(CID, action, request_id=RID, expect_repo=REPO, **kwargs)
    assert argv == [*BASE, *tail, f"--expect-repo={REPO}"]
    assert argv[-1] == f"--expect-repo={REPO}"


def test_argv_command_status_pinned():
    assert argv_command_status(CID, RID) == [*BASE, "command", RID]


def test_control_argv_addresses_container_and_runs_as_agent():
    argv = argv_control(CID, "stop", request_id=RID)
    assert argv[:4] == ["docker", "exec", "--user", "agent"]
    assert argv[4] == CID
    assert argv[5] == AGENTCTL_PATH


def test_no_shell_in_control_source():
    source = gateway.__file__
    import pathlib

    text = pathlib.Path(source).read_text()
    assert "shell=True" not in text


# --- validation --------------------------------------------------------------


@pytest.mark.parametrize(
    "bad", ["", "STOP", "Stop", "stop ", " stop", "stop\n", "delete", "stop;rm", "--stop"]
)
def test_validate_control_action_rejects(bad):
    with pytest.raises(ValueError):
        validate_control_action(bad)


@pytest.mark.parametrize("bad", [None, 0, True, False, ["stop"], b"stop"])
def test_validate_control_action_rejects_non_strings(bad):
    with pytest.raises(ValueError):
        validate_control_action(bad)


@pytest.mark.parametrize(
    "action", ["stop", "stop_after", "resume", "handoff", "next_issue"]
)
def test_validate_control_action_accepts(action):
    assert validate_control_action(action) == action


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "0",
        "00",
        "01",
        "007",
        "100",
        "000",
        "+3",
        "-3",
        "-1",
        " 3",
        "3 ",
        "3\n",
        "\n3",
        "3.0",
        "three",
        "٣",
        "１２",
        "3=3",
        "3--x",
        "--3",
    ],
)
def test_validate_control_n_rejects(bad):
    with pytest.raises(ValueError):
        validate_control_n(bad)


@pytest.mark.parametrize("bad", [0, 100, -1, 101, True, False, None, 3.5, [3]])
def test_validate_control_n_rejects_non_strings(bad):
    with pytest.raises(ValueError):
        validate_control_n(bad)


@pytest.mark.parametrize(("good", "want"), [(1, 1), (99, 99), ("1", 1), ("9", 9), ("99", 99)])
def test_validate_control_n_accepts(good, want):
    assert validate_control_n(good) == want


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "0",
        "00",
        "01",
        "007",
        "2147483648",
        "99999999999",
        "+42",
        "-42",
        " 42",
        "42 ",
        "42\n",
        "\n42",
        "4.2",
        "forty-two",
        "４２",
        "42=1",
        "42--x",
        "--42",
    ],
)
def test_validate_control_issue_rejects(bad):
    with pytest.raises(ValueError):
        validate_control_issue(bad)


@pytest.mark.parametrize("bad", [0, -1, 2147483648, True, False, None, 4.2, [42]])
def test_validate_control_issue_rejects_non_strings(bad):
    with pytest.raises(ValueError):
        validate_control_issue(bad)


@pytest.mark.parametrize(
    ("good", "want"),
    [(1, 1), (100, 100), (2147483647, 2147483647), ("1", 1), ("42", 42)],
)
def test_validate_control_issue_accepts(good, want):
    assert validate_control_issue(good) == want


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "dash-",
        "DASH-0123456789abcdef",
        "dash-0123456789abcde",
        "dash-0123456789abcdef0",
        "dash-ABCDEF1234567890",
        "dash_0123456789abcdef",
        "dash-0123456789abcdeg",
        " dash-0123456789abcdef",
        "dash-0123456789abcdef ",
        "dash-0123456789abcdef\n",
        "\ndash-0123456789abcdef",
        "-dash-0123456789abcdef",
        "--dash-0123456789abcdef",
        "dash-0123456789abcdef=x",
        "dash-0123456789abcdef--x",
        "--request-id=dash-0123456789abcdef",
        "dash-0123456789abcde١",
        "other-0123456789abcdef",
    ],
)
def test_validate_request_id_rejects(bad):
    with pytest.raises(ValueError):
        validate_request_id(bad)


@pytest.mark.parametrize("bad", [None, 0, True, b"dash-0123456789abcdef"])
def test_validate_request_id_rejects_non_strings(bad):
    with pytest.raises(ValueError):
        validate_request_id(bad)


def test_validate_request_id_accepts():
    assert validate_request_id(RID) == RID


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "owner",
        "/repo",
        "owner/",
        "/",
        "owner/repo/extra",
        " owner/repo",
        "owner/repo ",
        "owner/repo\n",
        "own er/repo",
        "owner/re po",
        "ownér/repo",
        "owner/repo\n",
        "--expect-repo=owner/repo",
        "--owner/repo=x",
        "owner/repo=x",
        "owner/repo--x ",
        "o" * 40 + "/repo",
        "owner/" + "r" * 101,
    ],
)
def test_validate_expect_repo_rejects(bad):
    with pytest.raises(ValueError):
        validate_expect_repo(bad)


@pytest.mark.parametrize("bad", [None, 0, True, ["owner/repo"]])
def test_validate_expect_repo_rejects_non_strings(bad):
    with pytest.raises(ValueError):
        validate_expect_repo(bad)


@pytest.mark.parametrize(
    "good",
    ["o/r", REPO, "OWNER/REPO", "owner/repo.js", "owner/.repo", "a" * 39 + "/" + "b" * 100],
)
def test_validate_expect_repo_accepts(good):
    assert validate_expect_repo(good) == good


@pytest.mark.parametrize(
    "kwargs",
    [
        {"action": "explode"},
        {"action": "stop", "n": 3},
        {"action": "stop", "issue": 42},
        {"action": "resume", "n": 1},
        {"action": "handoff", "issue": 7},
        {"action": "stop_after"},
        {"action": "stop_after", "n": "0"},
        {"action": "stop_after", "n": 100},
        {"action": "next_issue"},
        {"action": "next_issue", "issue": 2147483648},
    ],
)
def test_argv_control_rejects_bad_params(kwargs):
    action = kwargs.pop("action")
    with pytest.raises(ValueError):
        argv_control(CID, action, request_id=RID, **kwargs)


def test_argv_control_rejects_bad_ids_and_repo():
    with pytest.raises(ValueError):
        argv_control("not a container!", "stop", request_id=RID)
    with pytest.raises(ValueError):
        argv_control(CID, "stop", request_id="bogus")
    with pytest.raises(ValueError):
        argv_control(CID, "stop", request_id=RID, expect_repo="not a repo")
    with pytest.raises(ValueError):
        argv_command_status("not a container!", RID)
    with pytest.raises(ValueError):
        argv_command_status(CID, "bogus")


# --- running a control exec --------------------------------------------------


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


def test_control_reports_stdout_stderr_and_exit(monkeypatch):
    proc = _FakeProcess(stdout=b"acknowledgement: accepted\n", stderr=b"", returncode=0)
    seen = _patch_create_subprocess(monkeypatch, proc)
    result = asyncio.run(DockerGateway().control(CID, "stop", request_id=RID))
    assert result == ControlExec(stdout=b"acknowledgement: accepted\n", stderr=b"", returncode=0)
    assert seen["argv"] == argv_control(CID, "stop", request_id=RID)
    assert seen["argv"][:4] == ["docker", "exec", "--user", "agent"]
    assert "shell" not in seen["kwargs"]


def test_control_exit_codes_are_reported_not_raised(monkeypatch):
    proc = _FakeProcess(stdout=b"", stderr=b"rejected: busy\n", returncode=1)
    _patch_create_subprocess(monkeypatch, proc)
    result = asyncio.run(DockerGateway().control(CID, "resume", request_id=RID))
    assert result.returncode == 1
    assert result.stderr == b"rejected: busy\n"


def test_command_status_reports_exit_codes(monkeypatch):
    proc = _FakeProcess(stdout=b"request_id: x\n", stderr=b"warn\n", returncode=3)
    seen = _patch_create_subprocess(monkeypatch, proc)
    result = asyncio.run(DockerGateway().command_status(CID, RID))
    assert result == ControlExec(stdout=b"request_id: x\n", stderr=b"warn\n", returncode=3)
    assert seen["argv"] == argv_command_status(CID, RID)


def test_control_missing_cli(monkeypatch):
    _patch_create_subprocess(monkeypatch, exc=FileNotFoundError("no docker"))
    with pytest.raises(DockerNotAvailableError):
        asyncio.run(DockerGateway().control(CID, "stop", request_id=RID))


def test_command_status_missing_cli(monkeypatch):
    _patch_create_subprocess(monkeypatch, exc=FileNotFoundError("no docker"))
    with pytest.raises(DockerNotAvailableError):
        asyncio.run(DockerGateway().command_status(CID, RID))


def test_control_bad_value_spawns_nothing(monkeypatch):
    seen = _patch_create_subprocess(monkeypatch, _FakeProcess())
    with pytest.raises(ValueError):
        asyncio.run(DockerGateway().control(CID, "explode", request_id=RID))
    with pytest.raises(ValueError):
        asyncio.run(DockerGateway().control(CID, "stop_after", request_id=RID))
    with pytest.raises(ValueError):
        asyncio.run(DockerGateway().command_status(CID, "bogus"))
    assert seen == {}


def test_control_timeout_kills_and_reaps(monkeypatch):
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
    gateway = DockerGateway()
    gateway.control_exec_timeout = 0.05
    with pytest.raises(ControlTimeoutError, match="timed out"):
        asyncio.run(gateway.control(CID, "stop", request_id=RID))
    assert killed == [True]
    assert reaped == [True]
    assert gateway.control_in_flight(CID) is False


def test_command_status_timeout_kills_and_reaps(monkeypatch):
    killed = []

    class SlowProcess(_FakeProcess):
        async def communicate(self):
            await asyncio.sleep(10)
            return b"", b""

        def kill(self):
            killed.append(True)

    async def fake_create(*argv, **kwargs):
        return SlowProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    gateway = DockerGateway()
    gateway.control_exec_timeout = 0.05
    with pytest.raises(ControlTimeoutError, match="timed out"):
        asyncio.run(gateway.command_status(CID, RID))
    assert killed == [True]


class _GateProcess:
    """A control proc that stays open until released."""

    def __init__(self):
        self.returncode = 0
        self._release = asyncio.Event()
        self.killed = False

    async def communicate(self):
        await self._release.wait()
        return b"out", b"err"

    def kill(self):
        self.killed = True
        self._release.set()

    async def wait(self):
        return self.returncode


def test_busy_second_call_for_same_container(monkeypatch):
    procs = []

    async def fake_create(*argv, **kwargs):
        proc = _GateProcess()
        procs.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    gateway = DockerGateway()

    async def main():
        first = asyncio.create_task(gateway.control(CID, "stop", request_id=RID))
        for _ in range(1000):
            if gateway.control_in_flight(CID):
                break
            await asyncio.sleep(0.001)
        assert gateway.control_in_flight(CID) is True
        # A second call for the same container raises at once, spawning nothing.
        before = len(procs)
        with pytest.raises(ControlBusyError):
            await gateway.control(CID, "resume", request_id=RID)
        with pytest.raises(ControlBusyError):
            await gateway.command_status(CID, RID)
        assert len(procs) == before
        # Another container is unaffected and shares no slot.
        assert gateway.control_in_flight(OTHER) is False
        other = asyncio.create_task(gateway.command_status(OTHER, RID))
        for _ in range(1000):
            if gateway.control_in_flight(OTHER):
                break
            await asyncio.sleep(0.001)
        assert gateway.control_in_flight(OTHER) is True
        procs[0]._release.set()
        assert await first == ControlExec(stdout=b"out", stderr=b"err", returncode=0)
        assert gateway.control_in_flight(CID) is False
        procs[1]._release.set()
        assert (await other).returncode == 0
        assert gateway.control_in_flight(OTHER) is False
        return True

    assert asyncio.run(main())


def test_control_in_flight_false_when_idle():
    gateway = DockerGateway()
    assert gateway.control_in_flight(CID) is False
    assert gateway.control_in_flight("missing") is False


def test_gateway_rejects_small_control_timeout():
    with pytest.raises(ValueError):
        DockerGateway(control_exec_timeout=10.0)
    with pytest.raises(ValueError):
        DockerGateway(control_exec_timeout=5.0)
    assert DockerGateway(control_exec_timeout=10.5).control_exec_timeout == 10.5
    assert DockerGateway().control_exec_timeout == 15.0


# --- control_exec_timeout setting --------------------------------------------


def test_control_exec_timeout_default(monkeypatch):
    monkeypatch.delenv("AGENT_DASHBOARD_CONTROL_EXEC_TIMEOUT", raising=False)
    assert load_settings([]).control_exec_timeout == 15.0


def test_control_exec_timeout_env(monkeypatch):
    monkeypatch.setenv("AGENT_DASHBOARD_CONTROL_EXEC_TIMEOUT", "12")
    assert load_settings([]).control_exec_timeout == 12.0


def test_control_exec_timeout_flag_beats_env(monkeypatch):
    monkeypatch.setenv("AGENT_DASHBOARD_CONTROL_EXEC_TIMEOUT", "12")
    assert load_settings(["--control-exec-timeout", "20"]).control_exec_timeout == 20.0


@pytest.mark.parametrize("bad", ["10", "10.0", "5", "0", "-1"])
def test_control_exec_timeout_floor_env(monkeypatch, bad):
    monkeypatch.setenv("AGENT_DASHBOARD_CONTROL_EXEC_TIMEOUT", bad)
    with pytest.raises(ValueError):
        load_settings([])


@pytest.mark.parametrize("bad", ["10", "3"])
def test_control_exec_timeout_floor_flag(bad):
    with pytest.raises(ValueError):
        load_settings(["--control-exec-timeout", bad])


def test_control_exec_timeout_just_above_floor(monkeypatch):
    monkeypatch.setenv("AGENT_DASHBOARD_CONTROL_EXEC_TIMEOUT", "10.5")
    assert load_settings([]).control_exec_timeout == 10.5


# --- collector skip ----------------------------------------------------------


def test_control_in_flight_skips_telemetry_poll():
    state = FleetState()
    gateway = FakeGateway([ManagedInstance("worker-1", CID, "running")])
    gateway.set_snapshot(CID, {"schema_version": 1})
    run(state, gateway, now=1000.0)
    assert gateway.telemetry_calls == [CID]

    # The next cycle would fail; with a control exec in flight it is skipped.
    gateway.set_error_code(CID, "agent_unreachable", "no socket")
    gateway.hold_control(CID)
    run(state, gateway, now=1030.0)
    assert gateway.telemetry_calls == [CID]

    rows, _ = asyncio.run(state.snapshot(now=1045.0))
    assert rows[0].reach == "live"
    assert rows[0].has_snapshot is True
    assert rows[0].snapshot_age == pytest.approx(45.0)


def test_skip_is_not_a_failure_and_next_cycle_polls_normally():
    state = FleetState()
    gateway = FakeGateway([ManagedInstance("worker-1", CID, "running")])
    gateway.set_snapshot(CID, {"schema_version": 1})
    run(state, gateway, now=1000.0)

    gateway.set_error_code(CID, "agent_unreachable", "no socket")
    gateway.hold_control(CID)
    run(state, gateway, now=1030.0)
    rows, _ = asyncio.run(state.snapshot(now=1030.0))
    assert rows[0].reach == "live"

    gateway.release_control(CID)
    run(state, gateway, now=1060.0)
    assert gateway.telemetry_calls == [CID, CID]
    rows, _ = asyncio.run(state.snapshot(now=1060.0))
    assert rows[0].reach == "unreachable"


def test_other_instances_poll_during_control():
    state = FleetState()
    gateway = FakeGateway(
        [
            ManagedInstance("busy", CID, "running"),
            ManagedInstance("free", OTHER, "running"),
        ]
    )
    gateway.set_snapshot(CID, {"schema_version": 1})
    gateway.set_snapshot(OTHER, {"schema_version": 1})
    gateway.hold_control(CID)
    run(state, gateway, max_concurrent_execs=1, now=1000.0)
    assert gateway.telemetry_calls == [OTHER]
    rows, _ = asyncio.run(state.snapshot(now=1000.0))
    assert {r.name: r.reach for r in rows} == {"busy": "stale", "free": "live"}


def test_collector_without_control_slot_still_polls():
    """Gateways without control support (plain doubles) keep polling."""
    state = FleetState()
    gateway = FakeGateway([ManagedInstance("worker-1", CID, "running")])
    gateway.set_snapshot(CID, {"schema_version": 1})
    gateway.control_in_flight = None  # type: ignore[method-assign]
    run(state, gateway, now=1000.0)
    assert gateway.telemetry_calls == [CID]


# --- fake gateway support ------------------------------------------------------


def test_fake_scripted_control_reply():
    gateway = FakeGateway()
    gateway.set_control(CID, b"acknowledgement: accepted\n", b"", 0)
    result = asyncio.run(gateway.control(CID, "stop", request_id=RID))
    assert result == ControlExec(
        stdout=b"acknowledgement: accepted\n", stderr=b"", returncode=0
    )
    assert gateway.control_calls[0]["action"] == "stop"
    assert gateway.control_argvs[0] == argv_control(CID, "stop", request_id=RID)


def test_fake_scripted_command_status_reply():
    gateway = FakeGateway()
    gateway.set_command_status(CID, b"request_id: x\n", b"", 0)
    result = asyncio.run(gateway.command_status(CID, RID))
    assert result.stdout == b"request_id: x\n"
    assert gateway.command_status_calls == [(CID, RID)]


def test_fake_scripted_busy_and_timeout():
    gateway = FakeGateway()
    gateway.set_control_busy(CID)
    with pytest.raises(ControlBusyError):
        asyncio.run(gateway.control(CID, "stop", request_id=RID))
    gateway.set_control_timeout(OTHER)
    with pytest.raises(ControlTimeoutError):
        asyncio.run(gateway.control(OTHER, "stop", request_id=RID))
    gateway.set_command_status_error(CID, ControlTimeoutError("timed out"))
    with pytest.raises(ControlTimeoutError):
        asyncio.run(gateway.command_status(CID, RID))


def test_fake_control_validates_like_real():
    gateway = FakeGateway()
    with pytest.raises(ValueError):
        asyncio.run(gateway.control(CID, "explode", request_id=RID))
    with pytest.raises(ValueError):
        asyncio.run(gateway.command_status(CID, "bogus"))
    assert gateway.control_argvs == []


def test_fake_control_in_flight_during_delayed_exec():
    gateway = FakeGateway()
    gateway.control_delay = 0.05

    async def main():
        task = asyncio.create_task(gateway.control(CID, "stop", request_id=RID))
        for _ in range(1000):
            if gateway.control_in_flight(CID):
                break
            await asyncio.sleep(0.001)
        assert gateway.control_in_flight(CID) is True
        with pytest.raises(ControlBusyError):
            await gateway.control(CID, "resume", request_id=RID)
        assert (await task).returncode == 0
        assert gateway.control_in_flight(CID) is False

    asyncio.run(main())


def test_fake_unscripted_control_defaults_to_empty_ok():
    gateway = FakeGateway()
    assert asyncio.run(gateway.control(CID, "stop", request_id=RID)) == ControlExec(
        stdout=b"", stderr=b"", returncode=0
    )


def test_gateway_rejects_nan_control_timeout():
    with pytest.raises(ValueError):
        DockerGateway(control_exec_timeout=float("nan"))


def test_control_exec_timeout_nan_env_rejected(monkeypatch):
    monkeypatch.setenv("AGENT_DASHBOARD_CONTROL_EXEC_TIMEOUT", "nan")
    with pytest.raises(ValueError):
        load_settings([])


def test_control_exec_timeout_nan_flag_rejected():
    with pytest.raises(ValueError):
        load_settings(["--control-exec-timeout", "nan"])


def test_fake_busy_call_logs_no_phantom_argv():
    """A busy-raising fake call logs nothing: the real gateway spawns nothing."""
    gateway = FakeGateway()
    gateway.control_delay = 0.05

    async def main():
        task = asyncio.create_task(gateway.control(CID, "stop", request_id=RID))
        for _ in range(1000):
            if gateway.control_in_flight(CID):
                break
            await asyncio.sleep(0.001)
        assert gateway.control_in_flight(CID) is True
        logged_argvs = list(gateway.control_argvs)
        logged_calls = list(gateway.control_calls)
        with pytest.raises(ControlBusyError):
            await gateway.control(CID, "resume", request_id=RID)
        with pytest.raises(ControlBusyError):
            await gateway.command_status(CID, RID)
        assert gateway.control_argvs == logged_argvs
        assert gateway.control_calls == logged_calls
        assert gateway.command_status_calls == []
        assert (await task).returncode == 0
        assert gateway.control_in_flight(CID) is False

    asyncio.run(main())
