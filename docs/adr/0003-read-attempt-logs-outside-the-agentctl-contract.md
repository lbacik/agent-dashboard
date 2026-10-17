# Read Attempt logs from the agent's `/data` volume, outside the `agentctl` contract

The telemetry snapshot (`agentctl telemetry --json`) describes the current
moment. The per-attempt history lives only in the files the agent writes to
its `/data` volume; no `agentctl` subcommand exposes it. This record fixes
how the dashboard reads those files. Settled in
[Charting decisions](https://github.com/lbacik/agent-dashboard/issues/16);
layout and access verified against the agent in
[issue #17](https://github.com/lbacik/agent-dashboard/issues/17) (branch
`research/attempt-log-layout`).

## Layout (verified)

```
/data/state/attempt.json            only while an attempt is active
/data/logs/<issue>/<started_at>/    issue = digits; started_at = UTC, ':' -> '-'
    attempt.json                    merged progressively; only `outcome` marks completion
    agent_output.json               JSON lines; event `detail` names the evidence file
    NNNN_tool_call.json NNNN_tool_result.json NNNN_model_response.txt
```

An attempt record carries `outcome`, `started_at`, `completed_at`,
`duration_seconds`, and, only when the model ran, `model_usage` (per-model
cost/token counters) and `model_stop_reason`. Every key except `outcome` is
optional. The active attempt's `started_at` is in `/data/state/attempt.json`.

## Access

- **Source**: files on the agent's `/data` volume, not an `agentctl`
  subcommand.
- **Listing (running instances)**: `docker exec --user agent <id> ...` with
  fixed commands (`ls`/`stat`/`find`/`python` are present in the agent
  image). The Attempt history page collects every `attempt.json` plus the
  active checkpoint in **one** exec call running a fixed Python collector;
  evidence files are read on demand, one per view.
- **Single files**: `docker cp <id>:<path> -` (a tar stream, untarred by the
  reader).
- **Scope**: running instances only in v1. `docker exec` fails on stopped
  containers; they render a clear message instead.
- **Size**: evidence previews truncate at about 256 KB with a visible
  marker; no extra redaction (the agent redacts at write time).

## Safety

The gateway stays the only module that builds Docker argv (ADRs 0001, 0002):

- argv entries are only regex-validated container IDs, fixed command names,
  and the fixed collector script: no shell, no raw paths;
- the daemon resolves `..`, so names taken from container output are
  re-validated (digits-only issue, timestamp-shaped run, allow-listed file
  names) before any further argv is built.

## Tolerance

An unrecognised or missing layout renders as "layout not recognised", never
a 500. Attempts without usage data show "n/a" for cost and tokens, never
zero; attempts without an outcome are marked active or interrupted. Views
are static with manual refresh in v1.

## Consequences

- New gateway argv stays pinned by tests, like discovery and telemetry.
- Revisit when the agent documents or versions the log layout, offers
  `agentctl logs`, or when stopped-instance logs need a read path
  (`state/completed_attempts.json` via `docker cp` is a cheap but
  incomplete index).
