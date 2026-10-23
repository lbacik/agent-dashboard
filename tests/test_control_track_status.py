"""Following an accepted card and asking the agent directly (spec 8.4).

An Accepted card re-requests ``GET /instances/{name}/control/track`` on
every ``sse:fleet`` event until its acknowledgement is terminal; ``track``
reads only the latest good snapshot and makes no Docker call. ``POST
/instances/{name}/control/status`` runs the read-only ``agentctl command
<id>`` exec and re-renders the card without writing a Control audit entry.
Web tests assert HTML fragments through TestClient.
"""

from __future__ import annotations

import asyncio
import copy
import time

import pytest
from fastapi.testclient import TestClient
from test_control_submit import (
    GOOD_HEADERS,
    REPO,
    RID,
    audit_entries,
    base_form,
    live_client,
    make_submit_client,
)
from test_web import fresh_snapshot, load_snapshot

from agent_dashboard.collector import SNAPSHOT_FAILED, run_cycle
from agent_dashboard.gateway import ControlBusyError, ControlTimeoutError

STATUS_HEADERS = dict(GOOD_HEADERS)


@pytest.fixture(autouse=True)
def _isolated_audit(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))


def track(client: TestClient, name: str, params: dict, headers: dict | None = None):
    return client.get(f"/instances/{name}/control/track", params=params)


def status(client: TestClient, name: str, data: dict, headers: dict | None = None):
    if headers is None:
        headers = dict(STATUS_HEADERS)
    return client.post(f"/instances/{name}/control/status", data=data, headers=headers)


def track_params(cids: dict, name: str = "work", **overrides) -> dict:
    params = {
        "action": "stop",
        "container_id": cids[name],
        "repository": REPO,
        "request_id": RID,
        "sequence": "12",
    }
    params.update(overrides)
    return params


def accepted_stdout(
    *,
    acknowledgement: str = "accepted",
    effect: str = "stop will begin soon",
    sequence: int = 12,
) -> bytes:
    return (
        f"repository: {REPO}\nrequest_id: {RID}\nsequence: {sequence}\n"
        f"acknowledgement: {acknowledgement}\neffect: {effect}\n"
    ).encode()


def record_stdout(
    *,
    acknowledgement: str = "accepted",
    effect: str = "stop will begin soon",
    detail: str = "stop recorded",
    sequence: int = 12,
) -> bytes:
    return (
        f"repository: {REPO}\nrequest_id: {RID}\nsequence: {sequence}\n"
        f"acknowledgement: {acknowledgement}\neffect: {effect}\ndetail: {detail}\n"
    ).encode()


def fresh_control_mutation(base: dict, now: float, **control_fields) -> dict:
    """A fresh snapshot whose control section carries the given fields."""
    snap = fresh_snapshot(copy.deepcopy(base), now)
    assert isinstance(snap.get("control"), dict)
    snap["control"].update(control_fields)
    return snap


def submit_accepted(client, gateway, cids, form: dict, *, sequence: int = 12):
    gateway.set_control(
        cids["work"],
        accepted_stdout(sequence=sequence),
        b"",
        0,
    )
    response = client.post(
        "/instances/work/control/submit", data=form, headers=dict(GOOD_HEADERS)
    )
    assert response.status_code == 200
    assert "Accepted" in response.text
    return response


# --- the submit card starts following -------------------------------------------


def test_submit_accepted_card_follows_with_track_trigger():
    client, _, gateway, cids = live_client()
    response = submit_accepted(client, gateway, cids, base_form(cids))
    assert "control/track" in response.text
    assert "following…" in response.text
    assert 'hx-trigger="sse:fleet"' in response.text
    assert "Check status" not in response.text


def test_submit_completed_card_does_not_follow():
    client, _, gateway, cids = live_client()
    gateway.set_control(
        cids["work"],
        accepted_stdout(acknowledgement="completed", effect="resumed ok"),
        b"",
        0,
    )
    response = client.post(
        "/instances/work/control/submit", data=base_form(cids), headers=dict(GOOD_HEADERS)
    )
    assert response.status_code == 200
    assert "Completed" in response.text
    assert "control/track" not in response.text
    assert "following…" not in response.text


# --- track: an accepted stop-after follows to a terminal state ------------------


def test_track_stop_after_progress_then_completed():
    base = load_snapshot("snapshot-attempt-in-progress.json")
    client, state, gateway, cids = make_submit_client({"work": base})
    submit_accepted(
        client, gateway, cids, base_form(cids, action="stop_after", extra={"n": "3"})
    )
    # The submit above already ran one control exec; track must add none.
    submitted_controls = len(gateway.control_calls)
    submitted_statuses = len(gateway.command_status_calls)
    params = track_params(cids, action="stop_after", n="3")
    now = time.time() + 10.0
    snap = fresh_control_mutation(
        base,
        now,
        commands={
            RID: {
                "request_id": RID,
                "sequence": 12,
                "kind": "stop_after",
                "acknowledgement": "accepted",
                "detail": "stop after 3 recorded",
                "updated_at": "2026-09-30T10:20:00Z",
            }
        },
        stop_plan={"request_id": RID, "remaining": 2, "requested": 3},
    )
    gateway.set_snapshot(cids["work"], snap)
    asyncio.run(run_cycle(state, gateway, max_concurrent_execs=4, now=now))
    response = track(client, "work", params)
    assert response.status_code == 200
    assert "Accepted" in response.text
    assert "stop after 3 recorded" in response.text
    assert "2 of 3 attempt(s) left" in response.text
    assert "following…" in response.text
    assert "control/track" in response.text
    # Track reads the snapshot only: no control or command-status exec ran.
    assert len(gateway.control_calls) == submitted_controls
    assert len(gateway.command_status_calls) == submitted_statuses

    later = time.time() + 20.0
    done = fresh_control_mutation(
        base,
        later,
        commands={
            RID: {
                "request_id": RID,
                "sequence": 12,
                "kind": "stop_after",
                "acknowledgement": "completed",
                "detail": "stop completed after 3 attempts",
                "updated_at": "2026-09-30T10:30:00Z",
            }
        },
        stop_plan=None,
    )
    gateway.set_snapshot(cids["work"], done)
    asyncio.run(run_cycle(state, gateway, max_concurrent_execs=4, now=later))
    terminal = track(client, "work", params)
    assert terminal.status_code == 200
    assert "Completed" in terminal.text
    assert "ctl-tone-ok" in terminal.text
    assert "stop completed after 3 attempts" in terminal.text
    assert "control/track" not in terminal.text
    assert "following…" not in terminal.text
    assert len(gateway.control_calls) == submitted_controls
    assert len(gateway.command_status_calls) == submitted_statuses


def test_track_handoff_progress_then_superseded():
    base = load_snapshot("snapshot-attempt-in-progress.json")
    client, state, gateway, cids = make_submit_client({"work": base})
    submit_accepted(client, gateway, cids, base_form(cids, action="handoff"))
    params = track_params(cids, action="handoff")
    now = time.time() + 10.0
    snap = fresh_control_mutation(
        base,
        now,
        commands={
            RID: {
                "request_id": RID,
                "sequence": 12,
                "kind": "handoff",
                "acknowledgement": "accepted",
                "detail": "handoff requested",
                "updated_at": "2026-09-30T10:20:00Z",
            }
        },
        handoff={"request_id": RID, "acknowledgement": "accepted", "phase": "model_work"},
    )
    gateway.set_snapshot(cids["work"], snap)
    asyncio.run(run_cycle(state, gateway, max_concurrent_execs=4, now=now))
    response = track(client, "work", params)
    assert response.status_code == 200
    assert "phase model_work" in response.text
    assert "following…" in response.text

    later = time.time() + 20.0
    finished = fresh_control_mutation(
        base,
        later,
        commands={
            RID: {
                "request_id": RID,
                "sequence": 12,
                "kind": "handoff",
                "acknowledgement": "superseded",
                "detail": "superseded by a newer handoff",
                "updated_at": "2026-09-30T10:30:00Z",
            }
        },
        handoff=None,
    )
    gateway.set_snapshot(cids["work"], finished)
    asyncio.run(run_cycle(state, gateway, max_concurrent_execs=4, now=later))
    terminal = track(client, "work", params)
    assert terminal.status_code == 200
    assert "Superseded" in terminal.text
    assert "ctl-tone-muted" in terminal.text
    assert "superseded by a newer handoff" in terminal.text
    assert "control/track" not in terminal.text


def test_track_next_issue_progress_then_not_fulfilled():
    base = load_snapshot("snapshot-attempt-in-progress.json")
    client, state, gateway, cids = make_submit_client({"work": base})
    submit_accepted(
        client, gateway, cids, base_form(cids, action="next_issue", extra={"issue": "42"})
    )
    params = track_params(cids, action="next_issue", issue="42")
    now = time.time() + 10.0
    snap = fresh_control_mutation(
        base,
        now,
        commands={
            RID: {
                "request_id": RID,
                "sequence": 12,
                "kind": "next_issue",
                "acknowledgement": "accepted",
                "detail": "next issue recorded",
                "updated_at": "2026-09-30T10:20:00Z",
            }
        },
        pending_next_issue={"request_id": RID, "issue_number": 42},
    )
    gateway.set_snapshot(cids["work"], snap)
    asyncio.run(run_cycle(state, gateway, max_concurrent_execs=4, now=now))
    response = track(client, "work", params)
    assert response.status_code == 200
    assert "waiting for the next permitted claim" in response.text
    assert "following…" in response.text

    later = time.time() + 20.0
    finished = fresh_control_mutation(
        base,
        later,
        commands={
            RID: {
                "request_id": RID,
                "sequence": 12,
                "kind": "next_issue",
                "acknowledgement": "not fulfilled",
                "detail": "issue closed before the claim",
                "updated_at": "2026-09-30T10:30:00Z",
            }
        },
        pending_next_issue=None,
    )
    gateway.set_snapshot(cids["work"], finished)
    asyncio.run(run_cycle(state, gateway, max_concurrent_execs=4, now=later))
    terminal = track(client, "work", params)
    assert terminal.status_code == 200
    assert "Not fulfilled" in terminal.text
    assert "ctl-tone-warn" in terminal.text
    assert "issue closed before the claim" in terminal.text
    assert "control/track" not in terminal.text


# --- track: every following case -------------------------------------------------


def test_track_unknown_instance_stops_with_gone_message():
    client, _, gateway, cids = live_client()
    response = track(client, "nope", track_params(cids))
    assert response.status_code == 200
    assert "Instance replaced or unavailable" in response.text
    assert "/data volume" in response.text
    assert "control/track" not in response.text
    assert gateway.control_calls == []
    assert gateway.command_status_calls == []


def test_track_replaced_container_stops_with_gone_message():
    client, _, gateway, cids = live_client()
    response = track(client, "work", track_params(cids, container_id="abc123"))
    assert response.status_code == 200
    assert "Instance replaced or unavailable" in response.text
    # Autoescape renders the apostrophe as &#39;, so match the tail.
    assert "record of this command is on its /data volume." in response.text
    assert "control/track" not in response.text


def test_track_stale_reach_pauses_but_keeps_following():
    client, state, gateway, cids = live_client()
    gateway.set_error_code(cids["work"], SNAPSHOT_FAILED, "boom")
    asyncio.run(run_cycle(state, gateway, max_concurrent_execs=4, now=time.time() + 30))
    response = track(client, "work", track_params(cids))
    assert response.status_code == 200
    assert "following paused:" in response.text
    assert "control/track" in response.text
    assert "following…" in response.text
    assert gateway.control_calls == []
    assert gateway.command_status_calls == []


def test_track_absent_id_below_every_sequence_is_evicted():
    base = load_snapshot("snapshot-attempt-in-progress.json")
    client, state, gateway, cids = make_submit_client({"work": base})
    now = time.time() + 10.0
    snap = fresh_control_mutation(
        base,
        now,
        commands={
            "dash-aaaaaaaaaaaaaaaa": {
                "request_id": "dash-aaaaaaaaaaaaaaaa",
                "sequence": 20,
                "kind": "stop",
                "acknowledgement": "completed",
                "detail": "older command",
                "updated_at": "2026-09-30T10:20:00Z",
            },
            "dash-bbbbbbbbbbbbbbbb": {
                "request_id": "dash-bbbbbbbbbbbbbbbb",
                "sequence": 21,
                "kind": "resume",
                "acknowledgement": "accepted",
                "detail": "newer command",
                "updated_at": "2026-09-30T10:21:00Z",
            },
        },
    )
    gateway.set_snapshot(cids["work"], snap)
    asyncio.run(run_cycle(state, gateway, max_concurrent_execs=4, now=now))
    response = track(client, "work", track_params(cids))
    assert response.status_code == 200
    assert "No longer in the last 10 commands" in response.text
    assert "Check status" in response.text
    assert "control/track" not in response.text
    assert "following…" not in response.text


def test_track_absent_id_otherwise_waits_and_keeps_following():
    client, _, gateway, cids = live_client()
    # The fixture's commands are empty and the card names a sequence, so
    # the ID is absent with nothing recorded below it.
    response = track(client, "work", track_params(cids))
    assert response.status_code == 200
    assert "waiting for the next snapshot" in response.text
    assert "control/track" in response.text
    assert "following…" in response.text
    assert gateway.control_calls == []
    assert gateway.command_status_calls == []


def test_track_validation_rejects_bad_request_id():
    client, _, _, cids = live_client()
    response = track(client, "work", track_params(cids, request_id="bogus"))
    assert response.status_code == 422
    assert "Invalid request ID" in response.text


def test_track_agent_text_is_escaped():
    base = load_snapshot("snapshot-attempt-in-progress.json")
    client, state, gateway, cids = make_submit_client({"work": base})
    now = time.time() + 10.0
    snap = fresh_control_mutation(
        base,
        now,
        commands={
            RID: {
                "request_id": RID,
                "sequence": 12,
                "kind": "stop",
                "acknowledgement": "accepted",
                "detail": "<script>alert(1)</script>",
                "updated_at": "2026-09-30T10:20:00Z",
            }
        },
    )
    gateway.set_snapshot(cids["work"], snap)
    asyncio.run(run_cycle(state, gateway, max_concurrent_execs=4, now=now))
    response = track(client, "work", track_params(cids))
    assert response.status_code == 200
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text
    assert "<script>alert(1)</script>" not in response.text


# --- status: the read-only check --------------------------------------------------


def test_status_accepted_record_keeps_following_without_audit():
    client, _, gateway, cids = live_client()
    gateway.set_command_status(
        cids["work"], record_stdout(acknowledgement="accepted"), b"", 0
    )
    response = status(client, "work", base_form(cids))
    assert response.status_code == 200
    assert "Accepted" in response.text
    assert "stop will begin soon" in response.text
    assert "following…" in response.text
    assert "control/track" in response.text
    assert audit_entries() == []
    assert len(gateway.command_status_calls) == 1
    assert gateway.command_status_calls[0] == (cids["work"], RID)


@pytest.mark.parametrize(
    "acknowledgement,title,tone,stdout_kwargs,expected_body",
    [
        # Completed shows the record's effect when present.
        (
            "completed",
            "Completed",
            "ctl-tone-ok",
            {"effect": "the stop committed", "detail": "older detail line"},
            "the stop committed",
        ),
        # Completed falls back to the record's detail without an effect.
        (
            "completed",
            "Completed",
            "ctl-tone-ok",
            {"effect": "", "detail": "the stop committed"},
            "the stop committed",
        ),
        # Superseded shows the record's detail, not the effect.
        (
            "superseded",
            "Superseded",
            "ctl-tone-muted",
            {"effect": "stale effect line", "detail": "the stop committed"},
            "the stop committed",
        ),
        # Not fulfilled shows the record's detail, not the effect.
        (
            "not fulfilled",
            "Not fulfilled",
            "ctl-tone-warn",
            {"effect": "stale effect line", "detail": "the stop committed"},
            "the stop committed",
        ),
    ],
)
def test_status_terminal_record_renders_without_following(
    acknowledgement, title, tone, stdout_kwargs, expected_body
):
    client, _, gateway, cids = live_client()
    gateway.set_command_status(
        cids["work"],
        record_stdout(acknowledgement=acknowledgement, **stdout_kwargs),
        b"",
        0,
    )
    response = status(client, "work", base_form(cids))
    assert response.status_code == 200
    assert title in response.text
    assert tone in response.text
    assert expected_body in response.text
    assert "control/track" not in response.text
    assert "following…" not in response.text
    assert audit_entries() == []


def test_status_not_recorded_offers_retry_only():
    client, _, gateway, cids = live_client()
    gateway.set_command_status(cids["work"], b"", b"no such command: gone", 1)
    response = status(client, "work", base_form(cids))
    assert response.status_code == 200
    assert "Not recorded" in response.text
    assert (
        "Not recorded: the command was not applied. "
        "Retry with the same request ID to send it." in response.text
    )
    assert "Retry with the same request ID" in response.text
    assert "Check status" not in response.text
    assert audit_entries() == []


def test_status_unknown_shows_could_not_check_with_both_buttons():
    client, _, gateway, cids = live_client()
    gateway.set_command_status(
        cids["work"], b"", b"error: dial unix /tmp/agent.sock: refused", 2
    )
    response = status(client, "work", base_form(cids))
    assert response.status_code == 200
    assert "Could not check:" in response.text
    assert "Retry with the same request ID" in response.text
    assert "Check status" in response.text
    assert audit_entries() == []


def test_status_timeout_is_could_not_check_without_audit():
    client, _, gateway, cids = live_client()
    gateway.set_command_status_error(
        cids["work"], ControlTimeoutError("docker exec timed out after 15 s")
    )
    response = status(client, "work", base_form(cids))
    assert response.status_code == 200
    assert "Could not check:" in response.text
    assert audit_entries() == []


def test_status_missing_origin_is_403_without_audit():
    client, _, gateway, cids = live_client()
    gateway.set_command_status(
        cids["work"], record_stdout(acknowledgement="completed"), b"", 0
    )
    response = status(
        client, "work", base_form(cids), headers={"HX-Request": "true"}
    )
    assert response.status_code == 403
    assert "Request refused" in response.text
    assert audit_entries() == []
    assert gateway.command_status_calls == []


def test_status_busy_held_slot_is_409_without_audit():
    client, _, gateway, cids = live_client()
    gateway.hold_control(cids["work"])
    try:
        response = status(client, "work", base_form(cids))
    finally:
        gateway.release_control(cids["work"])
    assert response.status_code == 409
    assert "Another command is being sent to this instance." in response.text
    assert audit_entries() == []
    assert gateway.command_status_calls == []


def test_status_busy_race_is_409_without_audit():
    client, _, gateway, cids = live_client()
    gateway.set_command_status_error(
        cids["work"],
        ControlBusyError("another control exec is already in flight"),
    )
    response = status(client, "work", base_form(cids))
    assert response.status_code == 409
    assert "Another command is being sent to this instance." in response.text
    assert audit_entries() == []


def test_status_unknown_instance_is_404_without_audit():
    client, _, gateway, _ = live_client()
    response = status(client, "nope", {"action": "stop"})
    assert response.status_code == 404
    assert "instance not found" in response.text
    assert audit_entries() == []
    assert gateway.command_status_calls == []


def test_status_validation_is_422_without_audit():
    client, _, gateway, cids = live_client()
    response = status(client, "work", base_form(cids, request_id="bogus"))
    assert response.status_code == 422
    assert "Refused" in response.text
    assert "Invalid request ID" in response.text
    assert audit_entries() == []
    assert gateway.command_status_calls == []


def test_status_replaced_container_reports_gone_without_exec():
    client, _, gateway, cids = live_client()
    gateway.set_command_status(
        cids["work"], record_stdout(acknowledgement="completed"), b"", 0
    )
    response = status(client, "work", base_form(cids, container_id="abc123"))
    assert response.status_code == 200
    assert "Instance replaced or unavailable" in response.text
    assert gateway.command_status_calls == []
    assert audit_entries() == []


def test_status_agent_text_is_escaped():
    client, _, gateway, cids = live_client()
    gateway.set_command_status(
        cids["work"],
        record_stdout(
            acknowledgement="completed", effect="<b>stopped</b>", detail="<i>x</i>"
        ),
        b"",
        0,
    )
    response = status(client, "work", base_form(cids))
    assert response.status_code == 200
    assert "&lt;b&gt;stopped&lt;/b&gt;" in response.text
    assert "<b>stopped</b>" not in response.text


# --- wiring: the drawer keeps its console ------------------------------------------


def test_drawer_console_is_preserved_across_sse_swaps():
    client, _, _, _ = live_client()
    body = client.get("/instances/work").text
    assert 'id="control-console-work"' in body
    assert "hx-preserve" in body
    assert 'id="ctl-feedback-work"' in body
