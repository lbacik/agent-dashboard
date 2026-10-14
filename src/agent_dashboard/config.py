"""Configuration: AGENT_DASHBOARD_* environment variables with matching CLI flags."""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_POLL_INTERVAL = 5.0
MIN_POLL_INTERVAL = 2.0
DEFAULT_EXEC_TIMEOUT = 5.0
DEFAULT_MAX_CONCURRENT_EXECS = 4

ENV_PREFIX = "AGENT_DASHBOARD_"


@dataclass(frozen=True)
class Settings:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    poll_interval: float = DEFAULT_POLL_INTERVAL
    exec_timeout: float = DEFAULT_EXEC_TIMEOUT
    max_concurrent_execs: int = DEFAULT_MAX_CONCURRENT_EXECS


def is_loopback(host: str) -> bool:
    """True when host is a loopback address (bind refusal check)."""
    h = host.strip().strip("[]").lower()
    if h in ("localhost", "::1"):
        return True
    return h.startswith("127.") and len(h) > 4


def ensure_loopback(host: str) -> None:
    if not is_loopback(host):
        raise ValueError(f"refusing to bind to non-loopback address: {host!r}")


def _env(name: str) -> str | None:
    value = os.environ.get(ENV_PREFIX + name)
    return value if value not in (None, "") else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-dashboard", description=__doc__)
    parser.add_argument("--host", default=None, help="Bind address (loopback only).")
    parser.add_argument("--port", type=int, default=None, help="Bind port.")
    parser.add_argument(
        "--poll-interval", type=float, default=None, help="Discovery period in seconds."
    )
    parser.add_argument(
        "--exec-timeout", type=float, default=None, help="Docker exec timeout in seconds."
    )
    parser.add_argument(
        "--max-concurrent-execs", type=int, default=None, help="Max concurrent execs."
    )
    return parser


def load_settings(argv: list[str] | None = None) -> Settings:
    """Precedence: CLI flags > AGENT_DASHBOARD_* env > defaults."""
    args = build_parser().parse_args(argv)

    host = args.host if args.host is not None else (_env("HOST") or DEFAULT_HOST)
    port = args.port if args.port is not None else int(_env("PORT") or DEFAULT_PORT)
    poll = (
        args.poll_interval
        if args.poll_interval is not None
        else float(_env("POLL_INTERVAL") or DEFAULT_POLL_INTERVAL)
    )
    exec_timeout = (
        args.exec_timeout
        if args.exec_timeout is not None
        else float(_env("EXEC_TIMEOUT") or DEFAULT_EXEC_TIMEOUT)
    )
    max_execs = (
        args.max_concurrent_execs
        if args.max_concurrent_execs is not None
        else int(_env("MAX_CONCURRENT_EXECS") or DEFAULT_MAX_CONCURRENT_EXECS)
    )

    poll_interval = max(poll, MIN_POLL_INTERVAL)
    if not 1 <= port <= 65535:
        raise ValueError(f"port out of range: {port}")
    if exec_timeout <= 0:
        raise ValueError(f"exec timeout must be positive: {exec_timeout}")
    if max_execs < 1:
        raise ValueError(f"max concurrent execs must be >= 1: {max_execs}")

    ensure_loopback(host)
    return Settings(
        host=host,
        port=port,
        poll_interval=poll_interval,
        exec_timeout=exec_timeout,
        max_concurrent_execs=max_execs,
    )
