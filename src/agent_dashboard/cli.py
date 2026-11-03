"""Console entry point: agent-dashboard."""

from __future__ import annotations

import sys

from .app import create_app
from .config import is_loopback, load_settings

#: Migration line for ADR 0008 deployments (spec 2.4).
MIGRATION_LINE = (
    "agent-dashboard: Control actions are unavailable: "
    "AGENT_DASHBOARD_DISABLE_HOST_GUARD=1 no longer enables them on a "
    "non-loopback bind. Turn on LAN mode instead "
    '(README: "Migrating from AGENT_DASHBOARD_DISABLE_HOST_GUARD")'
)


def main(argv: list[str] | None = None) -> None:
    import uvicorn

    try:
        settings = load_settings(argv)
    except ValueError as exc:
        print(f"agent-dashboard: error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    if settings.lan_mode:
        proxies = ", ".join(str(net) for net in settings.trusted_proxies) or "none"
        check = "on" if settings.host_check else "off"
        print(
            "agent-dashboard: LAN mode is on: every request needs a sign-in "
            f"session (bind {settings.host}:{settings.port}, Host check "
            f"{check}, trusted proxies {proxies})"
        )
    elif not is_loopback(settings.host) and not settings.host_check:
        print(MIGRATION_LINE)

    app = create_app(settings=settings)
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
