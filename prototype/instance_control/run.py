"""PROTOTYPE, throwaway: instance control actions (ticket #97, map #93). Do not merge to main.

Question: how should Control actions look and behave in the drawer and the fleet table:
placement, the two-step confirmation, inputs for `stop --after N` / `next issue <n>`,
feedback for an issued command, and how that feedback survives the SSE swaps?

Three variants on the real fleet page, switch with `?variant=A|B|C`, the bottom bar or <- / ->:
  A  Drawer panel: actions grouped in the drawer's Control section, inline confirmation,
     a result line under them that follows the command through telemetry; the fleet
     table shows command state only.
  B  Row menu + sheet: an Actions button on every live row and a toolbar under the drawer
     head open one in-page sheet (<dialog>); results become toasts that follow the command.
  C  Console: a command composer at the top of the drawer with the exact argv preview, and
     a command timeline (one lifecycle stepper per request ID) fed by telemetry
     `control.commands`.

Runs the real app against a scripted in-memory fleet (no Docker). Control actions hit a
simulated agent that applies the agent's acceptance rules (research #94) and advances
accepted commands over time. The bottom bar also picks the next reply (rejected,
unreachable, timeout, repository mismatch, audit failure, stale binding, handoff not
fulfilled) and a dashboard mode (control off, non-loopback bind, Host guard disabled).
Request protection (#96) is not enforced here; the routes only have the right shape.

Run:  uv run python prototype/instance_control/run.py   ->  http://127.0.0.1:8766
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import secrets
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import uvicorn  # noqa: E402
from fake_gateway import FakeGateway  # noqa: E402
from fastapi import Request  # noqa: E402
from fastapi.responses import HTMLResponse, Response  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from jinja2 import ChoiceLoader, FileSystemLoader  # noqa: E402

from agent_dashboard.app import create_app  # noqa: E402
from agent_dashboard.config import Settings  # noqa: E402
from agent_dashboard.gateway import (  # noqa: E402
    ManagedInstance,
    TelemetryExec,
    TelemetryTimeoutError,
)

FIXTURES = ROOT / "tests" / "fixtures" / "contract" / "v1"
TERMINAL = {"completed", "superseded", "not fulfilled"}
AGENTCTL = "/usr/local/bin/agentctl"


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"snapshot-{name}.json").read_text())


def pub(rec: dict) -> dict:
    return {k: v for k, v in rec.items() if not k.startswith("_")}


# ---------------------------------------------------------------- simulated agent


@dataclass
class Inst:
    name: str
    repo: str
    base: dict
    running: bool = True
    stale_after: int | None = None
    polls: int = 0
    seq: int = 20
    commands: dict[str, dict] = field(default_factory=dict)
    timers: list[tuple[float, Any]] = field(default_factory=list)
    attempt_ends_at: float | None = None

    @property
    def cid(self) -> str:
        return hashlib.sha256(self.name.encode()).hexdigest()

    @property
    def c(self) -> dict:
        return self.base["control"]


CURRENT = fixture("attempt-in-progress")["current_attempt"]


class World:
    def __init__(self) -> None:
        t = time.time()
        alpha = Inst("agent-widgets", "example-owner/widgets", fixture("attempt-in-progress"))
        alpha.attempt_ends_at = t + 120
        bravo = Inst("agent-reports", "example-owner/reports", fixture("idle-with-last-finished"))
        charlie = Inst("agent-inventory", "example-owner/inventory",
                       fixture("soft-threshold-handoff"))
        for rid, rec in charlie.c["commands"].items():
            charlie.commands[rid] = {**rec, "_payload": {}}
        delta = Inst("agent-billing", "example-owner/billing", fixture("recovery-hold"))
        echo = Inst("agent-docs", "example-owner/docs", fixture("idle-with-last-finished"))
        echo.c["intake"] = "stopped"
        foxtrot = Inst("agent-search", "example-owner/search", fixture("attempt-in-progress"),
                       stale_after=1)
        golf = Inst("agent-archive", "example-owner/archive", fixture("idle-with-last-finished"),
                    running=False)
        self.insts = [alpha, bravo, charlie, delta, echo, foxtrot, golf]
        self.by_cid = {i.cid: i for i in self.insts}
        self.by_name = {i.name: i for i in self.insts}
        self.handoff_ends_not_fulfilled = False

    def snapshot(self, inst: Inst) -> dict:
        snap = copy.deepcopy(inst.base)
        snap["generated_at"] = now_iso()
        snap["sequence"] = inst.polls
        snap["identity"]["repository"] = inst.repo
        c = snap["control"]
        c["repository"] = inst.repo
        recs = sorted(inst.commands.values(), key=lambda r: r["sequence"])[-10:]
        c["commands"] = {r["request_id"]: pub(r) for r in recs}
        c["control_observed_at"] = now_iso()
        if c.get("active_attempt") is None:
            snap["current_attempt"] = None
        return snap

    # agent-side command store -------------------------------------------------
    def _record(self, inst: Inst, rid: str, kind: str, payload: dict, ack: str,
                detail: str) -> dict:
        inst.seq += 1
        rec = {"request_id": rid, "sequence": inst.seq, "kind": kind,
               "acknowledgement": ack, "detail": detail, "created_at": now_iso(),
               "updated_at": now_iso(), "_payload": payload}
        inst.commands[rid] = rec
        return rec

    @staticmethod
    def _ack(rec: dict | None, ack: str, detail: str | None = None) -> None:
        if rec is None:
            return
        rec["acknowledgement"] = ack
        rec["updated_at"] = now_iso()
        if detail:
            rec["detail"] = detail

    def _supersede(self, inst: Inst, kinds: set[str]) -> None:
        for rec in inst.commands.values():
            if rec["kind"] in kinds and rec["acknowledgement"] == "accepted":
                self._ack(rec, "superseded", "superseded by a later command")

    def lookup(self, inst: Inst, rid: str) -> dict | None:
        """`agentctl command <id>`."""
        return inst.commands.get(rid)

    def apply(self, inst: Inst, kind: str, payload: dict, rid: str) -> tuple[int, Any]:
        """Exit 0 with the record, or exit 1 with the rejection reason."""
        if rid in inst.commands:
            rec = inst.commands[rid]
            if rec["kind"] == kind and rec["_payload"] == payload:
                return 0, rec
            return 1, f"request ID {rid} was already used for a different command/payload"
        c = inst.c
        handoff = c.get("handoff")
        begun = bool(handoff and handoff.get("begun")
                     and handoff.get("acknowledgement") == "accepted")
        active = c.get("active_attempt")
        now = time.time()
        if kind == "stop":
            self._supersede(inst, {"stop", "stop_after"})
            c["stop_plan"] = None
            if active is None:
                rec = self._record(inst, rid, kind, payload, "completed", "intake stopped")
                c["intake"] = "stopped"
                c["pending_command"] = None
            else:
                rec = self._record(inst, rid, kind, payload, "accepted",
                                   "stop requested; the active attempt finishes first")
                c["intake"] = "stopping"
                c["pending_command"] = pub(rec)
                inst.attempt_ends_at = min(inst.attempt_ends_at or now + 25, now + 25)
            return 0, rec
        if kind == "stop_after":
            if c.get("intake") == "stopped":
                return 1, "intake is stopped"
            if begun:
                return 1, "a handoff has begun"
            self._supersede(inst, {"stop", "stop_after"})
            n = payload["n"]
            rec = self._record(inst, rid, kind, payload, "accepted",
                               f"stop after {n} more attempt(s)")
            c["stop_plan"] = {"request_id": rid, "requested": n, "remaining": n,
                              "includes_active_attempt": active is not None,
                              "active_attempt_id": None, "latest_counted_attempt_id": None,
                              "latest_counted_outcome": None}
            c["pending_command"] = pub(rec)
            if n == 1 and active is not None:
                c["intake"] = "stopping"
            return 0, rec
        if kind == "resume":
            if c.get("recovering"):
                return 1, "the instance is recovering"
            if c.get("recovery") is not None:
                return 1, "a recovery hold is active; resolve it with agentctl recovery first"
            self._supersede(inst, {"stop", "stop_after"})
            c["stop_plan"] = None
            c["pending_command"] = None
            if handoff and not handoff.get("begun"):
                self._ack(inst.commands.get(handoff["request_id"]), "superseded")
                c["handoff"] = None
            if not begun:
                c["intake"] = "running"
            rec = self._record(inst, rid, kind, payload, "completed",
                               "intake running" if not begun
                               else "resumes when the handoff ends")
            return 0, rec
        if kind == "handoff":
            if active is None:
                return 1, "there is no active attempt"
            if handoff and handoff.get("acknowledgement") == "accepted":
                return 1, "a handoff is already in progress"
            rec = self._record(inst, rid, kind, payload, "accepted",
                               "handoff requested; delivers at the next safe model boundary "
                               "or its single fallback")
            c["handoff"] = {"request_id": rid, "accepted_at": now_iso(), "begun": False,
                            "phase": "accepted", "acknowledgement": "accepted",
                            "detail": rec["detail"],
                            "model_deadline_at": None, "publication_deadline_at": None}
            c["intake"] = "stopping"
            c["stop_plan"] = None
            c["pending_command"] = pub(rec)
            fail = self.handoff_ends_not_fulfilled
            self.handoff_ends_not_fulfilled = False
            inst.timers += [(now + 5, lambda: self._handoff_phase(inst, "delivering")),
                            (now + 12, lambda: self._handoff_phase(inst, "model_work")),
                            (now + 25, lambda: self._handoff_end(inst, fail))]
            return 0, rec
        if kind == "next_issue":
            n = payload["issue"]
            if n == 13:
                return 1, "issue #13 is not eligible: it is assigned to someone else"
            if n > 5000:
                return 1, f"issue #{n} does not exist or is closed"
            self._supersede(inst, {"next_issue"})
            rec = self._record(inst, rid, kind, payload, "accepted",
                               f"issue #{n} is claimed at the next permitted claim")
            c["pending_next_issue"] = {"request_id": rid, "issue_number": n}
            if active is None and c.get("intake") == "running":
                inst.timers.append((now + 8, lambda: self._start_attempt(inst)))
            return 0, rec
        return 1, f"unknown command {kind}"

    # time passing -------------------------------------------------------------
    def _handoff_phase(self, inst: Inst, phase: str) -> None:
        h = inst.c.get("handoff")
        if not h or h.get("acknowledgement") != "accepted":
            return
        h["begun"] = True
        h["phase"] = phase
        if phase == "model_work":
            h["model_deadline_at"] = now_iso()
            h["publication_deadline_at"] = now_iso()

    def _handoff_end(self, inst: Inst, fail: bool) -> None:
        h = inst.c.get("handoff")
        if not h or h.get("acknowledgement") != "accepted":
            return
        rec = inst.commands.get(h["request_id"])
        if fail:
            self._ack(rec, "not fulfilled", "the model did not reach a safe boundary in time")
        else:
            self._ack(rec, "completed", "handoff delivered and published")
        inst.c["handoff"] = None
        inst.c["pending_command"] = None
        inst.c["active_attempt"] = None
        inst.c["intake"] = "stopped"
        inst.attempt_ends_at = None

    def _start_attempt(self, inst: Inst) -> None:
        c = inst.c
        nxt = c.get("pending_next_issue")
        if c.get("active_attempt") is not None or not nxt:
            return
        n = nxt["issue_number"]
        self._ack(inst.commands.get(nxt["request_id"]), "completed", f"claimed issue #{n}")
        c["pending_next_issue"] = None
        c["active_attempt"] = {"issue_number": n, "branch": f"agent/issue-{n}",
                               "phase": "model_execution", "started_at": now_iso(),
                               "issue_title": f"Example: synthetic issue {n}"}
        inst.base["current_attempt"] = {**CURRENT, "issue_number": n,
                                        "issue_title": f"Example: synthetic issue {n}"}
        inst.attempt_ends_at = time.time() + 60

    def _end_attempt(self, inst: Inst) -> None:
        c = inst.c
        inst.attempt_ends_at = None
        c["active_attempt"] = None
        pend = c.get("pending_command")
        plan = c.get("stop_plan")
        if pend and pend.get("kind") == "stop":
            self._ack(inst.commands.get(pend["request_id"]), "completed", "intake stopped")
            c["pending_command"] = None
            c["intake"] = "stopped"
        elif plan:
            plan["remaining"] -= 1
            if plan["remaining"] <= 0:
                self._ack(inst.commands.get(plan["request_id"]), "completed", "intake stopped")
                c["stop_plan"] = None
                c["pending_command"] = None
                c["intake"] = "stopped"
        if c.get("intake") == "running" and c.get("pending_next_issue"):
            inst.timers.append((time.time() + 6, lambda: self._start_attempt(inst)))

    def tick(self) -> None:
        now = time.time()
        for inst in self.insts:
            due = [t for t in inst.timers if t[0] <= now]
            inst.timers = [t for t in inst.timers if t[0] > now]
            for _, fn in due:
                fn()
            if inst.attempt_ends_at and inst.attempt_ends_at <= now:
                h = inst.c.get("handoff")
                if h and h.get("acknowledgement") == "accepted":
                    inst.attempt_ends_at = now + 30  # the handoff ends it
                else:
                    self._end_attempt(inst)


class ProtoGateway(FakeGateway):
    """Discovery and telemetry from the World; remembers the last good snapshot."""

    def __init__(self, world: World) -> None:
        super().__init__(rows=[ManagedInstance(i.name, i.cid,
                                               "running" if i.running else "exited")
                               for i in world.insts])
        self.world = world
        self.last_good: dict[str, dict] = {}

    async def telemetry(self, container_id: str) -> TelemetryExec:
        inst = self.world.by_cid[container_id]
        inst.polls += 1
        if inst.stale_after is not None and inst.polls > inst.stale_after:
            raise TelemetryTimeoutError("docker exec timed out after 5 s")
        snap = self.world.snapshot(inst)
        self.last_good[container_id] = snap
        return TelemetryExec(stdout=json.dumps(snap).encode(), returncode=0)


# ---------------------------------------------------------------- dashboard side

ACTIONS = {
    "stop": ("Stop", "stop"),
    "stop_after": ("Stop after N attempts", "stop --after {n}"),
    "resume": ("Resume", "resume"),
    "handoff": ("Handoff now", "handoff now"),
    "next_issue": ("Next issue", "next issue {issue}"),
}
GROUPS = [("Intake", ["stop", "stop_after", "resume"]), ("Attempt", ["handoff", "next_issue"])]
N_RE = re.compile(r"^[1-9][0-9]*$")

VARIANTS = {"A": "Drawer panel", "B": "Row menu + sheet, toasts", "C": "Console + timeline"}
OUTCOMES = {
    "auto": "agent decides (1 s)",
    "slow": "agent decides slowly (6 s)",
    "rejected": "rejected (exit 1)",
    "unreachable": "unreachable (exit 2, not applied)",
    "timeout": "timeout (exit 2, but applied)",
    "mismatch": "reply repository mismatch",
    "audit_fail": "Control audit write fails (503)",
    "stale_binding": "instance replaced before submit",
    "not_fulfilled": "next handoff ends not fulfilled",
}
MODES = {
    "normal": "loopback, control on",
    "control_off": "AGENT_DASHBOARD_CONTROL=off",
    "non_loopback": "non-loopback bind",
    "guard_disabled": "Host guard disabled",
}
MODE_REASONS = {
    "control_off": "control is off (AGENT_DASHBOARD_CONTROL=off, read-only mode)",
    "non_loopback": "the dashboard is bound to a non-loopback address (0.0.0.0)",
    "guard_disabled": "the Host guard is disabled (AGENT_DASHBOARD_DISABLE_HOST_GUARD=1)",
}
REACH_REASONS = {
    "stale": "the snapshot is stale (newer polls failed)",
    "unreachable": "the agent is unreachable",
    "not running": "the container is not running",
    "unsupported": "this agent's telemetry is not supported",
}


@dataclass
class Action:
    key: str
    label: str
    enabled: bool
    reason: str | None
    note: str | None = None

    @property
    def param(self) -> str | None:
        return {"stop_after": "n", "next_issue": "issue"}.get(self.key)


@dataclass
class Avail:
    unavailable: str | None
    actions: list[Action]
    container_id: str
    repository: str
    agent_version: str
    control: dict

    def group(self, keys: list[str]) -> list[Action]:
        return [a for a in self.actions if a.key in keys]

    def get(self, key: str) -> Action | None:
        return next((a for a in self.actions if a.key == key), None)


def command_text(action: str, n: Any = None, issue: Any = None) -> str:
    return ACTIONS[action][1].format(n=n, issue=issue)


class Proto:
    variant = "A"
    next_outcome = "auto"
    mode = "normal"
    variants = VARIANTS
    outcomes = OUTCOMES
    modes = MODES
    groups = GROUPS

    def __init__(self, gw: ProtoGateway) -> None:
        self.gw = gw
        self.audit: list[dict] = []

    @staticmethod
    def slug(name: str) -> str:
        return re.sub(r"[^a-zA-Z0-9-]", "-", name)

    def snap(self, cid: str) -> dict | None:
        return self.gw.last_good.get(cid)

    def avail(self, row) -> Avail:
        snap = self.snap(row.container_id) or {}
        c = snap.get("control")
        ident = snap.get("identity") or {}
        base = dict(container_id=row.container_id, repository=ident.get("repository", ""),
                    agent_version=ident.get("agent_version", ""),
                    control=c if isinstance(c, dict) else {})
        if self.mode != "normal":
            return Avail(MODE_REASONS[self.mode], [], **base)
        if row.reach != "live":
            return Avail(REACH_REASONS.get(row.reach, row.reach), [], **base)
        if not isinstance(c, dict):
            return Avail("the snapshot has no control section", [], **base)
        intake = c.get("intake")
        recovering = bool(c.get("recovering"))
        recovery = c.get("recovery")
        pend = c.get("pending_command") or {}
        h = c.get("handoff") or {}
        accepted_h = h.get("acknowledgement") == "accepted"
        begun_h = accepted_h and bool(h.get("begun"))
        acts = []
        # stop
        if intake == "stopped":
            acts.append(Action("stop", "Stop", False, "already stopped"))
        elif pend.get("kind") == "stop":
            acts.append(Action("stop", "Stop", False, "a stop is pending"))
        else:
            acts.append(Action("stop", "Stop", True, None))
        # stop --after
        if intake == "stopped":
            acts.append(Action("stop_after", "Stop after N", False, "already stopped"))
        elif begun_h:
            acts.append(Action("stop_after", "Stop after N", False, "a handoff is in progress"))
        else:
            plan = c.get("stop_plan")
            note = f"replaces the current plan ({plan['remaining']} left)" if plan else None
            acts.append(Action("stop_after", "Stop after N", True, None, note))
        # resume
        something = (intake != "running" or bool(pend) or c.get("stop_plan") is not None
                     or (accepted_h and not begun_h))
        if recovering:
            acts.append(Action("resume", "Resume", False, "the instance is recovering"))
        elif recovery is not None:
            acts.append(Action("resume", "Resume", False,
                               "recovery hold: resolve it with agentctl recovery"))
        elif not something:
            acts.append(Action("resume", "Resume", False, "already running"))
        else:
            acts.append(Action("resume", "Resume", True, None))
        # handoff now
        if c.get("active_attempt") is None:
            acts.append(Action("handoff", "Handoff now", False, "no active attempt"))
        elif recovery is not None or recovering:
            acts.append(Action("handoff", "Handoff now", False, "recovery hold"))
        elif accepted_h:
            acts.append(Action("handoff", "Handoff now", False, "a handoff is in progress"))
        else:
            acts.append(Action("handoff", "Handoff now", True, None))
        # next issue
        nxt = c.get("pending_next_issue")
        acts.append(Action("next_issue", "Next issue", True, None,
                           f"currently #{nxt['issue_number']} is next" if nxt else None))
        return Avail(None, acts, **base)

    def chips(self, row) -> list[str]:
        """Short command-state chips for the fleet table."""
        snap = self.snap(row.container_id) or {}
        c = snap.get("control")
        if not isinstance(c, dict):
            return []
        out = []
        h = c.get("handoff")
        if h and h.get("acknowledgement") == "accepted":
            out.append(f"handoff · {h.get('phase')}")
        pend = c.get("pending_command") or {}
        if pend.get("kind") == "stop":
            out.append("stop pending")
        plan = c.get("stop_plan")
        if plan:
            out.append(f"stops after {plan['remaining']} more")
        nxt = c.get("pending_next_issue")
        if nxt:
            out.append(f"next #{nxt['issue_number']}")
        return out

    def timeline(self, row) -> list[dict]:
        snap = self.snap(row.container_id) or {}
        c = snap.get("control") or {}
        recs = sorted((c.get("commands") or {}).values(), key=lambda r: -r["sequence"])
        out = []
        for r in recs:
            steps = ["submitted", "accepted"]
            ack = r["acknowledgement"]
            if r["kind"] == "handoff":
                h = c.get("handoff") or {}
                phase = h.get("phase") if h.get("request_id") == r["request_id"] else None
                steps += ["delivering", "model_work"]
                current = phase or ("accepted" if ack == "accepted" else ack)
            else:
                current = ack
            steps.append(ack if ack in TERMINAL else "completed")
            out.append({**r, "steps": steps, "current": current,
                        "by_dashboard": r["request_id"].startswith("dash-")})
        return out

    def write_audit(self, entry: dict) -> None:
        self.audit.insert(0, {"time": now_iso(), **entry})
        del self.audit[50:]


async def read_form(request: Request) -> dict[str, str]:
    """urlencoded body without python-multipart (prototype only)."""
    raw = parse_qs((await request.body()).decode(), keep_blank_values=True)
    return {k: v[-1] for k, v in raw.items()}


def install(app, world: World, gw: ProtoGateway) -> Proto:
    proto = Proto(gw)
    templates = app.state.templates
    templates.env.loader = ChoiceLoader([FileSystemLoader(str(HERE / "templates")),
                                         templates.env.loader])
    templates.env.globals["proto"] = proto
    templates.env.globals["ACTIONS"] = ACTIONS
    app.mount("/proto-static", StaticFiles(directory=HERE / "static"), name="proto-static")
    fleet = app.state.fleet
    poll = app.state.settings.poll_interval

    @app.middleware("http")
    async def pick_variant(request: Request, call_next):
        if not hasattr(app.state, "proto_ticker"):
            async def loop() -> None:
                while True:
                    world.tick()
                    await asyncio.sleep(0.5)

            app.state.proto_ticker = asyncio.create_task(loop())
        v = request.query_params.get("variant")
        if request.url.path == "/" and v in VARIANTS:
            proto.variant = v
        return await call_next(request)

    async def find(name: str):
        rows, _ = await fleet.snapshot(poll_interval=poll)
        return next((r for r in rows if r.name == name), None)

    def render(request, tpl: str, status: int = 200, headers=None, **kw) -> Response:
        return templates.TemplateResponse(request, tpl, {"request": request, **kw},
                                          status_code=status, headers=headers)

    def validate(action: str, n: str, issue: str) -> tuple[dict, str | None]:
        if action not in ACTIONS:
            return {}, "unknown Control action"
        if action == "stop_after":
            if not N_RE.fullmatch(n or "") or not 1 <= int(n) <= 99:
                return {}, "N must be a whole number from 1 to 99"
            return {"n": int(n)}, None
        if action == "next_issue":
            if not N_RE.fullmatch(issue or "") or not 1 <= int(issue) <= 2147483647:
                return {}, "the issue must be a positive whole number"
            return {"issue": int(issue)}, None
        return {}, None

    @app.get("/proto/ctl/{name}/body", response_class=HTMLResponse)
    async def body(request: Request, name: str, where: str = "drawer", part: str = ""):
        row = await find(name)
        if row is None:
            return Response(status_code=204)
        tpl = "proto_ctl/b_sheet.html" if where == "dialog" else \
            f"proto_ctl/{proto.variant.lower()}_body.html"
        if part == "timeline":
            tpl = "proto_ctl/c_timeline.html"
        return render(request, tpl, row=row, av=proto.avail(row), where=where, error=None)

    @app.post("/proto/ctl/{name}/confirm", response_class=HTMLResponse)
    async def confirm(request: Request, name: str):
        f = await read_form(request)
        action, n, issue = f.get("action", ""), f.get("n", ""), f.get("issue", "")
        where = f.get("where", "drawer")
        row = await find(name)
        av = proto.avail(row)
        payload, error = validate(action, n, issue)
        if error:
            proto.write_audit({"instance": name, "action": action, "outcome": "refused",
                               "reason": f"422 {error}"})
            tpl = "proto_ctl/b_sheet.html" if where == "dialog" else \
                f"proto_ctl/{proto.variant.lower()}_body.html"
            return render(request, tpl, status=422, row=row, av=av, where=where,
                          error={"action": action, "text": error, "n": n, "issue": issue})
        return render(request, "proto_ctl/_confirm.html", row=row, av=av, where=where,
                      action=action, payload=payload,
                      cmd=command_text(action, **payload),
                      request_id="dash-" + secrets.token_hex(8))

    async def submit_core(name: str, action: str, payload: dict, container_id: str,
                          repository: str, request_id: str) -> dict:
        cmd = command_text(action, **payload)
        res = {"instance": name, "action": action, "cmd": cmd, "request_id": request_id,
               "payload": payload, "container_id": container_id, "repository": repository}
        outcome = proto.next_outcome
        proto.next_outcome = "auto"
        row = await find(name)
        audit = {"instance": name, "container_id": container_id[:12],
                 "expected_repository": repository, "action": cmd, "request_id": request_id,
                 "agent_version": proto.avail(row).agent_version if row else "",
                 "expect_repo_guard": False}
        # binding: same container ID, Reach live, same repository as the latest snapshot
        snap = proto.snap(container_id) or {}
        if (outcome == "stale_binding" or row is None or row.container_id != container_id
                or row.reach != "live"
                or (snap.get("identity") or {}).get("repository") != repository):
            proto.write_audit({**audit, "outcome": "refused", "reason": "stale binding"})
            return {**res, "kind": "refused", "status": 409,
                    "text": "Instance replaced or unavailable: refresh the view. "
                            "Nothing was sent."}
        if proto.mode != "normal":
            proto.write_audit({**audit, "outcome": "refused", "reason": proto.mode})
            return {**res, "kind": "refused", "status": 403,
                    "text": f"Controls unavailable: {MODE_REASONS[proto.mode]}."}
        if outcome == "audit_fail":
            return {**res, "kind": "audit_fail", "status": 503,
                    "text": "The Control audit could not be written, so the command was not "
                            "sent. Check the dashboard's state directory."}
        proto.write_audit({**audit, "outcome": "submitted"})
        if outcome == "not_fulfilled":
            world.handoff_ends_not_fulfilled = True
        await asyncio.sleep({"slow": 6, "timeout": 10}.get(outcome, 1.0))
        inst = world.by_cid[container_id]
        if outcome == "unreachable":
            proto.write_audit({**audit, "outcome": "unreachable", "exit": 2})
            return {**res, "kind": "unreachable", "status": 200,
                    "text": "The agent did not answer; the command was not applied."}
        if outcome == "rejected":
            code, reply = 1, "the agent's preconditions no longer hold"
        else:
            code, reply = world.apply(inst, action, payload, request_id)
        if outcome == "timeout":
            proto.write_audit({**audit, "outcome": "unreachable", "exit": 2})
            return {**res, "kind": "ambiguous", "status": 200,
                    "text": "No reply within 10 s. The command may or may not have been "
                            "applied."}
        if code == 1:
            proto.write_audit({**audit, "outcome": "rejected", "exit": 1, "error": reply})
            return {**res, "kind": "rejected", "status": 200,
                    "text": f"Rejected: {reply}. No control change was accepted."}
        reply_repo = "example-owner/other-repo" if outcome == "mismatch" else inst.repo
        mismatch = reply_repo != repository
        proto.write_audit({**audit, "outcome": "accepted", "exit": 0,
                           "sequence": reply["sequence"],
                           "acknowledgement": reply["acknowledgement"],
                           "reply_repository": reply_repo, "mismatch": mismatch})
        if mismatch:
            return {**res, "kind": "mismatch", "status": 200, "sequence": reply["sequence"],
                    "text": f"The agent applied the command to {reply_repo}, but this view "
                            f"showed {repository}. Nothing was undone."}
        return {**res, **track_state(inst, request_id), "status": 200}

    def track_state(inst: Inst, request_id: str) -> dict:
        snap = gw.last_good.get(inst.cid) or {}
        c = snap.get("control") or {}
        rec = (c.get("commands") or {}).get(request_id)
        via = "telemetry"
        if rec is None:
            # just submitted (next poll not yet in) or fell out of the last 10
            rec = world.lookup(inst, request_id)
            via = "agentctl command"
            if rec is None:
                return {"kind": "unknown", "via": via,
                        "text": f"{request_id}: no such command is recorded. It was not "
                                "applied; it is safe to issue it again."}
            rec = pub(rec)
        ack = rec["acknowledgement"]
        extra = None
        h = c.get("handoff") or {}
        if rec["kind"] == "handoff" and h.get("request_id") == request_id:
            extra = f"phase {h.get('phase')}"
        plan = c.get("stop_plan") or {}
        if rec["kind"] == "stop_after" and plan.get("request_id") == request_id:
            extra = f"{plan['remaining']} of {plan['requested']} attempt(s) left"
        nxt = c.get("pending_next_issue") or {}
        if rec["kind"] == "next_issue" and nxt.get("request_id") == request_id:
            extra = "waiting for the next permitted claim"
        return {"kind": ack.replace(" ", "_"), "ack": ack, "sequence": rec["sequence"],
                "detail": rec.get("detail"), "extra": extra, "via": via,
                "terminal": ack in TERMINAL}

    @app.post("/proto/ctl/{name}/submit", response_class=HTMLResponse)
    async def submit(request: Request, name: str):
        f = await read_form(request)
        action, n, issue = f.get("action", ""), f.get("n", ""), f.get("issue", "")
        container_id, repository = f.get("container_id", ""), f.get("repository", "")
        request_id, where, retry = f.get("request_id", ""), f.get("where", "drawer"), f.get("retry")
        payload, error = validate(action, n, issue)
        if error:
            return Response(error, status_code=422)
        res = await submit_core(name, action, payload, container_id, repository, request_id)
        row = await find(name)
        headers = {"HX-Trigger": "proto-close-dialog"} if where == "dialog" else None
        return render(request, "proto_ctl/_submitted.html", status=res["status"],
                      headers=headers, res=res, row=row,
                      av=proto.avail(row) if row else None, where=where, retry=bool(retry))

    @app.post("/proto/ctl/{name}/status", response_class=HTMLResponse)
    async def status(request: Request, name: str):
        f = await read_form(request)
        action, n, issue = f.get("action", ""), f.get("n", ""), f.get("issue", "")
        container_id, repository = f.get("container_id", ""), f.get("repository", "")
        request_id, where = f.get("request_id", ""), f.get("where", "drawer")
        payload, _ = validate(action, n, issue)
        inst = world.by_cid[container_id]
        res = {"instance": name, "action": action, "cmd": command_text(action, **payload),
               "request_id": request_id, "payload": payload, "container_id": container_id,
               "repository": repository, **track_state(inst, request_id)}
        return render(request, "proto_ctl/_result.html", res=res, where=where)

    @app.get("/proto/ctl/{name}/track", response_class=HTMLResponse)
    async def track(request: Request, name: str, request_id: str, action: str,
                    container_id: str, repository: str, where: str = "drawer",
                    n: str = "", issue: str = ""):
        payload, _ = validate(action, n, issue)
        inst = world.by_cid[container_id]
        res = {"instance": name, "action": action, "cmd": command_text(action, **payload),
               "request_id": request_id, "payload": payload, "container_id": container_id,
               "repository": repository, **track_state(inst, request_id)}
        return render(request, "proto_ctl/_result.html", res=res, where=where)

    @app.get("/proto/audit", response_class=HTMLResponse)
    async def audit(request: Request):
        return render(request, "proto_ctl/_audit.html")

    @app.post("/proto/scenario")
    async def scenario(request: Request):
        f = await read_form(request)
        outcome, mode = f.get("outcome"), f.get("mode")
        if outcome in OUTCOMES:
            proto.next_outcome = outcome
        if mode in MODES:
            proto.mode = mode
        return Response(status_code=204)

    return proto


def main() -> None:
    world = World()
    gw = ProtoGateway(world)
    app = create_app(settings=Settings(poll_interval=2.0), gateway=gw)
    install(app, world, gw)
    uvicorn.run(app, host="127.0.0.1", port=8766)


if __name__ == "__main__":
    main()
