# Run as a host process with the operator's Docker access, bound to loopback

The dashboard has to run `docker exec` in agent containers, and access that can run `exec` is root-equivalent on the host. Everything else on the host runs in a container, but v1 runs the dashboard as a plain host process under the operator's own account. It uses the operator's existing Docker CLI access, so it grants nothing the operator does not already have. The web UI binds to `127.0.0.1` only: the process refuses a non-loopback bind, checks the `Host` header against an allow-list to defeat DNS rebinding, and is reached remotely through an SSH tunnel. One gateway module builds every Docker argv, and in v1 it exposes only "list instances" and "fetch telemetry".

## Considered Options

- **Docker socket mounted into a dashboard container**: rejected. It puts root-equivalent access behind a web-facing process.
- **docker-socket-proxy**: rejected. It cannot restrict *which* command `exec` runs, and allowing `POST /containers/{id}/exec` also allows creating containers.
- **Separate host-side helper owning Docker access**: rejected for v1. It adds a component and reduces no privilege while the dashboard already runs as the operator.
- **Binding on the LAN with built-in auth, or behind an authenticating reverse proxy**: deferred. v2 control needs authentication and CSRF/Origin protection anyway, so those are designed together then.

## Consequences

- The dashboard is installed with `uv tool install` and run as a `systemd --user` unit, not as a compose service.
- Revisit this when the dashboard must be reachable without an SSH tunnel, or when v2 adds commands that change agent state.
- Revisited for Control actions in [ADR 0007](0007-control-actions-on-a-loopback-only-dashboard.md): the posture stays, with request-origin checks added.

Decided in [Decide instance discovery and the dashboard's Docker access](https://github.com/lbacik/simple-coding-agent/issues/226) and [Define the dashboard project](https://github.com/lbacik/simple-coding-agent/issues/228).
