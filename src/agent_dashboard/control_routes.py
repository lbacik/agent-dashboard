"""Control console routes (spec section 6.1, read-only slice).

The routes live here rather than in the main app file so later Control
tickets add to this focused module. This slice serves only the console
body; Review/submit/status/track arrive in a later slice, so "Review…"
is not wired up yet.
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .control import (
    ACTION_COMMANDS,
    MISSING,
    ControlMode,
    control_availability,
    control_slug,
)


def _row_availability(row, mode: ControlMode):
    """The console view model for one fleet row (pure function of row + mode)."""
    control = MISSING if row.control_raw_missing else row.control_raw
    return control_availability(
        reach=row.reach,
        control=control,
        mode=mode,
        control_stale=bool(row.detail is not None and row.detail.control_stale),
        control_age_text=row.control_age_text,
    )


def register_control_routes(app: FastAPI, templates: Jinja2Templates) -> None:
    """Add the Control console routes to the app."""

    @app.get("/instances/{name}/control", response_class=HTMLResponse)
    async def instance_control(request: Request, name: str) -> Response:
        """Console body: the "Controls unavailable" line, or the composer.

        The drawer's console slot fetches this on load and on every
        filtered ``sse:fleet`` event. An unknown instance renders a 404
        fragment ("instance not found"), never an error.
        """
        fleet = request.app.state.fleet
        cfg = request.app.state.settings
        mode = getattr(request.app.state, "control_mode", ControlMode(available=True))
        poll_interval = getattr(cfg, "poll_interval", 5.0)
        rows, _ = await fleet.snapshot(poll_interval=poll_interval)
        row = next((r for r in rows if r.name == name), None)
        if row is None:
            return templates.TemplateResponse(
                request,
                "_control.html",
                {"name": name, "slug": control_slug(name), "error": "instance not found"},
                status_code=404,
            )
        availability = _row_availability(row, mode)
        return templates.TemplateResponse(
            request,
            "_control.html",
            {
                "name": name,
                "slug": control_slug(name),
                "av": availability,
                "error": None,
                "review_url": f"/instances/{quote(name, safe='')}/control/review",
                "ACTION_COMMANDS": dict(ACTION_COMMANDS),
            },
        )
