"""Control console routes (spec section 6, review slice).

GET routes serve the console body and the command timeline (read-only).
POST ``/review`` validates the composer's choice and renders the two-step
confirmation; it never runs a control exec and never writes a ``submitted``
entry. Every refusal writes one ``refused`` Control audit entry. Confirm
posts to the submit route, which arrives in a later slice.
"""

from __future__ import annotations

import secrets
from urllib.parse import parse_qs, quote

from fastapi import Request, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .agentctl_reply import supports_expect_repo
from .control import (
    ACTION_COMMANDS,
    ACTION_LABELS,
    MISSING,
    ControlMode,
    command_timeline,
    control_availability,
    control_slug,
    timeline_summary,
)
from .control_audit import append_entry, log_refusal_failure, refused_entry
from .gateway import (
    CONTROL_ACTIONS,
    argv_control,
    validate_control_action,
    validate_control_issue,
    validate_control_n,
    validate_expect_repo,
)

#: Fragment text when the Origin check refuses a POST control route.
ORIGIN_MESSAGE = "missing or mismatched Origin header"

#: Fragment text when the Sec-Fetch-Site check refuses a POST control route.
FETCH_SITE_MESSAGE = "Sec-Fetch-Site must be same-origin"

#: Fragment text when the HX-Request check refuses a POST control route.
HTMX_MESSAGE = "missing HX-Request header"

#: Actions whose confirmation uses the danger tone (spec section 8.3).
DANGER_ACTIONS = frozenset({"stop", "stop_after", "handoff"})


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


def _row_commands(row) -> tuple:
    """The timeline entries for one fleet row (latest good snapshot only)."""
    control = None if row.control_raw_missing else row.control_raw
    return command_timeline(control)


def parse_form(body: bytes) -> dict[str, str]:
    """Parse an ``application/x-www-form-urlencoded`` body (standard library).

    Uses :func:`urllib.parse.parse_qs`; the first value wins per field, so
    a repeated field reads as its first occurrence. Never raises on agent
    or operator input: undecodable bytes are replaced.
    """
    if not body:
        return {}
    parsed = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    return {key: values[0] if values else "" for key, values in parsed.items()}


def check_protection(request: Request, mode: ControlMode) -> tuple[str, str] | None:
    """The request-protection checks for every POST control route (spec 6.2).

    On top of the Host allow-list, in order: ``Origin`` present and equal
    to ``http://`` plus the request's ``Host`` header (host compared
    case-insensitively), ``Sec-Fetch-Site`` ``same-origin`` when present,
    ``HX-Request: true``, and the dashboard control mode available.
    Returns ``(reason, message)`` on refusal, else none. ``reason`` is the
    Control audit slug; ``message`` is the fragment text after its prefix.
    """
    host = (request.headers.get("host") or "").strip()
    origin = request.headers.get("origin")
    if origin is None or origin.strip().lower() != ("http://" + host).lower():
        return ("origin", ORIGIN_MESSAGE)
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None and fetch_site.strip().lower() != "same-origin":
        return ("fetch_site", FETCH_SITE_MESSAGE)
    if request.headers.get("hx-request") != "true":
        return ("htmx_header", HTMX_MESSAGE)
    if not mode.available:
        return ("controls_unavailable", mode.reason or "unavailable")
    return None


def _agent_version(row) -> str | None:
    """The row's agent version, or none when the snapshot never named one."""
    detail = getattr(row, "detail", None)
    version = getattr(detail, "agent_version", None)
    if isinstance(version, str) and version and version != "unavailable":
        return version
    return None


def _repository(row) -> str | None:
    """The row's repository, or none when the snapshot never named one."""
    detail = getattr(row, "detail", None)
    repository = getattr(detail, "repository", None)
    if isinstance(repository, str) and repository and repository != "unavailable":
        return repository
    return None


def _display_command(action: str, *, n: int | None = None, issue: int | None = None) -> str:
    """The human command for the confirmation head and the audit entry."""
    if action == "stop_after":
        return f"stop --after {n}"
    if action == "next_issue":
        return f"next issue {issue}"
    if action == "handoff":
        return "handoff now"
    return action


def _write_refused(entry: dict) -> None:
    """Append one ``refused`` entry; a failed write still returns the refusal."""
    try:
        append_entry(entry)
    except Exception as exc:  # noqa: BLE001 - refusal stands, failure goes to stderr
        log_refusal_failure(str(entry.get("reason") or "refused"), exc)


def register_control_routes(app, templates: Jinja2Templates) -> None:
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
        commands = _row_commands(row)
        return templates.TemplateResponse(
            request,
            "_control.html",
            {
                "name": name,
                "slug": control_slug(name),
                "av": availability,
                "error": None,
                "review_url": f"/instances/{quote(name, safe='')}/control/review",
                "commands_url": f"/instances/{quote(name, safe='')}/control/commands",
                "commands": commands,
                "commands_summary": timeline_summary(commands),
                "ACTION_COMMANDS": dict(ACTION_COMMANDS),
            },
        )

    @app.get("/instances/{name}/control/commands", response_class=HTMLResponse)
    async def instance_control_commands(request: Request, name: str) -> Response:
        """Timeline list plus its collapsed summary out of band (spec 8.5).

        The list element inside the ``<details>`` refreshes from here on
        every ``sse:fleet`` event; the summary updates out of band, so a
        refresh never opens or closes the ``<details>``. An empty
        ``commands`` renders "No commands recorded." An unknown instance
        renders a 404 fragment ("instance not found"). The fragment holds
        no ``<details>`` of its own.
        """
        fleet = request.app.state.fleet
        cfg = request.app.state.settings
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
        commands = _row_commands(row)
        return templates.TemplateResponse(
            request,
            "_control_commands.html",
            {
                "name": name,
                "slug": control_slug(name),
                "commands": commands,
                "commands_summary": timeline_summary(commands),
            },
        )

    def _composer_context(row, name: str, availability) -> dict:
        """The shared template context for every composer render."""
        commands = _row_commands(row)
        return {
            "name": name,
            "slug": control_slug(name),
            "av": availability,
            "error": None,
            "review_error": None,
            "entered_action": None,
            "entered_n": None,
            "entered_issue": None,
            "review_url": f"/instances/{quote(name, safe='')}/control/review",
            "commands_url": f"/instances/{quote(name, safe='')}/control/commands",
            "commands": commands,
            "commands_summary": timeline_summary(commands),
            "ACTION_COMMANDS": dict(ACTION_COMMANDS),
        }

    @app.post("/instances/{name}/control/review", response_class=HTMLResponse)
    async def instance_control_review(request: Request, name: str) -> Response:
        """Step 2: the confirmation for one Control action (spec 6.3).

        Request protection, then validation of ``action``, ``n``, ``issue``
        (422: the composer re-rendered with the message and the entered
        values), then the instance's controls available (403, console line),
        then the action enabled (409: the composer re-rendered with the
        action's disabled reason), then 200 with the confirmation and a
        newly generated request ID. Each refusal writes one ``refused``
        entry; a 200 writes nothing and runs no exec.
        """
        fleet = request.app.state.fleet
        cfg = request.app.state.settings
        mode = getattr(request.app.state, "control_mode", ControlMode(available=True))
        poll_interval = getattr(cfg, "poll_interval", 5.0)
        rows, _ = await fleet.snapshot(poll_interval=poll_interval)
        row = next((r for r in rows if r.name == name), None)

        form = parse_form(await request.body())
        action_raw = form.get("action", "")
        n_raw = form.get("n", "")
        issue_raw = form.get("issue", "")
        candidate_action = (
            action_raw if isinstance(action_raw, str) and action_raw in CONTROL_ACTIONS else None
        )

        protection = check_protection(request, mode)
        if protection is not None:
            reason, message = protection
            if reason == "controls_unavailable":
                fragment = f"Controls unavailable: {message}"
            else:
                fragment = f"Request refused: {message}"
            _write_refused(
                refused_entry(
                    instance=name,
                    reason=reason,
                    error=fragment,
                    container_id=None,
                    expected_repository=None,
                    action=candidate_action,
                    params={},
                    command=candidate_action or "",
                    agent_version=_agent_version(row) if row is not None else None,
                )
            )
            if reason == "controls_unavailable":
                return HTMLResponse(
                    f'<p class="ctl-unavailable">{fragment}</p>', status_code=403
                )
            return HTMLResponse(f'<p class="ctl-error">{fragment}</p>', status_code=403)

        if row is None:
            return templates.TemplateResponse(
                request,
                "_control.html",
                {"name": name, "slug": control_slug(name), "error": "instance not found"},
                status_code=404,
            )

        # Validation of action, n, issue (422).
        action: str | None = None
        n_value: int | None = None
        issue_value: int | None = None
        validation_message: str | None = None
        try:
            action = validate_control_action(action_raw)
        except ValueError:
            validation_message = (
                f"Invalid action: {action_raw!r} "
                "(expected one of stop, stop_after, resume, handoff, next_issue)."
            )
        if validation_message is None and action == "stop_after":
            try:
                n_value = validate_control_n(n_raw)
            except ValueError:
                validation_message = (
                    f"Invalid attempts before stopping: {n_raw!r} (expected 1–99)."
                )
        if validation_message is None and action == "next_issue":
            try:
                issue_value = validate_control_issue(issue_raw)
            except ValueError:
                validation_message = (
                    f"Invalid issue number: {issue_raw!r} (expected 1–2147483647)."
                )
        if validation_message is not None:
            _write_refused(
                refused_entry(
                    instance=name,
                    reason="validation",
                    error=validation_message,
                    container_id=None,
                    expected_repository=None,
                    action=candidate_action,
                    params={},
                    command="",
                    agent_version=_agent_version(row),
                )
            )
            context = _composer_context(row, name, _row_availability(row, mode))
            context["review_error"] = validation_message
            context["entered_action"] = candidate_action
            context["entered_n"] = n_raw
            context["entered_issue"] = issue_raw
            return templates.TemplateResponse(request, "_control.html", context, status_code=422)

        assert action is not None
        params: dict[str, int] = {}
        if action == "stop_after":
            assert n_value is not None
            params = {"n": n_value}
        elif action == "next_issue":
            assert issue_value is not None
            params = {"issue": issue_value}
        command = _display_command(action, n=n_value, issue=issue_value)

        # The instance's controls available (403, console line).
        availability = _row_availability(row, mode)
        if availability.unavailable is not None:
            error = f"Controls unavailable: {availability.unavailable}"
            _write_refused(
                refused_entry(
                    instance=name,
                    reason="action_unavailable",
                    error=error,
                    container_id=None,
                    expected_repository=None,
                    action=action,
                    params=params,
                    command=command,
                    agent_version=_agent_version(row),
                )
            )
            context = _composer_context(row, name, availability)
            return templates.TemplateResponse(
                request, "_control.html", context, status_code=403
            )

        # The action enabled (409: the composer with the disabled reason).
        selected = next((a for a in availability.actions if a.key == action), None)
        if selected is not None and not selected.enabled:
            label = ACTION_LABELS.get(action, action)
            error = f"{label} is unavailable: {selected.reason}"
            _write_refused(
                refused_entry(
                    instance=name,
                    reason="action_unavailable",
                    error=error,
                    container_id=None,
                    expected_repository=None,
                    action=action,
                    params=params,
                    command=command,
                    agent_version=_agent_version(row),
                )
            )
            context = _composer_context(row, name, availability)
            context["review_error"] = error
            context["entered_action"] = action
            context["entered_n"] = n_raw
            context["entered_issue"] = issue_raw
            return templates.TemplateResponse(request, "_control.html", context, status_code=409)

        # 200: the confirmation. No audit entry; nothing is sent yet.
        repository = _repository(row)
        agent_version = _agent_version(row)
        expect_repo: str | None = None
        if repository is not None and supports_expect_repo(agent_version):
            try:
                expect_repo = validate_expect_repo(repository)
            except ValueError:
                expect_repo = None
        request_id = "dash-" + secrets.token_hex(8)
        argv = argv_control(
            row.container_id,
            action,
            request_id=request_id,
            n=n_value,
            issue=issue_value,
            expect_repo=expect_repo,
        )
        tone = "danger" if action in DANGER_ACTIONS else "primary"
        return templates.TemplateResponse(
            request,
            "_control_confirm.html",
            {
                "name": name,
                "slug": control_slug(name),
                "action": action,
                "action_label": ACTION_LABELS.get(action, action),
                "display_command": command,
                "argv_text": " ".join(argv),
                "request_id": request_id,
                "container_id": row.container_id,
                "repository": repository or "unavailable",
                "expect_repo": expect_repo,
                "n_value": n_raw if action == "stop_after" else None,
                "issue_value": issue_raw if action == "next_issue" else None,
                "tone": tone,
                "submit_url": f"/instances/{quote(name, safe='')}/control/submit",
                "console_url": f"/instances/{quote(name, safe='')}/control",
            },
            status_code=200,
        )
