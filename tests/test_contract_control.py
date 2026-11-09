"""Control actions on the contract path (spec: "Control actions (contract path)").

A controller can ``stop`` and ``resume`` a live contract instance from its
Control console with the existing two-step confirmation. Every request
carries a consumer-generated request ID and ``--expect-repo``; every reply
ends in a feedback card; ambiguous outcomes are retried once with the same
request ID; accepted commands are followed until their final
Acknowledgement. Tests use the fake gateway's scripted contract replies and
assert HTML fragments through TestClient.
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

from agent_dashboard.app import create_app
from agent_dashboard.collector import run_cycle
from agent_dashboard.config import Settings
from agent_dashboard.contract_reply import validate_contract_request_id
from agent_dashboard.control_audit import audit_path
from agent_dashboard.fleet import FleetState
from agent_dashboard.gateway import (
    CONTRACT_PATH,
    ControlExec,
    ControlTimeoutError,
    ManagedInstance,
    argv_contract_exec,
    argv_contract_mutation,
)


@pytest.fixture(autouse)
def _isolated_audit(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))


GOOD_HEADERS = {"Origin": "http://localhost", "HX-Request": "true"}

CID = "abc123"
REPO = "example-owner/example-repo"
BACKEND = "coding-agent"


def _result(name: str) -> dict:
    """The ``result`` of a pinned package ``status`` fixture."""
    from agent_operator_contract import documents

    return json.loads(documents.load_fixture(name)["stdout"])["result"]


def _ok_envelope(
    request_id: str,
    *,
    acknowledgement: str = "accepted",
    sequence: int = 3,
    detail: str = "stopping",
    replayed: bool = False,
) -> bytes:
    return json.dumps(
        {
            "contract": "1.0",
            "ok": True,
            "result": {
                "replayed": replayed,
                "command": {
                    "request_id": request_id,
                    "sequence": sequence,
                    "acknowledgement": acknowledgement,
                    "detail": detail,
                    "repository": REPO,
                },
            },
        }
    ).encode()


def _err_envelope(
    code: str, message: str, *, details: dict | None = None, retryable: bool = False
) -> bytes:
    error: dict = {"code": code, "message": message, "retryable": retryable}
    if details is not None:
        error["details"] = details
    return json.dumps({"contract": "1.0", "ok": False, "error": error}).encode()


#: Exit codes per the contract error table.
ERROR_EXITS = {
    "rejected": 1,
    "repository_mismatch": 1,
    "payload_mismatch": 1,
    "invalid_argument": 1,
    "usage": 2,
    "unsupported": 3,
    "contract_mismatch": 4,
    "not_found": 5,
    "agent_unreachable": 6,
    "internal": 7,
}


def _snapshot(commands: list[dict] | None = None) -> dict:
    result = copy.deepcopy(_result("status-draining"))
    if commands is not None:
        result["recent_commands"] = commands
    return result


def _record(
    request_id: str,
    sequence: int,
    acknowledgement: str = "accepted",
    kind: str = "stop",
    detail: str = "stopping",
) -> dict:
    return {
        "request_id": request_id,
        "sequence": sequence,
        "kind": kind,
        "arguments": {},
        "repository": REPO,
        "acknowledgement": acknowledgement,
        "detail": detail,
        "created_at": "2026-11-06T09:12:03Z",
        "updated_at": "2026-11-06T09:12:03Z",
    }


def make_contract_client(
    snapshot: dict | None = None, settings: Settings | None = None
):
    """A client with one live contract instance and its scripted snapshot."""
    now = time.time()
    state = FleetState()
    gateway = FakeGateway(
        [
            ManagedInstance(
                "web-0",
                CID,
                "running",
                path=CONTRACT_PATH,
                instance="web-0",
                exec_user="agent",
                backend=BACKEND,
            )
        ]
    )
    gateway.set_contract_snapshot(CID, _snapshot() if snapshot is None else snapshot)
    asyncio.run(run_cycle(state, gateway, max_concurrent_execs=4, now=now))
    app = create_app(
        state=state,
        settings=settings if settings is not None else Settings(poll_interval=5.0),
        gateway=gateway,
        start_poller=False,
    )
    return TestClient(app, base_url="http://localhost"), state, gateway


def review(client: TestClient, data: dict) -> object:
    return client.post(
        "/instances/web-0/control/review", data=data, headers=dict(GOOD_HEADERS)
    )


def submit(client: TestClient, data: dict) -> object:
    return client.post(
        "/instances/web-0/control/submit", data=data, headers=dict(GOOD_HEADERS)
    )


def status(client: TestClient, data: dict) -> object:
    return client.post(
        "/instances/web-0/control/status", data=data, headers=dict(GOOD_HEADERS)
    )


def audit_entries() -> list[dict]:
    path: Path = audit_path()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


RID = "dash-0123456789abcdef"


def submit_form(**overrides) -> dict:
    form = {
        "action": "stop",
        "container_id": CID,
        "repository": REPO,
        "request_id": RID,
    }
    form.update(overrides)
    return form


# --- the confirmation ----------------------------------------------------------


def test_review_stop_confirmation_pins_exact_argv():
    client, _, _ = make_contract_client()
    response = review(client, {"action": "stop"})
    assert response.status_code == 200
    match = re.search(r"Request ID <code>(dash-[0-9a-f]{16})</code>", response.text)
    assert match is not None
    request_id = match.group(1)
    # The request ID conforms to the contract pattern.
    assert validate_contract_request_id(request_id) == request_id
    # The confirmation shows the exact argv: --contract 1 with
    # space-separated --request-id and --expect-repo.
    expected = " ".join(
        argv_contract_exec(
            CID, argv_contract_mutation("stop", request_id, REPO)
        )
    )
    assert expected in response.text
    assert "--contract 1 stop --request-id" in response.text
    assert "--expect-repo" in response.text
    # The head names the command and the instance; the Target Repository
    # and the request ID travel as hidden fields.
    assert "<code>agentctl stop</code> on <strong>web-0</strong>" in response.text
    assert REPO in response.text
    assert 'name="action" value="stop"' in response.text
    assert f'name="container_id" value="{CID}"' in response.text
    assert f'name="repository" value="{REPO}"' in response.text
    assert f'name="request_id" value="{request_id}"' in response.text
    assert "ctl-tone-danger" in response.text
    # A 200 writes no audit entry and runs no exec.
    assert audit_entries() == []


def test_review_resume_confirmation_pins_exact_argv():
    client, _, _ = make_contract_client()
    response = review(client, {"action": "resume"})
    assert response.status_code == 200
    match = re.search(r"Request ID <code>(dash-[0-9a-f]{16})</code>", response.text)
    assert match is not None
    request_id = match.group(1)
    expected = " ".join(
        argv_contract_exec(
            CID, argv_contract_mutation("resume", request_id, REPO)
        )
    )
    assert expected in response.text
    assert "--contract 1 resume --request-id" in response.text
    assert "ctl-tone-primary" in response.text


def test_review_rejects_non_core_action():
    client, _, _ = make_contract_client()
    response = review(client, {"action": "handoff"})
    assert response.status_code == 422
    assert "stop, resume" in response.text
    entries = audit_entries()
    assert len(entries) == 1
    assert entries[0]["outcome"] == "refused"
    assert entries[0]["protocol"] == "contract"


# --- cards by reply -------------------------------------------------------------


def test_submit_stop_accepted_card_follows_with_track():
    client, _, gateway = make_contract_client()
    gateway.set_contract_mutation(CID, _ok_envelope(RID), returncode=0)
    response = submit(client, submit_form())
    assert response.status_code == 200
    assert "Accepted" in response.text
    assert "/instances/web-0/control/track" in response.text
    # The exec ran once with the pinned argv.
    assert gateway.contract_calls == [
        {
            "container_id": CID,
            "action": "stop",
            "request_id": RID,
            "expect_repo": REPO,
            "exec_user": "agent",
        }
    ]
    assert gateway.contract_argvs == [
        argv_contract_exec(CID, argv_contract_mutation("stop", RID, REPO))
    ]
    # Submitted plus outcome entries carry the contract audit fields.
    entries = audit_entries()
    assert [entry["outcome"] for entry in entries] == ["submitted", "accepted"]
    for entry in entries:
        assert entry["protocol"] == "contract"
        assert entry["backend"] == BACKEND
        assert entry["attempts"] == 1
    assert entries[0]["error_code"] is None
    assert entries[1]["error_code"] == "accepted"
    assert entries[1]["replayed"] is None


def test_submit_stop_completed_card_is_terminal():
    client, _, gateway = make_contract_client()
    gateway.set_contract_mutation(
        CID, _ok_envelope(RID, acknowledgement="completed", detail="stopped"), returncode=0
    )
    response = submit(client, submit_form())
    assert response.status_code == 200
    assert "Completed" in response.text
    assert "stopped" in response.text
    # A terminal card stops following: no track trigger.
    assert "/instances/web-0/control/track" not in response.text
    entries = audit_entries()
    assert entries[1]["outcome"] == "accepted"
    assert entries[1]["error_code"] == "completed"


def test_submit_replayed_card_says_already_recorded():
    client, _, gateway = make_contract_client()
    gateway.set_contract_mutation(
        CID,
        _ok_envelope(RID, acknowledgement="completed", replayed=True),
        returncode=0,
    )
    response = submit(client, submit_form())
    assert response.status_code == 200
    assert "already recorded; nothing new happened" in response.text
    assert "/instances/web-0/control/track" in response.text
    entries = audit_entries()
    assert entries[1]["outcome"] == "accepted"
    assert entries[1]["replayed"] is True


@pytest.mark.parametrize(
    "code",
    ["rejected", "repository_mismatch", "payload_mismatch", "invalid_argument"],
)
def test_submit_rejection_family_card(code):
    client, _, gateway = make_contract_client()
    gateway.set_contract_mutation(
        CID, _err_envelope(code, "nope"), returncode=ERROR_EXITS[code]
    )
    response = submit(client, submit_form())
    assert response.status_code == 200
    assert "Rejected" in response.text
    assert "Nothing was stored" in response.text
    # Nothing stored: no track, no retry.
    assert "/instances/web-0/control/track" not in response.text
    assert "Retry with the same request ID" not in response.text
    entries = audit_entries()
    assert entries[1]["outcome"] == "rejected"
    assert entries[1]["error_code"] == code
    assert gateway.contract_calls and gateway.contract_calls[0]["request_id"] == RID


def test_submit_unsupported_card():
    client, _, gateway = make_contract_client()
    gateway.set_contract_mutation(
        CID, _err_envelope("unsupported", "no stop here"), returncode=3
    )
    response = submit(client, submit_form())
    assert response.status_code == 200
    assert "Not supported" in response.text
    assert "Nothing was stored" in response.text
    entries = audit_entries()
    assert entries[1]["error_code"] == "unsupported"


def test_submit_usage_card_is_not_sent():
    client, _, gateway = make_contract_client()
    gateway.set_contract_mutation(
        CID, _err_envelope("usage", "bad flags"), returncode=2
    )
    response = submit(client, submit_form())
    assert response.status_code == 200
    assert "Not sent" in response.text
    assert "nothing was sent" in response.text
    entries = audit_entries()
    assert entries[1]["error_code"] == "usage"


def test_submit_contract_mismatch_card_is_not_sent():
    client, _, gateway = make_contract_client()
    gateway.set_contract_mutation(
        CID,
        _err_envelope(
            "contract_mismatch", "speaks 2", details={"supported": [2]}
        ),
        returncode=4,
    )
    response = submit(client, submit_form())
    assert response.status_code == 200
    assert "Not sent" in response.text
    entries = audit_entries()
    assert entries[1]["error_code"] == "contract_mismatch"


def test_submit_internal_not_stored_card_offers_retry():
    client, _, gateway = make_contract_client()
    gateway.set_contract_mutation(
        CID,
        _err_envelope("internal", "boom", details={"stored": False}, retryable=True),
        returncode=7,
    )
    response = submit(client, submit_form())
    assert response.status_code == 200
    assert "Not stored" in response.text
    assert "Retry with the same request ID" in response.text
    assert f'name="request_id" value="{RID}"' in response.text
    entries = audit_entries()
    assert entries[1]["error_code"] == "internal"


# --- ambiguous retries -----------------------------------------------------------


def test_submit_ambiguous_is_retried_once_with_same_request_id():
    client, _, gateway = make_contract_client()
    gateway.set_contract_mutation(
        CID, _err_envelope("agent_unreachable", "gone"), returncode=6
    )
    real = gateway.contract_mutation

    async def _swap(container_id, action, *, request_id, expect_repo, exec_user="agent"):
        out = await real(
            container_id,
            action,
            request_id=request_id,
            expect_repo=expect_repo,
            exec_user=exec_user,
        )
        gateway.set_contract_mutation(CID, _ok_envelope(RID), returncode=0)
        return out

    gateway.contract_mutation = _swap  # type: ignore[method-assign]
    response = submit(client, submit_form())
    assert response.status_code == 200
    assert "Accepted" in response.text
    # Both argv carry the same request ID.
    assert len(gateway.contract_argvs) == 2
    assert all(RID in argv for argv in gateway.contract_argvs)
    entries = audit_entries()
    assert entries[1]["attempts"] == 2


def test_submit_timeout_is_retried_once_with_same_request_id():
    client, _, gateway = make_contract_client()
    gateway.set_contract_mutation(CID, _ok_envelope(RID), returncode=0)
    real = gateway.contract_mutation
    seen: list[list[str]] = []

    async def _flaky(container_id, action, *, request_id, expect_repo, exec_user="agent"):
        seen.append(
            argv_contract_exec(
                container_id,
                argv_contract_mutation(action, request_id, expect_repo),
                exec_user=exec_user,
            )
        )
        if len(seen) == 1:
            raise ControlTimeoutError("docker exec timed out after 15 s")
        return await real(
            container_id,
            action,
            request_id=request_id,
            expect_repo=expect_repo,
            exec_user=exec_user,
        )

    gateway.contract_mutation = _flaky  # type: ignore[method-assign]
    response = submit(client, submit_form())
    assert response.status_code == 200
    assert "Accepted" in response.text
    assert len(seen) == 2
    assert all(RID in argv for argv in seen)
    entries = audit_entries()
    assert entries[1]["attempts"] == 2


def test_submit_still_ambiguous_renders_outcome_unknown():
    client, _, gateway = make_contract_client()
    gateway.set_contract_mutation(CID, b"not json", returncode=0)
    response = submit(client, submit_form())
    assert response.status_code == 200
    assert "Outcome unknown" in response.text
    assert "Retry with the same request ID" in response.text
    assert "Check status" in response.text
    # Retried once with the same request ID.
    assert len(gateway.contract_argvs) == 2
    assert all(RID in argv for argv in gateway.contract_argvs)
    entries = audit_entries()
    assert entries[1]["outcome"] == "unknown"
    assert entries[1]["attempts"] == 2


# --- check status ------------------------------------------------------------------


def test_status_success_rerenders_terminal_card():
    client, _, gateway = make_contract_client()
    gateway.set_contract_command(
        CID,
        _ok_envelope(RID, acknowledgement="completed", detail="stopped"),
        returncode=0,
    )
    response = status(client, submit_form())
    assert response.status_code == 200
    assert "Completed" in response.text
    assert "stopped" in response.text
    # A read-only check writes no audit entry.
    assert audit_entries() == []
    assert gateway.contract_command_calls == [
        {"container_id": CID, "request_id": RID, "exec_user": "agent"}
    ]
    assert gateway.contract_argvs == [
        argv_contract_exec(CID, ["command", RID])
    ]


def test_status_success_accepted_keeps_following():
    snapshot = _snapshot([_record(RID, 3, "accepted")])
    client, _, gateway = make_contract_client(snapshot)
    gateway.set_contract_command(
        CID, _ok_envelope(RID, acknowledgement="accepted"), returncode=0
    )
    response = status(client, submit_form())
    assert response.status_code == 200
    assert "Accepted" in response.text
    assert "/instances/web-0/control/track" in response.text


def test_status_not_found_says_safe_to_retry_same_id():
    client, _, gateway = make_contract_client()
    gateway.set_contract_command(
        CID, _err_envelope("not_found", "no such command"), returncode=5
    )
    response = status(client, submit_form())
    assert response.status_code == 200
    assert "Not recorded" in response.text
    assert "safe to retry with the same request ID" in response.text
    assert "Retry with the same request ID" in response.text


def test_status_ambiguous_says_could_not_check():
    client, _, gateway = make_contract_client()
    gateway.set_contract_command(CID, b"not json", returncode=0)
    response = status(client, submit_form())
    assert response.status_code == 200
    assert "Could not check" in response.text
    assert "Retry with the same request ID" in response.text
    assert "Check status" in response.text


# --- follow and timeline ----------------------------------------------------------------


def test_track_follows_accepted_until_terminal():
    snapshot = _snapshot([_record(RID, 3, "accepted")])
    client, _, _ = make_contract_client(snapshot)
    query = (
        f"action=stop&container_id={CID}&repository={REPO}"
        f"&request_id={RID}&sequence=3"
    )
    response = client.get(f"/instances/web-0/control/track?{query}")
    assert response.status_code == 200
    assert "Accepted" in response.text
    assert "following" in response.text
    # The snapshot moves to completed: the card stops with the terminal tone.
    gateway_snapshot = _snapshot([_record(RID, 3, "completed", detail="stopped")])
    client2, _, _ = make_contract_client(gateway_snapshot)
    response = client2.get(f"/instances/web-0/control/track?{query}")
    assert response.status_code == 200
    assert "Completed" in response.text
    assert "following" not in response.text


def test_track_evicted_record_offers_check_status():
    snapshot = _snapshot(
        [_record("dash-aaaaaaaaaaaaaaaa", 7), _record("dash-bbbbbbbbbbbbbbbb", 8)]
    )
    client, _, _ = make_contract_client(snapshot)
    query = (
        f"action=stop&container_id={CID}&repository={REPO}"
        f"&request_id={RID}&sequence=1"
    )
    response = client.get(f"/instances/web-0/control/track?{query}")
    assert response.status_code == 200
    assert "No longer in the last 10 commands" in response.text
    assert "Check status" in response.text


def test_command_timeline_shows_newest_ten_with_origin_chips():
    commands = [
        _record(f"dash-{index:016x}", index, detail=f"command {index}")
        for index in range(12)
    ]
    commands.append(_record("req-3f1c9a2b7d10", 12, detail="cli command"))
    client, _, _ = make_contract_client(_snapshot(commands))
    response = client.get("/instances/web-0/control/commands")
    assert response.status_code == 200
    # Newest 10 by sequence: #12 down to #3; #0, #1, #2 drop out.
    assert "command 11" in response.text
    assert "command 3" in response.text
    assert "command 2" not in response.text
    assert "cli command" in response.text
    # Origin chips mark dashboard IDs and CLI IDs apart.
    assert "dashboard" in response.text
    assert "CLI" in response.text
    assert response.text.count("ctl-entry-head") == 10


def test_contract_console_slot_renders_in_drawer():
    client, _, _ = make_contract_client()
    body = client.get("/instances/web-0").text
    assert "06 // Control console" in body
    assert "01 // Contract status" in body
    assert "ctl-body-" in body
    assert "Control console, attempt statistics" not in body


# --- audit and polling -------------------------------------------------------------------


def test_refused_submit_carries_protocol_and_backend():
    client, _, _ = make_contract_client()
    response = submit(client, submit_form(action="handoff"))
    assert response.status_code == 422
    entries = audit_entries()
    assert len(entries) == 1
    assert entries[0]["protocol"] == "contract"
    assert entries[0]["backend"] == BACKEND


def test_contract_rows_keep_polling_while_control_in_flight():
    now = time.time()
    state = FleetState()
    gateway = FakeGateway(
        [
            ManagedInstance("legacy-0", "bbb111", "running"),
            ManagedInstance(
                "web-0",
                CID,
                "running",
                path=CONTRACT_PATH,
                instance="web-0",
                exec_user="agent",
                backend=BACKEND,
            ),
        ]
    )
    from test_web import fresh_snapshot, load_snapshot

    gateway.set_snapshot(
        "bbb111",
        fresh_snapshot(
            copy.deepcopy(load_snapshot("snapshot-attempt-in-progress.json")), now
        ),
    )
    gateway.set_contract_snapshot(CID, _snapshot())
    gateway.hold_control("bbb111")
    asyncio.run(run_cycle(state, gateway, max_concurrent_execs=4, now=now))
    # The legacy row's telemetry is skipped while its control exec is held;
    # the contract row's status poll still runs.
    assert "bbb111" not in gateway.telemetry_calls
    assert (CID, "agent") in gateway.contract_calls
