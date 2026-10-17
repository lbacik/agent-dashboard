# Design

Visual rules for the dashboard UI (`src/agent_dashboard/static/style.css`
and the Jinja templates). Read this before touching colours or layout.

## Theme: light only (for now)

We are experimenting with a **light theme only**. The earlier
`prefers-color-scheme: dark` override was removed because it was hard to
read (low-contrast text, muddy chart colours, a dark top bar and tooltip
that clashed with the light pages).

Until we decide to revisit dark mode:

- Do not add `@media (prefers-color-scheme: dark)` rules or a theme
  toggle.
- The page declares `color-scheme: light` (CSS on `:root` and a
  `<meta name="color-scheme">` in `base.html`), so the browser does not
  darken form controls or scrollbars on its own.
- Changes to the palette are tuned and checked against the light
  background only.

If dark mode comes back, it should be a separate, deliberate design pass
that redefines the tokens below, not a set of per-selector overrides.

## Colour tokens

Every colour lives as a custom property on `:root` in `style.css`. Rules
use `var(--…)`; no hex or `rgb()` values outside `:root`.

| Token | Use |
| --- | --- |
| `--bg` | Page background |
| `--card` | Surfaces: top bar, tables, drawer, charts, tooltip |
| `--surface-2` | Table headers, budget bar track, evidence previews |
| `--hover` | Hovered and active table rows |
| `--ink` / `--muted` | Body text / secondary text, axis labels |
| `--line` | Borders, grid lines, dividers |
| `--accent`, `--accent-soft` | Primary accent (budget fill, focus rings, top bar stripe) |
| `--link` | Links |
| `--shadow` | Drawer and tooltip shadow |
| `--{ok,warn,danger,info,off}-{bg,ink}` | Status pills, tags, banners, log levels |
| `--oc-*` | One colour per attempt outcome; token charts reuse them |

## Rules

- **Contrast.** Text meets WCAG AA (4.5:1) against the surface it sits
  on. Muted text is for secondary information only.
- **Not colour alone.** Charts and statuses always add a second cue:
  a label, a shape, a pattern or a tooltip (see
  [ADR 0004](docs/adr/0004-history-charts-inline-svg.md)).
- **Light surfaces.** Chrome (top bar, drawer, tooltip) stays light; the
  hierarchy comes from borders, the `--surface-2` tint and shadows rather
  than from dark blocks.
- **New outcome or series.** Add a `--oc-*` token if needed and one CSS
  rule per mark type that uses it.
