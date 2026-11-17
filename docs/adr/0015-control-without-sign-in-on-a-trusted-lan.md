# Control actions without sign-in on a trusted LAN, behind one explicit setting

ADR 0010 made LAN mode with sign-in the only way to issue Control actions
beyond loopback, and ADR 0011 allows a password only from a secure context.
A dashboard reached over plain `http://` on a LAN address can therefore show
Control actions only through a TLS reverse proxy or an SSH tunnel. On a
small, trusted home or lab LAN (the Raspberry Pi container install, published
on `0.0.0.0`, often with the `Host` check off) that is more setup than the
operator wants, and the dashboard stays read-only there.

`AGENT_DASHBOARD_UNAUTHENTICATED_CONTROL=on` (flag
`--unauthenticated-control on|off`, default `off`) brings Control actions
back on that deployment, as an explicit opt-in by the operator:

- **Where it applies.** Outside LAN mode, on a non-loopback bind, the
  non-loopback "Controls unavailable" reason no longer applies. On a loopback
  bind the setting changes nothing: Control actions are already available
  there.
- **Not with LAN mode.** Turning it on together with
  `AGENT_DASHBOARD_LAN_MODE=on` is a startup error. In LAN mode the sign-in
  session is the boundary, and silently ignoring the setting would hide a
  misconfiguration.
- **One switch, one meaning.** It does not replace
  `AGENT_DASHBOARD_ALLOW_NON_LOOPBACK` (the bind opt-in) and does not need the
  `Host` check: `AGENT_DASHBOARD_DISABLE_HOST_GUARD=1` stays allowed with it.
- **`control` off still wins.** `AGENT_DASHBOARD_CONTROL=off` keeps the
  dashboard read-only whatever this setting says.
- **Request checks unchanged.** The ADR 0007/0010 checks stay in force:
  `Origin` host and port equal to `Host` (any scheme, so plain `http` works),
  `Sec-Fetch-Site: same-origin` when present, `HX-Request: true`. A page on
  another origin still cannot fire a Control action.
- **Visible state.** When the setting actually lifts the rule, the top bar's
  Control chip reads `Control on · no sign-in`, and startup prints
  `agent-dashboard: Control actions are on without sign-in: anyone who can
  reach <host>:<port> can issue them (AGENT_DASHBOARD_UNAUTHENTICATED_CONTROL=on,
  Host check <on|off>)` instead of the ADR 0010 migration line. The
  non-loopback reason names both ways out: LAN mode, or this setting.
- **Control audit.** With no sign-in there is no Operator, so `operator`
  stays `null`. Every entry gains `client`: the direct TCP peer address,
  never `X-Forwarded-For`, so a click can at least be traced to a device.
  Other entries keep their shape, without the key.

## Accepted risk

Anyone who can reach the port can issue Control actions through a
root-equivalent Docker socket, with no login, no Role, and only a peer
address in the Control audit. With the `Host` check off, a DNS-rebinding page
on any website the operator opens can do the same, because its `Origin`
matches its own `Host`. The `Host` allow-list is the only defence left
against that, and it stays the operator's choice. Use the setting only on a
trusted, isolated LAN. Behind Docker's userland proxy the peer address can
be the bridge gateway rather than the device.

## Considered Options

- **Keep ADR 0010 (TLS proxy or SSH tunnel for Control beyond loopback)**:
  rejected for this deployment. It stays the recommended path; this setting
  is the explicit exception for a LAN the operator trusts.
- **Bring back ADR 0008 (`AGENT_DASHBOARD_DISABLE_HOST_GUARD=1` also enables
  Control)**: rejected again. One switch with two meanings is the reason
  ADR 0010 split it; the operator may want the `Host` check off and stay
  read-only.
- **A new value of `AGENT_DASHBOARD_ALLOW_NON_LOOPBACK`**: rejected. The bind
  opt-in and the Control opt-in are separate decisions; the setting's name
  says what is being given up.
- **Require the `Host` check with the setting**: rejected. On a trusted LAN
  the allow-list is configuration friction the operator declined; the
  DNS-rebinding risk is accepted above instead.
- **Plain HTTP sign-in**: rejected as in ADR 0011; it would send passwords
  in clear text and still need accounts.

## Consequences

- Partly supersedes [ADR 0010](0010-lan-mode-bind-and-host-check.md) (the
  non-loopback "Controls unavailable" reason outside LAN mode) and the
  read-only note of ADR 0002 and ADR 0007, but only when the setting is on.
- `docker-compose.yml` passes the variable through; `.env.example` lists it
  empty.
