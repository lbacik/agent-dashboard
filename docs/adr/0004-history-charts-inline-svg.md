# Render dashboard charts as server-side inline SVG

The attempt history page needs two charts ("what actually gets delivered,
and when does it go wrong?"): outcomes per day (stacked bars) and an
attempts timeline (one mark per attempt, coloured by outcome, sized by
cost). Later chart tickets reuse this decision and the shared building
blocks. Settled in
[Charting decisions](https://github.com/lbacik/agent-dashboard/issues/16).

## Constraints

- Fits the existing Jinja templates and `static/` assets: no new build
  step, no dependency to install.
- Works offline on an SSH-tunnelled host: no CDN or other network fetch.
- Readable in the dashboard theme (originally light and dark; dark was
  dropped for now, see DESIGN.md); outcomes distinguishable without
  relying on colour alone.
- Attempts without usage data still appear (outcome known, cost unknown).

## Decision

- **Aggregation in `charts.py`, rendering in Jinja.** `outcomes_per_day`
  buckets the same `AttemptSummary` rows as the history table (so both
  charts respect the page's time window filter); `timeline_points` maps
  each dated attempt to a fractional time position, a square-root-scaled
  radius, a shape and a tooltip. The history template turns those view
  models into inline `<svg>`: stacked `<rect>` segments per day, one
  shape per timeline attempt.
- **One stable vocabulary per outcome.** `OUTCOME_ORDER`, `OUTCOME_LABELS`
  and `OUTCOME_SHAPES` map each canonical outcome (plus `other`, which
  covers active/interrupted and any future agent value) to a label, a CSS
  class and a timeline shape (circle, square, triangle, diamond, ring,
  cross). Colours live in `style.css` as tokens (light theme only, see
  [DESIGN.md](../../DESIGN.md)); the non-colour cues are the legend's
  shape glyph plus label, count labels on bar segments, distinct mark
  shapes, and tooltips.
- **Tooltips for hover and focus.** Every mark carries its text
  (`Issue #… · Started … · Duration … · Outcome … · Cost …`) in both a
  `<title>` and a `data-tip`; the dependency-free `static/charts.js`
  upgrades `data-tip` into a floating note on mouse hover and keyboard
  focus, so the charts stay readable with JavaScript disabled.
- **Missing data degrades visibly.** Attempts without a parseable start
  are excluded from both charts and counted in a note; attempts without
  usage data share one neutral radius at reduced opacity (same colour
  and shape, since the outcome is known) and never affect the cost
  scale, which uses the square root of cost.

## Considered options

- **CDN chart library**: rejected. Needs a network fetch the
  SSH-tunnelled host cannot rely on, plus version pinning outside `uv`.
- **Client-side canvas rendering**: rejected. Needs JavaScript for
  anything to appear and gives less accessible marks for free (no DOM
  nodes to focus or label).
- **Server-side PNG (e.g. matplotlib)**: rejected. Adds a heavy
  dependency and build cost for marks that cannot be focused or carry
  tooltips.

## Consequences

- Later chart tickets reuse `charts.py` (`OUTCOME_ORDER`, labels,
  shapes, tooltip text), the `outcome-*` CSS classes, and the
  `data-tip` + `#chart-tip` tooltip pattern.
- A new agent outcome only needs an entry in the three `charts.py`
  maps plus its CSS rules (light theme only, see DESIGN.md).
- `window=all` over a very long history renders one bar per day; if
  that ever grows unwieldy, switch the `all` span to weekly buckets
  in `outcomes_per_day` (tests pin the per-day behaviour until then).
