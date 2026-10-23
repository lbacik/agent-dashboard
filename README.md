# agent-dashboard

A web dashboard for a fleet of [simple-coding-agent](https://github.com/lbacik/simple-coding-agent) containers on one Docker host. It discovers agent containers by image label, polls each one with `docker exec <container> agentctl telemetry --json`, and shows what every instance is doing: control state, the current attempt's token budget and context figures, and recent events. On live instances it can also send Control actions (see below); with control switched off it is a read-only view.

Status: specified, not yet built.

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
route answers 403. Controls also switch themselves off when the dashboard
is bound to a non-loopback address (`--allow-non-loopback`, the Docker test
deployment below) or when the Host guard is disabled
(`AGENT_DASHBOARD_DISABLE_HOST_GUARD=1`); each state names its reason after
"Controls unavailable:".

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

## Running in Docker (test deployments)

> Test environments only ([ADR 0002](docs/adr/0002-docker-test-deployment-exception.md)).
> The container mounts the Docker socket (root-equivalent on the host) and the
> UI has no authentication: anyone on the LAN who can reach the port can list
> instances and fetch telemetry. Use only on a trusted, isolated network,
> prefer binding to the host's LAN IP over `0.0.0.0`, and consider restricting
> the port with the host firewall. The supported v1 path stays
> `uv tool install` + the `systemd --user` unit ([ADR 0001](docs/adr/0001-host-process-with-operator-docker-access.md)).

Setup on the test host (e.g. a Raspberry Pi 5, arm64):

```sh
cp .env.example .env   # then edit: DOCKER_GID, DASHBOARD_BIND, AGENT_DASHBOARD_ALLOWED_HOSTS
docker compose build
docker compose up -d
```

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
