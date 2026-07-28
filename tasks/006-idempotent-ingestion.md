# Task 006: Phase 1 idempotent ATS ingestion

**Status:** Approved for implementation

**Depends on:** Task 005 — Phase 1 Lever adapter

**Specification:** `docs/jobs-forkstech-spec.html`, especially sections 03
“Architecture,” 04 “Data model,” 05 “Source layer,” 07 “Deduplication and hard
filters,” 13 “Phase 1,” and 14 source/workflow risks

## Approved product decision

Task 006 defines the database boundary for idempotent ingestion. Ashby is
deferred until the merged Greenhouse and Lever path works end to end. This task
must not add, seed, probe, select, or call an Ashby adapter.

## Outcome

Implement one narrow ingestion service that:

1. accepts one existing `pipeline_run`, one existing `source_endpoint`, and one
   already-produced `FetchResult`;
2. records one immutable endpoint-attempt result in `source_fetch`;
3. persists observed endpoint-local postings;
4. inserts only content versions not previously stored for each posting;
5. reopens positively observed postings under the approved ordering rule;
6. closes missing postings only under the exact authoritative reconciliation
   predicate; and
7. supports one narrow transport-failure persistence operation when no
   `FetchResult` exists.

All writes for one endpoint attempt are atomic. This task adds the single
approved `0003_posting_current_version` migration so the durable posting row
can identify which distinct content version is current, including after a
historical-hash reversion.

## Inputs and callable boundary

Expose callables equivalent to:

```python
ingest_fetch_result(
    *,
    run_id: int,
    source_endpoint_id: int,
    fetch_result: FetchResult,
    started_at: datetime,
    finished_at: datetime,
) -> IngestionResult

record_fetch_failure(
    *,
    run_id: int,
    source_endpoint_id: int,
    started_at: datetime,
    finished_at: datetime,
    reason_code: TransportFailureCode,
) -> IngestionResult
```

Before either function performs a replay lookup, validate only that:

- `run_id` is a positive, non-Boolean integer;
- `source_endpoint_id` is a positive, non-Boolean integer;
- both timestamps are timezone-aware; and
- `finished_at >= started_at`.

Do not inspect `fetch_result` or `reason_code`. Perform a read-only lookup by
`(run_id, source_endpoint_id)`. On a hit, return replay; its foreign keys prove
both targets. Only on a miss, require both rows and validate the input. For a
new attempt, both functions:

- obtain `company_id` only from the locked endpoint row;
- use the supplied timestamps for every persisted event and observation;
- return a closed typed result without terminating the process; and
- perform no network, adapter selection, retry, sleep, scheduling, or alert
  delivery.

`TransportFailureCode` is a closed enum containing exactly
`adapter.connect_timeout`, `adapter.read_timeout`, `adapter.write_timeout`,
`adapter.pool_timeout`, `adapter.connect_error`, `adapter.read_error`,
`adapter.write_error`, `adapter.protocol_error`, `adapter.tls_error`, and
`adapter.transport_error`. Every value is ASCII and at most 64 characters.
Persist exactly
`{"code":"<enum value>"}`. Reject unknown values; do not truncate or redact
arbitrary text into an accepted value. The failure callable must not accept an
exception, URL, endpoint token, request, response, headers, body, stack trace,
or nested error details. It only records a failed `source_fetch` for the
supplied run and endpoint.

`IngestionResult` has exactly these representation-independent fields:

- persisted attempt: `source_fetch_id`, `run_id`, `source_endpoint_id`,
  `source_fetch_status`, `http_status`, `postings_seen`,
  `source_fetch_postings_new`, `source_fetch_postings_changed`, `reason_code`;
- call effects: `postings_created`, `postings_updated`, `versions_created`,
  `postings_closed`, `postings_reopened`, `created_posting_version_ids`;
- execution: `reconciliation_ran`, `replayed`; and
- endpoint transition: `endpoint_marked_failing`, `endpoint_marked_active`,
  `alert_required`, `alert_code`.

No additional representation-dependent result field is added. Durable
current-version state lives on `posting.current_version_id`.

## Authoritative semantics

The frozen types in `api/app/sources/ats/contracts.py` remain unchanged.
Ingestion imports those contract types, not the Greenhouse or Lever modules.

Posting identity is exactly:

```text
(source_endpoint_id, RawPosting.external_id.strip())
```

Preserve external-ID case and internal text. Do not Unicode-normalize or derive
an identity, and do not deduplicate identity across endpoints.

Only after minimal callable validation, a read-only replay miss, and
confirmation that both referenced rows exist, fully validate and canonicalize
a new observation before its write phase:

- `FetchResult.complete` is exactly a Boolean;
- `http_status` is a non-Boolean integer from 100 through 599;
- `postings` is a list containing only `RawPosting`;
- validators and optional posting metadata are strings or null;
- `complete=True` requires `http_status == 200`;
- a `304` requires `complete=False` and an empty posting list;
- required posting fields have their declared string types;
- source timestamps are null or timezone-aware;
- locations are `RawLocation` values with string-or-null fields;
- each trimmed external ID, canonical title, and canonical location label is
  non-empty;
- canonical external IDs are unique within the result;
- each `raw` value is a JSON object with string keys containing only nulls,
  Booleans, strings, finite numbers, lists, and objects recursively; and
- canonical serialization completes.

Reject bytes, datetimes, tuples, custom objects, non-string object keys, NaN,
and infinity. Duplicate external IDs invalidate the observation even when the
duplicate rows are identical.

A validation failure writes one failed `source_fetch` with
`error={"code":"ingestion.invalid_observation"}` when both foreign-key targets
exist. Persist `http_status` only when it is a valid non-Boolean integer from
100 through 599; otherwise persist null. Persist `etag` and `last_modified`
independently and exactly when each is an observed string; otherwise persist
null. Never copy an earlier validator.

Persist `postings_seen`, `postings_new`, and `postings_changed` as zero. Return
zero `postings_created`, `postings_updated`, `versions_created`,
`postings_closed`, and `postings_reopened`, plus empty
`created_posting_version_ids`. Perform no other domain mutation.

The failed fetch is the only domain write in the same endpoint-attempt
transaction, not a separate transaction, and commits only with that
transaction. A missing target or database failure writes nothing.

Deep-copy validated raw payloads before persistence. Later mutation by the
caller must not alter `posting_version.raw_payload`.

Count definitions:

- `postings_seen`: validated unique postings in the accepted result, including
  unchanged and reopened postings. Persist it in `source_fetch`; invalid input,
  `304`, and transport failure persist zero.
- `postings_new`: `posting` rows inserted by the first execution. Persist it in
  `source_fetch` and expose it as `source_fetch_postings_new`.
- `postings_changed`: pre-existing postings for which the first execution
  changes `posting.current_version_id`, whether the selected version is newly
  inserted or is a previously stored historical hash. Persist it in
  `source_fetch`; a new posting is not also changed. Expose it as
  `source_fetch_postings_changed`.
- `postings_created`: posting rows inserted by this callable invocation.
- `postings_updated`: pre-existing posting rows whose stored columns change
  during this invocation, counted once per posting even if multiple columns
  change.
- `versions_created`: posting-version rows inserted by this invocation;
  `created_posting_version_ids` is their sorted ID list on first execution.
- `postings_closed`: rows changed from `closed_at IS NULL` to
  `closed_at = finished_at` during this invocation.
- `postings_reopened`: rows changed from non-null `closed_at` to null by the
  invocation.

An unchanged-current-hash or reopen-only observation increments neither new nor
changed. Replay changes no persisted count, and every replay call-effect count
is zero.

## Transaction and concurrency boundary

After a replay miss, `ingest_fetch_result` fully validates and canonicalizes
`FetchResult`, calculates hashes, defensively copies raw payloads, and prepares immutable values before one short PostgreSQL transaction:

1. lock the `source_endpoint` row with `FOR UPDATE`;
2. verify the `pipeline_run`;
3. recheck `source_fetch` by `(run_id, endpoint_id)`;
4. if it finds a race winner, return the defined replay without a write;
5. otherwise execute the approved ordering and ingestion behavior;
6. insert exactly one `source_fetch`; and
7. commit atomically before returning.

The endpoint row lock serializes cooperative ingestion for one endpoint.
Different endpoints lock different rows. The existing posting-identity and
posting-version unique constraints remain race backstops. An unresolved
integrity race rolls back the complete endpoint attempt and returns a bounded
database-conflict code. Do not retry automatically.

The recheck is mandatory because another caller may commit after the read-only
lookup. Discard a race loser's preparation without any domain effect.

The ordering predicate is true only when every previously persisted
`source_fetch` for the endpoint has non-null
`finished_at <= current.started_at`.

If any prior endpoint fetch has null `finished_at` or
`finished_at > current.started_at`:

- persist the current result as `status='incomplete'`;
- preserve its validated `postings_seen`;
- persist new and changed as zero;
- set `error={"code":"ingestion.stale_observation"}`; and
- perform no posting, version, reopen, close, or endpoint-status mutation.

This rule uses endpoint serialization and existing
`source_fetch.started_at`/`finished_at`. `pipeline_run.started_at` does not
participate. Return a replay before evaluating the predicate.

For an ordering-eligible positive observation, set:

```text
last_seen_at = greatest(existing.last_seen_at, finished_at)
```

Reopen only a row whose `closed_at` is non-null and
`finished_at >= closed_at`.

## Content canonicalization and hashing

Canonicalization is pure and locale-independent:

- **Title:** apply Unicode NFKC; replace each maximal run of code points for
  which Python `str.isspace()` is true with one ASCII space; trim; require a
  non-empty result. Store that display title. `title_norm` is its Unicode
  `casefold()`.
- **Description Markdown:** normalize CRLF and bare CR to LF, then apply NFKC;
  remove trailing ASCII spaces/tabs per line; collapse three or more
  consecutive empty lines to two; remove leading/trailing empty lines; emit no
  terminal newline. Preserve every other character and line boundary.
- **Locations:** process `label`, `country_code`, `region`, `city`, and
  `workplace_type` in that order. Apply NFKC, collapse `str.isspace()` runs to
  one ASCII space, and trim each non-null string. Convert an empty optional
  field to null; reject an empty label. Preserve case and punctuation. Remove
  exact duplicate objects, then sort by those five fields with null as empty.

Represent every location with exactly all five keys. Represent optional
absence as JSON `null`. Store the sorted list in both `posting.locations` and
`posting_version.locations`.

Hash exactly:

```json
{
  "description_md": "<canonical description>",
  "locations": ["<actual canonical location objects>"],
  "title": "<title_norm>"
}
```

Serialize with `ensure_ascii=False`, recursive lexical object-key sorting,
compact separators `(",", ":")`, and NaN/infinity rejection. Encode the exact
JSON as UTF-8 and return lowercase hexadecimal SHA-256.

Do not include URLs, department, vendor timestamps, raw payload, endpoint or
run IDs, or observation timestamps in the hash.

Required golden vector:

```text
canonical JSON:
{"description_md":"Build things.\n\nShip them.","locations":[{"city":null,"country_code":"US","label":"Remote — US","region":null,"workplace_type":"remote"}],"title":"senior product manager"}

SHA-256:
5695c190b2412ffbbde7ef1728e01bde6fc2a10f1234d189adc265df82b0a8e7
```

NFKC-equivalent text, line-ending variants, location reordering, exact
duplicate locations, and case-only title changes must be hash-stable. A change
to `title_norm`, a canonical location, or canonical Markdown must change the
hash.

## Posting/version persistence behavior

A new identity creates one `posting` with:

- `company_id` from the locked endpoint;
- the supplied endpoint ID and canonical external ID;
- canonical display title, `title_norm`, and locations;
- adapter-provided department, posting/apply URLs, and source timestamps;
- `first_seen_at = last_seen_at = finished_at`;
- `closed_at = null`; and
- null remote, remote-evidence, duplicate, and temporarily
  `current_version_id` fields.

It then inserts the initial `posting_version` and sets
`posting.current_version_id` to that version before transaction commit.

For an existing identity:

- never change company, endpoint, external ID, `first_seen_at`, remote fields,
  or `duplicate_of_id`;
- when observing a new content hash, insert the new version, set
  `current_version_id` to it, and replace title, `title_norm`, locations,
  department, URLs, and source timestamps with the observed projection;
- when observing a historical hash that is not current, reuse that existing
  version, set `current_version_id` back to it, and refresh the same mutable
  posting projection from the current observation without inserting another
  version;
- when the observed hash already belongs to `current_version_id`, leave the
  pointer and mutable projection unchanged except for `last_seen_at` and an
  allowed reopen;
- update `last_seen_at` and reopen only through the ordering rules above; and
- never update or delete an existing `posting_version`.

Each new `posting_version` stores the posting and run IDs, canonical hash,
display title, canonical locations and Markdown, defensive raw JSON,
`observed_at = finished_at`, and null embedding fields.

For `A -> B -> A`, reuse the existing A version, point
`posting.current_version_id` back to A, refresh the mutable posting projection
from the current observation, increment `postings_changed`, and do not
increment `versions_created`. Do not add an observation-history table.

## Complete/incomplete/304/failure reconciliation matrix

| Outcome | `source_fetch` | Posting/version mutation | Close |
| --- | --- | --- | --- |
| Valid, ordering-eligible, complete non-zero `200` | `complete` | Persist observed rows | Run exact predicate |
| Valid, ordering-eligible, complete zero not classified by the approved zero-history rule | `complete` | None observed | Run exact predicate |
| Complete zero classified by the approved zero-history rule | `incomplete` + `ingestion.suspicious_zero` | None | Never |
| Valid, ordering-eligible, incomplete `200` | `incomplete` | Persist observed rows | Never |
| Valid, ordering-eligible, retained rows with final non-`200` | `incomplete` | Persist retained rows | Never |
| `304` | `incomplete`, zero counts | None | Never |
| Ordering predicate false | `incomplete` + `ingestion.stale_observation` | None | Never |
| Invalid observation | `failed`, zero counts | None | Never |
| Transport failure | `failed`, null HTTP status, zero counts | None | Never |

Persist validators exactly as returned, including null when absent. A
transport failure stores null validators. `error` is null for an ordinary
result and otherwise contains only its defined closed code.

The authoritative reconciliation predicate is true only when all conditions
below are true:

- input validation succeeded;
- `complete is True`;
- `http_status == 200`;
- the endpoint ordering predicate is true;
- canonical external IDs contain no duplicate or contradictory identity;
- the approved zero-history rule did not classify the result; and
- this is the first execution of the idempotency key.

When the predicate is true, update rows satisfying all of:

```text
source_endpoint_id = supplied endpoint
closed_at IS NULL
external_id is absent from the canonical observed-ID set
last_seen_at < started_at
```

Set `closed_at = finished_at`. Do not rewrite a non-null `closed_at`.

A complete zero may close every matching open row when the approved
zero-history rule does not classify it. When the eligible prior average is
greater than 20, the first zero is classified and cannot close; therefore
neither member of a later consecutive pair closes.

Endpoint status and alert behavior are:

- when the consecutive-zero rule fires and locked status is not `failing`, set
  it to `failing`; return `endpoint_marked_failing=True`,
  `endpoint_marked_active=False`, `alert_required=True`, and the closed
  zero-volume alert code;
- when that rule fires while the endpoint is already `failing`, do not update
  its status and return `endpoint_marked_failing=False`,
  `endpoint_marked_active=False`, `alert_required=False`, and
  `alert_code=None`;
- a valid, ordering-eligible, complete non-zero `200` is recovery: change
  `failing` to `active` and return
  `endpoint_marked_active=True`, `endpoint_marked_failing=False`,
  `alert_required=False`, and `alert_code=None`;
- every other outcome leaves endpoint status unchanged and returns both
  transition booleans false, `alert_required=False`, and `alert_code=None`;
  and
- replay performs no transition, recovery, or alert.

## Idempotency and replay behavior

The unique constraint on `(source_fetch.run_id, endpoint_id)` is the
idempotency source of truth. The first committed attempt owns that key.

Replay may be found by the initial read-only lookup or locked recheck. Neither
path inspects, validates, canonicalizes, hashes, copies, or compares the later
`FetchResult`; malformed input returns replay, persisted foreign keys prove its
targets, and both paths use the rules below.

When the row already exists:

- do not overwrite it;
- mutate no posting, version, endpoint, or run;
- do not compare the new in-memory input with the original input;
- copy `source_fetch_id`, `run_id`, `source_endpoint_id`,
  `source_fetch_status`, `http_status`, `postings_seen`,
  `source_fetch_postings_new`, `source_fetch_postings_changed`, and
  `reason_code` from the persisted attempt;
- derive sorted `created_posting_version_ids` from versions whose joined
  posting has the endpoint and whose `observed_in_run_id = run_id`;
- return zero `postings_created`, `postings_updated`, `versions_created`,
  `postings_closed`, and `postings_reopened`;
- return `reconciliation_ran=False`, `replayed=True`,
  `endpoint_marked_failing=False`, `endpoint_marked_active=False`,
  `alert_required=False`, and `alert_code=None`.

Thus persisted facts describe the original attempt while replay effects are
zero. Replay repeats no close, reopen, transition, recovery, or alert.

The schema has no observation fingerprint or observed-ID manifest. Reusing the
key with different input is therefore also first-write-wins: ignore the later
input and return the persisted result. A new endpoint observation requires a
new `pipeline_run.id`. A failed first attempt cannot be replaced by a
successful call under the same key.

Same-key calls serialize on the endpoint lock; the second returns the committed
replay. Different-run calls for one endpoint serialize and then evaluate the
ordering predicate. Different endpoints share no ingestion lock.

## Approved schema migration

Add exactly one Alembic revision under `api/migrations/versions/`:

- `revision = "0003_posting_current_version"`;
- `down_revision = "0002_resume_source_state"`;
- `branch_labels = None`, `depends_on = None`.

Its upgrade must:

1. add nullable `posting.current_version_id BIGINT`;
2. create the stable non-unique index
   `posting_current_version_id_idx` on `posting.current_version_id`;
3. add the stable unique constraint
   `posting_version_posting_id_id_key` on
   `posting_version(posting_id, id)`;
4. add the composite ownership foreign key
   `posting_current_version_owner_fkey` from
   `posting(id, current_version_id)` to
   `posting_version(posting_id, id)`;
5. define that foreign key as `DEFERRABLE INITIALLY DEFERRED` with
   `ON DELETE NO ACTION`;
6. backfill each posting that has versions to the latest version ordered by
   `observed_at DESC, id DESC`; and
7. leave `current_version_id` null for legacy postings with no version.

The deferred ownership foreign key permits a new posting to be inserted with a
temporarily null pointer, its initial version to be inserted, and the pointer
to be set before commit, while preventing a pointer to another posting's
version at transaction commit.

The downgrade must be complete and reversible using the same stable names:
drop the composite foreign key, drop the current-version index, drop
`posting.current_version_id`, and drop the composite unique constraint. The
upgrade/backfill/downgrade/upgrade cycle must preserve the defined catalog and
must not mutate `0001_core_schema` or `0002_resume_source_state`.

## Required implementation work

- Add `api/app/ingestion/` with separate pure canonicalization, SQLAlchemy Core
  persistence, typed-result, and service/transaction responsibilities.
- Mirror only the existing tables touched by ingestion. Add no ORM model layer
  or schema creation.
- Add only the approved `0003_posting_current_version` revision and update the
  existing schema-enforcement tests narrowly to recognize its column, index,
  unique constraint, composite ownership foreign key, linear migration graph,
  and reversible backfill.
- Use parameterized statements, explicit transaction ownership, deterministic
  resource cleanup, and bounded database errors.
- Implement the validation, count, ordering, version, reconciliation, replay,
  and approved zero-history behavior defined above.
- Emit the closed alert code only on the transaction that changes a locked
  endpoint from a non-failing status to `failing`; implement the defined
  non-zero recovery transition and never emit an alert on replay.
- Log only safe database IDs, statuses, counts, booleans, and closed codes.
  Never log endpoint tokens, external IDs, posting content or URLs, validators,
  raw payloads, database URLs, SQL parameters, or raw exceptions.

## Required deterministic unit tests

Using invented contract values under the existing network guard, test:

- malformed existing-key input returns replay without validation,
  canonicalization, hashing, or copying; malformed missing-key input follows the invalid-observation rules;
- every validation and contradictory-input rule, including duplicate IDs;
- every title, description, location, serialization, and hash rule;
- the fixed hash vector and every hash inclusion/exclusion boundary;
- JSON compatibility, finite-number rejection, and defensive raw copying;
- every matrix status, error, mutation authorization, and count;
- invalid-result retention/nulling for HTTP status and both validators;
- exact transport-code membership and rejection of arbitrary text;
- every first-execution and replay field listed in `IngestionResult`; and
- log/result redaction using unique sentinels in every sensitive field.

Do not instantiate an ATS adapter or load adapter fixtures.

## Required PostgreSQL integration tests

Use disposable PostgreSQL 16 + pgvector through guarded loopback
`TEST_DATABASE_URL`. Do not use SQLite as evidence. Test:

- first insertion, unchanged later run, new content, the approved `A -> B -> A`
  representation, and raw/URL/department/timestamp-only changes;
- initial `current_version_id` assignment, reuse of the historical A version,
  rejection of a pointer to a version owned by another posting, and nullable
  legacy postings with no versions;
- `0003_posting_current_version` upgrade, latest-version backfill ordered by
  `observed_at DESC, id DESC`, stable catalog names, downgrade to
  `0002_resume_source_state`, and repeat upgrade to head;
- endpoint-local identity and the same external ID on different endpoints;
- exact persisted seen/new/changed and in-memory closed/reopened counts;
- incomplete and partial non-`200` positive observations without closing;
- `304`, invalid observation, and every transport-failure code;
- invalid observations with independently valid and invalid HTTP status, ETag,
  and Last-Modified values, proving zero mutation counts and atomic fetch-only
  commit;
- authoritative non-zero and unclassified-zero reconciliation;
- first and second classified zeros, no close on either, the second-zero
  endpoint transition, one alert, no repeat alert while already failing, and
  non-zero recovery to `active`;
- ordering-eligible reopen and ordering-ineligible no-mutation behavior;
- every close-predicate boundary and preservation of existing close times;
- exact replay, different-input key reuse, failed-key retry, every zero
  call-effect count, stable persisted IDs/counts, and no replayed alert;
- neither replay path duplicates a transition or alert;
- rollback after posting, version, close, reopen, endpoint-status, and
  `source_fetch` mutation points;
- two simultaneous same-key calls produce one first execution and one replay;
  the race loser returns replay from the transaction-level recheck;
- simultaneous different-run calls for one endpoint; and
- independent progress for different endpoints.

Concurrency tests use independent connections and bounded joins. Every test
creates invented prerequisites and cleans up only its own rows.

## Approved human-review decisions

1. **Canonical representation.** The canonical title, Markdown, structured
   location, canonical JSON, UTF-8, and SHA-256 rules above, including the
   golden vector, are approved and frozen for this task.

2. **Hash-stable posting projection.** When the observed hash is already the
   posting's current version, update `last_seen_at` through the ordering rule
   and perform an allowed reopen only. Do not refresh title, locations,
   department, URLs, or source timestamps; create no `posting_version`; and
   increment neither `postings_new` nor `postings_changed`.

3. **Zero-history calculation and order.** Calculate the prior lifetime
   arithmetic mean from endpoint fetches with `status='complete'`,
   `http_status=200`, and non-null
   `finished_at <= current.started_at`. Include legitimate complete zero
   results and exclude the current attempt. Classify a complete zero as
   suspicious when that eligible prior average is greater than 20. Prohibit
   closing on the classified first zero and every consecutive classified zero.
   The second consecutive classified zero transitions a non-failing endpoint
   to `failing` and requests exactly one alert. Suppress repeated alerts while
   already failing. A valid, ordering-eligible, complete non-zero `200`
   recovers `failing` to `active`. Replay performs no transition or alert.

   Consecutiveness uses the immediately preceding endpoint fetch ordered by
   `finished_at` and then ID; it must have `http_status=200`,
   `postings_seen=0`, and
   `error.code='ingestion.suspicious_zero'`.

4. **Historical-hash representation.** Use the minimal durable
   `posting.current_version_id` reference defined in the approved migration
   section. New postings set it to their initial version before commit; a new
   hash sets it to the newly inserted version; `A -> B -> A` reuses and points
   back to the existing A version while refreshing the mutable projection; an
   unchanged current hash leaves the pointer and projection unchanged except
   for `last_seen_at` and an allowed reopen. No observation-history table is
   added.

5. **Counts and replay.** `(run_id, source_endpoint_id)` is first-write-wins.
   Persisted fetch facts describe the original attempt, replay call-effect
   counts are zero, and replay emits no reconciliation, transition, recovery,
   or alert. Different later input under the same key is ignored; a failed
   first attempt cannot be replaced under the same key; a new attempt requires
   a new `pipeline_run.id`. No additional representation-dependent
   `IngestionResult` field is added.

## Out of scope

- adapter or frozen-contract changes;
- network calls, validator selection, retries, and rate limiting;
- discovery, seeding, endpoint probing, or multi-endpoint orchestration;
- run creation/completion or endpoint lifecycle counters other than the
  approved consecutive-zero status transition;
- alert delivery, notification transport, scheduling, or workflow-wide locks;
- filters, remote/geography derivation, semantic deduplication, embeddings,
  models, research, scoring, queues, or digests;
- CLI, FastAPI routes, HTML, or dashboard work;
- migrations or schema changes other than the single approved
  `0003_posting_current_version` revision;
- observation-history tables or any other representation beyond the approved
  current-version pointer; and
- Compose, deployment, authentication, backup, or VPS work.

## Acceptance criteria

- [ ] All five human-review decisions are resolved as approved above and are
      implemented without changing the frozen ATS contract.
- [ ] `0003_posting_current_version` is the sole new revision, revises
      `0002_resume_source_state`, has the specified nullable column, stable
      index, composite unique constraint, deferred composite ownership foreign
      key, deterministic backfill, and complete reversible downgrade.
- [ ] PostgreSQL enforces that `current_version_id` belongs to its posting,
      legacy postings without versions remain nullable, and the
      upgrade/downgrade/upgrade cycle reproduces the catalog.
- [ ] Each first execution commits one `source_fetch`; after minimal validation,
      replay returns it without inspecting input or writing, while a miss validates both targets and prepares the new attempt.
- [ ] The locked transaction rechecks the key; a race loser returns replay
      without a domain write, transition, reconciliation, or alert.
- [ ] Input validation rejects every listed invalid or contradictory shape
      with exact HTTP-status/validator retention or nulling, zero mutation
      counts, and one atomic failed-fetch commit.
- [ ] Canonicalization produces the specified golden JSON and SHA-256 and
      passes every stated stability/change boundary.
- [ ] Every `IngestionResult` field has the exact first-execution and replay
      semantics defined above; replay returns stable persisted facts, zero
      call-effect counts, and no transition or alert.
- [ ] New postings receive one initial version; existing versions remain
      immutable; `current_version_id` identifies the initial, newly inserted,
      or reused historical version and proves which version is current after
      `A -> B -> A` without sorting by version creation time.
- [ ] `postings_changed` counts a pre-existing posting whenever
      `current_version_id` changes, while `versions_created` counts only
      inserted version rows; unchanged-current-hash observations refresh no
      mutable projection field except `last_seen_at` and an allowed reopen.
- [ ] Every matrix row produces its specified fetch status, error code,
      posting/version authorization, and close result.
- [ ] Close reconciliation updates exactly the rows satisfying all four
      set-based predicates and never rewrites an existing close timestamp.
- [ ] Reopen changes `closed_at` only for an ordering-eligible positive
      observation with `finished_at >= closed_at`.
- [ ] Neither classified zero closes postings; an alert occurs only on a
      non-failing-to-`failing` transition, later zeros while failing do not
      alert, a valid complete non-zero recovery changes `failing` to `active`,
      and replay performs none of those actions.
- [ ] Ordering-ineligible, `304`, invalid, and transport-failure attempts
      perform no posting, version, reopen, close, or endpoint-status mutation.
- [ ] Same-key concurrency creates one result; same-endpoint concurrency obeys
      the ordering predicate; different endpoints do not share a lock, and
      both replay paths suppress duplicate transitions and alerts.
- [ ] A database error at every tested mutation point rolls back the complete
      endpoint attempt.
- [ ] Logs and results omit every prohibited sensitive value.
- [ ] Ruff, deterministic tests, PostgreSQL tests, adapter regressions, and the
      complete test suite pass without live network access.
- [ ] The implementation changes only the approved migration, ingestion paths,
      focused tests, minimal test cleanup, and existing schema-enforcement
      tests, and adds no adapter, workflow, API, CLI, or infrastructure
      behavior.

## Validation commands

```bash
cd api
test -x .venv/bin/python || python3.13 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest -m "not postgres"
.venv/bin/python -c "from app.main import app; assert app is not None"

.venv/bin/pytest -m "not postgres" \
  tests/test_ingestion_canonical.py \
  tests/test_ingestion_service.py \
  tests/test_ingestion_logging.py

.venv/bin/pytest -m "not postgres" \
  tests/test_ats_contracts.py \
  tests/test_greenhouse_adapter.py \
  tests/test_lever_adapter.py
cd ..
```

Use a dedicated disposable `pgvector/pgvector:pg16` container on an ephemeral
loopback port. Export only that database as `TEST_DATABASE_URL`, then run:

```bash
cd api
DATABASE_URL="$TEST_DATABASE_URL" .venv/bin/alembic upgrade head
DATABASE_URL="$TEST_DATABASE_URL" .venv/bin/alembic current
.venv/bin/pytest -m postgres
.venv/bin/pytest -m postgres tests/test_ingestion_persistence.py

DATABASE_URL="$TEST_DATABASE_URL" \
  .venv/bin/alembic downgrade 0002_resume_source_state
DATABASE_URL="$TEST_DATABASE_URL" .venv/bin/alembic current
DATABASE_URL="$TEST_DATABASE_URL" .venv/bin/alembic upgrade head
DATABASE_URL="$TEST_DATABASE_URL" .venv/bin/alembic current
.venv/bin/pytest
cd ..
```

Both head checks must report `0003_posting_current_version (head)`. The
downgrade check must report `0002_resume_source_state`. Stop and remove only
the disposable container afterward.

## Repository hygiene

The implementation diff is limited to:

- this approved task file;
- one new `0003_posting_current_version` migration;
- `api/app/ingestion/`;
- focused ingestion tests under `api/tests/`;
- the smallest ingestion-specific cleanup fixture required in
  `api/tests/conftest.py`; and
- existing schema-enforcement tests that must recognize the new migration,
  column, index, unique constraint, and composite foreign key.

```bash
git status --short
git diff --check
git diff --name-only
git diff -- \
  tasks/006-idempotent-ingestion.md \
  api/migrations/versions \
  api/app/ingestion \
  api/tests
git diff --exit-code -- \
  AGENTS.md docs docker-compose.yml api/pyproject.toml api/app/main.py \
  api/app/resume api/app/sources tasks/001-service-foundation.md \
  tasks/002-core-schema.md tasks/003-portfolio-resume-bootstrap.md \
  tasks/004-greenhouse-adapter.md tasks/005-lever-adapter.md
git status --short --ignored
```

No secret, `.env`, real provider payload, endpoint token, private résumé,
database dump, cache, coverage output, or generated artifact may be added.
