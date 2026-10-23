# Design

Visual rules for the dashboard UI (`src/agent_dashboard/static/style.css`
and the Jinja templates).

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
  key is stable per view (the drawer uses `drawer`); the container
  keeps the attribute across fragments, including the gone fragment.
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
