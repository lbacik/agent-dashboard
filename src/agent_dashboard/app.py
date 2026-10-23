"""FastAPI app: the fleet table reads only the in-memory fleet state.

The history page, the drawer statistics card and the lazily loaded
``/overview`` panel read attempt summaries on demand with bounded parallel
gateway execs; ``/``, the ``/fleet`` partial and the ``/stream`` events
serve the in-memory state only, so the main page never reads attempt
summaries on load.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from .activity import (
    call_preview,
    elapsed_text,
    find_activity,
    result_preview,
)
from .attempt_log import (
    event_names,
    filter_events,
    format_file_mb,
    format_preview,
    level_options,
    paginate,
    pair_tool_calls,
    paired_files,
    read_attempt_log,
    read_attempt_tail_log,
    read_stopped_attempt_log,
)
from .breadcrumbs import attempt_progress_trail, attempt_trail, history_trail
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
    COMMAND_LOG_RE,
    EVIDENCE_PREVIEW_CAP,
    DockerGateway,
    GatewayError,
    validate_attempt_ref,
    validate_log_file,
)
from .history import (
    DEFAULT_WINDOW,
    format_cost,
    format_duration,
    read_history,
    read_stopped_history,
)
from .overview import (
    collect_overview,
    overview_legend,
    read_full_history,
    summarize_instance,
)
from .progress import progress_series, read_progress, read_stopped_progress
from .range_stats import (
    COOKIE_MAX_AGE,
    COOKIE_NAME,
    FLEET_COOKIE_NAME,
    apply_range,
    day_strip,
    parse_cookie,
    parse_range,
)
from .range_stats import (
    PRESETS as RANGE_PRESETS,
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


class RevalidatedStaticFiles(StaticFiles):
    """Static files the browser revalidates on every load.

    `no-cache` keeps the ETag round trip (a cheap 304 when unchanged) but
    stops heuristic caching from serving a stale stylesheet or script after
    a deploy.
    """

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


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
    app.mount("/static", RevalidatedStaticFiles(directory=STATIC_DIR), name="static")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    def context(request: Request) -> dict:
        return {"request": request, "poll_interval": int(cfg.poll_interval)}

    def find_row(rows, name: str):
        """The fleet row for one instance name, or None when unknown/removed."""
        return next((r for r in rows if r.name == name), None)

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> Response:
        """Fleet table plus the instance drawer.

        The table streams over SSE after every poll cycle and reads only
        the in-memory fleet state: unlike the history page, the main page
        never reads attempt summaries, so one slow instance cannot delay it.
        The overview panel above the table loads separately from
        ``/overview`` once it is visible.
        """
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

    @app.get("/overview", response_class=HTMLResponse)
    async def fleet_overview(request: Request) -> Response:
        """Fleet overview panel: outcomes and cost per instance for a range.

        Loaded lazily by the fleet page (``/`` itself never reads attempt
        summaries) and on every range change. The range selector is the
        drawer card's: presets plus a fleet-wide 90-day activity strip.
        An explicit ``preset`` (or ``from``/``to``) wins and is remembered
        in its own cookie, so the fleet range and the drawer range stay
        independent. Every instance is read in parallel with a bounded
        timeout; unreadable ones are listed and left out of the totals.
        """
        params = request.query_params
        if "preset" in params or ("from" in params and "to" in params):
            spec = parse_range(params)
            remember = True
        else:
            spec = parse_cookie(request.cookies.get(FLEET_COOKIE_NAME))
            remember = False
        rows, _ = await fleet.snapshot(poll_interval=cfg.poll_interval)
        now_ts = time.time()
        overview = await collect_overview(
            rows,
            gw,
            spec=spec,
            now=now_ts,
            timeout=cfg.exec_timeout,
            max_concurrent_execs=cfg.max_concurrent_execs,
        )
        strip = day_strip(overview.summaries, spec, now_ts) if rows else []
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
        response = templates.TemplateResponse(
            request,
            "_overview.html",
            {
                **context(request),
                "spec": spec,
                "presets": list(RANGE_PRESETS),
                "overview": overview,
                "legend": overview_legend(overview),
                "chart_keys": list(OUTCOME_ORDER),
                "shape_glyphs": dict(SHAPE_GLYPHS),
                "strip": strip,
                "strip_peak": max((day.total for day in strip), default=0),
                "outcome_peak": outcome_peak,
                "outcome_ticks": outcome_ticks,
                "cost_peak": cost_peak,
                "cost_ticks": cost_ticks,
                "fleet_total_text": format_cost(overview.total_cost),
            },
        )
        if remember:
            response.set_cookie(
                FLEET_COOKIE_NAME,
                spec.query,
                path="/",
                samesite="lax",
                max_age=COOKIE_MAX_AGE,
            )
        return response

    @app.get("/instances/{name}", response_class=HTMLResponse)
    async def instance_drawer(request: Request, name: str) -> Response:
        """Instance drawer fragment, refreshed on every `fleet` SSE event.

        A click on a table row fetches this into `#drawer-holder`; the
        drawer then keeps its `<aside>` and swaps only the inner content
        (`hx-target="this"` with `hx-select=".drawer-inner"`), so the
        scroll position survives the refresh. An unknown or removed
        instance renders the gone fragment (status 200) so a refresh can
        also bring the drawer back. An empty fleet is transient (the
        dashboard just started, or a discovery cycle came back empty):
        status 204 performs no swap, so an open drawer keeps its last
        snapshot instead of flipping into "gone" and back.
        """
        rows, _ = await fleet.snapshot(poll_interval=cfg.poll_interval)
        row = find_row(rows, name)
        if row is None and not rows:
            return Response(status_code=204)
        return templates.TemplateResponse(
            request, "_drawer.html", {**context(request), "row": row, "name": name}
        )

    # Evidence files never change once written, so the Activity widget
    # caches their previews; bounded to keep memory small.
    ACTIVITY_EVIDENCE_CAP = 16 * 1024
    activity_cache: dict[tuple[str, int, str, str], tuple[bytes, bool]] = {}

    async def activity_evidence(
        container_id: str, number: int, dash: str, file: str
    ) -> tuple[bytes, bool]:
        key = (container_id, number, dash, file)
        if key not in activity_cache:
            result = await gw.read_file(
                container_id, number, dash, file, cap=ACTIVITY_EVIDENCE_CAP
            )
            if len(activity_cache) > 256:
                activity_cache.clear()
            activity_cache[key] = (result.content, result.truncated)
        return activity_cache[key]

    def attempt_log_url(name: str, number: int, dash: str) -> str:
        return (
            f"/instances/{quote(name, safe='')}"
            f"/attempts/{quote(str(number), safe='')}"
            f"/{quote(dash, safe='')}"
        )

    @app.get("/instances/{name}/activity", response_class=HTMLResponse)
    async def instance_activity(request: Request, name: str) -> Response:
        """Drawer Activity widget: what the agent is doing right now.

        Reads the active attempt's Attempt log tail (one exec) and at most
        three Evidence files (cached), then renders ``Now`` (the tool call
        running after the latest ``model_response``, or the model working
        otherwise) plus the latest paired call and its response. The tail
        holds the newest events with file line numbers, so the widget works
        even past the head read cap. Refreshed on every poll cycle; the
        drawer's refresh keeps the loaded widget via ``hx-preserve``, so
        there is no flicker and no ``Loading…`` again. The reference comes
        from the control section's active attempt, so the widget stays
        visible through the publishing and handoff phases when the telemetry
        ``current_attempt`` section is already ``n/a``.
        """
        slot = "activity-" + name.replace(" ", "-").replace(".", "-")
        summary_id = slot + "-sum"
        rows, _ = await fleet.snapshot(poll_interval=cfg.poll_interval)
        row = find_row(rows, name)

        def page(**extra) -> Response:
            return templates.TemplateResponse(
                request,
                "_activity.html",
                {
                    **context(request),
                    "name": name,
                    "row": row,
                    "summary_id": summary_id,
                    **extra,
                },
            )

        drawer = row.drawer if row is not None else None
        if row is None or row.state != "running" or drawer is None:
            return page(error="Instance is not running.")
        issue = (
            drawer.issue_number
            if drawer.issue_number is not None
            else drawer.active_issue
        )
        started = drawer.active_started
        if issue is None or not started:
            return page(error="No active attempt.")
        try:
            number, dash = validate_attempt_ref(issue, started)
            exec_result = await gw.attempt_tail_log(row.container_id, number, dash)
        except (GatewayError, ValueError) as exc:
            return page(error=f"Could not read the Attempt log: {exc}")
        log = read_attempt_tail_log(
            exec_result.stdout, exec_result.returncode, issue=number, started=dash
        )
        log_url = attempt_log_url(name, number, dash)
        if not log.ok:
            return page(error=log.error or "layout not recognised", log_url=log_url)
        activity = find_activity(log.events)
        now = datetime.now(UTC)
        view: dict = {
            "activity": activity,
            "log_url": log_url,
            "error": None,
        }
        try:
            if activity.state == "tool":
                current = activity.current[-1]
                content, _ = await activity_evidence(
                    row.container_id, number, dash, current.evidence.file
                )
                view["cur_head"], view["cur_body"] = call_preview(
                    current.evidence.tool, content
                )
                view["cur_elapsed"] = elapsed_text(
                    current.timestamp_text, now=now
                )
            elif activity.state == "model" and activity.since is not None:
                view["since_elapsed"] = elapsed_text(
                    activity.since.timestamp_text, now=now
                )
            if activity.last_call is not None and activity.last_result is not None:
                call_content, _ = await activity_evidence(
                    row.container_id, number, dash, activity.last_call.evidence.file
                )
                res_content, res_truncated = await activity_evidence(
                    row.container_id, number, dash, activity.last_result.evidence.file
                )
                view["call_head"], view["call_body"] = call_preview(
                    activity.last_call.evidence.tool, call_content
                )
                view["res_head"], view["res_body"], view["res_error"] = (
                    result_preview(
                        activity.last_call.evidence.tool, res_content, res_truncated
                    )
                )
                view["call_took"] = elapsed_text(
                    activity.last_call.timestamp_text,
                    activity.last_result.timestamp_text,
                )
                view["call_ago"] = elapsed_text(
                    activity.last_result.timestamp_text, now=now
                )
            if activity.last_message is not None:
                message, _ = await activity_evidence(
                    row.container_id, number, dash, activity.last_message.evidence.file
                )
                view["message"] = (
                    message.decode("utf-8", errors="replace").strip()[:400]
                )
        except (GatewayError, ValueError, FileNotFoundError) as exc:
            view["file_error"] = str(exc)
        return page(**view)

    @app.get("/instances/{name}/stats", response_class=HTMLResponse)
    async def instance_stats(request: Request, name: str) -> Response:
        """Drawer card: per-instance attempt statistics for the active range.

        Loaded once when the drawer opens (``hx-trigger="load"``) and again
        on every range change; the drawer's SSE refresh keeps the loaded
        card via ``hx-preserve``, so polling never re-reads attempt
        summaries. An explicit ``preset`` (or ``from``/``to``) wins and is
        remembered in a cookie; otherwise the cookie names the range, so it
        survives switching instances and reopening the drawer. Unknown
        instances, gone volumes and unreadable summaries render a hint
        instead of figures, never zero figures. Stopped instances read
        their summaries through a ``docker cp`` tree copy, like the
        history page.
        """
        params = request.query_params
        if "preset" in params or ("from" in params and "to" in params):
            spec = parse_range(params)
            remember = True
        else:
            spec = parse_cookie(request.cookies.get(COOKIE_NAME))
            remember = False
        slot_id = "stats-" + name.replace(" ", "-").replace(".", "-")
        rows, _ = await fleet.snapshot(poll_interval=cfg.poll_interval)
        row = find_row(rows, name)
        ctx: dict = {
            **context(request),
            "name": name,
            "slot_id": slot_id,
            "stats_base": f"/instances/{quote(name, safe='')}/stats?",
            "spec": spec,
            "presets": list(RANGE_PRESETS),
            "chart_keys": list(OUTCOME_ORDER),
            "chart_labels": dict(OUTCOME_LABELS),
            "shape_glyphs": dict(SHAPE_GLYPHS),
            "inst": None,
            "legend": [],
            "strip": [],
            "strip_peak": 0,
            "error": None,
        }

        def page() -> Response:
            response = templates.TemplateResponse(request, "_stats.html", ctx)
            if remember:
                response.set_cookie(
                    COOKIE_NAME,
                    spec.query,
                    path="/",
                    samesite="lax",
                    max_age=COOKIE_MAX_AGE,
                )
            return response

        if row is None:
            ctx["error"] = (
                f"Unknown Managed instance {name!r}: it is unknown or was removed."
            )
            return page()
        # Stopped containers cannot run ``docker exec``: their Attempt log
        # list arrives through a ``docker cp`` tree copy parsed on the
        # dashboard side, with the same tolerance rules as the exec reader.
        now_ts = time.time()
        full = await read_full_history(
            row, gw, now=now_ts, timeout=cfg.exec_timeout
        )
        if isinstance(full, str):
            ctx["error"] = full
            return page()
        cut = apply_range(full, spec, now_ts)
        ctx["inst"] = summarize_instance(
            name=row.name,
            state=row.state,
            container_id=row.container_id,
            reach=row.reach,
            rank=row.rank,
            window="all",
            result=cut,
        )
        ctx["strip"] = day_strip(full.summaries, spec, now_ts)
        ctx["strip_peak"] = max((day.total for day in ctx["strip"]), default=0)
        ctx["legend"] = outcome_legend(cut.summaries)
        return page()

    def _history_spec(params):
        """The history-page range: explicit preset/from-to wins, else legacy window.

        Legacy ``window=7|30|all`` links keep working by mapping onto the
        matching preset; anything unknown means ``all``, mirroring the old
        window fallback.
        """
        if "preset" in params or ("from" in params and "to" in params):
            return parse_range(params)
        window = params.get("window", DEFAULT_WINDOW)
        if window in ("7", "7d"):
            return parse_range({"preset": "7d"})
        if window in ("30", "30d"):
            return parse_range({"preset": "30d"})
        if window in ("24h", "90d", "all"):
            return parse_range({"preset": window})
        return parse_range({"preset": "all"})

    @app.get("/instances/{name}/history", response_class=HTMLResponse)
    async def instance_history(request: Request, name: str) -> Response:
        """Attempt history page: one row per implementation attempt, newest first.

        Static with manual refresh (no stream subscription): the summaries
        for all attempts are read on demand with a single gateway exec for
        running instances, or a ``docker cp`` tree copy for stopped ones.
        The page accepts the drawer's ranges (``preset=24h|7d|30d|90d|all``
        or an inclusive UTC ``from``/``to`` date span) plus the legacy
        ``window=7|30|all`` links; the table and every chart show only the
        selected range. An unknown instance, a gone volume, an unreadable
        log and an unrecognised layout each render a clear message with
        status 200, never an error.
        """
        spec = _history_spec(request.query_params)
        rows, _ = await fleet.snapshot(poll_interval=cfg.poll_interval)
        row = find_row(rows, name)

        def history_page(
            row, result, error: str | None, **extra
        ) -> Response:
            return templates.TemplateResponse(
                request,
                "history.html",
                {
                    **context(request),
                    "row": row,
                    "name": name,
                    "spec": spec,
                    "presets": list(RANGE_PRESETS),
                    "result": result,
                    "error": error,
                    "breadcrumbs": history_trail(name),
                    **extra,
                },
            )

        if row is None:
            return history_page(
                None,
                None,
                f"Unknown Managed instance {name!r}: it is unknown or was removed.",
            )
        # Stopped containers cannot run ``docker exec``: their Attempt log
        # list arrives through a ``docker cp`` tree copy parsed on the
        # dashboard side, with the same tolerance rules as the exec reader.
        # A missing tree (a gone volume, or an instance that never wrote
        # logs) renders a clear message, never an error.
        # Summaries are always read in full (window ``all``) and cut to the
        # selected range, so presets and custom spans share one code path.
        now_ts = time.time()
        if row.state == "running":
            try:
                exec_result = await gw.attempt_history(row.container_id)
            except (GatewayError, ValueError) as exc:
                return history_page(
                    row, None, f"Could not read the Attempt log: {exc}"
                )
            full = read_history(
                exec_result.stdout, exec_result.returncode, now=now_ts, window="all"
            )
        else:
            try:
                tree = await gw.copy_logs(row.container_id)
            except FileNotFoundError:
                return history_page(
                    row,
                    None,
                    "Attempt logs are unavailable for this instance: "
                    "its volume is gone or it holds no logs.",
                )
            except (GatewayError, ValueError) as exc:
                return history_page(
                    row, None, f"Could not read the Attempt log: {exc}"
                )
            full = read_stopped_history(tree, now=now_ts, window="all")
        if not full.ok:
            return history_page(row, None, full.error)
        result = apply_range(full, spec, now_ts)
        # Charts count only attempts with a usable summary: runs whose
        # attempt.json is missing or unparsable stay in the table (so the
        # Attempt log stays reachable) but never feed the charts.
        charted = tuple(s for s in result.summaries if s.has_summary)
        if spec.is_custom:
            assert spec.start is not None and spec.end is not None
            chart_span: tuple[str, str] | None = (
                spec.start.isoformat(),
                spec.end.isoformat(),
            )
            chart_window = "all"
        else:
            chart_span = None
            chart_window = spec.preset
        buckets = outcomes_per_day(
            charted, now=now_ts, window=chart_window, span=chart_span
        )
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
            charted, now=now_ts, window=chart_window, span=chart_span
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
        return history_page(
            row,
            result,
            None,
            **{
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
        exec for running instances, or a ``docker cp`` run copy for
        stopped ones. An unknown instance, a gone volume, an invalid
        reference, an unreadable log and an unrecognised layout each
        render a clear message with status 200, never an error. An
        attempt without limit readings renders a "no progress data" note;
        an active attempt renders its partial chart with a still-running
        note.
        """

        def progress_page(
            row, error: str | None, result=None, series=None, **extra
        ) -> Response:
            if result is not None and result.started_at:
                label_issue: int | str = result.issue
                label_started = result.started_at
            else:
                label_issue = issue
                label_started = started
            try:
                number, dash_started = validate_attempt_ref(issue, started)
                attempt_url = (
                    f"/instances/{quote(name, safe='')}"
                    f"/attempts/{quote(str(number), safe='')}"
                    f"/{quote(dash_started, safe='')}"
                )
            except ValueError:
                attempt_url = (
                    f"/instances/{quote(name, safe='')}"
                    f"/attempts/{quote(str(issue), safe='')}"
                    f"/{quote(str(started), safe='')}"
                )
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
                    "breadcrumbs": attempt_progress_trail(
                        name, label_issue, label_started, attempt_url
                    ),
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
        try:
            number, dash_started = validate_attempt_ref(issue, started)
        except ValueError as exc:
            return progress_page(row, f"Invalid attempt reference: {exc}")
        # Stopped containers cannot run ``docker exec``: their events
        # arrive through a ``docker cp`` run copy parsed on the dashboard
        # side, with the same tolerance rules as the exec reader.
        if row.state == "running":
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
        else:
            try:
                tree = await gw.copy_run(row.container_id, number, dash_started)
            except FileNotFoundError:
                return progress_page(
                    row,
                    "Attempt not found in this instance: "
                    "its volume is gone or the attempt is no longer stored.",
                )
            except (GatewayError, ValueError) as exc:
                return progress_page(row, f"Could not read the Attempt log: {exc}")
            result = read_stopped_progress(
                tree, issue=number, started=dash_started
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
        summary and events are read on demand with a single gateway exec
        for running instances, or a ``docker cp`` run copy for stopped
        ones. An unknown instance, a gone volume, an invalid reference, an
        unreadable log and an unrecognised layout each render a clear
        message with status 200, never an error. The stream is server-paged
        at 200 rows.
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
            pair_files: dict | None = None,
            command_logs: tuple[str, ...] = (),
        ) -> Response:
            if result is not None:
                label_issue: int | str = result.issue
                label_started = result.started_at
            else:
                label_issue = issue
                label_started = started
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
                    "breadcrumbs": attempt_trail(
                        name, label_issue, label_started
                    ),
                    "command_logs": command_logs,
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
                    "pair_files": pair_files or {},
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
        # Stopped containers cannot run ``docker exec``: the header, the
        # event stream and the file listing arrive through a ``docker cp``
        # run copy parsed on the dashboard side, with the same tolerance
        # rules as the exec readers. A missing run directory (a gone
        # volume, or an attempt no longer stored) renders a clear message.
        if row.state == "running":
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
            try:
                run_files = await gw.list_run_files(row.container_id, number, dash)
            except (GatewayError, ValueError, FileNotFoundError):
                run_files = []
            result = read_attempt_log(
                exec_result.stdout,
                exec_result.returncode,
                issue=number,
                started=dash,
            )
        else:
            try:
                tree = await gw.copy_run(row.container_id, number, dash)
            except FileNotFoundError:
                return log_page(
                    row,
                    "Attempt not found in this instance: "
                    "its volume is gone or the attempt is no longer stored.",
                    dash_started=dash,
                    attempt_url=base_url,
                    reset_url=base_url,
                    refresh_url=base_url,
                )
            except (GatewayError, ValueError) as exc:
                return log_page(
                    row,
                    f"Could not read the Attempt log: {exc}",
                    dash_started=dash,
                    attempt_url=base_url,
                    reset_url=base_url,
                    refresh_url=base_url,
                )
            result, run_files = read_stopped_attempt_log(
                tree, issue=number, started=dash
            )
        command_logs = tuple(
            sorted(
                name
                for name in run_files
                if isinstance(name, str) and COMMAND_LOG_RE.fullmatch(name) is not None
            )
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
                command_logs=command_logs,
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
        # FIFO pairing runs over the full stream before filtering and
        # paging, so a pair link works across pages and filters.
        pair_files_map = paired_files(result.events, pair_tool_calls(result.events))
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
            pair_files=pair_files_map,
            command_logs=command_logs,
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
        Docker call. Every other problem (unknown instance,
        invalid reference, gateway error, missing file,
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
        # Evidence files arrive via ``docker cp``, which works on stopped
        # containers too, so no running-instance guard is needed here.
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
            cap_text=f"{EVIDENCE_PREVIEW_CAP // 1024} KB",
            total_size=result.total_size,
            truncated=result.truncated,
            raw=raw,
            text=text,
            raw_url=file_url(dash, file, raw=True),
            formatted_url=file_url(dash, file),
            download_url=file_url(dash, file) + "/download",
        )

    @app.get(
        "/instances/{name}/attempts/{issue}/{started}/files/{file}/download",
    )
    async def attempt_file_download(
        request: Request, name: str, issue: str, started: str, file: str
    ) -> Response:
        """Download one allow-listed file of an attempt in full, as an attachment.

        The same ``read_file`` gateway operation as the inline preview but
        with no cap, sent to the client as a chunked stream. The file name
        passes the read allow-list first: a failure returns 404 without any
        Docker call. Every other problem (unknown instance,
        invalid reference, gateway error, missing file) renders a
        clear message with status 200, never an error.
        """
        try:
            validate_log_file(file)
        except ValueError:
            return PlainTextResponse(
                f"Unknown Evidence file {file!r}.", status_code=404
            )
        rows, _ = await fleet.snapshot(poll_interval=cfg.poll_interval)
        row = find_row(rows, name)
        if row is None:
            return PlainTextResponse(
                f"Unknown Managed instance {name!r}: it is unknown or was removed."
            )
        # Evidence files arrive via ``docker cp``, which works on stopped
        # containers too, so no running-instance guard is needed here.
        try:
            number, dash = validate_attempt_ref(issue, started)
        except ValueError as exc:
            return PlainTextResponse(f"Invalid attempt reference: {exc}")
        try:
            result = await gw.read_file(row.container_id, number, dash, file, cap=None)
        except FileNotFoundError:
            return PlainTextResponse(
                f"Evidence file {file} was not found in this attempt."
            )
        except (GatewayError, ValueError) as exc:
            return PlainTextResponse(f"Could not download the Evidence file: {exc}")
        content = result.content

        def chunks():
            """Yield the uncapped file chunk by chunk to the client."""
            for offset in range(0, len(content), 65536):
                yield content[offset : offset + 65536]

        return StreamingResponse(
            chunks(),
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{file}"',
                "X-Content-Type-Options": "nosniff",
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
