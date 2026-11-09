"""Contract path: discovery, ``status`` polling, headlines, ranks, Fleet view.

Covers the issue's acceptance criteria that the earlier tickets did not:
path selection and naming in discovery, misconfigured and duplicated rows,
the pinned ``status`` argv, the reply-class poll table, headline and rank
tiers (including unknown enums), the collector cycle, contract/legacy sort
order, and web coverage through the replay runner.
"""

import asyncio
import copy
import json
import time

from contract_replay import ReplayProcessRunner
from fake_gateway import FakeGateway
from fastapi.testclient import TestClient

from agent_dashboard.app import create_app
from agent_dashboard.collector import (
    AGENT_UNREACHABLE,
    CONTRACT_MISMATCH,
    GOOD,
    SNAPSHOT_FAILED,
    UNSUPPORTED_OPERATION,
    classify_contract_status,
    run_cycle,
)
from agent_dashboard.config import Settings
from agent_dashboard.contract_status import (
    ContractDetail,
    contract_headline,
    contract_state_rank,
    needs_operator,
    read_contract_snapshot,
)
from agent_dashboard.fleet import FleetState
from agent_dashboard.gateway import (
    AGENT_ROLE_LABEL,
    AGENTCTL_PATH,
    CONTRACT_LABEL,
    CONTRACT_PATH,
    LEGACY_PATH,
    ControlExec,
    ControlTimeoutError,
    DockerGateway,
    GatewayError,
    ManagedInstance,
    argv_contract_exec,
    argv_list_instances,
    parse_ps_output,
)


def _ps_row(
    container_id,
    name,
    state,
    oneoff="",
    contract="",
    instance="",
    exec_user="",
    backend="",
    marker="",
):
    """One 9-column discovery row for the pinned ``PS_FORMAT`` layout."""
    return "\t".join(
        [
            container_id,
            name,
            state,
            oneoff,
            contract,
            instance,
            exec_user,
            backend,
            marker,
        ]
    )


def _contract_row(
    container_id="abc123",
    name="c-web-1",
    state="running",
    contract="1",
    instance="web-1",
    exec_user="agent",
    backend="coding-agent",
    marker="",
):
    return _ps_row(
        container_id,
        name,
        state,
        contract=contract,
        instance=instance,
        exec_user=exec_user,
        backend=backend,
        marker=marker,
    )


def _reply(name):
    """The pinned package fixture as ``(stdout, exit_code)``."""
    from agent_operator_contract import documents

    fixture = documents.load_fixture(name)
    return fixture["stdout"].encode("utf-8"), fixture["exit_code"]


def _result(name):
    """The ``result`` of a pinned package ``status`` fixture."""
    from agent_operator_contract import documents

    return json.loads(documents.load_fixture(name)["stdout"])["result"]


def _run(state, gateway, **kwargs):
    kwargs.setdefault("now", 1000.0)
    asyncio.run(run_cycle(state, gateway, **kwargs))


def _rows(state, now=1012.0):
    rows, error = asyncio.run(state.snapshot(now=now))
    assert error is None
    return rows


# --- discovery: path selection and naming ------------------------------------


def test_discovery_lists_both_label_sets():
    argv = argv_list_instances()
    assert f"label={AGENT_ROLE_LABEL}" in argv
    assert f"label={CONTRACT_LABEL}" in argv


def test_contract_label_wins_over_legacy():
    rows = parse_ps_output(_contract_row())
    assert len(rows) == 1
    (row,) = rows
    assert row.path == CONTRACT_PATH
    assert row.name == "web-1"
    assert row.container_id == "abc123"
    assert row.instance == "web-1"
    assert row.exec_user == "agent"
    assert row.backend == "coding-agent"


def test_legacy_row_keeps_legacy_path():
    rows = parse_ps_output(_ps_row("abc123", "worker-1", "running"))
    assert [(r.name, r.path) for r in rows] == [("worker-1", LEGACY_PATH)]


def test_oneoff_and_offline_read_marker_rows_are_ignored():
    output = "\n".join(
        [
            _ps_row("aaa", "oneoff-run", "running", oneoff="True"),
            _contract_row(container_id="bbb", marker="1"),
            _contract_row(container_id="ccc"),
        ]
    )
    rows = parse_ps_output(output)
    assert [r.container_id for r in rows] == ["ccc"]


def test_contract_instance_addresses_urls_while_control_binds_by_id():
    (row,) = parse_ps_output(_contract_row(container_id="abc123", name="c-old"))
    assert row.name == "web-1"
    assert row.container_id == "abc123"


# --- misconfigured rows ------------------------------------------------------


def test_misconfigured_rows_show_the_reason_and_keep_the_container_name():
    bad_name = _contract_row(instance="Bad Name!")
    missing_user = _contract_row(container_id="b", instance="web-2", exec_user="")
    root_user = _contract_row(container_id="c", instance="web-3", exec_user="root")
    zero_user = _contract_row(container_id="d", instance="web-4", exec_user="0")
    rows = parse_ps_output("\n".join([bad_name, missing_user, root_user, zero_user]))
    assert len(rows) == 4
    assert all(r.path == CONTRACT_PATH for r in rows)
    assert rows[0].name == "c-web-1"
    assert "invalid instance name" in (rows[0].note or "")
    assert "missing exec-user" in (rows[1].note or "")
    assert "invalid exec user 'root'" in (rows[2].note or "")
    assert "invalid exec user '0'" in (rows[3].note or "")


def test_misconfigured_rows_are_never_polled():
    state = FleetState()
    gateway = FakeGateway(
        parse_ps_output(
            "\n".join(
                [
                    _contract_row(instance="Bad Name!"),
                    _contract_row(container_id="b", instance="ok-1", exec_user=""),
                ]
            )
        )
    )
    _run(state, gateway)
    assert gateway.contract_calls == []
    assert gateway.telemetry_calls == []
    (first, second) = _rows(state)
    assert "invalid instance name" in first.headline
    assert "missing exec-user" in second.headline


# --- duplicated rows ---------------------------------------------------------


def test_two_containers_sharing_an_address_are_never_addressed():
    output = "\n".join(
        [
            _contract_row(container_id="aaa", instance="web-1"),
            _contract_row(container_id="bbb", instance="web-1"),
        ]
    )
    rows = parse_ps_output(output)
    assert len(rows) == 2
    assert all(r.duplicate for r in rows)
    assert all(r.note and "duplicated instance address" in r.note for r in rows)

    state = FleetState()
    gateway = FakeGateway(rows)
    gateway.set_contract_snapshot("aaa", _result("status-idle"))
    gateway.set_contract_snapshot("bbb", _result("status-idle"))
    _run(state, gateway)
    assert gateway.contract_calls == []
    rendered = _rows(state)
    assert len(rendered) == 2
    assert all("duplicated instance address" in r.headline for r in rendered)


def test_legacy_name_equal_to_contract_instance_is_a_duplicate():
    output = "\n".join(
        [
            _ps_row("aaa", "web-1", "running"),
            _contract_row(container_id="bbb", instance="web-1"),
        ]
    )
    rows = parse_ps_output(output)
    assert len(rows) == 2
    assert all(r.note and "duplicated instance address" in r.note for r in rows)

    state = FleetState()
    gateway = FakeGateway(rows)
    _run(state, gateway)
    assert gateway.contract_calls == []
    assert gateway.telemetry_calls == []


# --- status argv -------------------------------------------------------------


EXPECTED_STATUS_ARGV = [
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


def test_status_argv_pinned():
    argv = argv_contract_exec("abc123", ["status"], exec_user="agent")
    assert argv == EXPECTED_STATUS_ARGV


def test_status_argv_has_no_tty_or_stdin_flags():
    assert "-t" not in EXPECTED_STATUS_ARGV
    assert "-i" not in EXPECTED_STATUS_ARGV
    assert "--tty" not in EXPECTED_STATUS_ARGV
    assert "--interactive" not in EXPECTED_STATUS_ARGV


def test_status_runs_as_the_labelled_user_with_the_default_bound():
    seen = {}

    class CapturingRunner:
        async def run(self, argv, *, timeout, timeout_error):
            seen["argv"] = argv
            seen["timeout"] = timeout
            return ControlExec(stdout=b"{}", stderr=b"", returncode=0)

    gateway = DockerGateway(process_runner=CapturingRunner())
    asyncio.run(gateway.contract_status("abc123", "someone"))
    assert seen["argv"] == [
        "docker",
        "exec",
        "--user",
        "someone",
        "abc123",
        AGENTCTL_PATH,
        "--contract",
        "1",
        "status",
    ]
    assert seen["timeout"] == 10.0


def test_status_replays_a_package_fixture():
    runner = ReplayProcessRunner.from_package(["status-idle"])
    gateway = DockerGateway(process_runner=runner)
    exec_result = asyncio.run(gateway.contract_status("abc123", "agent"))
    assert exec_result.returncode == 0
    assert b'"ok":true' in exec_result.stdout.replace(b" ", b"")
    assert runner.remaining == 0
    assert runner.calls == [EXPECTED_STATUS_ARGV]


# --- poll outcomes -----------------------------------------------------------


def test_success_is_a_good_snapshot():
    stdout, returncode = _reply("status-idle")
    outcome, snapshot = classify_contract_status(stdout, returncode, now=1.0)
    assert outcome.kind == GOOD
    assert snapshot == _result("status-idle")


def test_agent_unreachable_envelope_is_agent_unreachable():
    stdout, returncode = _reply("agent-unreachable")
    outcome, snapshot = classify_contract_status(stdout, returncode, now=1.0)
    assert outcome.kind == AGENT_UNREACHABLE
    assert snapshot is None


def test_docker_transport_failures_are_agent_unreachable():
    for name in ("transport-125", "transport-126", "transport-127"):
        stdout, returncode = _reply(name)
        outcome, snapshot = classify_contract_status(stdout, returncode, now=1.0)
        assert outcome.kind == AGENT_UNREACHABLE, name
        assert f"docker exec exited with status {returncode}" in outcome.reason
        assert snapshot is None


def test_dashboard_timeout_is_unreachable():
    state = FleetState()
    gateway = FakeGateway(
        [
            ManagedInstance(
                "web-1",
                "abc123",
                "running",
                path=CONTRACT_PATH,
                instance="web-1",
                exec_user="agent",
            )
        ]
    )
    gateway.set_contract_error(
        "abc123", ControlTimeoutError("docker exec timed out after 10 s")
    )
    _run(state, gateway)
    (row,) = _rows(state)
    assert row.reach == "unreachable"
    assert "timed out" in row.headline


def test_gateway_error_is_agent_unreachable():
    state = FleetState()
    gateway = FakeGateway(
        [
            ManagedInstance(
                "web-1",
                "abc123",
                "running",
                path=CONTRACT_PATH,
                instance="web-1",
                exec_user="agent",
            )
        ]
    )
    gateway.set_contract_error("abc123", GatewayError("boom"))
    _run(state, gateway)
    (row,) = _rows(state)
    assert row.reach == "unreachable"
    assert "boom" in row.headline


def test_internal_is_snapshot_failed():
    stdout, returncode = _reply("internal")
    outcome, snapshot = classify_contract_status(stdout, returncode, now=1.0)
    assert outcome.kind == SNAPSHOT_FAILED
    assert snapshot is None


def test_malformed_stdout_is_snapshot_failed():
    for name in ("malformed-empty", "malformed-non-json", "malformed-truncated"):
        stdout, returncode = _reply(name)
        outcome, snapshot = classify_contract_status(stdout, returncode, now=1.0)
        assert outcome.kind == SNAPSHOT_FAILED, name
        assert snapshot is None


def test_success_without_a_result_object_is_snapshot_failed():
    body = json.dumps({"contract": "1.0", "ok": True, "result": None}).encode()
    outcome, snapshot = classify_contract_status(body, 0, now=1.0)
    assert outcome.kind == SNAPSHOT_FAILED
    assert "malformed stdout" in outcome.reason
    assert snapshot is None


def test_contract_mismatch_names_the_supported_contract():
    stdout, returncode = _reply("contract-mismatch")
    outcome, snapshot = classify_contract_status(stdout, returncode, now=1.0)
    assert outcome.kind == CONTRACT_MISMATCH
    assert "[1]" in outcome.reason
    assert snapshot is None


def test_unsupported_is_unsupported_operation():
    stdout, returncode = _reply("unsupported")
    outcome, snapshot = classify_contract_status(stdout, returncode, now=1.0)
    assert outcome.kind == UNSUPPORTED_OPERATION
    assert snapshot is None


def test_unexpected_reply_class_is_snapshot_failed_not_a_crash():
    stdout, returncode = _reply("command-not-found")
    outcome, snapshot = classify_contract_status(stdout, returncode, now=1.0)
    assert outcome.kind == SNAPSHOT_FAILED
    assert "unexpected contract reply" in outcome.reason
    assert snapshot is None


# --- headlines ---------------------------------------------------------------


def test_headline_comes_from_state_and_attempt():
    cases = {
        "status-starting": "Starting: load-volumes",
        "status-recovering": "Recovering",
        "status-idle": "Idle: waiting for a ready issue",
        "status-active": "Working on #42 · run_step",
        "status-paused": "Paused",
    }
    for name, headline in cases.items():
        detail = read_contract_snapshot(_result(name))
        assert detail is not None, name
        assert contract_headline(detail) == headline, name


def test_draining_headline_when_no_stop_is_pending():
    # The pinned draining fixture itself carries a pending stop (which wins);
    # clear the operator intent to reach the draining branch.
    result = _result("status-draining")
    assert contract_headline(read_contract_snapshot(result)) == "Stop requested"
    result["intake"]["pending_stop"] = None
    result["intake"]["stop_plan"] = None
    detail = read_contract_snapshot(result)
    assert detail is not None
    assert contract_headline(detail) == "Draining · #42"


def test_headline_prefers_stop_then_halt_then_attention():
    idle = _result("status-idle")

    pending = copy.deepcopy(idle)
    pending["intake"]["pending_stop"] = {"request_id": "req-abc", "sequence": 1}
    assert contract_headline(read_contract_snapshot(pending)) == "Stop requested"

    planned = copy.deepcopy(idle)
    planned["intake"]["stop_plan"] = {
        "request_id": "req-abc",
        "remaining": 3,
        "counted": 0,
    }
    assert contract_headline(read_contract_snapshot(planned)) == "Stop planned (3 remaining)"

    halted = _result("offline-status-halted")
    assert contract_headline(read_contract_snapshot(halted)) == (
        "Halted (credential-failure): GitHub token rejected (401)"
    )

    attention = copy.deepcopy(idle)
    attention["last_finished"]["attention_required"] = True
    assert contract_headline(read_contract_snapshot(attention)) == (
        "Needs operator: #41 — Delivered"
    )


def test_headline_direct_branches():
    assert contract_headline(ContractDetail(pending_stop=True)) == "Stop requested"
    assert (
        contract_headline(ContractDetail(stop_plan_remaining=2))
        == "Stop planned (2 remaining)"
    )
    assert contract_headline(ContractDetail(instance_state="unknown")) is None
    assert contract_headline(
        ContractDetail(
            last_halt_reason="oom", last_halt_detail="", last_finished_attention=True
        )
    ).startswith("Halted")


def test_needs_operator_and_state_ranks():
    assert needs_operator(ContractDetail(last_halt_reason="oom"))
    assert needs_operator(ContractDetail(last_finished_attention=True))
    assert not needs_operator(ContractDetail(instance_state="active"))
    assert contract_state_rank("starting") == 1
    assert contract_state_rank("recovering") == 1
    assert contract_state_rank("active") == 5
    assert contract_state_rank("draining") == 5
    assert contract_state_rank("idle") == 6
    assert contract_state_rank("paused") == 6
    assert contract_state_rank("unknown") is None


def test_unknown_fields_and_enums_never_break_the_row():
    result = _result("status-idle")
    result["instance"]["state"] = "warping"
    result["instance"]["future_field"] = {"nested": [1, 2, 3]}
    result["brand_new_section"] = "ignored"
    detail = read_contract_snapshot(result)
    assert detail is not None
    assert detail.instance_state == "unknown"
    assert contract_headline(detail) is None

    assert read_contract_snapshot(None) is None
    assert read_contract_snapshot([]) is None
    assert read_contract_snapshot({"ok": True}) is None


def test_row_headlines_render_end_to_end():
    state = FleetState()
    gateway = FakeGateway(
        [
            ManagedInstance(
                "idle-1",
                "aaa111",
                "running",
                path=CONTRACT_PATH,
                instance="idle-1",
                exec_user="agent",
                backend="coding-agent",
            ),
            ManagedInstance(
                "halted-1",
                "bbb222",
                "running",
                path=CONTRACT_PATH,
                instance="halted-1",
                exec_user="agent",
                backend="coding-agent",
            ),
        ]
    )
    gateway.set_contract_snapshot("aaa111", _result("status-idle"))
    gateway.set_contract_snapshot("bbb222", _result("offline-status-halted"))
    _run(state, gateway)
    by_name = {r.name: r for r in _rows(state)}
    assert by_name["idle-1"].headline == "Idle: waiting for a ready issue"
    assert by_name["idle-1"].backend == "coding-agent"
    assert by_name["halted-1"].headline.startswith("Halted (credential-failure)")
    assert by_name["idle-1"].rank == 6
    assert by_name["halted-1"].rank == 0


# --- collector cycle ---------------------------------------------------------


def test_cycle_polls_contract_as_its_user_and_keeps_legacy_telemetry():
    state = FleetState()
    gateway = FakeGateway(
        [
            ManagedInstance(
                "web-1",
                "abc123",
                "running",
                path=CONTRACT_PATH,
                instance="web-1",
                exec_user="someone",
                backend="coding-agent",
            ),
            ManagedInstance("old-1", "def456", "running"),
        ]
    )
    gateway.set_contract_snapshot("abc123", _result("status-idle"))
    gateway.set_snapshot("def456", {"schema_version": 1})
    _run(state, gateway)
    assert gateway.contract_calls == [("abc123", "someone")]
    assert gateway.telemetry_calls == ["def456"]
    by_name = {r.name: r for r in _rows(state)}
    assert by_name["web-1"].path == CONTRACT_PATH
    assert by_name["web-1"].backend == "coding-agent"
    assert by_name["old-1"].path == LEGACY_PATH


def test_contract_mismatch_row_is_unsupported_and_names_supported():
    state = FleetState()
    gateway = FakeGateway(
        [
            ManagedInstance(
                "web-1",
                "abc123",
                "running",
                path=CONTRACT_PATH,
                instance="web-1",
                exec_user="agent",
            )
        ]
    )
    stdout, returncode = _reply("contract-mismatch")
    gateway.set_contract_status("abc123", stdout, returncode)
    _run(state, gateway)
    (row,) = _rows(state)
    assert row.reach == "unsupported"
    assert "[1]" in row.headline


def test_contract_and_legacy_rows_sort_together():
    state = FleetState()
    gateway = FakeGateway(
        [
            ManagedInstance("z-idle", "aaa111", "running"),
            ManagedInstance(
                "a-contract",
                "bbb222",
                "running",
                path=CONTRACT_PATH,
                instance="a-contract",
                exec_user="agent",
            ),
        ]
    )
    gateway.set_snapshot("aaa111", {"schema_version": 1})
    gateway.set_contract_snapshot("bbb222", _result("status-active"))
    _run(state, gateway)
    names = [r.name for r in _rows(state)]
    assert names == ["a-contract", "z-idle"]


# --- web coverage through the replay runner ----------------------------------


def _replay_client(fixture_names, instances):
    gateway = DockerGateway(
        process_runner=ReplayProcessRunner.from_package(fixture_names)
    )

    async def _list():
        return list(instances)

    gateway.list_instances = _list
    state = FleetState()
    now = time.time()
    asyncio.run(run_cycle(state, gateway, max_concurrent_execs=4, now=now))
    app = create_app(
        state=state,
        settings=Settings(poll_interval=5.0),
        gateway=gateway,
        start_poller=False,
    )
    return TestClient(app, base_url="http://localhost"), state, gateway


def test_fleet_page_shows_the_contract_instance():
    client, _, gateway = _replay_client(
        ["status-active"],
        [
            ManagedInstance(
                "web-1",
                "abc123",
                "running",
                path=CONTRACT_PATH,
                instance="web-1",
                exec_user="agent",
                backend="coding-agent",
            )
        ],
    )
    assert gateway.list_instances is not None
    page = client.get("/")
    assert page.status_code == 200
    assert "web-1" in page.text
    assert "coding-agent" in page.text
    assert "Working on #42" in page.text


def test_drawer_shows_the_contract_capsule_without_legacy_widgets():
    client, _, _ = _replay_client(
        ["status-idle"],
        [
            ManagedInstance(
                "web-1",
                "abc123",
                "running",
                path=CONTRACT_PATH,
                instance="web-1",
                exec_user="agent",
                backend="coding-agent",
            )
        ],
    )
    page = client.get("/instances/web-1")
    assert page.status_code == 200
    assert "Contract status" in page.text
    assert "coding-agent" in page.text
    assert "Idle: waiting for a ready issue" in page.text
    assert "Logs" not in page.text
