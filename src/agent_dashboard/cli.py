"""Console entry point: agent-dashboard."""

from __future__ import annotations

import sys
import time

from .account_cli import run_account
from .account_store import (
    StoreError,
    any_account_can_sign_in,
    open_store,
)
from .app import create_app
from .config import is_loopback, load_settings

#: Migration line for ADR 0008 deployments (spec 2.4).
MIGRATION_LINE = (
    "agent-dashboard: Control actions are unavailable: "
    "AGENT_DASHBOARD_DISABLE_HOST_GUARD=1 no longer enables them on a "
    "non-loopback bind. Turn on LAN mode instead "
    '(README: "Migrating from AGENT_DASHBOARD_DISABLE_HOST_GUARD")'
)

#: LAN mode startup line when no account can sign in yet (spec 2.3).
#: LAN mode still starts in this case.
NO_ACCOUNT_LINE = (
    "agent-dashboard: no Operator account can sign in yet. Create one on the host: "
    "agent-dashboard account add <name> --role controller "
    "(in the container: docker compose exec dashboard "
    "agent-dashboard account add <name> --role controller)"
)


def _startup_fail(exc: Exception) -> None:
    """Print a startup-check failure (spec 2.2) and exit 2."""
    print(f"agent-dashboard: error: {exc}", file=sys.stderr)
    raise SystemExit(2) from exc


def main(argv: list[str] | None = None) -> None:
    import uvicorn

    raw = list(sys.argv[1:] if argv is None else argv)
    if raw[:1] == ["account"]:
        # Account management works in either mode and takes no serve flags,
        # so it never reaches the serve settings below.
        code = run_account(raw[1:])
        if code != 0:
            raise SystemExit(code)
        return

    try:
        settings = load_settings(argv)
    except ValueError as exc:
        _startup_fail(exc)

    if settings.lan_mode:
        # Startup check 5 (spec 2.2): the state directory and the store open
        # and migrate. A failure names the path and exits 2.
        try:
            conn = open_store(
                now=time.time(),
                idle_timeout=settings.sign_in_idle_timeout,
                max_age=settings.sign_in_max_age,
            )
        except StoreError as exc:
            _startup_fail(exc)
        try:
            can_sign_in = any_account_can_sign_in(conn)
        finally:
            conn.close()
        proxies = ", ".join(str(net) for net in settings.trusted_proxies) or "none"
        check = "on" if settings.host_check else "off"
        print(
            "agent-dashboard: LAN mode is on: every request needs a sign-in "
            f"session (bind {settings.host}:{settings.port}, Host check "
            f"{check}, trusted proxies {proxies})"
        )
        if not can_sign_in:
            print(NO_ACCOUNT_LINE)
    elif not is_loopback(settings.host) and not settings.host_check:
        print(MIGRATION_LINE)

    try:
        from .sealed_archive_cache import SealedArchiveCache

        sealed_cache = SealedArchiveCache(
            directory=settings.sealed_archive_cache_dir,
            max_bytes=settings.sealed_archive_cache_max_bytes,
        )
        app = create_app(settings=settings, sealed_archive_cache=sealed_cache)
    except (StoreError, ValueError) as exc:
        _startup_fail(exc)
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        workers=1,
        loop="asyncio",
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
