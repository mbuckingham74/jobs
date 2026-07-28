# Project Status

## Source of truth

Implementation status is established by the actual repository and merged
GitHub pull requests. Approved, bounded work is defined in `tasks/*.md`, while
`docs/jobs-forkstech-spec.html` defines the product direction and build phases.
This document is a human-readable index, not a replacement for those
authoritative sources.

Implementation baseline reviewed through the T006 squash commit:
`ade6d82cadec14f172f5c4925730b6780883ec79`.

## Current position

The project remains in Phase 1. Tasks 001–006 have been implemented and
squash-merged. Task 006 added the idempotent ATS ingestion boundary. Greenhouse
and Lever results can now be persisted through deterministic canonical hashing,
immutable posting versions, durable current-version selection, replay-safe
endpoint-attempt idempotency, safe close/reopen reconciliation, and fail-closed
incomplete-fetch behavior. Adapter fetching is not yet wired into ingestion,
and no complete pipeline runner exists. No Task 007 has been defined.

## Completed work

| Task | Capability | PR | Status |
| --- | --- | --- | --- |
| 001 — Runnable FastAPI service foundation | FastAPI, health check, Python tooling, Alembic wiring, and container foundation | [#1 — Build service foundation](https://github.com/mbuckingham74/jobs/pull/1) | Implemented and squash-merged |
| 002 — Phase 1 core schema | PostgreSQL 16, pgvector, and the initial Phase 1 schema migration | [#2 — Build Phase 1 core schema](https://github.com/mbuckingham74/jobs/pull/2) | Implemented and squash-merged |
| 003 — Phase 1 portfolio résumé bootstrap | Conditional PDF import, extraction validation, versioning, initial activation, and bootstrap CLI | [#3 — Build portfolio resume bootstrap](https://github.com/mbuckingham74/jobs/pull/3) | Implemented and squash-merged |
| 004 — Phase 1 Greenhouse adapter | Shared ATS contract and bounded, deterministic Greenhouse adapter with fixtures | [#4 — Build Greenhouse adapter](https://github.com/mbuckingham74/jobs/pull/4) | Implemented and squash-merged |
| 005 — Phase 1 Lever adapter | Bounded, paginated global/EU Lever adapter with fixtures | [#5 — Build Lever adapter](https://github.com/mbuckingham74/jobs/pull/5) | Implemented and squash-merged |
| 006 — Phase 1 idempotent ATS ingestion | Canonical hashing, posting/version persistence, durable current-version tracking, replay-safe attempts, and safe reconciliation | [#6 — Build idempotent ATS ingestion](https://github.com/mbuckingham74/jobs/pull/6) | Implemented and squash-merged |

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

### Deferred

Ashby is deliberately deferred until the merged Greenhouse and Lever path works
end to end. The specification expressly permits cutting Ashby if Phase 1 begins
dragging.

### Remaining

- A bounded Greenhouse/Lever fetch-to-ingestion runner or orchestration slice,
  including run/endpoint-attempt lifecycle and conditional-validator reuse
- Deterministic filters
- Deep scoring
- Frozen zero-to-three queue
- CLI and JSON output
- Notification interface and initial ntfy transport

These are capability groups, not proposed task boundaries. One group may span
multiple tasks, and one task may cover more than one closely related group.

## Likely next decision

The leading next decision is the exact boundary for connecting the merged
Greenhouse and Lever adapters to Task 006 ingestion:

- first implement the smallest one-endpoint fetch-to-ingestion runner; or
- define a broader multi-endpoint pipeline-run lifecycle.

This is a decision to make, not an approved Task 007 scope.

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
