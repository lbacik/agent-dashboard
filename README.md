# agent-dashboard

A web dashboard for a fleet of [simple-coding-agent](https://github.com/lbacik/simple-coding-agent) containers on one Docker host. It discovers agent containers by image label, polls each one with `docker exec <container> agentctl telemetry --json`, and shows what every instance is doing: control state, the current attempt's token budget and context figures, and recent events. On live instances it can also send Control actions (see below); with control switched off it is a read-only view. By default it listens on loopback only (reach it through an SSH tunnel); optional [LAN mode](#lan-mode) puts every page behind a sign-in with [Operator accounts](#operator-accounts).

- Design map: [Agent fleet dashboard: map](https://github.com/lbacik/simple-coding-agent/issues/222)
- v1 specification of this project: [Define the dashboard project: repo, layout, stack and delivery slices](https://github.com/lbacik/simple-coding-agent/issues/228), which links the snapshot contract, discovery and Docker access decisions, and the UI prototype.
- Vocabulary: [CONTEXT.md](CONTEXT.md); contract terms (Dashboard, Managed instance, Telemetry snapshot, Figure basis) are defined in the [agent's CONTEXT.md](https://github.com/lbacik/simple-coding-agent/blob/main/CONTEXT.md).
- Decisions: [docs/adr/](docs/adr/)

## Fleet overview

Above the fleet table, a collapsible **Fleet overview** panel charts
outcomes and cost per instance. It loads from `/overview` only once it is
visible, so `/` itself never reads attempt summaries and the live table
never waits for it. Its range selector is the drawer's: presets
(`24h`/`7d`/`30d`/`90d`/`All`) plus a fleet-wide 90-day activity strip
(click a day or drag across days). The chosen range is remembered in its
own cookie, apart from the drawer's. Every instance is read in parallel
with a bounded timeout, stopped ones through `docker cp`; an unreadable
instance is listed under the charts and left out of the totals.

## Attempt log view

Each Managed instance's drawer links to its **Attempt history**
(`/instances/{name}/history`), the list of its Implementation attempts,
newest first. Every row links to that attempt's page
(`/instances/{name}/attempts/{issue}/{started}`), which shows the attempt
header beside the progress chart and the full ordered event stream of the
Attempt log, server-paged at 200 rows with level/event/text filters. Events
that point at an Evidence file offer an inline preview under the row
(`open`, or `reply` for model responses; call/result pairs are matched by
order and say so), with a formatted/raw toggle and a download link for the
whole file.

Stopped instances stay readable: `docker exec` fails on stopped containers,
so the attempt list and the per-attempt event stream arrive via `docker cp`
tree copies parsed dashboard-side ([ADR
0005](docs/adr/0005-stopped-instance-logs-via-docker-cp.md)). A stopped
instance whose volume is gone renders "its volume is gone or it holds no
logs" instead of its log.

Read paths (all on `/data/logs`, all built by the gateway, the only module
that assembles Docker argv): `docker exec` runs fixed collectors on running
instances — one exec for the whole attempts list, one for a single attempt's
event stream, and `ls -1` for a run's file names — while stopped instances
read the same bytes via `docker cp` tree copies (`/data/logs` for the list,
one run directory for a single attempt), and single files arrive via
`docker cp` (a tar stream the reader untars, expecting exactly one regular
file).
Previews show the first 256 KiB with a visible truncation marker and the
true total; downloads stream the whole file. File names pass a read
allow-list in the route layer and again inside the gateway, so traversal
names are rejected before any Docker call.

Caveat: the log layout is not a contract ([ADR
0003](docs/adr/0003-read-attempt-logs-outside-the-agentctl-contract.md)). An
agent release can move or rename these files and break the view until the
dashboard follows; an unrecognised layout renders "layout not recognised",
never an error.

## Control actions

On a live managed instance, the drawer offers a **Control console** with
five Control actions, each issuing exactly one `agentctl` command to that
instance via `docker exec`:

- `stop`
- `stop --after N` (1–99 attempts)
- `resume`
- `handoff now`
- `next issue <n>`

Each action takes two steps: "Review…" renders a confirmation showing the
exact `agentctl` command (and the full `docker exec` argv), the instance and
repository it is bound to, and a newly generated request ID; Confirm sends
it. A double submit reuses the same request ID, so the agent treats it as an
identical retry. After the send, a feedback card reports the outcome
(accepted, completed, rejected, outcome unknown, …) and, for an accepted
command, follows it through telemetry until it reaches a terminal state.
A read-only helper, `agentctl command <id>` behind each card's "Check
status" button, re-reads an outcome without sending anything.

**Switching control off.** Control defaults to on. Pass `--control off`
(or set `AGENT_DASHBOARD_CONTROL=off`) for read-only mode: every drawer
shows only "Controls unavailable: control is off
(`AGENT_DASHBOARD_CONTROL=off`, read-only mode)" and every Control action
route answers 403. Each state names its reason after "Controls unavailable:",
first match wins:

1. control is off (above);
2. outside LAN mode, the dashboard is bound to a non-loopback address
   (`--allow-non-loopback`, the Docker test deployment below): Control actions
   beyond loopback need [LAN mode](#lan-mode);
3. in LAN mode, the signed-in account has the `viewer` Role: "your account can
   view but not issue Control actions".

`AGENT_DASHBOARD_DISABLE_HOST_GUARD=1` no longer enables Control actions on a
non-loopback bind; see
[Migrating from AGENT_DASHBOARD_DISABLE_HOST_GUARD](#migrating-from-agent_dashboard_disable_host_guard).

**Timeout.** `--control-exec-timeout` (or
`AGENT_DASHBOARD_CONTROL_EXEC_TIMEOUT`, default 15 s) bounds one control
exec. It must stay greater than 10 s — the agent's own socket timeout for
mutating commands — so a smaller value is a startup error.

**Control audit.** The dashboard's own append-only record of every Control
action submission and its outcome at submission time lives at
`$XDG_STATE_HOME/agent-dashboard/control-audit.jsonl` (when
`XDG_STATE_HOME` is unset, `~/.local/state/agent-dashboard/control-audit.jsonl`).
The directory is created on first write (mode `0700`, file `0600`). Every
entry is also mirrored to stdout as one `control-audit <json>` line, for
journald or `docker logs`. There is no rotation: one or two lines per click.
Each entry carries `operator`: the signed-in account's name in LAN mode,
`null` outside it. The Operator account store, `accounts.sqlite3`, lives in
the same directory.

## Running on the host

```sh
uv run agent-dashboard              # serves http://127.0.0.1:8000
uv run agent-dashboard --port 8080
```

Every setting is a CLI flag or an `AGENT_DASHBOARD_*` environment variable;
a flag wins over the environment, which wins over the default. The process
reads only its own environment and never loads `.env` (that file feeds
Docker Compose). To reuse it for a local run:

```sh
uv run --env-file .env agent-dashboard
```

`DASHBOARD_BIND`, `DASHBOARD_PORT` and `DOCKER_GID` in `.env` are read by
Compose only; the process takes its address from `--host` / `--port` (or
`AGENT_DASHBOARD_HOST` / `AGENT_DASHBOARD_PORT`). Outside LAN mode a
non-loopback `--host` is refused unless `--allow-non-loopback` is given.

## LAN mode

Off by default. With LAN mode on, every request needs a sign-in session:
pages redirect to `/login`, and the top bar shows the signed-in account with
its Role and a "Sign out" button. Any bind address is accepted (`--allow-non-loopback` is
not needed). Prefer binding to the host's LAN address over `0.0.0.0`, and
restrict the port with the host firewall.

| Flag / environment | Default | Meaning |
| --- | --- | --- |
| `--lan-mode on\|off` / `AGENT_DASHBOARD_LAN_MODE` | `off` | Turns sign-in on. Any value other than on/off (or the usual true/false spellings) is a startup error. |
| `--allowed-hosts` / `AGENT_DASHBOARD_ALLOWED_HOSTS` | _(empty)_ | `Host` header allow-list: the names or addresses operators type in the browser, comma-separated. `localhost` / `127.0.0.1` are always allowed. Required in LAN mode unless `AGENT_DASHBOARD_DISABLE_HOST_GUARD=1`. |
| `--trusted-proxies` / `AGENT_DASHBOARD_TRUSTED_PROXIES` | _(none)_ | Comma-separated IPs and CIDR blocks of reverse proxies whose `X-Forwarded-For` gives the client address. Used only for the sign-in delay and the `auth` log lines. |
| `--sign-in-idle-timeout` / `AGENT_DASHBOARD_SIGN_IN_IDLE_TIMEOUT` | `1800` s | A sign-in session ends after this much inactivity. At least 300. |
| `--sign-in-max-age` / `AGENT_DASHBOARD_SIGN_IN_MAX_AGE` | `43200` s | A sign-in session ends this long after sign-in, whatever the activity. Between the idle timeout and 604800 (7 days). |

Example, signing in from the same machine:

```sh
AGENT_DASHBOARD_LAN_MODE=on AGENT_DASHBOARD_ALLOWED_HOSTS=localhost \
  uv run agent-dashboard
```

At startup it prints `agent-dashboard: LAN mode is on: every request needs a
sign-in session (bind …, Host check on|off, trusted proxies …)`. With no
account that can sign in yet it still starts and says so; create one with
`agent-dashboard account add` (see [Operator accounts](#operator-accounts)).
A startup check that fails (an unparsable setting, a missing
`AGENT_DASHBOARD_ALLOWED_HOSTS`, an account store that cannot be opened)
prints `agent-dashboard: error: …` and exits 2.

**HTTPS.** The sign-in and set-password forms accept a password only from a
secure context: a page opened over `https://`, or over `http://` on a
loopback host (`localhost`, `127.0.0.0/8`, `[::1]`, which is what an SSH
tunnel gives). Over plain `http://<lan-address>` the page opens but the
password is refused with "this page was not opened over HTTPS". For LAN
access, put a TLS-terminating reverse proxy in front of the dashboard and
list it in `AGENT_DASHBOARD_TRUSTED_PROXIES`
([ADR 0011](docs/adr/0011-tls-at-a-reverse-proxy-sign-in-from-a-secure-context.md)).

**Failed sign-ins** slow down per account name and per client address: 1 s
after the first failure, doubling up to 60 s, forgotten 15 minutes after the
last failure or on a successful sign-in. Accounts are never locked. The delay
is kept in memory, so a restart clears it.

**Log lines.** Sign-in events go to stdout as `auth <json>` lines (`sign_in`,
`sign_in_failed`, `sign_in_delayed`, `sign_out`, `password_set`,
`origin_refused`). They never enter the Control audit.

## Operator accounts

Accounts are managed only on the host, with `agent-dashboard account …`;
there is no account page in the UI
([ADR 0013](docs/adr/0013-operator-accounts-managed-from-the-host.md)). The
commands work whether or not LAN mode is on, and read `XDG_STATE_HOME` like
the server: the store is `$XDG_STATE_HOME/agent-dashboard/accounts.sqlite3`
(`~/.local/state/agent-dashboard/accounts.sqlite3` when it is unset). Run
them with the same environment as the server, e.g.
`uv run --env-file .env agent-dashboard account list`, or they work on a
different store.

| Command | Effect |
| --- | --- |
| `account add <name> [--role viewer\|controller] [--url <base>]` | Creates an account without a password (Role `viewer` unless given) and prints a Set-password link. |
| `account reset <name> [--url <base>]` | Deletes the password, ends all of the account's sign-in sessions and prints a new link. |
| `account role <name> viewer\|controller` | Changes the Role, effective from the account's next request. |
| `account remove <name>` | Ends the account's sign-in sessions and disables it. The name stays reserved and cannot be reused. |
| `account list` | All accounts with Role, state (`active`, `link pending`, `no password`, `removed`) and open sign-in sessions. |

Names match `^[a-z][a-z0-9-]{0,31}$`. A `viewer` sees everything a
`controller` sees but cannot issue Control actions.

**Adding a collaborator:**

```sh
agent-dashboard account add alice --role controller --url https://dashboard.example.lan
```

```
Created Operator account alice (controller).
Set-password link for alice (works once, until 2026-11-07 10:15 UTC):
  https://dashboard.example.lan/set-password?token=…
Send it to alice only. Open it through the dashboard's https:// address, or an SSH tunnel to localhost.
```

`--url` is the address operators open the dashboard at; without it the link
is printed as a bare `/set-password?token=…` path. The link works once and
for 24 hours; issuing a new one for the account voids the previous one. The
password is 15 to 128 characters. Setting it does not sign the person in:
they sign in at `/login` afterwards.

**Resetting a password** (forgotten password, or a link that expired):
`agent-dashboard account reset <name> --url <base>`, then send the new link.

**The last `controller`.** When `account role … viewer` or `account remove`
leaves no active account with the `controller` Role, the command still
succeeds and warns on stderr that Control actions are now unavailable to
everyone. Grant it again with `agent-dashboard account role <name>
controller`.

## Migrating from AGENT_DASHBOARD_DISABLE_HOST_GUARD

`AGENT_DASHBOARD_DISABLE_HOST_GUARD=1` used to keep Control actions available
on a non-loopback bind (ADR 0008). It now only turns the `Host` check off, in
either mode ([ADR 0010](docs/adr/0010-lan-mode-bind-and-host-check.md)). A
deployment that relied on it keeps running read-only and prints at startup:
`agent-dashboard: Control actions are unavailable:
AGENT_DASHBOARD_DISABLE_HOST_GUARD=1 no longer enables them on a non-loopback
bind. Turn on LAN mode instead`. To get Control actions back:

1. Turn on [LAN mode](#lan-mode) (`AGENT_DASHBOARD_LAN_MODE=on`) and set
   `AGENT_DASHBOARD_ALLOWED_HOSTS`.
2. Create an Operator account with the `controller` Role
   (`agent-dashboard account add <name> --role controller`) and set its
   password through the printed link.
3. Put a TLS reverse proxy in front of the dashboard, or reach it through an
   SSH tunnel to `localhost`, so the sign-in form accepts the password.

## Running in Docker (test deployments)

> Test environments only ([ADR 0002](docs/adr/0002-docker-test-deployment-exception.md)).
> The container mounts the Docker socket (root-equivalent on the host) and the
> UI has no authentication: anyone on the LAN who can reach the port can list
> instances and fetch telemetry. Use only on a trusted, isolated network,
> prefer binding to the host's LAN IP over `0.0.0.0`, and consider restricting
> the port with the host firewall. The supported v1 path stays
> `uv tool install` + the `systemd --user` unit ([ADR 0001](docs/adr/0001-host-process-with-operator-docker-access.md)).
> For [LAN mode](#lan-mode) behind a reverse proxy on the same host
> ([ADR 0012](docs/adr/0012-container-install-is-a-supported-lan-path.md)), add
> the `docker-compose.lan.yml` overlay through `COMPOSE_FILE` in `.env`
> (Docker Compose 2.24+); see the LAN sections of `.env.example`. Operator
> accounts are then managed with
> `docker compose exec dashboard agent-dashboard account …`.

Setup on the test host (e.g. a Raspberry Pi 5, arm64):

```sh
cp .env.example .env   # then edit: DOCKER_GID, DASHBOARD_BIND, AGENT_DASHBOARD_ALLOWED_HOSTS
docker compose build
docker compose up -d
```

The deployment runs as the fixed Compose project `fleet-dashboard`
(top-level `name:` in `docker-compose.yml`), whatever directory the
repository is checked out in. Without a fixed name Compose derives the
project from the directory name, so an agent instance deployed from a
same-named directory lands in the same project: each deployment then
reports the other's container as an orphan, and `docker compose
up/down --remove-orphans` in either directory removes the other
deployment's container. Do not override the name with `-p` /
`--project-name` or `COMPOSE_PROJECT_NAME` for this deployment — those
take precedence over `name:` and reintroduce the collision.

One-time migration for hosts already running the deployment under the
old derived name (`agent-dashboard`): stop the old project first, then
start the new one:

```sh
docker compose -p agent-dashboard down
docker compose up -d
```

Do not add `--remove-orphans` to the stop command: that flag would also
remove a co-located agent instance's container.

Then open `http://<host>:<port>` from another machine on the LAN; the fleet
table is filled by label discovery plus `docker exec agentctl telemetry
--json`, both executed by the host daemon through the mounted socket.

Environment variables (`.env`):

| Variable | Default | Meaning |
| --- | --- | --- |
| `DASHBOARD_BIND` | `127.0.0.1` | Publish address of the dashboard port. Set the Pi's LAN IP (or `0.0.0.0`) to expose the UI; the safe default stays loopback. |
| `DASHBOARD_PORT` | `8000` | Host port mapped to the container's `8000`. |
| `DOCKER_GID` | `999` | GID owning `/var/run/docker.sock` on the daemon host (see below). Passed as `group_add` so the non-root container user can reach the socket, whose GID differs per host. |
| `AGENT_DASHBOARD_ALLOWED_HOSTS` | _(empty)_ | Host header allow-list for LAN access: the Pi's hostname and/or LAN IP, comma-separated. `localhost` / `127.0.0.1` always stay allowed; `*` is never accepted. |
| `AGENT_DASHBOARD_POLL_INTERVAL` / `AGENT_DASHBOARD_EXEC_TIMEOUT` / `AGENT_DASHBOARD_MAX_CONCURRENT_EXECS` | _(built-in defaults)_ | Optional gateway tuning, passed through to the dashboard. |

`DOCKER_GID` must match the socket on the host that runs the daemon. Compose
reads `.env` on the machine you invoke it from, so when deploying through a
remote Docker context (e.g. from a laptop to the Pi), `getent group docker` on
the laptop gives the wrong number. Read the GID through the same context
instead:

```sh
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock alpine stat -c %g /var/run/docker.sock
```

A mismatch surfaces in the UI as `Discovery failed: permission denied running
docker ps ... /var/run/docker.sock: connect: permission denied`; fix the value
and run `docker compose up -d` to recreate the container.

Inside the container the process listens on `0.0.0.0` with
`AGENT_DASHBOARD_ALLOW_NON_LOOPBACK=1` (compose sets both); without that
explicit opt-in the dashboard still refuses a non-loopback bind, so the
host-process install is unchanged. No shared Docker network or exposed agent
ports are needed: the dashboard never connects to agents over the network.
