# LAN access: spec

Destination of map "LAN access to the dashboard with authentication" (issue 92). Ready to be sliced into implementation issues. Planning only: no feature code lives on this map.

Vocabulary follows `CONTEXT.md`: **LAN mode**, **Sign-in session**, **Operator**, **Operator account**, **Role**, **Set-password link**, plus **Control action**, **Control audit** and **Reach** from earlier efforts.

Inputs, all resolved on the map:

- Authentication mechanism, sign-in sessions and their lifetime: ticket "Decide built-in auth vs an authenticating reverse proxy", [ADR 0009](../adr/0009-built-in-password-auth-in-lan-mode.md) (PR 149).
- Operator accounts, Roles and the Control audit's `operator`: ticket "Decide operator identity, roles and their place in the Control audit", glossary in PR 152.
- LAN mode switch, bind, `Host` and `Origin` checks, trusted proxies: ticket "Decide what replaces the loopback bind and Host allow-list", [ADR 0010](../adr/0010-lan-mode-bind-and-host-check.md) (PR 153, supersedes ADR 0008).
- TLS at a reverse proxy, sign-in only from a secure context, cookie attributes: ticket "Decide TLS termination and certificate source on the host", [ADR 0011](../adr/0011-tls-at-a-reverse-proxy-sign-in-from-a-secure-context.md) (PR 154).
- The container as a supported LAN path: ticket "Decide whether the container deployment becomes a supported LAN path", [ADR 0012](../adr/0012-container-install-is-a-supported-lan-path.md) (PR 155, partly supersedes ADR 0002).
- Account management and Set-password links: ticket "Decide how operator accounts are created, how passwords are set and reset, and where accounts live", [ADR 0013](../adr/0013-operator-accounts-managed-from-the-host.md) (PR 156).
- Look and copy: variant A of ticket "Prototype the sign-in page, signed-in indicator and sign-out" (throwaway code `prototype/lan_sign_in/` on branch `prototype/lan-sign-in`; reuse the copy and the rules, not the code).
- Research: branches `research/lan-auth-options` and `research/lan-tls`.
- Constraints: ADR 0001, 0002, 0006, 0007 as revised by ADR 0009–0013. `gateway.py` stays the only module that builds Docker argv; this effort adds no argv. Templates consume framework-free view models only.

The ADR the map's destination asks for, superseding the loopback posture of ADR 0001/0002 and ADR 0008, is ADR 0010 together with ADR 0012. No further ADR is needed. Where ADR 0010 says LAN mode refuses to start without an Operator account, ADR 0013 (decided later) holds instead: LAN mode starts without one and says so on stdout (section 2.3).

Settled while writing this spec (ticket "Write the LAN access spec"): the setting names and defaults in section 2 (`--lan-mode on|off`; sign-in session lifetimes in seconds, like the other timeouts), the growing delay (1 s doubling to a 60 s cap, kept in memory), a persistent cookie whose `Max-Age` is the remaining absolute lifetime, uvicorn's own proxy-header handling switched off, the store file and its schema, the CLI output and exit codes, the gated URL `/whoami`, the stdout `auth` lines, the response headers of the sign-in pages, and the compose files.

## 1. Scope

In this effort:

- LAN mode: one explicit switch, the startup checks, the bind and `Host`/`Origin` rules of ADR 0010, `X-Forwarded-For` from trusted proxies.
- Built-in password sign-in with server-side sign-in sessions: `/login`, `/logout`, the gate on every other route, `/stream` ending with the sign-in session.
- Operator accounts with one Role each, managed only with `agent-dashboard account …` on the host, and the `/set-password` page a Set-password link opens.
- Roles in the Control console and the Control audit (`operator`, `not_permitted`).
- The top bar's Operator chip and Sign out button.
- The container install as a supported LAN path: state volume, hardening, the `docker-compose.lan.yml` overlay.
- The migration from ADR 0008 (`AGENT_DASHBOARD_DISABLE_HOST_GUARD=1` no longer enables Control actions).
- README, compose and Dockerfile wording.

Out of scope (see the map): the public internet, multiple Docker hosts, organisation-wide SSO, passkeys, changing one's own password in the UI, an admin page, TLS in the dashboard process, proxy recipes, a published image, showing who issued a command in the UI. Nothing changes in simple-coding-agent (section 13).

Unchanged outside LAN mode: no sign-in, no sign-in session, no new route answers (section 5.1), Control actions only on a loopback bind (ADR 0001, ADR 0007). The default install, loopback plus SSH tunnel, behaves as today apart from the scheme-agnostic `Origin` check (section 4.2) and the migration of section 2.4.

## 2. Settings and startup

### 2.1 New and changed settings

CLI flag plus `AGENT_DASHBOARD_*` env with the existing precedence (flag > env > default). `TRUTHY`/`FALSY` are the existing spellings in `config.py`.

| Setting | Flag / env | Default | Rule |
|---|---|---|---|
| `lan_mode` | `--lan-mode on\|off` / `AGENT_DASHBOARD_LAN_MODE` | `off` | Parsed like `control` (on/off plus truthy/falsy); any other value is a startup error, so a typo never silently leaves sign-in off. |
| `trusted_proxies` | `--trusted-proxies` / `AGENT_DASHBOARD_TRUSTED_PROXIES` | empty | Comma-separated IPv4/IPv6 addresses and CIDR blocks, mixed, parsed with `ipaddress.ip_network(entry, strict=False)`. Blank entries and `*` are dropped; any other entry that does not parse is a startup error naming it. Read in both modes, used only in LAN mode (section 4.4). |
| `sign_in_idle_timeout` | `--sign-in-idle-timeout` / `AGENT_DASHBOARD_SIGN_IN_IDLE_TIMEOUT` | `1800.0` s | At least 300 s (the store refreshes activity once a minute, section 6.2); NaN or smaller is a startup error. |
| `sign_in_max_age` | `--sign-in-max-age` / `AGENT_DASHBOARD_SIGN_IN_MAX_AGE` | `43200.0` s | At least `sign_in_idle_timeout` and at most 604800 s (7 days); otherwise a startup error. |
| `host_check` | env `AGENT_DASHBOARD_DISABLE_HOST_GUARD` (no flag) | on | Off only when the variable is exactly `1`, as today. Moves from `os.environ` reads in `app.py`/`control.py` into `Settings`. It now only turns off the `Host` check, in either mode (ADR 0010). |

`--host` help changes from "loopback only" to "Bind address (loopback unless LAN mode or --allow-non-loopback)". `--allow-non-loopback` keeps its meaning outside LAN mode and is ignored in LAN mode.

### 2.2 Startup checks

In order; each failure prints `agent-dashboard: error: <message>` to stderr and exits 2, as the existing `ValueError` path in `cli.py` does.

1. Every setting parses (section 2.1).
2. Outside LAN mode: the existing loopback refusal (`ensure_loopback`), unchanged.
3. In LAN mode: any bind address is accepted.
4. In LAN mode, unless `host_check` is off: `AGENT_DASHBOARD_ALLOWED_HOSTS` holds at least one entry after parsing. Message: "LAN mode needs AGENT_DASHBOARD_ALLOWED_HOSTS: the host names or addresses operators use to reach the dashboard, comma-separated (or AGENT_DASHBOARD_DISABLE_HOST_GUARD=1 to turn the Host check off)".
5. In LAN mode: the state directory and the store open and migrate (section 6.1). A failure names the path.

uvicorn runs with `proxy_headers=False`. Its default (`True`, trusting `X-Forwarded-For` and `X-Forwarded-Proto` from `127.0.0.1`) would rewrite the client address and scheme before the app sees them. ADR 0010 says only the app reads `X-Forwarded-For`, and only from `trusted_proxies`.

### 2.3 Startup lines

One stdout line each, after the checks pass:

- LAN mode on: `agent-dashboard: LAN mode is on: every request needs a sign-in session (bind <host>:<port>, Host check <on|off>, trusted proxies <list or none>)`.
- LAN mode with no account that can sign in (no account, or none with a password): `agent-dashboard: no Operator account can sign in yet. Create one on the host: agent-dashboard account add <name> --role controller (in the container: docker compose exec dashboard agent-dashboard account add <name> --role controller)`. LAN mode still starts (ADR 0013).
- Migration (section 2.4) when it applies.

### 2.4 Dashboard control mode and the ADR 0008 migration

`compute_control_mode` keeps the startup part of the "Controls unavailable" order. The Role part is per request (section 7.1).

| Mode | First matching reason |
|---|---|
| any | `control` off: `control is off (AGENT_DASHBOARD_CONTROL=off, read-only mode)` (unchanged) |
| outside LAN mode | bound to a non-loopback address: `the dashboard is bound to a non-loopback address (<host>); Control actions beyond loopback need LAN mode (AGENT_DASHBOARD_LAN_MODE=on)` |
| LAN mode | none at startup; the bind is never a reason |

`host_check` no longer enters this function. This is the migration of ADR 0010 and ADR 0012, with no grace period. A deployment with `AGENT_DASHBOARD_DISABLE_HOST_GUARD=1` on a non-loopback bind outside LAN mode keeps running, read-only. At startup it writes `agent-dashboard: Control actions are unavailable: AGENT_DASHBOARD_DISABLE_HOST_GUARD=1 no longer enables them on a non-loopback bind. Turn on LAN mode instead (README: "Migrating from AGENT_DASHBOARD_DISABLE_HOST_GUARD")`. The same applies to the container outside LAN mode, which always binds `0.0.0.0`, also when published only to `127.0.0.1` (ADR 0012).

## 3. Routes

### 3.1 New routes (LAN mode only)

Outside LAN mode none of them is registered, so they answer 404 like any unknown path.

| Method | Route | Anonymous | Section |
|---|---|---|---|
| GET | `/login` | yes | 8.1 |
| POST | `/login` | yes | 5.3 |
| POST | `/logout` | no | 5.4 |
| GET | `/set-password` | yes | 8.3 |
| POST | `/set-password` | yes | 6.5 |
| GET | `/whoami` | no | 5.6 |

`/static/*` is anonymous too. Every other route needs a sign-in session in LAN mode. No health endpoint is added.

### 3.2 Middleware order (LAN mode)

1. `Host` check (`HostGuardMiddleware`), unless `host_check` is off. Unchanged: 403 plain text "forbidden: unexpected Host header".
2. Sign-in gate (section 5.1). It resolves the sign-in session and the Operator account once and puts a framework-free `Operator(name, role, signed_in_at)` on `request.state`.
3. The route. Control routes keep their own checks (section 7).

Outside LAN mode the gate is not installed and `request.state.operator` is `None`.

## 4. Request checks

### 4.1 `Host` allow-list

Unchanged parsing (`parse_allowed_hosts`): `localhost` and `127.0.0.1` always allowed, `*` dropped, ports ignored. Required in LAN mode unless `host_check` is off (section 2.2).

### 4.2 `Origin` (both modes)

One function, used by the Control POST routes (replacing the `http://` literal in `control_routes.check_protection`) and by `POST /login`, `POST /logout` and `POST /set-password`:

- `Origin` is present and parses as `scheme://host[:port]` with scheme `http` or `https`. `null` or anything else is refused.
- The host equals the `Host` header's host, case-insensitively, IPv6 in brackets compared without them.
- The ports are equal, where a missing port means the scheme's default (80 for `http`, 443 for `https`) on both sides. The `Host` header's missing port takes the `Origin` scheme's default.
- `X-Forwarded-Host` and `X-Forwarded-Proto` are never read.

A refusal writes one stdout line (section 9) with the `Origin` and `Host` it compared, so a proxy that rewrites `Host` shows up at once (ADR 0011). `Sec-Fetch-Site` (`same-origin` when present) stays as in ADR 0007. `HX-Request: true` stays required on Control routes only. The sign-in forms are plain form POSTs.

### 4.3 Secure context for a password (LAN mode)

`POST /login` and `POST /set-password` accept a password only when the `Origin` (already checked by 4.2) uses `https`, or names a loopback host: `localhost`, an address in `127.0.0.0/8`, or `::1`. Anything else answers 403 with the error box "Sign-in was refused: this page was not opened over HTTPS." The password is not checked and no failure is counted. `GET /set-password` shows the form either way. The page script disables it when `window.isSecureContext` is false (section 8).

### 4.4 Client address

Used only for the per-address delay (section 5.5) and the stdout `auth` lines (section 9). Never for identity, the `Origin` check or the Control audit.

- When the peer address (`request.client.host`) is in `trusted_proxies` and `X-Forwarded-For` is present, walk its comma-separated entries from the right and take the first that is not in `trusted_proxies`. An entry that is not an IP address ends the walk and the peer address is used. If every entry is trusted, the left-most one is used.
- Otherwise the peer address is used and `X-Forwarded-For` is ignored.

## 5. Sign-in

### 5.1 The sign-in gate

For a request outside the anonymous routes (section 3.1), the gate reads the cookie (section 5.2), looks the sign-in session up (section 6.2) and refuses the request when the session is missing, has ended, or belongs to an account that cannot sign in:

| Request | Answer |
|---|---|
| `HX-Request: true` | 401, empty body, `HX-Redirect: /login?next=<page>`. `<page>` is the path and query of `HX-Current-URL` (the page, not the fragment). `next` is left out when the header is missing or its path fails the `next` rule. |
| GET or HEAD `/stream` | 401, plain text "Sign-in required." (EventSource cannot follow a redirect usefully; see section 5.6) |
| other GET or HEAD | 303 to `/login?next=<path and query>` |
| any other method | 401, plain text "Sign-in required." |

A refused request never reaches Control handling, so it never writes a Control audit entry (ADR 0010).

**`next` rule.** Accepted only when it starts with a single `/` (not `//`, not `/\`), has no control characters, is at most 2048 characters, and its path is not `/login`, `/logout` or `/set-password`. Otherwise `/` is used. Every redirect target derived from `next` passes this rule.

### 5.2 Cookie

- Value: the sign-in session ID, 32 random bytes from `secrets.token_urlsafe(32)`. The store keeps only its SHA-256 (section 6.1).
- Name and attributes follow the sign-in `Origin` (ADR 0011):
  - `https`: `__Host-agent-dashboard-sign-in`, `Secure; HttpOnly; SameSite=Strict; Path=/`.
  - loopback `http`: `agent-dashboard-sign-in`, `HttpOnly; SameSite=Strict; Path=/`, no `Secure` (Safari refuses `Secure` on `http://localhost`).
- `Max-Age` is the time left until the absolute limit, so a browser restart within it keeps the sign-in session. The idle limit is enforced on the server only.
- Reading tries `__Host-agent-dashboard-sign-in` first, then `agent-dashboard-sign-in`.
- Sign-out, and the `/login` page that finds a cookie without a sign-in session, clear both names.

### 5.3 `POST /login`

Form fields `account`, `password`, `next` (hidden, from the query). The body is read with the existing standard-library form helper (`parse_form`); a body over 8 KiB is refused with 413. Order:

1. `Origin` (4.2), `Sec-Fetch-Site`: 403, error box "Sign-in was refused: the request did not come from this page." plus the stdout line.
2. Secure context (4.3): 403.
3. The growing delay (5.5) for the account key or the address key still running: 429 with `Retry-After: <seconds>` and the box "Too many failed sign-ins. Wait N seconds, then try again. That attempt was not checked." The password is not checked.
4. The password check: the account name is lowercased and stripped, and the password is normalised to NFKC (as on set, section 6.5). An unknown name, a removed account, an account without a password, or a password over 1024 bytes runs the check against a fixed dummy hash, so the answer takes the same time, and fails. A failure counts against both keys and answers 401 with "Account name or password is incorrect.", plus "Wait N seconds before trying again." when the new delay is over 1 s. The account name stays filled in. The message never says whether the account exists.
5. Success: when `check_needs_rehash` is true, the password is rehashed and stored. The failure counters of both keys are cleared. A new sign-in session is created (a new ID, so any cookie the browser held is replaced, ADR 0009) and the cookie of section 5.2 is set. 303 to `next`, else `/`.

`GET /login` with a valid sign-in session redirects 303 to `next`, else `/`.

Hashing (argon2-cffi `PasswordHasher` with its defaults: argon2id, RFC 9106 low-memory profile, `t=3`, `m=64 MiB`, `p=4`, above OWASP's minimum) runs in a worker thread (`asyncio.to_thread`) behind a semaphore of 2, so at most 128 MiB is spent on hashing at once and the event loop never blocks. Tests inject a cheap hasher.

### 5.4 `POST /logout`

Behind the gate. `Origin` and `Sec-Fetch-Site` as for sign-in. Deletes only the current sign-in session (ADR 0009), clears both cookie names, and redirects 303 to `/login?signed_out=1`. A refused `Origin` answers 403 plain text "Sign-out was refused: the request did not come from this page." and keeps the session.

### 5.5 Growing delay

In memory, reset by a restart. Two independent maps, each keyed:

- by account name as typed (stripped and lowercased; an entry over 64 characters shares one key, `<long>`, so the map cannot be grown with long names), whether or not the account exists;
- by client address (section 4.4), IPv6 addresses by their `/64`.

After the n-th consecutive failure for a key the delay is `min(2 ** (n - 1), 60)` seconds: 1, 2, 4, 8, 16, 32, 60, 60, … An attempt is refused unchecked (section 5.3 step 3) while either of its keys is inside its delay, and the longer remaining wait is the one shown. A key is forgotten 15 minutes after its last failure. Each map holds at most 10 000 keys and drops the least recently failed first. A success clears both of its keys. Accounts are never locked.

The delay is answered at once. The server never sleeps on a request.

### 5.6 Ending a sign-in session

- **Expiry.** A sign-in session has ended when now is past `last_seen_at + sign_in_idle_timeout` or past `created_at + sign_in_max_age`. Ended rows are deleted when found, and on startup.
- **Activity.** Every request the gate passes refreshes `last_seen_at`, at most once a minute per sign-in session (one write per minute, not per request). An open `/stream` counts: it re-checks and refreshes on every fleet event (section 5.7), so a dashboard left open as a monitor stays signed in until the absolute limit (ADR 0009).
- **Revocation.** `account reset` and `account remove` delete all of the account's sign-in sessions in the same transaction (section 6.4). A Role change ends none (section 7).
- **The page after an ended session.** `GET /login` with a cookie that matches no sign-in session shows the info box "Your sign-in session has ended. Sign in again to continue where you were." (the last four words only with a `next`) and clears the cookie. The reason is never shown.
- **`/whoami`.** `GET /whoami` answers 204 while signed in. Unauthenticated, it gets the gate's answer, so an htmx request gets 401 and `HX-Redirect`.
- **SSE close.** A static script `sign-in.js`, included only in LAN mode, listens for `htmx:sseError` and `htmx:sseClose` on the page and then requests `/whoami` with `htmx.ajax("GET", "/whoami", {swap: "none"})`. When the sign-in session has ended, the 401's `HX-Redirect` takes the page to `/login?next=<page>`. When it has not (a dashboard restart), nothing happens and the SSE extension reconnects as today. At most one check runs at a time.

### 5.7 `/stream`

In LAN mode the generator checks the sign-in session before each `fleet` event and ends when the session has ended. The browser's reconnect then gets 401 from the gate, and section 5.6 takes over. The check also refreshes activity as above.

## 6. Accounts and the store

### 6.1 Store

One SQLite file, `accounts.sqlite3`, in the Control audit's directory (`$XDG_STATE_HOME/agent-dashboard/`, else `~/.local/state/agent-dashboard/`). It holds Operator accounts, sign-in sessions and Set-password links. The directory is created with mode `0700` and the file with `0600`, as for the Control audit. The standard library `sqlite3`, no ORM. Every connection sets `PRAGMA journal_mode=WAL`, `foreign_keys=ON` and `busy_timeout=5000`, so the CLI and the server write safely at the same time. `PRAGMA user_version` carries the schema version (1). Opening a newer version is a startup error. The server's store calls run in a worker thread.

The server opens the store only in LAN mode. The CLI opens it in either mode (ADR 0013), creating it when missing.

Times are Unix epoch seconds (`REAL`, UTC).

```sql
CREATE TABLE accounts (
  name            TEXT PRIMARY KEY,           -- ^[a-z][a-z0-9-]{0,31}$
  role            TEXT NOT NULL CHECK (role IN ('viewer', 'controller')),
  password_hash   TEXT,                       -- argon2 PHC string; NULL until set
  created_at      REAL NOT NULL,
  password_set_at REAL,
  removed_at      REAL                        -- tombstone when set
);
CREATE TABLE sign_in_sessions (
  id_hash      BLOB PRIMARY KEY,              -- SHA-256 of the cookie value
  account      TEXT NOT NULL REFERENCES accounts(name),
  created_at   REAL NOT NULL,
  last_seen_at REAL NOT NULL
);
CREATE INDEX sign_in_sessions_account ON sign_in_sessions(account);
CREATE TABLE set_password_links (
  token_hash    BLOB PRIMARY KEY,             -- SHA-256 of the token
  account       TEXT NOT NULL REFERENCES accounts(name),
  issued_at     REAL NOT NULL,
  expires_at    REAL NOT NULL,                -- issued_at + 24 h
  used_at       REAL,
  superseded_at REAL
);
CREATE INDEX set_password_links_account ON set_password_links(account);
```

An account **can sign in** when `removed_at` is NULL and `password_hash` is not NULL. Set-password link rows are kept for 30 days after they expire, so an old link reads as expired, used or superseded rather than unknown. Older rows are deleted at startup.

### 6.2 Sign-in session lookup

One query joins `sign_in_sessions` to `accounts` by the cookie's hash. The gate accepts the row when the account can sign in and the session has not ended (section 5.6). The Role is read from the account row on every request, never cached in the session (#144).

### 6.3 Set-password links

Issued by `account add` and `account reset`. The token is 32 random bytes (`secrets.token_urlsafe(32)`, 256 bits), stored only as its SHA-256. Issuing one sets `superseded_at` on every unused link of the account in the same transaction. The printed link is `/set-password?token=<token>`, or `<base>/set-password?token=<token>` with `--url <base>`.

A token's state, checked in this order: unknown (no row), used (`used_at`), superseded (`superseded_at`), expired (past `expires_at`), account removed (shown as unknown), valid.

### 6.4 CLI

`agent-dashboard account <command>`. `agent-dashboard` without a subcommand, or with only the serve flags, still serves. The subcommands take no serve flags. They read `XDG_STATE_HOME` like the server. In the container they run as `docker compose exec dashboard agent-dashboard account …`.

| Command | Effect | Output (stdout) |
|---|---|---|
| `account add <name> [--role viewer\|controller] [--url <base>]` | New account without a password, Role `viewer` unless given, plus a Set-password link | `Created Operator account <name> (<role>).` then the link block |
| `account reset <name> [--url <base>]` | Deletes the password and all sign-in sessions in one transaction, issues a new link | `Reset <name>: password deleted, <n> sign-in sessions ended.` then the link block |
| `account role <name> viewer\|controller` | Sets the Role. No sign-in session ends; the change applies from the next request | `<name> is now <role>.` (or `<name> is already <role>.`) |
| `account remove <name>` | Tombstone: password, sign-in sessions and unused links deleted or superseded, `removed_at` set, name kept | `Removed <name>: <n> sign-in sessions ended. The name stays reserved.` |
| `account list` | All accounts, removed ones last | the table below |

Link block:

```
Set-password link for <name> (works once, until 2026-11-04 10:15 UTC):
  /set-password?token=…
Send it to <name> only. Open it through the dashboard's https:// address, or an SSH tunnel to localhost.
```

`account list` columns: `NAME`, `ROLE`, `STATE` (`active`, `link pending`, `no password` for an expired link, `removed`), `CREATED`, `PASSWORD SET`, `REMOVED`, `SIGN-IN SESSIONS` (open, not ended). Times as `YYYY-MM-DD HH:MM` UTC, `-` when empty. With no account it prints `No Operator accounts. Create one with: agent-dashboard account add <name> --role controller`.

**Last `controller`.** When `role … viewer` or `remove` leaves no account that can sign in with the `controller` Role, the command still succeeds and prints to stderr: `Warning: no active account has the controller Role now, so Control actions are unavailable to everyone. Grant it again with: agent-dashboard account role <name> controller`.

**Validation and exit codes.** 0 on success. 2 on usage errors (argparse). 1, with `agent-dashboard: error: <message>` on stderr, for:

- a name that does not match `^[a-z][a-z0-9-]{0,31}$`;
- `add` with a name that exists, removed or not ("account name <name> was used before and cannot be reused");
- `reset`, `role` or `remove` on an unknown or removed account;
- `--url` that is not `http(s)://host[:port]` with an optional path prefix (a trailing `/` is dropped);
- a store that cannot be opened (with its path).

### 6.5 Passwords

NFKC-normalised, then 15 to 128 code points. No composition rules, no breached-password list (ADR 0013). Hashed as in section 5.3. `POST /set-password` checks the length after normalisation.

Setting a password through a link, in one transaction: the link is still valid, `password_hash` and `password_set_at` are set, `used_at` is set, and all of the account's sign-in sessions are deleted. One stdout `auth` line. No sign-in follows (ADR 0013).

## 7. Roles in Control actions

### 7.1 Availability per request

The "Controls unavailable" line and every Control POST route use one per-request function: the startup dashboard control mode (section 2.4) first, then, in LAN mode, the Role.

| Order | Condition | Reason after "Controls unavailable:" |
|---|---|---|
| 1 | `control` off | unchanged |
| 2 | outside LAN mode, non-loopback bind | section 2.4 |
| 3 | LAN mode, Role `viewer` | `your account can view but not issue Control actions` |

The top bar's Control chip keeps showing the dashboard mode (`on`/`off`), not the Role. A viewer keeps `GET /control`, `/control/commands` and `/control/track`. As for every other unavailable reason, the console shows only the line and hides the command timeline.

### 7.2 Order of checks on Control POST routes

Gate (section 5.1) → `Origin` (4.2) → `Sec-Fetch-Site` → `HX-Request` → control mode (2.4) → Role → the rest as in the instance control spec, section 6.3.

A viewer's `review` or `submit` answers 403 with the "Controls unavailable" fragment and writes a `refused` entry with reason `not_permitted`. A viewer's `status` answers 403 and writes nothing, as any refused `status` today. The Role is checked again at submit, so a downgrade between review and submit is refused.

### 7.3 Control audit

Every entry (`submitted`, every outcome, `refused`) gets `operator`: the account name in LAN mode, `null` outside it. The sign-in session ID and the client address are never recorded. The `refused` reasons gain `not_permitted`. Nothing else in the entry changes. The UI still never reads the Control audit.

## 8. UI

Look and copy from variant A of the prototype. Only existing tokens and classes (`.vc-module`, `.vc-module-bar`, `.btn`, `.btn-primary`, `.topbar-chip`). Notice boxes use the semantic `info`/`ok`/`warn`/`danger` bg/ink pairs from `DESIGN.md`, and one new rule for the notice box goes into `style.css` per `DESIGN.md`. No new colours. Templates get view models (`SignInView`, `SetPasswordView`, `OperatorChip`) built outside the template.

**Response headers.** In LAN mode every HTML response sends `X-Frame-Options: DENY` and `Referrer-Policy: same-origin`. The latter keeps `Origin` on form posts, unlike `no-referrer` (ADR 0011). `/login` and `/set-password` also send `Cache-Control: no-store`.

### 8.1 `/login`

- The normal top bar with the subtitle "LAN mode · sign-in required" and no chips. Under it, one narrow centred card with panel bar "Sign in".
- Fields: Account name (`autocomplete="username"`, `autocapitalize="none"`, `spellcheck="false"`) and Password (`autocomplete="current-password"`). One Sign in button (`.btn-primary`). Under it: "No account, or forgot your password? Ask whoever runs the dashboard host for a Set-password link."
- `form[data-secure-form]`: `sign-in.js` disables every field and the button when `window.isSecureContext` is false and shows the warn box "Sign-in requires HTTPS. Open the dashboard through its `https://` address (or an SSH tunnel to `localhost`)."
- One box above the form, at most:

| Situation | Box | Status |
|---|---|---|
| wrong name or password | danger: "Account name or password is incorrect." (+ "Wait N seconds before trying again." when over 1 s) | 401 |
| attempt inside the delay | warn: "Too many failed sign-ins. Wait N seconds, then try again. That attempt was not checked." | 429 |
| `Origin` not https or loopback | danger: "Sign-in was refused: this page was not opened over HTTPS." | 403 |
| `Origin` or `Sec-Fetch-Site` mismatch | danger: "Sign-in was refused: the request did not come from this page." | 403 |
| `?signed_out=1` | ok: "You have signed out." | 200 |
| `?password_set=1&account=<name>` | ok: "Your password is set. Sign in with it now." Name prefilled when it matches the name rule; focus on Password | 200 |
| stale cookie | info: "Your sign-in session has ended. Sign in again to continue where you were." | 200 |

### 8.2 Top bar

On every dashboard page (fleet, Attempt History, Attempt Log, Attempt Progress), only in LAN mode:

- After the Stream and Control chips: a chip `Operator <name> · <role>` with `title="Signed in since HH:MM UTC"` (the sign-in session's `created_at`).
- At the end of the bar: a Sign out button (`.btn`), a plain form POST to `/logout`.

Outside LAN mode the top bar is unchanged.

### 8.3 `/set-password?token=…`

- The same card, panel bar "Set your password", "Account **<name>**", fields New password and Repeat the password (`autocomplete="new-password"`, `minlength="15"`, `maxlength="128"`), a hidden `username` field with the account name for password managers, the hint "15 to 128 characters, anything allowed, spaces included. A long phrase is easiest." with a live character count, one Set password button, and "The link works once, until HH:MM UTC (N h left)".
- The same secure-context box and disabled form as `/login`.
- `POST /set-password` (fields `token`, `password`, `password_repeat`): `Origin`/`Sec-Fetch-Site` (403), secure context (403), token state (410, below), then the length and the match after normalisation (422: "Use at least 15 characters." / "Use at most 128 characters." / "The two passwords differ."), then section 6.5 and 303 to `/login?password_set=1&account=<name>`.
- A link that cannot be used answers 410 with the danger box instead of the form, then "On the host: `agent-dashboard account reset <name>` prints a new link." (not for unknown) and a link to `/login`:

| State | Box |
|---|---|
| expired | "This Set-password link has expired: links work for 24 hours. Ask whoever runs the dashboard host for a new one." |
| used | "This Set-password link has already been used. If that was not you, or you have forgotten the password, ask for a new link." |
| superseded | "A newer Set-password link was issued for this account, so this one no longer works. Use the newest link you received." |
| unknown (also a removed account) | "This Set-password link is not valid. Check that you copied the whole link, or ask for a new one." No account name. |

### 8.4 Control console for a viewer

The existing single "Controls unavailable: <reason>" line with the Role reason (section 7.1). A viewer's POST gets the same fragment with 403. The existing htmx swap opt-in for control routes already covers it.

### 8.5 Output safety

Account names come from the store and match the name rule. Names typed on `/login` are echoed only through Jinja autoescaping. The token never appears on a page, only in the form's hidden field.

## 9. Logging

Each line goes to stdout as `auth <json>`, like `control-audit <json>`. Fields: `at` (ISO 8601 UTC with milliseconds), `event`, and those listed. Values are JSON-escaped, so a typed account name can never break the line. An account name as typed is cut to 64 characters.

| `event` | When | Fields |
|---|---|---|
| `sign_in` | success | `account`, `client` |
| `sign_in_failed` | failed check | `account` (as typed), `client`, `delay_s` |
| `sign_in_delayed` | refused inside the delay | `account` (as typed), `client`, `wait_s` |
| `sign_out` | `/logout` | `account`, `client` |
| `password_set` | link used | `account`, `client` |
| `origin_refused` | any `Origin`, `Sec-Fetch-Site` or secure-context refusal (Control routes, sign-in, sign-out, set-password) | `path`, `origin`, `host`, `fetch_site`, `reason` |

Sign-in session expiry writes nothing. The CLI writes nothing to stdout beyond its output. Nothing here enters the Control audit.

## 10. Container install

### 10.1 Base `docker-compose.yml` (both modes)

- Header comment and `Dockerfile` comment: no longer "test deployments only". They describe both uses, read-only on a trusted LAN without sign-in (ADR 0002) and LAN mode behind a proxy (ADR 0012).
- `image: agent-dashboard` (drops `:test`), still built locally.
- A named volume `state` mounted at `/state`, with `XDG_STATE_HOME: /state` in `environment`. The `Dockerfile` creates `/state` owned by `appuser` before `USER appuser`, so the new volume inherits the owner.
- Hardening: `read_only: true`, `tmpfs: [/tmp]`, `cap_drop: [ALL]`, `security_opt: ["no-new-privileges:true"]`.
- Pass-through of `AGENT_DASHBOARD_LAN_MODE`, `AGENT_DASHBOARD_TRUSTED_PROXIES`, `AGENT_DASHBOARD_SIGN_IN_IDLE_TIMEOUT`, `AGENT_DASHBOARD_SIGN_IN_MAX_AGE`, `AGENT_DASHBOARD_CONTROL` and `AGENT_DASHBOARD_CONTROL_EXEC_TIMEOUT`, next to the existing ones, empty by default.
- Acceptance check on the test host (Raspberry Pi 5, arm64): `docker ps`, `docker exec` and `docker cp` work under `read_only: true`. If the client needs a writable `~/.docker`, set `DOCKER_CONFIG: /tmp/docker` and record that in the compose comment.

### 10.2 `docker-compose.lan.yml` overlay

```yaml
# LAN mode behind a reverse proxy on the same host (ADR 0012).
# Use with: COMPOSE_FILE=docker-compose.yml:docker-compose.lan.yml in .env
services:
  dashboard:
    ports: !override
      - "127.0.0.1:${DASHBOARD_PORT:-8000}:8000"
    environment:
      AGENT_DASHBOARD_LAN_MODE: "on"
      VIRTUAL_HOST: "${VIRTUAL_HOST:?set VIRTUAL_HOST to the dashboard's host name}"
      VIRTUAL_PORT: "8000"
      LETSENCRYPT_HOST: "${LETSENCRYPT_HOST:-}"
    networks: [default, proxy]
networks:
  proxy:
    external: true
    name: "${PROXY_NETWORK:?set PROXY_NETWORK to the reverse proxy's Docker network}"
```

`!override` needs Docker Compose 2.24 or newer. The README says so. `COMPOSE_FILE` in `.env` makes `up`, `exec` and `logs` use both files without `-f`.

### 10.3 `.env.example`

New, commented: `AGENT_DASHBOARD_LAN_MODE`, `AGENT_DASHBOARD_TRUSTED_PROXIES` (example: the proxy network's subnet from `docker network inspect <network> --format '{{(index .IPAM.Config 0).Subnet}}'`), the two sign-in session timeouts, and for the overlay `COMPOSE_FILE`, `PROXY_NETWORK`, `VIRTUAL_HOST` and `LETSENCRYPT_HOST`, marked as the tested nginx-proxy + acme-companion example. `AGENT_DASHBOARD_ALLOWED_HOSTS` gains "in LAN mode: the name in `VIRTUAL_HOST`". `DASHBOARD_BIND` notes that LAN mode publishes to `127.0.0.1` only.

## 11. Documentation

- **README**:
  - Intro and "Control actions": the switch-off paragraph drops ADR 0008 and gives the reasons of section 7.1.
  - New "LAN mode": what it changes, the settings of section 2.1, the startup checks, binding to a specific address plus a firewall.
  - New "Operator accounts": the `account` commands, adding a collaborator, resetting a password, on the host and through `docker compose exec`, and the last-`controller` warning.
  - New "Reverse proxy and HTTPS", the requirements of ADR 0011: TLS at the proxy, `Host` with the browser's port, the only way in, a certificate devices trust (Let's Encrypt DNS-01, a Tailscale `ts.net` name, or a private CA, as examples only), no `Referrer-Policy: no-referrer`, the proxy listed in `AGENT_DASHBOARD_TRUSTED_PROXIES` and setting `X-Forwarded-For` (otherwise one person's failed sign-ins delay everyone's).
  - "Running in Docker (test deployments)" becomes "Running in Docker", covering both uses and the overlay.
  - New "Migrating from AGENT_DASHBOARD_DISABLE_HOST_GUARD": turn on LAN mode, create an Operator account, put a TLS proxy in front or use the SSH tunnel.
  - The Control audit paragraph adds `operator` and the store file next to it.
- **`docs/spec/instance-control.md`**: a note at the top that sections 2 and 6.2 are revised by this spec (non-loopback reason, `Origin` rule, Role, `operator`).
- **`AGENTS.md`**, **`CONTEXT.md` intro**, **`pyproject.toml` description**: mention optional LAN mode with sign-in where they describe the dashboard's reach.
- `argon2-cffi` joins `dependencies` in `pyproject.toml` and `uv.lock`.

## 12. Testing approach

- Web tests through `TestClient`, asserting status codes, headers and HTML fragments, per AGENTS.md. The client sets `Origin`, `Host`, `Sec-Fetch-Site`, `HX-Request`, `HX-Current-URL` and `X-Forwarded-For` explicitly; `TestClient(client=(addr, port))` sets the peer address.
- Every row of the tables in sections 2.2, 4.2, 5.1, 7.1 and 8.1/8.3 has a test. The `Origin` rule is tested with default and explicit ports, IPv6, `null`, a different scheme, and a `Host` without a port.
- The store and the CLI run against a temporary `XDG_STATE_HOME`. The CLI is tested through `main(argv)` with captured output and exit codes. A clock is injected for expiry, links and the delay. A cheap argon2 hasher is injected.
- Concurrency: a CLI `reset` while the app holds the store ends the account's sign-in session on the next request.
- `sign-in.js` and the `isSecureContext` check are not run (no browser tests). Tests assert the markup it depends on (`data-secure-form`, the script tag only in LAN mode).
- Outside LAN mode, the existing suite passes unchanged, apart from the tests that pin ADR 0008 behaviour, which flip.

## 13. simple-coding-agent changes

None. Identity lives only in the Control audit, joined to the agent's command record by the request ID (#144).

## 14. Slice candidates

Ordered by dependency. Each fits one implementation session.

1. **LAN mode settings, the `Origin` rule and the migration.** Settings of section 2.1 (with `trusted_proxies` parsing and `host_check` in `Settings`), startup checks and lines of 2.2–2.4 except the account line, `proxy_headers=False`, the new non-loopback reason, the `Origin` function of 4.2 in the Control routes with the `origin_refused` line, the client-address function of 4.4. No sign-in yet: until slice 3 lands, `--lan-mode on` parses but is a startup error ("LAN mode is not available in this build"), so LAN mode never serves without the gate.
   *Accepts when:* every startup check and reason is tested; `DISABLE_HOST_GUARD=1` on a non-loopback bind is read-only with the startup line; the Control routes accept an `https` `Origin` on the right host and port; the ADR 0008 tests are flipped.
2. **Account store and CLI.** `accounts.sqlite3` and its schema, Set-password link issuing, password hashing helper (argon2, NFKC, lengths), `agent-dashboard account …` with its output and exit codes. Adds `argon2-cffi`.
   *Accepts when:* every command and error of section 6.4 is tested, including the tombstone, link supersession, the last-`controller` warning and the 0700/0600 modes.
3. **Sign-in, sign-out and the gate.** The gate of 5.1, the cookie, `/login`, `/logout`, `/whoami`, the delay, `/stream` re-check and close, `sign-in.js`, the `/login` page and the top bar chip and Sign out (8.1, 8.2), the response headers, the `auth` lines, the no-account startup line. Depends on 1 and 2.
   *Accepts when:* every row of 5.1 and 8.1 is tested; an expired, reset or removed account's next htmx request gets 401 with `HX-Redirect`; `/stream` ends after a reset; the delay sequence and its reset are tested with an injected clock.
4. **Set-password page.** `GET`/`POST /set-password` and section 8.3. Depends on 2 and 3. Can run in parallel with 5.
   *Accepts when:* every link state and every 422 case is tested; a used link signs nobody in and ends existing sign-in sessions.
5. **Roles in Control actions and the Control audit.** Section 7: the per-request availability, the viewer's console line, 403 and `not_permitted`, `operator` on every entry. Depends on 3. Can run in parallel with 4.
   *Accepts when:* a viewer cannot review, submit or check status; a downgrade between review and submit is refused; every entry carries `operator`, `null` outside LAN mode.
6. **Container install.** Section 10: volume, `Dockerfile` state directory, hardening, overlay, `.env.example`, the acceptance check on the test host. Depends on 1 for setting names; runs in parallel with 2–5.
   *Accepts when:* the base compose behaves as before apart from the volume and hardening; the overlay's `docker compose config` shows the loopback-only publish and the proxy network; the read-only check is recorded.
7. **Docs.** Section 11. Last.
   *Accepts when:* the README covers LAN mode, accounts, the proxy requirements, the container overlay and the migration, and no text still calls the container test-only or says `DISABLE_HOST_GUARD` enables Control actions.

Slices 1, 2 and 6 can start in parallel. 3 needs 1 and 2. 4 and 5 need 3. 7 comes last. Slice 3 lifts slice 1's startup error.

## 15. Residual risks (accepted)

- The delay counters live in memory, so a restart clears them. The per-account delay still bounds guessing between restarts, and only the host operator can restart.
- Behind a proxy that is not in `AGENT_DASHBOARD_TRUSTED_PROXIES`, or does not set `X-Forwarded-For`, every browser shares the proxy's address, so one person's failures delay everyone's sign-in. Too wide a trusted block lets a client in it choose its own address bucket, but the per-account delay still applies.
- An IPv6 client can switch `/64` blocks to reset its address bucket. The per-account delay still applies.
- The loopback cookie (`agent-dashboard-sign-in`, no `__Host-`) is sent to every port on `localhost` of the operator's machine while it lives, so another local service there can read it. It is still bound to the tunnel's host and port by the `Origin` check, and it ends with the sign-in session.
- A Set-password link's token appears in the proxy's access log and the browser history. It is single-use, valid for 24 hours, and superseded by the next one.
- A dashboard left open keeps its sign-in session alive until the absolute limit (12 h by default), by design (ADR 0009).
- Idle expiry is accurate to about a minute, because activity is written at most once a minute.
- A Role downgrade does not stop a control exec already in flight.
- With `AGENT_DASHBOARD_DISABLE_HOST_GUARD=1` in LAN mode, a DNS-rebinding page reaches only `/login`. It never carries the host-scoped cookie (ADR 0010).
- The mounted Docker socket stays root-equivalent on the host, as does the `docker` group of the host-process install. Sign-in and the Role decide who reaches it (ADR 0012).
