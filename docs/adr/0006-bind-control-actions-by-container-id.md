# Bind Control actions by container ID with a dashboard-side repository check

A Control action must reach the managed instance and target repository the
operator was looking at, never a container that replaced it under the same
name. The dashboard binds each Control action to the **container ID** it
rendered (the name appears only in URLs) and, at submission, checks that this
ID is still discovered with Reach live and that its latest good snapshot's
`identity.repository` equals the repository the form carried. After a
successful reply it also compares the reply's `repository:` line. Because
`TARGET_REPO` is container environment, it cannot change under a fixed
container ID, so this check closes the real risk without agent changes.

## Considered Options

- **Address by name, resolved at submission**: rejected. A recreated
  container keeps its name, so a stale view could command an instance the
  operator never saw.
- **Require an agent-side `--expect-repo` guard**: rejected as a requirement.
  It would gate every Control action on a new agent release and lock out
  older agents, while protecting only against a dashboard bug. It stays on
  the agent-side change list as defence in depth; the dashboard passes it
  when `identity.agent_version` says the agent supports it and does not
  require it otherwise.
- **A snapshot-age token on the form**: rejected. With a 5 s poll it would
  produce spurious rejections, and the agent already rejects commands whose
  preconditions no longer hold, changing nothing.

## Consequences

- A mismatch on the reply's `repository:` line means the command was already
  applied; the dashboard reports it as an error with the request ID and
  records it in the audit trail, but undoes nothing.
- Flags added by the agent later are gated by a minimum-version table read
  against `identity.agent_version`; an unreadable version counts as old.

Decided in [Decide the command set's availability rules and target binding](https://github.com/lbacik/agent-dashboard/issues/95).
