# Authentication options for LAN access

Research for [#141](https://github.com/lbacik/agent-dashboard/issues/141),
part of the map [LAN access to the dashboard with authentication](https://github.com/lbacik/agent-dashboard/issues/92).

**Question.** Which ways of authenticating operators fit this dashboard,
and how does each one hand an identity (and a group or role) to the app?
For each option: how the app can trust the identity, session lifetime and
sign-out, behaviour with the long-lived SSE stream and htmx requests,
arm64 support (Raspberry Pi 5), and the cost of adding or removing a
collaborator. No existing reverse proxy, VPN, identity provider (IdP) or
domain is assumed.

**Sources.** Read on 2026-11-02:

- Official docs and source code at these releases: Caddy v2.11.7, Traefik
  v3.7.17, oauth2-proxy v7.15.5, Authelia v4.39.28, Tailscale 1.104.4. Each
  claim links to the page or file it comes from.
- Specs: WHATWG HTML (server-sent events), WHATWG XMLHttpRequest, W3C
  WebAuthn Level 3, RFC 7617, and the OWASP cheat sheets.
- This repo at `c18be8c`. The installed Starlette 1.8.0 and uvicorn 0.54.0
  (versions pinned in `uv.lock`), and the htmx 2.0.4 and SSE extension
  bundled in `src/agent_dashboard/static/`.

Anything marked *inference* is reasoned from those sources rather than
stated in them. TLS and certificates are only covered as far as an option
requires them; the full picture is [#142](https://github.com/lbacik/agent-dashboard/issues/142).

## Answer in brief

- **Two ways an identity reaches the app.**
  - *Header-based*: a proxy or Tailscale authenticates the user, then adds
    request headers (`Remote-User`, `X-Forwarded-User`,
    `Tailscale-User-Login`, ...). The app has to make sure nobody else can
    reach it with those headers set.
  - *Built-in*: the app checks credentials itself and keeps its own session.
- **Trust for header-based options.** It rests on the app being reachable
  *only* through the proxy. Today's loopback bind gives exactly that on the
  host install, but there is a catch: every loopback client is
  indistinguishable from the proxy. That includes the SSH tunnel, which the
  map keeps as a supported path, and it can send forged identity headers.
  Two ways to tell the proxy apart:
  - a Unix socket between proxy and app (Caddy can use one; Tailscale Serve
    cannot, it proxies only to `http://127.0.0.1`);
  - a shared-secret header added by the proxy.
- **Groups.**
  - Authelia (`Remote-Groups`) and oauth2-proxy (`X-Forwarded-Groups`) carry
    groups.
  - Tailscale Serve carries none in its user headers, but it can forward ACL
    "app capabilities" as JSON.
  - Proxy basic auth carries only a user name.
  - Built-in auth owns the user-to-role mapping itself.
- **What some options need before they can run.**
  - **oauth2-proxy** always needs an upstream OAuth/OIDC provider: a
    client ID is mandatory, even with `--htpasswd-file`.
  - **Authelia** needs HTTPS and a cookie domain that contains a period and
    is not a special-use name like `localhost`.
  - **Tailscale Serve** needs a Tailscale account (a VPN plus a
    coordination service) and a client on every collaborator's device.
  - **Passkeys** need a secure context and an RP ID that is a domain name,
    never an IP address.
  - **Plain basic auth** and **password sessions** need nothing beyond TLS,
    which they need so the password is not sent in clear.
- **SSE.** No option re-checks authentication on a stream that is already
  open: forward auth runs once per request, and the dashboard's `/stream`
  generator runs until the client disconnects. When a reconnect is refused
  (401, 403, or a redirect to an HTML login page), the browser *fails* the
  `EventSource` connection for good. The bundled htmx SSE extension then
  retries on its own with backoff (0.5 s doubling to 64 s), forever, and the
  table stays stale. `EventSource` never exposes the status code.
- **htmx requests.** XHR follows redirects without telling the page. A
  redirect to a login portal on another origin turns into a CORS failure
  (`htmx:sendError`). A same-origin login page would be swapped into the
  fragment target. By default htmx 2 does not swap 4xx responses, so a bare
  401 is silent unless the app returns `HX-Redirect`/`HX-Refresh` or a
  handler acts on it.
  - Authelia answers 401 instead of 302 only when the request says
    `X-Requested-With: XMLHttpRequest` or does not accept `text/html`.
  - oauth2-proxy answers 401 only for `Accept: application/json` or
    configured API routes.
  - htmx sends neither header by default.
- **Changes every option forces on this app.**
  - `check_protection` compares `Origin` with `"http://" + Host`, so behind
    TLS every Control action would be refused.
  - The `Host` allow-list has to admit the new hostname.
  - uvicorn's on-by-default proxy-header handling rewrites `request.client`
    and the scheme for loopback peers.
- **arm64.** Every candidate ships linux/arm64 binaries or images, and every
  Python dependency that built-in auth would add ships aarch64 wheels or is
  pure Python. arm64 rules nothing out.
- **Adding or removing a collaborator.**
  - A file edit for Authelia (users file, re-read on each check by default),
    oauth2-proxy (`--authenticated-emails-file`, watched and re-checked on
    every request) and Traefik basic auth (`usersFile`).
  - A config edit plus reload for Caddy basic auth.
  - An invite, share or removal in the Tailscale admin console.
  - App-managed rows for built-in auth.
  - Revocation takes effect at once only where something is re-checked on
    each request. A signed-cookie session (Starlette's default) cannot be
    revoked, short of rotating the key for everyone, unless the app looks
    the user up on every request.

## 1. How the dashboard handles requests today

These are the points every option has to fit:

- **Bind and Host guard.** The process refuses a non-loopback bind unless
  `--allow-non-loopback` or `AGENT_DASHBOARD_ALLOW_NON_LOOPBACK` is set
  (`config.py:50-52`). `HostGuardMiddleware` returns 403 unless the `Host`
  name is `localhost`, `127.0.0.1` or on the allow-list (`app.py:162-196`).
  `AGENT_DASHBOARD_DISABLE_HOST_GUARD=1` skips the middleware
  (`app.py:269-273`).
- **Control action checks.** `check_protection` requires
  `Origin == "http://" + Host`, `Sec-Fetch-Site: same-origin` when present,
  and `HX-Request: true` (`control_routes.py:437-458`).
  - The scheme is **hard-coded `http://`**. With TLS in front, a browser
    sends `Origin: https://<host>`, so every Control action would be
    refused with reason `origin`.
  - Control actions also become unavailable under a non-loopback bind
    unless the Host guard is disabled (`control.py:104-125`, ADR 0008).
- **SSE.**
  - `GET /stream` is a `StreamingResponse`. It sends `retry: 3000`, then a
    `fleet` event after every poll cycle, and a `: ping` comment every 15 s
    (`app.py:117-158`, `:1442-1466`).
  - The generator runs until `request.is_disconnected()`; nothing in it
    re-checks anything per event.
  - The page connects with htmx's SSE extension (`index.html:14`), which
    creates `new EventSource(url, { withCredentials: true })` (`sse.js:76-78`).
- **htmx requests triggered by SSE.** The drawer, Control console and
  command cards re-issue `hx-get` on every `sse:fleet` event
  (`_drawer.html:33,58,114,172`, `_control.html:43`, `_control_card.html:12`).
  So while a drawer is open, an authenticated GET goes out at least once
  per poll cycle. That is a natural point to notice an expired session (see
  [section 5](#5-sse-and-htmx-behaviour-in-detail)).
- **htmx error swaps.** htmx 2.0.4's default `responseHandling` swaps 2xx
  and 3xx and does not swap 4xx or 5xx (bundled `htmx.min.js`).
  `control-error-swap.js` opts in only the Control routes' 403, 409, 422 and
  503 responses.
- **uvicorn.** It is started with `uvicorn.run(app, host, port, workers=1)`
  (`cli.py:21-27`). uvicorn defaults to `proxy_headers=True` with
  `forwarded_allow_ips` taken from `FORWARDED_ALLOW_IPS`, or `127.0.0.1,::1`
  if unset (`uvicorn/config.py:225-228`, `:361-365`, `:543-544`). For a
  loopback peer, `ProxyHeadersMiddleware` replaces `scope["client"]` with
  the `X-Forwarded-For` client and the scheme with `X-Forwarded-Proto`
  (`uvicorn/middleware/proxy_headers.py:24-60`). *Inference:* behind a
  same-host proxy, `request.client.host` no longer shows that the peer was
  loopback, and `request.url.scheme` becomes `https`.
- **Container path.** Compose publishes
  `${DASHBOARD_BIND:-127.0.0.1}:${DASHBOARD_PORT:-8000}:8000`, and the
  process inside binds `0.0.0.0` (`docker-compose.yml:16-17`, ADR 0002).
  *Inference:* a proxy in a sibling container reaches the app from a Docker
  network address, not loopback. uvicorn's default `forwarded_allow_ips`
  does not trust that address, and "peer is loopback" cannot be the trust
  rule there.

## 2. Authenticating reverse proxies

### 2.1 Caddy `forward_auth`

- **Mechanism.** Caddy sends a `GET` subrequest to an auth service with
  `X-Forwarded-Method` and `X-Forwarded-Uri` added. "If the upstream
  responds with a `2xx` status code, then access is granted"; otherwise "the
  upstream's response is copied back to the client", typically a redirect
  to the login page
  ([forward_auth docs](https://caddyserver.com/docs/caddyfile/directives/forward_auth)).
  Caddy authenticates nobody itself: it needs Authelia, oauth2-proxy or a
  similar service behind it.
- **Identity to the app.** `copy_headers` copies named headers from the
  auth response onto the proxied request, with optional renaming
  (`Tailscale-User>X-Webauth-User`).
- **Spoofing.**
  - Caddy 2.10.0 to 2.11.1 did **not** strip a client-supplied copy of a
    `copy_headers` header when the auth response lacked it. That is a
    high-severity advisory,
    [GHSA-7r4p-vjf4-gxv4](https://github.com/caddyserver/caddy/security/advisories/GHSA-7r4p-vjf4-gxv4),
    fixed in v2.11.2.
  - Since the fix, Caddy always deletes the client's header first
    ([`forwardauth/caddyfile.go:216-231`](https://github.com/caddyserver/caddy/blob/v2.11.7/modules/caddyhttp/reverseproxy/forwardauth/caddyfile.go#L216-L231)).
    So the minimum Caddy version is part of the trust story.
- **Forwarded headers.** `reverse_proxy` passes `Host` through unchanged.
  It sets `X-Forwarded-For`, `-Proto` and `-Host`, and ignores incoming
  `X-Forwarded-*` from clients that are not in `trusted_proxies`
  ([reverse_proxy docs](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)).
- **SSE through the proxy.** `text/event-stream` responses "are flushed
  immediately", whatever `flush_interval` says. `stream_timeout` (default:
  none) can force long streams closed. On a config reload Caddy closes
  WebSockets; `stream_close_delay` can postpone that (same page).
- **arm64.** The release ships `caddy_2.11.7_linux_arm64.tar.gz` and a
  `.deb`
  ([release assets](https://github.com/caddyserver/caddy/releases/tag/v2.11.7)).
  The official `caddy` image includes `arm64v8`
  ([Docker Hub](https://hub.docker.com/_/caddy)).

### 2.2 Traefik ForwardAuth

- **Mechanism.** "If the service answers with a 2XX code, access is
  granted ... Otherwise, the response from the authentication server is
  returned." `authSigninURL` can redirect on a 401
  ([ForwardAuth docs](https://doc.traefik.io/traefik/reference/routing-configuration/http/middlewares/forwardauth/)).
- **Identity to the app.** `authResponseHeaders` are copied onto the
  request. The code deletes the client's copy before setting the auth
  service's value, even when the auth response lacks the header
  ([`auth/forward.go:337-343`](https://github.com/traefik/traefik/blob/v3.7.17/pkg/middlewares/auth/forward.go#L337-L343)).
  A code comment says that alias spellings (`X_Auth_User`) rely on the
  entry point's `aliasHeadersStrategy`. *Inference:* that matters little
  here, because Starlette looks headers up by exact lower-cased name
  (`starlette/datastructures.py:552-557`), so `Remote_User` never matches
  `Remote-User`.
- **Forwarded headers.** `trustForwardHeader` is deprecated in favour of
  trusted IPs on the entry point (same docs page).
- **arm64.** The release ships `traefik_v3.7.17_linux_arm64.tar.gz`
  ([release](https://github.com/traefik/traefik/releases/tag/v3.7.17)), and
  the official image includes `arm64v8`
  ([Docker Hub](https://hub.docker.com/_/traefik)).

For both proxies, session lifetime, sign-out and how a collaborator is
added belong to whatever auth service sits behind them (2.3, 2.4).

### 2.3 oauth2-proxy

- **Prerequisite: an upstream IdP.** Configuration validation rejects a
  provider with no client ID (`provider missing setting: client-id`)
  ([`pkg/validation/providers.go:63-65`](https://github.com/oauth2-proxy/oauth2-proxy/blob/v7.15.5/pkg/validation/providers.go#L63-L65)),
  and `--provider` defaults to `google`
  ([config overview](https://oauth2-proxy.github.io/oauth2-proxy/configuration/overview)).
  `--htpasswd-file` only *additionally* authenticates local users, so it
  does not remove the need for a provider. With no IdP assumed, this means
  registering an OAuth app with a cloud provider (Google, GitHub, ...) or
  running one (Keycloak, Authelia's OIDC provider, ...).
- **Deployment.** It runs either as the reverse proxy itself (`--upstream`)
  or as an auth service behind Caddy or Traefik (`/oauth2/auth`).
- **Identity to the app.**
  - `--pass-user-headers` (default `true`) sends `X-Forwarded-User`,
    `X-Forwarded-Groups`, `X-Forwarded-Email` and
    `X-Forwarded-Preferred-Username` upstream.
  - `--set-xauthrequest` sets `X-Auth-Request-*` response headers for
    forward-auth use.
  - Groups come from the provider's claims, or from
    `--htpasswd-user-group` for htpasswd users (one group list shared by
    all of them).
  - `--trusted-proxy-ip` defaults to `0.0.0.0/0` (config overview).
- **Session.**
  - The default store is an encrypted cookie (`--session-store-type
    cookie`), with Redis as the alternative.
  - `--cookie-expire` defaults to `168h` (7 days); `0` makes it a browser
    session cookie. `--cookie-refresh` is optional and only some providers
    support it.
  - `--cookie-secure` defaults to `true`, so HTTPS is needed in practice.
  - `/oauth2/sign_out` clears the cookie and redirects
    ([`oauthproxy.go:771-800`](https://github.com/oauth2-proxy/oauth2-proxy/blob/v7.15.5/oauthproxy.go#L771-L800)).
    *Inference:* with the cookie store there is no server-side session to
    invalidate, so sign-out only affects the browser that signs out.
- **Revocation.** On every request the session's email is checked against
  the validator again (`oauthproxy.go:1176`). The
  `--authenticated-emails-file` is watched for changes
  ([`validator.go:24-40`](https://github.com/oauth2-proxy/oauth2-proxy/blob/v7.15.5/validator.go#L24-L40)).
  So deleting a line locks out an existing session on its next request.
- **htmx and SSE.** For an unauthenticated request, the proxy returns a JSON
  401 only if `--force-json-errors` is set, the `Accept` header contains
  exactly `application/json`, or the path is an `--api-route`. Otherwise it
  returns the sign-in page with status 403, or a redirect to the provider
  with `--skip-provider-button`
  ([`oauthproxy.go:1077-1093`](https://github.com/oauth2-proxy/oauth2-proxy/blob/v7.15.5/oauthproxy.go#L1077-L1093),
  `isAjax` at `:1340-1358`).
  - htmx sends XHR's default `Accept: */*`, so an htmx request gets the 403
    page, which htmx 2 does not swap, or a cross-origin redirect, which
    fails CORS.
  - `EventSource` sends `Accept: text/event-stream`, so it gets the same
    non-200 response and fails the connection
    ([section 5](#5-sse-and-htmx-behaviour-in-detail)).
  - `--flush-interval` defaults to `1s` for streamed responses (config
    overview). *Inference:* SSE events may arrive up to 1 s late unless
    this is lowered.
- **Collaborators.** Add or remove a line in the emails file (watched), or
  manage the user at the IdP.
- **arm64.** The release ships `oauth2-proxy-v7.15.5.linux-arm64.tar.gz`
  ([release](https://github.com/oauth2-proxy/oauth2-proxy/releases/tag/v7.15.5)).

### 2.4 Authelia

- **Prerequisites.**
  - "Authelia MUST be served via the https scheme. This is not optional
    even for testing." Protected apps must use `https`/`wss` as well
    ([get started](https://www.authelia.com/integration/prologue/get-started/)).
  - The session cookie domain must contain a period, must not be a public
    suffix, and must not be a special-use domain like `localhost`. It must
    be a hostname "a globally trusted CA would issue certificates for", and
    `authelia_url` must use `https://`
    ([session config](https://www.authelia.com/configuration/session/introduction/)).
    *Inference:* names like `pi.local` or `*.home.arpa` (special-use) and
    bare IPs are excluded, so a real (sub)domain is needed. The cookie is
    shared between the portal and the app, typically
    `auth.example.com` / `dash.example.com`.
  - Storage: SQLite is "recommended for testing and lite deployments". A
    notifier is required; local file delivery is allowed (get started).
- **Identity to the app.** On success, the forward-auth endpoint sets
  `Remote-User`, `Remote-Groups` (example `admin,dev`), `Remote-Name` and
  `Remote-Email`
  ([trusted header SSO](https://www.authelia.com/integration/trusted-header-sso/introduction/);
  names in [`handlers/const.go:32-35`](https://github.com/authelia/authelia/blob/v4.39.28/internal/handlers/const.go#L32-L35)).
  The docs warn that "your proxy must ensure only Authelia is setting these
  headers".
- **Authorization in the proxy.** Access-control rules can match subject
  (`user:`, `group:`), method and resource regex, first match wins, with a
  recommended `default_policy: deny`
  ([access control](https://www.authelia.com/configuration/security/access-control/)).
  *Inference:* "viewers may GET, only group X may POST to `/control/`" can
  be enforced at the proxy as well as in the app.
- **Session.** Defaults: `inactivity` 5 min, `expiration` 1 h,
  `remember_me` 1 month. Sessions are kept in memory by default, or in
  Redis (session config). *Inference:* in-memory sessions are lost when
  Authelia restarts.
- **Revocation.** The file backend refreshes user details on every check
  by default (`refresh_interval` default for the file provider: `always`)
  ([first factor](https://www.authelia.com/configuration/first-factor/introduction/)).
  `watch: true` reloads the users file when it changes; it defaults to
  `false` ([file backend](https://www.authelia.com/configuration/first-factor/file/)).
- **Passkeys and 2FA.** `enable_passkey_login` allows passkey sign-in,
  which "only counts as a single factor". WebAuthn is also available as a
  second factor
  ([WebAuthn config](https://www.authelia.com/configuration/second-factor/webauthn/)).
- **htmx and SSE.** For an unauthenticated request, Authelia answers **401**
  if the request is XHR (`X-Requested-With: XMLHttpRequest`) or does not
  accept `text/html`. Otherwise it answers **302** for GET/HEAD/OPTIONS and
  **303** for other methods
  ([`handler_authz_util.go:62-73`](https://github.com/authelia/authelia/blob/v4.39.28/internal/handlers/handler_authz_util.go#L62-L73)).
  `AcceptsMIME` counts a leading `*/*` as accepting HTML
  ([`authelia_context.go:620-640`](https://github.com/authelia/authelia/blob/v4.39.28/internal/middlewares/authelia_context.go#L620-L640)).
  So:
  - An htmx GET (`Accept: */*`, no `X-Requested-With`) gets a 302 to the
    portal on another origin. XHR follows it and fails CORS.
  - An htmx Control POST gets a 303 and fails the same way.
  - `EventSource` (`Accept: text/event-stream`) gets a 401 and fails the
    connection.
  - *Inference:* adding `X-Requested-With: XMLHttpRequest` to htmx requests
    (`hx-headers`) would turn the redirects into 401s that the page can act
    on.
- **Collaborators.** Edit the users YAML (username, display name, argon2
  hash from `authelia crypto hash generate`, email, groups). Authelia also
  writes this file back (file backend page).
- **arm64.** The release ships `authelia-v4.39.28-linux-arm64.tar.gz` and
  an arm64 `.deb`
  ([release](https://github.com/authelia/authelia/releases/tag/v4.39.28)),
  and the image includes `arm64`
  ([Docker Hub](https://hub.docker.com/r/authelia/authelia)).
- **Moving parts.** Running Authelia means a proxy, Authelia, its storage
  file, a notifier, and the secrets (session, JWT, and the required
  storage `encryption_key`,
  [storage config](https://www.authelia.com/configuration/storage/introduction/)).

### 2.5 Tailscale Serve identity headers

- **Prerequisites.**
  - Tailscale is a VPN: it brings a coordination service (a Tailscale
    account) and needs a client on the host and on every collaborator's
    device.
  - Serve "requires you to enable HTTPS certificates in your tailnet". The
    URL is `https://<device>.<tailnet>.ts.net`
    ([Serve KB](https://tailscale.com/kb/1312/serve)).
  - The Personal plan is free for "up to 6 users"; Standard is $8 per user
    per month ([pricing](https://tailscale.com/pricing)).
- **Proxy target.** "Only `http://127.0.0.1` is supported for proxies"
  ([`tailscale serve` reference](https://tailscale.com/docs/reference/tailscale-cli/serve)).
  With `--bg`, Serve resumes after a reboot. *Inference:* it fits the host
  install's loopback bind unchanged. In the container path it reaches the
  app through the loopback-published port, but it cannot reach a Unix
  socket.
- **Identity to the app.**
  - Serve sets `Tailscale-User-Login`, `Tailscale-User-Name` and
    `Tailscale-User-Profile-Pic`. Non-ASCII values may be RFC 2047
    Q-encoded.
  - The headers are absent for traffic from tagged devices and from Funnel.
  - They are present for "external users who have accepted a share" of the
    device.
  - Serve strips client-supplied copies.
  - Tailscale's own advice: have the backend listen only on localhost, or
    anyone who reaches it directly "could trivially provide their own
    values" (Serve KB).
  - **No groups** come in the user headers. With `--accept-app-caps`, Serve
    forwards `Tailscale-App-Capabilities` for capabilities granted to the
    caller in the tailnet policy (Serve KB, CLI reference). That is a way
    to carry a role defined in the ACL.
- **Session and sign-out.** There is no web session. *Inference:* identity
  is resolved per connection from the peer device, so "sign-out" means
  logging the device out of Tailscale or revoking its access.
- **Collaborators.** Invite them to the tailnet, or share only this device
  (single-use or reusable invite link). Revoke from the admin console:
  "After you revoke an invite, the recipient user can no longer access the
  shared machine". A shared machine gives the recipient "access to only the
  shared machine ... and nothing else", and ACLs can restrict ports further
  ([sharing KB](https://tailscale.com/kb/1084/sharing)).
- **SSE and htmx.** *Inference:* nothing redirects, because there is no
  login step. A revoked device loses the network path, so its stream
  breaks and its reconnects fail at the network level.
- **arm64.** The release ships `tailscale_1.104.4_arm64.tgz`
  ([pkgs.tailscale.com](https://pkgs.tailscale.com/stable/)).

### 2.6 Basic auth at the proxy

- **Caddy `basic_auth`.** Passwords are stored as hashes (`bcrypt` by
  default, or `argon2id`); "Caddy configuration does not accept plaintext
  passwords", and `caddy hash-password` generates the hash. After login,
  `{http.auth.user.id}` "contains the authenticated username", which the
  config can forward to the app with `header_up`
  ([basic_auth docs](https://caddyserver.com/docs/caddyfile/directives/basic_auth)).
- **Traefik `basicAuth`.** `users` or `usersFile` hold `name:hashed-password`
  entries, hashed with "MD5, SHA1, or BCrypt". `headerField` names a header
  that receives the user name; the client's copy is deleted first
  ([basicAuth docs](https://doc.traefik.io/traefik/reference/routing-configuration/http/middlewares/basicauth/),
  [`auth/basic_auth.go:107-113`](https://github.com/traefik/traefik/blob/v3.7.17/pkg/middlewares/auth/basic_auth.go#L107-L113)).
  `removeHeader` strips `Authorization` before it reaches the app.
- **Groups.** None. Roles would be a mapping from user name in the app's
  configuration.
- **Session and sign-out.**
  - The browser may resend credentials preemptively within the protection
    space ([RFC 7617 §2.2](https://www.rfc-editor.org/rfc/rfc7617.html#section-2.2)).
  - RFC 7617 defines no sign-out. *Inference:* a user stays signed in until
    the browser drops its cached credentials, usually when it closes.
  - The password crosses the network in clear unless TLS is used (§4).
- **SSE and htmx.** *Inference:* with credentials cached, `EventSource`
  (which includes credentials for same-origin requests) and XHR send them
  automatically. Credentials never expire, so nothing breaks mid-stream.
  A removed user gets 401 on the next request or reconnect.
- **Collaborators.** Caddy: edit the config and reload. Traefik: edit
  `usersFile`; the docs do not say whether a change is picked up without a
  reload.

## 3. Built-in auth in FastAPI

All three variants run in the dashboard process, so they are the same for
the host install and the container. They add no daemon, but the app takes
on credential storage, a sign-in page and session handling.

### 3.1 Password with sessions

- **Signed-cookie sessions (Starlette `SessionMiddleware`).**
  - Session data is JSON, base64-encoded and signed with
    `itsdangerous.TimestampSigner`. It is readable by the client but not
    forgeable.
  - Defaults: `max_age` 14 days, `same_site="lax"`, `https_only=False`
    (opt in to `Secure`), and the cookie is always `httponly`
    (`starlette/middleware/sessions.py:16-40`).
  - The timestamp is checked against `max_age` when the cookie is read, and
    the cookie is re-issued only when the session is modified (`:42-80`).
  - *Inference:* there is no server-side state. Sign-out clears the cookie
    in one browser only, and a stolen or old cookie stays valid until
    `max_age`. Rotating `secret_key` logs everyone out. Removing a user
    takes effect at once only if the app looks the user up on every
    request.
  - `itsdangerous` is pure Python and not installed today; Starlette
    imports it only for this middleware.
- **Server-side sessions** (an opaque session ID mapped to a stored
  record, for example in SQLite next to the Control audit) give real
  sign-out and immediate revocation. OWASP: logout "must invalidate the
  session at least on server side"; the session ID must be regenerated
  after login; the cookie should be `Secure`, `HttpOnly` and `SameSite`
  (`Strict` preferred, or `Lax`). Idle timeouts of 15-30 minutes suit
  low-risk apps, and 4-8 hours is the absolute timeout suggested for
  full-day office use
  ([OWASP Session Management](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html)).
  Starlette and FastAPI ship no server-side session store, so the app
  would own this code.
- **Password storage.** "Use Argon2id with a minimum configuration of
  19 MiB of memory, an iteration count of 2, and 1 degree of parallelism";
  bcrypt only for legacy, with work factor 10 or more and a 72-byte limit
  ([OWASP Password Storage](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html)).
  `argon2-cffi-bindings` 26.1.0 and `bcrypt` 5.0.0 publish aarch64 wheels
  for glibc and musl ([PyPI](https://pypi.org/project/argon2-cffi-bindings/)).
- **Identity and roles.** The app maps users to roles itself; nothing comes
  from outside. The identity is trusted because the app verified it, so no
  header or peer check is needed.
- **SSE and htmx.** The app can do what the proxies cannot:
  - re-check the session (expiry or revocation) per SSE event or ping and
    end the stream;
  - answer an expired htmx request with 401 plus `HX-Redirect: /login`
    (or `HX-Refresh: true`).
  - [Section 5](#5-sse-and-htmx-behaviour-in-detail) covers what the client
    then does.
  - The login page is same-origin, so a plain 302 on an htmx request would
    be followed by XHR and the login form swapped into a fragment.
- **TLS.** `Secure` cookies need HTTPS. Without TLS, the password and the
  session cookie cross the LAN in clear (RFC 7617 §4 makes the same point
  for basic auth).
- **Collaborators.** Add or remove a row in whatever store the app keeps.
  The app also needs a way to set the first password (CLI, invite token,
  ...); that is a design question.

### 3.2 Passkeys (WebAuthn)

- **Secure context.** `PublicKeyCredential` is exposed only in secure
  contexts (`[SecureContext, Exposed=Window]`), meaning HTTPS or
  `localhost`.
- **No IP addresses.** "If effective domain is not a valid domain, then
  throw a 'SecurityError' DOMException", with the note "Only the domain
  format of host is allowed here", which rules out IPv4 and IPv6. The RP ID
  is "a valid domain string" and must equal, or be a registrable domain
  suffix of, the origin's effective domain
  ([WebAuthn L3](https://www.w3.org/TR/webauthn-3/)).
  - *Inference:* LAN access needs a hostname, not a bare IP, plus HTTPS.
  - Whether browsers accept an mDNS `.local` name as RP ID was not
    verified.
- **Library.** `webauthn` (py_webauthn) 3.0.1 is pure Python but depends on
  `cryptography`, `cbor2`, `pyOpenSSL` and `pyasn1`. `cryptography` 50.0.2
  and `cbor2` publish aarch64 wheels
  ([PyPI](https://pypi.org/project/webauthn/3.0.1/)).
- **Session and identity.** A passkey replaces the password check only.
  Sessions, roles and the SSE/htmx behaviour are the same as in 3.1.
- **Collaborators.** Adding one needs an enrolment step: a one-time invite,
  or registration while signed in another way. Losing every device that
  holds the passkey needs a recovery path. Removing a collaborator means
  deleting their credential rows.
- **Elsewhere.** Authelia offers passkey login without the app writing any
  WebAuthn code (2.4).

### 3.3 HTTP basic auth in the app

- **How it works.** FastAPI's `HTTPBasic` dependency returns the
  credentials, and the docs show comparing them with
  `secrets.compare_digest` to avoid timing leaks. A 401 with
  `WWW-Authenticate: Basic` makes the browser show its login prompt
  ([FastAPI HTTP Basic](https://fastapi.tiangolo.com/advanced/security/http-basic-auth/)).
- **Same properties as proxy basic auth (2.6).** No sign-out, credentials
  cached by the browser, cleartext without TLS, no groups.
- **Cost.** *Inference:* the app has to verify a hash on *every* request,
  because there is no session. With argon2id at OWASP's minimum (19 MiB),
  that cost is paid on every htmx GET and every SSE reconnect, unless the
  app caches successful checks.
- **Collaborators.** App configuration.

## 4. Trusting a header-based identity in this app

Every proxy option above ends with "the app believes a request header".
The app has to rule out any other way to reach it with that header set.

| Mechanism | Host install | Container install | Notes |
|---|---|---|---|
| **Loopback bind; trust headers from any peer** | Works with today's bind refusal (`config.py:50-52`) | Does not apply: the app binds `0.0.0.0` in its container | Every loopback client is trusted, *including an SSH tunnel* (`ssh -L` arrives from `127.0.0.1`) and any local process. Tailscale's own advice is this model (Serve KB) |
| **Unix socket between proxy and app** | Caddy upstreams can be `unix//path` (reverse_proxy docs); uvicorn can listen on a Unix socket (`uds`, `uvicorn/config.py:201`) | Shared volume between containers | File permissions decide who can connect. Not possible with Tailscale Serve (`http://127.0.0.1` only). Then the SSH tunnel would have to forward to the socket or to a separate loopback listener |
| **Shared-secret header set by the proxy** | Caddy `header_up`, Traefik headers middleware, Authelia/oauth2-proxy behind either | Same | The app rejects identity headers without the secret. One more secret to rotate. Tailscale Serve cannot add custom headers (not in its docs) |
| **mTLS proxy to app** | Possible (uvicorn TLS with client-cert verification; Caddy upstream TLS client auth) | Same | Heaviest: needs a private CA |

Points that bear on this table:

- **The SSH tunnel.** The map keeps "loopback + SSH tunnel" as a supported
  path. With header-based identity, a tunnel request arrives on loopback
  with no identity headers, or with headers its user typed. The app has to
  decide what a header-less loopback request means, either the operator as
  today or refused, and a pure loopback trust rule cannot tell a tunnel
  from the proxy.
- **Who strips client copies.** Caddy ≥ 2.11.2 `copy_headers`, Traefik
  `authResponseHeaders` and `headerField`, and Tailscale Serve all delete
  a client-supplied copy before setting their own. Caddy 2.10.0-2.11.1 did
  not.
- **uvicorn.** Proxy-header handling is on by default and trusts loopback
  (section 1). *Inference:* the app should not use `request.client.host`
  after that middleware to decide "came through the proxy". It could pin
  `forwarded_allow_ips`, or decide trust from the listener instead.

## 5. SSE and htmx behaviour in detail

Facts from the specs and the bundled code:

- **The `EventSource` request** carries `Accept: text/event-stream`. Its
  CORS mode is "Anonymous", which means credentials are sent for
  same-origin requests; `withCredentials: true` sets the credentials mode
  to `include`
  ([HTML §9.2](https://html.spec.whatwg.org/multipage/server-sent-events.html)).
  The bundled extension always passes `withCredentials: true`
  (`sse.js:76-78`).
- **Refusal fails the connection.** "If res's status is not 200, or if
  res's `Content-Type` is not `text/event-stream`, then fail the
  connection", and "once the user agent has failed the connection, it does
  not attempt to reconnect". 301 and 307 redirects are followed (HTML
  §9.2). The page sees only a plain `error` event, with no status code.
- **A clean end reconnects.** If the server ends the response body cleanly,
  the browser waits the `retry` time (3 s here) and reconnects (HTML §9.2).
- **The htmx SSE extension retries anyway.** When the source is `CLOSED`,
  it retries with a delay of `500 ms × min(2·n, 128)`, which reaches 64 s,
  and it never gives up while the element exists (`sse.js:196-216`).
  *Inference:* after a refused reconnect the table keeps showing its last
  render and the extension keeps knocking. Nothing tells the user to sign
  in unless other code reacts to `htmx:sseError`.
- **XHR** follows redirects without telling the page, and sends
  `Accept: */*` when the author set none
  ([XHR standard, `send()`](https://xhr.spec.whatwg.org/#the-send()-method)).
  htmx 2.0.4 sets no `Accept` and no `X-Requested-With`. Its request
  headers are `HX-Request`, `HX-Current-URL`, `HX-Target`, `HX-Trigger`,
  `HX-Trigger-Name`, `HX-Boosted`, `HX-Prompt` and
  `HX-History-Restore-Request` ([htmx reference](https://htmx.org/reference/)).
- **htmx response headers.** htmx reads `HX-Redirect` (full navigation) and
  `HX-Refresh: true` (reload) *before* it decides whether to swap. A 401
  carrying `HX-Redirect` therefore still navigates, even though 4xx is not
  swapped (bundled `htmx.min.js`; htmx reference). `selfRequestsOnly`
  defaults to `true`.

What happens when a session ends, by option:

| Option | Open SSE stream | Reconnect / htmx GET | htmx Control POST |
|---|---|---|---|
| Caddy/Traefik + Authelia | Keeps flowing (auth only at request start) | SSE: 401 → fail → extension backoff. htmx GET: 302 to portal on another origin → CORS error (`htmx:sendError`) | 303 to portal → CORS error |
| oauth2-proxy | Keeps flowing | SSE: 403 sign-in page → fail. htmx GET: 403 page, not swapped (or 302 to IdP → CORS error with `--skip-provider-button`) | Same as GET |
| Tailscale Serve | No session; a revoked device loses the network path | Network error | Network error |
| Proxy / app basic auth | No expiry; a removed user is refused on the next request | 401; the browser may prompt again | 401 |
| Built-in sessions | The app can close it on expiry or revocation | The app answers 401 + `HX-Redirect`/`HX-Refresh` to htmx; SSE reconnect gets 401 → fail | Same; the Control audit can record the refusal |

*Inference:* for any option, the page needs some code to turn
"stream refused" or "fragment refused" into a visible sign-in prompt,
because `EventSource` cannot see the status and htmx 2 does not swap 4xx.
With built-in auth, the `sse:fleet`-triggered fragment GETs (section 1)
pick up an `HX-Redirect` within one poll cycle while a drawer is open.
With a proxy, the app never sees the expired request at all, so the
reaction has to be client-side or rely on the proxy answering 401 (the
`X-Requested-With` or `Accept: application/json` trick).

## 6. Trade-offs

| | Extra components | Needs before it can run | Identity / groups to the app | Sign-out | Add / remove collaborator | Works the same for both install paths |
|---|---|---|---|---|---|---|
| Caddy/Traefik + Authelia | Proxy + Authelia (+ SQLite, notifier, 3 secrets) | HTTPS; a real domain with a period, not special-use | Headers: user, groups, name, email | Portal logout; 5 min idle / 1 h absolute by default | Edit users YAML; re-read per check | Yes, with a proxy in each; trust needs network isolation or a secret in the container path |
| Caddy/Traefik + oauth2-proxy | Proxy (optional) + oauth2-proxy + an IdP | An OAuth/OIDC provider registration; HTTPS for `Secure` cookies | Headers: user, email, groups (from IdP) | `/oauth2/sign_out` clears that browser's cookie; 7-day default cookie | Edit emails file (watched, re-checked every request) or the IdP | Yes, as above |
| Tailscale Serve | `tailscaled` on host and on every client | Tailscale account; tailnet HTTPS certs | Headers: login, display name; roles only via app capabilities | Log the device out or revoke the share | Admin-console invite/share/revoke; client install for the collaborator | Host: as-is. Container: via the loopback-published port |
| Proxy basic auth | Caddy or Traefik | TLS (cleartext otherwise) | User name only (`header_up` / `headerField`) | None (browser cache) | Caddy: config + reload. Traefik: `usersFile` | Yes |
| Built-in password sessions | None | TLS for `Secure` cookies | App's own user → role map | Real sign-out only with server-side sessions | App-managed; first-password bootstrap needed | Yes, identical |
| Built-in passkeys | None (+ `webauthn` deps) | HTTPS **and** a hostname (no IP) | App's own map | As built-in sessions | Enrolment + recovery flow | Yes, identical |
| App basic auth | None | TLS | App's own map | None | App config | Yes, identical |

What the facts force, without picking a winner:

1. **No IdP and no domain assumed.** oauth2-proxy brings in an IdP, Authelia
   a domain, and Tailscale a VPN account. Each of these options adds that
   prerequisite to the install.
2. **TLS is required by every option that keeps a password or session off
   the wire.** Basic auth and passwords are cleartext without it; Authelia,
   Tailscale Serve and passkeys refuse to run without HTTPS. That makes
   [#142](https://github.com/lbacik/agent-dashboard/issues/142) a
   dependency of every option.
3. **`check_protection`'s `http://` assumption must change whatever is
   chosen**, as soon as TLS is in front (section 1).
4. **Header-based options conflict with the SSH-tunnel path** unless the
   proxy-to-app hop is told apart from other loopback clients: a Unix
   socket, a shared secret, or mTLS (section 4).
5. **Only built-in auth can end an open SSE stream on expiry or revocation**
   and answer htmx with `HX-Redirect`. With a proxy, the app sees nothing
   once a session ends.
6. **Revocation is immediate** for oauth2-proxy with an emails file,
   Authelia with the file backend, Tailscale, and built-in server-side
   sessions. It is **not** immediate for signed-cookie sessions or for any
   proxy's already-open SSE stream.
7. **arm64 rules nothing out.**
