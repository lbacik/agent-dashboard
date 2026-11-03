# Built-in password authentication with server-side sign-in sessions in LAN mode

Reaching the dashboard from a private network without an SSH tunnel needs
each operator to authenticate, so that a small group of people can use it
on a network that also carries untrusted devices. The dashboard authenticates
them itself, with the same mechanism in the host-process and container
installs. A reverse proxy may sit in front of it to terminate TLS, but the
dashboard never trusts an identity passed in request headers.

- **Scope.** Authentication applies only in LAN mode, the explicit opt-in
  that leaves the loopback posture. The default loopback + SSH tunnel install
  keeps ADR 0007: no sign-in, no sessions. In LAN mode every request needs a
  sign-in session, including one that arrives over loopback (an SSH tunnel);
  there is no loopback exemption.
- **Credential.** A password per person, stored as an argon2id hash with at
  least OWASP's minimum parameters.
- **Sign-in sessions.** Server-side: the cookie holds only a random ID, the
  record lives in an SQLite file under `$XDG_STATE_HOME/agent-dashboard/`
  next to the Control audit. The ID is regenerated on sign-in; the cookie is
  `HttpOnly` and `SameSite=Strict`, plus `Secure` with the `__Host-` prefix
  over TLS. A person may hold several sign-in sessions at once (one per
  browser). Defaults: 30 minutes idle, counted from any request including an
  open `/stream`, and 12 hours absolute; both configurable.
- **Ending a sign-in session.** Sign-out deletes the current one. Changing a
  person's password or removing the person deletes all of theirs at once.
  `/stream` re-checks the sign-in session on every cycle and closes when it
  has ended; an htmx request gets 401 with `HX-Redirect: /login?next=<page>`,
  a page navigation a 303 to `/login`; `next` accepts only a relative path.
- **Sign-in protection.** `POST /login` and `POST /logout` pass the ADR 0007
  `Origin` and `Sec-Fetch-Site` checks (without `HX-Request`), with the
  scheme derived from the request instead of the hard-coded `http://`, which
  Control actions get too. Failed sign-ins are slowed by a growing delay per
  account and per client address; accounts are never locked. Sign-ins,
  failures and sign-outs are logged as one stdout line each, not in the
  Control audit.

## Considered Options

- **Authenticating reverse proxy (Caddy or Traefik with Authelia or
  oauth2-proxy)**: rejected. oauth2-proxy needs an OAuth/OIDC provider and
  Authelia a real domain, neither of which is assumed. Identity headers are
  trustworthy only if the proxy is the sole way in, which a loopback bind
  cannot tell apart from an SSH tunnel, and in the container install the
  proxy is not on loopback at all. No proxy ends an open `/stream`, and a
  refused reconnect or a redirect to a portal on another origin leaves the
  page silently stale.
- **Tailscale Serve identity headers**: rejected. It needs a Tailscale
  account and client on every device, has no web sign-out, and proxies only
  to `http://127.0.0.1`, so the SSH tunnel problem stays.
- **Signed-cookie sessions (Starlette `SessionMiddleware`)**: rejected. They
  cannot be revoked: sign-out clears one browser only and removing a person
  does not lock out their cookie until it expires.
- **HTTP basic auth in the app**: rejected. No sign-out, and an argon2id
  check on every htmx request and SSE reconnect.
- **Passkeys (WebAuthn)**: not in this effort. They need a domain name and a
  TLS certificate the browser trusts without warnings, so they cannot be the
  one mechanism that works in every TLS setup.
- **Idle timeout counted only from operator clicks**: rejected. A dashboard
  left open as a monitor would sign itself out.

## Consequences

- The dashboard owns a sign-in page, a sign-in session store and password
  hashing, and gains `argon2-cffi` as a dependency.
- The `http://` literal in the Control action `Origin` check must go
  whatever the TLS setup.
- How accounts are created and passwords set or reset, roles, the bind and
  `Host` allow-list in LAN mode, and TLS termination are decided separately.

Decided in [Decide built-in auth vs an authenticating reverse proxy](https://github.com/lbacik/agent-dashboard/issues/143),
building on [Survey authenticating reverse proxies and built-in auth options](https://github.com/lbacik/agent-dashboard/issues/141)
and [Research TLS on a private LAN](https://github.com/lbacik/agent-dashboard/issues/142).
