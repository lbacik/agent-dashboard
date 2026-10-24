# Issue Control actions from a loopback-only dashboard without authentication

With Control actions the dashboard changes agent state, which ADR 0001 and
ADR 0002 named as the point to revisit their posture. The dashboard stays a
loopback-only host process reached through an SSH tunnel, with no login and
no sessions: anyone who can reach the loopback port already has the
operator's account and Docker access. What changes is that a page on another
origin, open in the same browser, must not be able to fire a Control action.
Every Control action route therefore requires, on top of the `Host`
allow-list, an `Origin` header equal to the origin the `Host` header names
(a missing `Origin` is refused), `Sec-Fetch-Site: same-origin` when that
header is present, and `HX-Request: true`, which turns any cross-origin
attempt into a CORS preflight the dashboard never answers. A refused request
never reaches `docker exec`.

Control actions are refused outright, and the UI says "Controls unavailable",
whenever the process runs with a non-loopback bind (the ADR 0002 test
exception) or with the `Host` guard disabled, and whenever the operator has
switched them off with a control-off setting. Every Control action goes
through an in-page two-step confirmation naming the instance, its repository
and the exact command; no browser dialogs, no typing the instance name.

The dashboard keeps its own Control audit: an append-only JSONL file under
`$XDG_STATE_HOME/agent-dashboard/`, mirrored as one line on stdout. It records
refusals too, because the agent stores only accepted commands. A `submitted`
entry is written before `docker exec` and the outcome after it; if the first
write fails, the command is not sent. The agent's command store stays the
authority for what happens to an accepted command later; the two are joined
by the request ID, which the dashboard generates (`dash-` plus 16 hex
characters) when it renders the confirmation, so a double submit or a retry
after "not submitted" is the agent's idempotent identical retry.

## Considered Options

- **Synchronizer CSRF token**: rejected. It needs a server-side secret or
  session the dashboard does not have; the `Origin`, `Sec-Fetch-Site` and
  custom-header checks are stateless and enough for a single-operator,
  loopback-only app.
- **Built-in authentication or LAN access with control**: deferred to the
  future LAN-access effort. Driving a root-equivalent Docker socket from an
  unauthenticated LAN page is not an acceptable test-environment risk, so
  ADR 0002's exception stays read-only.
- **Confirming only the "dangerous" actions, or typing the instance name**:
  rejected. One rule for every Control action costs one click and needs no
  classification; typing the name guards against nothing a single operator
  on loopback faces.
- **Audit in stdout/journald only**: rejected as the only record. The
  container test deployment and `systemd --user` keep different logs; a
  file gives one place to look. One entry per action written after the
  outcome was rejected because a crash during `docker exec` would leave no
  trace.

## Consequences

- The Control audit file has no built-in rotation; it grows by one or two
  lines per operator click.
- The UI never reads the Control audit; command progress comes from the
  telemetry `control` section.
- The test deployment of ADR 0002 shows the "Controls unavailable" line.
- Partly superseded by
  [ADR 0008](0008-disabled-host-guard-allows-control-actions.md): with
  `AGENT_DASHBOARD_DISABLE_HOST_GUARD=1` Control actions stay available.

Decided in [Decide request protection, confirmations and the audit trail](https://github.com/lbacik/agent-dashboard/issues/96).
