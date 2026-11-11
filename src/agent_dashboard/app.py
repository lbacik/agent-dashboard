"""FastAPI app: the fleet table reads only the in-memory fleet state.

The history page, the drawer statistics card and the lazily loaded
``/overview`` panel read attempt summaries on demand with bounded parallel
gateway execs; ``/``, the ``/fleet`` partial and the ``/stream`` events
serve the in-memory state only, so the main page never reads attempt
summaries on load.
"""

from __future__ import annotations

import asyncio
import json
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
    parse_ts,
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
from .auth import (
    SecurityHeadersMiddleware,
    SignInGateMiddleware,
    register_auth_routes,
    resolve_session,
)
from .breadcrumbs import (
    attempt_progress_trail,
    attempt_trail,
    contract_attempt_trail,
    history_trail,
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
from .contract_attempt import (
    ARTIFACT_PREVIEW_CAP,
    artifact_render_kind,
    fetch_verified_artifact,
    format_artifact_json,
    parse_contract_attempt_export,
    parse_contract_attempts_show,
    parse_contract_events,
    slice_artifact_preview,
    split_ndjson_rows,
    validate_attempt_id_shape,
    validate_contract_attempt_id,
    validate_export_path,
)
from .contract_history import (
    OFFLINE_STARTED_MESSAGE,
    apply_contract_range,
    collect_contract_history,
    collect_contract_history_offline,
    contract_day_strip,
    contract_legend,
    counts_by_label,
    is_instance_running_rejection,
    known_attempt_ids,
)
from .control import compute_control_mode, control_slug
from .control_routes import register_control_routes
from .cost_summary import summarize_cost
from .fleet import FleetState, poll_forever
from .gateway import (
    COMMAND_LOG_RE,
    CONTRACT_PATH,
    EVIDENCE_PREVIEW_CAP,
    ControlTimeoutError,
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
    overview_chart_keys,
    overview_chart_labels,
    overview_legend,
    overview_strip,
    read_full_history,
    summarize_contract_instance,
    summarize_instance,
)
from .presentation import format_age
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
from .sealed_archive_cache import SealedArchiveCache, SealedArchiveKey

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
    fleet,
    render_fleet,
    is_disconnected,
    poll_interval: float | None = None,
    is_signed_in=None,
) -> AsyncIterator[str]:
    """Yield SSE chunks: `retry`, the current table, then one `fleet` event per cycle.

    Ages are rendered at send time from the wall clock. `is_disconnected` is
    the request's disconnect check; when the client goes away the generator
    returns and unsubscribes. A task cancel (server shutdown) propagates
    through the `finally`, which unsubscribes as well. In LAN mode
    `is_signed_in` re-checks the sign-in session before every `fleet` event
    and ends the stream when it has ended (spec 5.6).
    """
    queue = fleet.subscribe()
    try:
        yield f"retry: {SSE_RETRY_MS}\n\n"
        if is_signed_in is not None and not await is_signed_in():
            return
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
            if is_signed_in is not None and not await is_signed_in():
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
    clock=None,
    password_hasher=None,
    sealed_archive_cache: SealedArchiveCache | None = None,
) -> FastAPI:
    """Build the dashboard app.

    ``sealed_archive_cache`` is the host-side Sealed Attempt Archive cache;
    ``None`` disables it (tests). Production passes one built from
    ``Settings`` (see ``cli.main``), so Attempt pages, previews and
    downloads of a cached Sealed Attempt make no Docker call.
    """
    fleet = state if state is not None else FleetState()
    cfg = settings if settings is not None else Settings()
    lan_mode = bool(getattr(cfg, "lan_mode", False))
    control_mode = compute_control_mode(
        control_enabled=getattr(cfg, "control", True),
        host=cfg.host,
        lan_mode=lan_mode,
    )
    if gateway is not None:
        gw = gateway
    else:
        gw = DockerGateway(
            timeout=cfg.exec_timeout,
            log_timeout=cfg.log_exec_timeout,
            control_exec_timeout=cfg.control_exec_timeout,
            contract_export_timeout=cfg.contract_export_timeout,
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.fleet = fleet
        app.state.settings = cfg
        app.state.gateway = gw
        app.state.control_mode = control_mode
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

    from .sign_in_delay import SignInDelay

    auth_clock = clock if clock is not None else time.time
    app = FastAPI(title="agent-dashboard", lifespan=lifespan)
    app.state.fleet = fleet
    app.state.settings = cfg
    app.state.gateway = gw
    app.state.control_mode = control_mode
    app.state.auth_clock = auth_clock
    app.state.sign_in_delay = SignInDelay(clock=auth_clock)
    app.state.password_hasher = password_hasher
    if lan_mode:
        # Startup cleanup (spec 5.6): ended sessions are deleted when the
        # store opens, so a restart never keeps one. A restart itself signs
        # nobody out: live sessions survive because only ended rows go.
        from . import account_store as _account_store

        _cleanup_conn = _account_store.open_store(
            now=auth_clock(),
            idle_timeout=cfg.sign_in_idle_timeout,
            max_age=cfg.sign_in_max_age,
        )
        _cleanup_conn.close()
    extra_hosts = getattr(cfg, "allowed_hosts", frozenset()) or frozenset()
    if getattr(cfg, "host_check", True):
        app.add_middleware(
            HostGuardMiddleware, allowed_hosts=set(ALLOWED_HOSTS) | set(extra_hosts)
        )
    if lan_mode:
        app.add_middleware(SecurityHeadersMiddleware)
        app.add_middleware(SignInGateMiddleware)
    app.mount("/static", RevalidatedStaticFiles(directory=STATIC_DIR), name="static")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    def context(request: Request) -> dict:
        return {
            "request": request,
            "poll_interval": int(cfg.poll_interval),
            "control_available": request.app.state.control_mode.available,
            "lan_mode": lan_mode,
            "operator": getattr(request.state, "operator", None),
        }

    def find_row(rows, name: str):
        """The fleet row for one instance name, or None when unknown/removed."""
        return next((r for r in rows if r.name == name), None)

    def is_contract_row(row) -> bool:
        """True when the row takes the contract path (no legacy widgets)."""
        return row is not None and row.path == CONTRACT_PATH

    #: Message for legacy Attempt-log widgets on a contract-path row.
    CONTRACT_WIDGET_MESSAGE = (
        "Unavailable for contract instances: this instance reports "
        "a contract Status Snapshot, not Attempt logs."
    )

    #: Message for the contract Attempt page on a legacy-path row.
    LEGACY_ATTEMPT_MESSAGE = (
        "Unavailable for legacy instances: this instance reports "
        "Attempt logs, not contract Attempt archives."
    )

    def sealed_cache_key(row, attempt_id: str) -> SealedArchiveKey | None:
        """Cache key for one contract Attempt, or None when the cache is off.

        Entries are keyed by (instance name, backend, repository, Attempt
        ID); the backend prefers the discovery label with the snapshot
        identity as fallback, and the repository comes from the snapshot.
        """
        if sealed_archive_cache is None:
            return None
        contract = getattr(row, "contract", None)
        backend = (
            getattr(row, "backend", None)
            or getattr(contract, "backend", None)
            or ""
        )
        repository = getattr(contract, "repository", None) or ""
        return SealedArchiveKey(
            instance=row.name,
            backend=backend,
            repository=repository,
            attempt_id=attempt_id,
        )

    def cached_attempt_show(key: SealedArchiveKey, attempt_id: str):
        """One cached show, parsed through the same validators as live.

        A corrupt, foreign or unsealed entry reads as a miss (the cache
        drops it); anything else re-parses exactly like an ``attempts show``
        reply, so an outcome without ``sealed`` is never treated as final.
        """
        assert sealed_archive_cache is not None
        raw = sealed_archive_cache.load_show(key)
        if raw is None:
            return None
        attempt_dict, manifest_list = raw
        try:
            text = json.dumps(
                {
                    "contract": "1.0",
                    "ok": True,
                    "result": {"attempt": attempt_dict, "manifest": manifest_list},
                }
            )
        except (ValueError, TypeError):
            return None
        show = parse_contract_attempts_show(
            text.encode("utf-8"), 0, attempt_id=attempt_id
        )
        return show if show.ok else None

    def raw_show_result(stdout: bytes) -> tuple[dict, list] | None:
        """Raw ``(attempt, manifest)`` behind one ``attempts show`` reply.

        The parsed view models lose the raw JSON shape the cache stores, so
        the envelope is read once more here; anything unrecognised reads as
        None and simply skips the cache write.
        """
        try:
            envelope = json.loads(stdout.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(envelope, dict):
            return None
        result = envelope.get("result")
        if not isinstance(result, dict):
            return None
        attempt = result.get("attempt")
        manifest = result.get("manifest")
        if not isinstance(attempt, dict) or not isinstance(manifest, list):
            return None
        return attempt, manifest

    def _offline_lock_for(container_id: str):
        get_lock = getattr(gw, "offline_lock", None)
        if not callable(get_lock):
            return None
        try:
            return get_lock(container_id)
        except ValueError:
            return None

    async def _offline_inspect(row):
        """Inspect a stopped row: ``(info, error)`` with a display message."""
        try:
            info = await gw.inspect_offline(row.container_id)
        except FileNotFoundError:
            return None, (
                "Attempt logs are unavailable for this instance: "
                "its volume is gone or it holds no logs."
            )
        except GatewayError as exc:
            lowered = str(exc).lower()
            if "no such" in lowered or "not found" in lowered:
                return None, (
                    "Attempt logs are unavailable for this instance: "
                    "its volume is gone or it holds no logs."
                )
            return None, f"Could not read the Attempt list: {exc}"
        except (ValueError, Exception) as exc:
            return None, f"Could not read the Attempt list: {exc}"
        if info.running:
            return None, OFFLINE_STARTED_MESSAGE
        return info, None

    async def _offline_history(row, spec, now_ts):
        """Offline ``attempts list`` paging for a stopped row."""
        info, error = await _offline_inspect(row)
        if error is not None:
            return None, error
        assert info is not None
        lock = _offline_lock_for(row.container_id)
        try:
            if lock is not None:
                async with lock:
                    result = await collect_contract_history_offline(
                        gw,
                        info.image,
                        info.mounts,
                        row.exec_user,
                        spec,
                        now=now_ts,
                        cap=cfg.contract_history_cap,
                        instance_name=row.name,
                    )
            else:
                result = await collect_contract_history_offline(
                    gw,
                    info.image,
                    info.mounts,
                    row.exec_user,
                    spec,
                    now=now_ts,
                    cap=cfg.contract_history_cap,
                    instance_name=row.name,
                )
        except FileNotFoundError:
            return None, (
                "Attempt logs are unavailable for this instance: "
                "its volume is gone or it holds no logs."
            )
        except ControlTimeoutError as exc:
            return None, f"Could not read the Attempt list: {exc}"
        except Exception as exc:
            return None, f"Could not read the Attempt list: {exc}"
        if not result.ok:
            return None, result.error or "Attempt list not recognised"
        return result, None

    async def _offline_show(row, attempt_id: str):
        """Offline ``attempts show`` for a stopped row: ``(exec, error)``."""
        info, error = await _offline_inspect(row)
        if error is not None:
            if error == OFFLINE_STARTED_MESSAGE:
                return None, (
                    f"Attempt {attempt_id!r} is unavailable for this instance: "
                    "the instance started while reading: refresh the view."
                )
            if error.startswith("Attempt logs are unavailable"):
                return None, (
                    f"Attempt {attempt_id!r} is unavailable for this instance: "
                    "its volume is gone or it holds no logs."
                )
            return None, f"Could not read the Attempt: {error}"
        assert info is not None
        lock = _offline_lock_for(row.container_id)
        try:
            if lock is not None:
                async with lock:
                    exec_result = await gw.offline_attempt_show(
                        info.image, info.mounts, row.exec_user, attempt_id
                    )
            else:
                exec_result = await gw.offline_attempt_show(
                    info.image, info.mounts, row.exec_user, attempt_id
                )
        except ValueError as exc:
            return None, f"Could not read the Attempt: offline read unavailable: {exc}"
        except ControlTimeoutError as exc:
            return None, f"Could not read the Attempt: {exc}"
        except (GatewayError, Exception) as exc:
            lowered = str(exc).lower()
            if "no such" in lowered or "not found" in lowered:
                return None, (
                    f"Attempt {attempt_id!r} is unavailable for this instance: "
                    "its volume is gone or it holds no logs."
                )
            return None, f"Could not read the Attempt: {exc}"
        try:
            if is_instance_running_rejection(
                exec_result.stdout, exec_result.returncode
            ):
                return None, (
                    f"Attempt {attempt_id!r} is unavailable for this instance: "
                    "the instance started while reading: refresh the view."
                )
        except Exception:
            pass
        return exec_result, None

    def _offline_export_once(row, info, attempt_id: str, path: str):
        """One offline ``attempts export`` call under the instance lock."""

        async def _once():
            lock = _offline_lock_for(row.container_id)
            if lock is not None:
                async with lock:
                    return await gw.offline_attempt_export(
                        info.image, info.mounts, row.exec_user, attempt_id, path
                    )
            return await gw.offline_attempt_export(
                info.image, info.mounts, row.exec_user, attempt_id, path
            )

        return _once

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
                "selected": wanted,
                "ctl_slug": control_slug(row.name) if row is not None else "",
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
            max_concurrent_execs=cfg.max_concurrent_execs,
            contract_cap=cfg.contract_history_cap,
        )
        strip = overview_strip(overview, spec, now_ts) if rows else []
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
                "chart_keys": overview_chart_keys(overview),
                "chart_labels": overview_chart_labels(overview),
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
            request,
            "_drawer.html",
            {
                **context(request),
                "row": row,
                "name": name,
                "ctl_slug": control_slug(name),
            },
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

    def contract_attempt_url(instance: str, attempt_id: str) -> str:
        """The contract Attempt page URL: the opaque ID, URL-encoded."""
        return (
            f"/instances/{quote(instance, safe='')}"
            f"/attempt/{quote(attempt_id, safe='')}"
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

        def contract_page(contract, **extra) -> Response:
            return templates.TemplateResponse(
                request,
                "_activity_contract.html",
                {
                    **context(request),
                    "name": name,
                    "row": row,
                    "summary_id": summary_id,
                    "contract": contract,
                    **extra,
                },
            )

        drawer = row.drawer if row is not None else None
        if is_contract_row(row):
            contract = row.contract if row is not None else None
            current = contract.current if contract is not None else None
            if current is None:
                return contract_page(None, error="No attempt in progress.")
            age_text: str | None = None
            seen = parse_ts(current.last_activity_at)
            if seen is not None:
                age = max(0.0, datetime.now(UTC).timestamp() - seen.timestamp())
                age_text = f"{format_age(age)} ago"
            return contract_page(contract, error=None, last_activity_age=age_text)
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
        """Drawer card plus Cost section: per-instance figures for the range.

        Loaded once when the drawer opens (``hx-trigger="load"``) and again
        on every range change; the drawer's SSE refresh keeps the loaded
        fragment via ``hx-preserve``, so polling never re-reads attempt
        summaries. One range selector drives both: a preset or strip
        selection re-renders the statistics card and the Cost section
        together from the same cut. An explicit ``preset`` (or
        ``from``/``to``) wins and is remembered in a cookie; otherwise the
        cookie names the range, so it survives switching instances and
        reopening the drawer. Unknown instances, gone volumes and unreadable
        summaries render a hint instead of figures, never zero figures.
        Stopped instances read their summaries through a ``docker cp`` tree
        copy, like the history page.
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
            "cost": None,
            "contract": False,
            "capped": False,
            "cap": cfg.contract_history_cap,
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
        if is_contract_row(row):
            # Contract instances page ``attempts list`` (newest first)
            # through the shared contract history reader: online while
            # running, offline through the helper while stopped, so the
            # statistics card shows the same Attempts that explain a Halt.
            # Paging stops at the first Attempt starting before the
            # selected range; outcome charts group by ``outcome.label``
            # (display only) and every Attempt counts as missing cost
            # data, never zero.
            now_ts = time.time()
            if not row.exec_user:
                ctx["error"] = (
                    "Attempt list is unavailable for this instance: "
                    "its exec user is missing."
                )
                return page()
            if row.state != "running":
                full_contract, error = await _offline_history(
                    row, spec, now_ts
                )
                if error is not None:
                    ctx["error"] = error
                    return page()
                assert full_contract is not None
            else:
                full_contract = await collect_contract_history(
                    gw,
                    row.container_id,
                    row.exec_user,
                    spec,
                    now=now_ts,
                    cap=cfg.contract_history_cap,
                    instance_name=name,
                )
                if not full_contract.ok:
                    ctx["error"] = full_contract.error
                    return page()
            cut = apply_contract_range(
                tuple(full_contract.summaries), spec, now_ts
            )
            ctx["inst"] = summarize_contract_instance(
                name=row.name,
                state=row.state,
                container_id=row.container_id,
                reach=row.reach,
                rank=row.rank,
                window="all",
                summaries=cut,
                total=full_contract.total,
                unreadable=full_contract.unreadable,
            )
            counts, labels = counts_by_label(cut)
            ctx["chart_keys"] = sorted(counts, key=lambda k: labels[k])
            ctx["chart_labels"] = dict(labels)
            ctx["strip"] = contract_day_strip(
                full_contract.summaries, spec, now_ts
            )
            ctx["strip_peak"] = max(
                (day.total for day in ctx["strip"]), default=0
            )
            ctx["legend"] = contract_legend(cut)
            ctx["contract"] = True
            ctx["capped"] = full_contract.capped
            ctx["cap"] = full_contract.cap
            return page()
        # Stopped containers cannot run ``docker exec``: their Attempt log
        # list arrives through a ``docker cp`` tree copy parsed on the
        # dashboard side, with the same tolerance rules as the exec reader.
        now_ts = time.time()
        full = await read_full_history(row, gw, now=now_ts)
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
        ctx["cost"] = summarize_cost(cut, full)
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
                    "contract_attempt_url": contract_attempt_url,
                    **extra,
                },
            )

        if row is None:
            return history_page(
                None,
                None,
                f"Unknown Managed instance {name!r}: it is unknown or was removed.",
            )
        if is_contract_row(row):
            # Contract instances page ``attempts list`` (newest first)
            # through the shared history reader: online while running,
            # offline through the helper while stopped. Paging stops at
            # the first Attempt starting before the selected range;
            # ``All`` stops at the configured cap with a visible
            # "showing newest N" note. Malformed or unknown replies
            # render a clear message with status 200, never an error.
            if not row.exec_user:
                return history_page(
                    row,
                    None,
                    "Attempt list is unavailable for this instance: "
                    "its exec user is missing.",
                )
            now_ts = time.time()
            if row.state != "running":
                offline_result, offline_error = await _offline_history(
                    row, spec, now_ts
                )
                if offline_error is not None:
                    return history_page(row, None, offline_error)
                assert offline_result is not None
                contracted = offline_result
            else:
                contracted = await collect_contract_history(
                    gw,
                    row.container_id,
                    row.exec_user,
                    spec,
                    now=now_ts,
                    cap=cfg.contract_history_cap,
                    instance_name=name,
                )
                if not contracted.ok:
                    return history_page(row, None, contracted.error)
            return history_page(
                row,
                contracted,
                None,
                contract=True,
                clegend=contract_legend(contracted.summaries),
                shape_glyphs=dict(SHAPE_GLYPHS),
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
            row,
            error: str | None,
            result=None,
            series=None,
            status: int = 200,
            **extra,
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
                status_code=status,
            )

        rows, _ = await fleet.snapshot(poll_interval=cfg.poll_interval)
        row = find_row(rows, name)
        if row is None:
            return progress_page(
                None,
                f"Unknown Managed instance {name!r}: it is unknown or was removed.",
            )
        if is_contract_row(row):
            return progress_page(row, CONTRACT_WIDGET_MESSAGE, status=404)
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
            status: int = 200,
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
                status_code=status,
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
        if is_contract_row(row):
            base_url = attempt_base_url("")
            return log_page(
                row,
                CONTRACT_WIDGET_MESSAGE,
                attempt_url=base_url,
                reset_url=base_url,
                refresh_url=base_url,
                status=404,
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

    # NOTE: the artifact routes must stay registered before the Attempt
    # page route: its {attempt_id:path} also matches the "/artifact" suffix.
    async def contract_artifact_source(
        name: str, attempt_id: str, path: str
    ) -> tuple:
        """Shared guards, header and manifest lookup for the artifact routes.

        Returns ``(row, show, entry, sealed, message, status, cached_data)``:
        ``message`` is not None when the route should render it instead of
        the artifact (a banner in the preview fragment, plain text for the
        download). ``status`` is 404 for an unknown instance, a legacy
        instance, an invalid reference, an unknown Attempt or an unlisted
        path; anything else renders with status 200, never an error.
        ``cached_data`` carries the verified artifact bytes when the Sealed
        Attempt Archive cache already holds them (no Docker call ran), else
        None. No Docker call runs before the path passes the read
        allow-list.
        """
        rows, _ = await fleet.snapshot(poll_interval=cfg.poll_interval)
        row = find_row(rows, name)
        if row is None:
            return (
                None,
                None,
                None,
                False,
                f"Unknown Managed instance {name!r}: it is unknown or was removed.",
                404,
                None,
            )
        if not is_contract_row(row):
            return (row, None, None, False, LEGACY_ATTEMPT_MESSAGE, 404, None)
        if not row.exec_user:
            return (
                row,
                None,
                None,
                False,
                f"Attempt {attempt_id!r} is unavailable for this instance: "
                "its exec user is missing.",
                200,
                None,
            )
        try:
            validate_attempt_id_shape(attempt_id)
        except ValueError as exc:
            return (row, None, None, False, f"Invalid attempt ID: {exc}", 404, None)
        try:
            validate_contract_attempt_id(attempt_id, known_attempt_ids(name))
        except ValueError:
            return (
                row,
                None,
                None,
                False,
                f"Unknown Attempt {attempt_id!r} for instance {name!r}: "
                "the instance did not list it. Open the Attempt history first.",
                404,
                None,
            )
        try:
            validate_export_path(path)
        except ValueError:
            return (
                row,
                None,
                None,
                False,
                f"Unknown artifact {path!r} for attempt {attempt_id!r}: "
                "the archive manifest does not list it.",
                404,
                None,
            )
        # The Sealed cache serves cached Attempts without starting a
        # helper: running and stopped rows reach the cache first.
        key = sealed_cache_key(row, attempt_id)
        if key is not None:
            assert sealed_archive_cache is not None
            hit = cached_attempt_show(key, attempt_id)
            if hit is not None:
                cached_entry = next(
                    (item for item in hit.manifest if item.path == path), None
                )
                if cached_entry is None:
                    return (
                        row,
                        hit,
                        None,
                        False,
                        f"Unknown artifact {path!r} for attempt {attempt_id!r}: "
                        "the archive manifest does not list it.",
                        404,
                        None,
                    )
                blob = sealed_archive_cache.load_artifact(key, path)
                if blob is not None:
                    return (row, hit, cached_entry, True, None, 200, blob)
                if sealed_archive_cache.load_show(key) is not None:
                    # Header cached but this member not fetched yet: serve
                    # the header without a show call; the export below
                    # stores the new member.
                    return (row, hit, cached_entry, True, None, 200, None)
                # A failed read verification dropped the entry: fall through
                # and fetch again below.
        stopped = row.state != "running"
        if stopped:
            show_exec, show_error = await _offline_show(row, attempt_id)
            if show_error is not None:
                return (row, None, None, False, show_error, 200, None)
            assert show_exec is not None
        else:
            try:
                show_exec = await gw.contract_attempt_show(
                    row.container_id, row.exec_user, attempt_id
                )
            except (GatewayError, ValueError) as exc:
                return (
                    row,
                    None,
                    None,
                    False,
                    f"Could not read the Attempt: {exc}",
                    200,
                    None,
                )
        show = parse_contract_attempts_show(
            show_exec.stdout, show_exec.returncode, attempt_id=attempt_id
        )
        if not show.ok:
            return (
                row,
                None,
                None,
                False,
                show.error,
                404 if show.not_found else 200,
                None,
            )
        entry = next((item for item in show.manifest if item.path == path), None)
        if entry is None:
            return (
                row,
                show,
                None,
                False,
                f"Unknown artifact {path!r} for attempt {attempt_id!r}: "
                "the archive manifest does not list it.",
                404,
                None,
            )
        sealed = show.summary.sealed if show.summary is not None else False
        if key is not None and sealed:
            assert sealed_archive_cache is not None
            raw = raw_show_result(show_exec.stdout)
            if raw is not None:
                sealed_archive_cache.store_show(key, raw[0], raw[1])
        return (row, show, entry, sealed, None, 200, None)

    def contract_artifact_urls(
        instance: str, attempt_id: str, path: str
    ) -> tuple[str, str]:
        """Preview and download URLs for one manifest path (query-addressed)."""
        base = contract_attempt_url(instance, attempt_id)
        query = urlencode({"path": path})
        return f"{base}/artifact?{query}", f"{base}/artifact/download?{query}"

    @app.get(
        "/instances/{name}/attempt/{attempt_id:path}/artifact",
        response_class=HTMLResponse,
    )
    async def contract_artifact_preview(
        request: Request, name: str, attempt_id: str
    ) -> Response:
        """One artifact preview fragment, rendered inline by its media type.

        The member arrives via ``attempts export <attempt-id> --path
        <path>`` and is verified (size and ``sha256``) against the archive
        manifest before anything renders; a torn read of an unsealed archive
        is fetched once more, then reported as still changing. Previews stop
        after 256 KiB and name the manifest's true size. ``application/json``
        renders with a formatted/raw toggle, ``application/x-ndjson`` as
        rows, ``text/*`` as text, and anything else as download only. All
        artifact text is untrusted agent output and renders through
        autoescape only.
        """

        def fragment(error: str | None, status: int = 200, **extra) -> Response:
            return templates.TemplateResponse(
                request,
                "_contract_artifact.html",
                {
                    "request": request,
                    "name": name,
                    "attempt_id": attempt_id,
                    "error": error,
                    **extra,
                },
                status_code=status,
            )

        path = request.query_params.get("path", "")
        raw = request.query_params.get("raw") == "1"
        row, _show, entry, sealed, message, status, cached_data = (
            await contract_artifact_source(name, attempt_id, path)
        )
        if message is not None:
            return fragment(message, status)
        assert row is not None and entry is not None
        preview_url, download_url = contract_artifact_urls(name, attempt_id, path)
        kind = artifact_render_kind(entry.media_type)
        if kind == "binary":
            return fragment(
                None,
                filename=entry.path,
                kind=kind,
                media_type=entry.media_type,
                download_url=download_url,
            )

        if cached_data is not None:
            verified_data = cached_data
        elif row.state != "running":
            info, inspect_error = await _offline_inspect(row)
            if inspect_error is not None:
                return fragment(
                    f"Could not export the Attempt archive: {inspect_error}"
                )
            assert info is not None
            lock = _offline_lock_for(row.container_id)

            async def _once():
                return await gw.offline_attempt_export(
                    info.image, info.mounts, row.exec_user, attempt_id, entry.path
                )

            try:
                if lock is not None:
                    async with lock:
                        verified = await fetch_verified_artifact(
                            _once, entry, sealed=sealed, attempt_id=attempt_id
                        )
                else:
                    verified = await fetch_verified_artifact(
                        _once, entry, sealed=sealed, attempt_id=attempt_id
                    )
            except (GatewayError, ValueError) as exc:
                return fragment(f"Could not export the Attempt archive: {exc}")
            if not verified.ok:
                return fragment(verified.error)
            assert verified.data is not None
            verified_data = verified.data
            if sealed_archive_cache is not None and sealed:
                key = sealed_cache_key(row, attempt_id)
                if key is not None:
                    sealed_archive_cache.store_artifact(key, entry.path, verified_data)
        else:

            async def _once():
                return await gw.contract_attempt_export(
                    row.container_id, row.exec_user, attempt_id, entry.path
                )

            try:
                verified = await fetch_verified_artifact(
                    _once, entry, sealed=sealed, attempt_id=attempt_id
                )
            except (GatewayError, ValueError) as exc:
                return fragment(f"Could not export the Attempt archive: {exc}")
            if not verified.ok:
                return fragment(verified.error)
            assert verified.data is not None
            verified_data = verified.data
            if sealed_archive_cache is not None and sealed:
                key = sealed_cache_key(row, attempt_id)
                if key is not None:
                    sealed_archive_cache.store_artifact(key, entry.path, verified_data)
        preview_bytes, truncated = slice_artifact_preview(verified_data)
        if kind == "json":
            text = (
                preview_bytes.decode("utf-8", errors="replace")
                if raw
                else format_artifact_json(preview_bytes, truncated)
            )
            return fragment(
                None,
                filename=entry.path,
                kind=kind,
                media_type=entry.media_type,
                size_text=f"{len(verified_data)} B",
                cap_text=f"{ARTIFACT_PREVIEW_CAP // 1024} KB",
                total_size=entry.size,
                truncated=truncated,
                raw=raw,
                text=text,
                raw_url=preview_url + "&raw=1",
                formatted_url=preview_url,
                download_url=download_url,
            )
        if kind == "ndjson":
            return fragment(
                None,
                filename=entry.path,
                kind=kind,
                media_type=entry.media_type,
                size_text=f"{len(verified_data)} B",
                cap_text=f"{ARTIFACT_PREVIEW_CAP // 1024} KB",
                total_size=entry.size,
                truncated=truncated,
                rows=split_ndjson_rows(preview_bytes),
                download_url=download_url,
            )
        return fragment(
            None,
            filename=entry.path,
            kind=kind,
            media_type=entry.media_type,
            size_text=f"{len(verified_data)} B",
            cap_text=f"{ARTIFACT_PREVIEW_CAP // 1024} KB",
            total_size=entry.size,
            truncated=truncated,
            text=preview_bytes.decode("utf-8", errors="replace"),
            download_url=download_url,
        )

    @app.get("/instances/{name}/attempt/{attempt_id:path}/artifact/download")
    async def contract_artifact_download(
        request: Request, name: str, attempt_id: str
    ) -> Response:
        """Download one archive member in full, verified, as an attachment.

        The same ``attempts export --path`` read as the inline preview, but
        uncapped; the bytes are verified (size and ``sha256``) against the
        archive manifest before they are served, with the same torn-read
        retry. The file name passes the read allow-list first: a failure
        returns 404 without any Docker call.
        """
        path = request.query_params.get("path", "")
        row, _show, entry, sealed, message, status, cached_data = (
            await contract_artifact_source(name, attempt_id, path)
        )
        if message is not None:
            return PlainTextResponse(message, status_code=status)
        assert row is not None and entry is not None

        if cached_data is not None:
            content = cached_data
        elif row.state != "running":
            info, inspect_error = await _offline_inspect(row)
            if inspect_error is not None:
                return PlainTextResponse(
                    f"Could not export the Attempt archive: {inspect_error}"
                )
            assert info is not None
            lock = _offline_lock_for(row.container_id)

            async def _once():
                return await gw.offline_attempt_export(
                    info.image, info.mounts, row.exec_user, attempt_id, entry.path
                )

            try:
                if lock is not None:
                    async with lock:
                        verified = await fetch_verified_artifact(
                            _once, entry, sealed=sealed, attempt_id=attempt_id
                        )
                else:
                    verified = await fetch_verified_artifact(
                        _once, entry, sealed=sealed, attempt_id=attempt_id
                    )
            except (GatewayError, ValueError) as exc:
                return PlainTextResponse(
                    f"Could not export the Attempt archive: {exc}"
                )
            if not verified.ok:
                return PlainTextResponse(
                    verified.error or "Could not read the artifact."
                )
            assert verified.data is not None
            content = verified.data
            if sealed_archive_cache is not None and sealed:
                key = sealed_cache_key(row, attempt_id)
                if key is not None:
                    sealed_archive_cache.store_artifact(key, entry.path, content)
        else:

            async def _once():
                return await gw.contract_attempt_export(
                    row.container_id, row.exec_user, attempt_id, entry.path
                )

            try:
                verified = await fetch_verified_artifact(
                    _once, entry, sealed=sealed, attempt_id=attempt_id
                )
            except (GatewayError, ValueError) as exc:
                return PlainTextResponse(
                    f"Could not export the Attempt archive: {exc}"
                )
            if not verified.ok:
                return PlainTextResponse(
                    verified.error or "Could not read the artifact."
                )
            assert verified.data is not None
            content = verified.data
            if sealed_archive_cache is not None and sealed:
                key = sealed_cache_key(row, attempt_id)
                if key is not None:
                    sealed_archive_cache.store_artifact(key, entry.path, content)
        filename = entry.path.rsplit("/", 1)[-1] or entry.path

        def chunks():
            """Yield the verified file chunk by chunk to the client."""
            for offset in range(0, len(content), 65536):
                yield content[offset : offset + 65536]

        return StreamingResponse(
            chunks(),
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/instances/{name}/attempt/{attempt_id:path}", response_class=HTMLResponse)
    async def contract_attempt_page(
        request: Request, name: str, attempt_id: str
    ) -> Response:
        """Contract Attempt page: header, archive events and manifest.

        Static with manual refresh (no stream subscription): the header and
        manifest come from ``attempts show <attempt-id>`` while the
        Attempt's Contract Events come from the ``events.jsonl`` member
        (kind ``events``) of its archive, fetched with ``attempts export
        <attempt-id> --path <path>``. An unknown instance, a legacy
        instance, an invalid reference and an unknown Attempt each render
        a clear message with status 404; a gateway failure, a malformed
        reply and an unreadable archive render a clear message with status
        200, never an error. The event stream is server-paged at 200 rows
        with the shared level, event and text filters.
        """

        def page(
            row,
            error: str | None,
            *,
            status: int = 200,
            show=None,
            manifest=(),
            events_error: str | None = None,
            page_events=(),
            page_num: int = 1,
            pages: int = 1,
            level_value: str = "",
            event_value: str = "",
            q_value: str = "",
            event_name_options: tuple = (),
            level_option_values: tuple = (),
            filtered_total: int = 0,
            total_events: int = 0,
            unreadable: int = 0,
            has_filters: bool = False,
            attempt_url: str = "",
            reset_url: str = "",
            refresh_url: str = "",
            prev_url: str | None = None,
            next_url: str | None = None,
        ) -> Response:
            return templates.TemplateResponse(
                request,
                "contract_attempt.html",
                {
                    **context(request),
                    "row": row,
                    "name": name,
                    "attempt_id": attempt_id,
                    "error": error,
                    "show": show,
                    "summary": (
                        show.summary if show is not None and show.ok else None
                    ),
                    "manifest": manifest,
                    "artifact_urls": contract_artifact_urls,
                    "events_error": events_error,
                    "page_events": page_events,
                    "page_num": page_num,
                    "pages": pages,
                    "level_value": level_value,
                    "event_value": event_value,
                    "q_value": q_value,
                    "event_name_options": event_name_options,
                    "level_option_values": level_option_values,
                    "filtered_total": filtered_total,
                    "total_events": total_events,
                    "unreadable": unreadable,
                    "has_filters": has_filters,
                    "attempt_url": attempt_url,
                    "reset_url": reset_url,
                    "refresh_url": refresh_url,
                    "prev_url": prev_url,
                    "next_url": next_url,
                    "breadcrumbs": contract_attempt_trail(name, attempt_id),
                },
                status_code=status,
            )

        def base_url() -> str:
            """The filter-free Attempt URL: filters live in the query."""
            return contract_attempt_url(name, attempt_id)

        rows, _ = await fleet.snapshot(poll_interval=cfg.poll_interval)
        row = find_row(rows, name)
        base = base_url()
        if row is None:
            return page(
                None,
                f"Unknown Managed instance {name!r}: it is unknown or was removed.",
                status=404,
                attempt_url=base,
                reset_url=base,
                refresh_url=base,
            )
        if not is_contract_row(row):
            return page(
                row,
                LEGACY_ATTEMPT_MESSAGE,
                status=404,
                attempt_url=base,
                reset_url=base,
                refresh_url=base,
            )
        if not row.exec_user:
            return page(
                row,
                f"Attempt {attempt_id!r} is unavailable for this instance: "
                "its exec user is missing.",
                attempt_url=base,
                reset_url=base,
                refresh_url=base,
            )
        try:
            validate_attempt_id_shape(attempt_id)
        except ValueError as exc:
            return page(
                row,
                f"Invalid attempt ID: {exc}",
                status=404,
                attempt_url=base,
                reset_url=base,
                refresh_url=base,
            )
        try:
            validate_contract_attempt_id(attempt_id, known_attempt_ids(name))
        except ValueError:
            return page(
                row,
                f"Unknown Attempt {attempt_id!r} for instance {name!r}: "
                "the instance did not list it. Open the Attempt history first.",
                status=404,
                attempt_url=base,
                reset_url=base,
                refresh_url=base,
            )
        key = sealed_cache_key(row, attempt_id)
        show = None
        cached_events: bytes | None = None
        if key is not None:
            assert sealed_archive_cache is not None
            show = cached_attempt_show(key, attempt_id)

        async def _read_show_exec():
            if row.state != "running":
                exec_result, show_error = await _offline_show(row, attempt_id)
                if show_error is not None:
                    raise _ShowReadError(show_error)
                assert exec_result is not None
                return exec_result
            return await gw.contract_attempt_show(
                row.container_id, row.exec_user, attempt_id
            )

        class _ShowReadError(Exception):
            pass

        async def _load_show():
            try:
                show_exec = await _read_show_exec()
            except _ShowReadError as exc:
                return None, str(exc)
            except (GatewayError, ValueError) as exc:
                return None, f"Could not read the Attempt: {exc}"
            parsed = parse_contract_attempts_show(
                show_exec.stdout, show_exec.returncode, attempt_id=attempt_id
            )
            if not parsed.ok:
                return None, (parsed.error, parsed.not_found)
            if (
                key is not None
                and parsed.summary is not None
                and parsed.summary.sealed
            ):
                assert sealed_archive_cache is not None
                raw = raw_show_result(show_exec.stdout)
                if raw is not None:
                    sealed_archive_cache.store_show(key, raw[0], raw[1])
            return parsed, None

        if show is None:
            loaded, load_error = await _load_show()
            if loaded is None:
                assert load_error is not None
                if isinstance(load_error, tuple):
                    message, not_found = load_error
                    return page(
                        row,
                        message,
                        status=404 if not_found else 200,
                        attempt_url=base,
                        reset_url=base,
                        refresh_url=base,
                    )
                return page(
                    row,
                    load_error,
                    attempt_url=base,
                    reset_url=base,
                    refresh_url=base,
                )
            show = loaded
        else:
            assert sealed_archive_cache is not None and key is not None
            cached_entry = next(
                (entry for entry in show.manifest if entry.kind == "events"), None
            )
            assert cached_entry is not None
            cached_events = sealed_archive_cache.load_artifact(key, cached_entry.path)
            if cached_events is None and sealed_archive_cache.load_show(key) is None:
                # A failed read verification dropped the entry: fall back to
                # a read below (online or offline, like above).
                show = None
        if show is None:
            loaded, load_error = await _load_show()
            if loaded is None:
                assert load_error is not None
                if isinstance(load_error, tuple):
                    message, not_found = load_error
                    return page(
                        row,
                        message,
                        status=404 if not_found else 200,
                        attempt_url=base,
                        reset_url=base,
                        refresh_url=base,
                    )
                return page(
                    row,
                    load_error,
                    attempt_url=base,
                    reset_url=base,
                    refresh_url=base,
                )
            show = loaded
        events_entry = next(
            (entry for entry in show.manifest if entry.kind == "events"), None
        )
        assert events_entry is not None
        events_error: str | None = None
        events: tuple = ()
        unreadable = 0
        if cached_events is not None:
            parsed = parse_contract_events(cached_events)
            if not parsed.ok:
                events_error = parsed.error
            else:
                events = parsed.events
                unreadable = parsed.unreadable
        else:
            try:
                if row.state != "running":
                    info, inspect_error = await _offline_inspect(row)
                    if inspect_error is not None:
                        raise GatewayError(inspect_error)
                    assert info is not None
                    lock = _offline_lock_for(row.container_id)
                    if lock is not None:
                        async with lock:
                            export_exec = await gw.offline_attempt_export(
                                info.image,
                                info.mounts,
                                row.exec_user,
                                attempt_id,
                                events_entry.path,
                            )
                    else:
                        export_exec = await gw.offline_attempt_export(
                            info.image,
                            info.mounts,
                            row.exec_user,
                            attempt_id,
                            events_entry.path,
                        )
                    try:
                        if is_instance_running_rejection(
                            export_exec.stdout, export_exec.returncode
                        ):
                            raise GatewayError(
                                "the instance started while reading: "
                                "refresh the view."
                            )
                    except GatewayError:
                        raise
                    except Exception:
                        pass
                else:
                    export_exec = await gw.contract_attempt_export(
                        row.container_id, row.exec_user, attempt_id, events_entry.path
                    )
            except (GatewayError, ValueError) as exc:
                events_error = f"Could not export the Attempt archive: {exc}"
            else:
                export = parse_contract_attempt_export(
                    export_exec.stdout,
                    export_exec.returncode,
                    attempt_id=attempt_id,
                    wanted=events_entry.path,
                )
                if not export.ok:
                    events_error = export.error
                else:
                    assert export.data is not None
                    parsed = parse_contract_events(export.data)
                    if not parsed.ok:
                        events_error = parsed.error
                    else:
                        events = parsed.events
                        unreadable = parsed.unreadable
                        if (
                            key is not None
                            and show.summary is not None
                            and show.summary.sealed
                        ):
                            assert sealed_archive_cache is not None
                            sealed_archive_cache.store_artifact(
                                key, events_entry.path, export.data
                            )
        if events_error is not None:
            return page(
                row,
                None,
                show=show,
                manifest=show.manifest,
                events_error=events_error,
                total_events=len(events),
                unreadable=unreadable,
                attempt_url=base,
                reset_url=base,
                refresh_url=base,
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
        names = event_names(events)
        levels = level_options(events)
        filtered = filter_events(
            events, level=level_value, event=event_value, q=q_value
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
            return base if not text else f"{base}?{text}"

        return page(
            row,
            None,
            show=show,
            manifest=show.manifest,
            page_events=page_events,
            page_num=page_num,
            pages=pages,
            level_value=level_value,
            event_value=event_value,
            q_value=q_value,
            event_name_options=names,
            level_option_values=levels,
            filtered_total=len(filtered),
            total_events=len(events),
            unreadable=unreadable,
            has_filters=bool(level_value or event_value or q_value),
            attempt_url=base,
            reset_url=base,
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
        if is_contract_row(row):
            return fragment(CONTRACT_WIDGET_MESSAGE, status=404)
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
        if is_contract_row(row):
            return PlainTextResponse(CONTRACT_WIDGET_MESSAGE, status_code=404)
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
    @app.get("/partials/fleet", response_class=HTMLResponse)
    async def fleet_partial(request: Request) -> Response:
        rows, error = await fleet.snapshot(poll_interval=cfg.poll_interval)
        selected = request.query_params.get("instance") or ""
        return templates.TemplateResponse(
            request,
            "_fleet_table.html",
            {**context(request), "rows": rows, "error": error, "selected": selected},
        )

    def render_fleet(rows, error) -> str:
        """Re-render the whole table body; ages are stamped at send time."""
        template = templates.get_template("_fleet_table.html")
        return template.render(rows=rows, error=error, selected="")

    @app.get("/stream")
    async def stream(request: Request) -> StreamingResponse:
        """Server-sent events: a `fleet` event with the whole table after every cycle.

        Ages are rendered at send time from the wall clock. EventSource
        reconnects by itself after a dashboard restart; the `retry:` directive
        sets the reconnect delay. Disconnects and shutdown cancel the
        generator, whose `finally` unsubscribes the client. In LAN mode the
        session is re-checked before every `fleet` event and the stream ends
        when it has ended (spec 5.6).
        """

        async def signed_in() -> bool:
            """Re-check the current session; refreshes activity (spec 5.6)."""
            return await resolve_session(request) is not None

        async def event_generator() -> AsyncIterator[str]:
            async for chunk in fleet_event_stream(
                fleet,
                render_fleet,
                request.is_disconnected,
                cfg.poll_interval,
                is_signed_in=signed_in if lan_mode else None,
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

    register_control_routes(app, templates)
    if lan_mode:
        register_auth_routes(app, templates)

    return app
