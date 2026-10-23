"""Submit route and feedback cards (spec sections 5.3, 6.3, 7, 8.3, 8.4, 9).

Pressing Confirm sends the Control action and shows its outcome as a
feedback card. Submit runs its checks in order (request protection,
validation, target binding, the ``submitted`` audit entry, the gateway
exec, reply parsing, the outcome entry) and returns one card; the
composer and the timeline summary come back out of band. Tests use the
fake gateway's scripted replies and assert HTML fragments through
TestClient.
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import time
from pathlib import Path

import pytest
from fake_gateway import FakeGateway
from fastapi.testclient import TestClient
from test_web import fresh_snapshot, load_snapshot

from agent_dashboard import control_routes
from agent_dashboard.app import create_app
from agent_dashboard.collector import SNAPSHOT_FAILED, run_cycle
from agent_dashboard.config import Settings
from agent_dashboard.control_audit import audit_path
from agent_dashboard.fleet import FleetState
from agent_dashboard.gateway import DockerNotAvailableError, ManagedInstance


@pytest.fixture(autouse=True)
def _isolated_audit(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))


GOOD_HEADERS = {"Origin": "http://localhost", "HX-Request": "true"}

RID = "dash-0123456789abcdef"
REPO = "example-owner/example-repo"


def make_submit_client(snapshots: dict[str, dict], settings: Settings | None = None):
    """A submit client with hex container IDs (the gateway validates them)."""
    now = time.time()
    state = FleetState()
    cids = {name: f"{0xABC000 + index:x}" for index, name in enumerate(snapshots)}
    gateway = FakeGateway(
        [ManagedInstance(name, cid, "running") for name, cid in cids.items()]
    )
    for name, snap in snapshots.items():
        gateway.set_snapshot(cids[name], fresh_snapshot(copy.deepcopy(snap), now))
    asyncio.run(run_cycle(state, gateway, max_concurrent_execs=4, now=now))
    app = create_app(
        state=state,
        settings=settings if settings is not None else Settings(poll_interval=5.0),
        gateway=gateway,
        start_poller=False,
    )
    return TestClient(app, base_url="http://localhost"), state, gateway, cids


def submit(client: TestClient, name: str, data: dict, headers: dict | None = None):
    if headers is None:
        headers = dict(GOOD_HEADERS)
    return client.post(f"/instances/{name}/control/submit", data=data, headers=headers)


def base_form(cids: dict, name: str = "work", **overrides) -> dict:
    form = {
        "action": "stop",
        "container_id": cids[name],
        "repository": REPO,
        "request_id": RID,
    }
    extra = overrides.pop("extra", None)
    form.update(overrides)
    if extra:
        form.update(extra)
    return form


def audit_entries() -> list[dict]:
    path = audit_path()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def live_client(name: str = "work", fixture: str = "snapshot-attempt-in-progress.json"):
    return make_submit_client({name: load_snapshot(fixture)})


def agentctl_stdout(name: str) -> bytes:
    return (Path(__file__).parent / "fixtures" / "agentctl" / name).read_bytes()


def agentctl_stderr(name: str) -> bytes:
    return (Path(__file__).parent / "fixtures" / "agentctl" / name).read_bytes()


# --- outcome cards ---------------------------------------------------------------


def test_submit_accepted_card_with_effect_and_seq():
    client, _, gateway, cids = live_client()
    gateway.set_control(
        cids["work"],
        (
            f"repository: {REPO}\nrequest_id: {RID}\nsequence: 12\n"
            "acknowledgement: accepted\n"
            "effect: stop will begin after the current attempt finishes\n"
        ).encode(),
        b"",
        0,
    )
    response = submit(client, "work", base_form(cids))
    assert response.status_code == 200
    assert "Accepted" in response.text
    assert "ctl-tone-info" in response.text
    assert "stop will begin after the current attempt finishes" in response.text
    assert "<code>agentctl stop</code>" in response.text
    assert RID in response.text
    assert "seq 12" in response.text
    # One exec with the validated values and no `--expect-repo` (old agent).
    assert len(gateway.control_calls) == 1
    call = gateway.control_calls[0]
    assert call["action"] == "stop"
    assert call["request_id"] == RID
    assert call["expect_repo"] is None
    # The audit wrote `submitted` then the `accepted` outcome, in order.
    entries = audit_entries()
    assert [entry["outcome"] for entry in entries] == ["submitted", "accepted"]
    submitted, outcome = entries
    assert submitted["request_id"] == RID
    assert submitted["retry"] is False
    assert submitted["expect_repo_guard"] is False
    assert submitted["exit_code"] is None
    assert outcome["action"] == "stop"
    assert outcome["params"] == {}
    assert outcome["command"] == "stop"
    assert outcome["exit_code"] == 0
    assert outcome["sequence"] == 12
    assert outcome["acknowledgement"] == "accepted"
    assert outcome["reply_repository"] == REPO
    assert outcome["repository_mismatch"] is False
    assert isinstance(outcome["duration_ms"], int)
    # The composer and the timeline summary come back out of band.
    assert 'hx-swap-oob="true"' in response.text
    assert "ctl-composer" in response.text
    assert "ctl-timeline-summary" in response.text


def test_submit_accepted_records_no_mismatch_for_expected_repository():
    client, _, gateway, cids = live_client()
    gateway.set_control(
        cids["work"],
        (
            f"repository: {REPO}\nrequest_id: {RID}\nsequence: 3\n"
            "acknowledgement: accepted\neffect: stopping soon\n"
        ).encode(),
        b"",
        0,
    )
    response = submit(client, "work", base_form(cids))
    assert response.status_code == 200
    assert "Accepted" in response.text
    assert "stopping soon" in response.text
    outcome = audit_entries()[-1]
    assert outcome["outcome"] == "accepted"
    assert outcome["repository_mismatch"] is False
    assert outcome["reply_repository"] == REPO


def test_submit_completed_card_and_ok_tone():
    client, _, gateway, cids = live_client()
    gateway.set_control(cids["work"], agentctl_stdout("completed-no-repository.stdout"), b"", 0)
    response = submit(client, "work", base_form(cids))
    assert response.status_code == 200
    assert "Completed" in response.text
    assert "ctl-tone-ok" in response.text
    assert "resumed normal operation" in response.text
    # A missing `repository:` line is no mismatch: the card notes it.
    assert "the reply did not name a repository" in response.text
    outcome = audit_entries()[-1]
    assert outcome["outcome"] == "accepted"
    assert outcome["acknowledgement"] == "completed"
    assert outcome["repository_mismatch"] is None
    assert outcome["reply_repository"] is None


def test_submit_rejected_card_with_reason():
    client, _, gateway, cids = live_client()
    gateway.set_control(cids["work"], b"", agentctl_stderr("rejected-no-repository.stderr"), 1)
    response = submit(client, "work", base_form(cids))
    assert response.status_code == 200
    assert "Rejected" in response.text
    assert "ctl-tone-error" in response.text
    assert "No control change was accepted." in response.text
    assert "already committed with a different payload" in response.text
    outcome = audit_entries()[-1]
    assert outcome["outcome"] == "rejected"
    assert outcome["exit_code"] == 1
    assert "already committed" in (outcome["error"] or "")


def test_submit_rejected_with_reply_repository_still_rejected():
    client, _, gateway, cids = live_client()
    gateway.set_control(
        cids["work"], b"", agentctl_stderr("rejected-repository-mismatch.stderr"), 1
    )
    response = submit(client, "work", base_form(cids))
    assert response.status_code == 200
    assert "Rejected" in response.text
    assert "Repository mismatch" not in response.text
    outcome = audit_entries()[-1]
    assert outcome["outcome"] == "rejected"
    assert outcome["reply_repository"] == "acme-corp/other-app"
    assert outcome["repository_mismatch"] is True


def test_submit_unknown_card_with_retry_and_check_status():
    client, _, gateway, cids = live_client()
    gateway.set_control(cids["work"], b"", agentctl_stderr("unreachable.stderr"), 2)
    response = submit(client, "work", base_form(cids))
    assert response.status_code == 200
    assert "Outcome unknown" in response.text
    assert "may or may not have been applied" in response.text
    assert "Retry with the same request ID" in response.text
    assert "Check status" in response.text
    assert 'name="retry" value="1"' in response.text
    assert f'name="request_id" value="{RID}"' in response.text
    outcome = audit_entries()[-1]
    assert outcome["outcome"] == "unknown"
    assert outcome["exit_code"] == 2


def test_submit_unknown_for_other_exit_codes_and_timeout():
    for returncode in (3, 99):
        client, _, gateway, cids = live_client()
        gateway.set_control(cids["work"], b"", b"", returncode)
        response = submit(client, "work", base_form(cids))
        assert response.status_code == 200, returncode
        assert "Outcome unknown" in response.text, returncode
        assert audit_entries()[-1]["outcome"] == "unknown", returncode
    client, _, gateway, cids = live_client()
    gateway.set_control_timeout(cids["work"])
    response = submit(client, "work", base_form(cids))
    assert response.status_code == 200
    assert "Outcome unknown" in response.text
    outcome = audit_entries()[-1]
    assert outcome["outcome"] == "unknown"
    assert outcome["exit_code"] is None


def test_submit_usage_error_is_not_sent():
    client, _, gateway, cids = live_client()
    gateway.set_control(cids["work"], b"", agentctl_stderr("usage-error.stderr"), 2)
    response = submit(client, "work", base_form(cids))
    assert response.status_code == 200
    assert "Not sent" in response.text
    assert "dashboard bug or a version mismatch" in response.text
    outcome = audit_entries()[-1]
    assert outcome["outcome"] == "usage_error"
    assert outcome["exit_code"] == 2


def test_submit_failed_exit_is_not_sent():
    client, _, gateway, cids = live_client()
    gateway.set_control(cids["work"], b"", b"docker: bad parameter", 125)
    response = submit(client, "work", base_form(cids))
    assert response.status_code == 200
    assert "Not sent" in response.text
    assert "Docker could not run agentctl: docker: bad parameter" in response.text
    outcome = audit_entries()[-1]
    assert outcome["outcome"] == "failed"
    assert outcome["exit_code"] == 125


def test_submit_docker_missing_is_not_sent():
    client, _, gateway, cids = live_client()
    gateway.set_control_error(cids["work"], DockerNotAvailableError("docker CLI not found"))
    response = submit(client, "work", base_form(cids))
    assert response.status_code == 200
    assert "Not sent" in response.text
    assert "Docker could not run agentctl" in response.text
    outcome = audit_entries()[-1]
    assert outcome["outcome"] == "failed"
    assert outcome["exit_code"] is None


def test_submit_repository_mismatch_card():
    client, _, gateway, cids = live_client()
    gateway.set_control(
        cids["work"],
        (
            "repository: other-owner/other-repo\n"
            f"request_id: {RID}\nsequence: 7\nacknowledgement: accepted\n"
            "effect: stopping\n"
        ).encode(),
        b"",
        0,
    )
    response = submit(client, "work", base_form(cids))
    assert response.status_code == 200
    assert "Repository mismatch" in response.text
    assert "other-owner/other-repo" in response.text
    assert REPO in response.text
    assert "Nothing was undone." in response.text
    outcome = audit_entries()[-1]
    assert outcome["outcome"] == "accepted"
    assert outcome["repository_mismatch"] is True
    assert outcome["reply_repository"] == "other-owner/other-repo"


def test_submit_stop_after_sends_params_and_command():
    client, _, gateway, cids = live_client()
    gateway.set_control(
        cids["work"],
        (
            f"repository: {REPO}\nrequest_id: {RID}\nsequence: 4\n"
            "acknowledgement: accepted\neffect: ok\n"
        ).encode(),
        b"",
        0,
    )
    form = base_form(cids, action="stop_after", extra={"n": "3"})
    response = submit(client, "work", form)
    assert response.status_code == 200
    assert "<code>agentctl stop --after 3</code>" in response.text
    assert gateway.control_calls[0]["n"] == 3
    entries = audit_entries()
    assert entries[0]["params"] == {"n": 3}
    assert entries[0]["command"] == "stop --after 3"
    assert entries[-1]["params"] == {"n": 3}


# --- request protection, unknown name, validation ---------------------------------


def test_submit_unknown_instance_is_404_without_audit():
    client, _, gateway, _ = live_client()
    response = submit(client, "nope", {"action": "stop"})
    assert response.status_code == 404
    assert "instance not found" in response.text
    assert audit_entries() == []
    assert gateway.control_calls == []


PROTECTION_CASES = [
    (
        "origin with another port",
        {"Origin": "http://localhost:9999", "HX-Request": "true"},
        "Request refused: missing or mismatched Origin header",
    ),
    (
        "missing origin",
        {"HX-Request": "true"},
        "Request refused: missing or mismatched Origin header",
    ),
    (
        "cross-site fetch",
        {"Origin": "http://localhost", "HX-Request": "true", "Sec-Fetch-Site": "cross-site"},
        "Request refused: Sec-Fetch-Site must be same-origin",
    ),
    (
        "missing htmx header",
        {"Origin": "http://localhost"},
        "Request refused: missing HX-Request header",
    ),
]


@pytest.mark.parametrize("label,headers,fragment", PROTECTION_CASES)
def test_submit_request_protection(label, headers, fragment):
    client, _, gateway, cids = live_client()
    response = submit(client, "work", base_form(cids), headers=headers)
    assert response.status_code == 403, label
    assert "Refused" in response.text, label
    assert fragment in response.text, label
    entries = audit_entries()
    assert len(entries) == 1, label
    assert entries[0]["outcome"] == "refused", label
    assert gateway.control_calls == [], label


def test_submit_control_off_is_403():
    client, _, gateway, cids = make_submit_client(
        {"work": load_snapshot("snapshot-attempt-in-progress.json")},
        Settings(poll_interval=5.0, control=False),
    )
    response = submit(client, "work", base_form(cids))
    assert response.status_code == 403
    assert "Refused" in response.text
    assert "Controls unavailable: control is off" in response.text
    assert audit_entries()[0]["reason"] == "controls_unavailable"
    assert gateway.control_calls == []


VALIDATION_CASES = [
    ("bad action", {"action": "explode"}, "Invalid action"),
    ("bad n", {"action": "stop_after", "n": "0"}, "Invalid attempts before stopping"),
    ("missing n", {"action": "stop_after"}, "Invalid attempts before stopping"),
    ("bad issue", {"action": "next_issue", "issue": "oops"}, "Invalid issue number"),
    ("bad request id", {"request_id": "bogus"}, "Invalid request ID"),
    ("bad container id", {"container_id": "not-a-container!"}, "Invalid container ID"),
    ("bad repository", {"repository": "not a repo"}, "Invalid repository"),
    ("unexpected param", {"n": "3"}, "takes no parameter"),
    ("bad retry", {"retry": "2"}, "Invalid retry flag"),
]


@pytest.mark.parametrize("label,overrides,fragment", VALIDATION_CASES)
def test_submit_validation_is_422_refused(label, overrides, fragment):
    client, _, gateway, cids = live_client()
    response = submit(client, "work", base_form(cids, **overrides))
    assert response.status_code == 422, label
    assert "Refused" in response.text, label
    assert fragment in response.text, label
    entries = audit_entries()
    assert len(entries) == 1, label
    assert entries[0]["reason"] == "validation", label
    assert gateway.control_calls == [], label


def test_submit_validation_failure_keeps_parseable_request_id():
    client, _, _, cids = live_client()
    response = submit(client, "work", base_form(cids, action="explode"))
    assert response.status_code == 422
    assert RID in response.text
    # The request ID was well-formed, so the audit keeps it; a malformed
    # request ID reads as null (covered below).
    assert audit_entries()[0]["request_id"] == RID


def test_submit_malformed_request_id_audits_null():
    client, _, _, cids = live_client()
    response = submit(client, "work", base_form(cids, request_id="bogus"))
    assert response.status_code == 422
    assert audit_entries()[0]["request_id"] is None


def test_submit_garbage_body_is_422_never_500():
    client, _, gateway, _ = live_client()
    response = submit(client, "work", {})
    assert response.status_code == 422
    assert "Refused" in response.text
    assert gateway.control_calls == []


# --- target binding -----------------------------------------------------------------


def test_submit_refuses_recreated_container():
    client, _, gateway, cids = live_client()
    response = submit(client, "work", base_form(cids, container_id="abc123"))
    assert response.status_code == 409
    assert "Refused" in response.text
    assert "Instance replaced or unavailable: refresh the view. Nothing was sent." in response.text
    entries = audit_entries()
    assert len(entries) == 1
    assert entries[0]["reason"] == "stale_binding"
    assert entries[0]["container_id"] == "abc123"
    assert gateway.control_calls == []


def test_submit_refuses_non_live_reach():
    client, state, gateway, cids = live_client()
    gateway.set_error_code(cids["work"], SNAPSHOT_FAILED, "boom")
    asyncio.run(run_cycle(state, gateway, max_concurrent_execs=4, now=time.time() + 30))
    response = submit(client, "work", base_form(cids))
    assert response.status_code == 409
    assert "Instance replaced or unavailable" in response.text
    assert audit_entries()[0]["reason"] == "stale_binding"
    assert gateway.control_calls == []


def test_submit_refuses_changed_repository():
    client, _, gateway, cids = live_client()
    response = submit(client, "work", base_form(cids, repository="other-owner/other-repo"))
    assert response.status_code == 409
    assert "Instance replaced or unavailable" in response.text
    entries = audit_entries()
    assert entries[0]["reason"] == "stale_binding"
    assert entries[0]["expected_repository"] == "other-owner/other-repo"
    assert gateway.control_calls == []


# --- audit write order and failures ----------------------------------------------------


def test_submit_write_order_submitted_before_exec(monkeypatch):
    client, _, gateway, cids = live_client()
    gateway.set_control(
        cids["work"],
        (
            f"repository: {REPO}\nrequest_id: {RID}\nsequence: 1\n"
            "acknowledgement: accepted\neffect: ok\n"
        ).encode(),
        b"",
        0,
    )
    events: list[tuple] = []
    original_append = control_routes.append_entry

    def recording_append(entry):
        events.append(("audit", entry["outcome"]))
        return original_append(entry)

    monkeypatch.setattr(control_routes, "append_entry", recording_append)
    original_control = gateway.control

    async def recording_control(*args, **kwargs):
        events.append(("exec",))
        return await original_control(*args, **kwargs)

    monkeypatch.setattr(gateway, "control", recording_control)
    response = submit(client, "work", base_form(cids))
    assert response.status_code == 200
    assert events == [("audit", "submitted"), ("exec",), ("audit", "accepted")]


def test_submit_failing_submitted_write_sends_nothing(monkeypatch):
    def boom(entry):
        raise OSError("disk full")

    monkeypatch.setattr(control_routes, "append_entry", boom)
    client, _, gateway, cids = live_client()
    response = submit(client, "work", base_form(cids))
    assert response.status_code == 503
    assert "Not sent" in response.text
    assert "could not be written, so the command was not sent" in response.text
    assert "state directory" in response.text
    assert gateway.control_calls == []


def test_submit_failed_outcome_write_keeps_card(capsys, monkeypatch):
    client, _, gateway, cids = live_client()
    gateway.set_control(
        cids["work"],
        (
            f"repository: {REPO}\nrequest_id: {RID}\nsequence: 2\n"
            "acknowledgement: accepted\neffect: stopping now\n"
        ).encode(),
        b"",
        0,
    )
    calls = []
    original_append = control_routes.append_entry

    def flaky_append(entry):
        calls.append(entry["outcome"])
        if entry["outcome"] != "submitted":
            raise OSError("disk full")
        return original_append(entry)

    monkeypatch.setattr(control_routes, "append_entry", flaky_append)
    response = submit(client, "work", base_form(cids))
    assert response.status_code == 200
    # The card is unchanged apart from the added note.
    assert "Accepted" in response.text
    assert "stopping now" in response.text
    assert "The Control audit outcome entry could not be written." in response.text
    assert "disk full" in capsys.readouterr().err
    assert calls == ["submitted", "accepted"]


def test_submit_failed_refusal_write_still_returns_refusal(monkeypatch, capsys):
    def boom(entry):
        raise OSError("disk full")

    monkeypatch.setattr(control_routes, "append_entry", boom)
    client, _, gateway, cids = live_client()
    response = submit(client, "work", base_form(cids, action="explode"))
    assert response.status_code == 422
    assert "Refused" in response.text
    assert gateway.control_calls == []
    assert "disk full" in capsys.readouterr().err


# --- busy, retry, expect-repo, output safety -------------------------------------------


def test_submit_busy_in_flight_refuses_before_submitted():
    client, _, gateway, cids = live_client()
    gateway.hold_control(cids["work"])
    try:
        response = submit(client, "work", base_form(cids))
    finally:
        gateway.release_control(cids["work"])
    assert response.status_code == 409
    assert "Refused" in response.text
    assert "Another command is being sent to this instance." in response.text
    entries = audit_entries()
    assert [entry["outcome"] for entry in entries] == ["refused"]
    assert entries[0]["reason"] == "busy"
    assert entries[0]["request_id"] == RID
    assert entries[0]["container_id"] == cids["work"]
    assert entries[0]["expected_repository"] == REPO
    assert entries[0]["action"] == "stop"
    assert entries[0]["exit_code"] is None
    assert gateway.control_calls == []
    assert gateway.command_status_calls == []


def test_submit_busy_race_after_submitted_is_409_with_refused_busy_outcome():
    client, _, gateway, cids = live_client()
    gateway.set_control_busy(cids["work"])
    response = submit(client, "work", base_form(cids))
    assert response.status_code == 409
    assert "Refused" in response.text
    assert "Another command is being sent to this instance." in response.text
    entries = audit_entries()
    assert [entry["outcome"] for entry in entries] == ["submitted", "refused"]
    assert entries[1]["reason"] == "busy"
    assert entries[1]["request_id"] == RID
    assert entries[1]["exit_code"] is None


def test_submit_retry_sends_same_request_id():
    client, _, gateway, cids = live_client()
    gateway.set_control(cids["work"], b"", agentctl_stderr("unreachable.stderr"), 2)
    first = submit(client, "work", base_form(cids, retry="1"))
    assert first.status_code == 200
    assert "Outcome unknown" in first.text
    gateway.set_control(
        cids["work"],
        (
            f"repository: {REPO}\nrequest_id: {RID}\nsequence: 9\n"
            "acknowledgement: accepted\neffect: resumed\n"
        ).encode(),
        b"",
        0,
    )
    retry_form = base_form(cids, retry="1")
    second = submit(client, "work", retry_form)
    assert second.status_code == 200
    assert "Accepted" in second.text
    assert "Outcome unknown" not in second.text
    assert [call["request_id"] for call in gateway.control_calls] == [RID, RID]
    submitted = [entry for entry in audit_entries() if entry["outcome"] == "submitted"]
    assert len(submitted) == 2
    assert all(entry["retry"] is True for entry in submitted)
    assert all(entry["request_id"] == RID for entry in submitted)


def test_submit_retry_goes_through_every_check_again():
    client, _, gateway, cids = live_client()
    gateway.set_control(cids["work"], b"", agentctl_stderr("unreachable.stderr"), 2)
    first = submit(client, "work", base_form(cids, retry="1"))
    assert "Outcome unknown" in first.text
    retry_form = base_form(cids, repository="other-owner/other-repo", retry="1")
    second = submit(client, "work", retry_form)
    assert second.status_code == 409
    assert "Instance replaced or unavailable" in second.text
    assert len(gateway.control_calls) == 1


def test_submit_expect_repo_never_passed_until_version_allows():
    client, _, gateway, cids = live_client()
    gateway.set_control(
        cids["work"],
        (
            f"repository: {REPO}\nrequest_id: {RID}\nsequence: 1\n"
            "acknowledgement: accepted\neffect: ok\n"
        ).encode(),
        b"",
        0,
    )
    submit(client, "work", base_form(cids))
    assert gateway.control_calls[0]["expect_repo"] is None
    assert audit_entries()[0]["expect_repo_guard"] is False


def test_submit_expect_repo_passed_when_version_allows(monkeypatch):
    monkeypatch.setattr(control_routes, "supports_expect_repo", lambda version: True)
    client, _, gateway, cids = live_client()
    gateway.set_control(
        cids["work"],
        (
            f"repository: {REPO}\nrequest_id: {RID}\nsequence: 1\n"
            "acknowledgement: accepted\neffect: ok\n"
        ).encode(),
        b"",
        0,
    )
    response = submit(client, "work", base_form(cids))
    assert response.status_code == 200
    assert gateway.control_calls[0]["expect_repo"] == REPO
    assert f"--expect-repo={REPO}" in gateway.control_argvs[0]
    assert audit_entries()[0]["expect_repo_guard"] is True


def test_submit_agent_text_is_escaped():
    client, _, gateway, cids = live_client()
    gateway.set_control(
        cids["work"],
        (
            f"repository: {REPO}\nrequest_id: {RID}\nsequence: 5\n"
            "acknowledgement: accepted\neffect: <script>alert(1)</script>\n"
        ).encode(),
        b"",
        0,
    )
    response = submit(client, "work", base_form(cids))
    assert response.status_code == 200
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text
    assert "<script>alert(1)</script>" not in response.text


def test_submit_rejection_reason_is_escaped():
    client, _, gateway, cids = live_client()
    gateway.set_control(cids["work"], b"", b"rejected: <b>nope</b>", 1)
    response = submit(client, "work", base_form(cids))
    assert response.status_code == 200
    assert "&lt;b&gt;nope&lt;/b&gt;" in response.text
    assert "<b>nope</b>" not in response.text


def test_submit_card_template_never_bypasses_autoescape():
    template_dir = Path(control_routes.__file__).resolve().parent / "templates"
    text = (template_dir / "_control_card.html").read_text()
    assert "|safe" not in text


# --- wiring: feedback slot, confirm target, card chrome ---------------------------------


def test_drawer_has_feedback_slot_and_body_id():
    client, _, _, _ = live_client()
    body = client.get("/instances/work").text
    assert 'id="ctl-feedback-work"' in body
    assert 'id="ctl-body-work"' in body


def test_confirm_posts_card_into_feedback_slot():
    client, _, _, _ = live_client()
    response = client.post(
        "/instances/work/control/review", data={"action": "stop"}, headers=dict(GOOD_HEADERS)
    )
    assert response.status_code == 200
    assert 'hx-target="#ctl-feedback-work"' in response.text
    assert 'hx-swap="afterbegin"' in response.text


def test_submit_card_is_dismissable_with_meta():
    client, _, gateway, cids = live_client()
    gateway.set_control(
        cids["work"],
        (
            f"repository: {REPO}\nrequest_id: {RID}\nsequence: 6\n"
            "acknowledgement: accepted\neffect: ok\n"
        ).encode(),
        b"",
        0,
    )
    response = submit(client, "work", base_form(cids))
    assert response.status_code == 200
    assert "ctl-card-dismiss" in response.text
    assert "aria-label=\"Dismiss\"" in response.text
    assert re.search(rf"Request ID <code>{RID}</code> · seq 6", response.text) is not None


def test_submit_audit_entry_schema():
    client, _, gateway, cids = live_client()
    gateway.set_control(
        cids["work"],
        (
            f"repository: {REPO}\nrequest_id: {RID}\nsequence: 8\n"
            "acknowledgement: accepted\neffect: ok\n"
        ).encode(),
        b"",
        0,
    )
    assert submit(client, "work", base_form(cids)).status_code == 200
    wanted = {
        "at",
        "outcome",
        "request_id",
        "instance",
        "container_id",
        "expected_repository",
        "action",
        "params",
        "command",
        "agent_version",
        "expect_repo_guard",
        "retry",
        "exit_code",
        "sequence",
        "acknowledgement",
        "reply_repository",
        "repository_mismatch",
        "reason",
        "error",
        "duration_ms",
    }
    for entry in audit_entries():
        assert set(entry) == wanted, entry["outcome"]
