# Task 003: Phase 1 portfolio résumé bootstrap

**Status:** Ready

**Depends on:** Task 002 — Phase 1 core schema

**Specification:** `docs/jobs-forkstech-spec.html`, especially section 04
“Candidate résumé source,” section 11 configuration and privacy requirements,
and section 13 Phase 1

## Outcome

Implement the smallest end-to-end Phase 1 slice that conditionally imports the
configured portfolio résumé PDF into `resume_version`, using the mutable
`resume_source_state` table added by this task to hold the current HTTP
conditional-fetch observation separately from immutable `resume_version`
provenance.

After this task, a developer can run one CLI command that:

1. reads the configured HTTPS PDF URL and validators from its matching
   `resume_source_state`;
2. performs one bounded conditional fetch;
3. accepts only a complete PDF response no larger than 10 MiB;
4. hashes the raw bytes, extracts and deterministically normalizes the text to
   Markdown, and rejects clearly incomplete extraction;
5. hashes the normalized Markdown;
6. transactionally creates at most one idempotent `base`/`portfolio_pdf`
   `resume_version` and updates current source state; and
7. automatically activates only the first valid base résumé when no active or
   prior base version exists.

A later normalized-content change is staged inactive for review. A raw-byte
change that produces identical normalized content does not create a new
scoring version.

This slice provides a callable service function and a small CLI suitable for
initial bootstrap and reuse by a later scheduler. It does not add scheduling,
an API route, review/activation UI, or notifications.

## Authoritative provenance model

Specification v0.13 defines `resume_version` as historical scoring provenance.
Its source and content fields (`source_url`, `source_sha256`, `source_etag`,
`source_last_modified`, `source_fetched_at`, `content_hash`, and `content_md`)
remain immutable historical snapshots: a new `resume_version` row captures the
exact URL, raw hash, validators, body-fetch timestamp, and normalized content
from the `200` response that created it, and no later fetch outcome refreshes
those fields on an existing row.

Current HTTP/source observation state — the mutable data needed to make the
*next* fetch conditional — lives in a new `resume_source_state` table, added by
this task in a second Alembic revision (`down_revision = "0001_core_schema"`).
Specification v0.13 section 04 defines the table and the state transitions;
this task implements them. See "1. Second Alembic revision:
`resume_source_state`" below for the schema.

### Authoritative semantics

- Existing `resume_version` rows are not updated by this sync. Their source and
  content provenance fields remain immutable after insertion; reviewed
  activation of the separate `active` flag belongs to a later workflow.
- The next request's `If-None-Match` and `If-Modified-Since` values come only
  from the matching `resume_source_state` row — never from `resume_version`.
- The matching row is selected (or created, if absent) by the unique
  `(variant, source_kind, source_url)` key. Changing `BASE_RESUME_URL`
  therefore selects or creates a different `resume_source_state` row;
  validators are never copied from one URL's row to another's.
- On `304 Not Modified`: update `last_checked_at` and replace only the
  validators the response actually returned (an `ETag` and/or
  `Last-Modified`), and set `updated_at`; an absent validator, the existing
  `last_body_fetched_at`, `last_body_sha256`, and
  `current_resume_version_id` are all preserved unchanged.
- On `200 OK` with new normalized content (a `content_hash` not already
  present): insert the new `resume_version` and update the matching
  `resume_source_state` row's validators (clearing either validator when the
  response omits it), `last_body_sha256`,
  `last_body_fetched_at`, `last_checked_at`, `updated_at`, and
  `current_resume_version_id` in the same short transaction.
- On `200 OK` whose normalized content is identical to an existing
  `resume_version` (byte-only change): create no new `resume_version`, but
  still update `resume_source_state` with the latest validators, clearing
  either validator when the response omits it, and set the new raw
  `last_body_sha256`, `last_body_fetched_at`, `last_checked_at`, `updated_at`,
  and the matched `current_resume_version_id`.
- On a provenance conflict (an existing `content_hash` owned by a
  non-portfolio or non-base version): mutate neither `resume_version` nor
  `resume_source_state`.

Do not mutate an existing `resume_version`'s provenance columns, and do not
invent an append-only fetch-event table; `resume_source_state` is the single
mutable record of current source observation state.

## Required work

### 1. Second Alembic revision: `resume_source_state`

Add one new Alembic revision under `api/migrations/versions/`, following the
same file-naming template and structural-only style as
`20260725_2200_0001_core_schema.py`:

- `revision = "0002_resume_source_state"`;
- `down_revision = "0001_core_schema"`;
- `branch_labels = None`, `depends_on = None`.

Create exactly one new table, `resume_source_state`, with these columns:

- `id` — `bigint generated by default as identity`, primary key;
- `variant` — `text not null`;
- `source_kind` — `text not null`;
- `source_url` — `text not null`;
- `source_etag` — `text`, nullable;
- `source_last_modified` — `timestamptz`, nullable;
- `last_checked_at` — `timestamptz not null`;
- `last_body_fetched_at` — `timestamptz`, nullable;
- `last_body_sha256` — `text`, nullable;
- `current_resume_version_id` — `bigint`, nullable;
- `created_at` — `timestamptz not null default now()`;
- `updated_at` — `timestamptz not null default now()`.

Constraints and indexes, every one given an explicit stable `name=`:

- `resume_source_state_pkey` — the primary-key constraint on `id`;
- `resume_source_state_variant_check` — the exact same enumerated check used
  by `resume_version_variant_check`
  (`variant in ('base','product','program','technical')`);
- `resume_source_state_source_kind_check` — the exact same enumerated check
  used by `resume_version_source_kind_check`
  (`source_kind in ('portfolio_pdf','manual','n8n')`);
- `resume_source_state_variant_source_kind_source_url_key` — a unique
  constraint on `(variant, source_kind, source_url)`. This is the sole lookup
  key the sync service uses to find or create the row for a configured source;
  no separate lookup index is needed;
- `resume_source_state_current_resume_version_id_fkey` — a foreign key from
  `current_resume_version_id` to `resume_version.id` with `ON DELETE
  RESTRICT`, matching the delete-action convention used by every other
  `resume_version` reference in the schema.

The named primary-key and unique constraints supply backing indexes with those
same stable names; the catalog tests must confirm both names. Do not add a
second or speculative index. The application sets `updated_at` explicitly on
every state update; do not add a trigger.

This table represents current HTTP/source observation state, not historical
scoring provenance: it is the only table this sync ever updates in place.
The sync never updates an existing `resume_version` row.

The repository ignores `*.sql` by default. Add only the narrow tracked
exception for `api/tests/resume_source_state_enforcement.sql` to `.gitignore`;
do not weaken the general SQL/database-dump protections.

Write PostgreSQL catalog tests in a new `api/tests/resume_source_state_enforcement.sql`,
parallel in structure to `api/tests/core_schema_enforcement.sql`, that prove
against a disposable PostgreSQL 16 + pgvector database:

- both enumerated checks reject an invalid `variant` and an invalid
  `source_kind` with `SQLSTATE 23514`;
- the composite unique constraint rejects a second row with the same
  `(variant, source_kind, source_url)` with `SQLSTATE 23505`;
- the foreign key rejects a `current_resume_version_id` that does not
  reference an existing `resume_version.id` with the expected foreign-key
  SQLSTATE, and rejects deleting a referenced `resume_version` row with
  `SQLSTATE 23503`/`ON DELETE RESTRICT` semantics; and
- every constraint and index name above exists exactly as specified.

Add a companion Python test, parallel to `api/tests/test_core_schema.py`, that
loads the new revision module directly and asserts: the revision id and
`down_revision` values above; that `upgrade` creates exactly one new table
(`resume_source_state`) and `downgrade` drops exactly that table, leaving
`0001_core_schema`'s tables untouched; that every constraint and index in the
revision has an explicit stable name; and that no inline unnamed
`sa.ForeignKey` column is used.

Update the existing `api/tests/test_core_schema.py`, whose current migration
loader assumes exactly one Python revision file exists. The updated test must:

- locate `0001_core_schema` by its revision identifier or its expected
  migration filename instead of selecting the sole file in
  `api/migrations/versions/`;
- preserve every existing assertion about `0001_core_schema`, including its
  identifiers, first-revision status, filename shape, extension lifecycle,
  vector/HNSW behavior, named constraints, exact Phase 1 tables, downgrade
  order, and absence of out-of-scope tables;
- confirm that the `0001_core_schema` migration itself is unchanged, including
  its existing source-level invariants, while changing only the test's
  multi-revision discovery logic;
- permit exactly the existing `0001_core_schema` revision and the new
  `0002_resume_source_state` revision required by this task; and
- validate that the Alembic graph is the single linear chain
  `0001_core_schema -> 0002_resume_source_state`, with no extra revision,
  branch, or second head.

Do not weaken, delete, skip, or broadly rewrite the core-schema checks to make
the second revision pass. Final review must also confirm that
`api/migrations/versions/20260725_2200_0001_core_schema.py` has no diff.

The revision must be fully reversible: `alembic upgrade head` followed by
`alembic downgrade -1` must drop `resume_source_state` and leave the database
identical to a fresh `0001_core_schema` upgrade, and a subsequent `alembic
upgrade head` must reproduce the same catalog. Cover this upgrade/downgrade/
upgrade cycle in the disposable-PostgreSQL validation commands below.

### 2. Minimal runtime configuration and database support

Add only the runtime support needed by this slice:

- resolve `DATABASE_URL`, `BASE_RESUME_URL`, and `RESUME_MAX_BYTES` from the
  environment;
- require `BASE_RESUME_URL` to be an absolute `https://` URL with no embedded
  credentials;
- default `RESUME_MAX_BYTES` to `10485760` and permit a lower positive value,
  but never permit configuration to raise the hard 10 MiB ceiling;
- provide a small SQLAlchemy engine/session factory with explicit transaction
  ownership and safe connection-error handling;
- provide a narrowly scoped résumé repository for reading and writing the
  matching `resume_source_state` row, reading base-version state, and
  inserting a portfolio version; and
- dispose resources deterministically in the CLI.

Keep the health endpoint independent of configuration and PostgreSQL. Importing
`app.main:app` and calling `GET /healthz` must still work when `DATABASE_URL` or
résumé configuration is absent.

Do not introduce a general ORM model layer, repositories for unrelated tables,
startup-time schema creation, or application startup database access. Use
parameterized SQLAlchemy statements; never interpolate configuration or résumé
data into SQL strings.

Add narrowly constrained runtime dependencies for the HTTP client and PDF text
extractor to `api/pyproject.toml`. Do not add scraping, OCR, document
conversion, model, or résumé-generation dependencies.

### 3. Bounded conditional HTTPS fetch

Implement one fetch component whose input is the configured URL, maximum byte
count, and the exact matching `(base, portfolio_pdf, BASE_RESUME_URL)`
`resume_source_state` row, if one exists. Its `source_etag` and
`source_last_modified` values are the only authoritative source for the next
request's conditional headers.

It must:

- send `If-None-Match` and/or `If-Modified-Since` only when the corresponding
  `resume_source_state` values exist;
- send no conditional validators when no exact matching row exists, and never
  borrow validators from another URL's row;
- retain an ETag exactly for HTTP semantics and format a persisted
  `Last-Modified` timestamp as a valid HTTP date;
- use explicit connect, read, write, and pool timeouts;
- allow at most three redirects, require the initial URL and every redirect
  target to remain HTTPS, and reject redirect targets with embedded
  credentials;
- identify itself with a fixed descriptive user agent;
- stream the response rather than reading an unbounded body;
- reject a declared `Content-Length` above the configured limit before reading
  the body;
- abort as soon as streamed bytes exceed the configured limit, including when
  `Content-Length` is missing or false;
- treat `304 Not Modified` as a successful no-body unchanged outcome;
- require exactly `200 OK` for a body-bearing outcome, rejecting partial,
  empty, error, and other status responses;
- require a media type of `application/pdf` (parameters such as `charset` may
  be ignored) and a PDF file signature in the downloaded bytes; and
- never retry an invalid response. Any limited retry policy for transient
  connection failures must be explicit, bounded, and tested if introduced.

The fetch component returns a typed value containing only the status and
metadata needed by later steps. It must not log or include PDF bytes in a
serialized result.

### 4. Raw-byte hashing, extraction, and deterministic Markdown

For each accepted `200 OK` response:

- compute lowercase hexadecimal SHA-256 over the exact raw response bytes
  before extraction;
- extract pages locally in source page order without OCR, a model call, an
  external converter, or a network service;
- fail closed on malformed, encrypted, password-protected, or extractor-error
  PDFs;
- preserve page order in the normalization input; and
- normalize using one small pure function whose complete rules are documented
  in its docstring and locked by golden tests.

The normalization rules must, at minimum:

- normalize line endings and Unicode consistently;
- remove NUL, soft-hyphen, zero-width, and unsupported control characters;
- normalize non-breaking and repeated horizontal whitespace;
- trim line-edge whitespace;
- join ordinary wrapped lines deterministically without joining across blank
  paragraph boundaries or page boundaries;
- preserve recognizable bullet/list items as Markdown list items;
- collapse repeated blank lines;
- avoid PDF metadata, timestamps, file names, HTTP headers, or extractor
  version strings in the output; and
- emit one canonical UTF-8 string with a single trailing newline.

Do not infer or rewrite résumé content. In particular, do not summarize,
correct, reorder, enrich, classify, or generate headings that are not present
in the extraction.

Compute `content_hash` as lowercase hexadecimal SHA-256 over the exact UTF-8
bytes of the final normalized Markdown.

### 5. Extraction completeness gate

Reject extraction before any database write when:

- the PDF reports zero pages;
- extraction fails to account for every reported page — the extractor must
  return a result for every page index, with no page silently skipped or
  dropped;
- any page's extracted, normalized text contains zero Unicode
  letter-or-number characters;
- the final Markdown is empty; or
- the complete normalized Markdown contains fewer than 500 Unicode
  letter-or-number characters or fewer than 75 maximal contiguous runs of
  Unicode letter-or-number characters.

Do not attempt to infer whether a visually blank PDF page exists without OCR:
there is no local, non-OCR way to distinguish an intentionally blank page from
a page the extractor failed to read, so both are rejected identically by the
zero-letter-or-number-characters rule above. This is a deliberate, conservative
simplification, not a byte-image analysis of the rendered page.

Use named constants for the two document minimums. Count a character whose
Unicode general category is Letter or Number as letter-or-number; define a
word-like token as a maximal contiguous run of those characters. Cover the
exact page and document boundaries in tests. Do not make acceptance depend on
a person's name, contact details, employer names, expected headings, or the
current production résumé wording.

A rejection result may report safe counts such as byte count, page count, and
normalized character count. It must not report excerpts, page text, or raw
bytes.

### 6. Idempotent `resume_version` persistence

For accepted normalized content, persist through the existing schema as:

- `variant = 'base'`
- `source_kind = 'portfolio_pdf'`
- `source_url` equal to the validated configured URL
- `source_sha256` equal to the raw-byte hash
- `source_etag`, `source_last_modified`, and `source_fetched_at` equal to the
  exact validators and body-fetch timestamp of the `200` response that created
  this row, with an absent response validator captured as null — captured once
  at insert and never updated afterward
- `content_hash` equal to the normalized-content hash
- `content_md` equal to the exact normalized Markdown

Required version behavior:

- a `304` creates no `resume_version`;
- an exact repeat creates no `resume_version`;
- changed PDF bytes with identical normalized Markdown create no new scoring
  version;
- a changed normalized-content hash creates exactly one new version;
- the first valid portfolio import may be active only when there is no active
  base version and no prior base version;
- if any base version already exists, a new normalized-content version is
  inserted with `active = false`, even if no base version is currently active;
- a later content change never deactivates or replaces the active base;
- an inactive staged version remains inactive until a separate, reviewed
  activation workflow is implemented later; and
- historical rows referenced by scores are never updated or deleted by this
  sync.

Treat an existing `content_hash` owned by a non-portfolio or non-base version as
a provenance conflict. Return a safe structured conflict result and make no
mutation to `resume_version` or `resume_source_state`; do not relabel or
overwrite the existing row to make the import fit.

On every outcome — `304`, a new version, an identical-content repeat, or a
provenance conflict — update or leave `resume_source_state` exactly per the
authoritative semantics above: a `304` updates only `last_checked_at` and any
validators actually returned, sets `updated_at`, and preserves the rest; a new
version or an identical-content repeat updates validators,
`last_body_sha256`, `last_body_fetched_at`, `last_checked_at`, `updated_at`,
and `current_resume_version_id` together; a provenance conflict changes
nothing in either table.

The operation must remain idempotent when the command is repeated and when two
processes race with the same fetched content.

### 7. Transaction and concurrency safety

Perform network fetch, hashing, and extraction before opening the write
transaction.

Inside one short PostgreSQL transaction:

- acquire a transaction-scoped advisory lock using a documented stable integer
  key for base-résumé sync; do not use Python's process-randomized `hash()`;
- re-read matching content, the matching `resume_source_state` row (selecting
  or creating it by `(variant, source_kind, source_url)`), and all state that
  controls first activation after acquiring the lock;
- decide active versus inactive from that locked database state;
- insert at most one `resume_version` row, and upsert exactly one
  `resume_source_state` row per the authoritative semantics above; and
- commit before returning success.

The partial unique index
`resume_version_one_active_per_variant`, the unique
`resume_version.content_hash` constraint, and the unique
`resume_source_state_variant_source_kind_source_url_key` constraint remain
database backstops. Handle an integrity race by rolling back and re-reading
into an idempotent structured outcome or a provenance conflict; do not leave
the session in a failed transaction and do not deactivate an existing active
row to make a sync succeed.

Add a PostgreSQL integration test that runs two concurrent persistence attempts
and proves:

- only one row is created for identical normalized content;
- at most one active `base` row exists;
- the winning first import is active;
- later different content is inactive;
- exactly one `resume_source_state` row exists for the source afterward, with
  `current_resume_version_id` pointing at the winning row; and
- a failed insert leaves no partial `resume_version` or `resume_source_state`
  row and no accidental activation change.

SQLite is not acceptable evidence for advisory locks, PostgreSQL transactions,
or the partial unique index.

### 8. Callable service and CLI

Expose one orchestration function that returns a typed structured result and
does not terminate the process. Keep fetching, extraction/normalization, and
persistence behind separate small interfaces so tests can replace the HTTP
boundary without live network access.

Add one console-script entry point, for example:

```text
jobs-resume-sync
```

The command must:

- run exactly one sync attempt and exit;
- emit exactly one JSON result object to standard output;
- write structured operational logs to standard error;
- exit `0` for `not_modified`, `unchanged_content`, `activated_initial`, and
  `staged_for_review`;
- exit nonzero for invalid configuration, fetch rejection/failure, extraction
  rejection/failure, database failure, and provenance conflict; and
- close the HTTP client, database session, and engine on every path.

The JSON result has a closed, documented status set. It may include safe fields
such as a created `resume_version_id`, HTTP status, activation boolean, byte
count, and page count. It must never include `content_md`, extracted snippets,
PDF bytes, response bodies, request/response header dumps, database URLs, or
credentials.

Do not make the CLI depend on FastAPI or expose it as an API route.

### 9. Privacy-safe structured logging

Use structured, stable event names and reason codes. Logs may contain:

- result status and safe reason code;
- source hostname without userinfo, query, or fragment;
- HTTP status;
- byte/page/character counts;
- whether validators were sent or returned, as booleans;
- whether raw or normalized content changed, as booleans; and
- a created or matched `resume_version_id`.

Logs and error serialization must not contain:

- résumé text, excerpts, extracted lines, or PDF bytes;
- request or response bodies;
- full headers, ETag values, or `Last-Modified` values;
- a database URL or credentials;
- a configured URL's userinfo, query, or fragment;
- raw SQL parameters containing résumé content; or
- raw extractor/database exception strings that can echo content or secrets.

Tests must capture stdout and logs for every outcome class and assert that
synthetic fixture text, raw-byte sentinels, credentials, and conditional header
values are absent.

### 10. Deterministic tests

Add focused tests for:

- configuration defaults, lower byte limits, the hard 10 MiB ceiling, invalid
  limits, non-HTTPS URLs, and credential-bearing URLs;
- conditional header construction for neither, either, and both validators;
- `304`, valid `200`, redirects, non-HTTPS redirect, redirect overflow,
  timeouts, non-200 statuses, partial responses, wrong or missing media type,
  missing PDF signature, empty bodies, oversized `Content-Length`, and streamed
  overflow without `Content-Length`;
- raw SHA-256 and normalized-content SHA-256 against fixed vectors;
- multi-page extraction order and every extraction rejection path;
- normalization golden cases for line endings, Unicode, controls, whitespace,
  wrapped paragraphs, bullets, page boundaries, blank lines, and trailing
  newline;
- exact completeness-boundary acceptance and rejection, including a page whose
  normalized text has zero Unicode letter-or-number characters and a page the
  extractor fails to account for;
- first activation, later inactive staging, exact repeat, byte-only change,
  existing inactive base state, provenance conflict, rollback, and concurrent
  idempotency;
- `resume_source_state` semantics: a `304` preserves an absent validator,
  `last_body_sha256`, `last_body_fetched_at`, and `current_resume_version_id`
  while updating `last_checked_at`, `updated_at`, and any validator actually
  returned; a new-content `200` and an identical-content `200` both replace
  validators (clearing either when absent) and update `last_body_sha256`,
  `last_body_fetched_at`, `last_checked_at`, `updated_at`, and
  `current_resume_version_id`; a provenance conflict leaves the row unchanged;
  and changing `BASE_RESUME_URL` selects or creates a distinct row rather than
  reusing another URL's validators;
- the `0002_resume_source_state` migration's reversibility and PostgreSQL
  catalog enforcement (see "1. Second Alembic revision" above), covering both
  named checks, the composite unique constraint, and the `ON DELETE RESTRICT`
  foreign key;
- the updated `api/tests/test_core_schema.py` multi-revision discovery:
  `0001_core_schema` is located explicitly, every existing core-schema
  assertion still runs unchanged against it, the original migration file
  remains unchanged, exactly `0002_resume_source_state` is permitted beside
  it, and the two revisions form one linear chain with one head;
- every CLI exit-code/status mapping; and
- stdout/log redaction.

HTTP tests must use the client's mock transport or a loopback test server.
PDFs must be small synthetic fixtures containing invented, non-personal text.
Do not commit the production résumé, a private résumé, or captured production
HTTP responses.

`pytest` must not contact the configured portfolio host or any other live
network service. Mark PostgreSQL tests explicitly and point them only at a
disposable local database supplied by `TEST_DATABASE_URL`; fail clearly rather
than silently using a development or production database.

## Out of scope

- alternative provenance or conditional-state policies that differ from
  specification v0.13
- any résumé-fetch event/history table or source-state mechanism beyond the
  specified `resume_source_state`
- n8n integration or n8n-derived variants
- résumé rewriting, generation, tailoring, summarization, OCR, or enrichment
- product, program, or technical résumé variants
- parent/child variant creation
- candidate-profile creation or activation
- scoring, embeddings, model calls, prompts, or research
- ATS adapters, source discovery, posting ingestion, or reconciliation
- FastAPI routes, request/response schemas, CSRF, HTML, or dashboard work
- manual review or activation endpoints/UI for staged versions
- scheduling, advisory locks for the overall pipeline, or background workers
- notifications, ntfy, SMTP, or notification interfaces
- application tracking
- production Compose changes, deployment, NPM, Authelia, DNS, firewall,
  backups, or VPS work

Do not add placeholders for these features.

## Acceptance criteria

- [ ] Exactly two Alembic revisions exist; the new head is
      `0002_resume_source_state` with
      `down_revision = "0001_core_schema"`, and `0001_core_schema` is
      unchanged.
- [ ] The new revision creates exactly the `resume_source_state` table with the
      specified columns, types, nullability, defaults, named primary key,
      named checks, named composite unique constraint, and named
      `ON DELETE RESTRICT` foreign key to `resume_version.id`; every new
      constraint and index has an explicit stable name.
- [ ] The new revision is fully reversible
      (`upgrade`/`downgrade`/`upgrade` reproduces an identical catalog),
      verified by PostgreSQL catalog tests and a migration-module test.
- [ ] `api/tests/test_core_schema.py` locates `0001_core_schema` explicitly
      rather than assuming one migration file, preserves every existing
      assertion against that revision, confirms the original migration file
      is unchanged, permits exactly `0002_resume_source_state` beside it, and
      validates a single linear two-revision chain with one head.
- [ ] This sync never updates an existing `resume_version`; its source and
      content provenance fields remain immutable after insertion, while
      reviewed changes to `active` remain a later workflow. The next request's
      conditional headers come only from the matching
      `resume_source_state` row, never from `resume_version`.
- [ ] A `304` updates `resume_source_state.last_checked_at`, `updated_at`, and
      only validators actually returned, leaving an absent validator,
      `last_body_sha256`, `last_body_fetched_at`, and
      `current_resume_version_id` unchanged.
- [ ] A `200` with new or byte-only-identical normalized content updates
      `resume_source_state`'s latest validators (clearing either when absent),
      `last_body_sha256`, `last_body_fetched_at`, `last_checked_at`,
      `updated_at`, and `current_resume_version_id` together; a provenance
      conflict updates neither `resume_version` nor `resume_source_state`.
- [ ] A new-content `200` inserts the immutable `resume_version` and updates
      source state in the same short transaction; the version snapshots that
      response's exact URL, raw hash, validators, and body-fetch timestamp.
- [ ] Changing `BASE_RESUME_URL` selects or creates a distinct
      `resume_source_state` row; validators are never copied between URLs.
- [ ] The health endpoint remains importable and returns its fixed response
      without database or résumé configuration.
- [ ] Runtime configuration rejects non-HTTPS or credential-bearing URLs and
      enforces a hard maximum of 10 MiB.
- [ ] The fetch is conditional, streaming, timeout-bounded, redirect-bounded,
      HTTPS-only across redirects, and accepts only `304` or a complete
      `200 application/pdf` within the byte limit.
- [ ] The raw PDF bytes and exact normalized Markdown are independently hashed
      with SHA-256.
- [ ] PDF extraction is local, preserves page order, and deterministically
      rejects malformed, encrypted, empty, and obviously incomplete results.
- [ ] Every reported PDF page is accounted for, and any page with zero Unicode
      letter-or-number characters is rejected without OCR or visual-page
      inference; the exact 500-character and 75-token document boundaries are
      tested.
- [ ] Markdown normalization is a documented pure function with golden tests
      and does not rewrite résumé meaning.
- [ ] Repeated identical content creates no duplicate version.
- [ ] Byte-only PDF changes with identical normalized content create no new
      scoring version.
- [ ] Only the first valid base résumé can auto-activate, and only when no
      active or prior base version exists.
- [ ] Every later normalized-content change creates one inactive
      `portfolio_pdf` base version requiring future review.
- [ ] Sync never deactivates an existing base version or mutates historical
      content/provenance.
- [ ] Persistence uses a short transaction-scoped advisory lock, re-reads
      activation state under the lock, and retains all three unique
      constraints (`resume_version.content_hash`,
      `resume_version_one_active_per_variant`, and
      `resume_source_state_variant_source_kind_source_url_key`) as backstops.
- [ ] Concurrent identical attempts leave one version and at most one active
      base; rollback tests prove failures do not partially change activation
      or source state.
- [ ] The CLI performs one run, emits one documented JSON result, returns
      stable exit codes, and is callable without FastAPI.
- [ ] Results and logs contain no résumé text, PDF bytes, secrets, database
      URLs, validator values, header dumps, or unsafe raw exception strings.
- [ ] HTTP and PDF tests use mocks or synthetic local fixtures; pytest has no
      live-network dependency.
- [ ] PostgreSQL-specific behavior is verified against a disposable PostgreSQL
      16 + pgvector database, not SQLite.
- [ ] Ruff lint and format checks and the complete pytest suite pass.
- [ ] No API route, scheduler, notification, n8n flow, résumé rewrite,
      candidate profile, scoring behavior, ATS adapter, or dashboard work is
      introduced.
- [ ] No secret, `.env` file, production/private résumé, captured production
      payload, database dump, or generated cache is added to Git.
- [ ] `.gitignore` gains only the narrow exception needed to track
      `api/tests/resume_source_state_enforcement.sql`; general `*.sql` and
      database-dump ignores remain intact.

## Validation commands

Run from the repository root unless a command changes directory.

### Local deterministic checks

```bash
cd api
test -x .venv/bin/python || python3.13 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest -m "not postgres"
.venv/bin/python -c "from app.main import app; assert app is not None"
cd ..
```

The non-PostgreSQL test run must pass with `BASE_RESUME_URL`,
`RESUME_MAX_BYTES`, `DATABASE_URL`, and `TEST_DATABASE_URL` unset and must make
no live network request.

### Disposable PostgreSQL integration checks

Use a dedicated disposable container with an ephemeral loopback port. Do not
point `TEST_DATABASE_URL` at a persistent development or production database.

```bash
export RESUME_DB_CONTAINER=jobs-resume-check
export RESUME_DB_PASSWORD=jobs-resume-dev

docker run --rm -d \
  --name "$RESUME_DB_CONTAINER" \
  -e POSTGRES_DB=jobs \
  -e POSTGRES_USER=jobs \
  -e POSTGRES_PASSWORD="$RESUME_DB_PASSWORD" \
  -p 127.0.0.1::5432 \
  pgvector/pgvector:pg16

until docker exec "$RESUME_DB_CONTAINER" pg_isready -U jobs -d jobs; do
  sleep 1
done

export RESUME_DB_PORT
RESUME_DB_PORT="$(
  docker port "$RESUME_DB_CONTAINER" 5432/tcp |
    sed -E 's/.*:([0-9]+)$/\1/' |
    head -n 1
)"
export TEST_DATABASE_URL="postgresql+psycopg://jobs:${RESUME_DB_PASSWORD}@127.0.0.1:${RESUME_DB_PORT}/jobs"

cd api
.venv/bin/alembic heads
.venv/bin/alembic history

DATABASE_URL="$TEST_DATABASE_URL" \
  .venv/bin/alembic upgrade 0001_core_schema
DATABASE_URL="$TEST_DATABASE_URL" .venv/bin/alembic current
docker exec "$RESUME_DB_CONTAINER" \
  psql -X -At -v ON_ERROR_STOP=1 -U jobs -d jobs \
  -c "SELECT to_regclass('public.resume_source_state');"

DATABASE_URL="$TEST_DATABASE_URL" .venv/bin/alembic upgrade head
DATABASE_URL="$TEST_DATABASE_URL" .venv/bin/alembic current
docker exec -i "$RESUME_DB_CONTAINER" \
  psql -X -v ON_ERROR_STOP=1 -U jobs -d jobs \
  < tests/core_schema_enforcement.sql
docker exec -i "$RESUME_DB_CONTAINER" \
  psql -X -v ON_ERROR_STOP=1 -U jobs -d jobs \
  < tests/resume_source_state_enforcement.sql
.venv/bin/pytest -m postgres

DATABASE_URL="$TEST_DATABASE_URL" \
  .venv/bin/alembic downgrade 0001_core_schema
DATABASE_URL="$TEST_DATABASE_URL" .venv/bin/alembic current
docker exec "$RESUME_DB_CONTAINER" \
  psql -X -At -v ON_ERROR_STOP=1 -U jobs -d jobs \
  -c "SELECT to_regclass('public.resume_source_state');"

DATABASE_URL="$TEST_DATABASE_URL" .venv/bin/alembic upgrade head
DATABASE_URL="$TEST_DATABASE_URL" .venv/bin/alembic current
docker exec -i "$RESUME_DB_CONTAINER" \
  psql -X -v ON_ERROR_STOP=1 -U jobs -d jobs \
  < tests/resume_source_state_enforcement.sql

docker exec -i "$RESUME_DB_CONTAINER" \
  psql -X -v ON_ERROR_STOP=1 -U jobs -d jobs <<'SQL'
SELECT
  column_name,
  data_type,
  udt_name,
  is_nullable,
  column_default
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name = 'resume_source_state'
ORDER BY ordinal_position;

SELECT
  conname,
  contype,
  pg_get_constraintdef(oid, true) AS definition
FROM pg_constraint
WHERE conrelid = 'public.resume_source_state'::regclass
ORDER BY conname;

SELECT indexname, indexdef
FROM pg_indexes
WHERE schemaname = 'public'
  AND tablename = 'resume_source_state'
ORDER BY indexname;
SQL
cd ..

docker stop "$RESUME_DB_CONTAINER"
unset TEST_DATABASE_URL RESUME_DB_PORT RESUME_DB_PASSWORD RESUME_DB_CONTAINER
```

The first and second `alembic current` checks after upgrading to head must
report `0002_resume_source_state (head)`. Both checks at
`0001_core_schema` must report that revision, and both `to_regclass` queries
must return null. The migration tests must compare the catalog after the first
and second head upgrades and fail on any difference. The enforcement SQL must
exercise SQLSTATE `23514` for both checks, `23505` for the unique key, and
`23503` for both an invalid pointer and deletion through the restricted foreign
key. All PostgreSQL tests must create their own rows, clean them up
transactionally, and never invoke the live portfolio URL.

### CLI contract and repository hygiene

Exercise CLI serialization with test doubles or local synthetic fixtures only;
do not use the production URL as a validation dependency.

```bash
cd api
.venv/bin/python -m pytest \
  tests/test_resume_cli.py \
  tests/test_resume_logging.py
cd ..

git status --short
git diff --check
git diff --name-only
git diff -- .gitignore api
git diff --exit-code -- docs tasks
git diff --exit-code -- \
  api/migrations/versions/20260725_2200_0001_core_schema.py
```

The implementation must not modify
`tasks/003-portfolio-resume-bootstrap.md`. The allowed implementation diff is
exactly `.gitignore` plus paths under `api/` for the new migration, runtime
modules, console script, existing core-schema test compatibility update, and
new tests. `docs/jobs-forkstech-spec.html` and every file under `tasks/` must
remain unchanged. The final review must include exactly
`git diff -- .gitignore api` for the allowed implementation scope, and the
explicit no-diff checks above must pass.
