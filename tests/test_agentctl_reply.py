"""Reply parser and version table (spec sections 4, 5.4).

The kinds table is exercised with the agent's published reply-contract
examples copied under ``tests/fixtures/contract/v1/agentctl/`` (contract:
``docs/agents/agentctl-command-replies.md`` in simple-coding-agent).
A contract change on the agent side shows up here as a failing test.

The synthetic fixtures left under ``tests/fixtures/agentctl/`` cover
only what the contract has no example for, and each test using one says
so: ``PRE-CONTRACT`` marks today's (pre-contract) agent output the
parser must keep tolerating, ``MALFORMED`` marks a malformed input the
contract has no example for.
"""

import json
from pathlib import Path

import pytest

from agent_dashboard.agentctl_reply import (
    MINIMUM_VERSIONS,
    REASON_MAX_LENGTH,
    agent_supports,
    docker_unavailable_reply,
    parse_agent_version,
    parse_command_reply,
    parse_control_reply,
    reply_for_exception,
    supports_expect_repo,
    timeout_reply,
)
from agent_dashboard.gateway import (
    ControlBusyError,
    ControlExec,
    ControlTimeoutError,
    DockerNotAvailableError,
)

FIXTURES = Path(__file__).parent / "fixtures" / "agentctl"
CONTRACT = Path(__file__).parent / "fixtures" / "contract" / "v1" / "agentctl"

CONTRACT_REPO = "example-owner/example-repo"

REPO = "acme-corp/web-app"
RID = "dash-0123456789abcdef"


def read_fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def read_contract(name: str) -> ControlExec:
    """One copied contract example as the exec it pins.

    Each ``examples/agentctl/*.json`` file is ``{"argv", "exit_code",
    "stdout", "stderr"}`` and byte-identical to real CLI output.
    """
    payload = json.loads((CONTRACT / name).read_text())
    return ControlExec(
        stdout=payload["stdout"].encode(),
        stderr=payload["stderr"].encode(),
        returncode=payload["exit_code"],
    )


def control(stdout: bytes, stderr: bytes, returncode: int) -> ControlExec:
    return ControlExec(stdout=stdout, stderr=stderr, returncode=returncode)


# --- kinds table: mutating Control actions (contract examples) ----------------


def test_exit_0_is_accepted_with_all_fields():
    reply = parse_control_reply(
        read_contract("accepted.json"),
        expected_repository=CONTRACT_REPO,
    )
    assert reply.kind == "accepted"
    assert reply.repository == CONTRACT_REPO
    assert reply.request_id == "req-example-accepted"
    assert reply.sequence == 1
    assert reply.acknowledgement == "accepted"
    assert reply.effect == "finish active attempt, then stop intake"
    assert reply.repository_mismatch is False
    assert reply.repository_note is None
    assert reply.exit_code == 0


def test_exit_0_completed_acknowledgement_is_accepted():
    reply = parse_control_reply(
        read_contract("completed.json"),
        expected_repository=CONTRACT_REPO,
    )
    assert reply.kind == "accepted"
    assert reply.repository == CONTRACT_REPO
    assert reply.request_id == "req-example-completed"
    assert reply.sequence == 1
    assert reply.acknowledgement == "completed"
    assert reply.effect == "intake stopped"
    assert reply.repository_mismatch is False
    assert reply.exit_code == 0


def test_exit_1_is_rejected_with_reason():
    reply = parse_control_reply(
        read_contract("rejected.json"),
        expected_repository=CONTRACT_REPO,
    )
    assert reply.kind == "rejected"
    assert reply.repository == CONTRACT_REPO
    assert reply.repository_mismatch is False
    assert "req-example-rejected" in reply.reason
    assert "No control change was accepted." in reply.reason
    assert reply.exit_code == 1


def test_exit_2_usage_is_usage_error():
    reply = parse_control_reply(read_contract("usage-error.json"))
    assert reply.kind == "usage_error"
    assert "expected one argument" in reply.reason
    assert reply.exit_code == 2


def test_exit_2_usage_line_need_not_be_first():
    stderr = b"warning: slow socket\nusage: agentctl stop --request-id=ID\n"
    assert parse_control_reply(control(b"", stderr, 2)).kind == "usage_error"


def test_exit_2_without_usage_is_unknown():
    reply = parse_control_reply(
        read_contract("not-submitted.json"),
        expected_repository=CONTRACT_REPO,
    )
    assert reply.kind == "unknown"
    assert "req-example-not-submitted" in reply.reason
    assert reply.repository is None
    assert reply.exit_code == 2


@pytest.mark.parametrize("code", [125, 126, 127])
def test_docker_could_not_run_agentctl_is_failed(code):
    # No contract example: Docker never started agentctl, so the agent
    # answered nothing.
    reply = parse_control_reply(control(b"", b"docker: error", code))
    assert reply.kind == "failed"
    assert reply.exit_code == code


@pytest.mark.parametrize("code", [3, 4, 99, 128, 255, -1])
def test_any_other_code_is_unknown(code):
    assert parse_control_reply(control(b"", b"boom", code)).kind == "unknown"


def test_timeout_exception_is_unknown():
    reply = reply_for_exception(ControlTimeoutError("docker exec timed out after 15 s"))
    assert reply.kind == "unknown"
    assert "timed out" in reply.reason
    assert reply.exit_code is None


def test_timeout_helper_is_unknown():
    assert timeout_reply("docker exec timed out after 15 s").kind == "unknown"


def test_docker_missing_exception_is_failed():
    reply = reply_for_exception(DockerNotAvailableError("docker CLI not found"))
    assert reply.kind == "failed"
    assert reply.exit_code is None


def test_docker_missing_helper_is_failed():
    assert docker_unavailable_reply("docker CLI not found").kind == "failed"


def test_busy_is_not_a_reply():
    with pytest.raises(ControlBusyError):
        reply_for_exception(ControlBusyError("busy"))


def test_unexpected_exception_is_reraised():
    with pytest.raises(ValueError, match="boom"):
        reply_for_exception(ValueError("boom"))


# --- agentctl command <id> (contract examples) ---------------------------------


def test_command_exit_0_yields_the_record():
    reply = parse_command_reply(
        read_contract("command-found.json"),
        expected_repository=CONTRACT_REPO,
    )
    assert reply.kind == "record"
    assert reply.repository == CONTRACT_REPO
    assert reply.request_id == "req-example-record"
    assert reply.sequence == 1
    assert reply.acknowledgement == "completed"
    assert reply.repository_mismatch is False
    assert reply.effect == "intake stopped"
    assert reply.exit_code == 0


def test_command_exit_1_is_not_recorded():
    reply = parse_command_reply(read_contract("command-not-found.json"))
    assert reply.kind == "not_recorded"
    assert "no such command" in reply.reason
    assert reply.repository == CONTRACT_REPO
    assert reply.exit_code == 1


def test_command_exit_2_is_unknown():
    # The contract has no command-specific exit-2 example; the shared
    # not-submitted example covers the shared exit-2-unknown path the
    # command parser reuses.
    reply = parse_command_reply(
        read_contract("not-submitted.json"),
        expected_repository=CONTRACT_REPO,
    )
    assert reply.kind == "unknown"
    assert "req-example-not-submitted" in reply.reason


def test_command_exit_2_usage_is_usage_error():
    reply = parse_command_reply(read_contract("usage-error.json"))
    assert reply.kind == "usage_error"


@pytest.mark.parametrize("code", [125, 126, 127])
def test_command_docker_failure_is_failed(code):
    assert parse_command_reply(control(b"", b"docker: error", code)).kind == "failed"


def test_command_other_codes_are_unknown():
    assert parse_command_reply(control(b"", b"x", 42)).kind == "unknown"


def test_command_timeout_is_unknown_and_docker_missing_is_failed():
    assert timeout_reply("timed out").kind == "unknown"
    assert docker_unavailable_reply("no docker").kind == "failed"
    assert reply_for_exception(ControlTimeoutError("t")).kind == "unknown"
    assert reply_for_exception(DockerNotAvailableError("d")).kind == "failed"


# --- pre-contract tolerance: today's output without a repository line ---------


def test_pre_contract_rejection_without_repository_line():
    # PRE-CONTRACT: agents before the reply contract reject without the
    # stderr `repository:` line; the parser must keep tolerating that.
    reply = parse_control_reply(
        control(b"", read_fixture("rejected-no-repository.stderr"), 1),
        expected_repository=REPO,
    )
    assert reply.kind == "rejected"
    assert reply.repository is None
    assert reply.repository_mismatch is None
    assert "already committed" in reply.reason
    assert reply.exit_code == 1


def test_pre_contract_command_not_found_without_repository_line():
    # PRE-CONTRACT: "no such command" before the contract carried no
    # `repository:` line; the parser must keep tolerating that.
    reply = parse_command_reply(
        control(b"", read_fixture("command-not-recorded.stderr"), 1)
    )
    assert reply.kind == "not_recorded"
    assert reply.repository is None
    assert "no such command" in reply.reason
    assert reply.exit_code == 1


def test_pre_contract_missing_repository_on_exit_0_is_no_mismatch_with_a_note():
    # PRE-CONTRACT: exit-0 replies before the contract may name no
    # repository; that is no mismatch, with a note.
    reply = parse_control_reply(
        control(read_fixture("completed-no-repository.stdout"), b"", 0),
        expected_repository=REPO,
    )
    assert reply.kind == "accepted"
    assert reply.repository is None
    assert reply.acknowledgement == "completed"
    assert reply.sequence == 13
    assert reply.repository_mismatch is None
    assert reply.repository_note == "the reply did not name a repository"


# --- tolerance rules: malformed input the contract has no example for --------


def test_unknown_keys_are_ignored_but_kept_in_raw():
    # MALFORMED (contract has no example): the contract says new keys are
    # additive and readers ignore unknown keys; the parser keeps them in
    # raw for tests without rendering them.
    reply = parse_control_reply(
        control(read_fixture("unknown-keys.stdout"), b"", 0),
        expected_repository=REPO,
    )
    assert reply.kind == "accepted"
    assert reply.acknowledgement == "accepted"
    assert reply.raw["new_future_key"] == "something the dashboard does not know yet"
    assert reply.effect == "handoff started"


def test_non_integer_sequence_is_none():
    # MALFORMED (contract has no example): a non-integer sequence reads
    # as none rather than raising.
    reply = parse_control_reply(
        control(read_fixture("non-integer-sequence.stdout"), b"", 0),
        expected_repository=REPO,
    )
    assert reply.kind == "accepted"
    assert reply.sequence is None


@pytest.mark.parametrize(
    "sequence",
    ["", "  ", "12.0", "12abc", "0x10", "+12", "-3", "1_0", "999999999999999999999999x"],
)
def test_unparsable_sequences_are_none(sequence):
    stdout = f"repository: {REPO}\nsequence: {sequence}\nacknowledgement: accepted\n".encode()
    assert parse_control_reply(control(stdout, b"", 0)).sequence is None


def test_duplicate_keys_first_wins():
    # MALFORMED (contract has no example): the contract prints one line
    # per key, so a repeated key is malformed input; the first occurrence
    # wins and never raises.
    reply = parse_control_reply(
        control(read_fixture("duplicate-keys.stdout"), b"", 0),
        expected_repository=REPO,
    )
    assert reply.repository == "acme-corp/web-app"
    assert reply.request_id == RID
    assert reply.effect == "first effect wins"
    assert reply.raw["repository"] == "acme-corp/web-app"
    assert reply.repository_mismatch is False


def test_stderr_repository_line_on_a_rejection():
    reply = parse_control_reply(
        read_contract("rejected.json"),
        expected_repository="other-owner/other-repo",
    )
    assert reply.kind == "rejected"
    assert reply.repository == CONTRACT_REPO
    assert reply.repository_mismatch is True
    # Only the first line is the repository; the rest is the reason.
    assert "repository:" not in reply.reason
    assert "No control change was accepted." in reply.reason


def test_stderr_repository_matching_is_no_mismatch():
    stderr = b"repository: acme-corp/web-app\nrejected: stale binding\n"
    reply = parse_control_reply(control(b"", stderr, 1), expected_repository=REPO)
    assert reply.repository == REPO
    assert reply.repository_mismatch is False


def test_stderr_repository_comparison_is_case_insensitive():
    stderr = b"repository: ACME-CORP/WEB-APP\nrejected: nope\n"
    reply = parse_control_reply(control(b"", stderr, 1), expected_repository=REPO)
    assert reply.repository_mismatch is False


def test_key_order_is_not_relied_on_and_missing_fields_are_none():
    reply = parse_control_reply(control(b"effect: only this\n", b"", 0))
    assert reply.kind == "accepted"
    assert reply.effect == "only this"
    assert reply.repository is None
    assert reply.request_id is None
    assert reply.sequence is None
    assert reply.acknowledgement is None


def test_non_matching_lines_are_ignored():
    stdout = b"hello world\n   \nacknowledgement: accepted\nnot a pair\n: empty\n"
    reply = parse_control_reply(control(stdout, b"", 0))
    assert reply.kind == "accepted"
    assert reply.acknowledgement == "accepted"
    assert "" not in reply.raw


def test_empty_stdout_on_exit_0_is_accepted_with_no_fields():
    reply = parse_control_reply(control(b"", b"", 0), expected_repository=REPO)
    assert reply.kind == "accepted"
    assert reply.repository is None
    assert reply.repository_mismatch is None
    assert reply.repository_note == "the reply did not name a repository"
    assert reply.raw == {}


def test_reason_is_stripped_and_capped():
    stderr = b"\n\n  padded reason  \n\n"
    assert parse_control_reply(control(b"", stderr, 1)).reason == "padded reason"
    long_reason = b"x" * (REASON_MAX_LENGTH + 500)
    assert len(parse_control_reply(control(b"", long_reason, 1)).reason) == (
        REASON_MAX_LENGTH
    )


def test_reason_is_display_only():
    first = parse_control_reply(control(b"", b"alpha", 1))
    second = parse_control_reply(control(b"", b"beta", 1))
    assert (first.kind, second.kind) == ("rejected", "rejected")


# --- no input makes the parser raise ------------------------------------------


@pytest.mark.parametrize("returncode", [0, 1, 2, 3, 125, 200])
@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"\x00\x01\x02",
        b"\xff\xfe\x00\x01 not utf-8 \xff",
        b"repository:\xff\xfe\nsequence: \x80\n",
        b"\n" * 1000,
        b"a" * 100000,
        "unicode—dash: value\n".encode(),
    ],
)
def test_garbage_never_raises_control(returncode, payload):
    reply = parse_control_reply(control(payload, payload, returncode))
    assert reply.kind in {"accepted", "rejected", "usage_error", "unknown", "failed"}


@pytest.mark.parametrize("returncode", [0, 1, 2, 3, 125])
def test_garbage_never_raises_command(returncode):
    payload = b"\xff\xfe garbage \x00 repository: \xff\n"
    reply = parse_command_reply(control(payload, payload, returncode))
    assert reply.kind in {"record", "not_recorded", "usage_error", "unknown", "failed"}


def test_invalid_utf8_stdout_still_reads_matching_lines():
    stdout = b"acknowledgement: accepted\n" + b"\xff\xfe\n"
    reply = parse_control_reply(control(stdout, b"", 0))
    assert reply.kind == "accepted"
    assert reply.acknowledgement == "accepted"


# --- version gating -------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("0.7.2", (0, 7, 2)),
        ("v0.7.2", (0, 7, 2)),
        ("v0.7.2-rc1", (0, 7, 2)),
        ("1.2.3+build", (1, 2, 3)),
        ("  v10.20.30  ", (10, 20, 30)),
        ("v0.5.0 (telemetry)", (0, 5, 0)),
    ],
)
def test_parse_agent_version_reads_leading_triple(text, want):
    assert parse_agent_version(text) == want


@pytest.mark.parametrize(
    "text",
    ["", "v1.2", "1.2", "v1.2.x", "not a version", "v", "version 3", None, 123, b"v1.2.3"],
)
def test_parse_agent_version_unreadable_is_none(text):
    assert parse_agent_version(text) is None


def test_unreadable_version_counts_as_old():
    minimums = dict(MINIMUM_VERSIONS)
    try:
        MINIMUM_VERSIONS["probe"] = (0, 5, 0)
        assert agent_supports("probe", "v0.5.0") is True
        assert agent_supports("probe", "v0.4.9") is False
        assert agent_supports("probe", "") is False
        assert agent_supports("probe", None) is False
        assert agent_supports("probe", "garbage") is False
        assert agent_supports("no-such-feature", "v99.0.0") is False
    finally:
        MINIMUM_VERSIONS.clear()
        MINIMUM_VERSIONS.update(minimums)


def test_pending_expect_repo_never_enables_the_flag():
    assert MINIMUM_VERSIONS["expect_repo"] is None
    for version in ["v0.5.0", "v0.7.2", "v99.0.0", "", None, "garbage", "v1.2.3-rc1"]:
        assert supports_expect_repo(version) is False
        assert agent_supports("expect_repo", version) is False
