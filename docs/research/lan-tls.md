# TLS on a private LAN: certificate sources and browser requirements

Research for [#142](https://github.com/lbacik/agent-dashboard/issues/142),
part of the map [LAN access to the dashboard with authentication](https://github.com/lbacik/agent-dashboard/issues/92).

**Question.** What are the realistic certificate sources for serving the
dashboard over HTTPS on a private LAN (self-signed, a private CA such as
mkcert or step-ca, Let's Encrypt via DNS-01 with an owned domain, Tailscale
certificates), and how does each handle trust distribution to laptops and
phones, renewal and arm64? Which browser facts does the design depend on:
when browsers send `Origin` and `Sec-Fetch-Site` (ADR 0007 relies on them),
what plain HTTP on a LAN address loses (secure context, `Secure` cookies,
`SameSite`, WebAuthn), and does a VPN address change any of it?

**Sources.** Read on 2026-11-02. Specs: WHATWG Fetch, HTML, XHR and URL
(living standards); W3C Secure Contexts, Fetch Metadata Request Headers
and Clipboard API (editor's drafts); WebAuthn Level 3 (W3C TR);
[draft-ietf-httpbis-rfc6265bis-22](https://www.ietf.org/archive/id/draft-ietf-httpbis-rfc6265bis-22.html)
(2025-12-01, approved, awaiting RFC number); WICG Local Network Access
(Draft CG Report, 2026-08-07); CA/Browser Forum Baseline Requirements
(`main`). Vendor docs: mkcert README and source (v1.4.4), smallstep step-ca
docs (v0.30.2), Let's Encrypt docs and blog, Tailscale KB, Caddy docs,
Apple and Android platform docs, Chrome Root Store FAQ, uvicorn docs and
source (0.54.0, the version in `uv.lock`), the Public Suffix List and the
dnsmasq man page. Code citations are `file:line` into this repository at
`c18be8c` (`main`). Statements marked *inference* are reasoning from the
cited facts, not a quoted rule. MDN was not used.

## Answer in brief

- **Plain HTTP on a LAN or VPN address is not a secure context.** An origin
  is potentially trustworthy only for `https`/`wss`, loopback
  (`127.0.0.0/8`, `::1`, `localhost`) or explicit user configuration.
  `http://192.168.x.y`, `http://pi.lan`, `http://100.x.y.z` and
  `http://pi.<tailnet>.ts.net` all fail. Today's SSH tunnel
  (`http://localhost:8000`) passes.
- **`Origin` does not depend on TLS.** htmx sends its POSTs with
  `XMLHttpRequest`, which uses request mode `cors`, so Fetch adds
  `Origin: <scheme>://<host>[:port]` to every non-GET request whatever the
  referrer policy. Under HTTPS the value starts with `https://`, and the
  dashboard's check hardcodes `http://` (`control_routes.py:448`). Every
  Control action over HTTPS would be refused as "missing or mismatched
  Origin header" until that changes.
- **`Sec-Fetch-Site` depends on TLS.** Fetch Metadata headers are set only
  when the request URL is potentially trustworthy. Over plain HTTP to a LAN
  or VPN address the browser omits them, so ADR 0007's "when present" rule
  becomes a no-op there. Over the tunnel or over HTTPS they are sent.
- **What plain HTTP loses:** secure-context-only APIs (WebAuthn,
  `navigator.clipboard`, ...); the `Secure` cookie attribute and with it
  `__Secure-`, `__Host-` and `SameSite=None`; and any confidentiality or
  integrity against other devices on the LAN. `SameSite=Lax/Strict` still
  works over HTTP. Cookies are never isolated by port.
- **WebAuthn needs three things together:** a secure context, TLS
  "established without errors", and a domain name (IP addresses are
  rejected as the RP ID). A clicked-through self-signed certificate or an
  `https://192.168.x.y` URL cannot use passkeys.
- **A VPN changes the wire, not the browser.** Tailscale encrypts traffic
  with WireGuard, but `http://100.x.y.z` and `http://<name>.ts.net` are
  still not trustworthy origins. Only an HTTPS URL changes that. For Local
  Network Access, `100.64.0.0/10` is "local", like RFC 1918.
- **Certificate sources:**
  - *Self-signed*: either every device installs it or users click through
    warnings, which loses WebAuthn and trains users to accept the
    certificate a LAN attacker would present.
  - *mkcert*: a dev-oriented private CA; leaf certificates last 825 days;
    no renewal tooling; manual root install on phones.
  - *step-ca*: an online private CA with ACME; 24 h certificates by
    default with daemon or systemd-timer renewal; manual root install on
    phones.
  - *Let's Encrypt DNS-01*: publicly trusted, so nothing to install on
    devices. It needs an owned domain, DNS API credentials on the host,
    name resolution to a private IP, and renewal every 64 then 45 days.
    Names are published in CT logs.
  - *Tailscale*: a publicly trusted Let's Encrypt certificate for
    `<machine>.<tailnet>.ts.net` only, valid 90 days. Renewal is manual with
    `tailscale cert` and automatic with `tailscale serve`. Only tailnet
    devices can use it. Names are published in CT logs.
  - All have arm64 builds.

## 1. What the dashboard does today

- **Server.** `cli.py:21-27` calls `uvicorn.run(app, host, port, workers=1,
  loop="asyncio")` with no TLS arguments, so the dashboard speaks plain
  HTTP/1.1.
- **Host guard.** `HostGuardMiddleware` (`app.py:185-196`) returns 403
  unless the `Host` hostname is in `{"localhost", "127.0.0.1"}` plus
  `AGENT_DASHBOARD_ALLOWED_HOSTS` (`app.py:115`, `:161-182`). It is skipped
  entirely when `AGENT_DASHBOARD_DISABLE_HOST_GUARD=1` (`app.py:268-273`,
  ADR 0008).
- **Control action checks.** `check_protection` (`control_routes.py:436-458`)
  runs these checks in order:
  1. `Origin` present and equal to `"http://" + Host` (line 448; the scheme
     is a literal);
  2. `Sec-Fetch-Site: same-origin`, but only if the header is present
     (lines 450-452);
  3. `HX-Request: true` (line 453);
  4. control mode available.
- **Cookies.** The only cookies are the UI range preferences
  `attempt_range` and `fleet_range` (`range_stats.py:56`, `:60`). They are
  set with `SameSite=Lax`, `Path=/` and no `Secure` (`app.py:385-391`,
  `:608-613`). There is no session cookie yet.
- **Secure-context API in use.** `copyNextAction` uses
  `navigator.clipboard.writeText` and falls back to
  `document.execCommand("copy")` when it is absent (`static/drawer.js:179-192`).
- **Transport.** Live updates come over SSE (`GET /stream`, `app.py:1442`;
  `templates/index.html:13-14`). Writes are htmx POSTs, which htmx sends
  with `XMLHttpRequest`.

## 2. Browser facts

### 2.1 Potentially trustworthy origins and secure contexts

[Secure Contexts §3.1 "Is origin potentially trustworthy?"](https://w3c.github.io/webappsec-secure-contexts/#is-origin-trustworthy)
returns "Potentially Trustworthy" when any of these holds:

- the scheme is `https` or `wss`;
- the host is in `127.0.0.0/8` or `::1/128`;
- the host is `localhost` or ends in `.localhost`, under the
  [localhost name resolution rules](https://w3c.github.io/webappsec-secure-contexts/#localhost)
  (§5.2);
- the scheme is `file`, or is one "the user agent considers to be
  authenticated";
- the origin "has been configured as a trustworthy origin".

Otherwise it returns "Not Trustworthy". §7.2 lets user agents "allow users
to configure specific sets of origins as trustworthy" for staging servers.
This is a per-browser developer setting, not something to roll out to
collaborators' phones.

[HTML §8.1.3.5](https://html.spec.whatwg.org/multipage/webappapis.html#secure-contexts)
makes an environment a secure context iff its top-level creation URL is
potentially trustworthy. Applied to the URLs an operator might open:

| URL | Potentially trustworthy? | Rule |
|---|---|---|
| `http://localhost:8000` (SSH tunnel, today) | yes | loopback name |
| `http://127.0.0.1:8000` | yes | `127.0.0.0/8` |
| `http://192.168.1.20:8000` (LAN IP) | **no** | none matches |
| `http://pi.lan:8000`, `http://pi.local:8000` | **no** | none matches |
| `http://100.101.102.103:8000` (Tailscale IP) | **no** | none matches |
| `http://pi.tail1234.ts.net:8000` (MagicDNS) | **no** | none matches |
| `https://` + any of the above | yes | scheme `https` |

The test is purely on the URL. A page served over `https` with a
certificate error the user clicked through still has an `https` origin.
WebAuthn adds its own "without errors" condition (2.5).

### 2.2 When the browser sends `Origin`

[Fetch §3.2 "append a request `Origin` header"](https://fetch.spec.whatwg.org/#origin-header)
works like this:

- If the request's response tainting is `cors`, or the mode is
  `websocket`/`webtransport`, `Origin` is always added.
- Otherwise, if the method is neither `GET` nor `HEAD`, `Origin` is added,
  but "if request's mode is not `cors`" the referrer policy can replace it
  with `null`:
  - `no-referrer` always does;
  - `strict-origin(-when-cross-origin)` and `no-referrer-when-downgrade` do
    so only on an https-to-http downgrade;
  - `same-origin` does so only for cross-origin targets.

[XHR §3.5.6 `send()`](https://xhr.spec.whatwg.org/#the-send()-method)
creates every request with mode `cors`. For an htmx POST to its own origin
that means:

- `Origin` is always sent with the real serialized origin, over HTTP and
  HTTPS alike, whatever `Referrer-Policy` the dashboard might set;
- the value is `http://192.168.1.20:8000` over plain HTTP and
  `https://pi.example.net` over HTTPS (the default port is omitted).

A plain HTML form POST (mode `navigate`) would go through the
referrer-policy switch, and `Referrer-Policy: no-referrer` would turn its
`Origin` into `null`. The dashboard sends no such forms for Control actions
today.

**Consequence for this code.** `check_protection` builds the expected value
as `"http://" + Host` (`control_routes.py:448`). With any certificate source
in section 3, the browser sends `Origin: https://...`, and every Control
action is refused with reason `origin`. Behind a TLS-terminating proxy the
comparison also depends on the proxy forwarding the original `Host` (4.2).

### 2.3 When the browser sends `Sec-Fetch-Site`

[Fetch Metadata §3](https://w3c.github.io/webappsec-fetch-metadata/#fetch-integration)
sets the metadata headers with this first step: "If r's current URL is not
an potentially trustworthy URL, return." Each setter also asserts that the
URL is potentially trustworthy. For values,
[§2.3](https://w3c.github.io/webappsec-fetch-metadata/#sec-fetch-site-header)
defaults to `same-origin`, uses `none` for user-initiated navigations, and
otherwise gives `cross-site` or `same-site` when any URL in the redirect
chain is not same-origin.

**Consequence for this code.**

- Over the SSH tunnel and over HTTPS, a modern browser sends
  `Sec-Fetch-Site`, and the ADR 0007 check is active.
- Over plain HTTP to a LAN or VPN address the browser never sends it.
  `check_protection` treats a missing header as a pass (line 451), so the
  check silently does nothing there, which is what ADR 0008's test
  deployment runs today.
- `Origin` and `HX-Request` still apply. The `HX-Request` custom header
  forces a CORS preflight cross-origin regardless of scheme.

*Inference:* on plain HTTP, all three ADR 0007 checks separate *initiators
inside one browser* (a foreign tab versus the dashboard's own page). None of
them helps against another device on the LAN that can read or modify the
HTTP traffic. That device can inject script into a dashboard response, and
the script runs as the dashboard origin, so it passes every check.

### 2.4 What plain HTTP on a LAN address loses

- **Secure-context-only APIs.** Web IDL `[SecureContext]` members are
  absent. Two that matter here:
  - WebAuthn: `[SecureContext, Exposed=Window] interface
    PublicKeyCredential`
    ([WebAuthn L3 §5.1](https://www.w3.org/TR/webauthn-3/#iface-pkcredential));
  - `navigator.clipboard`: `[SecureContext, SameObject]`
    ([Clipboard API §7.1](https://w3c.github.io/clipboard-apis/#navigator-interface)).
    The dashboard's copy button already falls back to `execCommand`
    (`drawer.js:183-191`).
- **`Secure` cookies.** RFC 6265bis-22 §5.7 step 13 ignores a cookie
  "entirely" when its secure-only flag is set and the request URI is not a
  "secure" protocol. "Secure" is "defined by the user agent", "typically
  HTTP over TLS" (§4.1.2.5). Step 16 also stops a non-secure origin from
  overwriting an existing `Secure` cookie. An HTTP-only deployment
  therefore cannot keep a session cookie that the browser withholds from
  plain-text requests.
- **Cookie prefixes.** `__Secure-` requires `Secure`. `__Host-` requires
  `Secure`, `Path=/` and no `Domain` (§4.1.3.1-4.1.3.2). Neither can be set
  over plain HTTP.
- **`SameSite=None`.** Step 19 ignores such a cookie "unless the cookie's
  secure-only-flag is true".
- **`SameSite=Lax`/`Strict` still work over HTTP.** A request is same-site
  when its URL's origin is same-site with the client's "site for cookies"
  (§5.2).
  [HTML "obtain a site"](https://html.spec.whatwg.org/multipage/browsers.html#obtain-a-site)
  returns *(scheme, host)* when the host has no registrable domain, and the
  [URL Standard](https://url.spec.whatwg.org/#host-public-suffix) gives a
  public suffix (hence a registrable domain) only to domains, so an IP
  host is its own site. ["Same site"](https://html.spec.whatwg.org/multipage/browsers.html#concept-site-same-site)
  compares schemes, so `http://pi` and `https://pi` are different sites.
- **No port isolation.** RFC 6265bis §1: "cookies for a given host are
  shared across all the ports on that host". §4.1.3.2: ports "are the only
  piece of the origin model that `__Host-` cookies continue to ignore". Any
  other service on the Pi, on any port, can read and overwrite the
  dashboard's cookies, with or without TLS.
- **Transport secrecy and integrity.** No TLS means credentials, session
  cookies and pages cross the LAN in clear text. In the settled threat
  model (untrusted devices on a non-isolated network) that is the attacker
  the design has to stop. *Inference from the threat model, not a spec
  rule.*

### 2.5 WebAuthn specifically

- [WebAuthn L3 §5](https://www.w3.org/TR/webauthn-3/#sctn-api): user
  agents "only expose this API to callers in secure contexts". For web
  contexts that means a secure transport such as TLS "established without
  errors".
- [§5.1.3 step 7](https://www.w3.org/TR/webauthn-3/#sctn-createCredential):
  if the caller's effective domain "is not a valid domain", a
  `SecurityError` is thrown. "Only the domain format of host is allowed
  here", so IPv4 and IPv6 literals are rejected.
- The RP ID must equal the effective domain or be a registrable domain
  suffix of it (§5.1.3.1).

Consequences:

- Passkeys need a DNS name and a certificate every operator device trusts
  without a warning.
- `https://192.168.1.20` fails whatever the certificate.
- A clicked-through self-signed certificate fails.
- `http://localhost` would pass (loopback is trustworthy and `localhost` is
  a domain). That only helps the tunnel path.
- `ts.net` is on the [Public Suffix List](https://publicsuffix.org/list/public_suffix_list.dat)
  (private section, Tailscale Inc.). For `pi.tail1234.ts.net` the
  registrable domain is `tail1234.ts.net`, so a Tailscale HTTPS name can
  serve as an RP ID, and each tailnet is its own site.

### 2.6 Does a VPN change any of this?

- **On the wire, yes.** Tailscale "enables encrypted point-to-point
  connections using the open source WireGuard protocol"
  ([What is Tailscale](https://tailscale.com/kb/1151/what-is-tailscale)).
  A LAN device outside the tailnet cannot read or modify dashboard traffic
  that travels over the tailnet. *Inference:* this holds only while the
  dashboard is reached through its tailnet address and is not also
  reachable on its LAN address.
- **In the browser, no.** Tailscale IPv4 addresses come from `100.64.0.0/10`
  ([100.x addresses](https://tailscale.com/kb/1015/100.x-addresses)). MagicDNS
  names look like `monitoring.yak-bebop.ts.net`
  ([MagicDNS](https://tailscale.com/kb/1081/magicdns)). Neither is loopback
  and neither is `https`, so over plain HTTP:
  - there is no secure context;
  - there is no `Sec-Fetch-Site`;
  - `Secure` and `__Host-` cookies cannot be used;
  - there is no WebAuthn.

  The secure-context rules have no "trusted network" input. Only an
  `https://` URL with a trusted certificate changes the browser's view (3.5).

### 2.7 Local Network Access (successor of Private Network Access)

- [Local Network Access](https://wicg.github.io/local-network-access/)
  (WICG Draft CG Report) classifies addresses into three spaces:
  - loopback: `127.0.0.0/8`, `::1`, ...;
  - local: `10/8`, `100.64.0.0/10`, `172.16/12`, `192.168/16`,
    `169.254/16`, `fc00::/7`, `fe80::/10`;
  - public: everything else.
- A request is a "local network request" when its target is less public
  than its initiator, and it needs a user permission (`local-network` or
  `loopback-network`). That permission is available only to secure
  contexts.
- Chromium does not enforce the permission for cross-origin local-to-local
  requests, and same-origin requests from a trustworthy origin are exempt.
- Chrome shipped the prompt in Chrome 142
  ([Chrome blog](https://developer.chrome.com/blog/local-network-access),
  [release notes](https://developer.chrome.com/release-notes/142)).

**Relevance.**

- The dashboard calling itself is same-origin and local-to-local, so LNA
  never blocks it, on either the LAN or a tailnet address.
- LNA is extra protection: in Chrome, a public website can no longer fire
  requests at `192.168.x.y` or `100.x.y.z` without a prompt.
- LNA is Chromium-only and a draft, so ADR 0007's checks cannot be dropped
  because of it.

## 3. Certificate sources

### Ground rules that apply to all of them

- **Publicly trusted CAs cannot cover private IPs or internal names.**
  Baseline Requirements §4.2.2: "CAs SHALL NOT issue Certificates
  containing Internal Names or Reserved IP Addresses". Reserved means any
  block in the IANA special-purpose registries (§1.6.1). That includes RFC
  1918 and `100.64.0.0/10`. Let's Encrypt and Tailscale certificates
  therefore name a public DNS name, never `192.168.x.y`, `100.x.y.z` or
  `pi.lan`.
- **Public certificate lifetimes are shrinking.** BR §1.2.2: at most 200
  days from 2026-03-15, 100 days from 2027-03-15, 47 days from 2029-03-15.
- **Apple platforms constrain private-CA leaves too.** For all TLS server
  certificates, iOS 13 and macOS 10.15 require the name in the SAN (not the
  CN), RSA ≥ 2048 bits, SHA-2, and, for certificates issued after
  2019-07-01, EKU `serverAuth` and validity ≤ 825 days
  ([Apple 103769](https://support.apple.com/en-us/103769)). The later
  398-day limit "will not affect certificates issued from user-added or
  administrator-added Root CAs"
  ([Apple 102028](https://support.apple.com/en-us/102028)).
- **Installing a private root on iOS takes two steps.** Install the
  profile, then turn it on under Settings > General > About > Certificate
  Trust Settings > "Enable full trust for root certificates"
  ([Apple 102390](https://support.apple.com/en-us/102390)).
- **On Android, user-installed roots are opt-in per app.** Apps targeting
  API 24 or later trust only `system` CAs by default; the `user` store is
  trusted only if the app opts in through its Network Security Config
  ([Android docs](https://developer.android.com/privacy-and-security/security-config)).
  Each browser is an app, so whether a user-installed root works depends on
  the browser. This was not verified against a primary source per browser
  (see Open points).
- **Desktop Chrome uses locally trusted roots.** The Chrome verifier takes
  locally added roots from the Windows stores and the macOS keychains,
  "for adding and removing trust" over TCP TLS. Chrome for iOS does not use
  it ([Chrome Root Store FAQ](https://chromium.googlesource.com/chromium/src/+/main/net/data/ssl/chrome_root_store/faq.md)).
  mkcert lists Firefox (NSS) as a separate store on macOS and Linux
  ([mkcert README](https://github.com/FiloSottile/mkcert)).
- **A private root can intercept everything.** A root trusted on a device
  is trusted for every name. mkcert warns that its root key "gives complete
  power to intercept secure requests from your machine. Do not share it."
  The same holds for any private CA whose root collaborators install.

### 3.1 Self-signed certificate

- **Names.** Anything, including IP SANs. Apple's rules above still apply
  if iOS should accept it.
- **Trust distribution.** Either install the certificate on every device as
  a trusted root (the same steps as for a private CA, with no CA to issue
  more), or click through the browser warning each time.
- **Renewal.** Manual reissue, then redistribute to every device.
- **arm64.** Any `openssl` will do.
- **Browser consequences of clicking through.** The page is still an
  `https` origin, so it is a secure context and gets `Sec-Fetch-*`. But
  WebAuthn requires TLS "established without errors" (2.5).
- **Threat model.** *Inference:* a user who routinely clicks through cannot
  tell the dashboard's certificate from one presented by an attacker on the
  LAN, which is the threat the settled model names.

### 3.2 Private CA: mkcert

- **What it is.** A local CA tool. It creates a root (valid 10 years) and
  leaf certificates valid "2 years and 3 months", chosen to stay under the
  825-day Apple limit "including custom roots" (`cert.go`, v1.4.4). Leaf
  certificates can carry DNS names and IPs.
- **Trust distribution.**
  - `mkcert -install` writes the root into the macOS, Windows and Linux
    system stores, Firefox/NSS and Java on the machine that runs it.
  - Other laptops need the root copied in by hand.
  - iOS: "use AirDrop, email the CA to yourself, or serve it from an HTTP
    server", then install it and enable full trust.
  - Android: install the CA, and "enable user roots in the development
    build of your app" (see Open points).
- **Renewal.** None built in. Reissue and restart before 825 days.
- **arm64.** Release v1.4.4 (2022-04-26) ships `linux-arm64`,
  `darwin-arm64` and `windows-arm64` binaries.
- **Caveats.**
  - "mkcert is meant for development purposes, not production, so it should
    not be used on end users' machines."
  - Collaborators would install a root whose key sits on the operator's
    machine.
  - The project's last release is from 2022.

### 3.3 Private CA: step-ca

- **What it is.** "An online Certificate Authority (CA) for secure,
  automated X.509 and SSH certificate management"
  ([docs](https://smallstep.com/docs/step-ca/)).
- **ACME server.** It supports `http-01`, `dns-01`, `tls-alpn-01` and
  `device-attest-01`. IP identifiers work with `http-01` and `tls-alpn-01`
  ([ACME basics](https://smallstep.com/docs/step-ca/acme-basics/)), so a
  LAN IP or a `.lan` name can be validated on the LAN itself.
- **Renewal.** "By default, step-ca issues short-lived certificates that
  expire after 24 hours"
  ([renewal](https://smallstep.com/docs/step-ca/renewal/)).
  - `step ca renew --daemon` renews at about two-thirds of the lifetime and
    retries every minute.
  - `--exec` reloads the server after each renewal.
  - The docs prefer a `cert-renewer@.timer` systemd unit that runs every
    5-10 minutes.
  - An expired certificate cannot be renewed unless the provisioner allows
    `--allow-renewal-after-expiry`.
- **Trust distribution.** The same manual root install on every laptop and
  phone as mkcert. The installation page does not cover installing the
  root on other devices.
- **arm64.** Binaries are built for arm64, armv6 and armv7
  ([installation](https://smallstep.com/docs/step-ca/installation/)).
  Release v0.30.2 (2026-03-23) ships `step-ca_*_arm64.deb` and `.rpm`.
- **Caveats.**
  - It adds a long-running service that must be up for every renewal
    (daily with the default lifetime).
  - Active revocation is "very limited (CRL, OCSP)" in the open-source
    edition.
  - A 24 h certificate does not fit uvicorn's load-once TLS without a
    reload hook (4.1).

### 3.4 Let's Encrypt via DNS-01 with an owned domain

- **What it is.** A publicly trusted certificate for a name such as
  `dash.example.net`. DNS-01 "can validate domain names whose webservers
  aren't exposed to the public internet" and supports wildcards
  ([challenge types](https://letsencrypt.org/docs/challenge-types/)).
  `HTTP-01` "can only be done on port 80" from the internet, so it does not
  fit a private host.
- **Trust distribution.** None needed: every laptop and phone already
  trusts the public roots.
- **Renewal.** Fully automatable with an ACME client (certbot, lego,
  acme.sh, Caddy), but lifetimes are shrinking
  ([90 to 45 days](https://letsencrypt.org/2025/12/02/from-90-to-45)):
  - the default `classic` profile goes to 64 days on 2027-02-10 and to 45
    days on 2028-02-16;
  - the opt-in `tlsserver` profile has been at 45 days since 2026-05-13;
  - renew at about two-thirds of the lifetime or use ARI, because "renewing
    at a hardcoded interval of 60 days will no longer be sufficient";
  - DNS-PERSIST-01 (expected in 2026) would remove the per-renewal TXT
    change.
- **Costs and risks.**
  - "Keeping API credentials on your web server is risky": the host needs
    a DNS-provider token that can usually change the whole zone.
  - Propagation waits can reach "as much as an hour".
  - The name appears in public CT logs (the same mechanism Tailscale warns
    about, 3.5).
- **IP certificates.** Let's Encrypt has issued IP-address certificates
  since 2026-01-15, only short-lived (160 h) and never via DNS-01
  ([GA post](https://letsencrypt.org/2026/01/15/6day-and-ip-general-availability)).
  BR §4.2.2 excludes private and CGNAT addresses, so this does not help on
  a LAN.
- **Name resolution is a separate problem.** The certificate covers a
  public name, but browsers have to resolve it to the Pi's private
  address. Options are a public A record pointing to the private IP,
  split-horizon DNS, or per-device hosts files (which phones do not
  offer). Many home routers run dnsmasq, whose `--stop-dns-rebind` option
  will "Reject (and log) addresses from upstream nameservers which are in
  the private ranges" unless the domain is listed in `--rebind-domain-ok`
  ([dnsmasq man page](https://thekelleys.org.uk/dnsmasq/docs/dnsmasq-man.html)).
  Whether a given router enables it is deployment-specific.
- **arm64.** lego v5.6.0 ships `linux_arm64`. certbot and acme.sh are a
  Python program and a shell script respectively, so architecture does not
  matter.

### 3.5 Tailscale certificates

- **What it is.** With MagicDNS and "Enable HTTPS" turned on in the admin
  console, `tailscale cert` obtains a Let's Encrypt certificate for the
  machine's `<machine>.<tailnet>.ts.net` name
  ([Enabling HTTPS](https://tailscale.com/kb/1153/enabling-https)). The
  browser must use the full name; `https://machine-name` cannot work. There
  is no certificate for the `100.x` address (BR §4.2.2).
- **Trust distribution.** None: the certificate is publicly trusted. Every
  operator device must, however, be on the tailnet. That means a Tailscale
  client on each laptop and phone and an account or share for every
  collaborator, which matters for the settled "small known group" model.
- **Renewal.**
  - The certificates "have a 90 day expiry". With `tailscale cert` "you are
    responsible for renewing the certificate"; the Caddy integration renews
    automatically.
  - `tailscale serve` proxies tailnet traffic to a local service on the
    tailnet HTTPS name and provisions the certificate itself
    ([Serve](https://tailscale.com/kb/1312/serve)).
  - The 90-day figure is the docs' wording and predates Let's Encrypt's
    move to 64 and 45 days (3.4).
- **Identity headers.** `tailscale serve` adds `Tailscale-User-Login`,
  `Tailscale-User-Name` and `Tailscale-User-Profile-Pic` for tailnet
  traffic, but not for traffic from tagged devices, and strips them if a
  client sends them. This is relevant to the authentication tickets, not
  to TLS.
- **Privacy.** Certificates go to public CT logs, which "includes the fully
  qualified domain name of your devices". "Do not enable the HTTPS feature
  if any of your machine names contain sensitive information."
- **arm64.** Static `arm64` tarballs and Raspbian/Debian packages
  ([packages](https://pkgs.tailscale.com/stable/)).

### Comparison

| | Self-signed | mkcert | step-ca | LE DNS-01 | Tailscale |
|---|---|---|---|---|---|
| Install on each device | cert or click-through | root | root | nothing | Tailscale client |
| IP / `.lan` names | yes | yes | yes | no | no (`ts.net` name only) |
| Needs an owned domain | no | no | no | yes | no (`ts.net`) |
| Default leaf lifetime | chosen | 825 days | 24 h | 90 → 64 → 45 days | 90 days (docs) |
| Renewal tooling | none | none | daemon or systemd timer | ACME client | `tailscale cert` (manual) or `serve` (auto) |
| Extra running service | no | no | the CA | no (client via cron/timer) | `tailscaled` |
| Name in public CT logs | no | no | no | yes | yes |
| WebAuthn-capable | no | yes (DNS name) | yes (DNS name) | yes | yes |
| arm64 | yes | yes | yes | yes | yes |
| Main risk | trains click-through | root key custody on phones | root key custody, CA uptime | DNS API token on the host | dependency on the Tailscale control plane and accounts |

## 4. Where TLS terminates

### 4.1 In-process (uvicorn)

- **Options.** uvicorn 0.54.0 takes `ssl_keyfile`, `ssl_certfile`,
  `ssl_keyfile_password`, `ssl_version`, `ssl_cert_reqs`, `ssl_ca_certs`,
  `ssl_ciphers` and `ssl_context_factory` (`uvicorn/config.py` at 0.54.0;
  [settings](https://raw.githubusercontent.com/encode/uvicorn/master/docs/settings.md)).
  `cli.py` would pass them through to `uvicorn.run`.
- **Certificates load once.** `Config.load()` builds the `SSLContext` once
  (`assert not self.loaded`) and nothing watches the files. Renewal
  therefore means a restart unless an `ssl_context_factory` hands back a
  context the application reloads itself. That is a minor nuisance at 64
  to 90 days and a daily restart with step-ca's default 24 h.
- **HTTP/2.** It needs `--http2` with the experimental `zttp` backend.
  Otherwise the server speaks HTTP/1.1.
- **No new Docker argv.** In-process TLS keeps the single-process install
  of ADR 0001 and does not touch `gateway.py`.

### 4.2 Behind a TLS-terminating proxy (Caddy, `tailscale serve`, ...)

- **What uvicorn's proxy support does.**
  - `ProxyHeadersMiddleware` reads only `X-Forwarded-Proto` and
    `X-Forwarded-For`, and only from peers in `forwarded_allow_ips`.
    The default list is `127.0.0.1` and `::1`
    ([source](https://raw.githubusercontent.com/encode/uvicorn/master/uvicorn/middleware/proxy_headers.py)).
  - It sets `scope["scheme"]` and the client address. It does not handle
    `X-Forwarded-Host` and never touches `Host`.
- **What follows for this app.**
  - The Host guard and the `Origin` comparison work only if the proxy
    forwards the browser's `Host` unchanged.
  - The expected `Origin` scheme has to come from the trusted
    `X-Forwarded-Proto` (or configuration), not from the literal in
    `control_routes.py:448`.
  - A proxy in another container reaches uvicorn from a Docker-network
    address, not loopback, so it is not trusted by default.
- **Caddy.** Caddy obtains and renews certificates automatically. For IP
  addresses and internal names it uses its own local CA and tries to
  install that root into the host's trust store. The DNS challenge needs a
  Caddy build with the provider's `caddy-dns` module
  ([Automatic HTTPS](https://caddyserver.com/docs/automatic-https)).

## 5. What the spec has to settle (inputs, not decisions)

1. **The scheme in the `Origin` check.** `control_routes.py:448` must derive
   the scheme (and, behind a proxy, the trusted host) instead of assuming
   `http://`. Otherwise HTTPS breaks every Control action.
2. **`Sec-Fetch-Site` "when present".** Over HTTPS it is sent by every
   browser that implements Fetch Metadata. Over plain HTTP on a LAN it is
   never sent. Whether
   LAN mode may run over plain HTTP decides whether this check means
   anything there.
3. **Session cookies.** A session cookie can be `Secure`/`__Host-` only
   under HTTPS. Even then it is shared with every other port on the same
   host (RFC 6265bis §1), as are the existing `attempt_range` and
   `fleet_range` cookies.
4. **Passkeys.** WebAuthn excludes IP-only URLs, plain HTTP and
   clicked-through certificates. Passkey sign-in therefore requires a DNS
   name plus a certificate every device trusts.
5. **Host allow-list.** It must list exactly the certificate names (and
   drop IPs if only names carry certificates). *Inference:* a site served
   only over HTTPS also defeats DNS rebinding at the TLS layer, because the
   rebinding page's hostname does not match the dashboard's certificate.
   The allow-list stays as the defence for any remaining HTTP listener.
6. **Name resolution for phones.** Public DNS pointing to a private IP may
   be filtered by router rebind protection; hosts files are not available
   on phones; MagicDNS covers tailnet devices only.
7. **Root-key custody.** With a private CA, every collaborator's device
   trusts a key the operator holds, for every site.
8. **Renewal versus restart.** Certificate lifetime decides how often
   uvicorn must reload, which also bears on the ADR 0002 container path.

## Open points

- **Android browsers and user-installed roots.** Platform docs say apps
  targeting API 24 or later ignore the user CA store unless they opt in. No
  primary source per browser was found for whether Chrome or Firefox on
  Android accept a user-installed root for web pages. Verify on a device
  before choosing a private CA.
- **Fetch Metadata support.** Browser support (in particular the first
  Safari version that sends `Sec-Fetch-*`) was not checked against vendor
  release notes. ADR 0007's "when present" rule already tolerates absence.
- **LNA in other browsers.** Firefox and Safari plans for Local Network
  Access were not researched.
- **`tailscale serve` details.** How `tailscale serve` forwards `Host` and
  `X-Forwarded-*` to the backend, and how it renews certificates, was not
  confirmed in its KB page. It matters for item 1 of section 5 if Serve
  becomes the proxy.
