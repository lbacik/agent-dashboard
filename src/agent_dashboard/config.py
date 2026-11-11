"""Configuration: AGENT_DASHBOARD_* environment variables with matching CLI flags."""

from __future__ import annotations

import argparse
import ipaddress
import os
from dataclasses import dataclass, field

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_POLL_INTERVAL = 5.0
MIN_POLL_INTERVAL = 2.0
DEFAULT_EXEC_TIMEOUT = 5.0
DEFAULT_LOG_EXEC_TIMEOUT = 15.0
DEFAULT_CONTROL_EXEC_TIMEOUT = 15.0
MIN_CONTROL_EXEC_TIMEOUT = 10.0
DEFAULT_CONTRACT_EXPORT_TIMEOUT = 30.0
DEFAULT_MAX_CONCURRENT_EXECS = 4
DEFAULT_CONTRACT_HISTORY_CAP = 2000
DEFAULT_SIGN_IN_IDLE_TIMEOUT = 1800.0
MIN_SIGN_IN_IDLE_TIMEOUT = 300.0
DEFAULT_SIGN_IN_MAX_AGE = 43200.0
MAX_SIGN_IN_MAX_AGE = 604800.0

ENV_PREFIX = "AGENT_DASHBOARD_"

#: Values of AGENT_DASHBOARD_ALLOW_NON_LOOPBACK that opt in to a non-loopback bind.
TRUTHY = {"1", "true", "yes", "on"}

#: Falsy spellings for the ``control`` setting (anything else is a startup error).
FALSY = {"0", "false", "no", "off"}


@dataclass(frozen=True)
class Settings:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    poll_interval: float = DEFAULT_POLL_INTERVAL
    exec_timeout: float = DEFAULT_EXEC_TIMEOUT
    log_exec_timeout: float = DEFAULT_LOG_EXEC_TIMEOUT
    control_exec_timeout: float = DEFAULT_CONTROL_EXEC_TIMEOUT
    contract_export_timeout: float = DEFAULT_CONTRACT_EXPORT_TIMEOUT
    max_concurrent_execs: int = DEFAULT_MAX_CONCURRENT_EXECS
    contract_history_cap: int = DEFAULT_CONTRACT_HISTORY_CAP
    allow_non_loopback: bool = False
    allowed_hosts: frozenset[str] = field(default_factory=frozenset)
    control: bool = True
    lan_mode: bool = False
    trusted_proxies: tuple = ()
    sign_in_idle_timeout: float = DEFAULT_SIGN_IN_IDLE_TIMEOUT
    sign_in_max_age: float = DEFAULT_SIGN_IN_MAX_AGE
    host_check: bool = True


def is_loopback(host: str) -> bool:
    """True when host is a loopback address (bind refusal check)."""
    h = host.strip().strip("[]").lower()
    if h in ("localhost", "::1"):
        return True
    return h.startswith("127.") and len(h) > 4


def ensure_loopback(host: str, allow_non_loopback: bool = False) -> None:
    if not allow_non_loopback and not is_loopback(host):
        raise ValueError(f"refusing to bind to non-loopback address: {host!r}")


def parse_allowed_hosts(value: str | None) -> frozenset[str]:
    """Parse a comma-separated AGENT_DASHBOARD_ALLOWED_HOSTS value.

    Entries are lowercased hostnames/IPs without ports or brackets. Empty
    entries and a lone ``*`` wildcard are dropped so the allow-list can
    never become allow-all.
    """
    if not value:
        return frozenset()
    hosts: set[str] = set()
    for entry in value.split(","):
        name = entry.strip().lower()
        if not name or name == "*":
            continue
        if name.startswith("[") and name.endswith("]"):
            name = name[1:-1]
        if ":" in name and not name.startswith("["):
            # Host entries must not carry a port; "example:8000" keeps only
            # the hostname part, unbracketed IPv6 stays rejected downstream.
            name = name.split(":")[0]
        name = name.strip("[]").strip()
        if not name or name == "*":
            continue
        hosts.add(name)
    return frozenset(hosts)


def parse_truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in TRUTHY


def parse_control(value: str | None) -> bool:
    """Parse the ``control`` setting: on/off plus truthy/falsy spellings.

    ``None`` (or blank) means the default (on). Anything else is a startup
    error, so a typo never silently disables Control actions.
    """
    if value is None or not value.strip():
        return True
    text = value.strip().lower()
    if text in TRUTHY:
        return True
    if text in FALSY:
        return False
    raise ValueError(
        f"invalid control value: {value!r} (expected on/off, "
        "or a truthy/falsy spelling like true/false, yes/no, 1/0)"
    )


def parse_lan_mode(value: str | None) -> bool:
    """Parse the ``lan_mode`` setting: on/off plus truthy/falsy spellings.

    ``None`` (or blank) means the default (off). Anything else is a startup
    error, so a typo never silently leaves sign-in off.
    """
    if value is None or not value.strip():
        return False
    text = value.strip().lower()
    if text in TRUTHY:
        return True
    if text in FALSY:
        return False
    raise ValueError(
        f"invalid lan-mode value: {value!r} (expected on/off, "
        "or a truthy/falsy spelling like true/false, yes/no, 1/0)"
    )


def parse_trusted_proxies(value: str | None) -> tuple:
    """Parse comma-separated IPv4/IPv6 addresses and CIDR blocks.

    Blank entries and ``*`` are dropped; any other entry that does not
    parse with :func:`ipaddress.ip_network` is a startup error naming it.
    """
    if value is None or not value.strip():
        return ()
    networks: list = []
    for raw in value.split(","):
        entry = raw.strip()
        if not entry or entry == "*":
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            raise ValueError(
                f"invalid trusted proxy: {entry!r} "
                "(expected an IPv4/IPv6 address or CIDR block)"
            ) from None
    return tuple(networks)


def parse_seconds(value: str | None, *, name: str, default: float) -> float:
    """Parse a timeout in seconds; blank means the default.

    A value that is not a number is a startup error naming it.
    """
    if value is None or not value.strip():
        return default
    try:
        return float(value.strip())
    except ValueError:
        raise ValueError(f"invalid {name}: {value!r} (expected seconds)") from None


def _env(name: str) -> str | None:
    value = os.environ.get(ENV_PREFIX + name)
    return value if value not in (None, "") else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-dashboard", description=__doc__)
    parser.add_argument(
        "--host",
        default=None,
        help="Bind address (loopback unless LAN mode or --allow-non-loopback).",
    )
    parser.add_argument("--port", type=int, default=None, help="Bind port.")
    parser.add_argument(
        "--poll-interval", type=float, default=None, help="Discovery period in seconds."
    )
    parser.add_argument(
        "--exec-timeout", type=float, default=None, help="Docker exec timeout in seconds."
    )
    parser.add_argument(
        "--log-exec-timeout",
        type=float,
        default=None,
        help="Attempt log read timeout in seconds.",
    )
    parser.add_argument(
        "--control-exec-timeout",
        type=float,
        default=None,
        help=(
            "Control exec timeout in seconds "
            f"(must be greater than {MIN_CONTROL_EXEC_TIMEOUT:g})."
        ),
    )
    parser.add_argument(
        "--contract-export-timeout",
        type=float,
        default=None,
        help=(
            "Attempt archive export timeout in seconds without progress "
            "(default 30 s)."
        ),
    )
    parser.add_argument(
        "--max-concurrent-execs", type=int, default=None, help="Max concurrent execs."
    )
    parser.add_argument(
        "--contract-history-cap",
        type=int,
        default=None,
        help="Max contract Attempts kept for the All range (default 2000).",
    )
    parser.add_argument(
        "--allow-non-loopback",
        action="store_true",
        default=False,
        help=(
            "Opt in to binding on a non-loopback address outside LAN mode "
            "(Control actions stay unavailable; ignored in LAN mode)."
        ),
    )
    parser.add_argument(
        "--allowed-hosts",
        default=None,
        help="Comma-separated extra Host allow-list entries (no wildcard).",
    )
    parser.add_argument(
        "--control",
        default=None,
        help="Control actions on/off (default on; off is read-only mode).",
    )
    parser.add_argument(
        "--lan-mode",
        default=None,
        help="LAN mode on/off (default off).",
    )
    parser.add_argument(
        "--trusted-proxies",
        default=None,
        help="Comma-separated trusted proxy addresses/CIDR blocks.",
    )
    parser.add_argument(
        "--sign-in-idle-timeout",
        default=None,
        help="Sign-in idle timeout in seconds (at least 300).",
    )
    parser.add_argument(
        "--sign-in-max-age",
        default=None,
        help="Sign-in absolute lifetime in seconds.",
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
    log_exec_timeout = (
        args.log_exec_timeout
        if args.log_exec_timeout is not None
        else float(_env("LOG_EXEC_TIMEOUT") or DEFAULT_LOG_EXEC_TIMEOUT)
    )
    control_exec_timeout = (
        args.control_exec_timeout
        if args.control_exec_timeout is not None
        else float(_env("CONTROL_EXEC_TIMEOUT") or DEFAULT_CONTROL_EXEC_TIMEOUT)
    )
    contract_export_timeout = (
        args.contract_export_timeout
        if args.contract_export_timeout is not None
        else float(_env("CONTRACT_EXPORT_TIMEOUT") or DEFAULT_CONTRACT_EXPORT_TIMEOUT)
    )
    max_execs = (
        args.max_concurrent_execs
        if args.max_concurrent_execs is not None
        else int(_env("MAX_CONCURRENT_EXECS") or DEFAULT_MAX_CONCURRENT_EXECS)
    )
    contract_history_cap = (
        args.contract_history_cap
        if args.contract_history_cap is not None
        else int(_env("CONTRACT_HISTORY_CAP") or DEFAULT_CONTRACT_HISTORY_CAP)
    )

    poll_interval = max(poll, MIN_POLL_INTERVAL)
    if not 1 <= port <= 65535:
        raise ValueError(f"port out of range: {port}")
    if exec_timeout <= 0:
        raise ValueError(f"exec timeout must be positive: {exec_timeout}")
    if log_exec_timeout <= 0:
        raise ValueError(f"log exec timeout must be positive: {log_exec_timeout}")
    # ``not x > floor`` (not ``x <= floor``) so NaN is also a startup
    # error: every comparison with NaN is False, including ``<=``.
    if not control_exec_timeout > MIN_CONTROL_EXEC_TIMEOUT:
        raise ValueError(
            "control exec timeout must be greater than "
            f"{MIN_CONTROL_EXEC_TIMEOUT:g} s: {control_exec_timeout}"
        )
    if not contract_export_timeout > 0:
        raise ValueError(
            f"contract export timeout must be positive: {contract_export_timeout}"
        )
    if max_execs < 1:
        raise ValueError(f"max concurrent execs must be >= 1: {max_execs}")
    if isinstance(contract_history_cap, bool) or contract_history_cap < 1:
        raise ValueError(
            f"contract history cap must be >= 1: {contract_history_cap}"
        )

    control = parse_control(
        args.control if args.control is not None else _env("CONTROL")
    )
    lan_mode = parse_lan_mode(
        args.lan_mode if args.lan_mode is not None else _env("LAN_MODE")
    )
    trusted_proxies = parse_trusted_proxies(
        args.trusted_proxies
        if args.trusted_proxies is not None
        else _env("TRUSTED_PROXIES")
    )
    sign_in_idle_timeout = parse_seconds(
        args.sign_in_idle_timeout
        if args.sign_in_idle_timeout is not None
        else _env("SIGN_IN_IDLE_TIMEOUT"),
        name="sign-in idle timeout",
        default=DEFAULT_SIGN_IN_IDLE_TIMEOUT,
    )
    sign_in_max_age = parse_seconds(
        args.sign_in_max_age
        if args.sign_in_max_age is not None
        else _env("SIGN_IN_MAX_AGE"),
        name="sign-in max age",
        default=DEFAULT_SIGN_IN_MAX_AGE,
    )
    # ``not x >= floor`` (not ``x < floor``) so NaN is also a startup
    # error: every comparison with NaN is False, including ``>=``.
    if not sign_in_idle_timeout >= MIN_SIGN_IN_IDLE_TIMEOUT:
        raise ValueError(
            "sign-in idle timeout must be at least "
            f"{MIN_SIGN_IN_IDLE_TIMEOUT:g} s: {sign_in_idle_timeout}"
        )
    if not sign_in_idle_timeout <= sign_in_max_age <= MAX_SIGN_IN_MAX_AGE:
        raise ValueError(
            "sign-in max age must be between the sign-in idle timeout "
            f"({sign_in_idle_timeout} s) and {MAX_SIGN_IN_MAX_AGE:g} s: "
            f"{sign_in_max_age}"
        )
    allow_non_loopback = bool(
        args.allow_non_loopback or parse_truthy(_env("ALLOW_NON_LOOPBACK"))
    )
    allowed_hosts = parse_allowed_hosts(
        args.allowed_hosts if args.allowed_hosts is not None else _env("ALLOWED_HOSTS")
    )
    host_check = os.environ.get("AGENT_DASHBOARD_DISABLE_HOST_GUARD") != "1"

    if lan_mode:
        # --allow-non-loopback is ignored in LAN mode: any bind is accepted.
        if host_check and not allowed_hosts:
            raise ValueError(
                "LAN mode needs AGENT_DASHBOARD_ALLOWED_HOSTS: the host names "
                "or addresses operators use to reach the dashboard, "
                "comma-separated (or AGENT_DASHBOARD_DISABLE_HOST_GUARD=1 "
                "to turn the Host check off)"
            )
    else:
        ensure_loopback(host, allow_non_loopback)
    return Settings(
        host=host,
        port=port,
        poll_interval=poll_interval,
        exec_timeout=exec_timeout,
        log_exec_timeout=log_exec_timeout,
        control_exec_timeout=control_exec_timeout,
        contract_export_timeout=contract_export_timeout,
        max_concurrent_execs=max_execs,
        contract_history_cap=contract_history_cap,
        allow_non_loopback=allow_non_loopback,
        allowed_hosts=allowed_hosts,
        control=control,
        lan_mode=lan_mode,
        trusted_proxies=trusted_proxies,
        sign_in_idle_timeout=sign_in_idle_timeout,
        sign_in_max_age=sign_in_max_age,
        host_check=host_check,
    )
