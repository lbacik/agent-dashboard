# The container install is a supported LAN path

ADR 0002 ran the dashboard as a container only as a test-environment
exception: the Docker socket mounted, the UI published to an isolated LAN,
no sign-in. With built-in sign-in in LAN mode (ADR 0009), the bind and
`Host` rules redrawn around it (ADR 0010), and TLS terminated by a reverse
proxy (ADR 0011), whose tested setup is nginx-proxy and reaches only
containers on its Docker network, the container becomes a supported way to
run the dashboard in LAN mode, next to the host-process install.

- **The socket behind sign-in is accepted.** The mounted socket is
  root-equivalent on the host, and so is the `docker` group membership of
  the host-process install (ADR 0001). A compromised web process gets the
  same reach in both; in the container it gets no operator account on top.
  Who may reach the socket through the dashboard is decided by sign-in and
  Role, the same in both installs.
- **Outside LAN mode the container is read-only.** Inside the container the
  process always binds `0.0.0.0` and cannot see that Docker publishes the
  port only to `127.0.0.1`, so a container reached through an SSH tunnel
  shows "Controls unavailable" with the non-loopback reason. Control actions
  in the container need LAN mode, which over the tunnel works with sign-in
  from `http://localhost` (ADR 0011). Publishing to a trusted LAN without
  sign-in stays the ADR 0002 exception, read-only.
- **State lives on a named volume.** The base `docker-compose.yml` always
  mounts a named volume and points `XDG_STATE_HOME` at it, so the Control
  audit, sign-in sessions and Operator accounts survive a recreated
  container. The image creates the directory owned by the non-root user, so
  the volume inherits that owner. The container needs no secret: sign-in
  sessions are server-side and the cookie holds only a random ID.
- **LAN mode behind a proxy is an overlay, not a recipe.**
  `docker-compose.lan.yml` turns on LAN mode, joins an external Docker
  network named in `.env` (the proxy's), and keeps the port published to
  `127.0.0.1` only, so the proxy is the only way in from the LAN and the SSH
  tunnel still works with sign-in. It passes the nginx-proxy variables
  (`VIRTUAL_HOST`, `VIRTUAL_PORT`, `LETSENCRYPT_HOST`) through, documented in
  `.env.example` as the tested example. The repo ships no proxy. Without the
  overlay the base compose behaves as before: loopback publish, no LAN mode.
- **`AGENT_DASHBOARD_TRUSTED_PROXIES` takes addresses and CIDR blocks**,
  IPv4 and IPv6, mixed and comma-separated, empty by default, with `*`
  dropped at parse time. A proxy's address on a Docker network changes when
  the network or the proxy is recreated; a block such as the network's
  subnet does not. Too wide a block only lets a client in it choose its own
  failed sign-in delay bucket, never an identity (ADR 0010).
- **Hardening in the base compose**, for both modes: `read_only: true` with
  a `tmpfs` at `/tmp`, `cap_drop: [ALL]` and
  `security_opt: [no-new-privileges:true]`, on top of the existing non-root
  user. The socket stays root-equivalent; these only narrow what a
  compromise that does not reach it can do or leave behind.
- **Migration from ADR 0008.** No grace period: a deployment that sets
  `AGENT_DASHBOARD_DISABLE_HOST_GUARD=1` on a non-loopback bind outside LAN
  mode turns read-only at once. The "Controls unavailable" reason points to
  LAN mode, startup writes one stdout line saying that Control actions need
  LAN mode, and the README gets a migration section (turn on LAN mode,
  create an Operator account, put a TLS proxy in front or use the tunnel).
- **The image is still built locally.** Its tag drops `:test`; publishing a
  multi-arch image is a separate effort.

## Considered Options

- **Keep the container a test-only exception, LAN mode supported only for
  the host process**: rejected. The tested TLS setup assumes the container,
  so it would be an unsupported configuration, and ADR 0009 already chose
  one sign-in mechanism for both installs.
- **`network_mode: host` with a loopback bind, so the tunnelled container
  keeps Control actions without sign-in**: rejected. A second compose
  variant, the container sees the whole host network, and nginx-proxy cannot
  reach it on a Docker network.
- **A setting that declares "published only to loopback"**: rejected. The
  container cannot check it, so it is ADR 0008 under another name.
- **A bind-mounted state directory**: rejected. The host directory needs a
  manual `chown` to the image's UID, which differs per host.
- **A state volume only in LAN mode**: rejected. Without it the Control audit
  is lost silently on every recreated container in either mode.
- **Shipping nginx-proxy and acme-companion in the compose file**: rejected.
  A step-by-step proxy recipe (ADR 0011), and a second proxy next to the one
  the operator already runs.
- **Proxy requirements in the README only, no overlay**: rejected. Joining
  the proxy network without publishing the port to the LAN is easy to get
  wrong by hand.
- **Trusted proxies as host names resolved at startup**: rejected. The
  address changes when the proxy restarts and the dashboard would not notice.
- **A grace period that keeps ADR 0008**: rejected. It would prolong Control
  actions without sign-in on a network that also carries untrusted devices.
- **Refusing to start with the `Host` check off on a non-loopback bind
  outside LAN mode**: rejected. It breaks deployments that only watch.

## Consequences

- Partly supersedes
  [ADR 0002](0002-docker-test-deployment-exception.md): the container is no
  longer test-only. ADR 0002 remains only for publishing to a trusted LAN
  without sign-in, outside LAN mode, read-only.
- The header of `docker-compose.yml`, the `Dockerfile` comment and the README
  section "Running in Docker (test deployments)" stop calling the container
  test-only and describe both uses.
- Account management in the container runs through
  `docker compose exec`, against the store on the state volume.
- Whether the Docker client needs a writable `~/.docker` under
  `read_only: true` (then `DOCKER_CONFIG` points at the `tmpfs`) is checked
  on the test host when this is built.

Decided in [Decide whether the container deployment becomes a supported LAN path](https://github.com/lbacik/agent-dashboard/issues/147),
building on [ADR 0009](0009-built-in-password-auth-in-lan-mode.md),
[ADR 0010](0010-lan-mode-bind-and-host-check.md) and
[ADR 0011](0011-tls-at-a-reverse-proxy-sign-in-from-a-secure-context.md).
