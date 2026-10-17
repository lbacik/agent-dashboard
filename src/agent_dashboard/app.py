"""FastAPI app: the fleet table reads only the in-memory fleet state.

The overview panel (on ``/``) and the history page read attempt summaries
on demand with bounded parallel gateway execs; the ``/fleet`` partial and
the ``/stream`` events serve the in-memory state only.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from .attempt_log import (
    event_names,
    filter_events,
    format_file_mb,
    format_preview,
    level_options,
    paginate,
    read_attempt_log,
)
from .charts import (
    CACHE_READ_SHARE_DEFINITION,
    OUTCOME_LABELS,
    OUTCOME_ORDER,
    SHAPE_GLYPHS,
    TOKEN_CATEGORIES,
    cache_share_per_attempt,
    cost_per_attempt,
    cost_soft_threshold,
    cost_totals,
    cumulative_cost_per_day,
    duration_vs_cost,
    format_tokens,
    missing_token_count,
    outcome_legend,
    outcomes_per_day,
    timeline_points,
    tokens_per_attempt,
    undated_count,
)
from .config import Settings
from .fleet import FleetState, poll_forever
from .gateway import (
    DockerGateway,
    GatewayError,
    validate_attempt_ref,
    validate_log_file,
)
from .history import (
    DEFAULT_WINDOW,
    VALID_WINDOWS,
    format_cost,
    format_duration,
    read_history,
)
from .overview import collect_overview, overview_legend
from .progress import progress_series, read_progress

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
    if gateway is not None:
        gw = gateway
    else:
        gw = DockerGateway(
            timeout=cfg.exec_timeout, log_timeout=cfg.log_exec_timeout
        )

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
        """Fleet table plus the static overview panel.

        The table streams over SSE after every poll cycle; the overview
        panel is static with manual refresh (like the history page):
        attempt summaries for every running instance are read on demand, in
        parallel with a bounded timeout, so one slow instance neither delays
        nor breaks the page.
        """
        rows, error = await fleet.snapshot(poll_interval=cfg.poll_interval)
        window = request.query_params.get("window", DEFAULT_WINDOW)
        if window not in VALID_WINDOWS:
            window = "all"
        wanted = request.query_params.get("instance") or ""
        row = find_row(rows, wanted) if wanted else None
        now_ts = time.time()
        overview = await collect_overview(
            rows,
            gw,
            window=window,
            now=now_ts,
            timeout=cfg.exec_timeout,
            max_concurrent_execs=cfg.max_concurrent_execs,
        )
        outcome_peak = max((inst.shown for inst in overview.rows if inst.ok), default=0)
        if outcome_peak >= 2:
            outcome_ticks = [0, (outcome_peak + 1) // 2, outcome_peak]
        elif outcome_peak == 1:
            outcome_ticks = [0, 1]
        else:
            outcome_ticks = []
        cost_peak = max(
            (inst.cost_total for inst in overview.rows if inst.cost_total is not None),
            default=0.0,
        )
        if cost_peak > 0:
            cost_ticks = [
                (0.0, format_cost(0)),
                (0.5, format_cost(cost_peak / 2)),
                (1.0, format_cost(cost_peak)),
            ]
        else:
            cost_ticks = []
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
                "window": window,
                "overview": overview,
                "legend": overview_legend(overview),
                "chart_keys": list(OUTCOME_ORDER),
                "shape_glyphs": dict(SHAPE_GLYPHS),
                "outcome_peak": outcome_peak,
                "outcome_ticks": outcome_ticks,
                "cost_peak": cost_peak,
                "cost_ticks": cost_ticks,
                "fleet_total_text": format_cost(overview.total_cost),
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
        # Charts count only attempts with a usable summary: runs whose
        # attempt.json is missing or unparsable stay in the table (so the
        # Attempt log stays reachable) but never feed the charts.
        charted = tuple(s for s in result.summaries if s.has_summary)
        buckets = outcomes_per_day(charted, now=now_ts, window=result.window)
        points = timeline_points(charted)
        peak = max((b.total for b in buckets), default=0)
        if peak >= 2:
            y_ticks = [0, (peak + 1) // 2, peak]
        elif peak == 1:
            y_ticks = [0, 1]
        else:
            y_ticks = []
        # X labels stay readable: show at most ~14 day ticks.
        label_every = max(1, -(-len(buckets) // 14))
        cost_points = cost_per_attempt(charted)
        cumulative = cumulative_cost_per_day(
            charted, now=now_ts, window=result.window
        )
        scatter = duration_vs_cost(charted)
        totals = cost_totals(charted)
        threshold = cost_soft_threshold(charted)
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
        token_points = tokens_per_attempt(charted)
        share_points = cache_share_per_attempt(charted)
        token_missing = missing_token_count(charted)
        token_peak = max((p.total for p in token_points), default=0.0)
        if token_peak > 0:
            token_ticks = [
                (0.0, format_tokens(0)),
                (0.5, format_tokens(token_peak / 2)),
                (1.0, format_tokens(token_peak)),
            ]
        else:
            token_ticks = []
        share_ticks = (
            [(0.0, "0%"), (0.5, "50%"), (1.0, "100%")] if share_points else []
        )
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
                "legend": outcome_legend(charted),
                "undated": undated_count(charted),
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
                "token_points": token_points,
                "token_peak": token_peak,
                "token_ticks": token_ticks,
                "token_missing": token_missing,
                "token_categories": list(TOKEN_CATEGORIES),
                "share_points": share_points,
                "share_ticks": share_ticks,
                "share_definition": CACHE_READ_SHARE_DEFINITION,
            },
        )

    @app.get(
        "/instances/{name}/attempts/{issue}/{started}/progress",
        response_class=HTMLResponse,
    )
    async def attempt_progress(
        request: Request, name: str, issue: str, started: str
    ) -> Response:
        """Attempt progress chart: turns and budget over elapsed time.

        Static with manual refresh (no stream subscription): the kept
        events for one attempt are read on demand with a single gateway
        exec, running managed instances only. An unknown instance, a
        stopped instance, an invalid reference, an unreadable log and an
        unrecognised layout each render a clear message with status 200,
        never an error. An attempt without limit readings renders a
        "no progress data" note; an active attempt renders its partial
        chart with a still-running note.
        """

        def progress_page(
            row, error: str | None, result=None, series=None, **extra
        ) -> Response:
            return templates.TemplateResponse(
                request,
                "progress.html",
                {
                    **context(request),
                    "row": row,
                    "name": name,
                    "issue": issue,
                    "started": started,
                    "result": result,
                    "series": series,
                    "error": error,
                    **extra,
                },
            )

        rows, _ = await fleet.snapshot(poll_interval=cfg.poll_interval)
        row = find_row(rows, name)
        if row is None:
            return progress_page(
                None,
                f"Unknown Managed instance {name!r}: it is unknown or was removed.",
            )
        if row.state != "running":
            return progress_page(
                row,
                "Attempt progress is available for running managed instances only.",
            )
        try:
            number, dash_started = validate_attempt_ref(issue, started)
        except ValueError as exc:
            return progress_page(row, f"Invalid attempt reference: {exc}")
        try:
            exec_result = await gw.attempt_events(
                row.container_id, number, dash_started
            )
        except (GatewayError, ValueError) as exc:
            return progress_page(row, f"Could not read the Attempt log: {exc}")
        result = read_progress(
            exec_result.stdout,
            exec_result.returncode,
            issue=number,
            started=dash_started,
        )
        if not result.ok:
            return progress_page(row, result.error, result=result)
        series = progress_series(result)
        handoff_points = []
        if series is not None:
            placed = [m for m in result.handoffs if m.elapsed is not None]
            handoff_points = [
                {
                    "frac": frac,
                    "name": mark.name,
                    "timestamp_text": mark.timestamp_text,
                }
                for mark, frac in zip(placed, series.handoff_fracs)
            ]
        y_ticks = [(0.0, "0"), (0.5, "50%"), (1.0, "100%")]
        return progress_page(
            row,
            None,
            result=result,
            series=series,
            handoff_points=handoff_points,
            y_ticks=y_ticks,
            x_max_text=format_duration(series.x_max) if series is not None else None,
            turns_peak_text=(
                format_tokens(int(series.turns_peak))
                if series is not None and series.turns_peak
                else None
            ),
            budget_peak_text=(
                format_tokens(series.budget_peak)
                if series is not None and series.budget_peak
                else None
            ),
            max_budget_text=(
                format_tokens(result.limits.max_budget_tokens)
                if result.limits.max_budget_tokens
                else None
            ),
            soft_threshold_text=(
                format_tokens(result.limits.soft_threshold_tokens)
                if result.limits.soft_threshold_tokens
                else None
            ),
        )

    @app.get(
        "/instances/{name}/attempts/{issue}/{started}",
        response_class=HTMLResponse,
    )
    async def attempt_log_page(
        request: Request, name: str, issue: str, started: str
    ) -> Response:
        """Attempt log page: header plus the full ordered event stream.

        Static with manual refresh (no stream subscription): one attempt's
        summary and events are read on demand with a single gateway exec,
        running managed instances only. An unknown instance, a stopped
        instance, an invalid reference, an unreadable log and an unrecognised
        layout each render a clear message with status 200, never an error.
        The stream is server-paged at 200 rows.
        """

        def log_page(
            row,
            error: str | None,
            result=None,
            dash_started: str = "",
            page_events=(),
            page_num: int = 1,
            pages: int = 1,
            file_mb_text: str | None = None,
            level_value: str = "",
            event_value: str = "",
            q_value: str = "",
            event_name_options: tuple = (),
            level_option_values: tuple = (),
            filtered_total: int = 0,
            has_filters: bool = False,
            attempt_url: str = "",
            reset_url: str = "",
            refresh_url: str = "",
            prev_url: str | None = None,
            next_url: str | None = None,
        ) -> Response:
            return templates.TemplateResponse(
                request,
                "attempt_log.html",
                {
                    **context(request),
                    "row": row,
                    "name": name,
                    "issue": issue,
                    "started": started,
                    "dash_started": dash_started,
                    "result": result,
                    "error": error,
                    "page_events": page_events,
                    "page_num": page_num,
                    "pages": pages,
                    "file_mb_text": file_mb_text,
                    "level_value": level_value,
                    "event_value": event_value,
                    "q_value": q_value,
                    "event_name_options": event_name_options,
                    "level_option_values": level_option_values,
                    "filtered_total": filtered_total,
                    "has_filters": has_filters,
                    "attempt_url": attempt_url,
                    "reset_url": reset_url,
                    "refresh_url": refresh_url,
                    "prev_url": prev_url,
                    "next_url": next_url,
                },
            )

        def attempt_base_url(dash: str) -> str:
            """The filter-free Attempt log URL: filters live in the query."""
            base = (
                f"/instances/{quote(name, safe='')}"
                f"/attempts/{quote(issue, safe='')}"
            )
            if dash:
                base += f"/{quote(dash, safe='')}"
            return base

        rows, _ = await fleet.snapshot(poll_interval=cfg.poll_interval)
        row = find_row(rows, name)
        if row is None:
            base_url = attempt_base_url("")
            return log_page(
                None,
                f"Unknown Managed instance {name!r}: it is unknown or was removed.",
                attempt_url=base_url,
                reset_url=base_url,
                refresh_url=base_url,
            )
        if row.state != "running":
            base_url = attempt_base_url("")
            return log_page(
                row,
                "Attempt logs are available for running managed instances only.",
                attempt_url=base_url,
                reset_url=base_url,
                refresh_url=base_url,
            )
        try:
            number, dash = validate_attempt_ref(issue, started)
        except ValueError as exc:
            base_url = attempt_base_url("")
            return log_page(
                row,
                f"Invalid attempt reference: {exc}",
                attempt_url=base_url,
                reset_url=base_url,
                refresh_url=base_url,
            )
        base_url = attempt_base_url(dash)
        try:
            exec_result = await gw.attempt_log(row.container_id, number, dash)
        except (GatewayError, ValueError) as exc:
            return log_page(
                row,
                f"Could not read the Attempt log: {exc}",
                dash_started=dash,
                attempt_url=base_url,
                reset_url=base_url,
                refresh_url=base_url,
            )
        result = read_attempt_log(
            exec_result.stdout,
            exec_result.returncode,
            issue=number,
            started=dash,
        )
        if not result.ok:
            return log_page(
                row,
                result.error,
                result=result,
                dash_started=dash,
                file_mb_text=format_file_mb(result.file_size),
                attempt_url=base_url,
                reset_url=base_url,
                refresh_url=base_url,
            )
        params = request.query_params
        raw_level = params.get("level", "")
        raw_event = params.get("event", "")
        raw_q = params.get("q", "")
        # A stripped ``all`` reads as no filtering, so the selects' "All"
        # option and a hand-typed ``?level=all`` agree.
        level_value = raw_level.strip()
        event_value = raw_event.strip()
        q_value = raw_q.strip()
        if level_value.lower() == "all":
            level_value = ""
        if event_value.lower() == "all":
            event_value = ""
        names = event_names(result.events)
        levels = level_options(result.events)
        filtered = filter_events(
            result.events, level=level_value, event=event_value, q=q_value
        )
        page_events, page_num, pages = paginate(
            filtered, params.get("page", "1")
        )

        def page_url(number: int | None) -> str:
            """One paging/refresh link: the active filters plus one page."""
            query: dict[str, str] = {}
            if level_value:
                query["level"] = level_value
            if event_value:
                query["event"] = event_value
            if q_value:
                query["q"] = q_value
            if number is not None:
                query["page"] = str(number)
            text = urlencode(query)
            return base_url if not text else f"{base_url}?{text}"

        return log_page(
            row,
            None,
            result=result,
            dash_started=dash,
            page_events=page_events,
            page_num=page_num,
            pages=pages,
            file_mb_text=format_file_mb(result.file_size),
            level_value=level_value,
            event_value=event_value,
            q_value=q_value,
            event_name_options=names,
            level_option_values=levels,
            filtered_total=len(filtered),
            has_filters=bool(level_value or event_value or q_value),
            attempt_url=base_url,
            reset_url=base_url,
            refresh_url=page_url(page_num if page_num > 1 else None),
            prev_url=page_url(page_num - 1) if page_num > 1 else None,
            next_url=page_url(page_num + 1) if page_num < pages else None,
        )

    @app.get(
        "/instances/{name}/attempts/{issue}/{started}/files/{file}",
        response_class=HTMLResponse,
    )
    async def attempt_file_fragment(
        request: Request, name: str, issue: str, started: str, file: str
    ) -> Response:
        """One Evidence file preview fragment, loaded under its event row.

        The ``open`` (``reply`` for model responses) action on an event row
        fetches this into the slot directly under the row; activating it
        again closes the slot. The file name passes the read allow-list here
        and again inside the gateway: a failure returns 404 without any
        Docker call. Every other problem (unknown instance, stopped
        instance, invalid reference, gateway error, missing file,
        unrecognised tar shape) renders a clear message with status 200,
        never an error. ``?raw=1`` shows the text as read; the default view
        pretty-prints JSON and falls back to raw text past the 256 KiB cap
        or for non-JSON content. All file text is untrusted agent output and
        renders through autoescape only.
        """

        def fragment(error: str | None, status: int = 200, **extra) -> Response:
            return templates.TemplateResponse(
                request,
                "_evidence.html",
                {
                    "request": request,
                    "name": name,
                    "issue": issue,
                    "started": started,
                    "error": error,
                    **extra,
                },
                status_code=status,
            )

        def file_url(dash: str, filename: str, *, raw: bool = False) -> str:
            base = (
                f"/instances/{quote(name, safe='')}"
                f"/attempts/{quote(issue, safe='')}"
                f"/{quote(dash, safe='')}"
                f"/files/{quote(filename, safe='')}"
            )
            return base + ("?raw=1" if raw else "")

        try:
            validate_log_file(file)
        except ValueError:
            return fragment(f"Unknown Evidence file {file!r}.", status=404)
        rows, _ = await fleet.snapshot(poll_interval=cfg.poll_interval)
        row = find_row(rows, name)
        if row is None:
            return fragment(
                f"Unknown Managed instance {name!r}: it is unknown or was removed."
            )
        if row.state != "running":
            return fragment(
                "Attempt logs are available for running managed instances only."
            )
        try:
            number, dash = validate_attempt_ref(issue, started)
        except ValueError as exc:
            return fragment(f"Invalid attempt reference: {exc}")
        raw = request.query_params.get("raw") == "1"
        try:
            result = await gw.read_file(row.container_id, number, dash, file)
        except FileNotFoundError:
            return fragment(
                f"Evidence file {file} was not found in this attempt."
            )
        except (GatewayError, ValueError) as exc:
            return fragment(f"Could not show the Evidence file: {exc}")
        if raw:
            text = result.content.decode("utf-8", errors="replace")
        else:
            text = format_preview(file, result.content, result.truncated)
        return fragment(
            None,
            filename=file,
            size_text=f"{len(result.content)} B",
            total_size=result.total_size,
            truncated=result.truncated,
            raw=raw,
            text=text,
            raw_url=file_url(dash, file, raw=True),
            formatted_url=file_url(dash, file),
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
