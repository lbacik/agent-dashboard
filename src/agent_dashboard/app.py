"""FastAPI app: handlers read only the in-memory fleet state."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse
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


def host_allowed(host_header: str) -> bool:
    """Accept only localhost / 127.0.0.1, with or without a port."""
    raw = host_header.strip().lower()
    if not raw:
        return False
    if raw.startswith("["):
        # IPv6 bracketed form, e.g. [::1]:8000 -- not on the allow-list.
        end = raw.find("]")
        hostname = raw[1:end] if end != -1 else raw
    elif raw.count(":") > 1:
        hostname = raw  # unbracketed IPv6: reject
    else:
        hostname = raw.split(":")[0]
    return hostname in ALLOWED_HOSTS


class HostGuardMiddleware(BaseHTTPMiddleware):
    """Reject DNS-rebinding requests whose Host is not loopback."""

    async def dispatch(self, request: Request, call_next):
        if not host_allowed(request.headers.get("host", "")):
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
    app.add_middleware(HostGuardMiddleware)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    def context(request: Request) -> dict:
        return {"request": request, "poll_interval": int(cfg.poll_interval)}

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> Response:
        rows, error = await fleet.snapshot()
        return templates.TemplateResponse(
            request, "index.html", {**context(request), "rows": rows, "error": error}
        )

    @app.get("/fleet", response_class=HTMLResponse)
    async def fleet_partial(request: Request) -> Response:
        rows, error = await fleet.snapshot()
        return templates.TemplateResponse(
            request, "_fleet_table.html", {**context(request), "rows": rows, "error": error}
        )

    return app
