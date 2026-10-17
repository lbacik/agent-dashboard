# Agent dashboard

A read-only view of the managed instances on one Docker host, built from the telemetry snapshots they report. The contract terms (Dashboard, Managed instance, Telemetry snapshot, Figure basis) are owned by the agent and defined in [simple-coding-agent's CONTEXT.md](https://github.com/lbacik/simple-coding-agent/blob/main/CONTEXT.md); this glossary adds only what the dashboard itself introduces.

## Language

**Poll outcome**:
The dashboard's classification of one attempt to fetch a telemetry snapshot from a managed instance: a good snapshot, the agent unreachable, the operation unsupported, the snapshot failed, or telemetry not supported by the agent's version.
_Avoid_: Poll result, health check, ping

**Reach**:
How current the dashboard's view of a managed instance is: live, stale (the last good snapshot is shown but newer polls failed), unreachable, not running, or unsupported (the agent's version or schema is too old or too new to read).
_Avoid_: Health, status, availability

**Status headline**:
One sentence the dashboard derives from a managed instance's reach and latest telemetry snapshot, saying what the instance is doing or what it needs from the operator. The agent never sends it.
_Avoid_: Summary, status message, state

**Attention rank**:
The order in which the dashboard lists managed instances: those needing the operator first (unreachable, recovery hold, consecutive errors, handoff in progress), then those approaching the soft threshold, then working, idle, and offline ones.
_Avoid_: Priority, severity, sort order
