# A disabled Host guard lifts the loopback rule for Control actions

ADR 0007 refused Control actions whenever the dashboard was bound to a
non-loopback address or ran with the `Host` guard disabled, which left the
ADR 0002 test deployment read-only. On that trusted, isolated test LAN the
operator needs to drive agents from the dashboard, not only watch them.

`AGENT_DASHBOARD_DISABLE_HOST_GUARD=1` is now the single opt-out from the
loopback posture: when it is set, the dashboard checks neither the bind
address nor the `Host` header, and Control actions stay available. Without it
nothing changes: a non-loopback bind still shows "Controls unavailable", and
`--control off` still wins over everything. The per-request checks of
ADR 0007 (`Origin` equal to the origin the `Host` header names,
`Sec-Fetch-Site: same-origin` when present, `HX-Request: true`) and the
Control audit stay in force, so a page on another origin still cannot fire a
Control action.

## Accepted risk

With the guard disabled, anyone on the LAN who can reach the port can issue
Control actions through a root-equivalent Docker socket, with no login.
Acceptable only on the trusted, isolated test network of ADR 0002; the
supported host-process install never sets the variable.

## Considered Options

- **Keep ADR 0007 (test deployment read-only)**: rejected; the test
  deployment is where Control actions are exercised.
- **A separate opt-in just for Control on a non-loopback bind**: rejected as
  a second switch with the same meaning; disabling the `Host` guard already
  states that the operator has left the loopback posture.

## Consequences

- Supersedes the "non-loopback bind or `Host` guard disabled" part of
  ADR 0007 and the "this exception stays read-only" note in ADR 0002, but only
  when the variable is set.
- The "the Host guard is disabled" reason no longer exists.
