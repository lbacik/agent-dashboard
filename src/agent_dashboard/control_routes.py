"""Control console routes (spec section 6, review/submit/track/status).

GET routes serve the console body and the command timeline (read-only).
POST ``/review`` validates the composer's choice and renders the two-step
confirmation; it never runs a control exec and never writes a ``submitted``
entry. POST ``/submit`` sends the Control action and returns one feedback
card. Every submit/review refusal writes one ``refused`` Control audit
entry. GET ``/track`` re-reads one accepted card from the latest good
snapshot (no Docker call) and POST ``/status`` re-renders it from
``agentctl command <id>``; neither writes a Control audit entry.
"""

from __future__ import annotations

import secrets
import sys
import time
from urllib.parse import parse_qs, quote, urlencode

from fastapi import Request, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .agentctl_reply import (
    ControlReply,
    parse_command_reply,
    parse_control_reply,
    reply_for_exception,
    supports_expect_repo,
)
from .control import (
    ACTION_COMMANDS,
    ACTION_LABELS,
    MISSING,
    ControlMode,
    FollowResult,
    command_timeline,
    control_availability,
    control_slug,
    follow_command,
    timeline_summary,
)
from .control_audit import (
    append_entry,
    audit_path,
    log_refusal_failure,
    outcome_entry,
    refused_entry,
    submitted_entry,
)
from .gateway import (
    CONTROL_ACTIONS,
    ControlBusyError,
    ControlTimeoutError,
    DockerNotAvailableError,
    argv_control,
    validate_container_id,
    validate_control_action,
    validate_control_issue,
    validate_control_n,
    validate_expect_repo,
    validate_request_id,
)

#: Fragment text when the Origin check refuses a POST control route.
ORIGIN_MESSAGE = "missing or mismatched Origin header"

#: Fragment text when the Sec-Fetch-Site check refuses a POST control route.
FETCH_SITE_MESSAGE = "Sec-Fetch-Site must be same-origin"

#: Fragment text when the HX-Request check refuses a POST control route.
HTMX_MESSAGE = "missing HX-Request header"

#: Actions whose confirmation uses the danger tone (spec section 8.3).
DANGER_ACTIONS = frozenset({"stop", "stop_after", "handoff"})

#: 409 fragment when the submit-time target binding fails (spec 5.3).
STALE_BINDING_MESSAGE = (
    "Instance replaced or unavailable: refresh the view. Nothing was sent."
)

#: 409 fragment when another control exec is in flight (spec 6.3).
BUSY_MESSAGE = (
    "Another command is being sent to this instance. "
    "Wait for its result, then try again. Nothing was sent."
)

#: Note appended to a card whose outcome audit entry could not be written.
OUTCOME_WRITE_NOTE = "The Control audit outcome entry could not be written."

#: Outcome unknown card body (spec 8.4).
UNKNOWN_BODY = "No reply from the agent. The command may or may not have been applied."

#: Usage-error Not sent card body and audit error (spec 8.4).
USAGE_ERROR_BODY = (
    "The agent's agentctl did not accept the dashboard's arguments; "
    "nothing was sent. This is a dashboard bug or a version mismatch."
)

#: Not recorded card body (spec 8.4): the command was never committed.
NOT_RECORDED_BODY = (
    "Not recorded: the command was not applied. "
    "Retry with the same request ID to send it."
)


def render_card(
    templates: Jinja2Templates,
    *,
    command: str,
    tone: str,
    title: str,
    body: str,
    request_id: str | None,
    sequence: int | None = None,
    note: str | None = None,
    outcome_note: str | None = None,
    progress: str | None = None,
    track_url: str | None = None,
    retry_fields: dict | None = None,
    status_fields: dict | None = None,
) -> str:
    """One feedback card fragment (agent text goes through autoescape).

    A card with ``track_url`` keeps following its command: it re-requests
    ``track`` on every ``sse:fleet`` event and its meta shows
    "following…". Shared by the submit, status and track routes.
    """
    return templates.get_template("_control_card.html").render(
        {
            "command": command,
            "tone": tone,
            "title": title,
            "body": body,
            "note": note,
            "outcome_note": outcome_note,
            "progress": progress,
            "track_url": track_url,
            "request_id": request_id,
            "sequence": sequence,
            "retry_fields": retry_fields,
            "status_fields": status_fields,
        }
    )


def track_url_for(
    name: str,
    *,
    action: str,
    n_value: int | None,
    issue_value: int | None,
    container_id: str,
    repository: str,
    request_id: str,
    sequence: int | None,
) -> str:
    """The ``track`` URL that re-reads one accepted card (spec 8.4).

    The card's values travel as query parameters, plus the sequence from
    the accepted reply, so the stateless ``track`` route can locate the
    command in the latest good snapshot.
    """
    params: dict[str, str] = {
        "action": action,
        "container_id": container_id,
        "repository": repository,
        "request_id": request_id,
    }
    if n_value is not None:
        params["n"] = str(n_value)
    if issue_value is not None:
        params["issue"] = str(issue_value)
    if sequence is not None:
        params["sequence"] = str(sequence)
    return f"/instances/{quote(name, safe='')}/control/track?{urlencode(params)}"


def _validate_track(query: dict[str, str]) -> tuple[dict | None, str | None]:
    """Validate the ``track`` query parameters (the card's values).

    Returns ``(values, None)`` with the parsed action, parameter, container
    ID, repository, request ID and sequence (none when the accepted reply
    named none), or ``(None, message)`` with the first failure. A failure
    answers 422 and never reaches the snapshot.
    """
    action_raw = query.get("action", "")
    try:
        action = validate_control_action(action_raw)
    except ValueError:
        return None, (
            f"Invalid action: {action_raw!r} "
            "(expected one of stop, stop_after, resume, handoff, next_issue)."
        )
    n_value: int | None = None
    issue_value: int | None = None
    if action == "stop_after":
        try:
            n_value = validate_control_n(query.get("n", ""))
        except ValueError:
            return None, (
                f"Invalid attempts before stopping: {query.get('n', '')!r} "
                "(expected 1–99)."
            )
    elif action == "next_issue":
        try:
            issue_value = validate_control_issue(query.get("issue", ""))
        except ValueError:
            return None, (
                f"Invalid issue number: {query.get('issue', '')!r} "
                "(expected 1–2147483647)."
            )
    try:
        container_id = validate_container_id(query.get("container_id", ""))
    except ValueError:
        return None, f"Invalid container ID: {query.get('container_id', '')!r}."
    try:
        request_id = validate_request_id(query.get("request_id", ""))
    except ValueError:
        return None, (
            f"Invalid request ID: {query.get('request_id', '')!r} "
            "(expected dash- plus 16 hex characters)."
        )
    raw_sequence = query.get("sequence", "")
    sequence: int | None = None
    if raw_sequence not in ("", None):
        try:
            sequence = int(str(raw_sequence))
        except (TypeError, ValueError):
            return None, f"Invalid sequence: {raw_sequence!r} (expected an integer)."
        if sequence < 0:
            return None, f"Invalid sequence: {raw_sequence!r} (expected an integer)."
    return (
        {
            "action": action,
            "n_value": n_value,
            "issue_value": issue_value,
            "container_id": container_id,
            "repository": query.get("repository", ""),
            "request_id": request_id,
            "sequence": sequence,
        },
        None,
    )


def follow_card(
    templates: Jinja2Templates,
    name: str,
    values: dict,
    result: FollowResult,
    *,
    submit_url: str | None = None,
    status_url: str | None = None,
) -> str:
    """Render one tracked card from a :class:`FollowResult` (spec 8.4).

    Following, paused and waiting cards keep their ``sse:fleet`` track
    trigger; the evicted card stops and offers Check status; terminal and
    gone cards stop with no buttons.
    """
    command = _display_command(
        values["action"], n=values["n_value"], issue=values["issue_value"]
    )
    if result.outcome in ("following", "paused", "waiting"):
        return render_card(
            templates,
            command=command,
            tone=result.tone,
            title=result.title,
            body=result.body,
            request_id=values["request_id"],
            sequence=values["sequence"],
            progress=result.progress,
            track_url=track_url_for(
                name,
                action=values["action"],
                n_value=values["n_value"],
                issue_value=values["issue_value"],
                container_id=values["container_id"],
                repository=values["repository"],
                request_id=values["request_id"],
                sequence=values["sequence"],
            ),
        )
    if result.outcome == "evicted" and status_url is not None:
        return render_card(
            templates,
            command=command,
            tone=result.tone,
            title=result.title,
            body=result.body,
            request_id=values["request_id"],
            sequence=values["sequence"],
            status_fields={
                "action": values["action"],
                "n_value": values["n_value"],
                "issue_value": values["issue_value"],
                "container_id": values["container_id"],
                "repository": values["repository"],
                "request_id": values["request_id"],
                "status_url": status_url,
            },
        )
    return render_card(
        templates,
        command=command,
        tone=result.tone,
        title=result.title,
        body=result.body,
        request_id=values["request_id"],
        sequence=values["sequence"],
        progress=result.progress,
    )


def _validate_submit(form: dict[str, str]) -> tuple[dict | None, str | None]:
    """Validate every submit field, including the request ID format.

    Returns ``(values, None)`` with the parsed action, parameter, container
    ID, repository, request ID, retry flag, params and human command, or
    ``(None, message)`` with the first failure in field order. A failure
    answers 422 with a Refused card and a ``refused``/``validation`` entry;
    it never reaches argv.
    """
    action_raw = form.get("action", "")
    n_raw = form.get("n", "")
    issue_raw = form.get("issue", "")
    container_raw = form.get("container_id", "")
    repository_raw = form.get("repository", "")
    request_id_raw = form.get("request_id", "")
    retry_raw = form.get("retry", "")
    try:
        action = validate_control_action(action_raw)
    except ValueError:
        return None, (
            f"Invalid action: {action_raw!r} "
            "(expected one of stop, stop_after, resume, handoff, next_issue)."
        )
    n_value: int | None = None
    issue_value: int | None = None
    if action == "stop_after":
        try:
            n_value = validate_control_n(n_raw)
        except ValueError:
            return None, (
                f"Invalid attempts before stopping: {n_raw!r} (expected 1–99)."
            )
    elif action == "next_issue":
        try:
            issue_value = validate_control_issue(issue_raw)
        except ValueError:
            return None, (
                f"Invalid issue number: {issue_raw!r} (expected 1–2147483647)."
            )
    elif n_raw or issue_raw:
        return None, f"Action {action!r} takes no parameter."
    try:
        container_id = validate_container_id(container_raw)
    except ValueError:
        return None, f"Invalid container ID: {container_raw!r}."
    try:
        repository = validate_expect_repo(repository_raw)
    except ValueError:
        return None, f"Invalid repository: {repository_raw!r} (expected owner/name)."
    try:
        request_id = validate_request_id(request_id_raw)
    except ValueError:
        return None, (
            f"Invalid request ID: {request_id_raw!r} "
            "(expected dash- plus 16 hex characters)."
        )
    if retry_raw in ("", None):
        retry = False
    elif retry_raw == "1":
        retry = True
    else:
        return None, f"Invalid retry flag: {retry_raw!r} (expected 1)."
    params: dict[str, int] = {}
    if action == "stop_after":
        assert n_value is not None
        params = {"n": n_value}
    elif action == "next_issue":
        assert issue_value is not None
        params = {"issue": issue_value}
    return (
        {
            "action": action,
            "n_value": n_value,
            "issue_value": issue_value,
            "container_id": container_id,
            "repository": repository,
            "request_id": request_id,
            "retry": retry,
            "params": params,
            "command": _display_command(action, n=n_value, issue=issue_value),
        },
        None,
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

    @app.post("/instances/{name}/control/submit", response_class=HTMLResponse)
    async def instance_control_submit(request: Request, name: str) -> Response:
        """The Control action: one feedback card (spec 6.3 submit).

        Request protection, then validation of every field including the
        request ID format (422, Refused card), then target binding (409,
        section 5.3), then the ``submitted`` Control audit entry (on write
        failure, 503 Not sent card, no exec), then ``gateway.control`` (a
        busy error gives 409 with the busy message, recorded as the
        outcome ``refused``/``busy``), then reply parsing, then the
        outcome entry, then 200 with the feedback card. The composer and
        the timeline summary come back out of band. A Retry posts here
        with the card's fields and ``retry=1`` and goes through every
        check again. No path returns a 500.
        """
        fleet = request.app.state.fleet
        cfg = request.app.state.settings
        mode = getattr(request.app.state, "control_mode", ControlMode(available=True))
        gateway = request.app.state.gateway
        poll_interval = getattr(cfg, "poll_interval", 5.0)
        rows, _ = await fleet.snapshot(poll_interval=poll_interval)
        row = next((r for r in rows if r.name == name), None)

        form = parse_form(await request.body())
        action_raw = form.get("action", "")
        candidate_action = (
            action_raw if isinstance(action_raw, str) and action_raw in CONTROL_ACTIONS else None
        )

        def body_oob() -> str:
            """The composer plus timeline summary out of band."""
            context = _composer_context(row, name, _row_availability(row, mode))
            context["slug"] = control_slug(name)
            return templates.get_template("_control_body_oob.html").render(context)

        def card(
            *,
            command: str,
            tone: str,
            title: str,
            body: str,
            request_id: str | None,
            sequence: int | None = None,
            note: str | None = None,
            outcome_note: str | None = None,
            progress: str | None = None,
            track_url: str | None = None,
            retry_fields: dict | None = None,
            status_fields: dict | None = None,
        ) -> str:
            """One feedback card fragment (agent text goes through autoescape)."""
            return render_card(
                templates,
                command=command,
                tone=tone,
                title=title,
                body=body,
                request_id=request_id,
                sequence=sequence,
                note=note,
                outcome_note=outcome_note,
                progress=progress,
                track_url=track_url,
                retry_fields=retry_fields,
                status_fields=status_fields,
            )

        def refused_card(message: str, *, request_id: str | None, command: str) -> str:
            """A Refused card for a 403/409/422 submit refusal."""
            return card(
                command=command,
                tone="error",
                title="Refused",
                body=message,
                request_id=request_id,
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
            oob = body_oob() if row is not None else ""
            return HTMLResponse(
                refused_card(
                    fragment,
                    request_id=None,
                    command=candidate_action or action_raw,
                )
                + oob,
                status_code=403,
            )

        if row is None:
            return templates.TemplateResponse(
                request,
                "_control.html",
                {"name": name, "slug": control_slug(name), "error": "instance not found"},
                status_code=404,
            )

        values, validation_message = _validate_submit(form)
        if values is None:
            assert validation_message is not None
            try:
                audit_request_id: str | None = validate_request_id(
                    form.get("request_id", "")
                )
            except ValueError:
                audit_request_id = None
            _write_refused(
                refused_entry(
                    instance=name,
                    reason="validation",
                    error=validation_message,
                    request_id=audit_request_id,
                    container_id=form.get("container_id", ""),
                    expected_repository=form.get("repository", ""),
                    action=candidate_action,
                    params={},
                    command=candidate_action or "",
                    agent_version=_agent_version(row),
                    retry=form.get("retry", "") == "1",
                )
            )
            return HTMLResponse(
                refused_card(
                    validation_message,
                    request_id=audit_request_id,
                    command=candidate_action or action_raw,
                )
                + body_oob(),
                status_code=422,
            )

        action = values["action"]
        n_value = values["n_value"]
        issue_value = values["issue_value"]
        container_id = values["container_id"]
        repository = values["repository"]
        request_id = values["request_id"]
        retry = values["retry"]
        params = values["params"]
        command = values["command"]

        # Target binding (spec 5.3): the fleet still lists the name with the
        # same container ID, Reach is live recomputed server-side, and the
        # latest good snapshot's repository equals the form's.
        if (
            row.container_id != container_id
            or row.reach != "live"
            or _repository(row) != repository
        ):
            _write_refused(
                refused_entry(
                    instance=name,
                    reason="stale_binding",
                    error=STALE_BINDING_MESSAGE,
                    request_id=request_id,
                    container_id=container_id,
                    expected_repository=repository,
                    action=action,
                    params=params,
                    command=command,
                    agent_version=_agent_version(row),
                    retry=retry,
                )
            )
            return HTMLResponse(
                refused_card(
                    STALE_BINDING_MESSAGE, request_id=request_id, command=command
                )
                + body_oob(),
                status_code=409,
            )

        # `--expect-repo` only when the version table allows it; the audit
        # records whether it was passed.
        agent_version = _agent_version(row)
        expect_repo: str | None = None
        if supports_expect_repo(agent_version):
            try:
                expect_repo = validate_expect_repo(repository)
            except ValueError:
                expect_repo = None
        common = {
            "instance": name,
            "request_id": request_id,
            "container_id": container_id,
            "expected_repository": repository,
            "action": action,
            "params": params,
            "command": command,
            "agent_version": agent_version,
            "expect_repo_guard": expect_repo is not None,
            "retry": retry,
        }

        # Fast-path busy check (spec 6.3): another control exec already in
        # flight refuses before the `submitted` entry, so the normal busy
        # path leaves no `submitted`-without-exec pair. The post-`submitted`
        # race below remains for an exec that loses the slot afterwards.
        in_flight = getattr(gateway, "control_in_flight", None)
        if callable(in_flight):
            try:
                claimed = bool(in_flight(row.container_id))
            except Exception:  # noqa: BLE001 - a broken probe never refuses
                claimed = False
            if claimed:
                _write_refused(
                    refused_entry(
                        instance=name,
                        reason="busy",
                        error=BUSY_MESSAGE,
                        request_id=request_id,
                        container_id=container_id,
                        expected_repository=repository,
                        action=action,
                        params=params,
                        command=command,
                        agent_version=agent_version,
                        expect_repo_guard=expect_repo is not None,
                        retry=retry,
                    )
                )
                return HTMLResponse(
                    refused_card(BUSY_MESSAGE, request_id=request_id, command=command)
                    + body_oob(),
                    status_code=409,
                )

        # The `submitted` entry comes before `docker exec`; a failing write
        # means the command is not sent (503, Not sent card, no exec).
        try:
            append_entry(submitted_entry(**common))
        except Exception as exc:  # noqa: BLE001 - fail closed, report the card
            print(f"control-audit submitted write failed: {exc}", file=sys.stderr)
            oob_path = audit_path().parent
            return HTMLResponse(
                card(
                    command=command,
                    tone="error",
                    title="Not sent",
                    body=(
                        "The Control audit could not be written, so the command "
                        f"was not sent. Check the dashboard's state directory ({oob_path})."
                    ),
                    request_id=request_id,
                )
                + body_oob(),
                status_code=503,
            )

        # The gateway exec. A busy slot raises at once (409, recorded as
        # the outcome `refused`/`busy`); timeout and missing Docker map to
        # a reply kind. Anything else is an unknown outcome, never a 500.
        started = time.perf_counter()
        duration_ms: int | None = None
        reply: ControlReply | None = None
        try:
            exec_result = await gateway.control(
                row.container_id,
                action,
                request_id=request_id,
                n=n_value,
                issue=issue_value,
                expect_repo=expect_repo,
            )
        except ControlBusyError:
            try:
                append_entry(
                    outcome_entry(
                        **common, outcome="refused", reason="busy", error=BUSY_MESSAGE
                    )
                )
            except Exception as exc:  # noqa: BLE001 - the refusal still stands
                print(
                    f"control-audit outcome write failed (busy): {exc}",
                    file=sys.stderr,
                )
            return HTMLResponse(
                refused_card(BUSY_MESSAGE, request_id=request_id, command=command)
                + body_oob(),
                status_code=409,
            )
        except (ControlTimeoutError, DockerNotAvailableError) as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            reply = reply_for_exception(exc)
        except Exception as exc:  # noqa: BLE001 - no path returns a 500
            duration_ms = int((time.perf_counter() - started) * 1000)
            reply = ControlReply(
                kind="unknown",
                reason=str(exc)[:2000] or "control exec failed",
                exit_code=None,
            )
        else:
            duration_ms = int((time.perf_counter() - started) * 1000)
            try:
                reply = parse_control_reply(
                    exec_result, expected_repository=repository
                )
            except Exception as exc:  # noqa: BLE001 - the parser never raises
                code = getattr(exec_result, "returncode", None)
                reply = ControlReply(
                    kind="unknown",
                    reason=str(exc)[:2000] or "control exec failed",
                    exit_code=code if isinstance(code, int) else None,
                )
        assert reply is not None

        if reply.kind == "accepted":
            outcome_name = "accepted"
            error: str | None = None
        elif reply.kind == "rejected":
            outcome_name = "rejected"
            error = reply.reason
        elif reply.kind == "usage_error":
            outcome_name = "usage_error"
            error = USAGE_ERROR_BODY
        elif reply.kind == "failed":
            outcome_name = "failed"
            error = (
                f"Docker could not run agentctl: {reply.reason}"
                if reply.reason
                else "Docker could not run agentctl."
            )
        else:
            outcome_name = "unknown"
            error = reply.reason or "no reply from the agent"

        outcome_note: str | None = None
        try:
            append_entry(
                outcome_entry(
                    **common,
                    outcome=outcome_name,
                    exit_code=reply.exit_code,
                    sequence=reply.sequence,
                    acknowledgement=reply.acknowledgement,
                    reply_repository=reply.repository,
                    repository_mismatch=reply.repository_mismatch,
                    error=error,
                    duration_ms=duration_ms,
                )
            )
        except Exception as exc:  # noqa: BLE001 - the card still stands
            print(f"control-audit outcome write failed: {exc}", file=sys.stderr)
            outcome_note = OUTCOME_WRITE_NOTE

        card_fields = {
            "action": action,
            "n_value": form.get("n", "") if action == "stop_after" else None,
            "issue_value": form.get("issue", "") if action == "next_issue" else None,
            "container_id": container_id,
            "repository": repository,
            "request_id": request_id,
        }
        submit_url = f"/instances/{quote(name, safe='')}/control/submit"
        status_url = f"/instances/{quote(name, safe='')}/control/status"

        if reply.kind == "accepted" and reply.repository_mismatch:
            feedback = card(
                command=command,
                tone="error",
                title="Repository mismatch",
                body=(
                    f"The agent applied the command to {reply.repository or '—'}, "
                    f"but this view showed {repository}. Nothing was undone."
                ),
                request_id=request_id,
                sequence=reply.sequence,
                outcome_note=outcome_note,
            )
        elif reply.kind == "accepted":
            completed = reply.acknowledgement == "completed"
            if completed:
                feedback = card(
                    command=command,
                    tone="ok",
                    title="Completed",
                    body=reply.effect or "—",
                    request_id=request_id,
                    sequence=reply.sequence,
                    note=reply.repository_note,
                    outcome_note=outcome_note,
                )
            else:
                # An accepted (non-terminal) card keeps following its
                # command: it re-requests `track` on every `sse:fleet`
                # event until the acknowledgement is terminal.
                feedback = card(
                    command=command,
                    tone="info",
                    title="Accepted",
                    body=reply.effect or "—",
                    request_id=request_id,
                    sequence=reply.sequence,
                    note=reply.repository_note,
                    outcome_note=outcome_note,
                    track_url=track_url_for(
                        name,
                        action=action,
                        n_value=n_value,
                        issue_value=issue_value,
                        container_id=container_id,
                        repository=repository,
                        request_id=request_id,
                        sequence=reply.sequence,
                    ),
                )
        elif reply.kind == "rejected":
            feedback = card(
                command=command,
                tone="error",
                title="Rejected",
                body=(
                    f"Rejected: {reply.reason or 'no reason given'}. "
                    "No control change was accepted."
                ),
                request_id=request_id,
                sequence=reply.sequence,
                outcome_note=outcome_note,
            )
        elif reply.kind == "unknown":
            feedback = card(
                command=command,
                tone="error",
                title="Outcome unknown",
                body=UNKNOWN_BODY,
                request_id=request_id,
                sequence=reply.sequence,
                outcome_note=outcome_note,
                retry_fields={**card_fields, "submit_url": submit_url},
                status_fields={**card_fields, "status_url": status_url},
            )
        elif reply.kind == "usage_error":
            feedback = card(
                command=command,
                tone="error",
                title="Not sent",
                body=USAGE_ERROR_BODY,
                request_id=request_id,
                sequence=reply.sequence,
                outcome_note=outcome_note,
            )
        else:  # failed: Docker could not run agentctl, nothing was sent.
            assert reply.kind == "failed"
            assert error is not None
            feedback = card(
                command=command,
                tone="error",
                title="Not sent",
                body=error,
                request_id=request_id,
                sequence=reply.sequence,
                outcome_note=outcome_note,
            )
        return HTMLResponse(feedback + body_oob(), status_code=200)

    @app.get("/instances/{name}/control/track", response_class=HTMLResponse)
    async def instance_control_track(request: Request, name: str) -> Response:
        """One accepted card, re-read from the latest good snapshot.

        The card re-requests this route on every ``sse:fleet`` event until
        its acknowledgement is terminal. Reads only the latest good
        snapshot (no Docker call, no Control audit entry): when the request
        ID is in ``control.commands`` the card takes its acknowledgement
        and detail plus the progress line from the related section; an
        absent ID below every recorded sequence stops with "No longer in
        the last 10 commands" and Check status; an absent ID otherwise
        keeps "waiting for the next snapshot"; a replaced container stops
        with the /data volume message; a non-live Reach pauses but keeps
        following. An instance the fleet no longer lists stops the same
        way (200, so the ``sse:fleet`` trigger swaps and tracking ends).
        """
        fleet = request.app.state.fleet
        cfg = request.app.state.settings
        poll_interval = getattr(cfg, "poll_interval", 5.0)
        rows, _ = await fleet.snapshot(poll_interval=poll_interval)
        row = next((r for r in rows if r.name == name), None)

        query = dict(request.query_params)
        values, message = _validate_track(query)
        if values is None:
            assert message is not None
            return HTMLResponse(f'<p class="ctl-error">{message}</p>', status_code=422)
        status_url = f"/instances/{quote(name, safe='')}/control/status"
        if row is None:
            return HTMLResponse(
                render_card(
                    templates,
                    command=_display_command(
                        values["action"],
                        n=values["n_value"],
                        issue=values["issue_value"],
                    ),
                    tone="info",
                    title="Accepted",
                    body=(
                        "Instance replaced or unavailable; the agent's record of "
                        "this command is on its /data volume."
                    ),
                    request_id=values["request_id"],
                    sequence=values["sequence"],
                ),
                status_code=200,
            )
        control = None if row.control_raw_missing else row.control_raw
        result = follow_command(
            action=values["action"],
            request_id=values["request_id"],
            sequence=values["sequence"],
            card_container_id=values["container_id"],
            control=control,
            reach=row.reach,
            row_container_id=row.container_id,
            row_exists=True,
        )
        return HTMLResponse(
            follow_card(templates, name, values, result, status_url=status_url),
            status_code=200,
        )

    @app.post("/instances/{name}/control/status", response_class=HTMLResponse)
    async def instance_control_status(request: Request, name: str) -> Response:
        """Ask the agent directly: ``agentctl command <id>`` for one card.

        Request protection, then validation, then the gateway's
        command-status exec (409 when busy), then the re-rendered card.
        Exit 0 takes the record's state; ``not_recorded`` offers Retry;
        ``unknown`` (and any other non-record outcome) shows "Could not
        check" with both buttons. It writes no Control audit entry: it
        changes nothing. No path returns a 500.
        """
        fleet = request.app.state.fleet
        cfg = request.app.state.settings
        mode = getattr(request.app.state, "control_mode", ControlMode(available=True))
        gateway = request.app.state.gateway
        poll_interval = getattr(cfg, "poll_interval", 5.0)
        rows, _ = await fleet.snapshot(poll_interval=poll_interval)
        row = next((r for r in rows if r.name == name), None)

        form = parse_form(await request.body())

        protection = check_protection(request, mode)
        if protection is not None:
            reason, message = protection
            if reason == "controls_unavailable":
                fragment = f"Controls unavailable: {message}"
            else:
                fragment = f"Request refused: {message}"
            return HTMLResponse(f'<p class="ctl-error">{fragment}</p>', status_code=403)

        if row is None:
            return templates.TemplateResponse(
                request,
                "_control.html",
                {"name": name, "slug": control_slug(name), "error": "instance not found"},
                status_code=404,
            )

        values, validation_message = _validate_submit(form)
        if values is None:
            assert validation_message is not None
            try:
                shown_request: str | None = validate_request_id(
                    form.get("request_id", "")
                )
            except ValueError:
                shown_request = None
            action_raw = form.get("action", "")
            return HTMLResponse(
                render_card(
                    templates,
                    command=action_raw or "—",
                    tone="error",
                    title="Refused",
                    body=validation_message,
                    request_id=shown_request,
                ),
                status_code=422,
            )

        action = values["action"]
        n_value = values["n_value"]
        issue_value = values["issue_value"]
        container_id = values["container_id"]
        repository = values["repository"]
        request_id = values["request_id"]
        command = values["command"]
        submit_url = f"/instances/{quote(name, safe='')}/control/submit"
        status_url = f"/instances/{quote(name, safe='')}/control/status"

        def check_card(reason: str) -> str:
            """A "Could not check" card with both Retry and Check status."""
            return render_card(
                templates,
                command=command,
                tone="error",
                title="Outcome unknown",
                body=f"Could not check: {reason}",
                request_id=request_id,
                sequence=None,
                retry_fields={
                    "action": action,
                    "n_value": n_value,
                    "issue_value": issue_value,
                    "container_id": container_id,
                    "repository": repository,
                    "request_id": request_id,
                    "submit_url": submit_url,
                },
                status_fields={
                    "action": action,
                    "n_value": n_value,
                    "issue_value": issue_value,
                    "container_id": container_id,
                    "repository": repository,
                    "request_id": request_id,
                    "status_url": status_url,
                },
            )

        # The card's container must still be the fleet's: otherwise the
        # agent's record of this command is on its /data volume.
        if row.container_id != container_id:
            return HTMLResponse(
                render_card(
                    templates,
                    command=command,
                    tone="info",
                    title="Accepted",
                    body=(
                        "Instance replaced or unavailable; the agent's record of "
                        "this command is on its /data volume."
                    ),
                    request_id=request_id,
                    sequence=None,
                ),
                status_code=200,
            )

        # The per-instance busy check refuses before the exec (409), like
        # submit but without any audit entry.
        in_flight = getattr(gateway, "control_in_flight", None)
        if callable(in_flight):
            try:
                claimed = bool(in_flight(row.container_id))
            except Exception:  # noqa: BLE001 - a broken probe never refuses
                claimed = False
            if claimed:
                return HTMLResponse(check_card(BUSY_MESSAGE), status_code=409)

        reply: ControlReply | None = None
        try:
            exec_result = await gateway.command_status(row.container_id, request_id)
        except ControlBusyError:
            return HTMLResponse(check_card(BUSY_MESSAGE), status_code=409)
        except (ControlTimeoutError, DockerNotAvailableError) as exc:
            reply = reply_for_exception(exc)
        except Exception as exc:  # noqa: BLE001 - no path returns a 500
            reply = ControlReply(
                kind="unknown",
                reason=str(exc)[:2000] or "command status exec failed",
                exit_code=None,
            )
        else:
            try:
                reply = parse_command_reply(
                    exec_result, expected_repository=repository
                )
            except Exception as exc:  # noqa: BLE001 - the parser never raises
                code = getattr(exec_result, "returncode", None)
                reply = ControlReply(
                    kind="unknown",
                    reason=str(exc)[:2000] or "command status exec failed",
                    exit_code=code if isinstance(code, int) else None,
                )
        assert reply is not None

        if reply.kind == "record":
            acknowledgement = reply.acknowledgement
            # The record carries `effect` as a first-class field; its
            # `detail` line survives in the raw mapping only. Per the spec
            # cards table, Completed shows `effect` or the record's
            # `detail`; Superseded and Not fulfilled show the record's
            # `detail` (a missing value renders as an em dash).
            record_text = reply.effect or reply.raw.get("detail") or None
            record_detail = reply.raw.get("detail") or None
            if isinstance(acknowledgement, str) and acknowledgement in (
                "completed",
                "superseded",
                "not fulfilled",
            ):
                titles = {
                    "completed": "Completed",
                    "superseded": "Superseded",
                    "not fulfilled": "Not fulfilled",
                }
                tones = {
                    "completed": "ok",
                    "superseded": "muted",
                    "not fulfilled": "warn",
                }
                if acknowledgement == "completed":
                    terminal_body = record_text or "—"
                else:
                    terminal_body = record_detail or "—"
                return HTMLResponse(
                    render_card(
                        templates,
                        command=command,
                        tone=tones[acknowledgement],
                        title=titles[acknowledgement],
                        body=terminal_body,
                        request_id=request_id,
                        sequence=reply.sequence,
                    ),
                    status_code=200,
                )
            # A non-terminal record keeps following from the snapshot: the
            # progress line comes from the related section there.
            control = None if row.control_raw_missing else row.control_raw
            result = follow_command(
                action=action,
                request_id=request_id,
                sequence=reply.sequence,
                card_container_id=container_id,
                control=control,
                reach=row.reach,
                row_container_id=row.container_id,
                row_exists=True,
            )
            if result.outcome == "terminal":
                return HTMLResponse(
                    render_card(
                        templates,
                        command=command,
                        tone=result.tone,
                        title=result.title,
                        body=result.body,
                        request_id=request_id,
                        sequence=reply.sequence,
                    ),
                    status_code=200,
                )
            status_values = {
                "action": action,
                "n_value": n_value,
                "issue_value": issue_value,
                "container_id": container_id,
                "repository": repository,
                "request_id": request_id,
                "sequence": reply.sequence,
            }
            # The agent just confirmed the command is still accepted. The
            # record's own text is fresher than the snapshot's, so it wins;
            # the progress line still comes from the snapshot's related
            # section.
            if result.outcome in ("following", "paused", "waiting"):
                outcome = result.outcome
            else:
                outcome = "following"
            body = record_text or result.body
            return HTMLResponse(
                follow_card(
                    templates,
                    name,
                    status_values,
                    FollowResult(
                        outcome=outcome,
                        acknowledgement=acknowledgement,
                        title="Accepted",
                        tone="info",
                        body=body,
                        progress=result.progress,
                        following=True,
                    ),
                    status_url=status_url,
                ),
                status_code=200,
            )
        if reply.kind == "not_recorded":
            return HTMLResponse(
                render_card(
                    templates,
                    command=command,
                    tone="error",
                    title="Not recorded",
                    body=NOT_RECORDED_BODY,
                    request_id=request_id,
                    sequence=reply.sequence,
                    retry_fields={
                        "action": action,
                        "n_value": n_value,
                        "issue_value": issue_value,
                        "container_id": container_id,
                        "repository": repository,
                        "request_id": request_id,
                        "submit_url": submit_url,
                    },
                ),
                status_code=200,
            )
        return HTMLResponse(
            check_card(reply.reason or "no reply from the agent"), status_code=200
        )
