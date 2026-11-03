# LAN mode allows any bind, keeps an optional Host check, and gates Control behind sign-in

ADR 0001 made the loopback bind and the `Host` allow-list the dashboard's
boundary, and ADR 0008 let `AGENT_DASHBOARD_DISABLE_HOST_GUARD=1` open
Control actions to a LAN with no login. With sign-in sessions in LAN mode
(ADR 0009) the boundary moves from the address to the sign-in session, so
the bind rules and the per-request checks are redrawn around it.

- **LAN mode is one explicit switch.** Without it the posture of ADR 0001
  and ADR 0007 is unchanged: no sign-in, Control actions only on a loopback
  bind. With it the dashboard refuses to start unless its LAN configuration
  is complete (a `Host` allow-list or the disabled `Host` check, and at
  least one Operator account).
- **Bind.** In LAN mode any address is accepted, loopback (a reverse proxy
  on the same host), a LAN address or `0.0.0.0` (a container), with no
  further opt-in. The default stays `127.0.0.1`.
- **`Host` check.** `AGENT_DASHBOARD_ALLOWED_HOSTS` is the allow-list in
  both modes: `localhost` and `127.0.0.1` are always allowed and `*` is
  dropped at parse time. In LAN mode the list is required unless
  `AGENT_DASHBOARD_DISABLE_HOST_GUARD=1` is set. The variable now does only
  that, in either mode: it turns off the `Host` check, nothing else.
- **`Origin` check.** One rule in both modes, for Control actions and for
  `POST /login` and `POST /logout`: `Origin` is present and its host and
  port equal the `Host` header, with either `http` or `https` as the scheme.
  This replaces the `http://` literal. `Sec-Fetch-Site: same-origin` when
  present and `HX-Request: true` on Control actions stay as in ADR 0007.
  `X-Forwarded-Proto` and `X-Forwarded-Host` are never read.
- **Client address behind a proxy.** `X-Forwarded-For` is read only from
  addresses listed in `AGENT_DASHBOARD_TRUSTED_PROXIES` (empty by default),
  and only for the failed sign-in delay per client address and the stdout
  sign-in lines. It never feeds identity, the `Origin` check or the Control
  audit.
- **"Controls unavailable".** On a loopback dashboard: `--control off`,
  then a non-loopback bind, which the disabled `Host` check no longer
  lifts. In LAN mode: `--control off`, then the `viewer` role; the
  non-loopback reason does not exist there. A request without a valid
  sign-in session is refused by authentication first and never reaches
  Control handling or the Control audit.
- **Outside LAN mode** `AGENT_DASHBOARD_ALLOW_NON_LOOPBACK` stays: the
  ADR 0002 test deployment may still publish to a trusted LAN without
  sign-in, read-only.

## Considered Options

- **LAN mode inferred from a non-loopback bind**: rejected. A reverse proxy
  on the same host reaches the dashboard over loopback and would get no
  sign-in.
- **A configured list of public origins (scheme, host and port)**:
  rejected. Its only job beyond the `Host` allow-list was the scheme behind
  a TLS-terminating proxy, and an `Origin` check that accepts both schemes
  needs no scheme. A page served over `http` on the dashboard's own host
  cannot use it either: the `SameSite=Strict` cookie is not sent across
  schemes, so its request fails authentication.
- **`Host` check always on in LAN mode**: rejected. With sign-in, a DNS
  rebinding page runs on its own origin and never carries the dashboard's
  host-scoped cookie, so it reaches only `/login`; an operator on a trusted
  network may skip the list, as long as that is one explicit setting rather
  than a forgotten one.
- **Keeping ADR 0008 (Control without sign-in when the `Host` check is
  off)**: rejected. The network also carries untrusted devices, and without
  sign-in there is no Operator in the Control audit and no Role.
- **Trusting `X-Forwarded-For` from anyone, or never**: rejected. From
  anyone it lets a client pick its own rate-limit bucket. Never would turn
  the per-address delay into one shared bucket behind a same-host proxy, so
  one attacker would slow everyone's sign-in.

## Consequences

- Supersedes [ADR 0008](0008-disabled-host-guard-allows-control-actions.md).
  A test deployment that set `AGENT_DASHBOARD_DISABLE_HOST_GUARD=1` on a
  non-loopback bind keeps running but turns read-only, with the
  non-loopback reason on the "Controls unavailable" line; that reason
  should point to LAN mode.
- `AGENT_DASHBOARD_ALLOW_NON_LOOPBACK` is redundant in LAN mode.
- Whether the dashboard sets `Secure` and the `__Host-` prefix behind a
  TLS-terminating proxy, which it cannot see, is decided with TLS
  termination.
- Whether a container that publishes only to loopback needs LAN mode for
  Control actions is decided with the container path.

Decided in [Decide what replaces the loopback bind and Host allow-list](https://github.com/lbacik/agent-dashboard/issues/145),
building on [ADR 0009](0009-built-in-password-auth-in-lan-mode.md) and
[Decide operator identity, roles and their place in the Control audit](https://github.com/lbacik/agent-dashboard/issues/144).
