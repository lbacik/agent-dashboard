"""View parity: a contract instance looks like a legacy one (ADR 0017).

Switching an instance from the legacy path to the contract path changes
where the dashboard reads data, never what the operator sees: the rail's
budget bar, the fleet matrix figure cells, the cockpit's numbered modules
in their columns, the Current attempt layout and the Activity widget's Now
/ Last call panes. These tests render both paths side by side and fail
when one of them drops or renames a view element the other has.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import io
import json
import re
import tarfile
import time
from urllib.parse import quote

import pytest
from fake_gateway import FakeGateway
from fastapi.testclient import TestClient
from test_web import fresh_snapshot, load_snapshot

from agent_dashboard.app import create_app
from agent_dashboard.collector import run_cycle
from agent_dashboard.config import Settings
from agent_dashboard.contract_activity import (
    fetch_members,
    parse_export_members,
    read_archive_events,
)
from agent_dashboard.contract_status import read_contract_snapshot
from agent_dashboard.fleet import FleetState
from agent_dashboard.gateway import (
    CONTRACT_PATH,
    MAX_EXPORT_MEMBERS,
    ControlExec,
    ManagedInstance,
    argv_contract_attempts_export_members,
)

LEGACY_CID = "aaa111"
CONTRACT_CID = "bbb222"
ATTEMPT = "2026-11-06T08:50:00.000000Z"


def _fig(value, unit, basis="measured", reason=None) -> dict:
    fig = {"value": value, "unit": unit, "basis": basis}
    if reason is not None:
        fig["reason"] = reason
    return fig


def _contract_result(*, now: float, estimated: bool = False) -> dict:
    """A simple-coding-agent Status Snapshot mid-attempt (synthetic)."""
    from agent_operator_contract import documents

    result = json.loads(documents.load_fixture("status-active")["stdout"])["result"]
    result = copy.deepcopy(result)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    result["identity"]["backend"] = "simple-coding-agent"
    result["instance"]["observed_at"] = stamp
    result["intake"]["observed_at"] = stamp
    used_basis = "estimated" if estimated else "measured"
    current = result["current_attempt"]
    current["attempt_id"] = ATTEMPT
    current["observed_at"] = stamp
    current["native_phase"] = "model_running"
    current["budget"] = {
        "raw_tokens": _fig(3931510, "tokens", used_basis),
        "effective_tokens": _fig(3931510, "tokens", "estimated"),
        "enforced_tokens": "raw",
        "limits": [
            {
                "name": "raw_tokens",
                "used": _fig(3931510, "tokens", used_basis),
                "limit": _fig(12000000, "tokens", "configured"),
            },
            {
                "name": "cost_usd",
                "used": _fig(None, "usd", "unavailable", "no live cost figure"),
                "limit": _fig(5, "usd", "configured"),
            },
            {
                "name": "turns",
                "used": _fig(51, "count"),
                "limit": _fig(240, "count", "configured"),
            },
        ],
    }
    current["figures"] = {
        "cache_hit_rate": _fig(0.97, "ratio"),
        "context_used": _fig(100343, "tokens"),
        "context_peak": _fig(100400, "tokens", "estimated"),
        "context_window": _fig(400000, "tokens", "configured"),
        "continuations": _fig(0, "count", "configured"),
        "turns": _fig(51, "count"),
        "elapsed_seconds": _fig(883, "seconds"),
        "native.soft_threshold_tokens": _fig(7200000, "tokens", "configured"),
        "native.auto_compact_threshold": _fig(367000, "tokens", "configured"),
        "native.compactions": _fig(0, "count"),
        "native.continuation_max": _fig(6, "count", "configured"),
        "native.input_tokens": _fig(224781, "tokens"),
        "native.output_tokens": _fig(34623, "tokens"),
        "native.timeout_seconds": _fig(3600, "seconds", "configured"),
    }
    return result


def _make_client(*, now: float | None = None, contract: dict | None = None):
    """One legacy and one contract instance, both mid-attempt."""
    at = time.time() if now is None else now
    gateway = FakeGateway(
        [
            ManagedInstance("legacy-0", LEGACY_CID, "running"),
            ManagedInstance(
                "web-0",
                CONTRACT_CID,
                "running",
                path=CONTRACT_PATH,
                instance="web-0",
                exec_user="agent",
                backend="simple-coding-agent",
            ),
        ]
    )
    gateway.set_snapshot(
        LEGACY_CID,
        fresh_snapshot(load_snapshot("snapshot-attempt-in-progress.json"), at),
    )
    gateway.set_contract_snapshot(
        CONTRACT_CID, contract if contract is not None else _contract_result(now=at)
    )
    state = FleetState()
    asyncio.run(run_cycle(state, gateway, max_concurrent_execs=4, now=at))
    app = create_app(
        state=state,
        settings=Settings(poll_interval=5.0),
        gateway=gateway,
        start_poller=False,
    )
    return TestClient(app, base_url="http://localhost"), gateway


def _cockpit_layout(body: str) -> list[tuple[str, str]]:
    """``(column, module title)`` pairs in DOM order, e.g. ``("control", "01 // ...")``."""
    layout: list[tuple[str, str]] = []
    for chunk in body.split('data-col="')[1:]:
        column = chunk.partition('"')[0]
        for title in re.findall(
            r'class="(?:vc-module-bar|activity-label)">(\d\d // [^<]+)<', chunk
        ):
            layout.append((column, title))
    return layout


def _row(body: str, name: str) -> str:
    """One rail item's markup."""
    start = body.index(f'data-instance="{name}"')
    return body[start : body.index("</li>", start)]


def _matrix_row(body: str, name: str) -> str:
    table = body.partition('<table class="fleet">')[2]
    start = table.index(f'<div class="inst-name">{name}</div>')
    return table[start : table.index("</tr>", start)]


# --- parity: the cockpit, the rail and the matrix --------------------------------


def test_cockpit_modules_and_columns_match_the_legacy_cockpit():
    client, _ = _make_client()
    legacy = _cockpit_layout(client.get("/instances/legacy-0").text)
    contract = _cockpit_layout(client.get("/instances/web-0").text)
    assert legacy == [
        ("control", "01 // Control console"),
        ("control", "02 // Identity &amp; last attempt"),
        ("telemetry", "03 // Activity"),
        ("telemetry", "04 // Current attempt"),
        ("history", "05 // Attempt statistics"),
        ("history", "06 // Recent events"),
    ]
    assert contract == legacy


def test_current_attempt_rows_match_the_legacy_layout():
    client, _ = _make_client()
    rows = re.compile(r"<dt>([^<]+)</dt>")
    legacy_body = client.get("/instances/legacy-0").text
    contract_body = client.get("/instances/web-0").text

    def current_rows(body: str) -> list[str]:
        module = body.partition("04 // Current attempt")[2].partition("</section>")[0]
        return rows.findall(module)

    legacy_rows = current_rows(legacy_body)
    contract_rows = current_rows(contract_body)
    # Every legacy row the Status Snapshot can fill is there, in order.
    for label in (
        "Budget used",
        "Hard ceiling",
        "Soft threshold",
        "Soft crossed",
        "Turns",
        "Cache hit rate",
        "Main context",
        "Auto-compact threshold",
        "Compactions",
    ):
        assert label in legacy_rows, label
        assert label in contract_rows, label
    shared = [label for label in contract_rows if label in legacy_rows]
    assert shared == [label for label in legacy_rows if label in contract_rows]
    assert 'class="budget-bar' in contract_body.partition("04 // Current attempt")[2]


def test_rail_shows_the_budget_bar_for_both_paths():
    client, _ = _make_client()
    body = client.get("/").text
    for name in ("legacy-0", "web-0"):
        item = _row(body, name)
        assert "vc-mini-bar" in item, name
        assert "budget-fill" in item, name
        assert "budget-tick" in item, name
    contract = _row(body, "web-0")
    # 3,931,510 of 12,000,000 with the soft threshold at 7,200,000.
    assert "width: 32.8%" in contract
    assert "left: 60.0%" in contract


def test_matrix_figure_cells_render_for_both_paths():
    client, _ = _make_client()
    body = client.get("/").text
    contract = _matrix_row(body, "web-0")
    assert "3,931,510 / 12,000,000" in contract
    assert 'class="budget-bar' in contract
    assert "100,343" in contract  # main context
    assert ">51 <span" in contract  # turns
    assert "97%" in contract  # cache hit rate
    assert "/instances/web-0/history" in contract  # Logs, like a legacy row
    legacy = _matrix_row(body, "legacy-0")
    assert legacy.count('<div class="figure">') == contract.count('<div class="figure">')


def test_idle_contract_row_has_no_bar_and_no_figures():
    from agent_operator_contract import documents

    idle = json.loads(documents.load_fixture("status-idle")["stdout"])["result"]
    client, _ = _make_client(contract=idle)
    body = client.get("/").text
    assert "vc-mini-bar" not in _row(body, "web-0")
    assert 'class="budget-bar' not in _matrix_row(body, "web-0")


# --- the gauge view model ---------------------------------------------------------


def test_gauge_reads_the_enforced_budget_soft_tick_and_native_rows():
    detail = read_contract_snapshot(_contract_result(now=time.time()))
    assert detail is not None and detail.current is not None
    g = detail.current.gauge
    assert g.enforced == "raw"
    assert g.budget_has_bar and g.budget_has_tick
    assert g.budget_used_pct == 32.8
    assert g.budget_soft_pct == 60.0
    assert not g.budget_hatched
    assert g.soft_crossed is False
    assert g.budget_used.text == "3,931,510"  # no unit word in this layout
    assert g.hard_ceiling.text == "12,000,000"
    assert g.soft_threshold.text == "7,200,000"
    assert (g.turns.text, g.turns_max.text) == ("51", "240")
    assert (g.continuation_index.text, g.continuation_max.text) == ("0", "6")
    assert g.cache_hit.text == "97%"
    assert (g.ctx_main.text, g.ctx_window.text, g.ctx_peak.text) == (
        "100,343",
        "400,000",
        "100,400",
    )
    assert g.ctx_auto_compact.text == "367,000"
    assert g.compactions.text == "0"
    assert [lim.name for lim in g.other_limits] == ["cost_usd"]
    assert [row.label for row in g.tokens] == ["input_tokens", "output_tokens"]
    # Figures with a named row are not repeated under "Other figures".
    assert [row.name for row in g.other_figures] == [
        "elapsed_seconds",
        "native.timeout_seconds",
    ]
    assert (g.budget_text, g.budget_basis) == ("3,931,510 / 12,000,000", "measured")


def test_gauge_estimated_budget_is_hatched():
    detail = read_contract_snapshot(_contract_result(now=time.time(), estimated=True))
    assert detail is not None and detail.current is not None
    assert detail.current.gauge.budget_hatched


def test_gauge_effective_backend_without_native_names():
    """coding-agent: effective tokens enforced, no soft threshold, no turns."""
    from agent_operator_contract import documents

    result = json.loads(documents.load_fixture("status-active")["stdout"])["result"]
    detail = read_contract_snapshot(result)
    assert detail is not None and detail.current is not None
    g = detail.current.gauge
    assert g.enforced == "effective"
    assert g.budget_has_bar and not g.budget_has_tick
    assert g.budget_used.text == "412,530"
    assert g.soft_crossed is None
    assert g.soft_threshold.state == "unknown"
    assert g.turns.state == "na"
    assert {lim.name for lim in g.other_limits} == {"cost_usd", "wall_clock_seconds"}


def test_gauge_without_an_enforced_budget_has_no_bar():
    from agent_operator_contract import documents

    result = json.loads(documents.load_fixture("status-active")["stdout"])["result"]
    result["current_attempt"]["budget"]["enforced_tokens"] = "none"
    detail = read_contract_snapshot(result)
    assert detail is not None and detail.current is not None
    assert not detail.current.gauge.budget_has_bar
    assert detail.current.gauge.budget_text == "unavailable"


# --- Activity: Now / Last call from the Attempt Archive ---------------------------


def _events(*rows: tuple[str, str, str]) -> bytes:
    return b"".join(
        json.dumps(
            {"at": at, "attempt_id": ATTEMPT, "event": event, "level": "info", "text": text}
        ).encode()
        + b"\n"
        for at, event, text in rows
    )


ARCHIVE = {
    "events.jsonl": _events(
        ("2026-11-06T09:00:00Z", "claim_opened", "claimed issue #42"),
        ("2026-11-06T09:00:01Z", "native.model_response", "-> 0001_model_response.txt (chars=9)"),
        ("2026-11-06T09:00:02Z", "native.tool_call", "Bash [main] -> 0002_tool_call.json"),
        ("2026-11-06T09:01:20Z", "native.tool_result", "Bash [main] -> 0003_tool_result.json"),
        ("2026-11-06T09:01:21Z", "native.model_response", "-> 0004_model_response.txt (chars=21)"),
        ("2026-11-06T09:01:22Z", "native.tool_call", "Bash [main] -> 0005_tool_call.json"),
    ),
    "0001_model_response.txt": b"Starting.",
    "0002_tool_call.json": json.dumps(
        {"command": "uv run ruff check", "description": "Lint the tree"}
    ).encode(),
    "0003_tool_result.json": json.dumps(
        {"stdout": "All checks passed!", "stderr": "", "interrupted": False}
    ).encode(),
    "0004_model_response.txt": b"Running the suite now.",
    "0005_tool_call.json": json.dumps(
        {"command": "uv run pytest -q", "description": "Run full test suite"}
    ).encode(),
}


def test_activity_renders_the_legacy_now_and_last_call_panes():
    client, gateway = _make_client()
    gateway.set_contract_archive(CONTRACT_CID, ATTEMPT, ARCHIVE)
    body = client.get("/instances/web-0/activity").text
    # The legacy widget's markup, fed from the archive.
    assert 'class="activity-now activity-now-tool"' in body
    assert "<strong>Bash</strong>" in body
    assert "Run full test suite" in body
    assert "uv run pytest -q" in body
    assert "Last call" in body
    assert "Lint the tree" in body
    assert "All checks passed!" in body
    assert "took 78 s" in body
    # The summary line, as for a legacy instance.
    assert "<strong>Bash</strong> running" in body
    # Links open the contract Attempt page (no Attempt-log query).
    attempt_url = f"/instances/web-0/attempt/{quote(ATTEMPT, safe='')}"
    assert f'href="{attempt_url}"' in body
    assert "?q=" not in body
    # Two execs: the events, then every Evidence file in one export.
    assert [call[3] for call in gateway.contract_members_calls] == [
        ("events.jsonl",),
        (
            "0005_tool_call.json",
            "0002_tool_call.json",
            "0003_tool_result.json",
            "0004_model_response.txt",
        ),
    ]


def test_activity_model_working_after_the_last_result():
    archive = dict(ARCHIVE)
    archive["events.jsonl"] = ARCHIVE["events.jsonl"].rsplit(b"\n", 2)[0] + b"\n"
    client, gateway = _make_client()
    gateway.set_contract_archive(CONTRACT_CID, ATTEMPT, archive)
    body = client.get("/instances/web-0/activity").text
    assert 'class="activity-now activity-now-model"' in body
    assert "Model is working" in body
    assert "Running the suite now." in body
    assert "Lint the tree" in body


def test_activity_falls_back_to_the_snapshot_when_the_archive_is_unreadable():
    client, gateway = _make_client()  # no archive scripted: not_found
    body = client.get("/instances/web-0/activity").text
    assert "Tool calls unavailable" in body
    assert "model_running" in body
    assert "Last call" not in body
    # One retry on a failed read, then the fallback.
    assert len(gateway.contract_members_calls) == 2


def test_archive_events_map_native_evidence_events_onto_legacy_rows():
    rows = read_archive_events(ARCHIVE["events.jsonl"] + b"not json\n")
    assert [row.event for row in rows] == [
        "claim_opened",
        "model_response",
        "tool_call",
        "tool_result",
        "model_response",
        "tool_call",
        "unparsable_line",
    ]
    assert rows[2].evidence is not None
    assert (rows[2].evidence.tool, rows[2].evidence.thread, rows[2].evidence.file) == (
        "Bash",
        "main",
        "0002_tool_call.json",
    )
    assert rows[0].level == "INFO"
    assert rows[2].timestamp_text == "2026-11-06T09:00:02Z"


# --- multi-member export: argv and verification -----------------------------------


def test_export_members_argv_is_pinned():
    assert argv_contract_attempts_export_members("plain-1", ["a.json", "b.json"]) == [
        "attempts",
        "export",
        "plain-1",
        "--path",
        "a.json",
        "--path",
        "b.json",
    ]


@pytest.mark.parametrize(
    "paths",
    [
        [],
        ["a"] * 2,
        [f"{n}.json" for n in range(MAX_EXPORT_MEMBERS + 1)],
        "events.jsonl",
        ["../escape"],
        ["/abs"],
    ],
)
def test_export_members_argv_rejects_bad_path_lists(paths):
    with pytest.raises(ValueError):
        argv_contract_attempts_export_members("plain-1", paths)


def _tar(files: dict[str, bytes], *, listed: dict[str, bytes] | None = None) -> bytes:
    """An export tar whose manifest lists ``listed`` (default: the members)."""
    listed = files if listed is None else listed
    manifest = {
        "contract": "1.0",
        "attempt": {
            "attempt_id": ATTEMPT,
            "issue": {"number": 42, "title": "x"},
            "started_at": "2026-11-06T08:50:00Z",
            "finished_at": None,
            "lifecycle_state": "running",
            "attention_required": False,
            "sealed": False,
            "sealed_at": None,
            "outcome": None,
            "delivery": None,
        },
        "files": [
            {
                "kind": "other",
                "path": path,
                "size": len(data),
                "media_type": "application/octet-stream",
                "sha256": hashlib.sha256(data).hexdigest(),
            }
            for path, data in listed.items()
        ],
    }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        raw = json.dumps(manifest).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(raw)
        tar.addfile(info, io.BytesIO(raw))
        for path, data in files.items():
            info = tarfile.TarInfo(path)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_export_members_verify_against_the_manifest():
    good = _tar({"events.jsonl": b"one\n"})
    result = parse_export_members(good, 0, ["events.jsonl"])
    assert result.ok and result.members == {"events.jsonl": b"one\n"}
    torn = _tar({"events.jsonl": b"one\ntwo\n"}, listed={"events.jsonl": b"one\n"})
    result = parse_export_members(torn, 0, ["events.jsonl"])
    assert not result.ok
    assert "manifest size" in (result.error or "")


def test_fetch_members_retries_a_torn_read_once():
    torn = _tar({"events.jsonl": b"one\ntwo\n"}, listed={"events.jsonl": b"one\n"})
    good = _tar({"events.jsonl": b"one\ntwo\n"})
    replies = [ControlExec(torn, b"", 0), ControlExec(good, b"", 0)]

    async def fetch():
        return replies.pop(0)

    result = asyncio.run(fetch_members(fetch, ["events.jsonl"]))
    assert result.ok
    assert replies == []
