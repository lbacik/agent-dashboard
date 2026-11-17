# Sealed archive cache lives on its own container volume

The Sealed Attempt Archive cache keeps sealed archives on the host under the
XDG cache home (`~/.cache` fallback, directory mode 0700, file mode 0600),
bounded at 256 MiB by default with least-recently-used eviction. Re-reading a
sealed Attempt then makes no Docker call. The base `docker-compose.yml` runs
the container with `read_only: true` (ADR 0012): only `/state` (named volume)
and `/tmp` (tmpfs) are writable, and only `XDG_STATE_HOME` points at a
writable location. The cache therefore resolved to `/home/appuser/.cache` on
the read-only root, `SealedArchiveCache` failed creating it with `OSError:
[Errno 30] Read-only file system`, the process died at startup and the
healthcheck never passed.

- **The cache gets its own named volume.** The base compose file mounts a
  `cache` volume at `/cache` and sets `XDG_CACHE_HOME: /cache`, so the cache
  resolves to `/cache/agent-dashboard/sealed-attempts`. The image creates
  `/cache` owned by the non-root user with mode 0700, the same way it
  creates `/state`, so a fresh volume inherits that owner. The LAN overlay
  inherits both the mount and the variable. The cache survives container
  recreates and restarts.
- **It stays out of `/state`.** `/state` holds the Control audit
  (`control-audit.jsonl`) and the Operator account store
  (`accounts.sqlite3`). A throwaway cache with its own size bound and
  eviction does not belong next to them: dropping the cache must never risk
  the audit or the accounts, and the audit's append-only record must not
  compete with cache eviction.
- **Hardening is unchanged.** `read_only: true`, the `/tmp` tmpfs,
  `cap_drop: [ALL]` and `no-new-privileges` stay as they are; the writable
  set grows from `/state` and `/tmp` to `/state`, `/cache` and `/tmp`.

## Considered Options

- **Point the cache at the `/tmp` tmpfs**: rejected. It would use container
  RAM for up to 256 MiB and lose the whole cache on every restart, so every
  sealed Attempt would be re-fetched after each recreate.
- **Point the cache at `/state`**: rejected (see above). It mixes a
  throwaway, evicted cache with the Control audit and the account store.
- **Set the cache directory setting explicitly
  (`--sealed-archive-cache-dir` /
  `AGENT_DASHBOARD_SEALED_ARCHIVE_CACHE_DIR`) instead of `XDG_CACHE_HOME`**:
  rejected. The setting stays available as an override, but the container
  default belongs in the XDG variable, next to `XDG_STATE_HOME`, so one look
  at the compose `environment` shows where state and cache each go.
- **Leave the cache on the read-only root and fail at startup with a clean
  error**: a separate follow-up, not this fix. Startup still crashes with a
  traceback when the cache directory is unwritable.

## Consequences

- Extends
  [ADR 0012](0012-container-install-is-a-supported-lan-path.md): the base
  compose file's writable locations are `/state`, `/cache` and `/tmp`.
- A CI job builds the image and brings the base stack up with
  `docker compose up -d --build --wait`, so a broken build or an unwritable
  cache directory fails CI instead of the next deploy.
