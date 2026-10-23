# Attempt log view: spec

Destination of map "Attempt log view for a managed instance" (issue 15). Ready to be sliced into implementation issues. Planning only: no feature code lives on this map.

Vocabulary follows `CONTEXT.md`: **Attempt log**, **Evidence file**, Managed instance, Implementation attempt. Never "session" or "request/response".

Inputs, all resolved on the map:

- Shape and data access: ticket "Charting decisions: shape and data access of the attempt log view".
- Verified layout and Docker behaviour: [`docs/research/attempt-log-layout.md`](https://github.com/lbacik/agent-dashboard/blob/research/attempt-log-layout/docs/research/attempt-log-layout.md) (branch `research/attempt-log-layout`, local commit 5b2a896 supersedes the remote ec06145), plus its correction comment on ticket "Verify attempt log layout and file access against the agent".
- Look and behaviour: variant A of ticket "Prototype the attempt log timeline and evidence viewer" (throwaway code `prototype/attempt_log/app.py` on branch `prototype/attempt-log-view`; reuse the parsing ideas, not the code).
- Constraints: ADR 0001, 0002, 0003. The Attempt log view adds no mutating operations (Control actions live in `instance-control.md`); the gateway is the only module that builds Docker argv.

## 1. Scope

In v1:

- Per managed instance, a page listing its attempts, and a page per attempt showing the full ordered event stream.
- On-demand inline viewing of Evidence files (tool call, tool result, model response), with download.
- Running instances only. Static pages, manual refresh.

Not in v1 (see the map's Not yet specified and Out of scope): live tail, stopped instances, caching, search across attempts or instances, remote Docker contexts, any write or control action.

## 2. Routes

All routes are `GET`, HTML only, behind the existing Host guard. `{name}` is a managed instance name resolved against the in-memory fleet (never used in argv); `{issue}`, `{run}`, `{file}` are validated per section 4 before anything else happens.

| Route | Returns |
|---|---|
| `/instances/{name}/logs` | Full page: attempts list. `?page=N` |
| `/instances/{name}/logs/{issue}/{run}` | Full page: attempt header plus event timeline. Query: `level`, `event`, `q`, `page` |
| `/instances/{name}/logs/{issue}/{run}/files/{file}` | htmx fragment: preview of one file. Query: `raw=1` for the unformatted text |
| `/instances/{name}/logs/{issue}/{run}/files/{file}/download` | The whole file as an attachment |

Entry point: the instance drawer gets an "Attempt log" link to `/instances/{name}/logs`, shown for every instance but only active for running ones (a not-running instance links to the page, which says "Attempt logs are available for running instances only"). The existing drawer route `/instances/{name}` is unchanged; `logs` is a sub-path, no conflict.

Unlike the other handlers (which read only fleet state), these handlers call the gateway on demand. The container ID comes from the fleet row for `{name}`, never from the request. Unknown name: 404 page "instance not found". Row not running: the "running instances only" page (200).

Pages are full pages extending `base.html` with a breadcrumb `instances / {name} / logs / #{issue} / {run}`, no SSE subscription, and a "refresh" link that reloads the same URL.

## 3. Gateway operations

New read-only operations on `DockerGateway` (and on `tests/fake_gateway.py`). Each builds its argv inside the gateway from validated values, runs without a shell, addresses the container by ID, and has a timeout. Exit codes are reported, not raised, as for `telemetry`; only a timeout or a missing CLI raises. A new setting `log_exec_timeout` (default 15 s, CLI/env alongside `exec_timeout`) bounds them, because they move more data than telemetry. At most 4 log operations run at once (own semaphore, separate from the poll cycle's `max_concurrent_execs`).

| Operation | Argv shape | Notes |
|---|---|---|
| `list_attempts(container_id)` | `docker exec --user agent <id> sh -c <CONSTANT SCRIPT>` | The script is a constant string with no interpolation. It walks `/data/logs/*/*/` and prints one record per run: `issue`, `run`, whether `agent_output.json` exists, and the raw `attempt.json` text (empty when missing). One exec for the whole instance (the research measured `find -printf` at 0.55 s for 59 attempts). The exact script is an implementation detail to prove against a real container in slice 1. |
| `list_run_files(container_id, issue, run)` | `docker exec --user agent <id> ls -1 /data/logs/<issue>/<run>` | Validated `issue` and `run` only. Non-zero exit means "run not found". |
| `read_file(container_id, issue, run, file, cap)` | `docker cp <id>:/data/logs/<issue>/<run>/<file> -` | Never `-L`, never a directory, never a path from the request. Output is a tar stream (research section 6.1). |

`read_file` details:

- Parse the stream with `tarfile` in streaming mode (`r|`). Exactly one regular-file member is expected; anything else (directory, symlink member, zero or several members) is "layout not recognised".
- The member header gives the total size. Read at most `cap` bytes of content, then terminate the `docker cp` process (kill and reap, as `telemetry` does on timeout).
- Result: `bytes` (up to `cap`), `total_size`, `truncated` (`total_size > len(bytes)`).
- Read the exit status of `docker cp` itself, not of a pipeline: a missing file is exit 1 with `Could not find the file` on stderr and maps to `FileNotFound`.
- The download operation is the same call with no cap, streamed to the client chunk by chunk rather than buffered (one baseline log measured 12.8 MB).

Caps:

| Use | Cap |
|---|---|
| Evidence file or command log preview | 256 KiB (262,144 B) of content |
| `agent_output.json` for the timeline | 4 MiB of content, then the page shows "event stream truncated: showing the first N events of a M B file". It cannot be previewed at 256 KiB because paging needs the whole stream; the measured max is 929 KB |
| `attempt.json` | 64 KiB (measured max 2.4 KB; larger means not recognised) |
| Download | none, streamed |

## 4. Validation and the allow-list (ADR 0003)

Applied in the route layer before the gateway is called, and again inside the gateway (defence in depth: the gateway must not trust its caller, because `docker cp` resolves `..` and there is no other barrier). Use `re.fullmatch`, never `match` with `$`.

| Value | Pattern |
|---|---|
| `issue` | `\d{1,9}` |
| `run` | `\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}(\.\d{1,9})?Z` |
| `file` | `\d{4,}_(tool_call\.json\|tool_result\.json\|model_response\.txt)`, or exactly `agent_output.json`, or exactly `attempt.json`, or `(setup\|baseline_check\|check)_(stdout\|stderr)\.log` |

A value that fails validation gives 404 (routes) or raises `ValueError` (gateway); it never reaches argv. `transcripts/` and everything under `/data/state` stay unreachable, deliberately. Tests must include `..`, `/`, `%2e%2e`, a newline, an empty string, leading `-`, and a symlink-shaped name for each of the three values.

## 5. Parsing rules (tolerance)

The reader owns a small parser module, pure functions on bytes/text, so it is testable without Docker. The layout has no contract (ADR 0003), so every rule below prefers rendering something over failing.

**`attempt.json`** (single JSON object, merged progressively): every key except `started_at` may be absent; unknown keys are ignored. Used keys: `outcome`, `started_at`, `completed_at`, `duration_seconds`, `issue_title`, `agent_version`, `pull_request.{number,url}`, cost. Cost is `token_budget.total_cost_usd` when present, else the sum of `model_usage[*].costUSD`, else blank. Missing or unparsable file: the header shows "no attempt.json: in progress or crashed".

**Attempt status** in the list:

- `outcome` present: show it (`complete`, `incomplete`, `infrastructure_error`, `no_changes`, `handoff`; render any other string as is).
- No `outcome`: "in progress" and highlighted when it is the newest run of its issue and the fleet row's current attempt issue number (from the drawer view model) equals that issue; otherwise "no outcome (crashed or abandoned)".
- No `agent_output.json` in the run: the row is shown, its link leads to the run page, which renders "layout not recognised" (never a 500).

**`agent_output.json`**: JSON lines. Record keys `timestamp`, `level`, `event`, `phase`, `detail`, `issue_number`; any may be missing. A line that does not parse becomes a synthetic row (`event=unparsable_line`, `level=WARNING`, first 200 chars of the line as detail) so row numbers keep matching the file. The sequence number shown is the 1-based line number. `level` is `INFO|WARNING|ERROR`; show other values unstyled. Unknown event names render generically (event name plus detail text); nothing branches on the full event list, which grows between agent releases.

**Evidence references**: only for events `tool_call`, `tool_result`, `model_response`. The detail is parsed with:

```
^(?P<tool>.*?)(?: \[(?P<thread>main|subagent:[^\]]+)\])? -> (?P<file>\d{4,}_(?:tool_call\.json|tool_result\.json|model_response\.txt))
```

- Thread tag absent (old agent versions) is valid; `thread` is then none.
- No `->` in the detail (agent ran without an archive, detail embeds the payload): the row renders as a plain event with its detail text and no "open" action.
- A file name that fails the allow-list never becomes a link.
- The parsed file must still be requested through the route, which validates again.
- Skill events (`skill_call`, `skill_result`) carry their payload inline, have no file, and are plain rows.
- No ok/error badge: the `(ok|error|interrupted)` suffix was seen in 0 of 3,454 results, so it is neither required nor used.

**Call/result pairing**: results carry no call id, and "number minus 1" holds for only about 80% of results. Pair FIFO per `(tool, thread)` in event order (a `tool_result` takes the oldest unmatched `tool_call` of the same key), as the prototype does. It is a heuristic and the UI says so ("paired by order"). Calls without a result stay unpaired (73 of 3,527 in the corpus); a result without a call renders without a pair link. Pairing runs over the full event stream before filtering and paging, so a pair link works across pages.

## 6. Pages

### 6.1 Attempts list (`/instances/{name}/logs`)

One table, newest first (sort by run directory name, which is an ISO timestamp), server-paged at 200 rows. Columns: issue (`#N`, links to the run page), title, started, outcome/status, duration, cost, PR (link when `pull_request.url` exists, plain number otherwise), and a note when the run has no events. The active attempt row is highlighted. Header shows the instance name, a count, and the refresh link. An empty `/data/logs` renders "no attempts yet".

### 6.2 Attempt page (`/instances/{name}/logs/{issue}/{run}`)

Variant A of the prototype.

- Header: issue title, outcome, started, duration, cost, agent version, PR, event count (from `attempt.json`, tolerant). Below it, links to the command logs that exist in the run (`list_run_files` intersected with the command-log allow-list), each opening in the same inline viewer.
- Filters (GET form, no JS required): `level` select, `event` select (names observed in this stream), `q` text (case-insensitive substring of `detail`), reset link. The server filters, then pages.
- Paging: 200 rows per page, prev/next, "page X/Y, N events".
- Table columns: `#` (line number), time (`HH:MM:SS` of `timestamp`), level, event, detail.
- Detail cell: plain text for ordinary events. For events with an Evidence file: tool name, `[subagent:id]` tag when present, the file name, and an "open" action; model responses show "reply". When a pair exists, add "open result" / "open call".
- "Open" is an htmx `GET` of the file fragment into a slot directly under the row (`hx-target`, `hx-swap="innerHTML"`); activating it again closes the slot.
- File fragment content: a small bar ("N B", download link, formatted/raw toggle), then the content in a `<pre>`. Formatted means pretty-printed JSON for `*.json`; it falls back to raw text when the content is truncated or is not valid JSON. `raw=1` shows the text as read. A truncated preview shows "truncated at 256 KB, N B total" prominently, above the content. Call and result opened together appear in two slots under the same row, as in the prototype.
- When the stream itself is truncated (over 4 MiB) or the file is missing, show the notice from section 3 or "layout not recognised" respectively, with whatever header data was readable.

### 6.3 Output safety

File and event text is untrusted agent output. Everything is rendered through Jinja autoescape, never `|safe`, never `innerHTML` with file text. The download route sets `Content-Disposition: attachment`, `Content-Type: application/octet-stream` and `X-Content-Type-Options: nosniff`. Preview fragments are `text/html` containing escaped text only. No extra redaction: the agent redacts at write time (ADR 0003).

## 7. Errors

| Situation | Behaviour |
|---|---|
| Unknown instance | 404 page "instance not found" |
| Instance not running | 200 page "Attempt logs are available for running instances only" |
| Docker CLI missing, timeout, daemon error | 200 page or fragment with the human-readable reason from the gateway (same wording as other gateway errors); no stack trace, no 500 |
| Run not found (`ls` non-zero, `FileNotFound`) | 404 page "attempt not found" |
| Validation failure | 404, no Docker call |
| Unrecognised layout (no `agent_output.json`, bad tar shape, zero events parsed) | page renders "layout not recognised" plus what is readable |

## 8. Testing approach

- Parser tests use fixtures cut from real logs (a few hundred bytes each; store under `tests/fixtures/attempt_log/`): current format with thread tags, old format without tags, an event with no `->`, an unknown event, an unparsable line, `attempt.json` with and without `outcome`/`pull_request`, call/result order with parallel calls.
- Gateway tests assert argv exactly (the pattern in `tests/test_gateway.py`), the tar stream handling (header size, cap, kill after cap, non-tar input, missing file), and every validation rejection from section 4.
- Web tests use the fake gateway extended with scripted `list_attempts`, `list_run_files`, `read_file` results, covering each row of the error table.
- One integration test in `tests/test_integration_docker.py` style against a throwaway container: lists, reads a small file, reads a file over the cap, rejects a traversal attempt. Gated like the existing integration tests.

## 9. Slice candidates

Ordered by dependency. Each is vertically small enough for one implementation session.

1. **Gateway read operations and validation.** `list_attempts`, `list_run_files`, `read_file` (tar parsing, cap, kill, exit-status handling), the three validators, `log_exec_timeout`, the log semaphore, fake gateway support.
   *Accepts when:* argv tests pass; every traversal case is rejected before argv; `read_file` returns at most the cap and reports `total_size`/`truncated`; the constant listing script is proven on a real container.
2. **Attempt log parsers.** Pure module: `attempt.json` summary, event stream (line numbers, unparsable lines), evidence reference parser, FIFO pairing, filtering and paging helpers.
   *Accepts when:* fixture tests cover every tolerance rule in section 5; no function raises on garbage input.
3. **Attempts list page and drawer link.** Route, template, status/highlight logic, paging, not-running and not-found handling, link in the drawer.
   *Accepts when:* a running instance lists its attempts newest first with outcome, duration, cost, PR; a not-running instance shows the explanatory page; missing `attempt.json` and missing `agent_output.json` render without error.
4. **Attempt page: timeline.** Route, header, filters, paging, truncated-stream notice, command-log links, "layout not recognised" path.
   *Accepts when:* filters and paging compose and the counts match the filtered stream; a 929 KB stream pages correctly; an unknown event and an unparsable line render; no 500 on any broken fixture.
5. **Evidence file viewer.** Fragment route, inline slot via htmx, formatted/raw toggle, truncation marker, call/result pairing links, download route with streaming.
   *Accepts when:* "open" expands under the row and a second activation closes it; a result over 256 KB shows the marker and the true total; "open result" opens the FIFO-paired file; download returns the whole file with attachment headers; HTML in file content is shown as text.
6. **Docs and integration test.** README section for the view and its running-only limit, ADR 0003 and `CONTEXT.md` terms committed, the Docker integration test from section 8.
   *Accepts when:* the integration test runs in the existing gated suite; README states the read paths and the no-contract caveat.

Slices 1 and 2 are independent and can run in parallel; 3, 4 and 5 depend on both; 5 depends on 4's row markup.

## 10. Residual risks (accepted)

- An agent release that moves or renames these files breaks the view until the dashboard follows (ADR 0003 consequence).
- FIFO call/result pairing is a heuristic; the UI labels it as such.
- Reading `agent_output.json` from the head of a runaway 4 MiB+ stream hides the newest events; the notice says so, and the download of `agent_output.json` is available.
- Each page view costs one or two Docker calls with no caching; acceptable for a single operator on a LAN, revisited only if it hurts.
