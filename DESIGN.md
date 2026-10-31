# Design

Visual rules for the dashboard UI (`src/agent_dashboard/static/style.css`
and the Jinja templates).

## Palette: Crisp Technical Daylight (prototype variant C)

Every dashboard page (the fleet view and the Attempt History, Attempt Log
and Attempt Progress pages) uses the **Crisp Technical Daylight**
instrument palette of prototype variant C ("Ops Mission Control
Cockpit"): a cool grey surface, white cards with square corners and mono
uppercase panel bars.

Base tokens on `:root`:

- `--bg: #e5e7eb`, `--card: #ffffff`, `--surface-2: #f3f4f6`,
  `--hover: #f0f9ff`
- `--ink: #111827`, `--muted: #4b5563`, `--line: #cbd5e1`
- `--accent: #0284c7`, `--accent-soft: #38bdf8`, `--link: #0369a1`,
  `--on-accent: #ffffff` (text on a filled badge)
- `--shadow: rgba(17, 24, 39, 0.12)`: drawer and tooltip shadows.
- `--radius: 2px`: every card, panel, button, input and bar. Only pills
  (`.pill`, `.stale-tag`, chips) stay fully rounded.
- `--font-sans`: system geometric sans stack for labels
  (`system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial,
  sans-serif`; no web-font fetch, the dashboard is loopback-only per
  ADR 0001).
- `--font-mono`: system monospace stack for figures and instrument
  labels (`ui-monospace, "SF Mono", "Cascadia Mono", Menlo, Consolas,
  "Liberation Mono", monospace`) with `font-variant-numeric:
  tabular-nums` on figures (budget, context, turns, cache, counts), chart
  axis and segment labels, and paging counts.

Semantic tokens (every text pair meets WCAG AA, 4.5:1 or better):

- Reach pills: live `var(--ok-ink) #166534` on `var(--ok-bg) #dcfce7`
  (6.49:1), stale `var(--warn-ink) #9a3412` on `var(--warn-bg) #ffedd5`
  (6.38:1), unreachable `var(--danger-ink) #991b1b` on
  `var(--danger-bg) #fee2e2` (6.80:1), not running `var(--off-ink)
  #374151` on `var(--off-bg) #e5e7eb` (8.33:1), unsupported
  `var(--info-ink) #075985` on `var(--info-bg) #e0f2fe` (6.59:1).
  `--danger-line: #fca5a5` borders danger surfaces.
- Muted text: `#4b5563` on card 7.56:1, on `--surface-2` 6.87:1, on
  `--bg` 6.10:1. Links: `#0369a1` on card 5.93:1, on `--info-bg` 5.17:1.
- ADR 0004 outcome series: `--oc-complete #15803d`, `--oc-incomplete
  #b45309`, `--oc-handoff #0369a1`, `--oc-infra #dc2626`,
  `--oc-nochanges #6b7280`, `--oc-other #7c3aed`; on white they read
  5.02/5.02/5.93/4.83/4.83/5.70:1, and white segment labels on the same
  fills read the same ratios. **Deliberate deviation from the prototype:**
  its `#16a34a`, `#d97706` and `#0284c7` read only 3.30/3.19/4.10:1, so
  these three use the next darker step of the same hue. Token chart
  categories reuse these fills (`tok-input` handoff, `tok-cache-read`
  complete, `tok-cache-creation` incomplete, `tok-output` other). Chart
  series keep their distinct shapes/patterns alongside colour (ADR 0004).
- Attention-rank stripes (`--vc-stripe-*`, non-text, beside the Reach pill
  and headline): alert `#dc2626`, warning `#b45309`, working `#15803d`,
  idle `#cbd5e1`, offline `#374151`.
- A filled ON badge uses `--link` (5.93:1 with `--on-accent`), not
  `--accent` (4.10:1). `var(--accent)` is never body text, only borders,
  fills and focus rings.

## Instrument chrome (prototype variant C)

- **Top bar**: white, `border-top: 3px solid var(--accent)`, mono. Three
  cells: a square status LED plus the canonical title `Agent fleet
  dashboard` (uppercase by CSS); the fleet subtitle or the breadcrumb
  trail; chips with **real values only** (`Stream <poll>s`, `Control
  on/off`) followed by the page's `topbar_actions` (on-demand note,
  Refresh). Never prototype host/schema chips.
- **Panel bar**: the title strip of every panel (fleet overview summary,
  rail head, Operations matrix bar, cockpit modules) is mono, 0.72rem,
  bold, uppercase with 0.04em tracking, on `--surface-2` with a bottom
  `--line` border.
- **Fleet Matrix**: four KPI tiles (cards with a 4px left edge, square
  LED, ON/OFF badge, large mono count and a `filter: …` hint) above the
  table; the matrix sits in one scroll box opened by the
  `Operations matrix // <filter summary>` bar.
- **Fleet units rail**: a flat list with row separators and a 3px rank
  stripe; boxed rank tag (`R3`, `OFF`), name, Reach pill, mono repository
  and attempt, mini budget bar, one-line headline. Offline units sit in a
  collapsible group. The selected unit is `--info-bg` with an accent
  stripe. Under the rail head the four Fleet Matrix filters repeat as one
  row of scaled-down chips (mono label, count, LED; no badge or hint) that
  share the matrix filter state; the selected unit stays listed whatever
  the filters say.
- **Instance cockpit**: the drawer has no chrome of its own. A header card
  (4px accent left edge) carries back button, kicker, name, Reach pill,
  status headline box, Logs/Live links and close. Below it three columns
  of numbered module cards (`.vc-module` + `.vc-module-bar`):
  01 Control console, 02 Identity & last attempt; 03 Activity,
  04 Current attempt; 05 Attempt statistics, 06 Recent events.

## Theme: light only (for now)

We are currently experimenting with **light themes only**. The earlier
`prefers-color-scheme: dark` override was removed: it was hard to read and
drifted from the light prototype the UI follows.

Until we deliberately revisit dark mode:

- Every colour is a token (custom property) on `:root` in `style.css`;
  rules use `var(--…)`, never a literal colour.
- The page declares `color-scheme: light` (on `:root` and as a
  `<meta name="color-scheme">` in `base.html`), so the browser keeps form
  controls and scrollbars light.
- Palette changes are tuned and checked against the light background only.
- Chrome (top bar, cockpit modules, tooltip) stays light; hierarchy comes
  from borders, the `--surface-2` panel bars and shadows.
- Text meets WCAG AA contrast against its surface, and no status or chart
  series relies on colour alone (see
  [ADR 0004](docs/adr/0004-history-charts-inline-svg.md)).

If dark mode comes back, it is a separate design pass that redefines the
`:root` tokens, not a set of per-selector overrides.

## Live refresh never moves a scroll position the user set

The main page updates live: the `fleet` SSE event re-renders the fleet
table, the drawer refreshes on `sse:fleet`, and the Activity widget
reloads on every poll cycle. Each of these swaps replaces markup the
user may have scrolled, so every scroll container inside a
live-refreshed region that should hold still opts into the shared
scroll keeper (`src/agent_dashboard/static/scroll-keep.js`, loaded on
the main page):

- Add `data-scroll-key="<stable-key>"` to the scroll container. The
  key is stable per view (the drawer uses `drawer`, the Activity body
  uses `activity-body`); the container keeps the attribute across
  fragments, including the gone fragment. When a pane shows
  ever-changing content, key it per item instead (the Activity
  call/response panes use `activity-now-<file>` and
  `activity-last-call-<file>` / `activity-last-response-<file>`): the
  pane then holds still while it shows the same call and starts at the
  top when a new call replaces it, because the old key is simply absent
  after the swap.
- The keeper captures the container's horizontal and vertical scroll
  position before the swap and reapplies it to the container with the
  same key afterwards. The browser clamps an out-of-range value to the
  new content, so a shorter list settles at its end.
- The page itself is kept too: the Fleet Matrix and the cockpit
  scroll with the window, and a swap can briefly shorten the page
  (the fresh fleet HTML carries the default filter set until
  `fleet.js` reapplies the active one), which clamps the window
  towards the top. The keeper records the window position with the
  keyed boxes and restores everything in a microtask, after every
  other `htmx:afterSwap` listener has rebuilt the page.
- Positions live in page memory only. A full page reload starts every
  view at its initial position; nothing is written to browser storage
  (`localStorage`/`sessionStorage`).
- A key whose container is absent after the swap is simply skipped,
  with no console errors.

Do not add per-widget save/restore code around a live swap: put the
stable key on the container and let the shared keeper handle it.

## An in-progress form inside a live region is never re-rendered mid-typing

An in-progress form inside a live-refreshed region gets a stable ID and
`hx-preserve`. Its own parts refresh through a filtered `sse:fleet`
trigger that holds while a confirmation is open or focus is inside it.
Never re-render a form the operator is typing into.
