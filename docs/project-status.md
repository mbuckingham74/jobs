# Project Status

## Source of truth

Implementation status is established by the actual repository and merged
GitHub pull requests. Approved, bounded work is defined in `tasks/*.md`, while
`docs/jobs-forkstech-spec.html` defines the product direction and build phases.
This document is a human-readable index, not a replacement for those
authoritative sources.

Implementation baseline reviewed through the T005 squash commit:
`537cd4c5f9a1a1bfb24f023a69041d79d2905a5d`.

## Current position

The project is in Phase 1. Tasks 001–005 have been implemented and
squash-merged. The repository now contains the runnable service foundation,
Phase 1 database schema, portfolio résumé bootstrap, Greenhouse adapter, and
Lever adapter. No Task 006 has been defined.

## Completed work

| Task | Capability | PR | Status |
| --- | --- | --- | --- |
| 001 — Runnable FastAPI service foundation | FastAPI, health check, Python tooling, Alembic wiring, and container foundation | [#1 — Build service foundation](https://github.com/mbuckingham74/jobs/pull/1) | Implemented and squash-merged |
| 002 — Phase 1 core schema | PostgreSQL 16, pgvector, and the initial Phase 1 schema migration | [#2 — Build Phase 1 core schema](https://github.com/mbuckingham74/jobs/pull/2) | Implemented and squash-merged |
| 003 — Phase 1 portfolio résumé bootstrap | Conditional PDF import, extraction validation, versioning, initial activation, and bootstrap CLI | [#3 — Build portfolio resume bootstrap](https://github.com/mbuckingham74/jobs/pull/3) | Implemented and squash-merged |
| 004 — Phase 1 Greenhouse adapter | Shared ATS contract and bounded, deterministic Greenhouse adapter with fixtures | [#4 — Build Greenhouse adapter](https://github.com/mbuckingham74/jobs/pull/4) | Implemented and squash-merged |
| 005 — Phase 1 Lever adapter | Bounded, paginated global/EU Lever adapter with fixtures | [#5 — Build Lever adapter](https://github.com/mbuckingham74/jobs/pull/5) | Implemented and squash-merged |

## Phase 1 progress

### Completed

- Runnable FastAPI service and lightweight health endpoint
- Alembic-managed Phase 1 schema on PostgreSQL 16 with pgvector
- Versioned portfolio résumé bootstrap and initial activation workflow
- Shared ATS adapter contract
- Greenhouse adapter with recorded synthetic fixtures
- Lever global/EU adapter with recorded synthetic fixtures

### Remaining

- Ashby adapter with recorded fixtures; the specification permits cutting it
  if Phase 1 begins dragging
- Idempotent ingestion
- Content hashing and posting-version gates
- Safe close and reopen reconciliation
- Deterministic filters
- Deep scoring
- Frozen zero-to-three queue
- CLI and JSON output
- Notification interface and initial ntfy transport

These are capability groups, not proposed task boundaries. One group may span
multiple tasks, and one task may cover more than one closely related group.

## Likely next decision

The next task definition will be selected only after checking the current
repository against the remaining Phase 1 roadmap. The leading decision is
whether to:

- implement Ashby next and complete the planned three-adapter source layer; or
- deliberately cut or defer Ashby and move into ingestion if Greenhouse and
  Lever are sufficient to prove the vertical slice.

This is a decision to make, not an approved Task 006 scope.

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
