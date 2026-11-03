# Agent dashboard

A view of the managed instances on one Docker host, built from the telemetry snapshots they report, with optional Control actions on live instances (read-only while control is off). The contract terms (Dashboard, Managed instance, Telemetry snapshot, Figure basis) are owned by the agent and defined in [simple-coding-agent's CONTEXT.md](https://github.com/lbacik/simple-coding-agent/blob/main/CONTEXT.md); this glossary adds only what the dashboard itself introduces.

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

**Operator**:
A person using the dashboard to watch managed instances and, when allowed, issue Control actions; on a dashboard reachable only over loopback there is exactly one, and it is unnamed.
_Avoid_: User, admin

**Operator account**:
One operator's named identity on a dashboard reachable beyond loopback, known by an account name that never changes; the Control audit names it on every entry.
_Avoid_: User, login, profile

**Role**:
What an operator account may do: a viewer sees every view, Attempt logs included; a controller may also issue Control actions.
_Avoid_: Permission, access level

**Control action**:
An operator action the dashboard offers on a live managed instance that issues exactly one `agentctl` command to it (stop, stop after N attempts, resume, handoff now, next issue), bound to the container and repository the operator was looking at.
_Avoid_: Button, command (the agent's term for what it records), operation

**Control audit**:
The dashboard's own append-only record of every Control action submission and its outcome at submission time, including those the dashboard refused before reaching the agent; one submission's record is a Control audit entry. The agent's command record remains the authority for what happened to an accepted command afterwards.
_Avoid_: Audit log, command log, history

**LAN mode**:
The explicit opt-in under which the dashboard is reached from a private network without an SSH tunnel and every request needs a sign-in session; without it there is no sign-in and Control actions are offered only on a loopback bind.
_Avoid_: v2, remote mode, network mode

**Sign-in session**:
The period during which one person is signed in to the dashboard in one browser, from signing in until signing out, expiry, or revocation; it exists only when the dashboard is reachable beyond loopback.
_Avoid_: Session, login

**Attempt log**:
One implementation attempt's event history on the managed instance's `/data` volume (`/data/logs/<issue>/<started_at>/`): the `attempt.json` summary, the `agent_output.json` event stream, and the numbered evidence files.
_Avoid_: Session, run log

**Evidence file**:
One numbered file an Attempt log event points at (`NNNN_tool_call.json`, `NNNN_tool_result.json`, `NNNN_model_response.txt`); the tool name comes only from the event's detail, never from the file.
_Avoid_: Attachment, artifact

**Set-password link**:
A one-time, short-lived link issued on the Docker host for one Operator account, with which the person behind that account chooses their own password, the first one and again after every reset; nobody else ever knows it.
_Avoid_: Invite, reset link, token
