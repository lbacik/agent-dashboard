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
