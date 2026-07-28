# Task 007: One-endpoint fetch-to-ingestion runner

**Status:** Approved for implementation

**Depends on:** Task 006 — Phase 1 idempotent ATS ingestion

**Specification:** `docs/jobs-forkstech-spec.html`, especially sections 03–05, 13, and 14

## Approved product decision

Task 007 proves the smallest Greenhouse/Lever seam before broader orchestration; Ashby stays deferred.

## Outcome

Implement one manually invokable service binding one persisted
`source_endpoint` to one `pipeline_run`, freezing its source-layer snapshot,
reconstructing validators, invoking Greenhouse or Lever, delegating persistence
to Task 006, and finalizing the run. No database resource spans adapter I/O;
network I/O occurs only in the selected adapter.

## Public callable boundary

Expose a typed boundary equivalent to:

```python
async def run_one_endpoint(
    *,
    engine: Engine,
    source_endpoint_id: int,
    run_id: int | None,
    config_version: str,
    adapters: AdapterRegistry,
    clock: UTCClock,
) -> OneEndpointRunnerResult: ...
```

`run_id=None` creates; a positive ID resumes exactly. Callers pass `None`
explicitly. `source_endpoint_id` is a positive non-Boolean integer.
`config_version` exactly matches `[A-Za-z0-9][A-Za-z0-9._-]{0,127}` without
repair; resumption requires exact equality.

Rows use `run_kind='manual'`; reject another kind. `AdapterRegistry` is frozen
with exactly `greenhouse` and `lever` zero-argument factories returning
matching-slug adapters. No extras, reflection, dynamic imports, entry points,
or name guessing. `UTCClock.now()` solely owns runner/attempt times; normalize
aware zero-offset values to UTC and never use provider timestamps.

`OneEndpointRunnerResult` has exactly:

- `run_id`, `source_endpoint_id`, and `endpoint_kind`;
- `run_status`: `running`, `succeeded`, `partial`, or `failed`;
- `finalized`: Boolean;
- `ingestion_result`: `IngestionResult | None`; and
- `failure_code`: `OneEndpointFailureCode | None`.

Define the closed failure projection exactly:

```python
class IngestionReasonCode(str, Enum):
    INVALID_OBSERVATION = "ingestion.invalid_observation"
    SUSPICIOUS_ZERO = "ingestion.suspicious_zero"
    STALE_OBSERVATION = "ingestion.stale_observation"

type OneEndpointFailureCode = RunnerFailureCode | TransportFailureCode | IngestionReasonCode
```

`RunnerFailureCode` contains exactly the `runner.*` values in the canonical
table below. Reuse, do not duplicate or rename, `TransportFailureCode`.
Convert a non-null Task 006 `reason_code` only by exact enum-value lookup; an
unknown string becomes `runner.ingestion_result_invalid`, never an open-ended
result value. Persist `pipeline_run.error.code` as the selected enum's exact
string value.

Only these closed errors escape before a result: `RunnerInputError` with
`runner.invalid_endpoint_id`, `runner.invalid_run_id`,
`runner.invalid_config_version`, or `runner.invalid_clock`; `RunnerTargetError`
with `runner.pipeline_run_not_found`, `runner.source_endpoint_not_found`, or
`runner.pipeline_run_incompatible`; and `RunnerDatabaseError` with
`runner.database_error` and optional run ID. Each carries its exact
`RunnerFailureCode`, never SQL/text. Post-cleanup `asyncio.CancelledError` also
escapes.

## Runner-level idempotency

The run is the retry handle. For `run_id=None`, insert the new manual run and
its exact reserved counts binding in the same transaction; creation and
reservation are atomic, so a Task 007-created run cannot be unbound. The
unique `(run_id, endpoint_id)` fetch key remains attempt truth. Add one
read-only Task 006 method returning its existing `_replay_result` projection
or `None`, changing no rules or writes.

For a positive `run_id`, take a write-intent row lock and resume it only when
its counts are an exact recognized Task 007 projection with `schema_version=1`.
Use PostgreSQL `FOR NO KEY UPDATE`: it serializes every Task 007 coordinator
while remaining compatible with the `KEY SHARE` lock required by Task 006's
separate `source_fetch` foreign-key insert.
Load and validate that run before consulting the live endpoint. The persisted
endpoint ID and adapter kind, `run_kind='manual'`, and exact
`config_version` must match the invocation. Reject through
`RunnerTargetError(RunnerFailureCode.PIPELINE_RUN_INCOMPATIBLE)` without
mutation when counts are `{}`, null-like, malformed, contain extra/missing
keys, use another schema version/workflow, or bind another endpoint/kind.
Never adopt an apparently unbound existing run.

For a compatible resumed run:

- matching attempt: return Task 006 replay and retry finalization without I/O;
- running without an attempt: continue from the endpoint binding;
- terminal without an attempt: return it only when its owned binding, status,
  error, counts, steps, and timestamps form a valid Task 007 terminal result;
  otherwise raise `runner.pipeline_run_incompatible`; and
- terminal with an attempt: return replay and preserve terminal fields.

A terminal replay derives its endpoint ID and adapter kind from the persisted
binding. It does not require, lock, or validate the live `source_endpoint`,
construct an adapter, call the clock, or write. Only a running run without a
persisted attempt loads the current endpoint and reconstructs validators.

A valid terminal result without an attempt has reserved counts, `status='failed'`,
one exact table-listed run error whose `source_fetch` column is `No` or `No new
row`, the terminal step dictated below, a non-null aware-UTC finish not before
its aware-UTC start, and no Task 006 replay row. Any deviation is incompatible.

A retry after attempt commit performs no network I/O. Every post-adapter or
local no-attempt outcome enters one coordination transaction that locks the
owned run, revalidates its binding, checks the matching attempt and terminal
state, and selects the durable winner before another write. Code already
holding that lock finalizes on the same connection.

Concurrent eligible same-key callers may each call `list_postings()` once
before outcome coordination. The first coordinated durable attempt or valid
no-attempt failure wins; later callers return the persisted winner and ignore
their later local outcome. A no-attempt winner prevents a later Task 006 call.
Cross-process network at-most-once still needs a durable claim or a database
resource held during I/O, so the current schema cannot provide it.

Every `run_id=None` call is a new manual run. Without an external trigger key,
a caller that loses its created ID has no trigger-level idempotency; never
claim otherwise.

## Endpoint eligibility and snapshot

The first transaction reads under a shared lock held only to commit, retaining
`id`, `company_id`, `status`, `kind`, `token`, `region`, and `base_url`.
Construct `SourceEndpoint` from exactly the last four; pass no IDs/status.

| Endpoint value | Runner behavior |
| --- | --- |
| `active` | Eligible |
| `failing` | Eligible so a complete non-zero fetch can recover it in Task 006 |
| `paused` | No `list_postings()`; terminal `runner.endpoint_paused` |
| `retired` | No `list_postings()`; terminal `runner.endpoint_retired` |
| `greenhouse` | Select the exact Greenhouse registry factory |
| `lever` | Select the exact Lever factory; its snapshot region chooses global or EU |
| `ashby` | No `list_postings()`; terminal `runner.adapter_deferred` |
| `workday`, `workable`, `smartrecruiters`, `recruitee` | No `list_postings()`; terminal `runner.adapter_not_implemented` |
| `custom` | No `list_postings()`; terminal `runner.adapter_unsupported` |
| Any value outside schema domains | No `list_postings()`; terminal `runner.endpoint_malformed` |

For a supported kind, the existing adapter owns token, region, and `base_url`
policy. A typed `GreenhouseEndpointError` or `LeverEndpointError` means
`runner.endpoint_policy`; it is not a transport failure.

After return/typed transport failure, re-read under a short shared lock and
compare all seven columns. Missing is `runner.endpoint_deleted`; difference is
`runner.endpoint_changed`; neither calls Task 006. Review covers the residual
race.

## Adapter registry and selection

Validate the frozen registry shape without invoking its factories. Replay calls no adapter factory and no `list_postings()`.
Only an eligible, supported, non-replay invocation selects exactly one adapter: `greenhouse` uses its exact factory; `lever`
uses its one factory, with snapshot region selecting global or EU. Construct it, verify its slug, and call `list_postings()`
exactly once with the exact snapshot and conditional values. A missing/raising factory, incompatible object, or slug mismatch
is `runner.adapter_registry_defect` and calls no `list_postings()`.

Paused, retired, deferred, unsupported, unimplemented, malformed, missing, incompatible-run, and pre-selection
registry-validation outcomes call no `list_postings()`. An endpoint-policy error occurs during that one call for an otherwise
supported endpoint, but the adapter may reject it before network I/O. Registry validation, adapter construction, method
invocation, and actual network I/O are distinct; only the selected adapter can perform network I/O.

No ATS import belongs in `api/app/ingestion/`. Workflow may import the contract
and typed errors. Construction must accept `httpx.MockTransport` or smaller
protocol fakes; no test contacts a provider.

## Conditional-validator reuse

Reconstruct each validator independently from the endpoint's prior rows.
Exclude this run/null finishes; order `finished_at DESC, id DESC`.

An eligible event is only:

- `status='complete'`, `http_status=200`, and `error IS NULL`; or
- `status='incomplete'`, `http_status=304`, and `error IS NULL`.

For `etag`, candidates are every eligible `200` plus eligible `304` rows with
non-null `etag`; select first, including null from `200`. Apply independently
to `last_modified`.

Thus absent on later `200` clears, absent on `304` preserves, and present on
`304` replaces. Values may come from different rows.

Failed, invalid, stale, suspicious-zero, incomplete `200`, `429`, other
statuses, and null-finished attempts never supply values. No candidate means
null. Never use arbitrary latest-non-null, another endpoint, payload, or new
cache state.

## Adapter error mapping

Map only these exact, complete typed transport categories:

| Exact adapter category | `TransportFailureCode` |
| --- | --- |
| `greenhouse.transport.connect_timeout` | `adapter.connect_timeout` |
| `greenhouse.transport.read_timeout` | `adapter.read_timeout` |
| `greenhouse.transport.connect_error` | `adapter.connect_error` |
| `greenhouse.transport.protocol_error` | `adapter.protocol_error` |
| `greenhouse.transport_error` | `adapter.transport_error` |
| `lever.transport.connect_timeout` | `adapter.connect_timeout` |
| `lever.transport.read_timeout` | `adapter.read_timeout` |
| `lever.transport.write_timeout` | `adapter.write_timeout` |
| `lever.transport.pool_timeout` | `adapter.pool_timeout` |
| `lever.transport.connect_error` | `adapter.connect_error` |
| `lever.transport.read_error` | `adapter.read_error` |
| `lever.transport.write_error` | `adapter.write_error` |
| `lever.transport.protocol_error` | `adapter.protocol_error` |
| `lever.transport_error` | `adapter.transport_error` |

Match exactly against the complete category string; suffix, prefix, substring, regex, and cause-based matching are forbidden.
An unknown category on a typed transport exception is `runner.adapter_defect`. Neither merged adapter publishes a distinct TLS
category, so do not manufacture `adapter.tls_error`. Map a recognized typed transport error through Task 006
`record_fetch_failure()`.

Endpoint errors map to `runner.endpoint_policy`. Unsupported kinds never
construct an adapter. `asyncio.CancelledError` follows the cancellation rule.
Any other `Exception` from a factory or adapter maps to
`runner.adapter_defect`; do not catch `BaseException`.

Task 006 has no callable for endpoint-policy or adapter-defect failures. The
approved behavior records those closed failures on `pipeline_run` only and
creates no `source_fetch`.

## Transaction and timestamp boundaries

Use four bounded phases:

1. Short transaction: atomically create/reserve or verify the owned run,
   checking terminal/attempt state from the persisted binding before requiring
   the live endpoint; snapshot and validators are needed only for a running
   run without an attempt.
2. With no database resource open: clock, await adapter, clock again.
3. Short transaction: revalidate all seven endpoint columns, then release its
   endpoint lock.
4. Coordination transaction: lock only the owned `pipeline_run`, revalidate
   ownership, check the matching `source_fetch` and terminal state, then
   resolve the durable winner. A running attempt candidate invokes Task 006
   while retaining only this run lock; Task 006 keeps its separate atomic
   transaction and remains the sole `source_fetch` writer. Re-read and
   reconstruct persisted attempt facts, then finalize with the already-locked
   connection.

No database connection, transaction, lock, result object, or session spans
adapter I/O. Do not hold an endpoint lock across Task 006. Preserve the narrow
race after the seven-column endpoint recheck.

Capture run start before new-row insertion and run finish for finalization;
resumption/replay preserves both. Attempt times exist only for a network call.

Require `run.started_at <= attempt.started_at <= attempt.finished_at <=
run.finished_at`. Invalid/non-UTC/backward output before binding raises the
closed input error; after binding it finalizes `runner.clock_invalid`. Neither
reaches Task 006.

Phase-one failure rolls back creation and performs no I/O. A Task 006
database/target/input failure finalizes failed when no attempt committed. If
Task 006 committed but finalization fails, only coordination/finalization
rolls back; the run remains running and retry discovers the committed attempt.
A coordination finalization failure returns `finalized=False`,
`run_status='running'`, `runner.finalization_database_error`, and the retryable
run ID.

## Pipeline-run lifecycle and status matrix

The bound initial row is:

- `run_kind='manual'`, `status='running'`, exact `config_version`;
- `last_completed_step=NULL`, `error=NULL`;
- `started_at` from the UTC clock and `finished_at=NULL`; and
- counts in the reserved form defined below.

Terminal steps are exact: `source_fetch_recorded` when Task 006 returned an
attempt, `adapter_fetch_completed` when a returned fetch could not reach Task
006, and `endpoint_snapshotted` for failures or cancellation before an adapter
result.

| Outcome | Run status | Error code |
| --- | --- | --- |
| Complete non-zero `200` | `succeeded` | null |
| Legitimate complete zero `200` | `succeeded` | null |
| Incomplete result with positive retained rows | `partial` | reason code or `runner.fetch_incomplete` |
| `304` with no Task 006 reason | `succeeded` | null |
| `ingestion.suspicious_zero` | `partial` | exact ingestion code |
| `ingestion.stale_observation` | `partial` | exact ingestion code |
| Other incomplete result with zero rows | `failed` | reason code or `runner.fetch_incomplete` |
| `ingestion.invalid_observation` | `failed` | exact ingestion code |
| Mapped transport failure | `failed` | exact transport code |
| Endpoint-policy, eligibility, registry, unsupported-kind, or mutation failure | `failed` | exact runner code |
| Cancellation before attempt persistence | `failed`, then propagate | `runner.cancelled` |
| Invalid clock after run binding | `failed` | `runner.clock_invalid` |
| Ingestion database, input, or target failure | `failed` | `runner.ingestion_database_error`, `runner.ingestion_input_error`, or `runner.ingestion_target_error` |
| Finalization database failure | remains `running` | not persisted |
| Replay of a persisted attempt | derive from persisted row using this same matrix | same deterministic code |

`pipeline_run.error` is null where shown, otherwise exactly `{"code":"..."}`.
`source_fetch.status='incomplete'` alone never selects run status: `304`
succeeds and positive observations are partial.

### Canonical runner failure codes

Every row is a `RunnerFailureCode`; no other `runner.*` string is valid. “Run
error” is persisted and returned; “Return-only” preserves the prior run error.

| Code | Exposure | Producing outcome | Matching `source_fetch` |
| --- | --- | --- | --- |
| `runner.invalid_endpoint_id` | `RunnerInputError` | Invalid endpoint ID before binding | No |
| `runner.invalid_run_id` | `RunnerInputError` | Invalid positive/nullable run ID | No |
| `runner.invalid_config_version` | `RunnerInputError` | Invalid config string | No |
| `runner.invalid_clock` | `RunnerInputError` | Invalid clock before binding | No |
| `runner.pipeline_run_not_found` | `RunnerTargetError` | Positive run ID absent | No |
| `runner.source_endpoint_not_found` | `RunnerTargetError` | Endpoint absent before binding | No |
| `runner.pipeline_run_incompatible` | `RunnerTargetError` | Existing run is foreign, malformed, or mismatched | No owned matching row |
| `runner.database_error` | `RunnerDatabaseError` | Database failure before a result can form | No new row |
| `runner.endpoint_paused` | Run error | Paused endpoint | No |
| `runner.endpoint_retired` | Run error | Retired endpoint | No |
| `runner.adapter_deferred` | Run error | Ashby endpoint | No |
| `runner.adapter_not_implemented` | Run error | Known unimplemented kind | No |
| `runner.adapter_unsupported` | Run error | Custom kind | No |
| `runner.endpoint_malformed` | Run error | Row violates schema-domain assumptions | No |
| `runner.endpoint_policy` | Run error | Typed adapter endpoint rejection | No |
| `runner.endpoint_deleted` | Run error | Endpoint deleted after snapshot | No |
| `runner.endpoint_changed` | Run error | Endpoint snapshot mismatch | No |
| `runner.adapter_registry_defect` | Run error | Missing/raising/bad-slug factory | No |
| `runner.adapter_defect` | Run error | Unexpected adapter error/category | No |
| `runner.clock_invalid` | Run error | Invalid clock after binding | No |
| `runner.ingestion_database_error` | Run error | Task 006 database exception | No new row |
| `runner.ingestion_input_error` | Run error | Task 006 input exception | No new row |
| `runner.ingestion_target_error` | Run error | Task 006 target exception | No new row |
| `runner.ingestion_result_invalid` | Run error | Invalid Task 006 result or unknown non-null reason code | Yes, or no new row from a defective result |
| `runner.fetch_incomplete` | Run error | Incomplete result without a narrower reason | Yes |
| `runner.cancelled` | Run error, then `CancelledError` | Cancellation before attempt persistence | No |
| `runner.finalization_database_error` | Return-only | Finalization transaction fails | May |
| `runner.finalization_conflict` | Return-only | Existing terminal projection differs | May |

## Counts and typed result

Store this exact `pipeline_run.counts` projection:

```json
{
  "schema_version": 1,
  "endpoint_id": 123,
  "adapter_kind": "greenhouse",
  "attempt_recorded": true,
  "source_fetch_id": 456,
  "http_status": 200,
  "postings_seen": 12,
  "postings_new": 3,
  "postings_changed": 2
}
```

The recognized Task 007 projection has exactly the nine keys shown and no
others. Its reserved form uses `schema_version=1`, the bound endpoint/kind,
`attempt_recorded=false`, null `source_fetch_id`/`http_status`, and three zero
posting counts. Integers are non-negative, Booleans are JSON Booleans, and
absent facts are JSON null. Creation writes this complete object atomically
with the new run.

An attempt form has `attempt_recorded=true` and copies every attempt fact under
the rules below. A valid terminal result without a `source_fetch` retains the
reserved form. Any other shape is unrecognized and cannot be resumed.

After Task 006, require the returned run ID and endpoint ID to match the owned
binding, require the matching persisted attempt, and use only persisted-fact fields:
`source_fetch_id`, `http_status`, `postings_seen`,
`source_fetch_postings_new`, and `source_fetch_postings_changed`. Never
recompute from posting tables. Build counts `endpoint_id` from the persisted
Task 007 binding, never an unverified returned value. A mismatched returned
result is `runner.ingestion_result_invalid` and cannot redirect the run.

Finalization replay writes the same projection. Do not store invocation-local
`replayed`, close/reopen/version call effects, endpoint transitions, or alert
flags because Task 006 does not persist enough information to reconstruct
those on replay. Do not double count. The returned `ingestion_result` may
still expose those flags for the current call.

The reservation freezes safe `adapter_kind`; never record token, region, base URL, or provider URL.
Do not add funnel, scoring, token, search, cost, or notification counts.

## Cancellation, cleanup, and observability

Catch `asyncio.CancelledError` around the await. After adapter cleanup, attempt
one short failed finalization with step `endpoint_snapshotted`, code
`runner.cancelled`, and UTC finish, then propagate the original cancellation
even if finalization fails. Never convert, shield, retry, or catch
`BaseException`.

Context managers close/rollback database resources. Adapters close owned
clients; the registry owns injected instances/clients, so the runner calls no
undeclared close method.

Logs may contain only database IDs, adapter slug, closed codes, persisted
counts, replay/transition/alert Booleans, and safe duration. Never log tokens,
names, external IDs, URLs, validators/headers, posting fields/payloads,
provider bodies, raw errors/stacks, database URLs/SQL parameters, candidate or
résumé data, prompts, or identity data.

## Required implementation work

- Add `api/app/workflow/` with small result, repository/transaction, registry,
  and orchestration modules.
- Add only the read-only Task 006 replay lookup; change no write/replay rule.
- Implement every binding, snapshot/recheck, validator, selection, delegation,
  finalization, and logging rule above.
- Use SQLAlchemy Core, parameterized statements, context-managed connections,
  and the synchronous `Engine`; add no ORM/async database layer.
- Return any proven schema blocker to review before adding a migration.

## Required deterministic unit tests

Using invented endpoints and injected protocol-compatible adapters:

- every input, run/config/registry/clock/result boundary and exact frozen endpoint construction;
- exact `RunnerFailureCode`, `TransportFailureCode`, and `IngestionReasonCode` projection, plus every unknown-string rejection;
- Greenhouse, Lever global/EU, every status/kind eligibility branch, malformed field, and exact factory/construction behavior;
- no history and complete, incomplete, failed, `304`, stale, suspicious, null-finished, clearing/preserving, and tied validator histories;
- exact adapter arguments and cardinality: eligible supported non-replay gets one factory and one `list_postings()` call; replay
  and every listed exclusion get neither, while endpoint policy gets one call that may perform no network;
- every lifecycle row, actual transport mapping, unknown category, policy, registry, unexpected defect, cancellation, and cleanup result;
- no retained database resource while a blocking fake awaits;
- atomic creation/reservation; rejection without mutation of unbound, `{}`-counts, foreign-version, malformed, mismatched, and
  unrelated manual runs; strict terminal-without-fetch validation;
- retry after attempt/finalization failure, both concurrent winner orders, new
  manual runs, Task 006 replay preservation, endpoint-independent terminal
  replay, terminal timestamp preservation, and fresh-result relationship
  validation; and
- exact sentinel redaction for every prohibited field.

Use `httpx.MockTransport` only at real adapter boundaries and smaller fakes
otherwise. No test resolves DNS, opens a provider socket, or needs internet.

## Required PostgreSQL integration tests

Use disposable PostgreSQL 16 + pgvector through the guarded loopback
`TEST_DATABASE_URL`; SQLite is not evidence. Test:

- atomic creation/reservation, recognized resumption, terminal replay, lifecycle fields, counts types, eligibility, and failing recovery;
- an unrelated existing manual run, `{}` counts, foreign/malformed counts, and endpoint/kind/config mismatch cannot be claimed or mutated;
- every eligible/excluded validator history and Task 006 outcome;
- endpoint deletion/change and zero database resources during adapter await;
- exact factory, `list_postings()`, and network-I/O cardinality across eligibility, replay, policy, registry validation, and concurrency;
- attempt/finalization replay without double count;
- deterministic concurrent attempt-first and no-attempt-first winners,
  including a local defect and cancellation or endpoint mutation; one winner
  when two endpoints bind one run; and distinct new manual runs;
- terminal replay after endpoint deletion or kind change, with no factory,
  adapter, clock, endpoint dependency, or write;
- rollback/closed behavior for failure in binding, validators, recheck, Task 006, and finalization; and
- privacy-safe stored errors/logs with unique sentinels.

Concurrency tests use independent connections and bounded joins. Every test
creates invented prerequisites and cleans up only its own rows.

## Approved human decisions

| # | Approved decision |
| --- | --- |
| 1 | `run_id=None` atomically creates/reserves a runner-owned manual run; a positive ID resumes only its exact compatible binding. No trigger idempotency is claimed. |
| 2 | Use the per-field validator reconstruction, including `200` clearing, `304` preservation/replacement, and unsafe-row exclusion. |
| 3 | Persist policy, unsupported-kind, registry-defect, and adapter-defect failures on `pipeline_run` only; create no `source_fetch`. |
| 4 | Use the exact terminal-status matrix. |
| 5 | Attempt one best-effort failed finalization, then propagate the original `asyncio.CancelledError`. |
| 6 | Concurrent callers may duplicate the network read; one post-adapter run-lock boundary serializes every durable outcome. A committed Task 006 attempt or a valid no-attempt failure may win, and later callers return that persisted winner. |
| 7 | Use the snapshot/recheck fence and accept the narrow post-recheck race without a migration or Task 006 write-interface change. |

## Out of scope

- multiple endpoints/daily pipeline, scheduling, retries/backoff/sleeping,
  rate coordination, or fan-out
- discovery, creation/seeding/probing/re-resolution, Ashby/another adapter, or
  ATS contract/adapter changes
- Task 006 hashing/persistence/reconciliation/replay-rule changes
- filters, embeddings/deduplication, research/models/scoring, queue/digest
- FastAPI, CLI, notification/alert delivery, deployment/auth/infrastructure
- migration without a proven blocker returned for review

## Acceptance criteria

- [ ] All seven approved human decisions are implemented exactly.
- [ ] The typed boundary, closed results/errors, manual-run/config/UTC rules, and exact factories are implemented.
- [ ] One run binds one endpoint; replay uses its Task 006 key and never claims trigger idempotency.
- [ ] Creation atomically writes the reserved projection; a positive ID resumes only exact Task 007-owned state, and incompatible rows are never mutated.
- [ ] The closed failure union and canonical runner-code table cover every persisted/returned/escaped value and reject arbitrary strings.
- [ ] Eligibility, snapshot/recheck, adapter, validator, and error rules match.
- [ ] Eligible supported non-replay calls one selected adapter's `list_postings()` once; replay and every listed exclusion call none.
- [ ] Cancellation/defects never masquerade as transport failures.
- [ ] No transaction, lock, connection, or session is held during network I/O.
- [ ] Every bound outcome is serialized by the owned run lock; a no-attempt
      winner prevents later Task 006, while an attempt winner overrides later
      local failures.
- [ ] Task 006 remains the sole attempt writer with unchanged semantics.
- [ ] Every matrix row has exact fields/error/counts/result and resumability.
- [ ] Terminal replay is independent of the live endpoint and performs no
      adapter, clock, or write work.
- [ ] Counts use the persisted binding and persisted attempt facts, never
      double count, and cannot point at another run or endpoint.
- [ ] Unit/PostgreSQL tests cover every case without live traffic.
- [ ] Ruff, focused tests, PostgreSQL tests, and the full suite pass.
- [ ] No migration, route, CLI, scheduler, notification, additional adapter, filter, model, queue, deployment, or infrastructure behavior is added.

## Validation commands

```bash
cd api
test -x .venv/bin/python || python3.13 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest -m "not postgres"
.venv/bin/pytest -m "not postgres" \
  tests/test_one_endpoint_workflow.py \
  tests/test_ingestion_service.py \
  tests/test_greenhouse_adapter.py \
  tests/test_lever_adapter.py
.venv/bin/python -c "from app.main import app; assert app is not None"
cd ..
```

With a dedicated disposable `pgvector/pgvector:pg16` database exposed only on
an ephemeral loopback port:

```bash
cd api
DATABASE_URL="$TEST_DATABASE_URL" .venv/bin/alembic upgrade head
DATABASE_URL="$TEST_DATABASE_URL" .venv/bin/alembic current
.venv/bin/pytest -m postgres tests/test_one_endpoint_workflow_postgres.py
.venv/bin/pytest
cd ..
```

Alembic current must remain `0003_posting_current_version (head)`.

Inventory the closed runner codes and compare the output exactly to the
canonical table:

```bash
rg -o 'runner\.[a-z0-9_]+' \
  tasks/007-one-endpoint-fetch-to-ingestion.md | sort -u
```

## Repository hygiene

The future implementation diff is limited to the approved Task 007 task file,
the new workflow package, the narrow read-only ingestion replay seam, focused
runner tests, and the smallest runner-specific cleanup fixture.

```bash
git status --short
git diff --check
git diff --name-only
git diff -- \
  tasks/007-one-endpoint-fetch-to-ingestion.md \
  api/app/workflow \
  api/app/ingestion \
  api/tests
git diff --exit-code -- \
  AGENTS.md docs docker-compose.yml api/pyproject.toml api/migrations \
  api/app/main.py api/app/resume api/app/sources tasks/001-service-foundation.md \
  tasks/002-core-schema.md tasks/003-portfolio-resume-bootstrap.md \
  tasks/004-greenhouse-adapter.md tasks/005-lever-adapter.md \
  tasks/006-idempotent-ingestion.md
git status --short --ignored
```

No secret, `.env`, real endpoint token, captured provider payload, private
résumé, prompt, model request, database dump, cache, coverage output, or
generated artifact may be added.
