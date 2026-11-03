"""PROTOTYPE, throwaway: sign-in surfaces for LAN mode (ticket #151, map #92). Do not merge to main.

Question: what does built-in authentication look like in the dashboard UI (DESIGN.md)?
  - the sign-in page, with the error after a failed attempt and the growing delay;
  - where the dashboard shows who is signed in (account name, Role), and sign-out;
  - the page an operator lands on after the sign-in session ended (ADR 0009: htmx gets
    401 + `HX-Redirect: /login?next=<page>`, a navigation a 303 to /login);
  - the Control console for a `viewer` (role reason; `--control off` keeps precedence);
  - the `/set-password` page a Set-password link opens (ADR 0013).

Plan: "Three variants of every sign-in surface, switchable via `?variant=A|B|C` and the
floating bottom bar, on the real fleet page plus throwaway `/login`, `/logout` and
`/set-password` routes."

  A  Plain card: one narrow centred card per page; identity is a top bar chip plus a
     Sign out button; a viewer gets the existing one-line "Controls unavailable" reason.
  B  Instrument split: sign-in pages in two modules, Connection readouts (secure context,
     cookie kind, where you return to, session limits) next to the form, with a live
     countdown on the button; identity is an account menu in the top bar with the
     sign-in session details; a viewer sees the composer with every action disabled.
  C  Console line: a sign-in event log above a one-row form, like the Control console;
     identity is a session strip under the top bar; a viewer sees a View-only notice
     and the command timeline, no composer.

Runs the real app against the scripted in-memory fleet (no Docker). Accounts, sign-in
sessions, failures and Set-password links live in memory. The bottom bar shows the whole
auth state and changes it (expire the sign-in session, change a Role, Control off,
simulate an insecure context, open a Set-password link in each state).

Run:  uv run python prototype/lan_sign_in/run.py   ->  http://127.0.0.1:8768
Accounts: alice (viewer), bob (controller); password for both: correct-horse-battery
"""

from __future__ import annotations

import secrets
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import uvicorn  # noqa: E402
from fastapi import Request  # noqa: E402
from fastapi.responses import HTMLResponse, RedirectResponse, Response  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from fastapi.templating import Jinja2Templates  # noqa: E402
from fleet_world import ProtoGateway, World  # noqa: E402
from jinja2 import ChoiceLoader, FileSystemLoader  # noqa: E402

import agent_dashboard.app as app_mod  # noqa: E402
from agent_dashboard.config import Settings  # noqa: E402
from agent_dashboard.control import ControlMode  # noqa: E402

PORT = 8768
PASSWORD = "correct-horse-battery"
COOKIE = "proto_sid"  # real: `__Host-…` + Secure over https, plain on loopback http
IDLE_MIN, ABSOLUTE_H = 30, 12
ROLE_REASON = "your account can view but not issue Control actions"
OFF_REASON = "control is off (AGENT_DASHBOARD_CONTROL=off, read-only mode)"
OPEN_PREFIXES = ("/login", "/logout", "/set-password", "/static/", "/proto-static/", "/proto/")

VARIANTS = {
    "A": "Plain card",
    "B": "Instrument split",
    "C": "Console line",
}


@dataclass
class Account:
    name: str
    role: str
    password: str | None


@dataclass
class Session:
    account: str
    signed_in: float
    last_seen: float


@dataclass
class Proto:
    variant: str = "A"
    insecure: bool = False
    control_off: bool = False
    accounts: dict[str, Account] = field(default_factory=dict)
    sessions: dict[str, Session] = field(default_factory=dict)
    # failures per account name and per client address: (count, next attempt allowed at)
    failures: dict[str, tuple[int, float]] = field(default_factory=dict)
    tokens: dict[str, dict] = field(default_factory=dict)
    log: list[dict] = field(default_factory=list)  # sign-in events, newest first (variant C)

    def reset(self) -> None:
        self.accounts = {
            "alice": Account("alice", "viewer", PASSWORD),
            "bob": Account("bob", "controller", PASSWORD),
            "carol": Account("carol", "viewer", None),  # just added, no password yet
        }
        self.sessions.clear()
        self.failures.clear()
        self.log.clear()
        now = time.time()
        self.tokens = {
            "valid": {"account": "carol", "issued": now - 600, "state": "valid"},
            "expired": {"account": "carol", "issued": now - 26 * 3600, "state": "expired"},
            "used": {"account": "carol", "issued": now - 3600, "state": "used"},
            "superseded": {"account": "carol", "issued": now - 7200, "state": "superseded"},
        }

    # -- helpers for templates ---------------------------------------------------------

    @property
    def variant_name(self) -> str:
        return VARIANTS[self.variant]

    @property
    def keys(self) -> str:
        return ",".join(VARIANTS)

    def session_for(self, request: Request) -> tuple[str | None, Session | None]:
        sid = request.cookies.get(COOKIE)
        return sid, self.sessions.get(sid) if sid else None

    def op(self, request) -> dict | None:
        if request is None:
            return None
        _, s = self.session_for(request)
        if s is None:
            return None
        acct = self.accounts[s.account]
        return {
            "name": acct.name,
            "role": acct.role,
            "since": clock(s.signed_in),
            "ends_by": clock(s.signed_in + ABSOLUTE_H * 3600),
            "idle_min": IDLE_MIN,
        }

    def is_viewer(self, request) -> bool:
        op = self.op(request)
        return bool(op and op["role"] == "viewer")

    def state_lines(self, request) -> list[str]:
        op = self.op(request)
        lines = [f"sign-in session: {op['name']} ({op['role']}) since {op['since']}"
                 if op else "sign-in session: none"]
        now = time.time()
        for key, (n, until) in self.failures.items():
            wait = max(0, int(until - now + 0.999))
            lines.append(f"failures {key}: {n}" + (f", next in {wait}s" if wait else ""))
        lines.append(f"sessions: {len(self.sessions)}  roles: "
                     + ", ".join(f"{a.name}={a.role}" for a in self.accounts.values()))
        return lines

    def add_log(self, kind: str, text: str) -> None:
        self.log.insert(0, {"t": clock(time.time()), "kind": kind, "text": text})
        del self.log[8:]

    def delay_left(self, *keys: str) -> int:
        now = time.time()
        left = max((self.failures.get(k, (0, 0.0))[1] - now for k in keys), default=0)
        return max(0, int(left + 0.999))

    def fail(self, *keys: str) -> int:
        now = time.time()
        worst = 0
        for k in keys:
            n = self.failures.get(k, (0, 0.0))[0] + 1
            delay = min(2 ** (n - 1), 60)  # 1, 2, 4, 8 … capped at a minute; never a lockout
            self.failures[k] = (n, now + delay)
            worst = max(worst, delay)
        return worst


def clock(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def safe_next(value: str | None) -> str:
    """ADR 0009: `next` accepts only a relative path."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    return value


async def read_form(request: Request) -> dict[str, str]:
    body = (await request.body()).decode("utf-8", errors="replace")
    return {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()}


def build_app():
    world = World()
    gw = ProtoGateway(world)
    proto = Proto()
    proto.reset()
    captured: list[Jinja2Templates] = []
    orig_jinja = app_mod.Jinja2Templates

    def hooked_jinja(directory: str, **kwargs):
        t = orig_jinja(directory=directory, **kwargs)
        t.env.loader = ChoiceLoader([FileSystemLoader(str(HERE / "templates")), t.env.loader])
        t.env.globals["proto"] = proto
        captured.append(t)
        return t

    app_mod.Jinja2Templates = hooked_jinja
    try:
        app = app_mod.create_app(settings=Settings(poll_interval=2.0), gateway=gw)
    finally:
        app_mod.Jinja2Templates = orig_jinja
    templates = captured[0]
    app.mount("/proto-static", StaticFiles(directory=str(HERE / "static")), name="proto-static")

    def render(request: Request, name: str, status: int = 200, **ctx) -> Response:
        return templates.TemplateResponse(request, name, {"auth_page": True, **ctx},
                                          status_code=status)

    def unauthenticated(request: Request) -> Response:
        if request.url.path == "/stream":
            return Response(status_code=401)
        if request.headers.get("hx-request") == "true":
            current = request.headers.get("hx-current-url") or "/"
            parts = urlsplit(current)
            page = parts.path + (f"?{parts.query}" if parts.query else "")
            target = f"/login?next={quote(safe_next(page), safe='')}"
            return Response(status_code=401, headers={"HX-Redirect": target})
        here = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        target = "/login" if here == "/" else f"/login?next={quote(here, safe='')}"
        return RedirectResponse(target, status_code=303)

    @app.middleware("http")
    async def gate(request: Request, call_next):
        v = (request.query_params.get("variant") or "").upper()
        if v in VARIANTS:
            proto.variant = v
        path = request.url.path
        if path.startswith(OPEN_PREFIXES):
            return await call_next(request)
        sid, s = proto.session_for(request)
        now = time.time()
        if s is not None and (now - s.last_seen > IDLE_MIN * 60
                              or now - s.signed_in > ABSOLUTE_H * 3600):
            proto.sessions.pop(sid, None)
            s = None
        if s is None:
            return unauthenticated(request)
        if path != "/stream":
            s.last_seen = now  # an open /stream counts too (ADR 0009); here: every cycle below
        role = proto.accounts[s.account].role
        # Dashboard-wide mode first (`--control off` wins), then the Role. Variant A shows
        # the Role as an ordinary "Controls unavailable" reason; B and C keep the console
        # data and draw the view-only state in the template.
        if proto.control_off:
            app.state.control_mode = ControlMode(available=False, reason=OFF_REASON)
        elif role == "viewer" and proto.variant == "A":
            app.state.control_mode = ControlMode(available=False, reason=ROLE_REASON)
        else:
            app.state.control_mode = ControlMode(available=True)
        if (role == "viewer" and request.method == "POST" and path.startswith("/instances/")
                and "/control/" in path):
            # refused / not_permitted in the Control audit (#144)
            fragment = f'<p class="ctl-unavailable">Controls unavailable: {ROLE_REASON}</p>'
            return HTMLResponse(fragment, status_code=403)
        response = await call_next(request)
        if path == "/stream":
            inner = response.body_iterator

            async def guarded():
                async for chunk in inner:
                    live = proto.sessions.get(sid)
                    if live is None:
                        break  # sign-in session ended: close the stream
                    live.last_seen = time.time()
                    yield chunk

            response.body_iterator = guarded()
        return response

    # -- sign-in ---------------------------------------------------------------------

    def login_page(request: Request, status: int = 200, **ctx) -> Response:
        sid, s = proto.session_for(request)
        ctx.setdefault("next", safe_next(request.query_params.get("next")))
        ctx.setdefault("notice", None)
        if ctx["notice"] is None:
            if request.query_params.get("signed_out"):
                ctx["notice"] = "signed_out"
            elif request.query_params.get("password_set"):
                ctx["notice"] = "password_set"
            elif sid and s is None:
                ctx["notice"] = "ended"  # a cookie whose sign-in session is gone
        ctx.setdefault("account", request.query_params.get("account", ""))
        ctx.setdefault("error", None)
        ctx.setdefault("retry_after", 0)
        ctx["origin"] = f"{request.url.scheme}://{request.headers.get('host', '')}"
        resp = render(request, f"proto_auth/login_{proto.variant.lower()}.html", status, **ctx)
        if sid and s is None:
            resp.delete_cookie(COOKIE)
        return resp

    @app.get("/login", response_class=HTMLResponse)
    async def login_get(request: Request):
        _, s = proto.session_for(request)
        if s is not None:
            return RedirectResponse(safe_next(request.query_params.get("next")), status_code=303)
        return login_page(request)

    @app.post("/login", response_class=HTMLResponse)
    async def login_post(request: Request):
        f = await read_form(request)
        name = (f.get("account") or "").strip().lower()
        nxt = safe_next(f.get("next"))
        addr = f"addr {request.client.host if request.client else '?'}"
        keys = (f"account {name or '-'}", addr)
        common = {"next": nxt, "account": name, "notice": "none"}
        if proto.insecure:
            # Origin is neither https nor loopback (ADR 0011): refused before anything else.
            return login_page(request, 403, error="insecure", **common)
        wait = proto.delay_left(*keys)
        if wait:
            proto.add_log("wait", f"attempt refused unchecked: wait {wait} s")
            return login_page(request, 429, error="wait", retry_after=wait, **common)
        acct = proto.accounts.get(name)
        if acct is None or acct.password is None or f.get("password") != PASSWORD:
            delay = proto.fail(*keys)
            proto.add_log("failed", f"sign-in failed for '{name or '(empty)'}'")
            return login_page(request, 401, error="wrong", retry_after=delay, **common)
        for k in keys:
            proto.failures.pop(k, None)
        sid = secrets.token_urlsafe(16)
        now = time.time()
        proto.sessions[sid] = Session(acct.name, now, now)
        proto.add_log("ok", f"signed in as {acct.name}")
        resp = RedirectResponse(nxt, status_code=303)
        resp.set_cookie(COOKIE, sid, httponly=True, samesite="strict")
        return resp

    @app.post("/logout")
    async def logout(request: Request):
        sid, s = proto.session_for(request)
        if s is not None:
            proto.sessions.pop(sid, None)
            proto.add_log("out", f"{s.account} signed out")
        resp = RedirectResponse("/login?signed_out=1", status_code=303)
        resp.delete_cookie(COOKIE)
        return resp

    @app.get("/proto-whoami")
    async def whoami(request: Request):
        # Gated like every page: used by proto.js when the SSE stream closes, so a page
        # whose sign-in session ended gets its HX-Redirect even with nothing else polling.
        return Response(status_code=204)

    # -- set password ----------------------------------------------------------------

    def token_view(token: str) -> dict:
        rec = proto.tokens.get(token)
        if rec is None:
            return {"state": "unknown", "account": None}
        return {"state": rec["state"], "account": rec["account"],
                "issued": clock(rec["issued"]), "expires": clock(rec["issued"] + 24 * 3600),
                "left_h": max(0, int((rec["issued"] + 24 * 3600 - time.time()) // 3600))}

    def setpw_page(request: Request, token: str, status: int = 200, **ctx) -> Response:
        ctx.setdefault("errors", {})
        return render(request, f"proto_auth/setpw_{proto.variant.lower()}.html", status,
                      token=token, link=token_view(token), **ctx)

    @app.get("/set-password", response_class=HTMLResponse)
    async def setpw_get(request: Request, token: str = ""):
        link = token_view(token)
        return setpw_page(request, token, 200 if link["state"] == "valid" else 410)

    @app.post("/set-password", response_class=HTMLResponse)
    async def setpw_post(request: Request):
        f = await read_form(request)
        token = f.get("token", "")
        link = token_view(token)
        if proto.insecure:
            return setpw_page(request, token, 403, errors={"form": "insecure"})
        if link["state"] != "valid":
            return setpw_page(request, token, 410)
        pw, again = f.get("password", ""), f.get("confirm", "")
        errors = {}
        if len(pw) < 15:
            errors["password"] = "short"
        elif len(pw) > 128:
            errors["password"] = "long"
        elif pw != again:
            errors["confirm"] = "mismatch"
        if errors:
            return setpw_page(request, token, 422, errors=errors)
        acct = proto.accounts[link["account"]]
        acct.password = pw
        proto.tokens[token]["state"] = "used"
        proto.add_log("ok", f"password set for {acct.name}")
        return RedirectResponse(f"/login?password_set=1&account={acct.name}", status_code=303)

    # -- scenario controls (bottom bar) ------------------------------------------------

    @app.post("/proto/scenario")
    async def scenario(request: Request):
        f = await read_form(request)
        if "insecure" in f:
            proto.insecure = f["insecure"] == "1"
        if "control" in f:
            proto.control_off = f["control"] == "off"
        if "role" in f:
            who, _, role = f["role"].partition(":")
            if who in proto.accounts:
                proto.accounts[who].role = role  # applies from the next request (ADR 0013)
        if f.get("expire"):
            proto.sessions.clear()
        if f.get("reset"):
            proto.reset()
            proto.insecure = proto.control_off = False
        if f.get("new_link"):
            proto.tokens["valid"] = {"account": "carol", "issued": time.time(), "state": "valid"}
        return Response(status_code=204)

    @app.get("/proto/state", response_class=HTMLResponse)
    async def state(request: Request):
        return HTMLResponse("<br>".join(proto.state_lines(request)))

    return app


def main() -> None:
    uvicorn.run(build_app(), host="127.0.0.1", port=PORT)


if __name__ == "__main__":
    main()
