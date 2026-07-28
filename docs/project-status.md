# Project Status

## Source of truth

Implementation status is established by the actual repository and merged
GitHub pull requests. Approved, bounded work is defined in `tasks/*.md`, while
`docs/jobs-forkstech-spec.html` defines the product direction and build phases.
This document is a human-readable index, not a replacement for those
authoritative sources.

Implementation baseline reviewed through the T007 squash commit:
`0349f4313d57ccc2e32f3aeb46cca44a566bef6b`.

## Current position

The project remains in Phase 1. Tasks 001–007 have been implemented and
squash-merged. Task 007 added the manually invokable one-endpoint
Greenhouse/Lever fetch-to-ingestion runner. One persisted eligible endpoint can
now be snapshotted, fetched through its existing adapter, passed through Task
006 ingestion, and finalized through a replay-safe pipeline-run lifecycle.
Conditional validators are reconstructed from prior safe source-fetch history,
concurrent same-run outcomes are serialized to one durable winner, and terminal
replay and endpoint-mutation retry behavior are implemented. No Task 008 has
been defined.

## Completed work

| Task | Capability | PR | Status |
| --- | --- | --- | --- |
| 001 — Runnable FastAPI service foundation | FastAPI, health check, Python tooling, Alembic wiring, and container foundation | [#1 — Build service foundation](https://github.com/mbuckingham74/jobs/pull/1) | Implemented and squash-merged |
| 002 — Phase 1 core schema | PostgreSQL 16, pgvector, and the initial Phase 1 schema migration | [#2 — Build Phase 1 core schema](https://github.com/mbuckingham74/jobs/pull/2) | Implemented and squash-merged |
| 003 — Phase 1 portfolio résumé bootstrap | Conditional PDF import, extraction validation, versioning, initial activation, and bootstrap CLI | [#3 — Build portfolio resume bootstrap](https://github.com/mbuckingham74/jobs/pull/3) | Implemented and squash-merged |
| 004 — Phase 1 Greenhouse adapter | Shared ATS contract and bounded, deterministic Greenhouse adapter with fixtures | [#4 — Build Greenhouse adapter](https://github.com/mbuckingham74/jobs/pull/4) | Implemented and squash-merged |
| 005 — Phase 1 Lever adapter | Bounded, paginated global/EU Lever adapter with fixtures | [#5 — Build Lever adapter](https://github.com/mbuckingham74/jobs/pull/5) | Implemented and squash-merged |
| 006 — Phase 1 idempotent ATS ingestion | Canonical hashing, posting/version persistence, durable current-version tracking, replay-safe attempts, and safe reconciliation | [#6 — Build idempotent ATS ingestion](https://github.com/mbuckingham74/jobs/pull/6) | Implemented and squash-merged |
| 007 — One-endpoint fetch-to-ingestion runner | Endpoint snapshot, adapter invocation, conditional validators, Task 006 delegation, run ownership and replay, and durable finalization | [#8 — Build one-endpoint fetch-to-ingestion runner](https://github.com/mbuckingham74/jobs/pull/8) | Implemented and squash-merged |

## Phase 1 progress

### Completed

- Runnable FastAPI service and lightweight health endpoint
- Alembic-managed Phase 1 schema on PostgreSQL 16 with pgvector
- Versioned portfolio résumé bootstrap and initial activation workflow
- Shared ATS adapter contract
- Greenhouse adapter with recorded synthetic fixtures
- Lever global/EU adapter with recorded synthetic fixtures
- Deterministic posting canonicalization and SHA-256 content hashing
- Idempotent endpoint-attempt ingestion and replay
- Immutable posting versions with durable current-version selection
- Safe incomplete-fetch, close, and reopen behavior
- Suspicious-zero fail-closed protection and endpoint transition signaling
- Manually invoked Greenhouse/Lever one-endpoint fetch-to-ingestion execution
- Conditional-validator reconstruction from safe persisted fetch history
- Runner-owned manual pipeline-run creation, strict resumption, and terminal replay
- Serialized durable attempt-versus-runner-failure outcome selection
- Endpoint snapshot recheck and retry-safe deletion/kind-change handling

### Deferred

Ashby is deliberately deferred until the merged Greenhouse and Lever path works
end to end. The specification expressly permits cutting Ashby if Phase 1 begins
dragging.

### Remaining

- Bounded multi-endpoint or daily pipeline orchestration, including eligible
  endpoint selection, per-endpoint invocation, aggregate lifecycle, and partial
  failure reporting
- Deterministic filters
- Deep scoring
- Frozen zero-to-three queue
- CLI and JSON output
- Notification interface and initial ntfy transport

These are capability groups, not proposed task boundaries. One group may span
multiple tasks, and one task may cover more than one closely related group.

## Likely next decision

The leading next decision is whether the next bounded task should:

1. add deterministic hard filters over persisted postings; or
2. add the smallest multi-endpoint orchestration layer over the merged Task 007
   callable.

Filters advance posting quality and prepare later scoring; multi-endpoint
orchestration advances daily operational coverage across the hand-picked
company set. This remains a decision to make, not a defined Task 008.

## Later phases

- **Phase 2 — calibration:** model-quality evaluation and validation against
  labelled postings.
- **Phase 3 — usable surface:** Today and Tracker user interfaces, actions, and
  outcome tracking.
- **Phase 4 — compounding:** discovery workflows and broader ATS coverage.
- **Phase 5 — resilience:** operational observability, recovery, backup,
  retention, and performance hardening.

## Maintenance rule

Update this document:

- after a task definition is approved;
- after its implementation PR is merged; and
- when roadmap work is deliberately deferred, cut, or reordered.

Resolve discrepancies in favor of the repository, merged PRs, approved task
files, and specification—not this summary.
