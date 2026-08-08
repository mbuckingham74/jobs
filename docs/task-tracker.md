# Jobs Task Tracker

Lightweight, hand-maintained companion to [project-status.md](project-status.md).
It tracks only the current task and the completed record; it does not predict
the entire project. Discrepancies resolve in favor of the repository, merged
PRs, approved task files, and `docs/jobs-forkstech-spec.html`.

## Current Focus

| Field | Value |
| --- | --- |
| Task ID | T008 |
| Title | Deterministic Phase 1 Hard Filters |
| Status | COMPLETE |
| Reference documents | `docs/jobs-forkstech-spec.html` (sections 02–04, 07, 08, 13, 14); `tasks/008-deterministic-hard-filters.md` |
| Acceptance criteria source | `tasks/008-deterministic-hard-filters.md`, "Acceptance criteria" section |

## Completed Tasks

### T008 — Deterministic Phase 1 Hard Filters

- Status: COMPLETE
- Completion commit: `5882c7e`
- Acceptance: Independent Sonnet QA PASS

Outcome summary:

- deterministic foundation — the approved `phase1-hard-filters-v1` policy,
  normalization, and nine-rule evaluation are implemented as specified;
- pure evaluator — rule evaluation reads and writes no database and performs
  no network or model calls;
- migration/schema contract — `0004_hard_filter_evaluation` creates only the
  approved table, constraints, and indexes with a reversible downgrade;
- repository persistence — the repository owns transaction boundaries, the
  exact lock sequence, and deterministic cleanup on every exit;
- atomic H/S projection — evaluation and filter-stage score insert as one
  atomic persistence unit;
- replay validation — persisted evaluation/score pairs are revalidated
  exactly and conflicts raise the closed `filter.evaluation_conflict` error;
- concurrency handling — identical concurrent calls converge on one
  evaluation and one score projection, with rollback-and-replay race
  recovery;
- provenance validation — `observed_in_run_id` and digest-to-score
  relationships are rechecked before persistence.

## Next Task

**PLACEHOLDER — not yet defined.**

The next bounded implementation task must be defined before it starts. It is
not defined here in advance. It should be derived from:

- remaining requirements in `docs/jobs-forkstech-spec.html` (Phase 1
  candidates currently include multi-endpoint orchestration, deep scoring,
  the frozen zero-to-three queue, CLI and JSON output, and the notification
  interface with ntfy);
- the current implementation state recorded in `docs/project-status.md`; and
- any newly discovered constraints that surfaced during T008.

Define it by copying the template below into `tasks/`, getting it approved,
and then filling in this section.

## Task Queue

| ID | Task | Status | Notes |
| --- | --- | --- | --- |
| T008 | Deterministic Phase 1 Hard Filters | COMPLETE | Accepted artifact `5882c7e` |

No speculative future tasks are listed. Rows appear only when a task is
defined or completed.

## Task Definition Template

Copy this block into a new `tasks/NNN-*.md` file and fill every field before
submitting it for approval.

```text
Task ID: T0XX
Title: <short capability title>
Source requirement: <spec section or clause this task implements>
Scope: <approved boundary: what is included and what is explicitly out>
Acceptance criteria: <checklist the implementation must satisfy>
Implementation checkpoints: <reviewable artifacts along the way>
Independent QA requirements: <how the completed artifact is reviewed>
```
