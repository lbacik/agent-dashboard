# agent-dashboard

A read-only web dashboard for a fleet of [simple-coding-agent](https://github.com/lbacik/simple-coding-agent) containers on one Docker host. It discovers agent containers by image label, polls each one with `docker exec <container> agentctl telemetry --json`, and shows what every instance is doing: control state, the current attempt's token budget and context figures, and recent events.

Status: specified, not yet built.

- Design map: [Agent fleet dashboard: map](https://github.com/lbacik/simple-coding-agent/issues/222)
- v1 specification of this project: [Define the dashboard project: repo, layout, stack and delivery slices](https://github.com/lbacik/simple-coding-agent/issues/228), which links the snapshot contract, discovery and Docker access decisions, and the UI prototype.
- Vocabulary: [CONTEXT.md](CONTEXT.md); contract terms (Dashboard, Managed instance, Telemetry snapshot, Figure basis) are defined in the [agent's CONTEXT.md](https://github.com/lbacik/simple-coding-agent/blob/main/CONTEXT.md).
- Decisions: [docs/adr/](docs/adr/)
