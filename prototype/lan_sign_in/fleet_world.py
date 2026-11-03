"""PROTOTYPE, throwaway: scripted in-memory fleet (copied from the visual design prototype)."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from fake_gateway import FakeGateway, make_logs_tree  # noqa: E402

from agent_dashboard.gateway import (  # noqa: E402
    ControlExec,
    FileResult,
    ManagedInstance,
    TelemetryExec,
    TelemetryTimeoutError,
)

FIXTURES = ROOT / "tests" / "fixtures" / "contract" / "v1"


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_ago(seconds: float) -> str:
    dt = datetime.now(UTC) - timedelta(seconds=seconds)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def dash_ts(iso_str: str) -> str:
    if "T" not in iso_str:
        return iso_str
    d, t = iso_str.split("T", 1)
    return f"{d}T{t.replace(':', '-')}"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"snapshot-{name}.json").read_text())




@dataclass
class Inst:
    name: str
    repo: str
    base: dict
    running: bool = True
    stale_after: int | None = None
    polls: int = 0
    seq: int = 25
    commands: dict[str, dict] = field(default_factory=dict)

    @property
    def cid(self) -> str:
        return hashlib.sha256(self.name.encode()).hexdigest()


def build_synthetic_history(
    inst_index: int, active_issue: int | None, active_started: str | None
) -> dict:
    """Build synthetic attempts over the last 60 days for rich charts and stats."""
    now = time.time()
    outcomes = [
        "complete",
        "complete",
        "complete",
        "handoff",
        "complete",
        "incomplete",
        "complete",
        "no_changes",
        "infrastructure_error",
        "complete",
    ]
    titles = [
        "Refactor telemetry collector to stream chunked snapshots",
        "Pin Docker argv validation in gateway",
        "Add idempotent retry keys to warehouse sync",
        "Migrate invoice ledger to decimal precision",
        "Preserve scroll position across SSE swaps",
        "Support stopped container logs via docker cp",
        "Parse tool_call and tool_result evidence pairs",
        "Bound concurrent gateway execs with semaphore",
        "Render inline SVG stacked outcome bars",
        "Reject non-loopback Host headers in middleware",
    ]
    attempts: list[dict] = []
    count = 34 - inst_index * 4
    for idx in range(max(8, count)):
        days_back = (idx * 1.6) + (inst_index * 0.35)
        ts = now - days_back * 86400 - (idx % 5) * 3100
        started_iso = datetime.fromtimestamp(ts, UTC).strftime(
            "%Y-%m-%dT%H:%M:%S.120000Z"
        )
        issue_num = 180 + idx + inst_index * 10
        outcome = outcomes[(idx + inst_index) % len(outcomes)]
        dur = 420 + ((idx * 137 + inst_index * 89) % 2400)
        inp = 42000 + ((idx * 9300) % 95000)
        cache_r = 180000 + ((idx * 41000) % 520000)
        cache_c = 18000 + ((idx * 5200) % 34000)
        out_tok = 6500 + ((idx * 1800) % 19000)
        cost = round(
            (inp * 3.0 + cache_r * 0.3 + cache_c * 3.75 + out_tok * 15.0) / 1_000_000,
            4,
        )
        pr_num = (issue_num + 100) if outcome == "complete" else None
        pr_url = (
            f"https://github.com/example-owner/repo/pull/{pr_num}" if pr_num else None
        )
        if outcome in ("complete", "handoff", "no_changes"):
            stop_reason = "end_turn"
        else:
            stop_reason = "max_tokens"
        attempts.append(
            {
                "issue": issue_num,
                "started": dash_ts(started_iso),
                "has_events": True,
                "attempt": {
                    "issue_number": issue_num,
                    "issue_title": titles[(idx + inst_index) % len(titles)],
                    "started_at": started_iso,
                    "duration_seconds": dur,
                    "outcome": outcome,
                    "stop_reason": stop_reason,
                    "agent_version": "0.8.2",
                    "cost_soft_threshold_usd": 1.60,
                    "pull_request": (
                        {"number": pr_num, "url": pr_url} if pr_num else None
                    ),
                    "token_budget": {"total_cost_usd": cost},
                    "model_usage": {
                        "claude-sonnet-4-20250514": {
                            "input_tokens": inp,
                            "cache_read_input_tokens": cache_r,
                            "cache_creation_input_tokens": cache_c,
                            "output_tokens": out_tok,
                            "costUSD": cost,
                        }
                    },
                },
            }
        )
    if active_issue is not None and active_started:
        attempts.append(
            {
                "issue": active_issue,
                "started": dash_ts(active_started),
                "has_events": True,
                "attempt": None,
            }
        )
    return {
        "layout": "attempt-logs/v1",
        "attempts": attempts,
        "active_started_at": active_started,
        "unreadable": 0,
    }


def build_attempt_events(
    issue: int, started_iso: str, active: bool = True
) -> list[dict]:
    """Build an ordered event stream with tool calls, model responses, and limits."""
    base_ts = time.time() - 540

    def ts_at(offset: float) -> str:
        return datetime.fromtimestamp(base_ts + offset, UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    def ev(offset: float, level: str, name: str, detail: str) -> dict:
        return {
            "timestamp": ts_at(offset),
            "level": level,
            "event": name,
            "detail": detail,
        }

    lim_base = "soft_threshold_tokens=160000 timeout_seconds=3600"
    events = [
        ev(0, "INFO", "attempt_started", f"issue #{issue} branch=agent/issue-{issue}"),
        ev(12, "INFO", "model_response", "-> 0001_model_response.txt (chars=312)"),
        ev(14, "INFO", "tool_call", "read_file [main] -> 0002_tool_call.json"),
        ev(16, "INFO", "tool_result", "read_file [main] -> 0003_tool_result.json"),
        ev(
            45,
            "INFO",
            "limits_checked",
            f"elapsed=45s turns=2/40 budget_tokens=28400/200000 {lim_base}",
        ),
        ev(80, "INFO", "model_response", "-> 0004_model_response.txt (chars=428)"),
        ev(84, "INFO", "tool_call", "edit_file [main] -> 0005_tool_call.json"),
        ev(89, "INFO", "tool_result", "edit_file [main] -> 0006_tool_result.json"),
        ev(
            160,
            "INFO",
            "limits_checked",
            f"elapsed=160s turns=6/40 budget_tokens=64200/200000 {lim_base}",
        ),
        ev(
            240,
            "WARNING",
            "rate_limit_backoff",
            "tokens_remaining=14200 waiting 2.4s before next turn",
        ),
        ev(
            310,
            "INFO",
            "limits_checked",
            f"elapsed=310s turns=10/40 budget_tokens=98500/200000 {lim_base}",
        ),
        ev(420, "INFO", "model_response", "-> 0007_model_response.txt (chars=275)"),
        ev(425, "INFO", "tool_call", "bash [main] -> 0008_tool_call.json"),
        ev(442, "INFO", "tool_result", "bash [main] -> 0009_tool_result.json"),
        ev(
            480,
            "INFO",
            "limits_checked",
            f"elapsed=480s turns=14/40 budget_tokens=124800/200000 {lim_base}",
        ),
        ev(510, "INFO", "model_response", "-> 0010_model_response.txt (chars=198)"),
    ]
    if active:
        events.append(
            ev(522, "INFO", "tool_call", "bash [main] -> 0011_tool_call.json")
        )
    else:
        events.append(
            ev(535, "INFO", "attempt_completed", "outcome=complete pr=#298")
        )
    return events


EVIDENCE_FILES: dict[str, bytes] = {
    "0001_model_response.txt": (
        b"I will inspect the telemetry collector and gateway modules first to see how "
        b"snapshots are buffered and parsed before updating the stream framing."
    ),
    "0002_tool_call.json": json.dumps(
        {
            "tool": "read_file",
            "arguments": {
                "path": "src/agent_dashboard/collector.py",
                "start_line": 1,
                "end_line": 120,
            },
        },
        indent=2,
    ).encode(),
    "0003_tool_result.json": json.dumps(
        {
            "ok": True,
            "lines": 120,
            "preview": (
                "async def collect_once(fleet: FleetState, gateway: DockerGateway):\n"
                "    instances = await gateway.list_instances()..."
            ),
        },
        indent=2,
    ).encode(),
    "0004_model_response.txt": (
        b"Now applying the non-blocking framing patch to collector.py and verifying "
        b"with the unit test suite."
    ),
    "0005_tool_call.json": json.dumps(
        {
            "tool": "edit_file",
            "arguments": {
                "path": "src/agent_dashboard/collector.py",
                "target": "timeout=5.0",
                "replacement": "timeout=cfg.exec_timeout",
            },
        },
        indent=2,
    ).encode(),
    "0006_tool_result.json": json.dumps(
        {
            "ok": True,
            "diff": "@@ -42,1 +42,1 @@\n-    timeout=5.0\n+    timeout=cfg.exec_timeout",
        },
        indent=2,
    ).encode(),
    "0007_model_response.txt": (
        b"Running the collector and gateway unit tests to confirm all assertions pass."
    ),
    "0008_tool_call.json": json.dumps(
        {
            "tool": "bash",
            "arguments": {"command": "uv run pytest tests/test_collector.py -q"},
        },
        indent=2,
    ).encode(),
    "0009_tool_result.json": json.dumps(
        {"exit_code": 0, "stdout": "18 passed in 0.84s\n", "stderr": ""},
        indent=2,
    ).encode(),
    "0010_model_response.txt": (
        b"Collector tests pass. Running the full CI gate (ruff + pytest)."
    ),
    "0011_tool_call.json": json.dumps(
        {
            "tool": "bash",
            "arguments": {"command": "uv run ruff check && uv run pytest -q"},
        },
        indent=2,
    ).encode(),
}


class World:
    def __init__(self) -> None:
        alpha = Inst(
            "agent-widgets",
            "example-owner/widgets",
            fixture("attempt-in-progress"),
        )
        charlie = Inst(
            "agent-inventory",
            "example-owner/inventory",
            fixture("soft-threshold-handoff"),
        )
        for rid, rec in charlie.base["control"]["commands"].items():
            charlie.commands[rid] = copy.deepcopy(rec)
        delta = Inst(
            "agent-billing", "example-owner/billing", fixture("recovery-hold")
        )
        bravo = Inst(
            "agent-reports",
            "example-owner/reports",
            fixture("idle-with-last-finished"),
        )
        bravo.base["control"]["stop_plan"] = {
            "request_id": "dash-9012ab",
            "requested": 3,
            "remaining": 2,
            "includes_active_attempt": False,
            "active_attempt_id": None,
            "latest_counted_attempt_id": None,
            "latest_counted_outcome": None,
        }
        foxtrot = Inst(
            "agent-gateway",
            "example-owner/gateway",
            fixture("attempt-in-progress"),
            stale_after=1,
        )
        foxtrot.base["current_attempt"]["issue_number"] = 89
        foxtrot.base["current_attempt"]["issue_title"] = (
            "Pin Docker argv validation in gateway"
        )
        foxtrot.base["control"]["active_attempt"]["issue_number"] = 89
        foxtrot.base["control"]["active_attempt"]["issue_title"] = (
            "Pin Docker argv validation in gateway"
        )

        golf = Inst(
            "agent-archive",
            "example-owner/archive",
            fixture("idle-with-last-finished"),
            running=False,
        )
        hotel = Inst(
            "agent-docs",
            "example-owner/docs",
            fixture("idle-with-last-finished"),
            running=False,
        )
        self.insts = [alpha, delta, charlie, bravo, foxtrot, golf, hotel]
        self.by_cid = {i.cid: i for i in self.insts}
        self.by_name = {i.name: i for i in self.insts}

    def snapshot(self, inst: Inst) -> dict:
        snap = copy.deepcopy(inst.base)
        snap["generated_at"] = now_iso()
        snap["sequence"] = inst.polls
        snap["identity"]["repository"] = inst.repo
        snap["identity"]["agent_version"] = "0.8.2"
        c = snap["control"]
        c["repository"] = inst.repo
        c["control_observed_at"] = now_iso()
        if inst.commands:
            recs = sorted(inst.commands.values(), key=lambda r: r["sequence"])[-10:]
            c["commands"] = {r["request_id"]: r for r in recs}
        return snap


class ProtoGateway(FakeGateway):
    def __init__(self, world: World) -> None:
        super().__init__(
            rows=[
                ManagedInstance(i.name, i.cid, "running" if i.running else "exited")
                for i in world.insts
            ]
        )
        self.world = world
        for idx, inst in enumerate(world.insts):
            active = inst.base.get("control", {}).get("active_attempt") or {}
            act_issue = active.get("issue_number")
            act_started = active.get("started_at")
            hist = build_synthetic_history(idx, act_issue, act_started)
            self.set_history(inst.cid, hist)
            if not inst.running:
                tree_entries: dict[str, bytes] = {}
                for entry in hist["attempts"][:12]:
                    if entry.get("attempt"):
                        key = f"logs/{entry['issue']}/{entry['started']}/attempt.json"
                        tree_entries[key] = json.dumps(entry["attempt"]).encode()
                self.set_logs_tree(inst.cid, make_logs_tree(tree_entries))

    async def telemetry(self, container_id: str) -> TelemetryExec:
        inst = self.world.by_cid[container_id]
        inst.polls += 1
        if inst.stale_after is not None and inst.polls > inst.stale_after:
            raise TelemetryTimeoutError("docker exec timed out after 5 s")
        snap = self.world.snapshot(inst)
        return TelemetryExec(stdout=json.dumps(snap).encode(), returncode=0)

    async def attempt_tail_log(
        self, container_id: str, issue: int | str, started: str
    ) -> TelemetryExec:
        events = build_attempt_events(int(issue), str(started), active=True)
        lines = [
            {"seq": i + 1, "line": json.dumps(ev)} for i, ev in enumerate(events)
        ]
        payload = {
            "layout": "attempt-log-tail/v1",
            "issue": int(issue),
            "started": str(started),
            "attempt": None,
            "events": lines,
            "total_events": len(lines),
            "unreadable": 0,
            "truncated": False,
            "file_size": 14820,
        }
        return TelemetryExec(stdout=json.dumps(payload).encode(), returncode=0)

    async def attempt_log(
        self, container_id: str, issue: int | str, started: str
    ) -> TelemetryExec:
        events = build_attempt_events(int(issue), str(started), active=False)
        pr_num = int(issue) + 100
        pr_url = f"https://github.com/example-owner/widgets/pull/{pr_num}"
        payload = {
            "layout": "attempt-log/v1",
            "issue": int(issue),
            "started": str(started),
            "attempt": {
                "issue_number": int(issue),
                "issue_title": (
                    "Refactor telemetry collector to stream chunked snapshots"
                ),
                "started_at": "2026-10-31T08:15:00Z",
                "duration_seconds": 535,
                "outcome": "complete",
                "stop_reason": "end_turn",
                "agent_version": "0.8.2",
                "pull_request": {"number": pr_num, "url": pr_url},
                "token_budget": {"total_cost_usd": 0.482},
            },
            "events": [json.dumps(ev) for ev in events],
            "total_events": len(events),
            "unreadable": 0,
            "truncated": False,
            "file_size": 18420,
        }
        return TelemetryExec(stdout=json.dumps(payload).encode(), returncode=0)

    async def attempt_events(
        self, container_id: str, issue: int | str, started: str
    ) -> TelemetryExec:
        events = build_attempt_events(int(issue), str(started), active=False)
        payload = {
            "layout": "attempt-events/v1",
            "issue": int(issue),
            "started": str(started),
            "attempt": {
                "issue_number": int(issue),
                "issue_title": (
                    "Refactor telemetry collector to stream chunked snapshots"
                ),
                "started_at": "2026-10-31T08:15:00Z",
                "duration_seconds": 535,
                "outcome": "complete",
            },
            "events": [json.dumps(ev) for ev in events],
            "total_events": len(events),
            "unreadable": 0,
            "truncated": False,
        }
        return TelemetryExec(stdout=json.dumps(payload).encode(), returncode=0)

    async def list_run_files(
        self, container_id: str, issue: int | str, started: str
    ) -> list[str]:
        return sorted(EVIDENCE_FILES.keys())

    async def read_file(
        self,
        container_id: str,
        issue: int | str,
        started: str,
        filename: str,
        cap: int | None = None,
    ) -> FileResult:
        default_bytes = json.dumps(
            {"file": filename, "note": "synthetic evidence preview"}
        ).encode()
        content = EVIDENCE_FILES.get(filename, default_bytes)
        if cap is not None and len(content) > cap:
            return FileResult(
                content=content[:cap], total_size=len(content), truncated=True
            )
        return FileResult(
            content=content, total_size=len(content), truncated=False
        )

    async def control(
        self,
        container_id: str,
        action: str,
        *,
        request_id: str,
        n: int | str | None = None,
        issue: int | str | None = None,
        expect_repo: str | None = None,
    ) -> ControlExec:
        inst = self.world.by_cid[container_id]
        inst.seq += 1
        ack = "completed" if action == "resume" else "accepted"
        detail = f"{action} acknowledged by synthetic agent"
        rec = {
            "request_id": request_id,
            "sequence": inst.seq,
            "kind": action,
            "acknowledgement": ack,
            "detail": detail,
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }
        inst.commands[request_id] = rec
        reply = {
            "ok": True,
            "command": action,
            "request_id": request_id,
            "sequence": inst.seq,
            "acknowledgement": ack,
            "detail": detail,
            "identity": {
                "instance_name": inst.name,
                "repository": inst.repo,
                "agent_version": "0.8.2",
            },
        }
        return ControlExec(
            stdout=json.dumps(reply).encode(), stderr=b"", returncode=0
        )


