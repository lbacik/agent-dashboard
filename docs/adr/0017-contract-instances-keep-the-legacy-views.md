# Contract instances keep the legacy views

Moving an instance from the legacy path to the contract path (ADR 0014)
changes where the dashboard reads data, never what the operator sees. In
v0.5.0/v0.5.1 a container carrying both labels switched to the contract path
and lost the rail's budget bar, the fleet matrix figure cells, the Activity
widget's Now / Last call panes and the Current attempt layout. The cockpit
also renumbered its modules (01 Contract status ... 05 Recent events, plus a
second 05 Attempt statistics outside the grid). Nothing had decided that
change: the contract views were built as a separate, minimal render of the
Status Snapshot, and no test compared them with the legacy views.

## Decision

- **One view, two data sources.** The rail item, the fleet matrix row, the
  cockpit's modules (01 Control console, 02 Identity & last attempt; 03
  Activity, 04 Current attempt; 05 Attempt statistics, 06 Recent events, in
  the same three columns) and the Activity widget render the same markup
  for both paths. Where a template must branch on `row.path`, it branches
  on the *data*, not the layout.
- **The Current attempt layout reads the Status Snapshot.**
  `contract_status.read_attempt_gauge` maps the budget and figures onto the
  legacy rows: the bar fills the enforced token limit (`raw_tokens` or
  `effective_tokens`) against its ceiling, and the soft tick, auto-compact
  threshold, compactions, continuation max and token categories come from
  simple-coding-agent's `native.*` names. A backend without those names
  shows the rows as `unknown`; nothing is derived that the backend did not
  report. Rows the contract cannot fill at all (soft rule, the cache-hit
  response count, rate-limit headroom) are left out rather than shown
  empty. Other enforced limits and the remaining figures follow under
  their own names. Token figures in this layout omit the unit word (the
  row names the quantity); every other unit and the basis markers stay
  (DESIGN.md).
- **Activity reads the Attempt Archive.** Each refresh exports
  `events.jsonl` (the contract requires it in every archive), maps
  `native.tool_call` / `native.tool_result` / `native.model_response`
  events onto the legacy event rows, and derives Now / Last call with the
  legacy `find_activity`. Then it reads the at most four Evidence files
  those panes need in one `attempts export <id> --path ... --path ...`
  exec (`argv_contract_attempts_export_members`, pinned). That makes two
  execs per refresh. Every member is verified against the export's own
  manifest (size and `sha256`), with one retry for a torn read. Nothing is
  cached, so ADR 0014's Sealed-only cache is unchanged. When the archive
  cannot be read, the widget falls back to the snapshot-only view (native
  phase, last activity, attempt events) and says why. The panes' links
  open the contract Attempt page, whose manifest previews every file.
- **Parity is tested.** `tests/test_contract_parity.py` renders a legacy
  and a contract instance side by side and compares the cockpit's
  (column, module) layout, the Current attempt rows, the rail bar, the
  matrix cells and the Activity panes. A change to a view element is a
  DESIGN.md change applied to both paths, and the parity test changes with
  it.

## Considered Options

- **Route dual-labelled containers back to the legacy path.** This would
  restore v0.4.1 exactly for them, but contract-only instances (every new
  instance from agent-forge's `create-agent`) would keep the degraded view,
  and it contradicts ADR 0014's routing. Rejected.
- **Cache Evidence previews of the running attempt** (the legacy widget
  caches its files). That would save one exec per refresh but keep unsealed
  archive content between reads, which ADR 0014 rules out. Rejected in
  favour of one multi-member export.

## Consequences

- The `native.*` names the gauge and the Activity widget read are
  simple-coding-agent's. Another backend gets the same layout with
  `unknown` rows and a model-working Activity until it reports equivalent
  figures or events, never an error.
- An open contract drawer runs two `attempts export` execs per poll cycle
  while an attempt is in progress (the legacy widget runs one exec plus
  cached file reads).

Amends ADR 0014 (the contract path's views). Prompted by the v0.5.1
regression report against v0.4.1.
