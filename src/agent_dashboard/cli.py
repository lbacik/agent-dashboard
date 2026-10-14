"""Console entry point: agent-dashboard."""

from __future__ import annotations

import sys

from .app import create_app
from .config import load_settings


def main(argv: list[str] | None = None) -> None:
    import uvicorn

    try:
        settings = load_settings(argv)
    except ValueError as exc:
        print(f"agent-dashboard: error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    app = create_app(settings=settings)
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        workers=1,
        loop="asyncio",
    )


if __name__ == "__main__":
    main()
