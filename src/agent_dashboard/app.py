"""FastAPI app: handlers read only the in-memory fleet state."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from .charts import (
    OUTCOME_LABELS,
    OUTCOME_ORDER,
    SHAPE_GLYPHS,
    cost_per_attempt,
    cost_soft_threshold,
    cost_totals,
    cumulative_cost_per_day,
    duration_vs_cost,
    outcome_legend,
    outcomes_per_day,
    timeline_points,
    undated_count,
)
from .config import Settings
from .fleet import FleetState, poll_forever
from .gateway import DockerGateway, GatewayError
from .history import (
    DEFAULT_WINDOW,
    VALID_WINDOWS,
    format_cost,
    format_duration,
    read_history,
)

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
    gw = gateway if gateway is not None else DockerGateway(timeout=cfg.exec_timeout)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.fleet = fleet
        app.state.settings = cfg
        app.state.gateway = gw
        task: asyncio.Task | None = None
        if start_poller:
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
    app.state.gateway = gw
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

    @app.get("/instances/{name}/history", response_class=HTMLResponse)
    async def instance_history(request: Request, name: str) -> Response:
        """Attempt history page: one row per implementation attempt, newest first.

        Static with manual refresh (no stream subscription): the summaries
        for all attempts are read on demand with a single gateway exec,
        running managed instances only. An unknown instance, a stopped
        instance, an unreadable log and an unrecognised layout each render a
        clear message with status 200, never an error.
        """
        window = request.query_params.get("window", DEFAULT_WINDOW)
        if window not in VALID_WINDOWS:
            window = "all"
        rows, _ = await fleet.snapshot(poll_interval=cfg.poll_interval)
        row = find_row(rows, name)
        if row is None:
            return templates.TemplateResponse(
                request,
                "history.html",
                {
                    **context(request),
                    "row": None,
                    "name": name,
                    "window": window,
                    "result": None,
                    "error": f"Unknown Managed instance {name!r}: "
                    "it is unknown or was removed.",
                },
            )
        if row.state != "running":
            return templates.TemplateResponse(
                request,
                "history.html",
                {
                    **context(request),
                    "row": row,
                    "name": name,
                    "window": window,
                    "result": None,
                    "error": "Attempt history is available for running "
                    "managed instances only.",
                },
            )
        try:
            exec_result = await gw.attempt_history(row.container_id)
        except (GatewayError, ValueError) as exc:
            return templates.TemplateResponse(
                request,
                "history.html",
                {
                    **context(request),
                    "row": row,
                    "name": name,
                    "window": window,
                    "result": None,
                    "error": f"Could not read the Attempt log: {exc}",
                },
            )
        now_ts = time.time()
        result = read_history(
            exec_result.stdout, exec_result.returncode, now=now_ts, window=window
        )
        if not result.ok:
            return templates.TemplateResponse(
                request,
                "history.html",
                {
                    **context(request),
                    "row": row,
                    "name": name,
                    "window": window,
                    "result": None,
                    "error": result.error,
                },
            )
        buckets = outcomes_per_day(result.summaries, now=now_ts, window=result.window)
        points = timeline_points(result.summaries)
        peak = max((b.total for b in buckets), default=0)
        if peak >= 2:
            y_ticks = [0, (peak + 1) // 2, peak]
        elif peak == 1:
            y_ticks = [0, 1]
        else:
            y_ticks = []
        # X labels stay readable: show at most ~14 day ticks.
        label_every = max(1, -(-len(buckets) // 14))
        cost_points = cost_per_attempt(result.summaries)
        cumulative = cumulative_cost_per_day(
            result.summaries, now=now_ts, window=result.window
        )
        scatter = duration_vs_cost(result.summaries)
        totals = cost_totals(result.summaries)
        threshold = cost_soft_threshold(result.summaries)
        cost_peak = (
            max(
                [p.cost_value for p in cost_points]
                + ([threshold] if threshold is not None else [])
            )
            if cost_points
            else 0.0
        )
        if cost_peak > 0:
            cost_ticks = [
                (0.0, format_cost(0)),
                (0.5, format_cost(cost_peak / 2)),
                (1.0, format_cost(cost_peak)),
            ]
        else:
            cost_ticks = []
        cum_peak = max((d.cumulative for d in cumulative), default=0.0)
        if cum_peak > 0:
            cum_ticks = [
                (0.0, format_cost(0)),
                (0.5, format_cost(cum_peak / 2)),
                (1.0, format_cost(cum_peak)),
            ]
        else:
            cum_ticks = []
        scatter_cost_peak = max((p.cost_value for p in scatter), default=0.0)
        scatter_cost_ticks = (
            [
                (0.0, format_cost(0)),
                (0.5, format_cost(scatter_cost_peak / 2)),
                (1.0, format_cost(scatter_cost_peak)),
            ]
            if scatter_cost_peak > 0
            else []
        )
        scatter_dur_peak = max((p.duration_value for p in scatter), default=0.0)
        scatter_dur_ticks = (
            [
                (0.0, format_duration(0)),
                (0.5, format_duration(scatter_dur_peak / 2)),
                (1.0, format_duration(scatter_dur_peak)),
            ]
            if scatter_dur_peak > 0
            else []
        )
        cum_label_every = max(1, -(-len(cumulative) // 14))
        return templates.TemplateResponse(
            request,
            "history.html",
            {
                **context(request),
                "row": row,
                "name": name,
                "window": window,
                "result": result,
                "error": None,
                "buckets": buckets,
                "bucket_peak": peak,
                "label_every": label_every,
                "points": points,
                "legend": outcome_legend(result.summaries),
                "undated": undated_count(result.summaries),
                "chart_keys": list(OUTCOME_ORDER),
                "chart_labels": dict(OUTCOME_LABELS),
                "shape_glyphs": dict(SHAPE_GLYPHS),
                "y_ticks": y_ticks,
                "cost_points": cost_points,
                "cost_peak": cost_peak,
                "cost_ticks": cost_ticks,
                "cost_threshold": threshold,
                "cost_threshold_text": format_cost(threshold)
                if threshold is not None
                else None,
                "cumulative": cumulative,
                "cum_peak": cum_peak,
                "cum_ticks": cum_ticks,
                "cum_label_every": cum_label_every,
                "scatter": scatter,
                "scatter_cost_ticks": scatter_cost_ticks,
                "scatter_dur_ticks": scatter_dur_ticks,
                "cost_total_text": format_cost(totals.total),
                "cost_with_count": totals.with_cost,
                "cost_missing": totals.missing,
            },
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
