# agentctl command replies and request-ID semantics

Research for [#94](https://github.com/lbacik/agent-dashboard/issues/94),
part of the map [Instance control actions from the dashboard](https://github.com/lbacik/agent-dashboard/issues/93).

**Question.** What does the dashboard get back when it runs a mutating
`agentctl` command via
`docker exec --user agent <id> /usr/local/bin/agentctl ...`, and is the
telemetry snapshot enough to tell when each command makes sense?

**Sources.** simple-coding-agent at commit `9820906` (v0.7.2, `main`). All
`file:line` citations below are into that repository. The CLI output samples
were observed by running the real `agentctl` entry point
(`python -m simple_coding_agent.agentctl --socket ...`) against a real
`ControlServer` and `ControlStore` with a stub lifecycle surface (no
containers, no GitHub). Docs were read but are cited only where they agree
with the code; disagreements are listed under [Doc drift](#doc-drift).

## Answer in brief

- Mutating commands print **human `key: value` text**, not JSON. There is no
  machine-readable mode for `stop`, `resume`, `handoff now`, `next issue` or
  `command <id>`; only `telemetry --json` is JSON.
- Exit codes: **0** = the agent committed the command (acknowledgement
  `accepted` or `completed`, on stdout); **1** = rejected, nothing changed
  (reason in English on stderr); **2** = agent unreachable, timed out or lost
  connection (ambiguous: the command may still have committed) **and also**
  any argparse usage error.
- Request IDs are free-form strings (non-empty, at most 128 characters). The
  dashboard can supply its own with `--request-id`. An identical retry
  returns the stored record without re-applying; reusing an ID with another
  command or payload is rejected (exit 1).
- A command moves through `accepted` then one terminal state: `completed`,
  `superseded` or `not fulfilled`. Records are never pruned; telemetry
  carries the last 10 by sequence.
- Every command replies on **acceptance**; none blocks until applied. The
  CLI's socket timeout is 10 s, but acceptance can wait on the control lock
  (held across the GitHub claim) and `next issue` does a GitHub fetch before
  replying.
- All five commands exist since v0.2.0; `telemetry` since v0.5.0. Any
  instance the dashboard can poll supports all five.
- The telemetry `control` section shows almost every precondition and
  effect. The gaps are listed at the end.
- A successful reply names `TARGET_REPO` (`repository:` first line).
  Rejections, unreachable errors and "no such command" do **not**, although
  the socket reply carries it. The snapshot's `identity.repository` and
  `control.repository` hold the same value (`config.target_repo`).

## 1. Reply format and exit codes

### Transport underneath the CLI

`agentctl` sends one JSON line over the Unix socket
`/run/simple-coding-agent/control.sock` and reads one JSON line back
(`control_server.py:629-679`). The socket reply to a mutating op is
`{"ok": true, "repository": ..., "request_id", "sequence", "kind",
"acknowledgement", "detail", "created_at", "updated_at"}`
(`control_server.py:626`, `control.py:2040-2051`) or
`{"ok": false, "repository", "error", "request_id"}`
(`control_server.py:604-625`). The CLI renders only part of that.

### What the CLI prints

| Case | Exit | stdout | stderr | Source |
|---|---|---|---|---|
| Accepted or completed | 0 | `repository:`, `request_id:`, `sequence:`, `acknowledgement:`, `effect:` lines | empty | `agentctl.py:212-219`, `:252-253` |
| Rejected by the agent (precondition, request-ID reuse, invalid value) | 1 | empty | `<id> rejected — <reason>. No control change was accepted.` | `agentctl.py:245-251` |
| Agent could not commit (SQLite error) | 1 | empty | `<id> rejected — Command could not be committed: ...` | `control_server.py:615-625` |
| Rejected locally before sending: `next issue` bad number | 1 | empty | `<id> rejected — Invalid issue number ...` | `agentctl.py:566-570` |
| Rejected locally before sending: `stop --after` bad N | 1 | empty | `rejected — Invalid --after value ...` (**no request ID**) | `agentctl.py:541-545` |
| Agent unreachable, timeout, lost connection, unreadable reply | 2 | empty | `<id> not submitted — cannot connect ... Retry with: agentctl <kind> --request-id <id>` | `agentctl.py:236-244` |
| argparse usage error (unknown subcommand or flag) | 2 | empty | `usage: ...` and `agentctl: error: ...` | argparse default |

Observed (abridged):

```text
$ agentctl stop --request-id dash-0001           # idle instance
exit=0
repository: example-owner/example-repo
request_id: dash-0001
sequence: 1
acknowledgement: completed
effect: intake stopped

$ agentctl stop --after 2 --request-id dash-0011  # active attempt
exit=0
repository: example-owner/example-repo
request_id: dash-0011
sequence: 7
acknowledgement: accepted
effect: stop after 2 attempts; active attempt 2026-10-23T10-00-00-attempt-0042 is count one

$ agentctl handoff now --request-id dash-0003     # no active attempt
exit=1
stderr: dash-0003 rejected — handoff now is rejected without an active attempt; no control change was accepted.. No control change was accepted.

$ agentctl stop --request-id dash-0020           # socket missing
exit=2
stderr: dash-0020 not submitted — cannot connect to the agent process: Cannot connect to the agent process at .../control.sock: [Errno 2] No such file or directory. The agent may not be running in this instance. Retry with: agentctl stop --request-id dash-0020
```

The stdout block omits `kind`, `created_at` and `updated_at`
(`agentctl.py:212-219`). `effect:` is the record's `detail`, free English
text.

### Read-only commands

- `agentctl command <id>`: same five-line block on stdout, exit 0
  (`agentctl.py:601-607`). An unknown ID prints
  `<id>: no such command is recorded.` to stderr, exit 1. If the agent
  answers `ok: false` (for example `Lookup is unavailable`), the CLI raises
  `ControlUnavailableError` and exits **2**, the same as unreachable
  (`agentctl.py:203-205`, `:651-653`).
- `agentctl status`: human text only, exit 0; `ok: false` also becomes exit 2
  (`agentctl.py:118-124`).

### Machine-readable mode

None for mutating commands, `command` or `status`. The parser defines
`--json` only on `telemetry` (`agentctl.py:459-468`). The telemetry doc says
`status` does not get `--json` in v1 (`docs/agents/telemetry-snapshot.md:136-138`).

### Exit-code collisions the dashboard must handle

- **Exit 2** means three things: unreachable or ambiguous (retry with the same
  ID), argparse usage error (version skew, retrying won't help), and, for
  `command`/`status`, "the agent answered with an error". To tell them apart,
  look for a `usage:` / `agentctl: error:` prefix on stderr.
- **Exit 1** covers both a real rejection (precondition, ID reuse) and a
  storage failure the agent could not commit (`Command could not be
  committed`). Neither changed anything, so the UI can treat them alike,
  but only the English text tells them apart.
- `docker exec` returns its own non-zero codes when the container is gone or
  not running. The dashboard's gateway already classifies these for
  telemetry.

## 2. Request IDs

- **Who generates them.** The CLI generates `req-` plus 12 hex characters
  from `uuid4` when `--request-id` is absent (`agentctl.py:45-48`, used at
  `:539`, `:554`, `:557`, `:565`).
- **Dashboard-supplied IDs.** Every mutating subcommand accepts
  `--request-id` (`agentctl.py:400-405`, `:416-421`, `:434-439`, `:451-456`).
  The agent validates only that the ID is a non-empty string of at most 128
  characters (`control.py:2011-2015`). Observed: a 129-character ID gives
  exit 1, `Request ID must be at most 128 characters`. A prefix such as
  `dash-<uuid>` is valid. To keep argparse from reading an ID that starts
  with `-` as an option, pass it as `--request-id=<id>`.
- **Scope.** IDs are unique per instance: `commands.request_id` is the primary
  key of that instance's `$DATA_DIR/state/control.sqlite3`
  (`control.py:1627-1638`). Two instances may hold the same ID.
- **Identical retry.** The submit path looks up the ID first. If the stored
  kind and canonical payload (`json.dumps(payload, sort_keys=True)`) match,
  it rolls back and returns the stored record, so the sequence and the
  current acknowledgement come back unchanged and nothing is re-applied
  (`control.py:248-254`, `:387-393`, `:510-516`, `:612-618`, `:682-688`,
  `:1921-1941`). The retry returns the record's **current** acknowledgement,
  which may already be `completed` or `superseded`. Observed: a repeated
  `stop --request-id dash-0001` returned the same `sequence: 1`. Tests:
  `tests/test_control_socket.py:91-99`, `tests/test_stop_after.py:337`,
  `tests/test_next_issue.py:121`.
- **Retry with a different payload.** Rejected with `PayloadMismatchError`,
  exit 1, nothing applied: `Request ID <id> was already used for a different
  command` (other kind) or `... with a different payload` (same kind, for
  example another issue number) (`control.py:1928-1941`). Observed for
  `resume --request-id dash-0001` after `stop` and for `next issue 18` after
  `next issue 17` with the same ID.
- **`next issue` retry skips GitHub.** If the ID already exists, the lifecycle
  goes straight to the store without re-verifying eligibility
  (`lifecycle.py:1529-1539`; test `tests/test_next_issue.py:273`).
- **Payload normalisation.** `stop --after` stores `{"after": N}` and
  `next issue` stores `{"issue": N}` after integer parsing
  (`control.py:513`, `:615`). `stop`, `resume` and `handoff` store `{}`.
  `stop` and `stop --after` are different kinds, so one ID cannot be reused
  across them.

## 3. `agentctl command <id>` and acknowledgement states

### Reply format

Same block as a successful submission, with `repository:` from the socket
reply merged in (`agentctl.py:196-209`; socket reply at
`control_server.py:504-538`). Observed:

```text
$ agentctl command dash-0006
repository: example-owner/example-repo
request_id: dash-0006
sequence: 3
acknowledgement: superseded
effect: superseded by dash-0007
```

### States

`CommandAcknowledgement` has four values: `accepted`, `completed`,
`superseded`, `not fulfilled` (note the space) (`control.py:39-45`). The
last three are terminal: every later transition updates only rows still
`accepted` (`control.py:801-803`, `:872-874`, `:1749-1760`).

| Command | At submission | Later transitions |
|---|---|---|
| `stop`, no active attempt | `completed` ("intake stopped" / "intake already stopped") | none |
| `stop`, active attempt | `accepted`, intake `stopping` | `completed` "intake stopped after active attempt" at attempt finalization (`control.py:1500-1540`); `completed` "intake stopped after handoff attempt" if a handoff owned the wait (`:823-840`); `superseded` by `resume` (`:438-452`) |
| `stop --after N` | `accepted` (intake `stopping` only if N == 1 with an active attempt) | `completed` "stopped after N attempts; last counted attempt ..." when the count reaches 0 (`control.py:1431-1459`); `superseded` by `stop`, `resume`, `handoff now` or a newer `stop --after` |
| `resume` | `completed` immediately (`control.py:409-418`, `:435-465`) | none |
| `handoff now` | `accepted`, intake `stopping` | `completed` after publication, release and cleanup (`control.py:782-850`); `not fulfilled` with reason (`:854-921`); `superseded` by `stop`, `resume` or `stop --after` only until the model begins the handoff skill |
| `next issue <n>` | `accepted` | `completed` "claimed issue #n as the prioritized next issue" (`control.py:969-1015`); `not fulfilled` "next issue #n not fulfilled: <reason>" when eligibility is lost at claim time (`:1019-1065`); `superseded` by a newer `next issue` (`:621-638`) |

`not fulfilled` is reachable only for `handoff now`, `next issue` and the
out-of-scope recovery commands. A `stop` or `stop --after` never ends
`not fulfilled`.

The handoff also has a finer **phase** in `control.handoff.phase`:
`accepted` → `delivering` → `model_work` (sets `begun: true`) → `completed`
or `not_fulfilled` (underscore) (`control.py:724`, `:748-751`, `:772-775`,
`:818-822`, `:889-893`). Deadlines: 240 s from acceptance for model work and
360 s absolute, recomputed from `accepted_at` and never renewed by a restart
(`control.py:130-139`, `:1966-1988`).

### Retention

- Command rows are never deleted: `control.py` has no `DELETE`. They live in
  `$DATA_DIR/state/control.sqlite3` on the `/data` volume and survive
  container replacement (`tests/test_per_instance_control.py:228`). So
  `agentctl command <id>` answers for the lifetime of the volume.
- The `control.commands` map in `status` and telemetry holds only the **last
  10** records by sequence (`control.py:1311-1325`, `:2112`). Older IDs are
  still readable through `command <id>`.
- `control.handoff` keeps the last tracked handoff after it ends: phase
  `completed` or `not_fulfilled`. It is cleared only when a not-yet-begun
  handoff is superseded (`control.py:298-300`, `:321-323`, `:425-428`).
  Treat a non-null `handoff` as "in progress" only while its
  `acknowledgement` is `accepted`.

## 4. Latency and timeouts

- **Every command replies on acceptance.** The socket handler commits one
  SQLite transaction and replies with that record (`control_server.py:585-626`).
  `handoff now` replies `accepted` immediately. Its delivery, model work and
  publication happen later in the lifecycle and show up through
  `command <id>` or telemetry. The out-of-scope `recovery retry` and
  `recovery release` are the exceptions: they execute before replying
  (`lifecycle.py:1627-1638`).
- **Client timeouts.**
  - Mutating commands, `command` and `status`: 10 s for connect and read
    (`send_request` default, `control_server.py:629`).
  - `telemetry`: 3 s (`telemetry.py:66`, `agentctl.py:150-152`).
  - Server per-connection socket timeout: 10 s (`control_server.py:226`).
  - The dashboard's current `exec_timeout` default is 5 s
    (`src/agent_dashboard/gateway.py:1043`). That is shorter than agentctl's
    own 10 s, so control execs need their own longer timeout.
- **What can make acceptance slow.**
  1. Every submit takes the lifecycle's control lock with no timeout
     (`lifecycle.py:1415`, `:1433`, `:1457`, `:1501`, `:1525`). The intake
     cycle holds that lock across the GitHub claim (`_gated_claim`,
     `lifecycle.py:4235` through `claim_next()` at `:4310`, and the
     prioritized fetch at `:4382`), by design: "either a stop commits first
     and no claim begins, or a claim begins first" (`lifecycle.py:1334-1339`).
  2. `next issue` fetches the issue from GitHub, plus a comment read for
     continuations, under the lock before committing (`lifecycle.py:1540-1580`).
  3. The listener is **single-threaded**: connections are handled one at a
     time (`control_server.py:197-222`, backlog 16). A slow control op also
     delays the dashboard's telemetry poll behind it. With a 3 s telemetry
     timeout, that poll can come back `agent_unreachable`. (Telemetry itself
     waits at most 1 s for the control lock and otherwise serves the cached
     control section, `lifecycle.py:3600-3632`, `telemetry.py:62`, but only
     once the listener reaches it.)
- **Ambiguous loss.** If the 10 s client timeout fires, the server still
  finishes and commits; its late reply is dropped silently
  (`control_server.py:706-710`). The CLI then prints "not submitted" with exit
  2, but the command **may have committed**. The documented recovery is to
  retry with the same `--request-id` (`agentctl.py:236-244`,
  `docs/agents/operator-onboarding.md:525-530`). The retry returns the stored
  record if it committed, or applies it once if it did not.

## 5. Older images and `recovering`

### Version skew

`agentctl` ships in the same image as the agent, so the CLI and the server
always share a version, and an unknown op never reaches the agent from the
bundled CLI. When the agent answers `Unknown operation`
(`control_server.py:273-281`), the reply is `ok: false`; for a mutating op
the CLI would print it as `rejected`, exit 1.

When each op first appeared, by first commit touching `control_server.py`:

| Op | First release |
|---|---|
| `command` | v0.2.0 |
| `stop_after` | v0.2.0 |
| `next_issue` | v0.2.0 |
| `handoff` | v0.2.0 |
| `telemetry` | v0.5.0 |

Every instance that serves telemetry therefore supports all five commands
with their current argv. Future flags, such as a `--json` or
expected-repository flag, will hit argparse on older images: exit 2, usage on
stderr, empty stdout. That is the same skew rule the telemetry contract
already states (`docs/agents/telemetry-snapshot.md:150-154`).

### While `recovering` (startup reconciliation)

The socket starts before the lifecycle loop (`__main__.py:182-191`), so
commands are served during reconciliation. `control.intake` reads
`"recovering"` and `control.recovering` is `true` (`control.py:2093-2094`;
test `tests/test_lifecycle_control.py:200-228`).

- **`resume`**: rejected, exit 1, "Resume is blocked ... startup
  reconciliation is still running ..." (`lifecycle.py:1775-1795`,
  `control.py:394-396`; test `tests/test_lifecycle_control.py:406`).
- **`stop`, `stop --after`, `next issue`**: accepted normally. The lifecycle
  checks only whether a checkpoint exists, and a reconciling attempt has one
  (`lifecycle.py:1416`, `:1434-1439`).
- **`handoff now`**: accepted if a checkpoint exists. This part is an
  inference, not exercised by a test: reconciliation finishes the attempt
  "without recreating its SDK execution context" (`lifecycle.py:5178-5179`),
  and a handoff completes only if the model began the handoff skill
  (`lifecycle.py:4781-4795`). So a handoff accepted during recovery, or
  against a held (non-running) retained attempt, should end `not fulfilled`.
  The agent does not reject it up front.
- **Other holds.** Outside `recovering`, `resume` is also rejected by:
  - a retained checkpoint not being processed;
  - an unreadable checkpoint or completion ledger;
  - a running recovery command;
  - unexplained dirty work with no checkpoint.

  (`lifecycle.py:1457-1484`, `:1796-1840`)

## 6. Snapshot `control` fields per command

`control` is built by the same `build_status` as `agentctl status`
(`control.py:2054-2113`, `lifecycle.py:3634-3674`), plus
`control_observed_at`. It is computed without network or git I/O, so a few
fields differ from `status` (see gaps).

| Command | Precondition visible in `control` | Effect visible in `control` |
|---|---|---|
| `stop` | Always accepted. It completes immediately iff `active_attempt == null` | `intake` → `stopped` (idle) or `stopping`; `pending_command` = the stop; `commands[<id>]` |
| `stop --after N` | Rejected iff `intake == "stopped"` or (`handoff.begun` and `handoff.acknowledgement == "accepted"`) (`control.py:526-538`) | `stop_plan` = {`request_id`, `requested`, `remaining`, `includes_active_attempt`, `active_attempt_id`, `latest_counted_attempt_id`, `latest_counted_outcome`} (`control.py:1579-1607`); `pending_command`; `intake` → `stopping` iff N == 1 with an active attempt |
| `resume` | Rejected iff `recovering` or a recovery hold (`recovery != null` covers most cases, see gaps). It has an effect iff `intake != "running"`, `pending_command != null`, `stop_plan != null`, or an accepted handoff that has not begun; otherwise it completes as "intake already running" | `intake` → `running`; `pending_command` → null; `stop_plan` → null; superseded IDs in `commands`. With a begun handoff it completes but changes nothing until the handoff ends (`control.py:397-418`) |
| `handoff now` | Requires `active_attempt != null`; rejected iff `handoff.begun` with `acknowledgement == "accepted"` (`control.py:689-701`). Sensible only if `recovery == null` and not `recovering` (inference above) | `intake` → `stopping`; `pending_command` = the handoff; `handoff` = {`request_id`, `accepted_at`, `begun`, `phase`, `acknowledgement`, `detail`, `model_deadline_at`, `publication_deadline_at`} (`control.py:925-954`); `stop_plan` cleared |
| `next issue <n>` | No intake gate: accepted even while `stopped` or `stopping`, and waits for the next permitted claim. Eligibility (open, unassigned, `ready-for-agent`, non-empty body, no open blocker, or the continuation rule) is checked against GitHub **only at submission** (`lifecycle.py:1507-1580`) | `pending_next_issue` = {`request_id`, `issue_number`} (`control.py:956-967`); a superseded earlier priority shows in `commands` |

The tracked attempt is `control.active_attempt`: {`issue_number`, `branch`,
`phase`, `started_at`, `issue_title`}. Its `started_at` is the stable attempt
ID that `stop_plan.active_attempt_id` refers to (`lifecycle.py:1439`).

## 7. `TARGET_REPO` in replies and in the snapshot

- **Successful submission and `command <id>`**: the first stdout line is
  `repository: <TARGET_REPO>` (`agentctl.py:212-219`; tests
  `tests/test_per_instance_control.py:133-164`).
- **Rejections**: the socket reply carries `repository`
  (`control_server.py:604-614`; test `tests/test_per_instance_control.py:180-196`),
  but the CLI prints only `<id> rejected — <error>` (`agentctl.py:245-251`).
  The repository is **not shown**.
- **Unreachable or "no such command"**: no repository, since no agent
  answered or the CLI drops it (`agentctl.py:236-244`, `:603-605`).
- **Snapshot**: `identity.repository` comes from
  `TelemetryReporter(repository=config.target_repo)` (`__main__.py:117-118`,
  `telemetry.py:717-735`). `control.repository` comes from the lifecycle's
  `repository=config.target_repo` (`__main__.py:171`,
  `lifecycle.py:1394-1398`, `:3660`). They are the same string, the
  validated `owner/name` (`config.py:29`, `:202-203`), and
  `identity.repository` is one of the contract's stable fields
  (`docs/agents/telemetry-snapshot.md:116-117`).
- **No pre-commit guard.** No option makes the agent check an expected
  repository before committing. The repository in a successful reply can
  only be compared **after** the command has been applied.

## Gaps: what the dashboard cannot learn today

These feed [#98](https://github.com/lbacik/agent-dashboard/issues/98)
(agent-side changes) and [#95](https://github.com/lbacik/agent-dashboard/issues/95)
(target binding).

1. **No machine-readable reply** for mutating commands or `command <id>`.
   The dashboard would have to parse the `key: value` block, and the rejection
   reason is free English text with no code. Candidate: `--json`, printing the
   socket reply (including `kind`, `created_at`, `updated_at` and a rejection
   code) to stdout. [#98]
2. **No expected-repository guard.** A command is committed before the
   dashboard can see which repository answered. Candidate:
   `--expect-repo <owner/name>` that rejects with no change on mismatch.
   [#95, #98]
3. **Rejections don't show the repository** on the CLI, although the socket
   reply has it. [#95, #98]
4. **Exit-code overloading.** 2 = unreachable/ambiguous, argparse error, and
   (for `command`/`status`) "agent answered `ok: false`". 1 = precondition
   rejection or storage failure. [#98]
5. **Ambiguous timeout reads "not submitted".** On exit 2 the command may
   still commit. The dashboard has to treat exit 2 as unknown and resolve it
   by retrying with the same ID or via `command <id>`. [spec]
6. **Single-threaded listener and lock waits.** A slow `next issue` (GitHub
   fetch) or a claim in progress can delay acceptance up to the 10 s client
   timeout and make concurrent telemetry polls time out. [spec; possibly #98]
7. **`stop --after` local rejection omits the request ID** in its stderr
   line (`agentctl.py:544`). [#98, cosmetic]
8. **Telemetry `recovery` is network-free**, so a dirty-tree-only hold
   (no checkpoint, unexplained dirt) does not appear there, yet `resume` is
   rejected for it (`lifecycle.py:1829-1840` vs `:3676-3731`, `:3753-3755`).
   The dashboard cannot predict that rejection. [#98 or accept]
9. **`handoff now` is accepted against a held or reconciling attempt** that
   cannot deliver it, and presumably ends `not fulfilled`. The dashboard has
   to gate on `recovery == null` and `!recovering` itself. [spec; #98 could
   reject up front]
10. **`next issue` eligibility cannot be pre-checked** from the snapshot. The
    only signal is the rejection at submission. [accept]
11. **`commands` holds only the last 10** in the snapshot, and nothing marks
    who issued a command. A dashboard-chosen ID prefix (`dash-...`) and a
    `command <id>` fallback cover this without agent changes. [spec]
12. **Rejections are not recorded.** A rejected command never enters the
    store (`control.py:52-93`), so only the dashboard's own audit trail can
    show it. [spec]

## Doc drift

`docs/agents/operator-onboarding.md` disagrees with the code in two places:

- "The CLI prints a unique request ID before submission" (`:526-527`). The
  code prints the ID only in the final reply or error, not before sending
  (`agentctl.py:222-253`).
- "The first line of every response names the `TARGET_REPO`" (`:520-522`).
  That holds for successful replies and `status`, not for rejections,
  unreachable errors or "no such command".

The `handoff now` rejection message also repeats "No control change was
accepted" twice (the store text plus the CLI suffix).

## Bearing on the map

- **Progress to the UI** (map "Not yet specified"). The telemetry `control`
  section is enough to track progress for every command while it is among
  the last 10: `commands[<id>].acknowledgement` and `detail`,
  `pending_command`, `stop_plan`, `handoff.phase` with deadlines, and
  `pending_next_issue`. `agentctl command <id>` is needed only to resolve an
  ambiguous exit 2 (or to retry with the same ID) and for IDs that have
  dropped out of the last-10 window.
- **Gateway parameters** (map "Not yet specified"):
  - Timeout: above agentctl's 10 s socket timeout plus `docker exec`
    overhead, so not the 5 s telemetry `exec_timeout`.
  - Concurrency: at most one control exec per instance, since the agent
    serializes anyway.
  - Retry: on exit 2 or a dashboard-side timeout, retry with the same
    `--request-id`. On exit 2 with a `usage:` prefix on stderr, do not retry.
