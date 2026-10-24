# Containerised dashboard on a trusted test LAN (exception to ADR 0001)

ADR 0001 runs the dashboard as a host process under the operator's account,
bound to loopback and reached through an SSH tunnel. That stays the supported
v1 path. This record covers a deliberate **test-environment exception**: on an
isolated LAN (a Raspberry Pi 5 on arm64) the dashboard runs as a container,
with the Docker socket mounted and the UI published to the LAN without
authentication.

## Why

The test host needs `docker compose up -d` and a URL other machines on the
LAN can open directly. An SSH tunnel per viewer is unnecessary overhead on a
trusted network. The gateway needs no change: it already builds `docker` argv,
and the host daemon executes both discovery (`docker ps --filter label=...`)
and telemetry (`docker exec <container> agentctl telemetry --json`). The
dashboard container therefore carries a static Docker client binary (no daemon)
and mounts `/var/run/docker.sock`; the process inside listens on `0.0.0.0` so
the port mapping works, and runs as a non-root user whose socket access comes
from the compose `group_add` entry with the host's `docker` group GID.

## Exposure controls

Two safeguards that enforce the ADR 0001 posture stay on by default and are
lifted only by explicit opt-in:

- **Bind refusal.** The process still refuses a non-loopback
  `AGENT_DASHBOARD_HOST` unless `AGENT_DASHBOARD_ALLOW_NON_LOOPBACK` is set to
  a truthy value (`1`/`true`/`yes`/`on`) or `--allow-non-loopback` is passed.
  The `uv tool install` / systemd path is unchanged because it never sets the
  flag.
- **Host header allow-list.** `localhost` / `127.0.0.1` always stay allowed;
  LAN hostnames/IPs are added through `AGENT_DASHBOARD_ALLOWED_HOSTS`
  (comma-separated) or `--allowed-hosts`. A `*` entry is dropped at parse time
  and never matches, so the DNS-rebinding protection stays meaningful.

Compose publishes `${DASHBOARD_BIND:-127.0.0.1}:${DASHBOARD_PORT:-8000}:8000`,
so the safe default stays loopback and LAN exposure happens only through the
host's `.env` (`DASHBOARD_BIND` plus the allow-list).

## Accepted risk

The UI has no authentication and the mounted socket is root-equivalent on the
host: anyone on the LAN who can reach the port can drive the v1 gateway
("list instances" and "fetch telemetry" only). Acceptable solely on a trusted,
isolated test network. Prefer binding `DASHBOARD_BIND` to the Pi's LAN IP
rather than `0.0.0.0`, and restrict the port with the host firewall.

## Considered alternatives

- **SSH tunnel to a loopback-only port**: rejected here as unnecessary overhead
  on a trusted LAN (still the recommended remote-access path for the
  host-process install).
- **`DOCKER_HOST=ssh://user@host`**: no socket mount, but needs an SSH key in
  the container (same risk in another form). Only useful when the dashboard
  runs on a different machine than the agents.
- **docker-socket-proxy**: already rejected in ADR 0001; it cannot restrict
  which command `exec` runs.

## Consequences

- `Dockerfile` (multi-arch client binary, non-root user), `docker-compose.yml`
  (single `dashboard` service), `.dockerignore`, and `.env.example` exist for
  test deployments only and are not the supported v1 production path.
- Revisit when v2 adds authentication (then LAN exposure can be reconsidered)
  or commands that change agent state (then the socket-in-container tradeoff
  must be re-evaluated).
- Revisited for Control actions in
  [ADR 0007](0007-control-actions-on-a-loopback-only-dashboard.md): this
  exception stays read-only; Control actions are refused under a
  non-loopback bind. [ADR 0008](0008-disabled-host-guard-allows-control-actions.md)
  lifts that when `AGENT_DASHBOARD_DISABLE_HOST_GUARD=1` is set.
