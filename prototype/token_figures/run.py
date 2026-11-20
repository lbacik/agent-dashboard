"""PROTOTYPE, throwaway: how the dashboard presents the token figures.

Do not merge to main. Question (coding-agent#561, map coding-agent#553):
how does the dashboard present the token figures operator contract 1.1
carries (Conversation Context against the Usable Threshold, the Handoff
Threshold and the Model Context Window; spend per bucket and Usage Origin;
Sub-agent counts), and which belong in the fleet table vs the drawer?

Plan: "Three variants of the fleet matrix and the cockpit's 04 Current
attempt module, switchable via `?variant=A|B|C` and a floating bottom bar,
on the existing `/` route with synthetic contract instances."

  A  Ledger: the current facts list evolves in place. One context bar,
     an origins x buckets table, limits as text. Fleet keeps its columns.
  B  Gauges: separate Main context and Spend modules. Two-scale context
     gauge (window strip + zoomed threshold bar), origin-stacked limit bars,
     buckets folded away. Fleet gains a Sub-agents column.
  C  Ladder: a sorted context ladder with distances, a share heatmap for
     spend; the fleet matrix carries most of the figures, the drawer adds
     detail only.

The contract parser is 1.0, so the snapshots stay 1.0-valid and the 1.1
figures (`context_threshold`, `native.handoff_threshold_tokens`,
`budget.by_origin`, `subagents_running` / `subagents_ended`) are served to
the templates straight from the synthetic world through `ptok(name)`.

Run:  uv run python prototype/token_figures/run.py  ->  http://127.0.0.1:8768
"""

from __future__ import annotations

import copy
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

import uvicorn  # noqa: E402
from agent_operator_contract import documents  # noqa: E402
from fastapi import Request  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from jinja2 import ChoiceLoader, FileSystemLoader  # noqa: E402

import agent_dashboard.app as app_mod  # noqa: E402
from agent_dashboard.config import Settings  # noqa: E402
from agent_dashboard.gateway import (  # noqa: E402
    CONTRACT_PATH,
    ControlExec,
    DockerGateway,
    ManagedInstance,
)

VARIANTS = {
    "A": {"name": "Ledger", "fleet_cols": 4},
    "B": {"name": "Gauges", "fleet_cols": 5},
    "C": {"name": "Ladder", "fleet_cols": 4},
}


def stamp(t: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if t is None else t))


def fig(value, unit, basis="measured", reason=None) -> dict:
    out = {"value": value, "unit": unit, "basis": basis}
    if reason:
        out["reason"] = reason
    return out


def cell(inp, cr, cw, out, cost, basis="measured", reason=None) -> dict:
    """One Usage Origin cell (contract 1.1 `budget.by_origin.<origin>`)."""
    return {
        "input": inp,
        "cache_read": cr,
        "cache_write": cw,
        "output": out,
        "effective": max(0, inp - 0) + cw + out,
        "cost": cost,
        "basis": basis,
        "reason": reason,
    }


UNAVAILABLE = {"basis": "unavailable", "reason": "not recorded"}


class Inst:
    def __init__(self, name, cid, backend, base, tok, running=True, issue=None):
        self.name = name
        self.cid = cid
        self.backend = backend
        self.base = base
        self.tok = tok
        self.running = running
        self.issue = issue
        self.polls = 0


def status_result(name: str) -> dict:
    return json.loads(documents.load_fixture(name)["stdout"])["result"]


def coding_agent_tok(**kw) -> dict:
    tok = {
        "backend": "coding-agent",
        "window": 1_000_000,
        "window_basis": "configured",
        "handoff": 80_000,
        "usable": 78_000,
        "auto_compact": None,
        "eff_limit": 2_000_000,
        "cost_limit": 20.0,
    }
    tok.update(kw)
    return tok


class World:
    def __init__(self) -> None:
        widgets = Inst(
            "agent-widgets",
            "a1b2c3d4e5f6",
            "coding-agent",
            status_result("status-active"),
            coding_agent_tok(
                used=61_340,
                used_reason="anchored on provider-reported input",
                peak=77_410,
                peak_reason=None,
                handoffs=1,
                sub_running=3,
                sub_ended=7,
                sub_reason=None,
                origins={
                    "main": cell(312_400, 4_812_000, 141_200, 96_300, 6.12),
                    "subagent": cell(208_900, 1_904_500, 88_700, 61_400, 3.31),
                    "handoff": cell(78_100, 0, 0, 4_100, 0.44),
                },
            ),
            issue=(42, "Add CSV export"),
        )
        billing = Inst(
            "agent-billing",
            "b2c3d4e5f6a1",
            "coding-agent",
            status_result("status-active"),
            coding_agent_tok(
                used=77_250,
                used_reason="anchored on provider-reported input",
                peak=77_900,
                peak_reason=None,
                handoffs=2,
                sub_running=0,
                sub_ended=2,
                sub_reason=None,
                origins={
                    "main": cell(402_700, 6_920_400, 172_300, 118_900, 8.47),
                    "subagent": cell(41_300, 288_000, 12_100, 9_800, 0.61),
                    "handoff": cell(
                        236_000,
                        0,
                        0,
                        6_200,
                        1.37,
                        basis="estimated",
                        reason="upper bound: 1 response without usage charged at worst case",
                    ),
                },
                eff_limit=1_500_000,
                cost_limit=12.0,
            ),
            issue=(118, "Retry failed invoice webhooks"),
        )
        reports = Inst(
            "agent-reports",
            "c3d4e5f6a1b2",
            "coding-agent",
            status_result("status-active"),
            coding_agent_tok(
                used=12_400,
                used_reason="local estimate, no measured response yet",
                peak=41_200,
                peak_reason="covers only requests recorded since upgrade",
                handoffs=1,
                sub_running=1,
                sub_ended=1,
                sub_reason="counts only endings recorded since upgrade",
                origins=None,
                eff_limit=2_000_000,
                cost_limit=20.0,
            ),
            issue=(7, "Weekly digest e-mail"),
        )
        docs = Inst(
            "agent-docs",
            "d4e5f6a1b2c3",
            "simple-coding-agent",
            status_result("status-simple-coding-agent"),
            {
                "backend": "simple-coding-agent",
                "window": 200_000,
                "window_basis": "assumed",
                "handoff": None,
                "usable": None,
                "auto_compact": 160_000,
                "used": 142_000,
                "used_reason": None,
                "peak": 158_300,
                "peak_reason": None,
                "handoffs": None,
                "sub_running": None,
                "sub_ended": None,
                "sub_reason": None,
                "origins": None,
                "eff_limit": None,
                "cost_limit": None,
                "raw_limit": 2_000_000,
            },
            issue=(31, "Document the CLI flags"),
        )
        archive = Inst(
            "agent-archive",
            "e5f6a1b2c3d4",
            "coding-agent",
            status_result("status-idle"),
            None,
            running=False,
        )
        self.insts = [billing, widgets, reports, docs, archive]
        self.by_cid = {i.cid: i for i in self.insts}
        self.by_name = {i.name: i for i in self.insts}

    def tick(self, inst: Inst) -> None:
        """agent-widgets grows its context each poll and hands off at the threshold."""
        inst.polls += 1
        tok = inst.tok
        if inst.name != "agent-widgets":
            return
        if tok["used_reason"].startswith("local"):
            tok["used_reason"] = "anchored on provider-reported input"
        tok["used"] += 1_150
        tok["peak"] = max(tok["peak"], tok["used"])
        if tok["used"] >= tok["usable"]:
            tok["used"] = 9_200
            tok["used_reason"] = "local estimate, no measured response yet"
            tok["handoffs"] += 1
            h = tok["origins"]["handoff"]
            h["input"] += 76_000
            h["output"] += 3_900
            h["effective"] += 79_900
            h["cost"] += 0.42
        m = tok["origins"]["main"]
        m["input"] += 2_300
        m["cache_read"] += 58_000
        m["output"] += 900
        m["effective"] += 3_200
        m["cost"] += 0.04

    def snapshot(self, inst: Inst) -> dict:
        snap = copy.deepcopy(inst.base)
        now = stamp()
        snap["generated_at"] = now
        snap["identity"]["repository"] = f"example-owner/{inst.name.removeprefix('agent-')}"
        snap["identity"]["process_started_at"] = stamp(time.time() - 7200)
        for key in ("instance", "intake"):
            snap[key]["observed_at"] = now
        cur = snap.get("current_attempt")
        if not isinstance(cur, dict) or inst.tok is None:
            return snap
        self.tick(inst)
        tok = inst.tok
        cur["observed_at"] = now
        cur["last_activity_at"] = now
        cur["started_at"] = stamp(time.time() - 1500)
        cur["issue"] = {"number": inst.issue[0], "title": inst.issue[1]}
        figs = cur.setdefault("figures", {})
        figs["context_used"] = fig(tok["used"], "tokens", "estimated", tok["used_reason"])
        figs["context_peak"] = fig(tok["peak"], "tokens", "estimated", tok["peak_reason"])
        figs["context_window"] = fig(tok["window"], "tokens", tok["window_basis"])
        if tok["handoffs"] is not None:
            figs["continuations"] = fig(tok["handoffs"], "count")
        if tok["backend"] == "coding-agent":
            if tok["origins"] is not None:
                m = tok["origins"]["main"]
                rate = m["cache_read"] / (m["cache_read"] + m["input"] + m["cache_write"])
                figs["cache_hit_rate"] = fig(round(rate, 4), "ratio")
            eff, cost = totals(tok)
            budget = cur["budget"]
            for lim in budget["limits"]:
                if lim["name"] == "effective_tokens":
                    lim["limit"]["value"] = tok["eff_limit"]
                    if eff is not None:
                        lim["used"]["value"] = eff
                if lim["name"] == "cost_usd":
                    lim["limit"]["value"] = tok["cost_limit"]
                    if cost is not None:
                        lim["used"]["value"] = round(cost, 2)
            if eff is not None:
                budget["effective_tokens"]["value"] = eff
                budget["raw_tokens"]["value"] = sum(
                    c["input"] + c["cache_read"] + c["cache_write"] + c["output"]
                    for c in tok["origins"].values()
                )
            else:
                budget["effective_tokens"]["value"] = 1_036_200
                budget["raw_tokens"]["value"] = 5_918_400
                for lim in budget["limits"]:
                    if lim["name"] == "effective_tokens":
                        lim["used"]["value"] = 1_036_200
                    if lim["name"] == "cost_usd":
                        lim["used"]["value"] = 7.12
        return snap


def totals(tok):
    if tok.get("origins") is None:
        return None, None
    eff = sum(c["effective"] for c in tok["origins"].values())
    cost = sum(c["cost"] for c in tok["origins"].values())
    return eff, cost


WORLD = World()


class ProtoRunner:
    """Answers `agentctl status` from the synthetic world; anything else is an error."""

    async def run(self, argv, *, timeout, timeout_error) -> ControlExec:
        _ = (timeout, timeout_error)
        inst = next((i for i in WORLD.insts if i.cid in argv), None)
        if inst is not None and inst.running and argv[-1] == "status":
            env = {"contract": "1.0", "ok": True, "result": WORLD.snapshot(inst)}
            return ControlExec(stdout=json.dumps(env).encode(), stderr=b"", returncode=0)
        env = {
            "contract": "1.0",
            "ok": False,
            "error": {"code": "internal", "message": "prototype: not served", "retryable": False},
        }
        return ControlExec(stdout=json.dumps(env).encode(), stderr=b"", returncode=7)


# ---- template helpers -------------------------------------------------------


def n(v) -> str:
    return "—" if v is None else f"{int(round(v)):,}"


def k(v) -> str:
    if v is None:
        return "—"
    if v >= 1_000_000:
        return f"{v / 1_000_000:.2f}M"
    if v >= 1_000:
        return f"{v / 1_000:.1f}K"
    return str(int(v))


def usd(v) -> str:
    return "—" if v is None else f"${v:,.2f}"


def pct(a, b) -> float:
    if a is None or not b:
        return 0.0
    return max(0.0, min(100.0, 100.0 * a / b))


ORIGIN_LABELS = {"main": "Main", "subagent": "Sub-agents", "handoff": "Handoff"}
BUCKETS = [
    ("input", "Input"),
    ("cache_read", "Cache read"),
    ("cache_write", "Cache write"),
    ("output", "Output"),
]


def ptok(name: str):
    inst = WORLD.by_name.get(name)
    if inst is None or inst.tok is None:
        return None
    tok = inst.tok
    eff, cost = totals(tok)
    view = dict(tok)
    view["eff_total"] = eff
    view["cost_total"] = cost
    view["has_origins"] = tok["origins"] is not None
    view["is_coding_agent"] = tok["backend"] == "coding-agent"
    # The compared threshold: Usable Threshold (coding-agent) or none.
    view["compare"] = tok["usable"]
    view["headroom"] = None if tok["usable"] is None else tok["usable"] - tok["used"]
    if not view["has_origins"] and view["is_coding_agent"]:
        view["eff_total"] = 1_036_200
        view["cost_total"] = 7.12
    return view


class ProtoState:
    variant = "A"
    variants = VARIANTS

    @property
    def cols(self) -> int:
        return VARIANTS[self.variant]["fleet_cols"]

    @property
    def name(self) -> str:
        return VARIANTS[self.variant]["name"]


class NoCacheStatic(StaticFiles):
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-store"
        return resp


def build_app():
    proto = ProtoState()
    gateway = DockerGateway(process_runner=ProtoRunner())

    async def _list():
        return [
            ManagedInstance(
                i.name,
                i.cid,
                "running" if i.running else "exited",
                path=CONTRACT_PATH,
                instance=i.name,
                exec_user="agent",
                backend=i.backend,
            )
            for i in WORLD.insts
        ]

    gateway.list_instances = _list  # type: ignore[method-assign]

    orig = app_mod.Jinja2Templates

    def hooked(directory: str, **kwargs):
        t = orig(directory=directory, **kwargs)
        t.env.loader = ChoiceLoader([FileSystemLoader(str(HERE / "templates")), t.env.loader])
        t.env.globals.update(
            proto=proto,
            ptok=ptok,
            n=n,
            k=k,
            usd=usd,
            pct=pct,
            ORIGIN_LABELS=ORIGIN_LABELS,
            BUCKETS=BUCKETS,
        )
        return t

    app_mod.Jinja2Templates = hooked
    try:
        app = app_mod.create_app(settings=Settings(poll_interval=2.0), gateway=gateway)
    finally:
        app_mod.Jinja2Templates = orig

    app.mount("/proto-static", NoCacheStatic(directory=str(HERE / "static")), name="proto-static")

    @app.middleware("http")
    async def variant_mw(request: Request, call_next):
        v = (request.query_params.get("variant") or "").upper()
        if v in VARIANTS:
            proto.variant = v
        return await call_next(request)

    return app


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8768
    uvicorn.run(build_app(), host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
