"""FastAPI app: handlers read only the in-memory fleet state."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from .config import Settings
from .fleet import FleetState, poll_forever
from .gateway import DockerGateway

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

ALLOWED_HOSTS = {"localhost", "127.0.0.1"}

SSE_RETRY_MS = 3000
SSE_HEARTBEAT_S = 15.0


def format_sse_event(event: str, data: str) -> str:
    """Format one server-sent event; multiline data gets a `data:` prefix per line."""
    lines = data.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out = f"event: {event}\n"
    for line in lines:
        out += f"data: {line}\n"
    return out + "\n"


async def fleet_event_stream(
    fleet, render_fleet, is_disconnected, poll_interval: float | None = None
) -> AsyncIterator[str]:
    """Yield SSE chunks: `retry`, the current table, then one `fleet` event per cycle.

    Ages are rendered at send time from the wall clock. `is_disconnected` is
    the request's disconnect check; when the client goes away the generator
    returns and unsubscribes. A task cancel (server shutdown) propagates
    through the `finally`, which unsubscribes as well.
    """
    queue = fleet.subscribe()
    try:
        yield f"retry: {SSE_RETRY_MS}\n\n"
        rows, error = await fleet.snapshot(poll_interval=poll_interval)
        yield format_sse_event("fleet", render_fleet(rows, error))
        while True:
            try:
                await asyncio.wait_for(queue.get(), timeout=SSE_HEARTBEAT_S)
            except TimeoutError:
                if await is_disconnected():
                    break
                yield ": ping\n\n"
                continue
            if await is_disconnected():
                break
            rows, error = await fleet.snapshot(poll_interval=poll_interval)
            yield format_sse_event("fleet", render_fleet(rows, error))
    finally:
        fleet.unsubscribe(queue)


def host_allowed(
    host_header: str, allowed_hosts: set[str] | frozenset[str] | None = None
) -> bool:
    """Accept loopback plus configured allow-list entries, with or without a port.

    A ``*`` entry never matches: the allow-list cannot become allow-all.
    """
    allow = ALLOWED_HOSTS if allowed_hosts is None else set(allowed_hosts) | ALLOWED_HOSTS
    allow = {h for h in allow if h != "*"}
    raw = host_header.strip().lower()
    if not raw:
        return False
    if raw.startswith("["):
        # IPv6 bracketed form, e.g. [::1]:8000 -- not on the allow-list
        # unless explicitly configured.
        end = raw.find("]")
        hostname = raw[1:end] if end != -1 else raw
    elif raw.count(":") > 1:
        hostname = raw  # unbracketed IPv6: reject
    else:
        hostname = raw.split(":")[0]
    return hostname in allow


class HostGuardMiddleware(BaseHTTPMiddleware):
    """Reject DNS-rebinding requests whose Host is not allow-listed."""

    def __init__(self, app, allowed_hosts: set[str] | frozenset[str] | None = None):
        super().__init__(app)
        self.allowed_hosts = (
            frozenset(ALLOWED_HOSTS) if allowed_hosts is None else frozenset(allowed_hosts)
        )

    async def dispatch(self, request: Request, call_next):
        if not host_allowed(request.headers.get("host", ""), self.allowed_hosts):
            return PlainTextResponse("forbidden: unexpected Host header", status_code=403)
        return await call_next(request)


def create_app(
    state: FleetState | None = None,
    settings: Settings | None = None,
    gateway: DockerGateway | None = None,
    *,
    start_poller: bool = True,
) -> FastAPI:
    fleet = state if state is not None else FleetState()
    cfg = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.fleet = fleet
        app.state.settings = cfg
        task: asyncio.Task | None = None
        if start_poller:
            gw = gateway if gateway is not None else DockerGateway(timeout=cfg.exec_timeout)
            task = asyncio.create_task(
                poll_forever(
                    fleet,
                    gw,
                    cfg.poll_interval,
                    max_concurrent_execs=cfg.max_concurrent_execs,
                )
            )
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    app = FastAPI(title="agent-dashboard", lifespan=lifespan)
    app.state.fleet = fleet
    app.state.settings = cfg
    extra_hosts = getattr(cfg, "allowed_hosts", frozenset()) or frozenset()
    # TEMPORARY escape hatch: AGENT_DASHBOARD_DISABLE_HOST_GUARD=1 skips the Host check.
    if os.environ.get("AGENT_DASHBOARD_DISABLE_HOST_GUARD") != "1":
        app.add_middleware(
            HostGuardMiddleware, allowed_hosts=set(ALLOWED_HOSTS) | set(extra_hosts)
        )
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    def context(request: Request) -> dict:
        return {"request": request, "poll_interval": int(cfg.poll_interval)}

    def find_row(rows, name: str):
        """The fleet row for one instance name, or None when unknown/removed."""
        return next((r for r in rows if r.name == name), None)

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> Response:
        rows, error = await fleet.snapshot(poll_interval=cfg.poll_interval)
        wanted = request.query_params.get("instance") or ""
        row = find_row(rows, wanted) if wanted else None
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                **context(request),
                "rows": rows,
                "error": error,
                "drawer_open": bool(wanted),
                "row": row,
                "name": wanted,
            },
        )

    @app.get("/instances/{name}", response_class=HTMLResponse)
    async def instance_drawer(request: Request, name: str) -> Response:
        """Instance drawer fragment, refreshed on every `fleet` SSE event.

        A click on a table row fetches this into `#drawer-holder`; the
        drawer's own `hx-trigger="sse:fleet"` re-fetches it after every poll
        cycle. An unknown or removed instance renders the gone fragment
        (status 200) so a refresh can also bring the drawer back.
        """
        rows, _ = await fleet.snapshot(poll_interval=cfg.poll_interval)
        row = find_row(rows, name)
        return templates.TemplateResponse(
            request, "_drawer.html", {**context(request), "row": row, "name": name}
        )

    @app.get("/fleet", response_class=HTMLResponse)
    async def fleet_partial(request: Request) -> Response:
        rows, error = await fleet.snapshot(poll_interval=cfg.poll_interval)
        return templates.TemplateResponse(
            request, "_fleet_table.html", {**context(request), "rows": rows, "error": error}
        )

    def render_fleet(rows, error) -> str:
        """Re-render the whole table body; ages are stamped at send time."""
        template = templates.get_template("_fleet_table.html")
        return template.render(rows=rows, error=error)

    @app.get("/stream")
    async def stream(request: Request) -> StreamingResponse:
        """Server-sent events: a `fleet` event with the whole table after every cycle.

        Ages are rendered at send time from the wall clock. EventSource
        reconnects by itself after a dashboard restart; the `retry:` directive
        sets the reconnect delay. Disconnects and shutdown cancel the
        generator, whose `finally` unsubscribes the client.
        """

        async def event_generator() -> AsyncIterator[str]:
            async for chunk in fleet_event_stream(
                fleet, render_fleet, request.is_disconnected, cfg.poll_interval
            ):
                yield chunk

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return app
