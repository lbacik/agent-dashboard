# Stopped-instance Attempt logs via `docker cp` tree copies

Stopped Managed instances hide in the fleet page's collapsed section, but
operators still need their Attempt logs. Every Attempt log read used
`docker exec`, which fails on stopped containers, so the history, attempt
log, progress and evidence routes rendered "running managed instances only".

## Decision

Read stopped instances with `docker cp` tree copies, parsed dashboard-side:

- `copy_logs` copies `/data/logs` for the attempt list (history page, drawer
  stats card);
- `copy_run` copies one `/data/logs/<issue>/<started>` directory for a
  single attempt (attempt log page header/events/file listing, progress
  chart events);
- Evidence previews and downloads already arrive via `docker cp` single-file
  reads, which work on stopped containers unchanged — their running-only
  guards were removed, not replaced.

No new Docker verb, no helper container, no shell: the argv carries only the
validated container ID plus fixed paths (the validated attempt reference
joined inside the gateway), and tar member names are re-validated during
parsing (digits-only issue, timestamp-shaped run directory, recognised file
names; only regular-file members; `..`/absolute names skipped). Caps and
tolerance mirror the exec collectors entry for entry, so a stopped instance
renders what the same exec would have rendered while running.

A missing tree (gone volume, or an instance that never wrote logs) maps to
`FileNotFoundError` before parsing and renders a clear 200 message ("its
volume is gone or it holds no logs" for lists, "Attempt not found … its
volume is gone or the attempt is no longer stored" for single attempts),
never an error.

## Alternatives considered

A short-lived helper container on the instance's volume (e.g. `docker run
--volumes-from`) would keep the collector scripts running unchanged, but it
needs image pulls, container lifecycle management, and volume-name discovery
— all new privileged surface for the same bytes `docker cp` already streams.
Rejected.

## Consequences

- The Docker access model is unchanged (`docker cp` was already used for
  evidence files); no new argv verb to pin beyond the two copy argv builders.
- `gateway.py` stays the only module that builds Docker argv; the
  `stopped.py` parsers never touch Docker.
- A missing or moved log layout still renders "layout not recognised".
