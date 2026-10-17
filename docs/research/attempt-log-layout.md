# Attempt log layout and Docker read paths (ticket #17, map #15)

Question: does the layout documented by the `debug-agent` skill still match the
current simple-coding-agent source, which exact facts does the attempt log reader
need, and do `docker cp` / `docker exec ls|stat` work on running and stopped
managed instances?

Vocabulary follows `CONTEXT.md`: Attempt log, Evidence file, Managed instance,
Implementation attempt.

## Sources and method

- Reference doc: `agent-forge/.agents/skills/debug-agent/references/log-layout.md`
  (cited as `DOC:line`).
- Agent source: `/Volumes/Sources/python/ai/simple-coding-agent`, `main` at
  `9c75f94` (2026-10-17), package dir `simple_coding_agent/` (cited as
  `src/<file>:line`). Installed agent versions seen in real logs: 0.3.1, 0.3.2,
  0.5.1.
- Real data (read-only): `simple-coding-agent-data/logs` (7 attempts), the OrbStack
  volume `agent-simple-rpg-engine_agent_data` (3 attempts), the `rpi5` cache under
  `~/.cache/simple-coding-agent-env/rpi5/*/logs`, 73 attempts in total with 3,550
  tool-call files; plus the running `agent-paysubscriptions-agent-1`
  (59 attempts across 44 issues) read through `docker exec`/`docker cp`.
- Docker: client 29.8.2, server 26.1.5, OrbStack. The throwaway container
  `research-attempt-log-tmp` (agent image, `--entrypoint sh`) was created, used and
  removed (`docker rm -f`; verified 0 left). No existing managed instance was
  written to or restarted.

## 1. Layout: confirmed vs changed against the debug-agent doc

Layout root is unchanged: `/data/logs/<issue>/<started_at with ':' -> '-'>/`
(`src/observability.py:103-104`, `DOC:36-37`). Run directory name is the
`started_at` with colons replaced, e.g. `2026-10-17T12-32-55.560873Z`.

Confirmed unchanged:

- Evidence file names `NNNN_tool_call.json`, `NNNN_tool_result.json`,
  `NNNN_model_response.txt`, one counter shared by all three kinds, four digits
  (`src/model_execution.py:1112-1125`).
- `agent_output.json` is JSON lines, mirror of stdout records that carry an issue
  number (`src/observability.py:64-94,130-137`).
- Command logs `setup_*`, `baseline_check_*`, `check_*` as `<name>_stdout.log` and
  `<name>_stderr.log` (`src/lifecycle.py:5283-5294`); `check_*` exist only if the
  final check ran (`src/lifecycle.py:645-669`).
- Skill calls are inline in the event detail, no numbered file
  (`src/model_execution.py:2756-2778`).
- Outcome values `complete|incomplete|infrastructure_error|no_changes|handoff`
  (`src/completion.py:15-22`).
- `state/attempt.json` (active checkpoint) and `state/consecutive_errors.json`
  still exist (`src/attempt_state.py:35-36`).

Discrepancies (doc is wrong or stale):

| # | Doc says | Source / data says |
|---|---|---|
| D1 | Tree lists no `transcripts/` | Every attempt that reached the model also has `transcripts/<session_id>.jsonl` and `transcripts/<session_id>/subagents/agent-*.jsonl`, copied and redacted (`src/observability.py:147-245`, `src/lifecycle.py:432,451`). Largest real files are here (up to 2.65 MB). Not an Evidence file; ADR 0003's allow-list excludes it, correctly. |
| D2 | `NNNN_tool_call.json` "only the tool's input" | Payload is wrapped: `{"agent_id": null\|"<id>", ...input keys}` for dict payloads, or `{"agent_id":..., "payload": <non-dict>}` (`src/model_execution.py:3312-3321`, used at `:2786,:2796`). `agent_id` present in 3,404 of 3,550 call files in the corpus; the 146 without it come from older agent versions. Same for results. |
| D3 | Event detail form `Bash -> 0003_tool_call.json` | Current form is `Bash [main] -> 0003_tool_call.json` or `Bash [subagent:<agent_id>] -> 0003_tool_call.json` (`src/model_execution.py:3294-3300,:2787-2790`). Old logs (123 of 3,527 call events) have no `[thread]` part. A parser must accept all three. |
| D4 | Typical order starts with `workspace_prepared` (or `workspace_reprepared` for a continuation) | `workspace_reprepared` does not exist in source or in any real log. Real start of the stream is `issue_claimed`, `workspace_prepared_for_profile_read`, `profile_loaded`, `workspace_prepared`, `attempt_phase_transitioned` (`src/lifecycle.py:3313,3335,3342,3357,3364`). Continuation is signalled in `model_dispatch_starting` detail (`continuation=True`). |
| D5 | Trouble events `cost_soft_threshold_crossed` / `cost_soft_threshold_handoff_context_injected` | Source emits only `token_soft_threshold_*` (`src/model_execution.py:2124,2681`, plus `token_soft_threshold_subagents_stopped`, `..._handoff_followup`, `..._handoff_note_committed`, `token_hard_ceiling_reached`). `cost_soft_threshold` survives only as a legacy handoff-reason string (`src/lifecycle.py:5469-5480`) and in old logs. |
| D6 | `model_execution_finished` statuses: four | Five: `succeeded`, `model_limit_reached`, `infrastructure_error`, `handoff_requested`, `operator_handoff_expired` (`src/model_execution.py:378-385`). |
| D7 | `attempt.json` "summary, written at the end"; sample has `token_estimate_authority` | It is a merged record written incrementally (`write_attempt` does read-update-write, `src/observability.py:107-118`): after the model run (`model_usage`, `skill_events`, `model_stop_reason`, `token_budget`; `src/lifecycle.py:434-441`), around the final check, at outcome (`src/lifecycle.py:5000-5027`), and again after publication (`pull_request`, `src/lifecycle.py:1772-1774`, may therefore arrive after `outcome`). `token_estimate_authority` does not exist in current source (grep: zero hits); only 28 older real files carry it. New keys: `agent_version`, `token_budget`, `issue_number`, `issue_title`, `final_check_started/finished`, `pull_request`. See section 2. |
| D8 | `state/` has `consecutive_errors.json`, `control.sqlite3`, `attempt.json` | Also `state/completed_attempts.json` (seen in `simple-coding-agent-data/state` and the live instance). Irrelevant to the reader. |
| D9 | Events table is a sample | Source has about 96 distinct event names (section 3); real corpus shows 53. The doc's list is not exhaustive and the reader must not treat unknown names as errors. |

Also worth knowing: `state/attempt.json` and `logs/.../attempt.json` share a file
name but have different schemas (checkpoint vs summary). The reader only
touches the latter.

## 2. `attempt.json` (attempt log summary) fields

Written with `json.dumps(..., sort_keys=True)`, single line, no indent
(`src/observability.py:247-249`). Redacted at write time. Merged across several
writes, so any subset can be present. Missing `outcome` means in progress or the
process died (`read_latest_completed_attempt` treats a record as completed only
if `outcome`, `started_at`, `completed_at` are non-empty strings,
`src/observability.py:290-303`).

| key | type | written by | notes |
|---|---|---|---|
| `agent_version` | string | `write_attempt`, `setdefault` (`observability.py:117`) | e.g. `0.5.1`, or `unknown` (`agent_version.py:13-19`); present in 42 of 71 files in the corpus |
| `started_at` | string (UTC, `...Z`) | outcome write | equals run directory name with `-` for `:` |
| `completed_at` | string | outcome write | |
| `duration_seconds` | number | outcome write | |
| `outcome` | string | outcome write | five values above |
| `issue_number` | int | outcome write, only if > 0 | absent in older files |
| `issue_title` | string | outcome write, only if non-empty | absent in older files |
| `model_stop_reason` | string or null | after model run | e.g. `end_turn` |
| `model_usage` | object keyed by model name | after model run | each value keys: `cacheCreationInputTokens`, `cacheReadInputTokens`, `canonicalModel`, `contextWindow`, `costBasis`, `costUSD`, `inputTokens`, `maxOutputTokens`, `outputTokens`, `provider`, `thinkingTokens`, `webSearchRequests` (0.5.1 sample) |
| `skill_events` | list of `{agent_id, name, phase, timestamp}` | after model run | `phase` is `PreToolUse` or `PostToolUse` |
| `token_budget` | object | after model run | 0.5.1 keys: `actual_by_category`, `actual_tokens`, `budget_tokens`, `effective_soft_threshold_tokens`, `elapsed_seconds`, `error_ratio`, `estimated_tokens`, `handoff_reserve_turns`, `hard_ceiling_reached`, `interrupted`, `max_budget_tokens`, `measured_by_category`, `mode`, `peak_main_context_tokens`, `prompt_cache`, `soft_threshold_crossed`, `soft_threshold_crossed_at_tokens`, `soft_threshold_percentage`, `soft_threshold_rule`, `soft_threshold_tokens`, `total_cost_usd`, `turns`, `unreported`. Older versions have fewer keys; present in 31 of 71 files in the corpus. Cost figures are per `model_usage[*].costUSD` and `token_budget.total_cost_usd`. |
| `final_check_started`, `final_check_finished` | string | `lifecycle.py:652,655` | start without finish means the process died during the check (`lifecycle.py:5033-5045`) |
| `final_check_dirtied_workspace` | `{paths, reason, preservation_commit}` | `lifecycle.py:707-719` | rare |
| `deferred_baseline_failed` | `{reason}` | `lifecycle.py:630` | rare |
| `pull_request` | `{number:int, url:string}` | after publication (`lifecycle.py:1772`, `:4422`, shape `:5110-5126`) | |
| `token_estimate_authority` | string | legacy only | not written by current source |

Reader rule: unknown keys are ignored; every key above except `started_at` may be
missing. In the corpus, 2 attempt directories had no `attempt.json` at all and 2
had one without `outcome`.

Active-attempt checkpoint (`/data/state/attempt.json`), for contrast only:
`issue_number, branch, phase (claimed|setup|model_running|pushing|publishing),
started_at, updated_at`, optional `auto_merge_opt_in, requeue_source, issue_title,
continuation_index` (`src/attempt_state.py:19-33,218-229`). Not needed by the
attempt log view.

## 3. `agent_output.json`: record shape and event names

Each line: `{"detail", "event", "issue_number", "level", "phase", "timestamp"}`
(keys sorted; `src/observability.py:73-83`). `level` is `INFO|WARNING|ERROR`
(corpus: 11,428 / 1,531 / 203). `timestamp` is UTC ISO with `Z`. `phase` is the
coarse process phase, not the checkpoint phase; seen: `polling`, `setup`,
`model_execution`, `publication`, `interruption`. `detail` is a free string
(truncated to 2000 chars plus `...[truncated]` for tool events,
`src/model_execution.py:501,3511-3514`). Only events with an issue number are
mirrored here; the directory is created on the first mirrored event
(`observability.py:133`). Largest file in the corpus: 929 KB.

Events seen in real logs, with frequency in the 73-attempt corpus (no per-event
detail keys exist; `detail` is `key=value; key=value` prose, except where noted):

- Evidence-pointing: `tool_call` 3527, `tool_result` 3454, `model_response` 487
  (section 4).
- Skills (inline payload): `skill_call` 38, `skill_result` 36. Detail
  `skill:<name> [main]: <repr of input|response>`.
- Progress: `limits_checked` 3015 (detail `thread=main; budget_tokens=..;
  measured_tokens=..; estimated_tokens=..; soft_threshold_tokens=..;
  max_budget_tokens=..; main_context_tokens=..; turns=..; max_turns=..;
  elapsed_seconds=..; timeout_seconds=..`; format of the pre-0.5 versions differed,
  e.g. cost-based), `model_usage_shape`, `token_budget_reconciled` (detail is a
  JSON string equal to the `token_budget` object), `token_estimate_degraded`,
  `prompt_cache_ineffective`, `context_compacted`.
- Lifecycle: `issue_claimed`, `workspace_prepared_for_profile_read`,
  `profile_loaded`, `workspace_prepared`, `attempt_phase_transitioned`
  (`phase=setup` etc.), `setup_started`, `setup_succeeded`, `setup_failed`,
  `baseline_check_succeeded`, `baseline_check_failed`, `model_dispatch_starting`,
  `model_execution_started` (detail carries the limits),
  `model_execution_finished` (`status=<status>; <explanation>`),
  `transcripts_copied` (`session_id=..; files=..; bytes=..`),
  `final_check_started|succeeded|failed`, `completion_decided`
  (`outcome=..; reasons=..`), `background_tasks_settled`,
  `background_result_deferred`, `code_review_nudge`.
- Handoff and thresholds: `token_soft_threshold_crossed`,
  `token_soft_threshold_subagents_stopped`,
  `token_soft_threshold_handoff_context_injected`,
  `token_soft_threshold_handoff_followup`,
  `token_soft_threshold_handoff_note_committed`, `token_hard_ceiling_reached`,
  `handoff_note_rejected`; legacy `cost_soft_threshold_crossed`,
  `cost_soft_threshold_handoff_context_injected`,
  `cost_soft_threshold_subagent_deferred`.
- Trouble: `attempt_exception` (ERROR, detail `ExceptionType: message`),
  `attempt_interrupted`, `attempt_finalization_held`, `workspace_hold` (738 each in
  one runaway instance, level WARNING), `consecutive_error_limit_reached` (177),
  `continuation_branch_missing`, `setup_deferred_unresolved_conflicts`,
  `rebase_conflict`, `rebase_resolution_started|succeeded|failed`,
  `auto_continuation_claimed`, `auto_merge_enabled`, `auto_merge_failed`.

Source emits further names not seen in the corpus (full list from
`grep` of `_log(`/`_event_log(` call sites, 96 names): e.g.
`operator_handoff_*`, `token_soft_threshold_handoff_*_reinjected|blocked`,
`final_check_dirtied_workspace`, `final_check_interrupted`,
`git_workspace_unrecoverable`, `working_tree_clean|dirty`, `intake_stopped`,
`pull_request_merged`, `publication_failed`, `deferred_baseline_failed`,
`transcripts_copy_failed`, `archive_pull_request_failed`. Some, such as
`provenance_verification_failed`, `polling_for_issue`, `no_eligible_issue_found`,
have no issue number and appear on stdout only. Because the set grows between
releases (D5 shows a rename), the reader must render unknown events generically.

Detail keys are not a stable contract; the only structured details are the
`tool_*`/`model_response` filename references and `token_budget_reconciled` JSON.

## 4. Evidence file naming and call/result pairing

Rule (`src/model_execution.py:1112-1125`, `:2781-2806`):

1. One integer counter `_event_sequence`, reset to 0 at the start of each model
   execution (`:1146`, so every Implementation attempt starts at `0001`).
2. Each archived payload increments it and is named
   `f"{n:04d}_{kind}.{ext}"` with kind/ext one of `tool_call`/`json`,
   `tool_result`/`json`, `model_response`/`txt`. Beyond 9999 the number simply
   gets five digits (format width is a minimum). Not seen in the corpus (max
   observed counter was below 1,000).
3. The same string is the reference in the event detail:
   - `tool_call`: `<Tool>[ [main]| [subagent:<id>]] -> <file>`
   - `tool_result`: same, plus optional ` (interrupted)` / ` (ok)` / ` (error)`
     suffix (`:2795-2800,:3524-3534`). The suffix was never observed in the
     corpus (0 of 3,454), so the reader must not rely on it.
   - `model_response`: `-> <file> (chars=<N>)` (`:2803-2808`); `N` counts
     characters of the text, not bytes.
4. If there is no archive (`self._archive is None`), no file is written and
   the detail embeds `repr` of the payload instead (`:2788-2789,:2798-2799`).
   The reader must tolerate a `tool_call` event whose detail has no `->`.
5. Tool names exist only in event details. File contents never carry them
   (`tool_call.json` = tool input plus `agent_id`; `tool_result.json` = response
   plus `agent_id`).

Tool names seen: Bash, Read, Edit, Write, Agent, TaskOutput, TaskStop, WebFetch
(MCP tools would appear as `mcp__...`). Typical payload keys: calls `command`,
`description`, `file_path`, `old_string`, `new_string`, `content`, `offset`,
`limit`, `prompt`, `subagent_type`; Bash results `stdout`, `stderr`,
`interrupted`, `isImage`, `noOutputExpected`; Edit results `structuredPatch`,
`originalFile` (whole original file contents, hence the large results), etc.

Pairing: there is no `tool_use_id` in either the file or the event detail. A
result belongs to a call only by order. Measured on 3,454 results in the corpus:

- Matching each `tool_result` event to the oldest unmatched `tool_call` event
  with the same tool name and same thread label (FIFO per tool and thread)
  succeeded for 3,454 of 3,454 results; 0 results without a call; 73 calls
  without a result (denied, interrupted or killed before the post hook). FIFO per
  (tool, thread) is a heuristic: it was never contradicted but cannot be proven,
  since parallel calls of the same tool in the same thread lose ordering
  information.
- Adjacent numbering (`call` at n-1, `result` at n) holds for only 2,752 of 3,454
  results (80%); the other 702 are parallel tool calls
  (`0005 call, 0006 call, 0007 result, 0008 result`) or interleaved with
  `model_response`/subagent traffic. The reader must not pair by `n-1`.
- Every evidence file on disk had a matching event, and every event reference
  had a file (0 and 0 exceptions); sequence numbers inside an attempt were
  always consecutive.
- Skill calls do not consume a number.

Recommendation for the view: render evidence from the event stream in event
order, show the file name for each event, and offer "pair" only as a best-effort
link by FIFO per (tool, thread).

## 5. Size distribution

Combined corpus of 73 attempts (bytes; p50 / p90 / p99 / max), plus the live
`agent-paysubscriptions` volume of 59 attempts as a cross-check.

| file kind | n (corpus) | p50 | p90 | p99 | max | n (paysub) | max (paysub) |
|---|---|---|---|---|---|---|---|
| `NNNN_tool_call.json` | 3,550 | 231 | 1,288 | 7,673 | 37,602 | 3,868 | 31,777 |
| `NNNN_tool_result.json` | 3,473 | 3,100 | 32,226 | 181,496 | 217,289 | 3,813 | 81,449 |
| `NNNN_model_response.txt` | 490 | 91 | 1,885 | 5,135 | 26,602 | 404 | 11,412 |
| `attempt.json` | 71 | 964 | 1,911 | 2,209 | 2,209 | 59 | 2,418 |
| `agent_output.json` | 67 | 38,633 | 92,261 | 889,429 | 889,429 | 59 | 928,703 |
| `setup_stdout.log` | 69 | 274 | 11,286 | 11,557 | 11,557 | 59 | 279 |
| `setup_stderr.log` | 69 | 491 | 1,217 | 18,415 | 18,415 | 59 | 18,415 |
| `baseline_check_stdout.log` | 63 | 4,783 | 30,187 | 12,810,993 | 12,810,993 | 56 | 164,745 |
| `baseline_check_stderr.log` | 63 | 226 | 15,797 | 24,679 | 24,679 | 56 | 28,637 |
| `check_stdout.log` | 24 | 2,891 | 7,416 | 24,332 | 24,332 | 41 | 25,912 |
| `check_stderr.log` | 24 | 226 | 15,545 | 15,797 | 15,797 | 41 | 15,797 |
| `transcripts/*` (not an Evidence file) | 65 | 259,841 | 1,564,375 | 2,650,718 | 2,650,718 | 67 | 1,703,295 |

Against the decided 256 KiB (262,144 B) preview cap:

- No numbered Evidence file exceeded 256 KiB in either set (largest 217,289 B, a
  tool result). 139 tool results exceeded 64 KiB in the corpus, none exceeded 1 MiB.
- Files that do exceed it: `baseline_check_stdout.log` once (12.8 MB), `agent_output.json`
  once (929 KB; 738 repeated `workspace_hold` events), and all large transcripts.
  The cap must therefore also apply to `agent_output.json` and command `*.log`.
- Because `agent_output.json` is the first thing to read, a head-cap truncates the
  newest events. Consider reading its tail, or accept "first 256 KiB" with a
  visible truncation notice.

## 6. Docker verification

Environment: Docker client 29.8.2 / server 26.1.5 (OrbStack), agent image
`simple-coding-agent:paysubscriptions` (Debian, GNU coreutils 9.7, user `agent`
uid 1000; `docker inspect` `Config.User` is `root` and the entrypoint drops to
`agent`, `docker exec -u agent` is therefore required as ADR 0003 and the doc
state). Running instance: `agent-paysubscriptions-agent-1` (read-only commands
only). Stopped instance: `agent-hiretheagent-agent-1` (`Exited (143)`, read
only). Throwaway container: `research-attempt-log-tmp` (agent image, files
created as `agent`, one run dir with `attempt.json`, `agent_output.json`,
`0001_model_response.txt`, a 300,000-byte `0003_tool_result.json`, and a
symlink `0002_tool_call.json -> /etc/passwd`).

### 6.1 `docker cp <c>:<file> -` output is a tar stream (key surprise)

The `-` destination does not emit the raw file. It emits a tar archive containing
one member named by the file's base name. Raw bytes need `tar -xOf -`.

```
$ docker cp agent-paysubscriptions-agent-1:/data/logs/233/2026-10-17T12-32-55.560873Z/attempt.json - | head -c 64 | od -c | head -3
0000000    a   t   t   e   m   p   t   .   j   s   o   n  \0  \0  \0  \0
...
$ docker cp <same> - | tar -tvf -
-rw-r--r--  0 1000   1000     2203 Oct 17 15:06 attempt.json
$ docker cp <same> - | tar -xOf - | head -c 80
{"agent_version": "0.5.1", "completed_at": "2026-10-17T13:06:46.281362Z", ...
```

Size accounting: a 300,000-byte file produced a 301,568-byte stream (512-byte
header + 300,032 data padded to 512 + 1,024 end-of-archive zero blocks). A 256 KiB
cap is best applied to the extracted member; capping the raw stream at
`512 + 262144 = 262656` bytes (as tested below) also works but makes `tar` print a
truncation warning on stderr.

### 6.2 Running container

```
$ docker exec -u agent agent-paysubscriptions-agent-1 ls -1 /data/logs/233/2026-10-17T12-32-55.560873Z | head -3
0001_model_response.txt
0002_tool_call.json
0003_tool_result.json

$ docker exec -u agent agent-paysubscriptions-agent-1 ls -l --time-style=+%Y-%m-%dT%H:%M:%S /data/logs/233/2026-10-17T12-32-55.560873Z | head -4
total 1460
-rw-r--r-- 1 agent agent    85 2026-10-17T12:38:20 0001_model_response.txt
-rw-r--r-- 1 agent agent   147 2026-10-17T12:38:21 0002_tool_call.json
-rw-r--r-- 1 agent agent   823 2026-10-17T12:38:21 0003_tool_result.json

$ docker exec -u agent <c> stat -c '%n|%F|%s|%Y' <dir>/attempt.json <dir>/agent_output.json
/data/logs/233/2026-10-17T12-32-55.560873Z/attempt.json|regular file|2203|1792242406
/data/logs/233/2026-10-17T12-32-55.560873Z/agent_output.json|regular file|84919|1792242395

$ docker exec -u agent <c> find /data/logs -mindepth 2 -maxdepth 2 -type d -printf '%P|%T@|%y\n' | sort | tail -2
98/2026-09-27T16-46-06.290293Z|1790528195.3659232300|d
99/2026-09-27T13-39-09.650405Z|1790517092.7206323120|d      (0.55 s for 59 attempts)

$ docker exec -u agent <c> sh -c "cd /data/logs/233 && stat -c '%n|%F|%s|%Y' *"
2026-10-17T12-32-55.560873Z|directory|12288|1792242395
```

Notes: a glob in a `docker exec` argument is expanded by the host shell, not in
the container (zsh failed with "no matches found" in the throwaway test), so use
`sh -c` or `find`. `find ... -printf` lists
all attempts of all issues in one exec call. Times are container UTC
(mtime shown in tar listing is host-local). `ls -1` of an issue directory lists run
dirs, which sort chronologically because names are ISO timestamps (not true across
issues, where the directory name order is numeric-as-string).

`docker cp` from a running container, single file:

```
$ docker cp research-attempt-log-tmp:/data/logs/7/2026-01-02T03-04-05.123456Z/attempt.json - | tar -xOf -
{"outcome":"complete"}
$ docker cp ...0003_tool_result.json - | wc -c            # file is 300000 bytes
  301568
$ docker cp ...0003_tool_result.json - | head -c 262656 | tar -xOf - | wc -c
  262254          # cap by truncating stream: tar warns on stderr, member is cut
$ docker cp <c>:<run dir>/nope.json -                     # missing file
Error response from daemon: Could not find the file /data/logs/233/.../nope.json in container agent-paysubscriptions-agent-1
exit=1
```

### 6.3 Stopped container

`docker cp` works unchanged on stopped containers, including files on the named
volume mounted at `/data`. `docker exec` does not.

```
$ docker stop -t 1 research-attempt-log-tmp ; docker ps -a --filter name=research-attempt-log-tmp --format '{{.Status}}'
Exited (137) Less than a second ago
$ docker exec -u agent research-attempt-log-tmp ls /data/logs/7
Error response from daemon: container 708acd6a7c73... is not running      (exit 1)
$ docker cp research-attempt-log-tmp:/data/logs/7/2026-01-02T03-04-05.123456Z/attempt.json - | tar -xOf -
{"outcome":"complete"}
$ docker cp ...0003_tool_result.json - | wc -c
  301568
```

Real stopped instance (`agent-hiretheagent-agent-1`):

```
$ docker exec -u agent agent-hiretheagent-agent-1 ls /data/logs
Error response from daemon: container 371d0a17... is not running           (exit 1)
$ docker cp agent-hiretheagent-agent-1:/data/logs/1/2026-10-05T17-19-57.756546Z/attempt.json - | tar -xOf - | head -c 120
{"agent_version": "0.3.2", "completed_at": "2026-10-05T17:22:04.903573Z", ...
$ docker cp agent-hiretheagent-agent-1:/data/logs - | tar -tf - | grep -c .
704            # 0.76 s; this streams every file in the tree
```

So for stopped containers the only listing is `docker cp <c>:/data/logs - | tar
-tvf -`, which transfers the whole tree (in the sample about 0.8 s, but with
transcripts it grows to tens of MB). Out of scope for v1 (running only), but this
is the cheapest path if v1.1 adds stopped instances without `docker run`.

### 6.4 Safety findings for the gateway

- `docker cp` resolves `..` inside the container. This succeeded and returned a
  file outside the attempt directory:
  `docker cp "agent-paysubscriptions-agent-1:/data/logs/233/2026-10-17T12-32-55.560873Z/../../../state/consecutive_errors.json" -`
  gave `-rw-------  0 1000 1000 112 Oct 17 15:06 consecutive_errors.json`
  (mode 600 is no barrier, the daemon reads as root). Hence ADR 0003's rule that
  only validated identifiers reach argv is load-bearing; there is no sandboxing in
  docker cp.
- Pointing `docker cp` at a directory streams the directory tree
  (`docker cp <c>:<run dir> -` returned all files). The gateway must always name a
  single allow-listed file.
- Symlinks: without `-L`, `docker cp` returns the link itself
  (`lrwxrwxrwx 0 1000 1000 0 ... 0002_tool_call.json -> /etc/passwd`, no content).
  With `-L` it follows it and returns `/etc/passwd`. Do not pass `-L`.
- Missing file: stderr `Error response from daemon: Could not find the file ...`,
  exit 1 (tar side then sees empty input; check the `docker cp` status, e.g.
  `set -o pipefail` or reading the exit code directly rather than the pipeline's).
- Mid-stream truncation by the reader (`| head -c N`) completes without error from
  the Docker side; nothing needed server-side.

## 7. Short list of surprises

1. `docker cp ... -` emits a tar stream, not file bytes (section 6.1).
2. `docker cp` follows `..` and will stream directories; `-L` follows symlinks
   (6.4). The identifier validation in ADR 0003 is the only guard.
3. Evidence files carry an `agent_id` envelope; they are not "just the tool input"
   (D2), and call/result have no shared id, only order (section 4). Adjacent
   numbering holds for just 80% of results.
4. Event detail format changed across versions (`Bash ->` vs `Bash [main] ->`),
   and `cost_soft_threshold_*` was renamed to `token_soft_threshold_*` (D3, D5).
5. `attempt.json` is a merged, incrementally written record and can gain
   `pull_request` after `outcome` is present (D7). Same file name as the
   active-attempt checkpoint in `/data/state/` with a different schema.
6. The cap matters for `agent_output.json` and command logs, not for numbered
   Evidence files (largest 217 KB); one baseline log was 12.8 MB.
7. Non-evidence `transcripts/` is the biggest content in every attempt log
   (up to 2.65 MB) and is absent from the doc.
8. One instance wrote 738 `workspace_hold` + 738 `attempt_finalization_held`
   warnings into a single `agent_output.json`; event counts can be huge.
9. `ls` of an issue directory lists finished and in-progress runs alike; a run
   has no `attempt.json` until the first `write_attempt` (after the model
   execution returns, `src/lifecycle.py:434`, or at the outcome write), so a
   missing `attempt.json` means running or crashed.
