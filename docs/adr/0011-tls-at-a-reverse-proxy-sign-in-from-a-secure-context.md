# TLS only at a reverse proxy; sign-in only from a secure context

In LAN mode (ADR 0010) passwords and sign-in session cookies (ADR 0009) cross
a network that also carries untrusted devices. Over plain HTTP any of them can
read the password and the cookie, or inject script that runs as the dashboard
origin and passes every ADR 0007 check. A browser treats plain HTTP on a LAN
or VPN address as an insecure context: it sends no `Sec-Fetch-Site` and
accepts no `Secure` or `__Host-` cookie. Only an `https://` URL changes that.
A VPN encrypts the wire but leaves the URL insecure.

- **The dashboard speaks plain HTTP only.** It has no certificate options and
  never terminates TLS. HTTPS is the job of a reverse proxy in front of it.
  The proxy also obtains the certificate (any source) and renews it, and it
  owns HSTS and any HTTP-to-HTTPS redirect. The dashboard sends neither:
  HSTS covers every port of a host and would break other plain-HTTP services
  on it.
- **In LAN mode, sign-in only from a secure context.** `POST /login` accepts a
  password only when its `Origin` uses `https` or names a loopback host
  (`localhost`, `127.0.0.0/8`, `::1`, which is the SSH tunnel). These are the
  browser's own "potentially trustworthy" rules. Anything else is refused,
  so a browser that reaches the dashboard's port directly over `http://` on a
  LAN address cannot sign in. The sign-in page checks
  `window.isSecureContext` and, when it is false, disables the form and says
  that sign-in requires HTTPS, so the password is never sent.
- **Cookie attributes follow the sign-in `Origin`.** A sign-in whose `Origin`
  uses `https` gets a `__Host-` cookie with `Secure`. A loopback `http`
  sign-in gets a cookie without the prefix and without `Secure`, because
  Safari refuses `Secure` cookies on `http://localhost`. Reading a sign-in
  session tries the `__Host-` cookie first. `X-Forwarded-Proto` stays unread
  (ADR 0010).
- **What the proxy must do.**
  - Pass the browser's `Host` header including the port the browser used, so
    that the ADR 0010 `Origin` check (host and port) holds. Default nginx
    (`Host $proxy_host`) and nginx-proxy published on a host port other than
    443 do not do this.
  - Be the only way in from the LAN: the dashboard binds to loopback or to a
    Docker network the LAN cannot reach.
  - Serve a certificate that operator devices trust without a warning.
    A self-signed certificate that people click through is not supported: it
    trains them to accept the certificate an attacker on the LAN would
    present.
  - Not set `Referrer-Policy: no-referrer`, which would blank `Origin` on a
    plain form POST.
- A request refused by the `Origin` check writes one stdout line with the
  `Origin` and `Host` it compared, so a proxy that rewrites `Host` shows up at
  once.

## Considered Options

- **TLS in the dashboard process (uvicorn `ssl_certfile`/`ssl_keyfile`)**:
  rejected. uvicorn loads the certificate once, so every renewal needs a
  restart or a reload hook, and HSTS, redirects and renewal would have to be
  rebuilt next to it. The tested deployment already runs nginx-proxy with
  acme-companion, which does all of that.
- **Plain HTTP sign-in in LAN mode, optionally behind an extra opt-in**:
  rejected. It sends passwords and session cookies in clear text on a network
  with untrusted devices, and adds a second switch to LAN mode. A trusted
  test LAN without sign-in still has the read-only ADR 0002 deployment
  outside LAN mode.
- **Refusing to start in LAN mode without certificate files**: rejected. The
  dashboard cannot tell whether a TLS-terminating proxy is in front of it.
- **An explicit setting for the public scheme, or `X-Forwarded-Proto` from
  `AGENT_DASHBOARD_TRUSTED_PROXIES`**: rejected. `Origin` already carries the
  browser's scheme on every sign-in, without trusting a proxy header or
  adding configuration.
- **Always `Secure` in LAN mode**: rejected. Safari would drop the cookie on
  the SSH tunnel, which ADR 0009 keeps as a way in.
- **Comparing only the host name in the `Origin` check, so a proxy on a
  non-standard port works**: rejected. A page served from another port on the
  same host is same-site, gets the `SameSite=Strict` cookie, and could fire
  Control actions. The port is the only thing separating them.
- **Step-by-step proxy recipes (Caddy, `tailscale serve`, nginx) in the
  README**: rejected. The README states the proxy requirements; the tested
  setup is nginx-proxy.

## Consequences

- The README names the certificate sources only as examples: Let's Encrypt
  DNS-01 with an owned domain, a Tailscale `ts.net` name, or a private CA
  whose root every device installs. Choosing and installing one is the
  operator's job.
- nginx-proxy reaches only containers on its Docker network, so the tested
  setup assumes the container install. Whether that becomes a supported LAN
  path is decided separately.
- `AGENT_DASHBOARD_TRUSTED_PROXIES` must match the proxy's address as the
  dashboard sees it, which is a Docker network address for nginx-proxy.

Decided in [Decide TLS termination and certificate source on the host](https://github.com/lbacik/agent-dashboard/issues/146),
building on [Research TLS on a private LAN](https://github.com/lbacik/agent-dashboard/issues/142),
[ADR 0009](0009-built-in-password-auth-in-lan-mode.md) and
[ADR 0010](0010-lan-mode-bind-and-host-check.md).
