# Instance control: spec

Destination of map "Instance control actions from the dashboard" (issue 93). Ready to be sliced into implementation issues. Planning only: no feature code lives on this map.

Vocabulary follows `CONTEXT.md`: **Control action**, **Control audit**, **Reach**, **Poll outcome**. The agent's own word for what it records is a *command*; its contract terms live in [simple-coding-agent's CONTEXT.md](https://github.com/lbacik/simple-coding-agent/blob/main/CONTEXT.md).

Inputs, all resolved on the map:

- Reply format, exit codes, request-ID semantics and the snapshot's `control` fields: [`docs/research/agentctl-command-replies.md`](https://github.com/lbacik/agent-dashboard/blob/research/agentctl-command-replies/docs/research/agentctl-command-replies.md) (branch `research/agentctl-command-replies`, simple-coding-agent `9820906`, v0.7.2), ticket "Verify agentctl command replies and request-ID semantics for the dashboard".
- Availability rules, target binding, version gating, input validation: ticket "Decide the command set's availability rules and target binding", ADR 0006.
- Request protection, confirmation, Control audit: ticket "Decide request protection, confirmations and the audit trail", ADR 0007.
- Look and behaviour: variant C of ticket "Prototype control actions in the drawer and fleet table" (throwaway code `prototype/instance_control/` on branch `prototype/instance-control`; reuse the copy and the rules, not the code).
- Agent-side changes and the reply contract rules: ticket "Decide the agent-side changes for dashboard control".
- Constraints: ADR 0001, 0002, 0006, 0007. `gateway.py` is the only module that builds Docker argv and every argv it can produce is pinned by a test. Templates consume framework-free view models only.

Settled while writing this spec (ticket "Write the instance control spec"): the gateway parameters in section 3 (control timeout, one control exec per instance, telemetry polls skipped during it, operator-driven retry only), one "Outcome unknown" state for every ambiguous result, the `agentctl command <id>` fallback behind a button rather than automatic, and `AGENT_DASHBOARD_CONTROL` defaulting to on.

## 1. Scope

In this effort:

- Five Control actions on a live managed instance, each issuing exactly one `agentctl` command via `docker exec`: `stop`, `stop --after N`, `resume`, `handoff now`, `next issue <n>`.
- A Control console in the instance drawer: composer, two-step confirmation, feedback cards that follow an accepted command through telemetry, and a timeline of the last 10 commands.
- Command-state chips in the fleet table's Intake cell (no action controls there).
- Request protection, the control-off setting and the Control audit.
- One read-only helper exec, `agentctl command <id>`, behind a "Check status" button.

Out of scope (see the map): `recovery retry`, `recovery release` and `errors reset` (CLI-only), container lifecycle actions (`docker start/stop/restart`), LAN access with authentication, multiple users, remote Docker contexts and multiple hosts. No change waits on an agent release (section 12).

## 2. Settings and dashboard-wide availability

Two new settings, CLI flag plus `AGENT_DASHBOARD_*` env with the existing precedence (flag > env > default):

| Setting | Flag / env | Default | Rule |
|---|---|---|---|
| `control` | `--control on\|off` / `AGENT_DASHBOARD_CONTROL` | `on` | Accepts `on`/`off` and the existing truthy/falsy spellings (`1`, `true`, `yes` / `0`, `false`, `no`); any other value is a startup error. `off` is read-only mode. |
| `control_exec_timeout` | `--control-exec-timeout` / `AGENT_DASHBOARD_CONTROL_EXEC_TIMEOUT` | `15.0` s | Must be greater than 10 s (agentctl's own socket timeout for mutating ops and `command`); a smaller value is a startup error, because it would turn every slow acceptance into "Outcome unknown". |

At startup the app computes one **dashboard control mode**: available, or unavailable with the first matching reason, in this order:

| Condition | Reason shown after "Controls unavailable:" |
|---|---|
| `control` is off | `control is off (AGENT_DASHBOARD_CONTROL=off, read-only mode)` |
| bound to a non-loopback address (`--allow-non-loopback`, ADR 0002) | `the dashboard is bound to a non-loopback address (<host>)` |
| `AGENT_DASHBOARD_DISABLE_HOST_GUARD=1` | `the Host guard is disabled (AGENT_DASHBOARD_DISABLE_HOST_GUARD=1)` |

When the mode is unavailable, every drawer shows only that line in the console and every Control action route answers 403 (section 6). The ADR 0002 test deployment therefore shows "Controls unavailable" and never runs a control exec.

## 3. Gateway operations

New functions and methods in `gateway.py` (and on `tests/fake_gateway.py`). Each builds its argv inside the gateway from validated values, runs without a shell, addresses the container by ID, calls `agentctl` by absolute path (`AGENTCTL_PATH`), and runs as `--user agent` like `telemetry`.

### 3.1 Argv

`argv_control(container_id, action, *, request_id, n=None, issue=None, expect_repo=None)` builds `docker exec --user agent <container id> /usr/local/bin/agentctl` followed by:

| `action` | Tail |
|---|---|
| `stop` | `stop --request-id=<id>` |
| `stop_after` | `stop --after <N> --request-id=<id>` |
| `resume` | `resume --request-id=<id>` |
| `handoff` | `handoff now --request-id=<id>` |
| `next_issue` | `next issue <n> --request-id=<id>` |

When `expect_repo` is given, `--expect-repo=<owner/name>` is appended as the last element. Each of `--request-id=<id>` and `--expect-repo=<value>` is a single argv element. The action keys are the agent's command kinds (`stop`, `stop_after`, `resume`, `handoff`, `next_issue`), so the same key names a Control action, a `control.commands` record and a Control audit entry.

`argv_command_status(container_id, request_id)` builds `docker exec --user agent <container id> /usr/local/bin/agentctl command <id>`.

Every combination is pinned by a test: each action with and without `--expect-repo`, and `command <id>`.

### 3.2 Validation

New `validate_*` functions next to `validate_container_id`, used by the route layer first and again inside the `argv_*` functions (the gateway does not trust its caller). `re.fullmatch` only; a failure raises `ValueError` and never reaches argv.

| Value | Rule |
|---|---|
| `action` | one of the five keys above |
| `N` (`stop_after`) | `[1-9][0-9]?`, so 1–99 |
| issue number (`next_issue`) | `[1-9][0-9]{0,9}`, then at most 2147483647 |
| request ID | `dash-[0-9a-f]{16}` (the only IDs the dashboard sends) |
| `expect_repo` | `[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}` |
| container ID | existing `validate_container_id` |

Tests cover, per value: empty, leading zero, sign, whitespace, a newline, a leading `-`, out of range (0, 100, 2147483648), non-ASCII digits, and an extra `=`/`--` in the request ID and repository.

### 3.3 Running a control exec

`DockerGateway.control(container_id, action, *, request_id, n=None, issue=None, expect_repo=None) -> ControlExec` and `DockerGateway.command_status(container_id, request_id) -> ControlExec`. `ControlExec` holds `stdout`, `stderr` (both bytes) and `returncode`. As for `telemetry`, exit codes are reported, not raised. They raise only:

- `DockerNotAvailableError` when the Docker CLI cannot start (nothing was sent);
- `ControlTimeoutError` (a new `GatewayError`) when `control_exec_timeout` fires, after killing and reaping the client process. Killing the `docker exec` client does not stop `agentctl` inside the container, which may still commit, so a timeout means "outcome unknown";
- `ControlBusyError` (a new `GatewayError`) when another control exec for the same container ID is in flight (section 3.4).

### 3.4 One control exec per instance, and telemetry polls

The agent's control socket listener is single-threaded, and a submit can wait on the control lock that the intake cycle holds across the GitHub claim. Until agent change 1 (section 12) ships, a telemetry poll that lands behind a waiting submit fails, and the instance's Reach would flicker to stale right after the operator presses Confirm.

- The gateway keeps a per-container-ID lock for `control` and `command_status`. Acquisition is non-blocking: a second call for the same container while one is in flight raises `ControlBusyError` at once. There is no queue; the route answers 409 (section 6). Different instances never block each other, and control execs do not count against `max_concurrent_execs`.
- `DockerGateway.control_in_flight(container_id) -> bool` reports the lock.
- The collector skips the telemetry exec for a running instance whose container has a control exec in flight. It checks right before the exec, after taking the poll semaphore. The skipped instance gets no `InstancePoll` this cycle, so `FleetState.apply_cycle` keeps its previous Poll outcome and snapshot. The skip is not counted as a failure. The next cycle after the exec ends polls normally.
- Retry is always operator-driven ("Retry with the same request ID" in the card, section 8.4). The dashboard never retries a control exec on its own.

## 4. Reply parsing

A new pure module (`agentctl_reply.py`), testable without Docker, turns one `ControlExec` into a `ControlReply` with a `kind` and the parsed fields. It follows the reply contract rules from ticket "Decide the agent-side changes for dashboard control" and tolerates today's agents, which predate that contract.

**Parsing rules**

- stdout on exit 0: one `key: value` per line (`^([a-z_]+):[ \t]?(.*)$`). The first occurrence of a key wins. Lines that do not match are ignored, and so are unknown keys (they stay in a raw mapping for tests, never rendered). Key order is not relied on.
- Keys used: `repository`, `request_id`, `sequence` (an integer, else none), `acknowledgement`, `effect`. Each may be missing; a missing value renders as an em dash.
- stderr on exit 1/2: an optional first line `repository: <owner/name>`, taken when present; the rest, stripped and capped at 2,000 characters, is the reason. The reason is display text only: nothing branches on it.

**Kinds**

| Exit / condition | `kind` | Meaning |
|---|---|---|
| 0 | `accepted` | The agent committed the command; `acknowledgement` is `accepted` or `completed`. |
| 1 | `rejected` | No control change. This includes a request ID reused with another payload, a storage failure, and a `docker exec` daemon error such as a container removed after the binding check (also exit 1 and also no change). |
| 2 with a stderr line starting `usage:` | `usage_error` | argparse refused the argv, so nothing was sent. This is a dashboard bug or version skew, never retried. |
| 2 otherwise | `unknown` | Unreachable, timed out or lost connection. The command may have committed. |
| 125, 126, 127 | `failed` | Docker could not run `agentctl` in the container, so nothing was sent. |
| any other code | `unknown` | — |
| `ControlTimeoutError` | `unknown` | The dashboard timeout fired. |
| `DockerNotAvailableError` | `failed` | — |

**`agentctl command <id>`** uses the same parser. Exit 0 yields the record. Exit 1 is `not_recorded`: the command was never committed, so retrying with the same request ID is safe. Exit 2 (unreachable, or an agent `ok: false`) is `unknown`, shown as "Could not check".

**Repository on the reply.** On exit 0, compare `repository:` with the repository the form carried, case-insensitively. A difference is a **repository mismatch** (ADR 0006): the command was applied, nothing is undone, and the card and the Control audit say so. A missing `repository:` line on exit 0 is no mismatch: the mismatch flag is none and the card notes "the reply did not name a repository". On exit 1, a present `repository:` line that differs is recorded in the Control audit; the card still says Rejected.

**Fixtures.** Until agent change 3 publishes the reply contract, parser tests use synthetic examples modelled on the research's observed output (section 1 of the research), stored under `tests/fixtures/agentctl/`. Once change 3 ships, its examples are copied to `tests/fixtures/contract/v1/agentctl/`, with their source commit recorded in `tests/fixtures/contract/v1/SOURCE.txt`, and the tests move to them.

## 5. Availability and target binding

Availability is computed by a pure view-model function from a `FleetRow` (Reach plus the latest good snapshot's `control` section and identity) and the dashboard control mode. Templates receive only its result.

### 5.1 Controls unavailable for an instance

The console shows the single line "Controls unavailable: <reason>" instead of the composer for the first matching reason:

1. the dashboard control mode is unavailable (section 2);
2. Reach is not `live`: `the snapshot is stale (newer polls failed)`, `the agent is unreachable`, `the container is not running`, `this agent's telemetry is not supported`;
3. the snapshot's `control` section is `null` or missing (`the snapshot has no control section`), or is not a JSON object (`the snapshot's control section is unreadable`).

### 5.2 Per-action rules (Reach `live`)

An action is **enabled** only when the agent would accept it **and** it would have an effect. Otherwise it is shown **disabled with its reason**. Reasons are checked in table order; the first one that holds is shown.

| Control action | Disabled when (reason shown) | Note on an enabled action |
|---|---|---|
| Stop | `intake == "stopped"` (already stopped); `pending_command.kind == "stop"` (a stop is pending) | — |
| Stop after N | `intake == "stopped"` (already stopped); `handoff.begun` and `handoff.acknowledgement == "accepted"` (a handoff is in progress) | with a `stop_plan`: "replaces the current plan (<remaining> left)" |
| Resume | `recovering` (the instance is recovering); `recovery != null` (recovery hold: resolve it with agentctl recovery); nothing to resume, i.e. `intake == "running"` and no `pending_command`, no `stop_plan` and no accepted not-begun handoff (already running) | — |
| Handoff now | `active_attempt == null` (no active attempt); `recovering` (the instance is recovering); `recovery != null` (recovery hold); `handoff.acknowledgement == "accepted"` (a handoff is in progress) | — |
| Next issue | never | with `pending_next_issue`: "currently #<n> is next" |

- **A missing field leaves the action enabled** (an older agent; tolerant reader). The agent is the authority, and its rejection is harmless and shown with its reason. A dirty-tree-only recovery hold is absent from telemetry, so Resume can be enabled and still rejected; this gap is accepted.
- **Control state behind.** When the snapshot's control section is stale (`SnapshotDetail.control_stale`: `control_observed_at` older than one poll interval while the snapshot is fresh), the rules still apply, and the console adds a note above the composer: "The agent's control state is <age> old; it may be busy (for example claiming an issue), so acceptance can take up to 10 s."
- The review route re-checks these rules (section 6.3). The submit route does not: between review and submit only the agent decides.

### 5.3 Target binding at submission (ADR 0006)

The confirmation form carries the container ID and the `identity.repository` it was rendered for. The submit route then checks, in order, that:

1. the fleet still lists `{name}` with the same container ID (a recreated container gets a new ID);
2. the row's Reach is `live`, recomputed server-side;
3. the latest good snapshot's `identity.repository` equals the form's repository.

Any failure gives 409 with "Instance replaced or unavailable: refresh the view. Nothing was sent." and a `refused` Control audit entry, and no `docker exec` runs. There is no snapshot-age token.

### 5.4 Version gating

A minimum-version table maps agent-side flags to the first agent version that supports them, compared with `identity.agent_version`:

| Feature | Minimum agent version |
|---|---|
| `--expect-repo` | pending: the simple-coding-agent release that ships change 4 (section 12). Until it is filled in, the flag is never passed. |

- The version is parsed from a leading `v?<major>.<minor>.<patch>`. An unreadable or missing version counts as old.
- When the agent supports the guard and the repository passes `validate_*`, the dashboard passes `--expect-repo=<identity.repository>`. Otherwise it omits the flag, and the dashboard-side check above still applies. The Control audit records which happened.
- Every agent that serves telemetry (v0.5.0+) supports all five commands with today's argv; older agents see the same UI.

## 6. Routes and request protection

### 6.1 Routes

`{name}` is the managed instance name, resolved against the in-memory fleet and never used in argv. Unknown name: 404 fragment "instance not found". Form bodies are `application/x-www-form-urlencoded` and are parsed with the standard library (`urllib.parse.parse_qs`), so no new dependency (`python-multipart`) is added.

| Method | Route | Returns | Protection |
|---|---|---|---|
| GET | `/instances/{name}/control` | Console body: the "Controls unavailable" line, or the composer. Used by Cancel, by the composer's filtered `sse:fleet` refresh and out of band after a submit. | Host guard |
| GET | `/instances/{name}/control/commands` | The timeline list, plus its collapsed summary out of band. | Host guard |
| POST | `/instances/{name}/control/review` | Step 2: the confirmation, with a newly generated request ID. | Section 6.2 |
| POST | `/instances/{name}/control/submit` | The Control action: one feedback card. | Section 6.2 |
| POST | `/instances/{name}/control/status` | `agentctl command <id>` for one card: the re-rendered card. | Section 6.2 |
| GET | `/instances/{name}/control/track` | One card, re-read from the latest good snapshot (no Docker call). | Host guard |

Form fields: `action`, `n`, `issue` (review, submit, status); `container_id`, `repository` (submit, status); `request_id` (submit, status); `retry=1` when a card's Retry sent it. `track` takes the same values as query parameters, plus `sequence` from the accepted reply.

### 6.2 Request protection (ADR 0007)

Every POST control route runs these checks, on top of the Host allow-list, before anything else:

1. `Origin` is present and equals `http://` plus the request's `Host` header. The comparison is with the Host header, not with the configured port, so `ssh -L 9000:localhost:8000` works; the host is compared case-insensitively. A missing or different `Origin` is refused.
2. `Sec-Fetch-Site`, when present, is `same-origin`.
3. `HX-Request` is `true`.
4. The dashboard control mode is available.

A failure answers 403 with a fragment: "Request refused: <reason>", or "Controls unavailable: <reason>" for check 4. It writes a `refused` Control audit entry and runs no `docker exec`. There is no synchronizer token.

### 6.3 Order of checks per route

**review**: request protection → validation of `action`, `n`, `issue` (422: the composer re-rendered with the message and the entered values) → the instance's controls available (403, section 5.1) → the action enabled (409: the composer re-rendered with the action's disabled reason) → 200: the confirmation with `request_id = "dash-" + secrets.token_hex(8)`. Each refusal writes a `refused` entry. A 200 writes nothing; it is not a submission.

**submit**: request protection → validation of every field, including the request ID format (422, card "Refused") → target binding (409, section 5.3) → the per-instance busy check (409: "Another command is being sent to this instance. Wait for its result, then try again. Nothing was sent.") → the `submitted` Control audit entry (on write failure, 503 card "Not sent", section 7) → `gateway.control` → reply parsing → the outcome entry → 200 with the feedback card, plus the composer and the timeline summary out of band.

The busy check is the gateway's `ControlBusyError`, so it is decided atomically when the exec starts. A `ControlBusyError` raised after the `submitted` entry was written is recorded as the outcome `refused` with reason `busy`.

**status**: request protection → validation → `gateway.command_status` (409 when busy) → 200 with the re-rendered card. It writes no Control audit entry: it changes nothing.

### 6.4 htmx error swaps

htmx 2 does not swap 4xx/5xx responses. A small static script opts in only the control routes' 403, 409, 422 and 503 responses (`htmx:beforeSwap`: `shouldSwap = true`, `isError = false` when the request path matches `/instances/*/control/*`), because each of them carries a fragment to show. Other routes keep the default.

## 7. Control audit

Append-only JSONL at `$XDG_STATE_HOME/agent-dashboard/control-audit.jsonl`; when `XDG_STATE_HOME` is unset, `~/.local/state/agent-dashboard/control-audit.jsonl` (the XDG default). The directory is created on first write with mode `0700`, the file with `0600`. Every entry is also written to stdout as one line, `control-audit <json>`, for journald or `docker logs`. There is no rotation; README says where the file is.

**Entry fields** (UTC timestamps, `null` where not known):

| Field | Content |
|---|---|
| `at` | ISO 8601 UTC with milliseconds |
| `outcome` | `submitted`, `refused`, `accepted`, `rejected`, `unknown`, `usage_error`, `failed` |
| `request_id` | The request ID; `null` for a refusal before one exists (review, or a malformed submit) |
| `instance`, `container_id` | From the route and the form. `container_id` is the form's value, even when the binding check refused it. |
| `expected_repository` | The repository the form carried |
| `action`, `params` | The action key and `{"n": 3}` / `{"issue": 42}` / `{}` |
| `command` | The human command, e.g. `stop --after 3` |
| `agent_version` | `identity.agent_version` of the latest good snapshot |
| `expect_repo_guard` | Whether `--expect-repo` was passed |
| `retry` | `true` when sent from a card's Retry |
| `exit_code` | The `agentctl`/`docker exec` exit code; `null` on a dashboard timeout or when Docker did not start |
| `sequence`, `acknowledgement` | From an exit 0 reply |
| `reply_repository`, `repository_mismatch` | From the reply. The mismatch flag is `null` when the reply named no repository. |
| `reason` | For `refused`: one of `origin`, `fetch_site`, `htmx_header`, `controls_unavailable`, `validation`, `action_unavailable`, `stale_binding`, `busy` |
| `error` | Human text: the refusal message, the agent's rejection reason, or the timeout message |
| `duration_ms` | Exec duration, on outcome entries |

There is no client address: requests always come from loopback.

**Write order** (ADR 0007):

- A submission writes `submitted` before `docker exec` and one outcome entry after it. If the `submitted` write fails (open, write, flush or `fsync` raises), the command is not sent: 503, card "Not sent", "The Control audit could not be written, so the command was not sent. Check the dashboard's state directory (<path>)."
- A failed outcome write does not change the result shown. It logs an error to stderr, and the card adds "The Control audit outcome entry could not be written."
- A refusal is one `refused` entry. If writing it fails, the refusal is still returned, and the failure is logged to stderr.
- One process-wide lock serialises writes. Each entry is one `write` of one line, then `flush` and `os.fsync`.
- A `submitted` entry without an outcome (for example after a crash during the exec) is resolved with `agentctl command <id>`.

**Relation to the agent's record.** The agent's `control.sqlite3` is the authority for an accepted command's later lifecycle (`completed`, `superseded`, `not fulfilled`). The Control audit records only the submission and its outcome at submission time. The two are joined by request ID. The UI never reads the Control audit.

## 8. UI

Variant C of the prototype, with the collapsible timeline. Follow `DESIGN.md`: colour tokens only, no status by colour alone, light theme.

### 8.1 Placement

- **Drawer.** A "Control console" section directly under the drawer head, above the stats and the current attempt. Its outer element has a stable ID per instance (`control-console-<slug of the instance name>`) and `hx-preserve`, so the drawer's `sse:fleet` refresh keeps it, including feedback cards and an open confirmation. Opening another instance's drawer brings that instance's console.
- **Fleet table.** No action controls. The Intake cell gets command-state chips derived from the latest good snapshot's `control`, in this order:
  - `handoff · <phase>` when `handoff.acknowledgement == "accepted"`;
  - `stop pending` when `pending_command.kind == "stop"`;
  - `stops after <remaining> more` when there is a `stop_plan`;
  - `next #<n>` when there is a `pending_next_issue`.

  The chips take the Intake cell's stale styling when the control state is stale. Chips are text, not colour alone.

### 8.2 Step 1: composer

- One radio group with the five Control actions in this order: Stop, Stop after N, Resume, Handoff now, Next issue.
  - A disabled action stays visible, greyed and struck through, with its reason under the label.
  - An enabled action may carry its note (section 5.2).
  - The first enabled action is preselected. When none is enabled, "Review…" is disabled.
- A parameter field appears only for the chosen action: "Attempts before stopping" (`n`, default 1) or "Issue number" (`issue`, empty, placeholder "e.g. 123"). Numeric input mode; the server validates.
- A live preview line `$ agentctl <command>` follows the choice and the typed value (small script; without it the line shows the preselected action).
- "Review…" posts to the review route. A 422 or 409 re-renders the composer with the message and the entered values.
- The control-stale note (section 5.2) sits above the composer when it applies.
- The composer refreshes on `sse:fleet` only while no confirmation is open and focus is not inside it: `hx-trigger="sse:fleet[!this.querySelector('.ctl-confirm') && !this.contains(document.activeElement)]"`. The server re-checks everything at submission anyway.

### 8.3 Step 2: confirmation

The confirmation replaces the composer and shows:

- "Confirm: `agentctl <command>` on **<instance>** (<repository>)";
- the exact argv line, rendered from the gateway's own `argv_control` for the values in the form (including `--expect-repo` when it will be passed), so the line is what runs;
- Confirm and Cancel. Confirm has `autofocus`; Cancel GETs the console body;
- "Request ID `<id>`".

Stop, Stop after N and Handoff now use the danger tone; Resume and Next issue use the primary tone. While the submit is in flight, both buttons are disabled and the console shows "Sending… waiting for the agent to accept" (up to the 10 s CLI timeout). The confirmation carries the action, its parameter, the container ID, the repository and the request ID as hidden fields. A double submit reuses the request ID, so the agent treats it as an identical retry.

### 8.4 Feedback cards

Each submit returns one card into a feedback slot above the composer, newest first. A card can be dismissed (×). After a submit, the composer comes back out of band. A card shows the command, its title, a body line, and meta: the request ID, `seq <n>` when known and "following…" while it is tracking.

| Card | When | Body | Actions |
|---|---|---|---|
| **Accepted** | `accepted` with acknowledgement `accepted` | The agent's `effect`, plus the progress line below | — (follows) |
| **Completed** | acknowledgement `completed` (from the reply or later) | `effect` or the record's `detail` | — |
| **Superseded** | later `superseded` | the record's `detail` | — |
| **Not fulfilled** | later `not fulfilled` (handoff, next issue) | the record's `detail` | — |
| **Rejected** | `rejected` | "Rejected: <reason>. No control change was accepted." | — |
| **Outcome unknown** | `unknown` | "No reply from the agent. The command may or may not have been applied." | Retry with the same request ID; Check status |
| **Repository mismatch** | exit 0, `repository:` differs | "The agent applied the command to <reply repository>, but this view showed <expected>. Nothing was undone." | — |
| **Refused** | 403 / 409 / 422 from submit | the refusal message | — |
| **Not sent** | 503 audit failure; `failed`; `usage_error` | the audit message (section 7); "Docker could not run agentctl: <stderr>"; "The agent's agentctl did not accept the dashboard's arguments; nothing was sent. This is a dashboard bug or a version mismatch." | — |

Tones: Accepted info, Completed ok, Superseded muted, Not fulfilled warn, every other card error. Each card has a text title, so its state does not rely on colour.

**Following an accepted card.** The card re-requests `track` on every `sse:fleet` event, until its acknowledgement is terminal (`completed`, `superseded`, `not fulfilled`). `track` reads only the latest good snapshot:

- When `control.commands[<request id>]` is there, the card takes its acknowledgement and `detail`. The progress line comes from the related section:
  - handoff: `phase <handoff.phase>` when `handoff.request_id` matches;
  - stop after: `<remaining> of <requested> attempt(s) left` from the matching `stop_plan`;
  - next issue: "waiting for the next permitted claim" while `pending_next_issue.request_id` matches.
- When the ID is absent and the card's `sequence` is lower than every sequence in `control.commands`, the command fell out of the last 10. The card stops following, says "No longer in the last 10 commands", and offers **Check status**.
- When the ID is absent otherwise, the card says "waiting for the next snapshot" and keeps following.
- When the row's container ID differs from the card's, or the instance is gone, the card stops following: "Instance replaced or unavailable; the agent's record of this command is on its /data volume."
- When Reach is not `live`, the card shows "following paused: <reach reason>" and keeps following.

**Check status** posts to the status route. It re-renders the card from the `agentctl command <id>` reply:

- exit 0: the card's state comes from the record's acknowledgement;
- `not_recorded`: "Not recorded: the command was not applied. Retry with the same request ID to send it.", with Retry;
- `unknown`: "Could not check: <reason>", with both buttons.

**Retry with the same request ID** posts to submit with the card's fields and `retry=1`, through every check again. The agent treats an identical retry as idempotent and returns the stored record. The resulting card replaces this one.

Cards for Rejected, Refused, Not sent and Outcome unknown exist only in the response, because the UI never reads the Control audit. They survive SSE swaps (`hx-preserve`) but not a page reload. Accepted commands come back through the timeline.

### 8.5 Command timeline

Under the composer, a `<details data-persist="control-timeline" open>` titled "Commands (last 10 from telemetry)". Its open state is one page-wide preference kept by the existing `data-persist` mechanism in `fleet.js`. The list shows `control.commands` from the latest good snapshot, newest first (by `sequence`). Each entry shows:

- `#<sequence>`, the kind (`stop after`, `next issue`, ...), and an origin chip: `dashboard` for `dash-` request IDs, `CLI` otherwise;
- the request ID and the time of `updated_at` (`HH:MM:SS`);
- a lifecycle stepper: submitted → accepted → completed / superseded / not fulfilled. A handoff adds delivering → model work, its current step taken from `handoff.phase` when `handoff.request_id` matches;
- the record's `detail`.

Collapsed, the summary still shows the count and the latest command with its current step. The list element inside the `<details>` refreshes from `/control/commands` on every `sse:fleet` event, and the summary updates out of band, so a refresh never opens or closes the `<details>`. An empty `commands` renders "No commands recorded."

### 8.6 Output safety

Reply text, rejection reasons, `effect` and `detail` are untrusted agent output. They are rendered through Jinja autoescape, never `|safe`. The argv line is rendered as text.

### 8.7 DESIGN.md

Add a rule next to "Live refresh never moves a scroll position the user set": an in-progress form inside a live-refreshed region gets a stable ID and `hx-preserve`. Its own parts refresh through a filtered `sse:fleet` trigger that holds while a confirmation is open or focus is inside it. Never re-render a form the operator is typing into.

## 9. Failure handling

| Situation | Behaviour |
|---|---|
| Unknown instance name | 404 fragment "instance not found" |
| Dashboard control mode unavailable | Console line; POST routes 403 and `refused` (`controls_unavailable`) |
| Origin / Sec-Fetch-Site / HX-Request check fails | 403 "Request refused: <reason>", `refused` entry, no exec |
| Invalid `action`, `N`, issue, request ID, container ID | 422; review re-renders the composer; submit shows a Refused card; `refused` (`validation`) |
| Instance controls unavailable at review | 403, console line re-rendered |
| Action disabled at review | 409, composer with the reason |
| Container replaced, Reach not live or repository changed at submit | 409 "Instance replaced or unavailable: refresh the view. Nothing was sent." |
| Another control exec in flight for the instance | 409 busy message, `refused` (`busy`) |
| Control audit `submitted` write fails | 503 Not sent card, no exec |
| Docker CLI missing | Not sent card, outcome `failed` |
| Exit 1 | Rejected card with the reason |
| Exit 2 `usage:` | Not sent card, outcome `usage_error` |
| Exit 2 otherwise, or dashboard timeout | Outcome unknown card with Retry and Check status |
| Exit 0 with a different repository | Repository mismatch card; nothing undone |
| Agent rejects an identical-ID retry with another payload | Rejected card (cannot happen from a card's Retry, which resends the same payload) |

No path returns a 500 or a stack trace.

## 10. Testing approach

- **Gateway**: argv pinned exactly for every action with and without `--expect-repo` and for `command <id>`; every validation rejection from section 3.2 raises before argv; the timeout kills and reaps and raises `ControlTimeoutError`; a second concurrent call for one container raises `ControlBusyError`, while one for another container does not; `control_in_flight` is true only during the exec.
- **Collector**: a running instance with a control exec in flight gets no telemetry exec, and its previous Poll outcome and snapshot are kept.
- **Reply parser**: fixtures for every row of the kinds table, including a missing `repository:` line, unknown keys, a non-integer `sequence`, a stderr `repository:` line on a rejection, a `usage:` exit 2, and `command <id>` exit 0/1/2. No input makes it raise.
- **Availability**: a table-driven test per row of section 5.2 built from the contract fixtures (`snapshot-recovery-hold`, `snapshot-soft-threshold-handoff`, `snapshot-attempt-in-progress`, `snapshot-idle-with-last-finished`) plus small synthetic variations; missing fields leave the action enabled; each reason of section 5.1; the version table (unreadable version counts as old).
- **Control audit**: entry schema per outcome, write order (`submitted` before the fake exec), fail closed when the `submitted` write raises (no exec happens), refusals recorded, stdout mirror, file and directory modes, `XDG_STATE_HOME` fallback. Tests use `tmp_path`.
- **Web** (TestClient and the fake gateway with scripted control replies): each row of section 9; request protection, including an `Origin` with another port, a missing `Origin`, `Sec-Fetch-Site: cross-site` and a missing `HX-Request`; each control mode; the confirmation shows the exact argv and a `dash-` ID; `track` for each following case; the drawer has the preserved console; the fleet chips. Assert HTML fragments; no browser tests.
- **Integration** (gated like `tests/test_integration_docker.py`): against a throwaway container with a stub `agentctl` that prints a scripted reply, one control exec and one `command <id>`, proving the argv runs as `--user agent` and the exit codes come back.

## 11. Slice candidates

Ordered by dependency. Each fits one implementation session.

1. **Gateway control operations.** Validators, `argv_control`, `argv_command_status`, `DockerGateway.control` / `command_status` / `control_in_flight`, the per-container lock, `ControlTimeoutError` / `ControlBusyError`, the `control_exec_timeout` setting, fake gateway support, the collector's poll skip.
   *Accepts when:* every argv is pinned; every validation case is rejected before argv; busy and timeout behave as in section 3; a control exec in flight skips that instance's poll and keeps its previous outcome.
2. **Reply parser and version table.** `agentctl_reply.py` with synthetic fixtures under `tests/fixtures/agentctl/`; the minimum-version table and version parsing.
   *Accepts when:* every row of the kinds table and every tolerance rule in section 4 is covered by a test; garbage input never raises.
3. **Control mode, request protection and the Control audit.** The `control` setting and the dashboard control mode, the request-protection dependency, the urlencoded form helper, the audit writer (path, modes, fsync, stdout mirror, lock) and the htmx swap opt-in script.
   *Accepts when:* each mode yields its reason; each protection check refuses with 403 and a `refused` entry; a failing `submitted` write is reported to the caller so the exec can be skipped.
4. **Availability view model and the read-only console.** The section 5 availability function, the fleet chips and the timeline items; the drawer's Control console with the "Controls unavailable" line, the composer (disabled with reason, notes, preview, control-stale note), the collapsible timeline with its refresh routes; `hx-preserve` and the filtered refresh; the DESIGN.md rule. Review is not wired yet.
   *Accepts when:* every row of section 5.2 renders its state; chips show in the Intake cell; an SSE refresh keeps the console and the `<details>` state.
5. **Review, submit and feedback cards.** The review, submit, status and track routes; target binding; the confirmation fragment with the exact argv; every card in section 8.4, including following, Check status and Retry; the audit integration.
   *Accepts when:* every row of section 9 is covered by a web test; an accepted card follows a command to its terminal state through scripted snapshots; a retry sends the same request ID.
6. **Docs and integration test.** README section (Control actions, the control-off setting, the timeout, the Control audit file location); the read-only wording in `README.md`, `AGENTS.md`, `CONTEXT.md` (intro) and `pyproject.toml` updated; the gated integration test.
   *Accepts when:* README states where the Control audit lives and how to switch control off; the integration test runs in the gated suite.

Slices 1, 2 and 3 are independent and can run in parallel; 4 depends on 3 (the dashboard control mode); 5 depends on 1–4; 6 comes last. Moving the parser tests to the agent's contract fixtures is a follow-up once agent change 3 ships, as is filling the `--expect-repo` version once change 4 ships.

## 12. simple-coding-agent changes

No agent change is a prerequisite: the dashboard works with today's argv and text output on every agent that serves telemetry (v0.5.0+), and picks each change up through the version table. Filed in simple-coding-agent as issues 314–317; 1 and 2 are independent and can run in parallel, 3 blocks 4.

1. **Serve telemetry and `command <id>` while a mutating command waits on the control lock** (research gap 6; [simple-coding-agent#314](https://github.com/lbacik/simple-coding-agent/issues/314)). Today the single-threaded listener blocks on a submit that waits, with no timeout, on the lock the intake cycle holds across the GitHub claim. A 3 s telemetry poll then fails. The issue states the behaviour; the mechanism (per-connection threads, a lock timeout, ...) is the agent's choice. High priority. Once it ships, the dashboard's poll skip (section 3.4) can stay; it costs one skipped cycle.
2. **Reject `handoff now` while `control.intake == "recovering"` or under a recovery hold** (gap 9; [simple-coding-agent#315](https://github.com/lbacik/simple-coding-agent/issues/315)): exit 1, no change, reason named. Today it is accepted and ends `not fulfilled`. A correctness fix for CLI operators too; the dashboard already gates it.
3. **Publish the agentctl command reply contract** (gaps 3 and 7, the operator-onboarding drift; [simple-coding-agent#316](https://github.com/lbacik/simple-coding-agent/issues/316)):
   - on a rejection and on "no such command", print `repository: <TARGET_REPO>` as the first stderr line when the socket reply carries it ("not submitted" has no reply, so no line);
   - name the request ID on every rejection path, including local `stop --after` validation;
   - fix `docs/agents/operator-onboarding.md` ("prints a unique request ID before submission", "first line of every response names the TARGET_REPO");
   - add a contract document next to `telemetry-snapshot.md` with synthetic examples pinned by tests, under these rules: stdout on exit 0 is `key: value` lines, values without newlines, key order not part of the contract, new keys additive and ignored by readers; stderr on exit 1/2 is an optional `repository:` line, then a display-only reason; exit 0 committed, 1 rejected with no change (or unknown ID for `command <id>`), 2 outcome unknown or a usage error recognised by `usage:`; no version number in the output, new flags and behaviours marked "since vX.Y".
4. **Add `--expect-repo=<owner/name>` to the five mutating commands** (gap 2; [simple-coding-agent#317](https://github.com/lbacik/simple-coding-agent/issues/317)), defence in depth per ADR 0006: checked before the idempotency lookup, case-insensitive, a mismatch is an ordinary rejection (exit 1, nothing stored, reason `expected repository <X>, this instance targets <Y>`), not part of the canonical payload, documented "since vX.Y". Lowest priority.

Ruled out: `--json` for mutating commands and `command <id>`, a distinct usage-error exit code, the dirty-tree-only hold in telemetry, an issuer field on `control.commands`.

## 13. Residual risks (accepted)

- A dashboard-side timeout or exit 2 leaves the outcome unknown until the operator checks or retries; the card says so and offers both.
- Killing `docker exec` on a timeout leaves `agentctl` running inside the container until its own 10 s timeout, so a quick second submit to that instance can be slow too.
- Until agent change 1 ships, the skipped poll makes that instance's snapshot one cycle older during a slow acceptance. Other instances are unaffected.
- A `docker exec` daemon error that exits 1 shows as Rejected with the daemon's text; nothing was applied in either case.
- Resume can be enabled under a dirty-tree-only hold and then rejected; the reason is shown.
- Feedback for refused and failed submissions is lost on a page reload; the Control audit keeps it.
- The Control audit grows without rotation, one or two lines per click.
