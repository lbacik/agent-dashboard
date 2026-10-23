"""Slice 5 (timeline half): fleet Intake chips and the command timeline.

Covers spec sections 8.1 ("Fleet table") and 8.5 (command timeline):
chips render in the Intake cell in order with stale styling; the console
carries a `<details>` timeline newest-first with origin chips, stepper
and detail; the timeline refreshes from
`GET /instances/{name}/control/commands` with an out-of-band summary.
Tests assert HTML fragments through TestClient, following the
``make_control_client`` + ``fresh_snapshot`` patterns in
``test_control_console.py``.
"""

from __future__ import annotations

import copy

from test_control_console import control_body, make_control_client
from test_web import load_snapshot


def rich_control() -> dict:
    """A control section warranting every chip and several timeline rows."""
    control = copy.deepcopy(
        load_snapshot("snapshot-attempt-in-progress.json")["control"]
    )
    control["handoff"] = {
        "request_id": "dash-aaaaaaaaaaaaaaaa",
        "accepted_at": "2026-09-30T11:58:00Z",
        "begun": True,
        "phase": "model_work",
        "acknowledgement": "accepted",
        "detail": "handoff in progress",
    }
    control["pending_command"] = {
        "request_id": "dash-bbbbbbbbbbbbbbbb",
        "sequence": 7,
        "kind": "stop",
        "acknowledgement": "accepted",
        "detail": "stop requested",
        "created_at": "2026-09-30T11:59:00Z",
        "updated_at": "2026-09-30T11:59:00Z",
    }
    control["stop_plan"] = {"remaining": 2}
    control["pending_next_issue"] = {"issue_number": 99}
    control["commands"] = {
        "dash-aaaaaaaaaaaaaaaa": {
            "request_id": "dash-aaaaaaaaaaaaaaaa",
            "sequence": 9,
            "kind": "handoff",
            "acknowledgement": "accepted",
            "detail": "handoff accepted; delivering soon",
            "created_at": "2026-09-30T11:58:00Z",
            "updated_at": "2026-09-30T12:00:00Z",
        },
        "dash-bbbbbbbbbbbbbbbb": {
            "request_id": "dash-bbbbbbbbbbbbbbbb",
            "sequence": 7,
            "kind": "stop",
            "acknowledgement": "completed",
            "detail": "stop done",
            "created_at": "2026-09-30T11:59:00Z",
            "updated_at": "2026-09-30T11:59:30Z",
        },
        "op-cli-1": {
            "request_id": "op-cli-1",
            "sequence": 8,
            "kind": "next_issue",
            "acknowledgement": "accepted",
            "detail": "next issue queued",
            "created_at": "2026-09-30T11:59:10Z",
            "updated_at": "2026-09-30T11:59:20Z",
        },
    }
    return control


def client_with_control(control: dict, **kwargs):
    snap = load_snapshot("snapshot-attempt-in-progress.json")
    snap["control"] = control
    return make_control_client({"work": snap}, **kwargs)


# Fleet Intake chips (spec 8.1).


def test_chips_render_in_order_in_intake_cell():
    client, _, _ = client_with_control(rich_control())
    body = client.get("/fleet").text
    for chip in (
        "handoff · model_work",
        "stop pending",
        "stops after 2 more",
        "next #99",
    ):
        assert chip in body
    positions = [
        body.index("handoff · model_work"),
        body.index("stop pending"),
        body.index("stops after 2 more"),
        body.index("next #99"),
    ]
    assert positions == sorted(positions)


def test_no_chips_without_command_state():
    client, _, _ = make_control_client(
        {"work": load_snapshot("snapshot-attempt-in-progress.json")}
    )
    body = client.get("/fleet").text
    assert "ctl-chip" not in body


def test_chips_take_stale_styling_when_control_stale():
    client, _, _ = client_with_control(rich_control(), backdate_control=90.0)
    body = client.get("/fleet").text
    assert "stale-tag" in body
    assert "ctl-chip is-stale" in body


def test_chips_not_stale_when_fresh():
    client, _, _ = client_with_control(rich_control())
    body = client.get("/fleet").text
    assert "ctl-chip is-stale" not in body
    assert "ctl-chip" in body


# Command timeline in the console body (spec 8.5).


def test_console_carries_open_persisted_timeline():
    client, _, _ = client_with_control(rich_control())
    body = control_body(client).text
    assert "Commands (last 10 from telemetry)" in body
    assert 'data-persist="control-timeline"' in body
    assert body.index("<details") < body.index("Commands (last 10 from telemetry)")
    details = body[body.index("<details") : body.index("<details") + 120]
    assert "open" in details
    assert 'hx-get="/instances/work/control/commands"' in body
    assert "sse:fleet" in body


def test_timeline_entries_newest_first_with_origin_and_detail():
    client, _, _ = client_with_control(rich_control())
    body = control_body(client).text
    assert body.index("#9") < body.index("#8") < body.index("#7")
    assert "dashboard" in body  # dash- request IDs
    assert "CLI" in body  # op-cli-1
    assert "handoff accepted; delivering soon" in body
    assert "stop done" in body
    # Kind labels read "next issue", not the action key.
    assert "next issue" in body
    # Updated-at clock readings.
    assert "12:00:00" in body
    assert "11:59:30" in body


def test_timeline_handoff_step_from_matching_phase():
    client, _, _ = client_with_control(rich_control())
    body = control_body(client).text
    assert "delivering" in body
    assert "model work" in body


def test_timeline_handoff_without_match_has_no_extra_steps():
    control = rich_control()
    control["handoff"] = None
    client, _, _ = client_with_control(control)
    body = control_body(client).text
    # The handoff command row still renders, but with the base stepper only.
    assert "#9" in body
    assert body.count("model work") == 0


def test_timeline_terminal_ack_wins_over_stale_handoff_phase():
    """A finished handoff reads as finished, not delivering/model work."""
    control = rich_control()
    control["handoff"]["phase"] = "model_work"
    control["commands"]["dash-aaaaaaaaaaaaaaaa"]["acknowledgement"] = "completed"
    client, _, _ = client_with_control(control)
    body = control_body(client).text
    assert '<li class="ctl-step is-current">completed</li>' in body
    assert '<li class="ctl-step is-current">model work</li>' not in body


def test_timeline_empty_renders_message():
    client, _, _ = make_control_client(
        {"work": load_snapshot("snapshot-attempt-in-progress.json")}
    )
    assert "No commands recorded." in control_body(client).text


def test_timeline_detail_is_escaped():
    control = rich_control()
    control["commands"]["op-cli-1"]["detail"] = "<b>bold</b> & done"
    client, _, _ = client_with_control(control)
    body = control_body(client).text
    assert "<b>bold</b>" not in body
    assert "&lt;b&gt;bold&lt;/b&gt;" in body


# The commands refresh route.


def commands_body(client, name: str = "work"):
    response = client.get(f"/instances/{name}/control/commands")
    assert response.status_code == 200
    return response


def test_commands_route_returns_list_plus_oob_summary():
    client, _, _ = client_with_control(rich_control())
    body = commands_body(client).text
    assert 'hx-swap-oob="true"' in body
    assert 'id="ctl-timeline-summary-work"' in body
    # The out-of-band swap replaces the summary element outright, so the
    # fragment must carry its class or the styling is lost on first refresh.
    assert 'class="ctl-timeline-sum"' in body
    assert "3 commands" in body
    assert "#9" in body
    # Newest first in the fragment too.
    assert body.index("#9") < body.index("#8") < body.index("#7")


def test_commands_refresh_never_toggles_details():
    """The fragment holds no `<details>`: the swap cannot open or close it."""
    client, _, _ = client_with_control(rich_control())
    body = commands_body(client).text
    assert "<details" not in body
    assert "data-persist" not in body


def test_commands_route_empty_renders_message():
    client, _, _ = make_control_client(
        {"work": load_snapshot("snapshot-attempt-in-progress.json")}
    )
    body = commands_body(client).text
    assert "No commands recorded." in body
    assert 'hx-swap-oob="true"' in body


def test_commands_route_unknown_name_is_404_fragment():
    client, _, _ = client_with_control(rich_control())
    response = client.get("/instances/nope/control/commands")
    assert response.status_code == 404
    assert "instance not found" in response.text


def test_commands_route_detail_is_escaped():
    control = rich_control()
    control["commands"]["dash-bbbbbbbbbbbbbbbb"]["detail"] = "<script>alert(1)</script>"
    client, _, _ = client_with_control(control)
    body = commands_body(client).text
    assert "<script>" not in body
    assert "&lt;script&gt;" in body


def test_only_last_ten_commands_render():
    control = rich_control()
    commands = {}
    for seq in range(1, 15):
        rid = f"dash-{seq:016x}"
        commands[rid] = {
            "request_id": rid,
            "sequence": seq,
            "kind": "stop",
            "acknowledgement": "completed",
            "detail": f"command {seq}",
            "created_at": "2026-09-30T10:00:00Z",
            "updated_at": "2026-09-30T10:00:00Z",
        }
    control["commands"] = commands
    client, _, _ = client_with_control(control)
    body = commands_body(client).text
    assert "#14" in body
    assert "#5" in body
    assert "#4" not in body
    assert "10 commands" in body


def test_stepper_marks_current_and_done():
    client, _, _ = client_with_control(rich_control())
    body = commands_body(client).text
    # The completed stop row: submitted/accepted done, completed current.
    assert "is-done" in body
    assert "is-current" in body
    assert "submitted" in body
    assert "accepted" in body
