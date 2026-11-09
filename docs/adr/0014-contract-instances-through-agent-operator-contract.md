# Contract instances are read and controlled through agent-operator-contract

Instances carrying the `io.github.lbacik.agent-operator.contract` label take
the contract path through the pinned agent-operator-contract library
(`v1.0.0`); instances carrying only the legacy
`io.github.lbacik.simple-coding-agent.role` label keep the telemetry v1 path
unchanged. The label gates the path: discovery reads both labels, and a
container without the contract label never sends or receives a contract
call. A label is not proof — the runtime `--contract` check and the
contract's `contract_mismatch` refusal stay the authority — but it is the
router.

## Library use, with execution kept in the gateway

From the library only its pure parts are used:

- DTOs and schema validation (`documents.validate_*`, the envelope, result
  and export-manifest schemas);
- the contract argv grammar (`agentctl --contract 1 ...` per contract
  section 3.1);
- envelope parsing and classification by `error.code`, with the exit code
  as the fallback for malformed stdout;
- capability checks (`required_capability`, the identity's advertised set);
- request-ID generation and validation;
- the timeout policy (`client.timeout_for`, a pure operation-to-seconds
  mapping with no executor coupling).

Execution, timeouts, kill-and-reap and concurrency stay in the gateway's
async runner. The library's client part couples its argv shape to a blocking
`subprocess.run` executor the gateway cannot use (it would stall the poll
cycle), so the gateway builds contract argv itself from contract section 3
and runs it through its injectable process runner with the contract's
timeout policy. The reply classifier (`contract_reply.py`) never raises:
any incoherent answer — malformed stdout, an exit-code contradiction, a
transport failure (Docker exits 125-127), a timeout, a missing Docker CLI —
reads as `ambiguous`, and the same request ID may be retried.

## Considered Options

- **Use the library's client executor (`client.invoke`) directly**:
  rejected. It blocks on `subprocess.run`, so a slow producer would hold a
  poll-cycle slot; its timeouts and retry loop cannot share the gateway's
  semaphore and per-container control slot.
- **Vendor or copy the fixtures into this repository**: rejected. The
  consumer suite loads from the pinned package data at test time; copies
  would drift from the tag they claim to pin.
- **Keep reading contract instances with the in-container Python
  collectors**: rejected for contract instances. The collectors assume the
  legacy log layout; contract instances expose `attempts list|show|export`
  with stable schemas instead.

## Offline Read through a network-less, read-only helper container

Stopped contract instances are read offline, never through `docker exec`
(which fails on stopped containers): the gateway re-inspects the stopped
container and starts its image removed (`--rm`) with no network
(`--network none`), every volume and bind mount re-mounted read-only at the
same destination, and `agentctl --contract 1 --offline` as the entrypoint
command (contract section 3.2). A running container is never read offline:
the live engine owns the volume, so the read refuses while the container
runs.

## The Sealed-only cache

Exported attempt archives are cached only when sealed. An unsealed attempt
is still moving — its manifest `sha256` may disagree with the tar on the
next read — so every export is verified (size and `sha256` against its
manifest) before it is trusted, and a torn read is refetched within its
bound and otherwise discarded. Nothing about an unsealed archive is kept
between reads; a sealed archive never changes, so it is the only thing the
cache holds.

## The automatic same-ID retry

A mutation whose outcome is ambiguous (`agent_unreachable`, `internal`
without `details.stored: false`, a transport failure, a timeout) is resent
with the same request ID and payload, bounded (three attempts with
backoff), inside the one call. The producer replays an already-committed
ID idempotently (`replayed: true`) instead of committing twice, so the
retry is safe; when the bound is exhausted the caller gets the request ID
back to follow up with `command <id>` instead of resubmitting.

## Consequences

- `gateway.py` stays the only module that builds Docker argv; every contract
  argv it can produce is pinned by a test, like the legacy argv.
- Contract-path fixtures come from the pinned package and are never copied;
  the legacy `tests/fixtures/contract/v1/` copies stay where they are.
- The consumer conformance suite (fixture index plus every recording) runs
  in this repository's CI against the classifier, from the pinned tag.

This record supersedes ADR 0003 and ADR 0005 for contract instances (the
in-container collectors and the `docker cp` tree copies stay the read path
for legacy instances) and amends ADR 0006 (the dashboard-side container-ID
binding and repository check stay for legacy Control actions; contract
mutations bind by instance name through discovery and carry `--expect-repo`
with the request ID on every mutation).

Decided in [Prepare the contract path without any visible feature](https://github.com/lbacik/agent-dashboard/issues/187),
building on the contract adoption plan (agent-operator-contract `v1.0.0`).
