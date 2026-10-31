# Design

Visual rules for the dashboard UI (`src/agent_dashboard/static/style.css`
and the Jinja templates).

## Palette: Crisp Technical Daylight

Every dashboard page (the fleet view and the Attempt History, Attempt Log
and Attempt Progress pages) uses the **Crisp Technical Daylight**
instrument palette: a light slate surface with a white card chrome and a
slim instrument-style top bar. The top bar keeps the canonical title
`Agent fleet dashboard`, the breadcrumb trail on sub-pages, the fleet-page
subtitle and the existing `topbar_actions` block (on-demand note /
Refresh); it shows only real values, never prototype host/schema/mode
chips.

Base tokens on `:root`:

- `--bg: #eef2f6`, `--card: #ffffff`, `--surface-2: #e2e8f0`,
  `--hover: #f8fafc`
- `--ink: #0f172a`, `--muted: #475569`, `--line: #cbd5e1`
- `--accent: #0284c7`, `--link: #0369a1`

New tokens on `:root`:

- `--font-sans`: system geometric sans stack for labels
  (`system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial,
  sans-serif`; no web-font fetch, the dashboard is loopback-only per
  ADR 0001).
- `--font-mono`: system monospace stack for figures
  (`ui-monospace, "SF Mono", "Cascadia Mono", Menlo, Consolas,
  "Liberation Mono", monospace`) with `font-variant-numeric:
  tabular-nums` on figures (budget, context, turns, cache, counts), chart
  axis and segment labels, and paging counts.
- `--accent-soft: #7dd3fc`: hover/focus borders and the light half of the
  hatched partly-estimated budget bar.
- `--shadow: rgba(15, 23, 42, 0.12)`: drawer and tooltip shadows, keyed to
  the ink colour.

Semantic tokens (unchanged hues, checked against the surfaces above; every
text pair meets WCAG AA, 4.5:1 or better):

- Reach pills: live `var(--ok-ink)` on `var(--ok-bg)` (5.24:1), stale
  `var(--warn-ink)` on `var(--warn-bg)` (6.80:1), unreachable
  `var(--danger-ink)` on `var(--danger-bg)` (7.96:1), not running
  `var(--off-ink)` on `var(--off-bg)` (6.94:1), unsupported
  `var(--info-ink)` on `var(--info-bg)` (6.46:1).
- Figure basis tags and stale markers: `var(--muted)` on `var(--card)`
  (7.58:1); `.stale-tag` is warn ink on warn bg (6.80:1).
- Danger/ok/warn/info/off surfaces: `--danger-ink #8c1d1d` on
  `--danger-bg #fdecec` (7.96:1), `--ok-ink #137333` on `--ok-bg #e6f4ea`
  (5.24:1), `--warn-ink #7a4b00` on `--warn-bg #fff4e0` (6.80:1),
  `--info-ink #3949ab` on `--info-bg #e8eaf6` (6.46:1), `--off-ink #45525e`
  on `--off-bg #eceff1` (6.94:1).
- ADR 0004 outcome series (`--oc-complete #1a7f37`, `--oc-incomplete
  #9a6700`, `--oc-handoff #1f6feb`, `--oc-infra #cf222e`,
  `--oc-nochanges #6e7781`, `--oc-other #8250df`): legend glyphs on
  `var(--card)` read 5.08/4.87/4.63/5.36/4.55/5.05:1, and the white
  segment labels on the same fills read the same ratios in reverse; token
  chart categories reuse these fills (`tok-input` handoff, `tok-cache-read`
  complete, `tok-cache-creation` incomplete, `tok-output` other).
  Chart series keep their distinct shapes/patterns alongside colour
  (ADR 0004).
- Links use `var(--link)` (`#0369a1` on white, 5.93:1); `var(--accent)`
  (`#0284c7`) is never body text, only borders, fills and focus rings.

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
- Chrome (top bar, drawer, tooltip) stays light; hierarchy comes from
  borders, the `--surface-2` tint and shadows.
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
