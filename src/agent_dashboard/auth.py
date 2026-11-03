"""Built-in password sign-in for LAN mode (spec sections 3, 4.3, 5, 6.2, 8, 9).

Every route needs a sign-in session: the gate middleware (spec 5.1) refuses
requests without one, ``GET``/``POST /login`` signs an Operator in, and
``POST /logout`` signs them out. ``GET /whoami`` answers 204 while signed
in. All templates consume the framework-free view models here (``Operator``,
``SignInView``); the server's store calls run in a worker thread.

The growing sign-in delay (spec 5.5) refuses attempts inside either key's
delay with 429 before the password is checked. ``GET /login`` with a cookie
that matches no session clears the cookie and shows the stale-session info
box (spec 5.6, 8.1).
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import math
import secrets
import sys
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode, urlsplit

from fastapi import Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from . import account_store
from .control_audit import utc_now_ms
from .control_routes import parse_form
from .passwords import verify_password

#: Cookie holding the sign-in session ID over ``https`` (spec 5.2).
HOST_COOKIE_NAME = "__Host-agent-dashboard-sign-in"

#: Cookie holding the sign-in session ID over loopback ``http`` (spec 5.2).
#: Safari refuses ``Secure`` on ``http://localhost``, so the plain name
#: carries no ``Secure`` flag there (ADR 0011).
LOOPBACK_COOKIE_NAME = "agent-dashboard-sign-in"

#: Paths whose ``next`` target is rejected (spec 5.1): signing back in
#: lands on ``/`` instead of looping on a sign-in route.
LOGIN_PATHS = frozenset({"/login", "/logout", "/set-password"})

#: Form bodies over this size are refused with 413 (spec 5.3).
MAX_FORM_BYTES = 8 * 1024

#: Login password probes over this many bytes run against the dummy hash
#: without checking (spec 5.3).
MAX_PASSWORD_BYTES = 1024

#: Fixed argon2id hash (default parameters) for the timing-safe dummy check
#: (spec 5.3): unknown, removed or password-less accounts, and over-long
#: passwords, verify against this and fail. It is the hash of a random
#: string nobody knows; it never authenticates anyone.
DUMMY_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4"
    "$ccVoPRA/5o/Hbi3bfsKDgQ$4GaIkVybyYnPY5EZ55cU7ZCtWlIXCEtdMp95nEmSJTM"
)

#: Argon2 hashing runs in a worker thread behind this semaphore (spec 5.3),
#: so at most 128 MiB is spent on hashing at once.
_HASH_SEMAPHORE = asyncio.Semaphore(2)


@dataclass(frozen=True)
class Operator:
    """The signed-in Operator: account name, Role, session start (spec 3.2).

    The gate puts one on ``request.state``; templates read only this.
    """

    name: str
    role: str
    signed_in_at: float

    @property
    def since(self) -> str:
        """The session start as ``HH:MM`` UTC for the top-bar chip."""
        return datetime.fromtimestamp(self.signed_in_at, UTC).strftime("%H:%M")


@dataclass(frozen=True)
class SignInView:
    """The ``/login`` card view model (spec 8.1): at most one box."""

    account: str = ""
    focus_password: bool = False
    notice_tone: str | None = None  # "danger" | "warn" | "ok" | "info"
    notice_text: str = ""
    next: str = ""


@dataclass(frozen=True)
class SetPasswordView:
    """The ``/set-password`` card view model (spec 8.3): at most one box.

    ``show_form`` is False for unusable links (410): the danger box replaces
    the form, followed by the reset hint (except for unknown) and a link to
    ``/login``. ``expires_text`` is the ``HH:MM UTC (N h left)`` tail of the
    "works once" line for a usable link. The token travels only in the
    form's hidden field, never anywhere else on the page.
    """

    account: str = ""
    token: str = ""
    expires_text: str = ""
    notice_tone: str | None = None  # "danger" only on this page
    notice_text: str = ""
    show_form: bool = True


#: The 410 danger boxes for unusable Set-password links (spec 8.3).
LINK_STATE_BOXES = {
    "expired": (
        "This Set-password link has expired: links work for 24 hours. "
        "Ask whoever runs the dashboard host for a new one."
    ),
    "used": (
        "This Set-password link has already been used. If that was not you, "
        "or you have forgotten the password, ask for a new link."
    ),
    "superseded": (
        "A newer Set-password link was issued for this account, so this one "
        "no longer works. Use the newest link you received."
    ),
    "unknown": (
        "This Set-password link is not valid. Check that you copied the whole "
        "link, or ask for a new one."
    ),
}


def format_link_expiry(expires_at: float, now: float) -> str:
    """The ``HH:MM UTC (N h left)`` tail of the "works once" line (spec 8.3)."""
    moment = datetime.fromtimestamp(expires_at, UTC).strftime("%H:%M")
    hours = max(0, math.ceil((expires_at - now) / 3600))
    return f"{moment} UTC ({hours} h left)"


def sanitize_next(value: str | None) -> str:
    """Apply the ``next`` rule (spec 5.1), falling back to ``/``.

    Accepted only when it starts with a single ``/`` (not ``//``, not
    ``/\\``), has no control characters, is at most 2048 characters, and
    its path is not a sign-in route.
    """
    cleaned = try_sanitize_next(value)
    return cleaned if cleaned is not None else "/"


def try_sanitize_next(value: str | None) -> str | None:
    """The ``next`` rule returning None when the target is rejected."""
    if not value:
        return None
    if len(value) > 2048:
        return None
    if any(unicodedata.category(char) == "Cc" for char in value):
        return None
    if not value.startswith("/") or value[1:2] in ("/", "\\"):
        return None
    path = value.split("?", 1)[0].split("#", 1)[0]
    if path in LOGIN_PATHS:
        return None
    if len(path) > 1 and path.rstrip("/") in LOGIN_PATHS:
        return None
    return value


def login_target(next: str | None) -> str:
    """The ``/login?next=…`` URL for a refused request (spec 5.1)."""
    cleaned = try_sanitize_next(next)
    if cleaned is None:
        return "/login"
    return "/login?" + urlencode({"next": cleaned})


def normalize_account_name(value: str) -> str:
    """Lowercase and strip a typed account name (spec 5.3)."""
    return value.strip().lower()


def cut_account_name(value: str) -> str:
    """Cut a typed account name to 64 characters for the ``auth`` lines."""
    return value[:64]


def read_session_id(request: Request) -> str | None:
    """The session ID cookie, trying ``__Host-`` first (spec 5.2)."""
    return (
        request.cookies.get(HOST_COOKIE_NAME)
        or request.cookies.get(LOOPBACK_COOKIE_NAME)
        or None
    )


def hash_session_id(session_id: str) -> bytes:
    """SHA-256 of the session ID cookie value: what the store keeps."""
    return hashlib.sha256(session_id.encode("utf-8")).digest()


def cookie_for_origin(origin: str) -> tuple[str, bool]:
    """The cookie name and ``Secure`` flag for a sign-in ``Origin`` (5.2).

    ``https`` gets the ``__Host-`` name with ``Secure``; loopback ``http``
    (the only other secure context, checked before calling) gets the plain
    name without it.
    """
    if urlsplit(origin).scheme.lower() == "https":
        return HOST_COOKIE_NAME, True
    return LOOPBACK_COOKIE_NAME, False


def is_secure_origin(origin: str) -> bool:
    """Whether an ``Origin`` is a secure context for a password (4.3).

    The ``Origin`` was already checked by the ``Origin`` rule: ``https``, or
    a loopback host (``localhost``, ``127.0.0.0/8``, ``::1`` — the SSH
    tunnel). Anything else refuses the password.
    """
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    if parts.scheme.lower() == "https":
        return True
    if parts.scheme.lower() != "http":
        return False
    host = (parts.hostname or "").lower()
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback


def log_auth(event: str, **fields: Any) -> None:
    """Write one stdout ``auth`` line (spec 9): ``auth <json>``."""
    payload = {"at": utc_now_ms(), "event": event, **fields}
    print(f"auth {json.dumps(payload, separators=(',', ':'))}", flush=True)
    sys.stdout.flush()


def client_of(request: Request) -> str:
    """The client address for the delay and the ``auth`` lines (4.4, 9).

    The peer address resolved through ``X-Forwarded-For`` from trusted
    proxies (``request_checks.client_address``).
    """
    from .request_checks import client_address

    settings = request.app.state.settings
    peer = request.client.host if request.client is not None else ""
    return client_address(
        peer,
        request.headers.get("x-forwarded-for"),
        getattr(settings, "trusted_proxies", ()),
    )


def hx_page(request: Request) -> str | None:
    """The page of an htmx request from ``HX-Current-URL`` (spec 5.1).

    The path and query of the header, or None when the header is missing
    or its path fails the ``next`` rule.
    """
    raw = request.headers.get("hx-current-url")
    if not raw:
        return None
    try:
        parts = urlsplit(raw.strip())
    except ValueError:
        return None
    if not parts.path:
        return None
    page = parts.path + (f"?{parts.query}" if parts.query else "")
    return try_sanitize_next(page)


def refuse(request: Request) -> Response:
    """The gate's answer for a request without a sign-in session (5.1)."""
    if request.headers.get("hx-request") == "true":
        page = hx_page(request)
        target = login_target(page) if page is not None else "/login"
        return Response(status_code=401, headers={"HX-Redirect": target})
    if request.url.path == "/stream" and request.method in ("GET", "HEAD"):
        return PlainTextResponse("Sign-in required.", status_code=401)
    if request.method in ("GET", "HEAD"):
        path = request.url.path
        query = request.url.query
        return RedirectResponse(
            login_target(f"{path}?{query}" if query else path),
            status_code=303,
        )
    return PlainTextResponse("Sign-in required.", status_code=401)


async def resolve_session(request: Request) -> account_store.SignInSession | None:
    """Read the cookie and look the session up, touching activity (6.2).

    Ended rows are deleted when found; the Role comes from the account row
    on every request.
    """
    raw = read_session_id(request)
    if not raw:
        return None
    settings = request.app.state.settings
    now = request.app.state.auth_clock()
    id_hash = hash_session_id(raw)

    def work() -> account_store.SignInSession | None:
        conn = account_store.open_store()
        try:
            return account_store.find_sign_in_session(
                conn,
                id_hash,
                now=now,
                idle_timeout=settings.sign_in_idle_timeout,
                max_age=settings.sign_in_max_age,
            )
        finally:
            conn.close()

    return await asyncio.to_thread(work)


class SignInGateMiddleware(BaseHTTPMiddleware):
    """The sign-in gate (spec 5.1): every route needs a session in LAN mode.

    Runs after the ``Host`` check. It resolves the session and puts a
    framework-free ``Operator`` on ``request.state``. ``/login`` and
    ``/static/*`` are anonymous. A refused request never reaches Control
    handling and writes no Control audit entry.
    """

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.url.path in ("/login", "/set-password") or request.url.path.startswith(
            "/static/"
        ):
            request.state.operator = None
            request.state.session_id_hash = None
            return await call_next(request)
        raw = read_session_id(request)
        record = await resolve_session(request)
        if record is None:
            return refuse(request)
        request.state.operator = Operator(
            name=record.account,
            role=record.role,
            signed_in_at=record.created_at,
        )
        request.state.session_id_hash = hash_session_id(raw) if raw else None
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Response headers in LAN mode (spec 8).

    Every HTML response sends ``X-Frame-Options: DENY`` and
    ``Referrer-Policy: same-origin``; ``/login`` and ``/set-password`` also
    send ``Cache-Control: no-store``.
    """

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        content_type = response.headers.get("content-type", "")
        if "text/html" in content_type:
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Referrer-Policy"] = "same-origin"
            if request.url.path in ("/login", "/set-password"):
                response.headers["Cache-Control"] = "no-store"
        return response


def _login_view_for_query(request: Request) -> SignInView:
    """The ``/login`` view for a plain GET: ``signed_out``/``password_set``."""
    params = request.query_params
    next = sanitize_next(params.get("next"))
    if params.get("password_set") == "1":
        account = params.get("account", "")
        try:
            account_store.check_name(account)
        except ValueError:
            account = ""
        return SignInView(
            account=account,
            focus_password=True,
            notice_tone="ok",
            notice_text="Your password is set. Sign in with it now.",
            next=next if params.get("next") else "",
        )
    if params.get("signed_out") == "1":
        return SignInView(
            notice_tone="ok",
            notice_text="You have signed out.",
            next=next if params.get("next") else "",
        )
    return SignInView(next=next if params.get("next") else "")


def _stale_session_view(next_param: str | None) -> SignInView:
    """The ``/login`` view for a cookie without a session (spec 5.6, 8.1).

    The info box never names the reason (expiry, reset, remove). The last
    four words — the ``next`` page clause — only appear when a ``next``
    target survived the ``next`` rule.
    """
    cleaned = try_sanitize_next(next_param)
    if cleaned is not None:
        text = (
            "Your sign-in session has ended. "
            "Sign in again to continue where you were."
        )
    else:
        text = "Your sign-in session has ended. Sign in again."
        cleaned = ""
    return SignInView(notice_tone="info", notice_text=text, next=cleaned)


def _render_login(
    request: Request,
    templates: Jinja2Templates,
    view: SignInView,
    *,
    status_code: int = 200,
) -> HTMLResponse:
    """Render the ``/login`` card (spec 8.1)."""
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "request": request,
            "lan_mode": True,
            "operator": None,
            "page_subtitle": "LAN mode · sign-in required",
            "view": view,
        },
        status_code=status_code,
    )


def _render_set_password(
    request: Request,
    templates: Jinja2Templates,
    view: SetPasswordView,
    *,
    status_code: int = 200,
) -> HTMLResponse:
    """Render the ``/set-password`` card (spec 8.3)."""
    return templates.TemplateResponse(
        request,
        "set-password.html",
        {
            "request": request,
            "lan_mode": True,
            "operator": None,
            "page_subtitle": "LAN mode · sign-in required",
            "view": view,
        },
        status_code=status_code,
    )


def _unusable_link_view(
    detail: account_store.SetPasswordLinkDetail,
    token: str,
    *,
    now: float,
) -> SetPasswordView:
    """The 410 view for an unusable link: danger box, no form (spec 8.3)."""
    account = detail.account or ""
    return SetPasswordView(
        account=account,
        token=token,
        expires_text="",
        notice_tone="danger",
        notice_text=LINK_STATE_BOXES[detail.state],
        show_form=False,
    )


def _valid_link_view(
    detail: account_store.SetPasswordLinkDetail,
    token: str,
    *,
    now: float,
    notice_tone: str | None = None,
    notice_text: str = "",
) -> SetPasswordView:
    """The form view for a usable link, optionally with one notice box."""
    assert detail.account is not None and detail.expires_at is not None
    return SetPasswordView(
        account=detail.account,
        token=token,
        expires_text=format_link_expiry(detail.expires_at, now),
        notice_tone=notice_tone,
        notice_text=notice_text,
        show_form=True,
    )


async def _check_credentials(
    request: Request,
    name: str,
    password: str,
) -> bool:
    """Check a password in a worker thread behind the hash semaphore (5.3).

    The account name is normalised here; the password is NFKC-normalised
    by :func:`passwords.verify_password`. An unknown name, a removed
    account, an account without a password, or a password over 1024 bytes
    runs the check against the fixed dummy hash, so the answer takes the
    same time, and fails.
    """
    hasher = request.app.state.password_hasher

    def work() -> bool:
        conn = account_store.open_store()
        try:
            account = account_store.get_account(conn, name)
            if (
                account is None
                or account.removed
                or account.password_hash is None
                or len(password.encode("utf-8")) > MAX_PASSWORD_BYTES
            ):
                candidate = DUMMY_HASH
            else:
                candidate = account.password_hash
            checker = hasher if hasher is not None else None
            from .passwords import default_hasher

            active = checker if checker is not None else default_hasher()
            return verify_password(candidate, password, hasher=active)
        finally:
            conn.close()

    async with _HASH_SEMAPHORE:
        return await asyncio.to_thread(work)


async def _maybe_rehash(
    request: Request, name: str, password: str
) -> None:
    """Rehash the stored password when its parameters are stale (5.3)."""
    from .passwords import default_hasher, normalize_password

    hasher = request.app.state.password_hasher
    active = hasher if hasher is not None else default_hasher()

    def work() -> None:
        conn = account_store.open_store()
        try:
            account = account_store.get_account(conn, name)
            if (
                account is None
                or account.removed
                or account.password_hash is None
            ):
                return
            if not active.check_needs_rehash(account.password_hash):
                return
            with conn:
                conn.execute(
                    "UPDATE accounts SET password_hash = ? WHERE name = ?",
                    (active.hash(normalize_password(password)), name),
                )
        finally:
            conn.close()

    async with _HASH_SEMAPHORE:
        await asyncio.to_thread(work)


def _clear_sign_in_cookies(response: Response) -> None:
    """Clear both sign-in cookie names (spec 5.2)."""
    response.delete_cookie(HOST_COOKIE_NAME, path="/")
    response.delete_cookie(LOOPBACK_COOKIE_NAME, path="/")


def _set_sign_in_cookie(
    response: Response, *, name: str, secure: bool, value: str, max_age: int
) -> None:
    """Set the sign-in cookie (spec 5.2): ``HttpOnly; SameSite=Strict``."""
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        path="/",
        secure=secure,
        httponly=True,
        samesite="strict",
    )


def login_redirect_param(value: str | None) -> str:
    """The ``next`` hidden field value: the sanitised target or ``""``."""
    cleaned = try_sanitize_next(value)
    return cleaned if cleaned is not None else ""


def register_auth_routes(app, templates: Jinja2Templates) -> None:
    """Add the LAN-only auth routes: ``/login``, ``/logout``, ``/whoami``,
    ``GET``/``POST /set-password``."""

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request) -> Response:
        """The sign-in card (spec 8.1).

        A valid session redirects to ``next``, else ``/``. A cookie
        without a session is cleared and the stale-session info box shows
        (spec 5.6); the reason is never named.
        """
        record = await resolve_session(request)
        if record is not None:
            return RedirectResponse(
                sanitize_next(request.query_params.get("next")), status_code=303
            )
        view = _login_view_for_query(request)
        if read_session_id(request) is not None and view.notice_tone is None:
            view = _stale_session_view(request.query_params.get("next"))
            response = _render_login(request, templates, view)
            _clear_sign_in_cookies(response)
            return response
        return _render_login(request, templates, view)

    @app.post("/login", response_class=HTMLResponse)
    async def login_submit(request: Request) -> Response:
        """Check the credentials and start a sign-in session (spec 5.3).

        In order: ``Origin``/``Sec-Fetch-Site``, the secure context, the
        growing delay (5.5), then the password check. On success a new
        session ID is created and the cookie set, with a 303 to ``next``,
        else ``/``.
        """
        from .request_checks import is_origin_allowed, log_origin_refused
        from .sign_in_delay import account_key, client_key

        body = await request.body()
        if len(body) > MAX_FORM_BYTES:
            return PlainTextResponse("Form body too large.", status_code=413)
        form = parse_form(body)
        host = (request.headers.get("host") or "").strip()
        origin = request.headers.get("origin")
        fetch_site = request.headers.get("sec-fetch-site")
        typed = normalize_account_name(form.get("account", ""))
        next = login_redirect_param(form.get("next"))
        client = client_of(request)

        def error_box(message: str, *, status: int) -> Response:
            return _render_login(
                request,
                templates,
                SignInView(
                    account=typed,
                    focus_password=True,
                    notice_tone="danger",
                    notice_text=message,
                    next=next,
                ),
                status_code=status,
            )

        def warn_box(message: str, *, status: int) -> Response:
            return _render_login(
                request,
                templates,
                SignInView(
                    account=typed,
                    focus_password=True,
                    notice_tone="warn",
                    notice_text=message,
                    next=next,
                ),
                status_code=status,
            )

        if not is_origin_allowed(origin, host) or (
            fetch_site is not None and fetch_site.strip().lower() != "same-origin"
        ):
            reason = (
                "fetch_site"
                if is_origin_allowed(origin, host)
                else "origin"
            )
            log_origin_refused(request, reason=reason)
            return error_box(
                "Sign-in was refused: the request did not come from this page.",
                status=403,
            )
        assert origin is not None
        if not is_secure_origin(origin):
            log_origin_refused(request, reason="secure_context")
            return error_box(
                "Sign-in was refused: this page was not opened over HTTPS.",
                status=403,
            )
        tracker = request.app.state.sign_in_delay
        now = request.app.state.auth_clock()
        account_bucket = account_key(typed)
        client_bucket = client_key(client)
        wait_s = tracker.wait_s(account_bucket, client_bucket, now=now)
        if wait_s > 0:
            log_auth(
                "sign_in_delayed",
                account=cut_account_name(typed),
                client=client,
                wait_s=wait_s,
            )
            response = warn_box(
                "Too many failed sign-ins. "
                f"Wait {wait_s} seconds, then try again. "
                "That attempt was not checked.",
                status=429,
            )
            response.headers["Retry-After"] = str(wait_s)
            return response
        password = form.get("password", "")
        if not isinstance(password, str):
            password = ""
        if not await _check_credentials(request, typed, password):
            new_delay = tracker.record_failure(
                account_bucket,
                client_bucket,
                now=request.app.state.auth_clock(),
            )
            log_auth(
                "sign_in_failed",
                account=cut_account_name(typed),
                client=client,
                delay_s=new_delay,
            )
            message = "Account name or password is incorrect."
            if new_delay > 1:
                message += (
                    f" Wait {new_delay} seconds before trying again."
                )
            return _render_login(
                request,
                templates,
                SignInView(
                    account=typed,
                    focus_password=True,
                    notice_tone="danger",
                    notice_text=message,
                    next=next,
                ),
                status_code=401,
            )
        tracker.record_success(
            account_bucket,
            client_bucket,
            now=request.app.state.auth_clock(),
        )
        await _maybe_rehash(request, typed, password)
        session_id = secrets.token_urlsafe(32)
        settings = request.app.state.settings
        now = request.app.state.auth_clock()

        def create() -> None:
            conn = account_store.open_store()
            try:
                account_store.create_sign_in_session(
                    conn, typed, hash_session_id(session_id), now=now
                )
            finally:
                conn.close()

        await asyncio.to_thread(create)
        cookie_name, secure = cookie_for_origin(origin)
        response = RedirectResponse(next or "/", status_code=303)
        _set_sign_in_cookie(
            response,
            name=cookie_name,
            secure=secure,
            value=session_id,
            max_age=int(settings.sign_in_max_age),
        )
        log_auth("sign_in", account=typed, client=client)
        return response

    @app.post("/logout")
    async def logout(request: Request) -> Response:
        """End only the current sign-in session (spec 5.4)."""
        from .request_checks import is_origin_allowed, log_origin_refused

        current = getattr(request.state, "operator", None)
        host = (request.headers.get("host") or "").strip()
        origin = request.headers.get("origin")
        fetch_site = request.headers.get("sec-fetch-site")
        if not is_origin_allowed(origin, host) or (
            fetch_site is not None and fetch_site.strip().lower() != "same-origin"
        ):
            reason = (
                "fetch_site"
                if is_origin_allowed(origin, host)
                else "origin"
            )
            log_origin_refused(request, reason=reason)
            return PlainTextResponse(
                "Sign-out was refused: the request did not come from this page.",
                status_code=403,
            )
        account = current.name if current is not None else ""
        id_hash = getattr(request.state, "session_id_hash", None)
        if id_hash is not None:

            def delete() -> None:
                conn = account_store.open_store()
                try:
                    account_store.delete_sign_in_session(conn, id_hash)
                finally:
                    conn.close()

            await asyncio.to_thread(delete)
        log_auth("sign_out", account=account, client=client_of(request))
        response = RedirectResponse("/login?signed_out=1", status_code=303)
        _clear_sign_in_cookies(response)
        return response

    @app.get("/set-password", response_class=HTMLResponse)
    async def set_password_page(request: Request) -> Response:
        """The Set-password card for a link token (spec 8.3).

        LAN mode only, anonymous. A usable link shows the form; an
        expired, used, superseded or unknown link (a removed account reads
        as unknown) answers 410 with the danger box instead of the form.
        """
        token = request.query_params.get("token", "")
        if not isinstance(token, str):
            token = ""
        now = request.app.state.auth_clock()

        def work() -> account_store.SetPasswordLinkDetail:
            conn = account_store.open_store(now=now)
            try:
                return account_store.describe_set_password_link(
                    conn, token, now=now
                )
            finally:
                conn.close()

        detail = await asyncio.to_thread(work)
        if detail.state != "valid":
            return _render_set_password(
                request,
                templates,
                _unusable_link_view(detail, token, now=now),
                status_code=410,
            )
        return _render_set_password(
            request, templates, _valid_link_view(detail, token, now=now)
        )

    @app.post("/set-password", response_class=HTMLResponse)
    async def set_password_submit(request: Request) -> Response:
        """Set a password through a link (spec 8.3, 6.5).

        In order: ``Origin``/``Sec-Fetch-Site`` (403), the secure context
        (403), the token state (410), then the length and the match after
        NFKC normalisation (422). Then one transaction sets the hash,
        marks the link used and deletes the account's sign-in sessions.
        No sign-in follows: 303 to ``/login?password_set=1&account=<name>``.
        """
        from .passwords import normalize_password
        from .request_checks import is_origin_allowed, log_origin_refused

        body = await request.body()
        if len(body) > MAX_FORM_BYTES:
            return PlainTextResponse("Form body too large.", status_code=413)
        form = parse_form(body)
        raw_token = form.get("token", "")
        token = raw_token if isinstance(raw_token, str) else ""
        host = (request.headers.get("host") or "").strip()
        origin = request.headers.get("origin")
        fetch_site = request.headers.get("sec-fetch-site")
        client = client_of(request)

        async def describe_at(moment: float) -> account_store.SetPasswordLinkDetail:
            def work() -> account_store.SetPasswordLinkDetail:
                conn = account_store.open_store(now=moment)
                try:
                    return account_store.describe_set_password_link(
                        conn, token, now=moment
                    )
                finally:
                    conn.close()

            return await asyncio.to_thread(work)

        def refused_view(
            detail: account_store.SetPasswordLinkDetail,
            moment: float,
            message: str,
        ) -> SetPasswordView:
            if detail.state == "valid":
                return _valid_link_view(
                    detail,
                    token,
                    now=moment,
                    notice_tone="danger",
                    notice_text=message,
                )
            return SetPasswordView(
                account=detail.account or "",
                token=token,
                expires_text="",
                notice_tone="danger",
                notice_text=message,
                show_form=False,
            )

        if not is_origin_allowed(origin, host) or (
            fetch_site is not None and fetch_site.strip().lower() != "same-origin"
        ):
            reason = (
                "fetch_site" if is_origin_allowed(origin, host) else "origin"
            )
            log_origin_refused(request, reason=reason)
            moment = request.app.state.auth_clock()
            detail = await describe_at(moment)
            return _render_set_password(
                request,
                templates,
                refused_view(
                    detail,
                    moment,
                    "Sign-in was refused: the request did not come from this page.",
                ),
                status_code=403,
            )
        assert origin is not None
        if not is_secure_origin(origin):
            log_origin_refused(request, reason="secure_context")
            moment = request.app.state.auth_clock()
            detail = await describe_at(moment)
            return _render_set_password(
                request,
                templates,
                refused_view(
                    detail,
                    moment,
                    "Sign-in was refused: this page was not opened over HTTPS.",
                ),
                status_code=403,
            )
        moment = request.app.state.auth_clock()
        detail = await describe_at(moment)
        if detail.state != "valid":
            return _render_set_password(
                request,
                templates,
                _unusable_link_view(detail, token, now=moment),
                status_code=410,
            )
        password = form.get("password", "")
        repeat = form.get("password_repeat", "")
        if not isinstance(password, str):
            password = ""
        if not isinstance(repeat, str):
            repeat = ""
        normalised = normalize_password(password)
        normalised_repeat = normalize_password(repeat)
        if len(normalised) < 15:
            message = "Use at least 15 characters."
        elif len(normalised) > 128:
            message = "Use at most 128 characters."
        elif normalised != normalised_repeat:
            message = "The two passwords differ."
        else:
            message = ""
        if message:
            return _render_set_password(
                request,
                templates,
                _valid_link_view(
                    detail,
                    token,
                    now=moment,
                    notice_tone="danger",
                    notice_text=message,
                ),
                status_code=422,
            )

        hasher = request.app.state.password_hasher

        def hash_work() -> str:
            from .passwords import default_hasher

            active = hasher if hasher is not None else default_hasher()
            return active.hash(normalised)

        async with _HASH_SEMAPHORE:
            password_hash = await asyncio.to_thread(hash_work)
        consume_moment = request.app.state.auth_clock()

        def consume_work() -> account_store.SetPasswordLinkDetail:
            conn = account_store.open_store(now=consume_moment)
            try:
                return account_store.consume_set_password_link(
                    conn, token, password_hash, now=consume_moment
                )
            finally:
                conn.close()

        result = await asyncio.to_thread(consume_work)
        if result.state != "valid" or result.account is None:
            return _render_set_password(
                request,
                templates,
                _unusable_link_view(result, token, now=consume_moment),
                status_code=410,
            )
        log_auth("password_set", account=result.account, client=client)
        target = "/login?" + urlencode(
            {"password_set": "1", "account": result.account}
        )
        return RedirectResponse(target, status_code=303)

    @app.get("/whoami")
    async def whoami(request: Request) -> Response:
        """204 while signed in; else the gate's answer (spec 5.6)."""
        return Response(status_code=204)


__all__ = [
    "DUMMY_HASH",
    "HOST_COOKIE_NAME",
    "LOGIN_PATHS",
    "LOOPBACK_COOKIE_NAME",
    "MAX_FORM_BYTES",
    "MAX_PASSWORD_BYTES",
    "LINK_STATE_BOXES",
    "Operator",
    "SecurityHeadersMiddleware",
    "SetPasswordView",
    "SignInGateMiddleware",
    "SignInView",
    "client_of",
    "cookie_for_origin",
    "cut_account_name",
    "format_link_expiry",
    "hash_session_id",
    "hx_page",
    "is_secure_origin",
    "log_auth",
    "login_redirect_param",
    "login_target",
    "normalize_account_name",
    "read_session_id",
    "refuse",
    "register_auth_routes",
    "resolve_session",
    "sanitize_next",
    "try_sanitize_next",
]
