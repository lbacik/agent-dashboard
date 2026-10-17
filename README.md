# agent-dashboard

A read-only web dashboard for a fleet of [simple-coding-agent](https://github.com/lbacik/simple-coding-agent) containers on one Docker host. It discovers agent containers by image label, polls each one with `docker exec <container> agentctl telemetry --json`, and shows what every instance is doing: control state, the current attempt's token budget and context figures, and recent events.

Status: specified, not yet built.

- Design map: [Agent fleet dashboard: map](https://github.com/lbacik/simple-coding-agent/issues/222)
- v1 specification of this project: [Define the dashboard project: repo, layout, stack and delivery slices](https://github.com/lbacik/simple-coding-agent/issues/228), which links the snapshot contract, discovery and Docker access decisions, and the UI prototype.
- Vocabulary: [CONTEXT.md](CONTEXT.md); contract terms (Dashboard, Managed instance, Telemetry snapshot, Figure basis) are defined in the [agent's CONTEXT.md](https://github.com/lbacik/simple-coding-agent/blob/main/CONTEXT.md).
- Decisions: [docs/adr/](docs/adr/)

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

Running instances only: `docker exec` fails on stopped containers, so a
stopped instance renders "Attempt logs are available for running managed
instances only" instead of its log.

Read paths (all on `/data/logs`, all built by the gateway, the only module
that assembles Docker argv): `docker exec` runs fixed collectors — one exec
for the whole attempts list, one for a single attempt's event stream, and
`ls -1` for a run's file names — while single files arrive via `docker cp`
(a tar stream the reader untars, expecting exactly one regular file).
Previews show the first 256 KiB with a visible truncation marker and the
true total; downloads stream the whole file. File names pass a read
allow-list in the route layer and again inside the gateway, so traversal
names are rejected before any Docker call.

Caveat: the log layout is not a contract ([ADR
0003](docs/adr/0003-read-attempt-logs-outside-the-agentctl-contract.md)). An
agent release can move or rename these files and break the view until the
dashboard follows; an unrecognised layout renders "layout not recognised",
never an error.

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
