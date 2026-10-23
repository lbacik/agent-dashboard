"""Control console (spec slice 4): the read-only composer in the drawer.

The console tells the operator which Control actions they could take and
why the others are unavailable. Nothing is sent yet: "Review…" is not
wired up. Tests assert HTML fragments through TestClient, following the
``make_fleet_client`` + ``fresh_snapshot`` patterns in ``test_web.py``.
"""

from __future__ import annotations

import asyncio
import copy
import time

import pytest
from fake_gateway import FakeGateway
from fastapi.testclient import TestClient
from test_web import fresh_snapshot, load_snapshot

from agent_dashboard import cli as cli_module
from agent_dashboard.app import create_app
from agent_dashboard.collector import run_cycle
from agent_dashboard.config import Settings, load_settings
from agent_dashboard.control import (
    ACTION_COMMANDS,
    ACTION_ORDER,
    ControlAvailability,
    ControlMode,
    control_availability,
    control_slug,
)
from agent_dashboard.fleet import FleetState
from agent_dashboard.gateway import ManagedInstance


def make_control_client(
    snapshots: dict[str, dict],
    settings: Settings | None = None,
    *,
    backdate_control: float = 0.0,
):
    """A client like ``make_fleet_client`` with custom settings.

    ``backdate_control`` moves every ``control_observed_at`` that many
    seconds into the past, so the control-stale note renders.
    """
    now = time.time()
    state = FleetState()
    gateway = FakeGateway(
        [ManagedInstance(name, f"id-{name}", "running") for name in snapshots]
    )
    for name, snap in snapshots.items():
        stamped = fresh_snapshot(copy.deepcopy(snap), now)
        if backdate_control:
            control = stamped.get("control")
            if isinstance(control, dict):
                old = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - backdate_control)
                )
                control["control_observed_at"] = old
        gateway.set_snapshot(f"id-{name}", stamped)
    asyncio.run(run_cycle(state, gateway, max_concurrent_execs=4, now=now))
    app = create_app(
        state=state,
        settings=settings if settings is not None else Settings(poll_interval=5.0),
        gateway=gateway,
        start_poller=False,
    )
    return TestClient(app, base_url="http://localhost"), state, gateway


def control_body(client: TestClient, name: str = "work") -> TestClient:
    response = client.get(f"/instances/{name}/control")
    assert response.status_code == 200
    return response


LIVE_MODE = ControlMode(available=True)


# Control-mode reasons (spec section 2): every drawer shows only the line.
MODE_CASES = [
    (
        "control off",
        Settings(poll_interval=5.0, control=False),
        "Controls unavailable: control is off (AGENT_DASHBOARD_CONTROL=off, read-only mode)",
    ),
    (
        "non-loopback bind",
        Settings(
            poll_interval=5.0,
            host="192.168.1.5",
            allow_non_loopback=True,
        ),
        "Controls unavailable: the dashboard is bound to a non-loopback address (192.168.1.5)",
    ),
]


@pytest.mark.parametrize("label,settings,line", MODE_CASES)
def test_control_mode_unavailable_in_drawer(label, settings, line):
    client, _, _ = make_control_client(
        {"work": load_snapshot("snapshot-attempt-in-progress.json")},
        settings,
    )
    # The drawer carries the console slot; the slot's body (fetched on load
    # and on every filtered sse:fleet event) renders the line.
    drawer = client.get("/instances/work").text
    assert f'id="control-console-{control_slug("work")}"' in drawer
    assert control_body(client).text.count(line) >= 1


def test_control_mode_host_guard_disabled_in_drawer(monkeypatch):
    monkeypatch.setenv("AGENT_DASHBOARD_DISABLE_HOST_GUARD", "1")
    client, _, _ = make_control_client(
        {"work": load_snapshot("snapshot-attempt-in-progress.json")},
        Settings(poll_interval=5.0),
    )
    line = (
        "Controls unavailable: the Host guard is disabled "
        "(AGENT_DASHBOARD_DISABLE_HOST_GUARD=1)"
    )
    assert f'id="control-console-{control_slug("work")}"' in (
        client.get("/instances/work").text
    )
    assert line in control_body(client).text


def test_control_mode_unavailable_in_every_fixture_drawer():
    settings = Settings(poll_interval=5.0, control=False)
    line = "Controls unavailable: control is off"
    for fixture in (
        "snapshot-attempt-in-progress.json",
        "snapshot-soft-threshold-handoff.json",
        "snapshot-recovery-hold.json",
        "snapshot-idle-with-last-finished.json",
    ):
        client, _, _ = make_control_client({f"w-{fixture}": load_snapshot(fixture)}, settings)
        name = f"w-{fixture}"
        assert f'id="control-console-{control_slug(name)}"' in (
            client.get(f"/instances/{name}").text
        )
        assert line in client.get(f"/instances/{name}/control").text


# Per-instance "Controls unavailable" reasons (spec section 5.1).
def test_reach_not_live_lines():
    from agent_dashboard.collector import (
        AGENT_UNREACHABLE,
        SNAPSHOT_FAILED,
        UNSUPPORTED_OPERATION,
    )

    now = time.time()
    state = FleetState()
    gateway = FakeGateway(
        [
            ManagedInstance("up", "id-up", "running"),
            ManagedInstance("stale", "id-stale", "running"),
            ManagedInstance("down", "id-down", "running"),
            ManagedInstance("old", "id-old", "running"),
            ManagedInstance("off", "id-off", "exited"),
        ]
    )
    gateway.set_snapshot(
        "id-up", fresh_snapshot(load_snapshot("snapshot-attempt-in-progress.json"), now)
    )
    for container_id, code in (
        ("id-stale", SNAPSHOT_FAILED),
        ("id-down", AGENT_UNREACHABLE),
        ("id-old", UNSUPPORTED_OPERATION),
    ):
        gateway.set_error_code(container_id, code, "boom")
    asyncio.run(run_cycle(state, gateway, max_concurrent_execs=4, now=now))
    app = create_app(
        state=state,
        settings=Settings(poll_interval=5.0),
        gateway=gateway,
        start_poller=False,
    )
    client = TestClient(app, base_url="http://localhost")
    assert "Controls unavailable: the snapshot is stale (newer polls failed)" in (
        client.get("/instances/stale/control").text
    )
    assert "Controls unavailable: the agent is unreachable" in (
        client.get("/instances/down/control").text
    )
    assert "Controls unavailable: this agent&#39;s telemetry is not supported" in (
        client.get("/instances/old/control").text
    )
    assert "Controls unavailable: the container is not running" in (
        client.get("/instances/off/control").text
    )


@pytest.mark.parametrize(
    "control,line",
    [
        (None, "Controls unavailable: the snapshot has no control section"),
        (
            "bogus",
            "Controls unavailable: the snapshot&#39;s control section is unreadable",
        ),
        (
            ["not", "a", "dict"],
            "Controls unavailable: the snapshot&#39;s control section is unreadable",
        ),
    ],
)
def test_control_section_missing_or_unreadable(control, line):
    snap = fresh_snapshot(load_snapshot("snapshot-attempt-in-progress.json"), time.time())
    if control is None and "control" in snap:
        # Null and missing read the same; exercise the null spelling here.
        snap["control"] = None
    else:
        snap["control"] = control
    client, _, _ = make_control_client({"work": snap})
    assert line in control_body(client).text


def test_control_section_missing_key():
    snap = fresh_snapshot(load_snapshot("snapshot-attempt-in-progress.json"), time.time())
    del snap["control"]
    client, _, _ = make_control_client({"work": snap})
    assert (
        "Controls unavailable: the snapshot has no control section"
        in control_body(client).text
    )


def test_unknown_instance_is_404_fragment():
    client, _, _ = make_control_client(
        {"work": load_snapshot("snapshot-attempt-in-progress.json")}
    )
    response = client.get("/instances/nope/control")
    assert response.status_code == 404
    assert "instance not found" in response.text


# Per-action rules (spec section 5.2), table-driven from the contract
# fixtures plus small synthetic variations. Each row names the action, the
# control section it renders from, and the expected state: enabled with an
# optional note, or disabled with its reason.
def idle_control() -> dict:
    return copy.deepcopy(
        load_snapshot("snapshot-idle-with-last-finished.json")["control"]
    )


def with_control(base: dict, **overrides) -> dict:
    control = copy.deepcopy(base)
    control.update(overrides)
    return control


ACTION_CASES = [
    # Stop.
    (
        "stop enabled while running",
        idle_control(),
        "stop",
        True,
        None,
        None,
    ),
    (
        "stop disabled when stopped",
        with_control(idle_control(), intake="stopped"),
        "stop",
        False,
        "already stopped",
        None,
    ),
    (
        "stop disabled when a stop is pending",
        with_control(
            idle_control(), pending_command={"kind": "stop", "request_id": "dash-1"}
        ),
        "stop",
        False,
        "a stop is pending",
        None,
    ),
    # Stop after N.
    (
        "stop_after enabled while running",
        idle_control(),
        "stop_after",
        True,
        None,
        None,
    ),
    (
        "stop_after disabled when stopped",
        with_control(idle_control(), intake="stopped"),
        "stop_after",
        False,
        "already stopped",
        None,
    ),
    (
        "stop_after disabled while a begun handoff runs",
        copy.deepcopy(
            load_snapshot("snapshot-soft-threshold-handoff.json")["control"]
        ),
        "stop_after",
        False,
        "a handoff is in progress",
        None,
    ),
    (
        "stop_after notes the plan it replaces",
        with_control(idle_control(), stop_plan={"remaining": 3}),
        "stop_after",
        True,
        None,
        "replaces the current plan (3 left)",
    ),
    # Resume.
    (
        "resume disabled when already running",
        idle_control(),
        "resume",
        False,
        "already running",
        None,
    ),
    (
        "resume enabled while stopping",
        with_control(
            idle_control(),
            intake="stopping",
            active_attempt={"issue_number": 1},
        ),
        "resume",
        True,
        None,
        None,
    ),
    (
        "resume enabled with a stop plan",
        with_control(idle_control(), stop_plan={"remaining": 2}),
        "resume",
        True,
        None,
        None,
    ),
    (
        "resume enabled with an accepted not-begun handoff",
        with_control(
            idle_control(),
            handoff={"acknowledgement": "accepted", "begun": False},
        ),
        "resume",
        True,
        None,
        None,
    ),
    (
        "resume disabled while recovering",
        with_control(idle_control(), recovering=True),
        "resume",
        False,
        "the instance is recovering",
        None,
    ),
    (
        "resume disabled under a recovery hold",
        copy.deepcopy(load_snapshot("snapshot-recovery-hold.json")["control"]),
        "resume",
        False,
        "recovery hold: resolve it with agentctl recovery",
        None,
    ),
    # Handoff now.
    (
        "handoff disabled with no active attempt",
        idle_control(),
        "handoff",
        False,
        "no active attempt",
        None,
    ),
    (
        "handoff enabled while an attempt runs",
        copy.deepcopy(
            load_snapshot("snapshot-attempt-in-progress.json")["control"]
        ),
        "handoff",
        True,
        None,
        None,
    ),
    (
        "handoff disabled while recovering",
        with_control(
            copy.deepcopy(
                load_snapshot("snapshot-attempt-in-progress.json")["control"]
            ),
            recovering=True,
        ),
        "handoff",
        False,
        "the instance is recovering",
        None,
    ),
    (
        "handoff disabled under a recovery hold",
        with_control(
            copy.deepcopy(
                load_snapshot("snapshot-attempt-in-progress.json")["control"]
            ),
            recovery={"issue_number": 1},
        ),
        "handoff",
        False,
        "recovery hold",
        None,
    ),
    (
        "handoff disabled while accepted",
        copy.deepcopy(
            load_snapshot("snapshot-soft-threshold-handoff.json")["control"]
        ),
        "handoff",
        False,
        "a handoff is in progress",
        None,
    ),
    # Next issue.
    (
        "next_issue never disabled",
        idle_control(),
        "next_issue",
        True,
        None,
        None,
    ),
    (
        "next_issue notes the queued issue",
        with_control(
            idle_control(), pending_next_issue={"issue_number": 42}
        ),
        "next_issue",
        True,
        None,
        "currently #42 is next",
    ),
]


@pytest.mark.parametrize(
    "label,control,key,enabled,reason,note", ACTION_CASES
)
def test_action_rules(label, control, key, enabled, reason, note):
    availability = control_availability(
        reach="live", control=control, mode=LIVE_MODE
    )
    assert availability.unavailable is None
    action = next(a for a in availability.actions if a.key == key)
    assert action.enabled is enabled, label
    assert action.reason == reason, label
    assert action.note == note, label


MISSING_FIELD_CASES = [
    ("intake", "stop"),
    ("pending_command", "stop"),
    ("intake", "stop_after"),
    ("handoff", "stop_after"),
    ("stop_plan", "stop_after"),
    ("intake", "resume"),
    ("pending_command", "resume"),
    ("stop_plan", "resume"),
    ("handoff", "resume"),
    ("recovering", "resume"),
    ("recovery", "resume"),
    ("active_attempt", "handoff"),
    ("recovering", "handoff"),
    ("recovery", "handoff"),
    ("handoff", "handoff"),
    ("pending_next_issue", "next_issue"),
]


@pytest.mark.parametrize("missing,key", MISSING_FIELD_CASES)
def test_missing_field_leaves_action_enabled(missing, key):
    control = idle_control()
    # The idle fixture disables stop_after's note path and resume/handoff by
    # value; dropping the key must still read as enabled, so start from a
    # control section where every action is enabled.
    control.update(
        {
            "intake": "stopping",
            "recovering": False,
            "active_attempt": {"issue_number": 7},
            "pending_command": None,
            "pending_next_issue": None,
            "stop_plan": None,
            "recovery": None,
            "handoff": None,
        }
    )
    del control[missing]
    availability = control_availability(
        reach="live", control=control, mode=LIVE_MODE
    )
    action = next(a for a in availability.actions if a.key == key)
    assert action.enabled, f"dropping {missing!r} disabled {key!r}"


# Composer fragments through TestClient.
def test_composer_preselects_first_enabled_with_preview():
    client, _, _ = make_control_client(
        {"work": load_snapshot("snapshot-attempt-in-progress.json")}
    )
    body = control_body(client).text
    # Resume reads "already running"; Stop is the first enabled action.
    assert "already running" in body
    assert 'value="stop" checked' in body
    assert "agentctl <span data-preview>stop</span>" in body
    assert 'type="submit"' in body
    assert "Review…" in body


def test_composer_shows_reasons_and_notes():
    client, _, _ = make_control_client(
        {"work": load_snapshot("snapshot-recovery-hold.json")}
    )
    body = control_body(client).text
    assert "recovery hold: resolve it with agentctl recovery" in body
    # The fixture has no active attempt, so handoff reads that first (spec
    # table order), not the hold.
    assert "no active attempt" in body
    # Stop stays enabled and preselected; Review stays enabled.
    assert 'value="stop" checked' in body
    assert "disabled" in body  # the two held actions


def test_composer_handoff_in_progress_reasons():
    client, _, _ = make_control_client(
        {"work": load_snapshot("snapshot-soft-threshold-handoff.json")}
    )
    body = control_body(client).text
    assert body.count("a handoff is in progress") == 2
    assert 'value="stop" checked' in body


def test_composer_stop_plan_and_next_issue_notes():
    snap = load_snapshot("snapshot-attempt-in-progress.json")
    snap["control"]["stop_plan"] = {"remaining": 2}
    snap["control"]["pending_next_issue"] = {"issue_number": 99}
    client, _, _ = make_control_client({"work": snap})
    body = control_body(client).text
    assert "replaces the current plan (2 left)" in body
    assert "currently #99 is next" in body


def test_composer_shows_control_stale_note():
    client, _, _ = make_control_client(
        {"work": load_snapshot("snapshot-attempt-in-progress.json")},
        backdate_control=90.0,
    )
    body = control_body(client).text
    assert "may be busy" in body
    assert "acceptance can take up to 10 s" in body


def test_review_disabled_when_nothing_enabled():
    from pathlib import Path

    from jinja2 import Environment, FileSystemLoader

    from agent_dashboard.control import ControlAction

    templates_dir = Path(__file__).parent.parent / "src" / "agent_dashboard" / "templates"
    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        autoescape=True,
    )
    template = env.get_template("_control.html")
    actions = tuple(
        ControlAction(
            key=key,
            label=label,
            enabled=False,
            reason="held",
            checked=False,
            command="",
        )
        for key, label in (
            ("stop", "Stop"),
            ("stop_after", "Stop after N"),
            ("resume", "Resume"),
            ("handoff", "Handoff now"),
            ("next_issue", "Next issue"),
        )
    )
    body = template.render(
        av=ControlAvailability(unavailable=None, actions=actions, stale_note=None),
        review_url="/instances/work/control/review",
        ACTION_COMMANDS=dict(ACTION_COMMANDS),
        error=None,
    )
    assert "held" in body
    assert "disabled" in body
    assert "(choose an action)" in body


def test_drawer_keeps_console_across_sse_refresh():
    client, _, _ = make_control_client(
        {"work": load_snapshot("snapshot-attempt-in-progress.json")}
    )
    body = client.get("/instances/work").text
    slug = control_slug("work")
    assert f'id="control-console-{slug}"' in body
    assert "hx-preserve" in body
    assert 'hx-get="/instances/work/control"' in body
    assert "hx-trigger=" in body and "sse:fleet" in body
    # The composer holds while a confirmation is open or focus is inside.
    assert "!this.querySelector('.ctl-confirm')" in body
    assert "!this.contains(document.activeElement)" in body
    # The console sits directly under the drawer head.
    assert body.index("Control console") < body.index("Current attempt")


def test_composer_lists_actions_in_spec_order():
    import re

    client, _, _ = make_control_client(
        {"work": load_snapshot("snapshot-attempt-in-progress.json")}
    )
    body = control_body(client).text
    values = re.findall(r'<input type="radio" name="action" value="([^"]+)"', body)
    assert values == list(ACTION_ORDER)


def test_console_body_serves_from_drawer_slot():
    client, _, _ = make_control_client(
        {"work": load_snapshot("snapshot-attempt-in-progress.json")}
    )
    first = control_body(client).text
    second = control_body(client).text
    assert first == second
    assert "ctl-composer" in first


# Startup: an invalid --control value is an error, never a silent default.
def test_invalid_control_value_is_startup_error():
    with pytest.raises(ValueError):
        load_settings(["--control", "maybe"])


def test_invalid_control_value_exits_2(monkeypatch):
    monkeypatch.setattr(
        "sys.argv", ["agent-dashboard", "--control", "maybe"]
    )
    with pytest.raises(SystemExit) as exc:
        cli_module.main(["--control", "maybe"])
    assert exc.value.code == 2
