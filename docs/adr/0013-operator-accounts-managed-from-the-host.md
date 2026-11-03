# Operator accounts managed from the host with set-password links

Built-in sign-in in LAN mode (ADR 0009) needs Operator accounts, each with
an account name that never changes and one Role (`viewer` or `controller`).
They are managed only from a shell on the Docker host, through subcommands
of `agent-dashboard`, and every person chooses their own password through a
one-time set-password link, so that the Control audit names the person who
actually acted and no one, the host operator included, knows anyone else's
password.

- **Where accounts live.** In the same SQLite file as sign-in sessions,
  under `$XDG_STATE_HOME/agent-dashboard/` (the container's state volume,
  ADR 0012), with each account's Role, so ending all of a person's sign-in
  sessions on a reset or removal is one transaction. The Control audit stays
  its own JSONL file.
- **Who manages them.** Whoever has a shell on the host, which already
  carries Docker access (ADR 0001, ADR 0012). There is no admin Role and no
  account management in the web UI. The commands are
  `agent-dashboard account add <name> [--role viewer|controller]`,
  `account reset <name>`, `account role <name> <role>`,
  `account remove <name>` and `account list`; `agent-dashboard` without a
  subcommand still serves. In the container they run through
  `docker compose exec`. They work whether LAN mode is on or not, so
  accounts can be prepared before turning it on.
- **Set-password link.** `add` creates an account without a password and
  `reset` deletes the password and all sign-in sessions at once; both print a
  link to `/set-password?token=…`, as a path, or as a full URL when given
  `--url`. The token is 256 random bits, stored only as its SHA-256 hash,
  single-use, valid for 24 hours, and a new link invalidates the previous
  one. The page exists only in LAN mode, passes the same secure-context,
  `Origin` and `Sec-Fetch-Site` checks as sign-in (ADR 0009, ADR 0011), logs
  one stdout line when used, and redirects to `/login` instead of signing
  the person in.
- **Passwords.** 15 to 128 characters (NIST SP 800-63B for a single-factor
  password; the upper bound caps argon2id work), no composition rules, no
  breached-password list.
- **Account names.** `^[a-z][a-z0-9-]{0,31}$`. `remove` keeps the account as
  a tombstone (no password, no sign-in sessions, never signs in again) and
  its name is never reused, so a Control audit entry always means one
  person; someone who returns gets a new name.
- **Roles.** New accounts are `viewer` unless `--role controller` is given.
  A change applies from the next request, without ending sign-in sessions.
  Nothing prevents removing or downgrading the last `controller`; the CLI
  warns that Control actions are then unavailable to everyone, and the host
  can always grant the Role again.
- **No account yet.** LAN mode starts with no account and writes one stdout
  line saying how to create one. Refusing to start would stop the container,
  and with it `docker compose exec`.
- **Trail.** Each account records when it was created, when its password
  was last set and when it was removed, shown by `account list` with the
  Role and the number of open sign-in sessions. Account changes are not
  Control actions and do not enter the Control audit.

## Considered Options

- **Accounts in a file the operator edits**: rejected. An argon2id hash is
  not typed by hand, so it needs a tool anyway, and the server would have to
  notice the edit before it could end the sign-in sessions of a removed
  person.
- **An admin page in the web UI**: rejected. It needs a third Role, forms
  and their protection, for a small group whose manager already has a shell
  on the host.
- **The host operator sets or generates the password**: rejected. Everyone's
  password would be known to one person, so an `operator` in the Control
  audit would not show who acted, and passwords would travel over chat.
- **Removal as a hard delete with the name free again**: rejected. A later
  account under the same name would merge two people in the Control audit.
- **Keeping the old password valid until the link is used**: rejected. A
  reset is most often needed when the password leaked.
- **A configured public URL for the link**: rejected. The address differs
  between the proxy and the SSH tunnel; `--url` covers the convenience.
- **Changing one's own password in the UI**: not in this effort; a reset
  from the host covers it for a small group.

## Consequences

- The CLI gains subcommands and the SQLite schema gains accounts and
  set-password tokens next to sign-in sessions.
- A `/set-password` page joins the sign-in page, and its look belongs with
  the sign-in page prototype.

Decided in [Decide how operator accounts are created, how passwords are set and reset, and where accounts live](https://github.com/lbacik/agent-dashboard/issues/150),
building on [ADR 0009](0009-built-in-password-auth-in-lan-mode.md),
[ADR 0011](0011-tls-at-a-reverse-proxy-sign-in-from-a-secure-context.md) and
[ADR 0012](0012-container-install-is-a-supported-lan-path.md).
