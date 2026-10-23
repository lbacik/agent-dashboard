"""Review route, request protection and the audit trail (spec sections 6-7, 8.3).

The review route returns the confirmation with the exact argv line and a
``dash-`` request ID; Cancel restores the composer. Every POST control
route is protected (Origin, Sec-Fetch-Site, HX-Request, control mode): a
failure is a 403 fragment and one ``refused`` audit entry, and no exec
runs. Validation, unavailable-instance and disabled-action refusals return
422/403/409 with the composer or console re-rendered and a ``refused``
entry. Tests assert HTML fragments through TestClient.
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
from agent_dashboard.collector import run_cycle
from agent_dashboard.config import Settings
from agent_dashboard.control_audit import audit_path
from agent_dashboard.fleet import FleetState
from agent_dashboard.gateway import ManagedInstance, argv_control


@pytest.fixture(autouse=True)
def _isolated_audit(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))


GOOD_HEADERS = {"Origin": "http://localhost", "HX-Request": "true"}


def make_review_client(snapshots: dict[str, dict], settings: Settings | None = None):
    """A review client with hex container IDs (the gateway validates them)."""
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


def post_review(client: TestClient, name: str, data: dict, headers: dict | None = None):
    if headers is None:
        headers = dict(GOOD_HEADERS)
    return client.post(f"/instances/{name}/control/review", data=data, headers=headers)


def audit_entries() -> list[dict]:
    path = audit_path()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def live_client(name: str = "work", fixture: str = "snapshot-attempt-in-progress.json"):
    return make_review_client({name: load_snapshot(fixture)})


# --- the confirmation ----------------------------------------------------------


def test_review_stop_returns_confirmation_with_exact_argv():
    client, _, gateway, cids = live_client()
    response = post_review(client, "work", {"action": "stop"})
    assert response.status_code == 200
    match = re.search(r"Request ID <code>(dash-[0-9a-f]{16})</code>", response.text)
    assert match is not None
    request_id = match.group(1)
    expected = " ".join(argv_control(cids["work"], "stop", request_id=request_id))
    assert expected in response.text
    # The head names the command, the instance and the repository.
    assert "<code>agentctl stop</code> on <strong>work</strong>" in response.text
    assert "example-owner/example-repo" in response.text
    # Hidden fields carry the action, container ID, repository and request ID.
    assert 'name="action" value="stop"' in response.text
    assert f'name="container_id" value="{cids["work"]}"' in response.text
    assert 'name="repository" value="example-owner/example-repo"' in response.text
    assert f'name="request_id" value="{request_id}"' in response.text
    # Danger tone for stop; Confirm is autofocus; Cancel GETs the console body.
    assert "ctl-tone-danger" in response.text
    assert "autofocus" in response.text
    assert 'hx-get="/instances/work/control"' in response.text
    # While the submit is in flight the buttons disable and the sending note
    # shows: the indicator lives inside the requesting form.
    assert 'hx-disabled-elt=".ctl-confirm-form button"' in response.text
    form_body = response.text.split('<form class="ctl-confirm-form"')[1].split("</form>")[0]
    assert "ctl-sending htmx-indicator" in form_body
    assert "Sending… waiting for the agent to accept" in form_body
    # A 200 writes no audit entry and runs no exec.
    assert audit_entries() == []
    assert gateway.control_calls == []
    assert gateway.command_status_calls == []


def test_review_stop_after_renders_param_and_danger_tone():
    client, _, _, cids = live_client()
    response = post_review(client, "work", {"action": "stop_after", "n": "3"})
    assert response.status_code == 200
    match = re.search(r"dash-[0-9a-f]{16}", response.text)
    assert match is not None
    expected = " ".join(
        argv_control(cids["work"], "stop_after", request_id=match.group(0), n=3)
    )
    assert expected in response.text
    assert "stop --after 3" in response.text
    assert "ctl-tone-danger" in response.text
    assert 'name="n" value="3"' in response.text


def test_review_next_issue_renders_param_and_primary_tone():
    client, _, _, cids = live_client()
    response = post_review(client, "work", {"action": "next_issue", "issue": "42"})
    assert response.status_code == 200
    match = re.search(r"dash-[0-9a-f]{16}", response.text)
    assert match is not None
    expected = " ".join(
        argv_control(cids["work"], "next_issue", request_id=match.group(0), issue=42)
    )
    assert expected in response.text
    assert "next issue 42" in response.text
    assert "ctl-tone-primary" in response.text
    assert 'name="issue" value="42"' in response.text


def test_review_resume_uses_primary_tone():
    snap = load_snapshot("snapshot-attempt-in-progress.json")
    snap["control"]["stop_plan"] = {"remaining": 2}
    client, _, _, _ = make_review_client({"work": snap})
    response = post_review(client, "work", {"action": "resume"})
    assert response.status_code == 200
    assert "ctl-tone-primary" in response.text
    assert "<code>agentctl resume</code>" in response.text


def versioned_client(version: str | None):
    """A review client whose snapshot reports ``version`` as the agent version.

    ``None`` removes the version (an unreadable version, which counts as
    old). The fixture default (``0.4.5``) predates the guard.
    """
    snap = load_snapshot("snapshot-attempt-in-progress.json")
    if version is None:
        snap["identity"].pop("agent_version", None)
    else:
        snap["identity"]["agent_version"] = version
    return make_review_client({"work": snap})


@pytest.mark.parametrize(
    ("version", "want_guard"),
    [
        ("v0.8.0", True),  # at the release that ships --expect-repo
        ("v0.9.0", True),  # above it
        ("v0.7.2", False),  # below it
        ("garbage", False),  # unreadable versions count as old
        (None, False),
    ],
)
def test_review_confirmation_expect_repo_version_gate(version, want_guard):
    client, _, _, cids = versioned_client(version)
    response = post_review(client, "work", {"action": "stop"})
    assert response.status_code == 200
    match = re.search(r"dash-[0-9a-f]{16}", response.text)
    assert match is not None
    if want_guard:
        expected = " ".join(
            argv_control(
                cids["work"],
                "stop",
                request_id=match.group(0),
                expect_repo="example-owner/example-repo",
            )
        )
        assert expected in response.text
        assert "--expect-repo=example-owner/example-repo" in response.text
    else:
        expected = " ".join(
            argv_control(cids["work"], "stop", request_id=match.group(0))
        )
        assert expected in response.text
        assert "--expect-repo" not in response.text


def test_cancel_restores_the_composer():
    client, _, _, _ = live_client()
    response = post_review(client, "work", {"action": "stop"})
    assert response.status_code == 200
    match = re.search(r'hx-get="([^"]+/control)"', response.text)
    assert match is not None
    restored = client.get(match.group(1))
    assert restored.status_code == 200
    assert "ctl-composer" in restored.text


def test_review_unknown_instance_is_404_without_audit():
    client, _, gateway, _ = live_client()
    response = post_review(client, "nope", {"action": "stop"})
    assert response.status_code == 404
    assert "instance not found" in response.text
    assert audit_entries() == []
    assert gateway.control_calls == []


# --- request protection ----------------------------------------------------------


PROTECTION_CASES = [
    (
        "origin with another port",
        {"Origin": "http://localhost:9999", "HX-Request": "true"},
        "origin",
        "Request refused: missing or mismatched Origin header",
    ),
    (
        "missing origin",
        {"HX-Request": "true"},
        "origin",
        "Request refused: missing or mismatched Origin header",
    ),
    (
        "cross-site fetch",
        {"Origin": "http://localhost", "HX-Request": "true", "Sec-Fetch-Site": "cross-site"},
        "fetch_site",
        "Request refused: Sec-Fetch-Site must be same-origin",
    ),
    (
        "missing htmx header",
        {"Origin": "http://localhost"},
        "htmx_header",
        "Request refused: missing HX-Request header",
    ),
]


@pytest.mark.parametrize("label,headers,reason,fragment", PROTECTION_CASES)
def test_request_protection_refusals(label, headers, reason, fragment):
    client, _, gateway, _ = live_client()
    response = post_review(client, "work", {"action": "stop"}, headers=headers)
    assert response.status_code == 403, label
    assert fragment in response.text, label
    entries = audit_entries()
    assert len(entries) == 1, label
    assert entries[0]["outcome"] == "refused", label
    assert entries[0]["reason"] == reason, label
    assert entries[0]["instance"] == "work", label
    assert gateway.control_calls == [], label
    assert gateway.command_status_calls == [], label


def test_request_protection_origin_is_case_insensitive():
    client, _, gateway, _ = live_client()
    headers = {"Origin": "HTTP://LOCALHOST", "HX-Request": "true"}
    response = post_review(client, "work", {"action": "stop"}, headers=headers)
    assert response.status_code == 200
    assert gateway.control_calls == []


def test_request_protection_control_off():
    client, _, gateway, _ = make_review_client(
        {"work": load_snapshot("snapshot-attempt-in-progress.json")},
        Settings(poll_interval=5.0, control=False),
    )
    response = post_review(client, "work", {"action": "stop"})
    assert response.status_code == 403
    assert "Controls unavailable: control is off" in response.text
    entries = audit_entries()
    assert len(entries) == 1
    assert entries[0]["reason"] == "controls_unavailable"
    assert gateway.control_calls == []


def test_failed_refusal_write_still_returns_the_refusal(monkeypatch, capsys):
    from agent_dashboard import control_routes as routes

    def boom(entry):
        raise OSError("disk full")

    monkeypatch.setattr(routes, "append_entry", boom)
    client, _, gateway, _ = live_client()
    response = post_review(
        client, "work", {"action": "stop"}, headers={"HX-Request": "true"}
    )
    assert response.status_code == 403
    assert "Request refused:" in response.text
    assert gateway.control_calls == []
    assert "disk full" in capsys.readouterr().err


# --- validation, unavailable instance, disabled action ---------------------------


def test_review_invalid_action_is_422_with_composer_and_values():
    client, _, gateway, _ = live_client()
    response = post_review(client, "work", {"action": "explode", "n": "5"})
    assert response.status_code == 422
    assert "Invalid action" in response.text
    assert "ctl-composer" in response.text
    assert 'name="n"' in response.text
    assert 'value="5"' in response.text
    entries = audit_entries()
    assert len(entries) == 1
    assert entries[0]["reason"] == "validation"
    assert entries[0]["request_id"] is None
    assert gateway.control_calls == []


def test_review_invalid_n_is_422_with_entered_values():
    client, _, gateway, _ = live_client()
    response = post_review(client, "work", {"action": "stop_after", "n": "0"})
    assert response.status_code == 422
    assert "Invalid attempts before stopping" in response.text
    assert "ctl-composer" in response.text
    assert 'value="stop_after" checked' in response.text
    assert 'name="n"' in response.text
    assert 'value="0"' in response.text
    entries = audit_entries()
    assert len(entries) == 1
    assert entries[0]["reason"] == "validation"
    assert entries[0]["action"] == "stop_after"
    assert gateway.control_calls == []


def test_review_invalid_issue_is_422_with_entered_values():
    client, _, gateway, _ = live_client()
    response = post_review(client, "work", {"action": "next_issue", "issue": "oops"})
    assert response.status_code == 422
    assert "Invalid issue number" in response.text
    assert 'value="next_issue" checked' in response.text
    assert 'name="issue"' in response.text
    assert 'value="oops"' in response.text
    assert audit_entries()[0]["reason"] == "validation"
    assert gateway.control_calls == []


def test_review_unavailable_instance_is_403_console_line():
    snap = load_snapshot("snapshot-attempt-in-progress.json")
    snap["control"] = None
    client, _, gateway, _ = make_review_client({"work": snap})
    response = post_review(client, "work", {"action": "stop"})
    assert response.status_code == 403
    assert "Controls unavailable: the snapshot has no control section" in response.text
    entries = audit_entries()
    assert len(entries) == 1
    assert entries[0]["outcome"] == "refused"
    assert entries[0]["reason"] == "action_unavailable"
    assert gateway.control_calls == []


def test_review_disabled_action_is_409_with_reason():
    client, _, gateway, _ = live_client()
    # Resume reads "already running" on the attempt-in-progress fixture.
    response = post_review(client, "work", {"action": "resume"})
    assert response.status_code == 409
    assert "already running" in response.text
    assert "ctl-composer" in response.text
    assert 'value="resume" disabled checked' in response.text
    entries = audit_entries()
    assert len(entries) == 1
    assert entries[0]["reason"] == "action_unavailable"
    assert entries[0]["action"] == "resume"
    assert gateway.control_calls == []


# --- form parsing and the htmx error swap ------------------------------------------


def test_parse_form_first_value_wins_and_blanks_kept():
    assert control_routes.parse_form(b"") == {}
    assert control_routes.parse_form(b"action=stop&n=3&n=4") == {"action": "stop", "n": "3"}
    assert control_routes.parse_form(b"action=&n=") == {"action": "", "n": ""}
    assert control_routes.parse_form(b"issue=caf%C3%A9") == {"issue": "café"}


def test_htmx_error_swap_script_opts_in_only_control_routes():
    path = Path(control_routes.__file__).resolve().parent / "static" / "control-error-swap.js"
    text = path.read_text()
    assert "htmx:beforeSwap" in text
    assert "shouldSwap" in text
    assert "isError" in text
    for status in ("403", "409", "422", "503"):
        assert status in text
    assert "instances" in text and "control" in text


def test_index_loads_the_error_swap_script():
    client, _, _, _ = live_client()
    body = client.get("/").text
    assert "/static/control-error-swap.js" in body
