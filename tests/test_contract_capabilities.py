"""Capability-gated Control actions on the contract path.

A controller sees ``stop --after N``, ``next issue N`` and ``handoff now``
on a contract instance only when the latest good Status Snapshot advertises
the matching Operator Capability in ``identity.capabilities``. Tests replay
the pinned package fixtures (coding-agent's ``status-draining`` set, the
full ``status-simple-coding-agent`` set, and an empty set) through the fake
gateway and assert HTML fragments through TestClient.
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
from agent_dashboard.control import contract_availability
from agent_dashboard.control_audit import audit_path
from agent_dashboard.fleet import FleetState
from agent_dashboard.gateway import (
    CONTRACT_PATH,
    ManagedInstance,
    argv_contract_exec,
    argv_contract_mutation,
    validate_contract_n,
    validate_control_n,
)


@pytest.fixture(autouse=True)
def _isolated_audit(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))


GOOD_HEADERS = {"Origin": "http://localhost", "HX-Request": "true"}

CID = "abc123"
REPO = "example-owner/example-repo"


def _result(name: str) -> dict:
    """The ``result`` of a pinned package ``status`` fixture."""
    from agent_operator_contract import documents

    return json.loads(documents.load_fixture(name)["stdout"])["result"]


def _snapshot(name: str, *, capabilities: list[str] | None = None) -> dict:
    result = copy.deepcopy(_result(name))
    if capabilities is not None:
        result["identity"]["capabilities"] = list(capabilities)
    return result


def make_client(snapshot: dict, settings: Settings | None = None):
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
                backend="coding-agent",
            )
        ]
    )
    gateway.set_contract_snapshot(CID, snapshot)
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


def _ok_envelope(
    request_id: str,
    *,
    acknowledgement: str = "accepted",
    sequence: int = 3,
    detail: str = "stopping",
) -> bytes:
    return json.dumps(
        {
            "contract": "1.0",
            "ok": True,
            "result": {
                "replayed": False,
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


def _err_envelope(code: str, message: str, **details) -> bytes:
    error: dict = {"code": code, "message": message, "retryable": False}
    if details:
        error["details"] = details
    return json.dumps({"contract": "1.0", "ok": False, "error": error}).encode()


RID = "dash-0123456789abcdef"


# --- availability comes from identity.capabilities ----------------------------


def test_coding_agent_offers_stop_after_and_next_issue_but_not_handoff():
    """``status-draining`` advertises ``stop-after``/``next-issue`` only."""
    assert _result("status-draining")["identity"]["capabilities"] == [
        "next-issue",
        "stop-after",
    ]
    client, _, _ = make_client(_snapshot("status-draining"))
    body = client.get("/instances/web-0/control").text
    assert 'value="stop_after"' in body
    assert 'value="next_issue"' in body
    assert 'value="stop"' in body
    assert 'value="resume"' in body
    assert 'value="handoff"' not in body
    assert "Handoff now" not in body
    # Recovery and errors-reset operations are never offered.
    for missing in ("recovery_retry", "recovery_release", "errors_reset"):
        assert missing not in body


def test_full_simple_coding_agent_set_offers_handoff_now():
    """``status-simple-coding-agent`` advertises all six capabilities."""
    assert set(_result("status-simple-coding-agent")["identity"]["capabilities"]) == {
        "errors-reset",
        "next-issue",
        "operator-handoff",
        "recovery-release",
        "recovery-retry",
        "stop-after",
    }
    client, _, _ = make_client(_snapshot("status-simple-coding-agent"))
    body = client.get("/instances/web-0/control").text
    for key in ("stop", "stop_after", "resume", "handoff", "next_issue"):
        assert f'value="{key}"' in body
    assert "Handoff now" in body
    # Even with the full set, recovery and errors-reset stay hidden.
    for missing in ("recovery_retry", "recovery_release", "errors_reset"):
        assert missing not in body


def test_empty_capability_set_offers_only_core_actions():
    client, _, _ = make_client(_snapshot("status-draining", capabilities=[]))
    body = client.get("/instances/web-0/control").text
    assert 'value="stop"' in body
    assert 'value="resume"' in body
    for missing in ("stop_after", "next_issue", "handoff"):
        assert f'value="{missing}"' not in body


def test_contract_availability_unit_maps_capabilities_to_actions():
    assert [a.key for a in contract_availability(reach="live").actions] == [
        "stop",
        "resume",
    ]
    assert [a.key for a in contract_availability(reach="live", capabilities=[]).actions] == [
        "stop",
        "resume",
    ]
    offered = contract_availability(
        reach="live", capabilities=["stop-after", "next-issue"]
    )
    assert [a.key for a in offered.actions] == [
        "stop",
        "stop_after",
        "resume",
        "next_issue",
    ]
    full = contract_availability(
        reach="live",
        capabilities=[
            "errors-reset",
            "next-issue",
            "operator-handoff",
            "recovery-release",
            "recovery-retry",
            "stop-after",
        ],
    )
    assert [a.key for a in full.actions] == [
        "stop",
        "stop_after",
        "resume",
        "handoff",
        "next_issue",
    ]
    # Unrelated or non-list capabilities never add actions; the backend
    # name or image is never consulted.
    assert [a.key for a in contract_availability(
        reach="live", capabilities="stop-after"
    ).actions] == ["stop", "resume"]
    assert [a.key for a in contract_availability(
        reach="live", capabilities=["stop-after", "bogus-cap"]
    ).actions] == ["stop", "stop_after", "resume"]


# --- stop --after range: 1-999 on the contract path ----------------------------


def test_stop_after_accepts_contract_range_and_legacy_keeps_99():
    assert validate_contract_n("1") == 1
    assert validate_contract_n("999") == 999
    assert validate_contract_n(999) == 999
    for bad in ("0", "00", "01", "1000", "", " 12", "-1", "abc"):
        with pytest.raises(ValueError):
            validate_contract_n(bad)
    # The legacy path keeps 1-99.
    assert validate_control_n("99") == 99
    with pytest.raises(ValueError):
        validate_control_n("100")
    with pytest.raises(ValueError):
        validate_control_n("999")


def test_review_stop_after_boundary_values():
    client, _, gateway = make_client(_snapshot("status-draining"))
    response = review(client, {"action": "stop_after", "n": "999"})
    assert response.status_code == 200
    assert "stop --after 999" in response.text
    for bad in ("0", "1000"):
        response = review(client, {"action": "stop_after", "n": bad})
        assert response.status_code == 422
        assert "1–999" in response.text
    # Validation refusals run before any argv exists: nothing was sent.
    assert gateway.contract_mutation_calls == []
    assert gateway.contract_argvs == []


# --- argv pins ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "kwargs", "expected_tail"),
    [
        ("stop", {}, ["stop"]),
        ("resume", {}, ["resume"]),
        ("stop_after", {"n": 2}, ["stop", "--after", "2"]),
        ("next_issue", {"issue": 122}, ["next", "issue", "122"]),
        ("handoff", {}, ["handoff", "now"]),
    ],
)
def test_argv_pins_request_id_and_expect_repo(action, kwargs, expected_tail):
    request_id = "dash-9f8e7d6c5b4a3210"
    argv = argv_contract_mutation(action, request_id, REPO, **kwargs)
    assert argv == [
        *expected_tail,
        "--request-id",
        request_id,
        "--expect-repo",
        REPO,
    ]


def test_argv_matches_replayed_fixture_shapes():
    """The pinned shapes equal the package fixtures' agentctl argv tails."""
    from agent_operator_contract import documents

    cases = [
        ("stop-after-accepted", {"n": 2}),
        ("next-issue-accepted", {"issue": 122}),
        ("handoff-completed", {}),
    ]
    for name, kwargs in cases:
        fixture = documents.load_fixture(name)
        doc = json.loads(fixture["stdout"])
        argv = fixture["argv"]
        assert argv[:2] == ["--contract", "1"]
        action = (
            "stop_after"
            if name == "stop-after-accepted"
            else "next_issue"
            if name == "next-issue-accepted"
            else "handoff"
        )
        request_id = doc["result"]["command"]["request_id"]
        pinned = argv_contract_mutation(action, request_id, REPO, **kwargs)
        assert argv[2:] == pinned


def test_validation_refusals_raise_before_argv_is_built():
    with pytest.raises(ValueError):
        argv_contract_mutation("stop_after", RID, REPO, n=0)
    with pytest.raises(ValueError):
        argv_contract_mutation("stop_after", RID, REPO, n=1000)
    with pytest.raises(ValueError):
        argv_contract_mutation("next_issue", RID, REPO, issue=0)
    with pytest.raises(ValueError):
        argv_contract_mutation("stop", RID, REPO, n=2)
    with pytest.raises(ValueError):
        argv_contract_mutation("handoff", RID, REPO, issue=1)


def test_review_confirmation_pins_gated_argv():
    from agent_operator_contract import documents

    client, _, _ = make_client(_snapshot("status-draining"))
    for action, field, value, shape in [
        ("stop_after", "n", "2", "stop --after 2 --request-id"),
        ("next_issue", "issue", "122", "next issue 122 --request-id"),
    ]:
        response = review(client, {"action": action, field: value})
        assert response.status_code == 200
        match = re.search(r"Request ID <code>(dash-[0-9a-f]{16})</code>", response.text)
        assert match is not None
        request_id = match.group(1)
        assert validate_contract_request_id(request_id) == request_id
        expected = " ".join(
            argv_contract_exec(
                CID,
                argv_contract_mutation(
                    action,
                    request_id,
                    REPO,
                    **({field: int(value)}),
                ),
            )
        )
        assert expected in response.text
        assert shape in response.text
    client, _, _ = make_client(_snapshot("status-simple-coding-agent"))
    response = review(client, {"action": "handoff"})
    assert response.status_code == 200
    match = re.search(r"Request ID <code>(dash-[0-9a-f]{16})</code>", response.text)
    assert match is not None
    expected = " ".join(
        argv_contract_exec(
            CID, argv_contract_mutation("handoff", match.group(1), REPO)
        )
    )
    assert expected in response.text
    assert "handoff now --request-id" in response.text
    assert documents.load_fixture("handoff-completed")["argv"][2:] == [
        "handoff",
        "now",
        "--request-id",
        json.loads(documents.load_fixture("handoff-completed")["stdout"])["result"][
            "command"
        ]["request_id"],
        "--expect-repo",
        REPO,
    ]


def test_submit_sends_gated_params_and_tracks_them():
    client, _, gateway = make_client(_snapshot("status-draining"))
    gateway.set_contract_mutation(CID, _ok_envelope(RID), returncode=0)
    response = submit(
        client,
        {
            "action": "stop_after",
            "n": "2",
            "container_id": CID,
            "repository": REPO,
            "request_id": RID,
        },
    )
    assert response.status_code == 200
    assert "Accepted" in response.text
    assert gateway.contract_mutation_calls == [
        {
            "container_id": CID,
            "action": "stop_after",
            "request_id": RID,
            "expect_repo": REPO,
            "exec_user": "agent",
            "n": 2,
            "issue": None,
        }
    ]
    expected = argv_contract_exec(CID, argv_contract_mutation("stop_after", RID, REPO, n=2))
    assert gateway.contract_argvs == [expected]
    # The follow-up track URL carries the gated parameter.
    assert "n=2" in response.text
    assert "stop --after 2" in response.text


# --- withdrawn capability surfaces as unsupported ------------------------------


def test_review_unadvertised_action_is_not_supported():
    client, _, _ = make_client(_snapshot("status-draining"))
    response = review(client, {"action": "handoff"})
    assert response.status_code == 409
    assert "not supported by this instance" in response.text


def test_submit_unsupported_reply_says_nothing_stored():
    client, _, gateway = make_client(_snapshot("status-simple-coding-agent"))
    gateway.set_contract_mutation(
        CID,
        _err_envelope(
            "unsupported",
            "this instance does not advertise operator-handoff",
            capability="operator-handoff",
        ),
        returncode=3,
    )
    response = submit(
        client,
        {
            "action": "handoff",
            "container_id": CID,
            "repository": REPO,
            "request_id": RID,
        },
    )
    assert response.status_code == 200
    assert "Not supported" in response.text
    assert "not supported by this instance; nothing stored" in response.text.lower()


# --- progress lines from intake.stop_plan / intake.priority ----------------------


def _intake_snapshot() -> dict:
    result = _snapshot("status-draining")
    stop_id = "dash-aaaaaaaaaaaaaaaa"
    next_id = "dash-bbbbbbbbbbbbbbbb"
    result["intake"]["stop_plan"] = {
        "request_id": stop_id,
        "sequence": 5,
        "remaining": 3,
        "counted": 2,
    }
    result["intake"]["priority"] = {"request_id": next_id, "issue": 122}

    def _record(request_id: str, sequence: int, kind: str, detail: str) -> dict:
        return {
            "request_id": request_id,
            "sequence": sequence,
            "kind": kind,
            "arguments": {},
            "repository": REPO,
            "acknowledgement": "accepted",
            "detail": detail,
            "created_at": "2026-11-06T09:12:03Z",
            "updated_at": "2026-11-06T09:12:03Z",
        }

    result["recent_commands"] = [
        _record(stop_id, 5, "stop_after", "stopping after 2 more"),
        _record(next_id, 6, "next_issue", "prioritized"),
    ]
    return result


def test_track_progress_lines_come_from_matching_intake_sections():
    client, _, _ = make_client(_intake_snapshot())
    stop_query = (
        f"action=stop_after&n=2&container_id={CID}&repository={REPO}"
        "&request_id=dash-aaaaaaaaaaaaaaaa&sequence=5"
    )
    response = client.get(f"/instances/web-0/control/track?{stop_query}")
    assert response.status_code == 200
    assert "3 left and 2 counted" in response.text
    next_query = (
        f"action=next_issue&issue=122&container_id={CID}&repository={REPO}"
        "&request_id=dash-bbbbbbbbbbbbbbbb&sequence=6"
    )
    response = client.get(f"/instances/web-0/control/track?{next_query}")
    assert response.status_code == 200
    assert "issue #122 pending" in response.text
    # A mismatched request ID shows no progress line.
    other_query = (
        f"action=stop_after&n=2&container_id={CID}&repository={REPO}"
        "&request_id=dash-cccccccccccccccc&sequence=5"
    )
    response = client.get(f"/instances/web-0/control/track?{other_query}")
    assert response.status_code == 200
    assert "3 left and 2 counted" not in response.text


def test_track_progress_line_needs_a_stamped_request_id():
    result = _intake_snapshot()
    result["intake"]["stop_plan"] = {"sequence": 5, "remaining": 3, "counted": 2}
    client, _, _ = make_client(result)
    stop_query = (
        f"action=stop_after&n=2&container_id={CID}&repository={REPO}"
        "&request_id=dash-aaaaaaaaaaaaaaaa&sequence=5"
    )
    response = client.get(f"/instances/web-0/control/track?{stop_query}")
    assert response.status_code == 200
    assert "3 left and 2 counted" not in response.text


# --- Intake cell and drawer ------------------------------------------------------


def test_fleet_intake_cell_shows_state_stop_plan_and_priority():
    client, _, _ = make_client(_intake_snapshot())
    body = client.get("/").text
    assert "closed" in body
    assert "Stop requested" in body
    assert "Stop planned (3 remaining)" in body
    assert "Next #122" in body


def test_drawer_contract_status_shows_counted_and_priority():
    client, _, _ = make_client(_intake_snapshot())
    body = client.get("/instances/web-0").text
    assert "3 remaining" in body
    assert "2 counted" in body
    assert "#122" in body


def test_audit_entries_for_gated_submit_carry_params():
    client, _, gateway = make_client(_snapshot("status-draining"))
    gateway.set_contract_mutation(CID, _ok_envelope(RID), returncode=0)
    response = submit(
        client,
        {
            "action": "next_issue",
            "issue": "122",
            "container_id": CID,
            "repository": REPO,
            "request_id": RID,
        },
    )
    assert response.status_code == 200
    path: Path = audit_path()
    entries = [
        json.loads(line) for line in path.read_text().splitlines() if line.strip()
    ]
    assert entries[0]["action"] == "next_issue"
    assert entries[0]["params"] == {"issue": 122}
    assert "issue=122" in response.text
