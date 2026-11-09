"""Contract Attempt history: paged ``attempts list`` reader (contract major 1).

A running contract instance lists its Attempts through
``attempts list --limit 100 [--before <cursor>]`` (newest first). The
drawer statistics card and the Fleet overview share this reader: outcome
charts group by ``outcome.label`` (display only) and contract Attempts
count as "no cost data" in cost figures, never as zero cost. Malformed or
unknown replies render a clear message, never a 500. Vocabulary: Attempt
log, Managed instance, Reach.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from contract_replay import ReplayProcessRunner
from fake_gateway import FakeGateway
from fastapi.testclient import TestClient

from agent_dashboard.app import create_app
from agent_dashboard.collector import run_cycle
from agent_dashboard.config import Settings
from agent_dashboard.contract_history import (
    HISTORY_CAP_DEFAULT,
    HISTORY_PAGE_LIMIT,
    NOT_REPORTED,
    clear_known_attempt_ids,
    collect_contract_history,
    contract_legend,
    counts_by_label,
    known_attempt_ids,
    parse_contract_attempts_page,
    remember_attempt_ids,
)
from agent_dashboard.fleet import FleetState
from agent_dashboard.gateway import (
    AGENTCTL_PATH,
    CONTRACT_PATH,
    ControlExec,
    DockerGateway,
    GatewayError,
    ManagedInstance,
    argv_contract_attempts_list,
)
from agent_dashboard.range_stats import RangeSpec


@pytest.fixture(autouse=True)
def _forget_ids():
    clear_known_attempt_ids()
    yield
    clear_known_attempt_ids()


def _package_fixture(name: str) -> dict:
    """One pinned consumer fixture from the agent-operator-contract package."""
    from agent_operator_contract import documents

    return documents.load_fixture(name)


def _reply(name: str) -> tuple[bytes, int]:
    fixture = _package_fixture(name)
    return fixture["stdout"].encode("utf-8"), fixture["exit_code"]


def _iso(days_ago: float, now: float | None = None) -> str:
    at = time.time() if now is None else now
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at - days_ago * 86400))


def _attempt(
    attempt_id: str,
    issue: int,
    started: str,
    finished: str | None = None,
    *,
    label: str = "Delivered",
    lifecycle: str = "finished",
    attention: bool = False,
    sealed: bool = True,
    title: str | None = None,
    delivery: int | None = 78,
) -> dict:
    entry: dict = {
        "attempt_id": attempt_id,
        "issue": {"number": issue, "title": title or f"Issue {issue}"},
        "started_at": started,
        "finished_at": finished,
        "lifecycle_state": lifecycle,
        "outcome": {"label": label, "native": label.lower(), "reason": None},
        "attention_required": attention,
        "sealed": sealed,
    }
    if sealed:
        entry["sealed_at"] = finished or started
    if delivery is not None:
        entry["delivery"] = {
            "pull_request": {
                "number": delivery,
                "url": f"https://example.invalid/repo/pull/{delivery}",
            }
        }
    else:
        entry["delivery"] = None
    return entry


def _envelope(attempts: list[dict], next_cursor: str | None = None) -> dict:
    return {
        "contract": "1.0",
        "ok": True,
        "result": {"attempts": attempts, "next_cursor": next_cursor},
    }


# --- argv pinning ----------------------------------------------------------


def test_attempts_list_argv_pinned_without_cursor():
    assert argv_contract_attempts_list() == [
        "attempts",
        "list",
        "--limit",
        "100",
    ]
    assert argv_contract_attempts_list(100, None) == [
        "attempts",
        "list",
        "--limit",
        "100",
    ]


def test_attempts_list_argv_pinned_with_cursor():
    assert argv_contract_attempts_list(100, "eyJvZmZzZXQiOjF9") == [
        "attempts",
        "list",
        "--limit",
        "100",
        "--before",
        "eyJvZmZzZXQiOjF9",
    ]


def test_attempts_list_argv_rejects_bad_values():
    for bad in (0, -1, True, "100", 1.5, None):
        with pytest.raises(ValueError):
            argv_contract_attempts_list(bad)
    for bad in ("", "has space", "has\ttab", 123, b"bytes"):
        with pytest.raises(ValueError):
            argv_contract_attempts_list(100, bad)


def test_attempts_list_runs_online_with_15s_default_timeout():
    from agent_operator_contract import client as contract_client

    assert contract_client.timeout_for("attempts-list", None) == 15.0
    assert HISTORY_PAGE_LIMIT == 100

    seen = {}

    class CapturingRunner:
        async def run(self, argv, *, timeout, timeout_error):
            seen["argv"] = list(argv)
            seen["timeout"] = timeout
            return ControlExec(stdout=b"{}", stderr=b"", returncode=0)

    gateway = DockerGateway(process_runner=CapturingRunner())
    asyncio.run(gateway.contract_attempts_list("abc123", "someone"))
    assert seen["timeout"] == 15.0
    assert seen["argv"] == [
        "docker",
        "exec",
        "--user",
        "someone",
        "abc123",
        AGENTCTL_PATH,
        "--contract",
        "1",
        "attempts",
        "list",
        "--limit",
        "100",
    ]


def test_attempts_list_cursor_and_timeout_override_are_pinned():
    seen = {}

    class CapturingRunner:
        async def run(self, argv, *, timeout, timeout_error):
            seen["argv"] = list(argv)
            seen["timeout"] = timeout
            return ControlExec(stdout=b"{}", stderr=b"", returncode=0)

    gateway = DockerGateway(process_runner=CapturingRunner())
    asyncio.run(
        gateway.contract_attempts_list(
            "abc123", "someone", before="eyJvZmZzZXQiOjF9", timeout=2.5
        )
    )
    assert seen["timeout"] == 2.5
    assert seen["argv"][-2:] == ["--before", "eyJvZmZzZXQiOjF9"]
    assert seen["argv"][:9] == [
        "docker",
        "exec",
        "--user",
        "someone",
        "abc123",
        AGENTCTL_PATH,
        "--contract",
        "1",
        "attempts",
    ]


def test_attempts_list_replays_the_pinned_paged_fixture():
    runner = ReplayProcessRunner.from_package(["attempts-list-paged"])
    gateway = DockerGateway(process_runner=runner)
    exec_result = asyncio.run(
        gateway.contract_attempts_list("abc123", "agent", limit=1)
    )
    assert exec_result.returncode == 0
    assert b'"ok":true' in exec_result.stdout.replace(b" ", b"")
    assert runner.remaining == 0
    (call,) = runner.calls
    assert call == [
        "docker",
        "exec",
        "--user",
        "agent",
        "abc123",
        AGENTCTL_PATH,
        "--contract",
        "1",
        "attempts",
        "list",
        "--limit",
        "1",
    ]


def test_attempts_list_paged_fixture_pins_the_cursor_argv():
    fixture = _package_fixture("attempts-list-paged")
    assert fixture["argv"] == [
        "--contract",
        "1",
        "attempts",
        "list",
        "--limit",
        "1",
    ]
    assert fixture["exit_code"] == 0


# --- page parsing with replayed fixtures -----------------------------------


def test_pinned_list_fixture_parses_every_row_field():
    stdout, code = _reply("attempts-list")
    page = parse_contract_attempts_page(stdout, code)
    assert page.ok and page.error is None
    assert page.next_cursor is None
    assert page.unreadable == 0
    assert [a.attempt_id for a in page.attempts] == ["#42/1", "2026-11-06T08:01:10Z"]
    first, second = page.attempts
    assert first.issue == 42
    assert first.issue_title == "Add CSV export"
    assert first.started_at == "2026-11-06T08:50:00Z"
    assert first.finished_at == "2026-11-06T09:10:00Z"
    assert first.duration_text != "—"
    assert first.duration_value == pytest.approx(20 * 60.0)
    assert first.lifecycle_state == "finished"
    assert first.outcome_label == "Delivered"
    assert first.outcome_text == "Delivered"
    assert first.delivery_number == 78
    assert first.delivery_url == "https://github.com/example-owner/example-repo/pull/78"
    assert first.attention_required is False
    assert first.sealed is True
    assert first.sealed_text == "sealed"
    # Contract major 1 carries no per-Attempt usage: never zero cost.
    assert first.cost_text == NOT_REPORTED == "not reported"
    assert first.cost_value is None
    assert first.input_text == NOT_REPORTED
    assert first.has_usage is False
    assert second.delivery_number is None
    assert second.delivery_url is None
    assert second.sealed_text == "sealed"


def test_pinned_paged_fixture_carries_the_next_cursor():
    stdout, code = _reply("attempts-list-paged")
    page = parse_contract_attempts_page(stdout, code)
    assert page.ok and page.error is None
    assert page.next_cursor == "eyJvZmZzZXQiOjF9"
    (only,) = page.attempts
    assert only.attempt_id == "#42/1"
    assert only.issue == 42


def test_unknown_entries_are_skipped_and_counted():
    now = time.time()
    good = _attempt("good-1", 42, _iso(1), _iso(1, time.time()), delivery=None)
    payload = _envelope(
        [
            good,
            {"attempt_id": "", "issue": {"number": 7}},  # empty ID: unreadable
            {"issue": {"number": 7}},  # missing ID: unreadable
            {"attempt_id": "bad-issue", "issue": {"number": "x"}},  # bad issue
            {"attempt_id": "sealed-gap", "issue": {"number": 8},  # sealed, no stamp
             "started_at": _iso(2), "finished_at": _iso(2),
             "lifecycle_state": "finished",
             "outcome": {"label": "Delivered"},
             "attention_required": False, "sealed": True},
            "not-a-dict",  # wrong shape: unreadable
        ]
    )
    page = parse_contract_attempts_page(json.dumps(payload).encode(), 0, now=now)
    assert page.ok
    assert [a.attempt_id for a in page.attempts] == ["good-1"]
    assert page.unreadable == 5


def test_unknown_fields_are_ignored():
    now = time.time()
    entry = _attempt("x-1", 42, _iso(1), _iso(1))
    entry["future_field"] = {"nested": [1, 2, 3]}
    entry["outcome"]["extra"] = "ignored"
    page = parse_contract_attempts_page(
        json.dumps(_envelope([entry])).encode(), 0, now=now
    )
    assert page.ok
    (only,) = page.attempts
    assert only.attempt_id == "x-1"
    assert only.outcome_label == "Delivered"


def test_malformed_replies_are_a_clear_message_never_an_error():
    for name in ("malformed-empty", "malformed-non-json", "malformed-truncated"):
        stdout, code = _reply(name)
        page = parse_contract_attempts_page(stdout, code)
        assert not page.ok, name
        assert page.error, name
        assert page.attempts == ()


def test_unrecognised_shapes_are_a_clear_message():
    now = time.time()
    for stdout, code in (
        (b"{}", 0),
        (b"[]", 0),
        (json.dumps({"contract": "1.0", "ok": True, "result": {}}).encode(), 0),
        (
            json.dumps(
                {"contract": "1.0", "ok": True, "result": {"attempts": {}}}
            ).encode(),
            0,
        ),
        (
            json.dumps(
                {
                    "contract": "1.0",
                    "ok": True,
                    "result": {"attempts": [], "next_cursor": ""},
                }
            ).encode(),
            0,
        ),
        (b"not json at all", 2),
    ):
        page = parse_contract_attempts_page(stdout, code, now=now)
        assert not page.ok
        assert page.error


def test_error_envelopes_name_their_cause():
    mismatch = _package_fixture("contract-mismatch")
    page = parse_contract_attempts_page(
        mismatch["stdout"].encode("utf-8"), mismatch["exit_code"]
    )
    assert not page.ok
    assert "mismatch" in page.error.lower()
    unsupported = _package_fixture("unsupported")
    page = parse_contract_attempts_page(
        unsupported["stdout"].encode("utf-8"), unsupported["exit_code"]
    )
    assert not page.ok
    assert page.error


# --- paged collection ------------------------------------------------------


def _collect(gateway, spec=None, **kwargs):
    return asyncio.run(
        collect_contract_history(
            gateway,
            "abc123",
            "agent",
            RangeSpec("all") if spec is None else spec,
            **kwargs,
        )
    )


def test_collect_pages_newest_first_through_cursors():
    gateway = FakeGateway()
    first = _attempt("new-1", 42, _iso(1), _iso(1), delivery=None)
    second = _attempt("old-1", 41, _iso(2), _iso(2), delivery=None)
    gateway.set_contract_attempts(
        "abc123",
        {None: _envelope([first], "c1"), "c1": _envelope([second], None)},
    )
    result = _collect(gateway)
    assert result.ok and result.error is None
    assert [a.attempt_id for a in result.summaries] == ["new-1", "old-1"]
    assert result.total == 2
    assert result.unreadable == 0
    assert result.capped is False
    assert gateway.contract_attempts_calls == [
        ("abc123", "agent", 100, None),
        ("abc123", "agent", 100, "c1"),
    ]


def test_collect_stops_once_an_attempt_starts_before_the_range():
    gateway = FakeGateway()
    recent = _attempt("new-1", 42, _iso(1), _iso(1))
    old = _attempt("old-1", 41, _iso(40), _iso(40))
    gateway.set_contract_attempts(
        "abc123",
        {
            None: _envelope([recent], "c1"),
            # A next cursor is present, but the range cutoff stops the read.
            "c1": _envelope([old], "c2"),
        },
    )
    result = _collect(gateway, spec=RangeSpec("7d"))
    assert result.ok
    assert [a.attempt_id for a in result.summaries] == ["new-1"]
    assert result.total == 2
    assert [call[3] for call in gateway.contract_attempts_calls] == [None, "c1"]


def test_collect_all_stops_at_the_cap_with_a_visible_note():
    assert HISTORY_CAP_DEFAULT == 2000
    gateway = FakeGateway()
    first = _attempt("a-1", 42, _iso(1), _iso(1))
    second = _attempt("a-2", 41, _iso(2), _iso(2))
    third = _attempt("a-3", 40, _iso(3), _iso(3))
    gateway.set_contract_attempts(
        "abc123",
        {None: _envelope([first, second], "c1"), "c1": _envelope([third], None)},
    )
    result = _collect(gateway, cap=2)
    assert result.ok
    assert result.capped is True
    assert result.cap == 2
    assert len(result.summaries) == 2
    # Only the first page was needed: the cap stops the read.
    assert [call[3] for call in gateway.contract_attempts_calls] == [None]


def test_collect_cap_spans_pages_until_reached():
    gateway = FakeGateway()
    made = [_attempt(f"a-{n}", n, _iso(n), _iso(n)) for n in range(1, 5)]
    gateway.set_contract_attempts(
        "abc123",
        {None: _envelope(made[:2], "c1"), "c1": _envelope(made[2:], None)},
    )
    result = _collect(gateway, cap=3)
    assert result.ok
    assert result.capped is True
    assert [a.attempt_id for a in result.summaries] == ["a-1", "a-2", "a-3"]


def test_collect_rejects_a_bad_cap_with_the_default():
    gateway = FakeGateway()
    gateway.set_contract_attempts("abc123", {None: _envelope([], None)})
    for bad in (0, -3, True, "many"):
        result = _collect(gateway, cap=bad)
        assert result.ok
        assert result.cap == HISTORY_CAP_DEFAULT


def test_collect_remembers_every_attempt_id_per_instance():
    gateway = FakeGateway()
    gateway.set_contract_attempts(
        "abc123",
        {
            None: _envelope(
                [
                    _attempt("#42/1", 42, _iso(1), _iso(1)),
                    _attempt("legacy-id", 41, _iso(2), _iso(2)),
                ],
                None,
            )
        },
    )
    result = asyncio.run(
        collect_contract_history(
            gateway,
            "abc123",
            "agent",
            RangeSpec("all"),
            instance_name="web-1",
        )
    )
    assert result.ok
    assert known_attempt_ids("web-1") == frozenset({"#42/1", "legacy-id"})
    assert known_attempt_ids("other") == frozenset()
    remember_attempt_ids("web-1", ["#42/1"])
    assert known_attempt_ids("web-1") == frozenset({"#42/1", "legacy-id"})


def test_collect_malformed_page_is_an_error_not_an_exception():
    gateway = FakeGateway()
    malformed = _package_fixture("malformed-truncated")
    gateway.set_contract_attempts(
        "abc123", {None: (malformed["stdout"].encode("utf-8"), malformed["exit_code"])}
    )
    result = _collect(gateway)
    assert not result.ok
    assert result.error


def test_collect_gateway_failure_is_an_error_not_an_exception():
    gateway = FakeGateway()
    gateway.set_contract_attempts("abc123", {None: GatewayError("boom")})
    result = _collect(gateway)
    assert not result.ok
    assert "boom" in result.error


def test_collect_gone_volume_names_the_logs():
    gateway = FakeGateway()
    gateway.set_contract_attempts("abc123", {None: FileNotFoundError("gone")})
    result = _collect(gateway)
    assert not result.ok
    assert "unavailable" in result.error


# --- shared reader: label grouping, no-cost-data ----------------------------


def test_outcome_charts_group_by_label_display_only():
    now = time.time()
    rows = (
        _attempt("a-1", 1, _iso(1), _iso(1), label="Delivered"),
        _attempt("a-2", 2, _iso(2), _iso(2), label="Delivered"),
        _attempt("a-3", 3, _iso(3), _iso(3), label="No Changes"),
    )
    page = parse_contract_attempts_page(
        json.dumps(_envelope([dict(r) for r in rows])).encode(), 0, now=now
    )
    counts, labels = counts_by_label(page.attempts)
    assert counts == {"delivered": 2, "no-changes": 1}
    assert labels == {"delivered": "Delivered", "no-changes": "No Changes"}
    legend = contract_legend(page.attempts)
    assert [entry.label for entry in legend] == ["Delivered", "No Changes"]


def test_contract_summaries_never_count_as_zero_cost():
    now = time.time()
    page = parse_contract_attempts_page(
        json.dumps(
            _envelope([_attempt("a-1", 1, _iso(1), _iso(1))])
        ).encode(),
        0,
        now=now,
    )
    (summary,) = page.attempts
    assert summary.cost_value is None
    assert summary.cost_text == "not reported"
    assert summary.input_text == "not reported"
    # Cost figures aggregate only real numbers: no value means missing,
    # never a zero that would drag totals down.
    assert sum(1 for s in page.attempts if s.cost_value is not None) == 0
    assert sum(1 for s in page.attempts if s.cost_value is None) == 1


# --- web: history page -------------------------------------------------------


def _status_result(name: str = "status-idle") -> dict:
    return json.loads(_package_fixture(name)["stdout"])["result"]


def make_contract_client(pages, names=("web-1",), **settings_kwargs):
    """A client with running contract instances and scripted ``attempts list`` pages."""
    state = FleetState()
    instances = [
        ManagedInstance(
            name,
            f"abc{name}",
            "running",
            path=CONTRACT_PATH,
            instance=name,
            exec_user="agent",
        )
        for name in names
    ]
    gateway = FakeGateway(instances)
    for name in names:
        gateway.set_contract_snapshot(f"abc{name}", _status_result())
    asyncio.run(run_cycle(state, gateway, now=1000.0))
    for container_id, scripted in pages.items():
        gateway.set_contract_attempts(container_id, scripted)
    kwargs = {"poll_interval": 5.0}
    kwargs.update(settings_kwargs)
    app = create_app(
        state=state, settings=Settings(**kwargs), gateway=gateway, start_poller=False
    )
    return TestClient(app, base_url="http://localhost"), state, gateway


def test_history_page_lists_contract_rows_with_every_field():
    client, _, _ = make_contract_client(
        {
            "abcweb-1": {
                None: _envelope(
                    [
                        _attempt(
                            "#42/1",
                            42,
                            "2026-11-06T08:50:00Z",
                            "2026-11-06T09:10:00Z",
                            label="Delivered",
                            attention=True,
                            title="Add CSV export",
                        ),
                        _attempt(
                            "plain-1",
                            41,
                            "2026-11-05T08:01:10Z",
                            "2026-11-05T08:44:52Z",
                            label="No Changes",
                            sealed=False,
                            delivery=None,
                        ),
                    ],
                    None,
                )
            }
        }
    )
    response = client.get("/instances/web-1/history?preset=all")
    assert response.status_code == 200
    body = response.text
    for header in (
        "Issue",
        "Started",
        "Finished",
        "Duration",
        "Lifecycle",
        "Outcome",
        "Delivery",
        "Attention",
        "Sealed",
        "Cost",
        "Tokens",
    ):
        assert header in body
    # Newest first.
    tbody = body.split("<tbody>", 1)[1]
    assert tbody.index("#42") < tbody.index("#41")
    assert "Add CSV export" in body
    assert "2026-11-06T08:50:00Z" in body
    assert "2026-11-06T09:10:00Z" in body
    assert "finished" in body
    assert "Delivered" in body
    assert "No Changes" in body
    assert 'href="https://example.invalid/repo/pull/78"' in body
    assert ">yes<" in body
    assert "sealed" in body
    assert "open" in body
    # Contract major 1 carries no per-Attempt usage: never zero, never a value.
    assert "not reported" in body
    assert "$0.00" not in tbody


def test_history_page_pages_through_cursors_and_notes_the_cap():
    paged = _package_fixture("attempts-list-paged")
    cursor = json.loads(paged["stdout"])["result"]["next_cursor"]
    assert cursor
    client, _, gateway = make_contract_client(
        {
            "abcweb-1": {
                None: json.loads(paged["stdout"]),
                cursor: _envelope(
                    [
                        _attempt("tail-1", 40, _iso(9), _iso(9), delivery=None),
                        _attempt("tail-2", 39, _iso(10), _iso(10), delivery=None),
                    ],
                    None,
                ),
            }
        },
        contract_history_cap=2,
    )
    response = client.get("/instances/web-1/history?preset=all")
    assert response.status_code == 200
    body = response.text
    assert "#42" in body
    assert "#40" in body
    # ``All`` stops at the cap with a visible "showing newest N" note.
    assert "capped at 2" in body
    assert "Showing newest 2 of 2 attempts" in body
    assert gateway.contract_attempts_calls == [
        ("abcweb-1", "agent", 100, None),
        ("abcweb-1", "agent", 100, cursor),
    ]


def test_history_page_range_cut_stops_paging():
    client, _, gateway = make_contract_client(
        {
            "abcweb-1": {
                None: _envelope(
                    [_attempt("new-1", 42, _iso(1), _iso(1))], "c1"
                ),
                "c1": _envelope(
                    [_attempt("old-1", 41, _iso(40), _iso(40))], "c2"
                ),
            }
        }
    )
    body = client.get("/instances/web-1/history?preset=7d").text
    assert "#42" in body
    assert "#41" not in body
    assert [call[3] for call in gateway.contract_attempts_calls] == [None, "c1"]


def test_history_page_remembers_attempt_ids_for_the_attempt_ticket():
    client, _, _ = make_contract_client(
        {
            "abcweb-1": {
                None: _envelope([_attempt("#42/1", 42, _iso(1), _iso(1))], None)
            }
        }
    )
    assert client.get("/instances/web-1/history?preset=all").status_code == 200
    assert known_attempt_ids("web-1") == frozenset({"#42/1"})


def test_history_page_malformed_reply_is_a_message_not_500():
    malformed = _package_fixture("malformed-truncated")
    client, _, _ = make_contract_client(
        {
            "abcweb-1": {
                None: (malformed["stdout"].encode("utf-8"), malformed["exit_code"])
            }
        }
    )
    response = client.get("/instances/web-1/history?preset=all")
    assert response.status_code == 200
    assert "banner" in response.text
    assert "tbody" not in response.text


def test_history_page_stopped_contract_instance_is_a_message():
    state = FleetState()
    gateway = FakeGateway(
        [
            ManagedInstance(
                "down-1",
                "abcdown",
                "exited",
                path=CONTRACT_PATH,
                instance="down-1",
                exec_user="agent",
            )
        ]
    )
    gateway.set_contract_snapshot("abcdown", _status_result("offline-status-halted"))
    asyncio.run(run_cycle(state, gateway, now=1000.0))
    app = create_app(
        state=state,
        settings=Settings(poll_interval=5.0),
        gateway=gateway,
        start_poller=False,
    )
    client = TestClient(app, base_url="http://localhost")
    response = client.get("/instances/down-1/history?preset=all")
    assert response.status_code == 200
    assert "not running" in response.text


def test_history_page_unknown_instance_is_a_message():
    client, _, _ = make_contract_client({"abcweb-1": {}})
    response = client.get("/instances/missing/history?preset=all")
    assert response.status_code == 200
    assert "Unknown Managed instance" in response.text


# --- web: drawer statistics card --------------------------------------------


def test_stats_card_groups_by_label_with_no_cost_data():
    client, _, _ = make_contract_client(
        {
            "abcweb-1": {
                None: _envelope(
                    [
                        _attempt("a-1", 1, _iso(1), _iso(1), label="Delivered"),
                        _attempt("a-2", 2, _iso(2), _iso(2), label="Delivered"),
                        _attempt("a-3", 3, _iso(3), _iso(3), label="No Changes"),
                    ],
                    None,
                )
            }
        }
    )
    response = client.get("/instances/web-1/stats?preset=all")
    assert response.status_code == 200
    body = response.text
    assert "Delivered" in body
    assert "No Changes" in body
    assert "No cost data" in body
    assert "Not reported" in body
    assert "contract major 1 carries no per-Attempt usage" in body
    assert "$0.00" not in body


def test_stats_card_notes_the_cap():
    client, _, _ = make_contract_client(
        {
            "abcweb-1": {
                None: _envelope(
                    [
                        _attempt("a-1", 1, _iso(1), _iso(1)),
                        _attempt("a-2", 2, _iso(2), _iso(2)),
                    ],
                    "c1",
                )
            }
        },
        contract_history_cap=1,
    )
    body = client.get("/instances/web-1/stats?preset=all").text
    assert "capped at 1" in body


def test_stats_card_malformed_reply_is_a_hint_not_500():
    malformed = _package_fixture("malformed-empty")
    client, _, _ = make_contract_client(
        {
            "abcweb-1": {
                None: (malformed["stdout"].encode("utf-8"), malformed["exit_code"])
            }
        }
    )
    response = client.get("/instances/web-1/stats?preset=all")
    assert response.status_code == 200
    assert "hint" in response.text


def test_drawer_links_contract_history_and_stats():
    client, _, _ = make_contract_client({"abcweb-1": {}})
    body = client.get("/instances/web-1").text
    assert "/instances/web-1/history" in body
    assert "/instances/web-1/stats" in body


# --- web: fleet overview ------------------------------------------------------


def test_overview_includes_contract_instances_by_label():
    client, _, _ = make_contract_client(
        {
            "abcweb-1": {
                None: _envelope(
                    [
                        _attempt("a-1", 1, _iso(1), _iso(1), label="Delivered"),
                        _attempt("a-2", 2, _iso(2), _iso(2), label="No Changes"),
                    ],
                    None,
                )
            }
        }
    )
    response = client.get("/overview?preset=all")
    assert response.status_code == 200
    body = response.text
    assert "web-1" in body
    assert "Delivered" in body
    assert "No Changes" in body
    # Contract Attempts count as "no cost data" in cost figures.
    assert "No cost data" in body or "n/a" in body


def test_overview_lists_an_unreadable_contract_instance_below_the_charts():
    malformed = _package_fixture("malformed-non-json")
    client, _, _ = make_contract_client(
        {
            "abcweb-1": {
                None: (malformed["stdout"].encode("utf-8"), malformed["exit_code"])
            }
        }
    )
    response = client.get("/overview?preset=all")
    assert response.status_code == 200
    body = response.text
    assert "overview-errors" in body
    assert "web-1" in body


def test_overview_contract_and_legacy_rows_share_the_strip():
    state = FleetState()
    gateway = FakeGateway(
        [
            ManagedInstance(
                "web-1",
                "abcweb",
                "running",
                path=CONTRACT_PATH,
                instance="web-1",
                exec_user="agent",
            ),
            ManagedInstance("old-1", "id-old-1", "running"),
        ]
    )
    gateway.set_contract_snapshot("abcweb", _status_result())
    gateway.set_history(
        "id-old-1",
        {
            "layout": "attempt-logs/v1",
            "attempts": [
                {
                    "issue": 9,
                    "started": _iso(1).replace(":", "-"),
                    "attempt": {
                        "outcome": "complete",
                        "started_at": _iso(1),
                        "duration_seconds": 60,
                    },
                }
            ],
            "active_started_at": None,
            "unreadable": 0,
        },
    )
    asyncio.run(run_cycle(state, gateway, now=1000.0))
    gateway.set_contract_attempts(
        "abcweb", {None: _envelope([_attempt("a-1", 1, _iso(1), _iso(1))], None)}
    )
    app = create_app(
        state=state,
        settings=Settings(poll_interval=5.0),
        gateway=gateway,
        start_poller=False,
    )
    client = TestClient(app, base_url="http://localhost")
    response = client.get("/overview?preset=all")
    assert response.status_code == 200
    body = response.text
    assert "web-1" in body
    assert "old-1" in body
    assert "complete" in body
    assert "Delivered" in body


def test_overview_collect_merges_labels_and_cost_as_missing():
    from agent_dashboard.overview import (
        collect_overview,
        overview_chart_keys,
        overview_chart_labels,
        summarize_contract_instance,
    )

    now = time.time()
    page = parse_contract_attempts_page(
        json.dumps(
            _envelope(
                [
                    _attempt("a-1", 1, _iso(1, now), _iso(1, now), label="Delivered"),
                    _attempt("a-2", 2, _iso(2, now), _iso(2, now), label="No Changes"),
                ]
            )
        ).encode(),
        0,
        now=now,
    )
    inst = summarize_contract_instance(
        name="web-1",
        state="running",
        container_id="abc123",
        reach="working",
        rank=1,
        window="all",
        summaries=page.attempts,
        total=2,
        unreadable=0,
    )
    assert inst.is_contract is True
    assert inst.ok is True
    assert inst.cost_total is None
    assert inst.cost_missing == 2
    assert inst.cost_text == "No cost data"

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
    gateway.set_contract_attempts(
        "abc123",
        {
            None: _envelope(
                [
                    _attempt("a-1", 1, _iso(1), _iso(1), label="Delivered"),
                    _attempt("a-2", 2, _iso(2), _iso(2), label="No Changes"),
                ],
                None,
            )
        },
    )
    poll_state = FleetState()
    asyncio.run(run_cycle(poll_state, gateway, now=1000.0))
    rows, error = asyncio.run(poll_state.snapshot(now=1012.0))
    assert error is None
    result = asyncio.run(
        collect_overview(rows, gateway, spec=RangeSpec("all"), now=time.time())
    )
    assert result.readable == 1
    assert result.unreadable == 0
    assert result.contract_labels == {"delivered": "Delivered", "no-changes": "No Changes"}
    keys = overview_chart_keys(result)
    labels = overview_chart_labels(result)
    assert "complete" in keys  # legacy key space stays
    assert "delivered" in keys  # label slugs extend it
    assert labels["delivered"] == "Delivered"
    assert result.contract_summaries
    assert all(row.cost_total is None for row in result.rows)
