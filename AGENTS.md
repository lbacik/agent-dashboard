# AGENTS.md

Read-only web dashboard (FastAPI + Jinja2 + htmx/SSE) for a fleet of
simple-coding-agent containers on one Docker host. Start with `README.md`.

## Where things are decided

- **Vocabulary**: `CONTEXT.md` (Poll outcome, Reach, Status headline,
  Attention rank, Attempt log, Evidence file). Use these terms in code,
  tests and commits; contract terms live in the agent repo's glossary it
  links to.
- **Decisions**: `docs/adr/`. Read the relevant ADR before changing Docker
  access, log reading or charts; record a new decision as the next ADR.
- **Specs**: `docs/spec/`.
- **UI look**: `DESIGN.md`. Read it before touching `style.css` or a
  template's markup.
- **Handoffs**: `.agent/handoff/<issue>.md` holds the note from an
  interrupted attempt on that issue; read it first when resuming one.

## Invariants

- `gateway.py` is the only module that builds Docker argv; every argv it
  can produce is pinned by a test.
- Templates consume framework-free view models only.
- Test fixtures are synthetic; `tests/fixtures/contract/v1/` is copied
  from the agent repo (source commit in `SOURCE.txt`).
- Web tests assert HTML fragments through `TestClient`; there are no
  browser end-to-end tests.

## Done

`uv run ruff check` and `uv run pytest` both pass (the CI gate).
