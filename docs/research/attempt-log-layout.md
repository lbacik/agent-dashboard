# Attempt log layout and file access: verification against the agent

Answers lbacik/agent-dashboard#17. Research only; no code. Read-only throughout
(`docker exec ls/stat/find/python` reads, `docker cp <c>:<path> -` reads; no
container or volume was modified).

## Verdict

The layout in `debug-agent/references/log-layout.md` is still correct in shape
(`/data/logs/<issue>/<started_at>/`, numbered evidence files, `agent_output.json`,
`attempt.json`, command logs). It is **incomplete and wrong in three places that
matter to a reader**, and `docker cp ... -` does **not** stream raw bytes. See
[Corrections](#corrections-to-debug-agent) and [File access](#file-access).

Sources (all primary):

- Upstream `simple-coding-agent` at `9c75f94` (main, 2026-10-17):
  `observability.py`, `attempt_state.py`, `model_execution.py`, plus
  `lifecycle.py` and `finalization.py` for the writers of `attempt.json` and
  `completed_attempts.json`.
- `agent-forge` `4c1a847`, `.agents/skills/debug-agent/references/log-layout.md`
  (last changed `bf355a7`, 2026-10-11).
- Live data: 11 `simple-coding-agent:*` containers on this host (1 running,
  `agent-paysubscriptions-agent-1`; 10 stopped). Full statistics come from the
  running one (59 attempts, 7,685 evidence files); the 10 stopped ones were
  checked for file-name shape only. Instances span agent 0.5.0 and earlier.
  The host path `~/OrbStack/docker/volumes/*_agent_data` showed only 2 of the
  11 volumes, one of them empty while its container holds 57 attempts, so it is
  not a usable route. All data below came through Docker.

## Layout (verified)

```
/data/state/attempt.json            only while an attempt is active
/data/state/completed_attempts.json index of finalized attempts (not in debug-agent)
/data/state/consecutive_errors.json
/data/logs/<issue>/<started_at>/    issue = digits; started_at = UTC, ':' -> '-'
    attempt.json
    agent_output.json               JSON lines
    setup_stdout.log setup_stderr.log
    baseline_check_stdout.log baseline_check_stderr.log   absent on some runs
    check_stdout.log check_stderr.log                     only if final check ran
    NNNN_tool_call.json NNNN_tool_result.json NNNN_model_response.txt
    transcripts/                    not in debug-agent; see below
```

- Directory name: `AttemptArchive.__init__` does `started_at.replace(":", "-")`
  (`observability.py`). All 59 live attempt directories match
  `\d{4}-\d\d-\d\dT\d\d-\d\d-\d\d\.\d+Z`; all 42 issue directories are digits.
  Lexicographic order equals chronological order.
- File name set seen across all 11 containers, excluding numbered files and
  `transcripts/`: exactly `agent_output.json`, `attempt.json`,
  `setup_{stdout,stderr}.log`, `baseline_check_{stdout,stderr}.log`,
  `check_{stdout,stderr}.log`. Nothing unknown.
- `NNNN` is one counter per attempt (`self._event_sequence`, `:04d`, widening
  past 9999 is possible but not observed; max seen 394). It is shared by
  `tool_call`, `tool_result`, `model_response`. Written by `_archive_evidence`.
- Command logs are written by `archive.write_text(f"{name}_stdout.log"...)`
  (`lifecycle.py:5293`), with `name` in `setup`, `baseline_check`, `check`.

## `attempt.json` (attempt directory)

Not "written at the end": `write_attempt` **merges** into the file several
times (`observability.py:107`, `lifecycle.py:434,630,652,655,707,1772,4422,5027`).
A partial file (model fields, `final_check_started` without `..._finished`) can
exist mid-attempt or after a kill. Only the last write adds `outcome`. A
completed record is one with non-empty string `outcome`, `started_at`,
`completed_at` (this is exactly upstream's own `_read_completed_record`).

Keys and how many of the 59 live files carry them:

| key | n | notes |
|---|---|---|
| `outcome`, `started_at`, `completed_at`, `duration_seconds` | 59 | outcomes seen: complete 40, incomplete 7, infrastructure_error 6, handoff 5, no_changes 1 |
| `model_usage` (map: model -> cost/token counters), `model_stop_reason`, `skill_events` | 51 | absent when the model never ran; `skill_events[]` = `{agent_id,name,phase,timestamp}` |
| `final_check_started`, `final_check_finished` | 41 | ISO timestamps; start without finish = process died in the check |
| `agent_version` | 31 | `setdefault` on write; older attempts lack it |
| `token_budget` | 26 | large nested object (tokens, cost, turns, soft threshold, prompt cache) |
| `token_estimate_authority` | 28 | |
| `issue_number`, `issue_title` | 5 | only on recent attempts; do not rely on them, the issue is in the path |
| `pull_request` | 5 | `{number, url}`; written at publication |

Also possible per source, none seen live: `deferred_baseline_failed`
(`{reason}`), `final_check_dirtied_workspace`
(`{paths, reason, preservation_commit}`). Size: 2.2 KB typical, JSON with
sorted keys. The reader must treat every key except `outcome` as optional.

## `state/attempt.json` (active checkpoint)

Fields (`attempt_state.py`): `issue_number`, `branch`, `phase`, `started_at`,
`updated_at` required; `auto_merge_opt_in`, `requeue_source` (`human` |
`automatic`), `issue_title`, `continuation_index` optional. `phase` is one of
`claimed, setup, model_running, pushing, publishing`. Written atomically
(temp file plus `os.replace`), so a reader never sees a torn file. `started_at`
with `:` is the attempt id and equals the log directory name with `-`.
Validation is strict upstream: any other key makes the agent reject the file.

## `state/completed_attempts.json`

Not documented in debug-agent. `AttemptCompletionStore` (`finalization.py`).
One JSON object keyed by attempt id (`started_at` with `:`):

```json
{"2026-09-27T16:33:43.385090Z": {"branch": "agent/issue-98", "completed_at": "...", "issue_number": 98, "outcome": "infrastructure_error"}}
```

Written only after outcome, publication, issue release and cleanup are all
durable. Coverage: 107 of 107 (one container), 6 of 6, but 24 of 30 in the
oldest one, so it is missing attempts from before the store existed.
Atomic writes. Useful as a cheap attempt index for a **stopped** container
(see below).

## `agent_output.json` events

JSON lines, sorted keys: `timestamp, level, event, issue_number, phase, detail`
(`JsonEventLogger.emit`), redacted. Only records with an `issue_number` are
mirrored; levels seen INFO 12,561, WARNING 1,531, ERROR 276. Size p50 51 KB,
max 929 KB. No malformed lines in 14,368 events.

Event names seen live (count): `tool_call` 3868, `tool_result` 3813,
`limits_checked` 3556, `workspace_hold` 752, `attempt_finalization_held` 752,
`model_response` 404, `consecutive_error_limit_reached` 266 (so it is **not**
stdout-only, contrary to debug-agent), `workspace_prepared_for_profile_read`,
`profile_loaded`, `workspace_prepared`, `attempt_phase_transitioned`,
`setup_started` 59 each, `skill_call`/`skill_result` 54, `setup_succeeded` 56,
`model_dispatch_starting`/`model_execution_started`/`model_usage_shape`/
`model_execution_finished` 51, `baseline_check_succeeded` 50, `issue_claimed`
46, `final_check_started` 41, `final_check_succeeded` 39, `token_budget_reconciled`
26, `transcripts_copied` 26, `token_estimate_degraded` 21,
`baseline_check_failed` 6, `completion_decided` 5, `token_soft_threshold_*` 4
each (`crossed`, `subagents_stopped`, `handoff_context_injected`),
`background_result_deferred` 4, `setup_failed` 3, `prompt_cache_ineffective` 3,
`final_check_failed` 2, `rebase_resolution_*` 2, `setup_deferred_unresolved_conflicts`
2, `background_tasks_settled` 2, `cost_soft_threshold_*` 1. The set is open
(upstream emits many more); the reader must render unknown events generically.
Rendering ordering is by file order, not by `timestamp`.

### How `tool_call` / `tool_result` / `model_response` name their files

Source: `model_execution.py` `_log_tool_use`, `_tool_call_detail`,
`_tool_result_detail`, `_model_response_detail`. `detail` strings:

| event | detail | regex (matched 100% of 7,681 live tool events) |
|---|---|---|
| `tool_call` | `<Tool>[ [main]\| [subagent:<id>]] -> NNNN_tool_call.json` | `^(\S+?)(?: \[(main\|subagent:[^\]]+)\])? -> (\d{4}_tool_call\.json)$` |
| `tool_result` | same with `_tool_result.json`, plus optional ` (ok\|error\|interrupted)` | same, `(?: \((\w+)\))?$` |
| `model_response` | `-> NNNN_model_response.txt (chars=N)`; no tool name, no thread | `^-> (\d{4}_model_response\.txt) \(chars=(\d+)\)$` |

- The thread tag (`[main]`, `[subagent:<id>]`) exists only on newer attempts;
  1,365 of 3,868 live `tool_call` events (older attempts) have none.
- The ` (ok|error|interrupted)` hint is emitted only when the response is a map
  with `interrupted: true` or a boolean `is_error`. Live: **0 of 3,813** results
  carried a hint, so it cannot drive an ok/error badge. Read the result file.
- Tool names seen: Bash 3878, Read 2077, Edit 1239, Write 232, Agent 170,
  TaskOutput 67, ListAgents 8, WebFetch 4, Monitor 4, SendMessage 2 (MCP-style
  names are possible).
- `Skill` tool calls are different: events `skill_call`/`skill_result`, detail
  `skill:<name> [thread]: <repr>` **inline**, truncated at 2,000 chars with
  `...[truncated]`; no numbered file. `_MAX_LOG_DETAIL = 2000`.
- If the archive is unavailable the detail falls back to inline
  `<Tool> [thread]: <repr>` (no file reference). Treat "no `->`" as inline.
- A call and its result are not adjacent with parallel tools. Pairing key is
  not stored; the counter only orders. (The result file for subagent tools
  carries `agent_id`, below.)

### Evidence file contents

- `NNNN_tool_call.json`: indented JSON (`indent=2`, sorted keys) of the tool
  **input**, now wrapped by `_with_agent_id`: `{"agent_id": null|"<id>", ...input}`;
  a non-object input becomes `{"agent_id":..., "payload": <value>}`.
  The tool **name is still not in the file**; only the event gives it.
- `NNNN_tool_result.json`: the same envelope around the tool response. Shapes
  seen: Bash `{interrupted,isImage,noOutputExpected,stderr,stdout}`, Read
  `{file,type}`, Edit `{filePath,newString,oldString,originalFile,replaceAll,structuredPatch}`,
  Agent `{agentId,canReadOutputFile,description,isAsync,outputFile,...}`.
  Shape varies per tool: the reader must show it as pretty JSON, not parse
  fields.
- `agent_id` envelope present in 2,503 of 3,868 call files and 2,463 of 3,813
  result files; absent before attempt `2026-09-27T17-24-41Z`. Both forms parse
  as JSON objects.
- `NNNN_model_response.txt`: raw assistant text, UTF-8, not JSON.
- All redacted at write time (`Redactor`, literal replacement with
  `[REDACTED]`); `agent_output.json` records redacted twice.

### Size distribution (live, 59 attempts)

| file | n | min | p50 | p90 | p99 | max |
|---|---|---|---|---|---|---|
| `tool_call.json` | 3,868 | 2 B | 251 B | 1.3 KB | 7.2 KB | 31.8 KB |
| `tool_result.json` | 3,813 | 91 B | 2.8 KB | 15.7 KB | 34.6 KB | 81.4 KB |
| `model_response.txt` | 404 | 20 B | 96 B | 2.5 KB | 4.9 KB | 11.4 KB |
| `agent_output.json` | 59 | | 51 KB | | | 929 KB |
| `transcripts/**.jsonl` | 67 | | | | | 1.7 MB |

Files per attempt: min 4, median 144, max 400. Whole `/data/logs` of the
running container: 81 MB. Implication: any single numbered file is safe to
load whole (cap at 1 MB as defence); `agent_output.json` is fine in memory;
**never** list or fetch the tree eagerly.

## `transcripts/`

Not in debug-agent. `copy_cli_transcripts` (`observability.py`) copies the
Claude CLI session into `transcripts/<session_id>.jsonl` plus
`transcripts/<session_id>/subagents/agent-<id>.jsonl`, redacted line by line.
Best-effort: events `transcripts_copied` (INFO, `files=N; bytes=N`) or
`transcripts_copy_failed` (WARNING). Present in 26 of 59 live attempts
(the rest predate it or failed). Up to 1.7 MB per file. It is outside the
ADR 0003 allow-list; reading it would need a decision.

## Corrections to debug-agent

1. "`NNNN_tool_call.json`: only the tool's input": wrong for current agents; an
   `agent_id` key is added (null on the main thread). Tool name remains only in
   the event, so the CONTEXT.md "Evidence file" definition still holds.
2. `attempt.json` is "written at the end": it is merged progressively; only
   `outcome` marks completion. Keys `agent_version`, `token_budget`,
   `pull_request`, `final_check_*`, `issue_*` are missing from its example.
3. `consecutive_error_limit_reached` "stdout only": it appears in 266
   `agent_output.json` events live (it carries an issue number when one is
   claimed). Likewise `workspace_hold` and `attempt_finalization_held`, which
   it does not list at all.
4. Missing from the tree: `transcripts/`, `state/completed_attempts.json`.
5. The thread tag in the `tool_call` event detail is undocumented.
6. State `attempt.json` example lacks `auto_merge_opt_in`, `requeue_source`,
   `issue_title`, `continuation_index`.

## File access

All run against `b513d59f6a16` (running) and `371d0a172108` (stopped).

### `docker cp <c>:<path> -`

- **Output is a tar stream, not file bytes**, even for one file. A single file
  yields one entry named by the **basename** (`attempt.json`), with a header
  carrying size and mtime. Read it with `tar -xOf -` (verified; the first bytes
  are the tar header `0002_tool_call.json\0...`). Python: `tarfile.open(
  fileobj=stdout, mode="r|")`.
- Works identically on running and stopped containers. No `-u` applies (the
  daemon reads the filesystem); the user is not an access control.
- Missing path: exit status 1, stderr `Error response from daemon: Could not
  find the file <path> in container <id>`, empty stdout. Same for both states.
  Map this to "layout not recognised".
- **`..` is resolved by the daemon**:
  `docker cp "<c>:/data/logs/../state/consecutive_errors.json" -` returned the
  state file. The allow-list in ADR 0003 (digits, timestamp name, fixed file
  names, `NNNN_(tool_call|tool_result|model_response).(json|txt)`) is therefore
  load-bearing and is the only barrier. No symlinks exist under `/data/logs`.
- A directory path streams its **entire subtree** (all of `/data/logs`: 6.5 MB
  in 0.7 s for a small stopped container, 81 MB for the paysubscriptions one,
  107 attempts in another). There is no depth limit or listing mode.
- Latency about 0.07 s of CPU, 0.7 s wall per call.

### Listing, running container (`docker exec -u agent <id> ...`)

`uid=1000(agent)`; GNU coreutils 9.7 (`ls`, `stat`, `find`, `du`) and `python`
are present. `ls -1 /data/logs/231` returns the timestamp directory names, one
per line. `stat -c '%n|%F|%s|%Y' <path>` and `find . -maxdepth 2 -printf
'%p|%y|%s|%T@\n'` both work. Facts:

- Directory mtime is bumped by every file created in it, so an attempt
  directory's mtime = last evidence write (live activity), and `attempt.json`
  mtime = completion. `agent_output.json` mtime ≈ last event.
- `ls /data/logs | grep -v '^[0-9]*$'` and a pattern check on the 59 run dirs
  returned nothing odd. Names are safe to treat as validated identifiers only
  after an explicit regex check; do not trust the shell.
- `docker exec` on a **stopped** container fails: `container <id> is not
  running`. So no `ls`/`stat` for stopped instances.

### Listing, stopped container

No cheap native listing. Options verified:

- `docker cp <c>:/data/state/completed_attempts.json -` gives issue, branch,
  outcome and completion time for every finalized attempt, cheaply, then
  `docker cp .../attempt.json -` per attempt for detail. Misses attempts that
  predate the store and any attempt that did not finalize.
- `docker cp <c>:/data/logs - | tar -tf -` works but transfers everything
  (see above). Acceptable only for small volumes; ADR 0003 already defers
  stopped instances to after v1.

## Consequences for the dashboard (facts, not design)

- A viewer needs three reads: the event stream (`agent_output.json`), the
  evidence files by name taken from event `detail`, and `attempt.json`
  (optional keys only).
- The event `detail` is the only place that links a numbered file to a tool
  name, thread and (rarely) outcome. Parse it with the regexes above and fall
  back to "no file" for inline details.
- `docker cp` output must be untarred; validation must happen before the argv
  is built because the daemon resolves `..`.
- ADR 0003's v1 scope (running only, `docker exec ... ls/stat`) is sound: those
  tools exist and work; stopped containers need a different read path and
  should stay out of v1.
