# Task 004: Phase 1 Greenhouse adapter

**Status:** Ready

**Depends on:** Task 003 — Phase 1 portfolio résumé bootstrap

**Specification:** `docs/jobs-forkstech-spec.html`, especially sections 03
“Architecture,” 04 source-related data structures, 05 “Source layer,” and 13
“Phase 1”

## Outcome

Implement the smallest complete ATS source-layer vertical slice: the shared
immutable adapter contract and one asynchronous Greenhouse adapter for the
documented Phase 1 public job-board endpoint.

After this task, application code can pass a typed Greenhouse source endpoint
and optional HTTP validators to an adapter which performs one bounded HTTPS
fetch, returns deterministic `RawPosting` values for valid Greenhouse rows, and
marks every partial response as unsafe for later close reconciliation. The
slice includes Greenhouse board-token recognition and recorded synthetic
fixtures, but no database or workflow integration.

## Required work

### 1. Shared ATS contract

Create the source package under `api/app/sources/ats/` and define the shared
contract in `contracts.py`. It must include:

- a frozen source-layer `SourceEndpoint` value containing only the endpoint
  fields an adapter needs: `kind`, `token`, `region`, and optional `base_url`;
- the specification's frozen `RawLocation`, `RawPosting`, `FetchResult`, and
  `ConditionalHeaders` dataclasses, with the field names and meanings shown in
  section 05; and
- the asynchronous `ATSAdapter` protocol, including `slug`,
  `rate_limit_per_min`, `list_postings(...)`, and `token_patterns()`.

Use timezone-aware `datetime` values for source timestamps. `RawPosting.raw`
must be a defensive snapshot of the complete vendor posting object, including
unknown fields; normalization must not mutate the decoded response or a caller's
conditional-header object.

Keep this a source-layer contract, not an ORM model. It must not import
SQLAlchemy, repository code, FastAPI, pipeline code, or résumé modules.

The Greenhouse adapter must expose:

```python
slug = "greenhouse"
rate_limit_per_min = 60
```

The rate value is a conservative local scheduling hint, not a claim about a
provider-published quota. This task does not implement a rate limiter.

### 2. Narrow Greenhouse endpoint and exact URL construction

`GreenhouseAdapter.list_postings(...)` must accept the shared
`SourceEndpoint`, but fail before network I/O unless:

- `kind == "greenhouse"`;
- `region == "global"`;
- `base_url is None`; and
- `token` fully matches `[A-Za-z0-9][A-Za-z0-9_-]{0,127}`.

The adapter must construct exactly this request URL from the validated token:

```text
https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
```

Do not accept a caller-supplied host, path, query string, URL, or credentials,
and do not perform string concatenation with an unvalidated token. Tests must
assert the exact scheme, host, path, single `content=true` query parameter, and
absence of any other query parameter for tokens containing each permitted
punctuation character.

### 3. Injectable, bounded HTTPS fetch

Use the constrained `httpx` runtime dependency established by the preceding
task. Keep the HTTP policy inside the Greenhouse module and expose only the
small constructor seam needed to inject an `httpx` mock transport or equivalent
small async test double. Do not refactor the résumé fetcher or create a generic
application-wide HTTP framework.

The production request must:

- be asynchronous and stream the response body;
- use explicit `httpx.Timeout` values of 5 seconds for connect, write, and pool,
  and 30 seconds for read;
- send the fixed user agent
  `ForksTech-Jobs/0.1 (+https://jobs.forkstech.com)`;
- send `Accept: application/json`;
- forward a non-null `ConditionalHeaders.etag` exactly as `If-None-Match` and a
  non-null `last_modified` exactly as `If-Modified-Since`, without otherwise
  inventing either header;
- disable automatic redirects and follow redirects explicitly;
- follow at most three redirect responses across `301`, `302`, `303`, `307`,
  and `308`;
- resolve relative `Location` values against the current URL, but require the
  initial URL and every resolved target to remain on the exact
  `https://boards-api.greenhouse.io` origin with the effective default HTTPS
  port and no username or password;
- treat a redirect without a valid `Location`, a cross-origin target, a target
  using another scheme or port, a credential-bearing target, a lookalike host,
  or a fourth redirect as incomplete;
- reject a declared `Content-Length` greater than 10,485,760 bytes before
  consuming the body; and
- abort streaming as soon as accumulated bytes exceed that same hard ceiling,
  including when `Content-Length` is absent, malformed, or false.

The byte ceiling applies to the decoded response bytes handed to the JSON
parser. Content decompression must not permit a response to exceed the ceiling
after decoding.

The adapter must make no automatic retry. A later workflow owns retry policy.
Close every response and any adapter-owned client deterministically on success,
failure, cancellation, and size rejection.

Transport, timeout, and TLS failures must not be represented by a made-up HTTP
status. Raise a small typed adapter exception with a privacy-safe category and
no response body, request headers, token, or raw provider exception in its
public string. Allow task cancellation to propagate after deterministic
cleanup rather than converting it into a provider failure. Since no
`FetchResult` is produced in either case, such a failure cannot authorize
reconciliation.

### 4. HTTP and payload outcome semantics

Handle outcomes as follows:

- `200` with a well-formed top-level object, a `jobs` list, and valid rows
  returns the normalized rows with `complete=True`.
- `200` with exactly an empty `jobs` list is a valid empty board and returns
  `postings=[]` with `complete=True`. Historical zero-row anomaly detection
  belongs to later source-fetch orchestration and is not available in this
  adapter.
- `200` with malformed JSON, a non-object top level, a missing or non-list
  `jobs`, a present non-object `meta`, or a present `meta.total` which is not a
  non-negative JSON integer equal to the number of job rows returns no
  postings and `complete=False`.
- `200` with one or more invalid posting rows returns all independently valid
  rows in the deterministic order below, but `complete=False`. A skipped row
  can never make the fetch complete.
- `304` uses the proposed representation in “Specification questions
  requiring review”: no postings, `complete=False`, and `http_status=304`.
- `429` returns no postings with `complete=False` and `http_status=429`. Do not
  sleep or retry; ignore the response body.
- every other final non-success status returns no postings with
  `complete=False` and its actual HTTP status; do not parse its body.
- an unsafe or exhausted redirect returns no postings with `complete=False`
  and the actual redirect status.
- an oversized `200` response returns no postings with `complete=False` and
  `http_status=200`.

For every returned `FetchResult`, copy the final response's `ETag` and
`Last-Modified` header values exactly when present, otherwise use `None`.
Redirect-hop validators must not leak into the final result.

Malformed JSON, invalid rows, redirects, rate limits, non-success statuses, and
oversized bodies are expected provider outcomes, not uncaught JSON, key,
Unicode, URL, or `httpx` exceptions. None may return `complete=True`.

### 5. Deterministic Greenhouse normalization

Normalize each valid job independently using these rules:

- `external_id` is the decimal string form of the Greenhouse posting `id`.
  Accept only a positive JSON integer which is not a Boolean; do not use
  `internal_job_id` or `requisition_id`.
- `title` is a non-empty string after trimming leading and trailing
  whitespace. Preserve internal text and case.
- `locations` contains zero or one `RawLocation`. When
  `location.name` is a non-empty string, preserve its trimmed value as
  `label`; otherwise return an empty location list. Set `country_code`,
  `region`, `city`, and `workplace_type` to `None`. Do not infer geography,
  remote status, or workplace type from punctuation, offices, title, or
  description text.
- `description_md` comes from a string `content` value using the reviewed
  conversion policy below. A missing or non-string `content` makes that row
  invalid.
- `posting_url` is the exact absolute HTTP or HTTPS `absolute_url` supplied by
  Greenhouse after URL parsing confirms it has a host and no embedded
  credentials. Preserve its query string and fragment.
- `apply_url` is populated separately from `posting_url`. The documented list
  response exposes one public candidate destination, `absolute_url`, rather
  than a second apply URL, so this adapter must copy that exact value into
  `apply_url` as well. Do not invent a `#app` fragment and never expose the
  authenticated application-submission API as a candidate URL. The two
  contract fields must remain independently populated and asserted so an
  adapter whose vendor supplies distinct URLs can preserve them later.
- `source_published_at` is parsed from an optional `first_published` string and
  `source_updated_at` from an optional `updated_at` string. Accept ISO 8601
  offsets and `Z`; return `None` for an absent, malformed, or timezone-naive
  value rather than assuming a timezone.
- `department` is the first non-empty string `name` in the response's
  `departments` list, in vendor order, or `None`.
- `raw` is a defensive copy of the complete decoded job object, not a reduced
  projection and not a serialized log field.

An invalid `absolute_url`, non-object job row, invalid ID, empty title, or
missing/non-string content makes that row invalid. Optional malformed
locations, departments, and timestamps degrade only those optional normalized
fields and do not invalidate an otherwise usable row.

Sort all returned postings lexically by
`(external_id, title, posting_url)`. Never rely on vendor array order, mapping
iteration order, set order, or fixture order.

### 6. HTML unescaping and deterministic Markdown

For each valid `content` string:

1. call Python's `html.unescape` exactly once;
2. convert the resulting HTML to Markdown with the reviewed narrow dependency
   and fixed options;
3. normalize CRLF and bare CR to LF;
4. remove trailing horizontal whitespace from every line;
5. collapse runs of three or more blank lines to exactly two blank lines; and
6. strip leading and trailing blank lines without adding a terminal newline.

Do not strip visible description text, links, headings, lists, emphasis, or
line breaks merely to simplify tests. Script and style contents must not appear
as visible Markdown. The conversion must be a pure deterministic function with
golden tests for nested entities, paragraphs, headings, ordered and unordered
lists, links, emphasis, `<br>`, CRLF, repeated blank lines, and non-ASCII text.

### 7. Greenhouse token-pattern recognition

Implement `token_patterns()` as a deterministic list of compiled regular
expressions with a named `token` capture. It must recognize tokens from these
HTTPS URL shapes, with optional trailing slash, job path, query, or fragment:

```text
https://boards.greenhouse.io/{token}
https://job-boards.greenhouse.io/{token}
https://boards.greenhouse.io/embed/job_board?for={token}
https://boards-api.greenhouse.io/v1/boards/{token}/jobs
```

Matching must be host-boundary safe and must not recognize non-HTTPS URLs,
credential-bearing URLs, lookalike hosts such as
`boards.greenhouse.io.example.com`, missing tokens, path traversal, or tokens
outside the adapter's token grammar. Pattern order and returned token must be
stable. Recognition performs no network call and does not seed or persist an
endpoint.

### 8. Privacy-safe behavior

The adapter may log a bounded event category, HTTP status, redirect count,
byte count, and normalized posting count. It must never log:

- the response body or a full vendor posting;
- job descriptions or Markdown;
- request or response header dumps;
- conditional validator values;
- endpoint tokens or full request/redirect URLs; or
- raw exception strings which may contain any of the above.

No logged error or serialized exception may contain fixture sentinel
description text or conditional-header values.

## Specification questions requiring review

These are specification gaps, not permission to expand the implementation.
The proposals below are the task's smallest safe defaults. A reviewer who wants
different semantics must revise this task or the specification before T004 is
implemented.

### 1. Representation of `304 Not Modified`

**Human review — proposed behavior:** return
`FetchResult(postings=[], complete=False, http_status=304, ...)`.

Section 05 does not define a separate “not modified” variant, and
`postings=[]` with `complete=True` could be misread by later reconciliation as
a complete empty board. The status code is sufficient for later orchestration
to recognize an unchanged fetch, while `complete=False` fails closed until that
orchestration has explicit `304` handling. Do not add a result union, new
database status, or persistence behavior in this task.

### 2. Paginated fixtures

**Human review — proposed behavior:** do not add pagination code or a fake
paginated fixture for this adapter.

Section 05 broadly asks every adapter fixture set to cover pagination, but the
selected Greenhouse `GET /v1/boards/{token}/jobs?content=true` contract returns
the board's jobs in one response and does not define listing pagination. The
hard byte ceiling still protects that single response. Pagination support and
fixtures belong only to an adapter endpoint that actually exposes pagination.

### 3. HTML-to-Markdown implementation

**Human review — proposed behavior:** use one narrowly constrained
`markdownify` runtime dependency with fixed conversion options, followed by the
canonical whitespace pass in section 6.

A maintained parser-backed converter is safer than growing an incomplete
application-owned HTML parser in this task. Pin a compatible bounded version
range in `api/pyproject.toml`; record the exact installed version in normal
dependency-resolution output, not in application data. Keep the wrapper small
and golden-test its output so a later dependency upgrade is deliberate. Do not
add BeautifulSoup directly, a browser, readability extraction, an external
conversion service, or a model call.

## Out of scope

- database writes, ORM models, repositories, or transaction management
- migrations or schema changes
- creating `pipeline_run` or `source_fetch` rows
- posting or posting-version persistence
- ingestion content hashing
- deduplication
- close/reopen reconciliation or historical zero-row anomaly decisions
- company or source-endpoint seeding
- CLI commands, API routes, workflow orchestration, scheduling, or
  notifications
- deterministic job filters
- embeddings, research, model calls, scoring, ranking, or digest creation
- Lever, Ashby, or any other ATS, feed, or scraping adapter
- live API validation or committed production payloads
- source discovery beyond pure Greenhouse token-pattern recognition
- a generic HTTP framework or résumé-module refactoring unrelated to this
  adapter
- retries, rate-limit sleeping, circuit breakers, caching, or conditional-state
  persistence
- changes to Compose, deployment, NPM, Authelia, DNS, firewall, backups, or the
  VPS

Do not add placeholders for any of these features.

## Test requirements

Add recorded response-shape fixtures under
`api/tests/fixtures/greenhouse/`. They must be synthetic and contain only
invented companies, jobs, locations, URLs, and description text:

- `normal.json` — at least two valid rows in non-sorted vendor order, escaped
  HTML content, unknown vendor fields, distinct timestamps, optional fields,
  and query/fragment-bearing public URLs;
- `empty.json` — a valid empty `jobs` list and matching zero total;
- `malformed.json` — intentionally invalid JSON bytes; and
- `rate_limited.json` — a small invented error body used with HTTP `429`.

These are recorded HTTP response-shape fixtures, not captured production
payloads. Keep response status and headers explicit in the test cases. Do not
commit a real board token, employer payload, job description, request dump, or
provider response headers.

Use `httpx.MockTransport` or the constructor's equivalent in-process async test
double. No adapter test may resolve DNS, open a socket, contact a live
Greenhouse board, or depend on internet availability. The existing autouse
network guard in `api/tests/conftest.py` must remain active and unchanged for
all non-PostgreSQL tests.

Tests must cover:

- frozen contract values, protocol conformance, and no mutation of inputs;
- exact URL and headers, valid token punctuation, and every invalid endpoint
  condition;
- normal, empty, malformed, and rate-limited fixtures;
- `200`, `304`, all redirect codes, relative redirects, exactly three
  redirects, a fourth redirect, missing/malformed locations, HTTPS downgrade,
  cross-origin redirects, alternate ports, credential-bearing redirect
  targets, and lookalike hosts;
- conditional validators are never sent to a cross-origin redirect target
  because every such redirect is rejected before issuing the next request;
- conditional headers independently and together, plus final-response-only
  validators;
- a declared oversized body, a streamed oversized body without a usable
  `Content-Length`, and an exact-ceiling body;
- invalid UTF-8, malformed JSON, wrong top-level shapes, wrong `jobs` and
  `meta.total` types, one invalid row among valid rows, and all-invalid rows;
- stable IDs, deterministic posting order independent of fixture row order,
  timezone-aware valid timestamps, and absent/malformed/naive timestamps;
- optional location and department degradation without geographic invention;
- posting and application URL fields, including the documented Greenhouse
  single-destination behavior;
- exact preservation of unknown vendor fields in `raw`;
- single unescaping plus golden deterministic Markdown conversion;
- transport, timeout, TLS, and cancellation behavior;
- token-pattern positive and boundary-negative cases; and
- log and exception redaction using unique fixture sentinels.

Tests for unsuccessful outcomes must assert both the exact HTTP status and
`complete=False`. Tests for typed transport failures must assert that no
`FetchResult` is returned and the exception text is bounded and redacted.

Do not test with a loopback HTTP server when `MockTransport` can express the
case. Do not add a paginated fixture unless the selected endpoint contract is
changed by an approved specification update.

## Acceptance criteria

- [ ] The shared source contract defines frozen `SourceEndpoint`,
      `RawLocation`, `RawPosting`, `FetchResult`, and `ConditionalHeaders`
      values plus an asynchronous `ATSAdapter` protocol, without database,
      FastAPI, pipeline, or résumé imports.
- [ ] `GreenhouseAdapter` has `slug == "greenhouse"` and
      `rate_limit_per_min == 60`, and `list_postings` is asynchronous.
- [ ] Only a valid global Greenhouse endpoint with no custom base URL reaches
      the HTTP boundary.
- [ ] A valid token produces exactly
      `https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true`;
      invalid or injection-bearing tokens fail before I/O.
- [ ] The request is HTTPS-only, streaming, timeout-bounded,
      redirect-bounded, identified by the fixed user agent, limited to
      10,485,760 decoded bytes, and never leaves the exact
      `https://boards-api.greenhouse.io` origin.
- [ ] Available conditional headers are forwarded exactly and absent ones are
      omitted.
- [ ] A valid normal `200` is complete, a structurally valid empty board is
      complete, and every malformed, partial, rate-limited, non-success,
      unsafe-redirect, or oversized returned outcome is incomplete.
- [ ] `304` follows the reviewed fail-closed representation and cannot be
      confused with a complete empty board.
- [ ] Transport-layer failures use the typed, redacted exception path and
      cannot authorize close reconciliation.
- [ ] Invalid rows do not discard valid rows, but any skipped row makes the
      whole fetch incomplete.
- [ ] Stable external IDs use Greenhouse posting IDs, and all returned
      postings have deterministic lexical ordering.
- [ ] Parseable timestamps are timezone-aware; missing, malformed, or naive
      timestamps remain `None`.
- [ ] Locations preserve only the vendor label and do not invent country,
      region, city, remote status, or workplace type.
- [ ] Posting and application URLs remain separate required contract fields;
      the Greenhouse single public URL is copied exactly into both without
      inventing an application API or fragment.
- [ ] Each posting's full vendor object, including unknown fields, is retained
      in `raw` without mutation or logging.
- [ ] Greenhouse content is unescaped exactly once before deterministic
      parser-backed Markdown conversion and canonical whitespace
      normalization.
- [ ] `token_patterns()` recognizes the four approved Greenhouse URL shapes
      and rejects non-HTTPS, credential-bearing, lookalike-host, traversal, and
      invalid-token cases without network access.
- [ ] Synthetic fixtures cover normal, empty, malformed, and rate-limited
      responses; no pagination machinery or fake pagination fixture is added.
- [ ] The complete pytest suite and focused adapter tests pass under the
      existing autouse network guard for non-PostgreSQL tests.
- [ ] Ruff lint and format checks pass for all Python under `api/`.
- [ ] Logs and public exception strings contain no endpoint token, URL,
      validator, payload, description, Markdown, header dump, or unsafe raw
      exception text.
- [ ] No database access, persistence, migration, hash gate, reconciliation,
      API, CLI, workflow, scheduling, filter, model, scoring, other adapter, or
      résumé refactor is introduced.
- [ ] No secret, `.env` file, real board token, captured production payload,
      private résumé, database dump, cache, or generated test artifact is added
      to Git.

## Validation commands

Run from the repository root unless a command changes directory. These commands
are requirements for implementing T004; no external validation is claimed by
this planning document.

### Local lint, format, and complete test suite

```bash
cd api
test -x .venv/bin/python || python3.13 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest
.venv/bin/python -c "from app.main import app; assert app is not None"
cd ..
```

The complete suite must not require a live Greenhouse board or any other
external network service. Environment-dependent PostgreSQL tests inherited
from preceding tasks must use only their documented disposable local database
setup.

### Explicit network-isolation verification

```bash
cd api
.venv/bin/pytest -m "not postgres" \
  tests/test_ats_contracts.py \
  tests/test_greenhouse_adapter.py
cd ..
```

This command must pass with the existing autouse network guard active. A test
must fail rather than silently fall back to a live request when the mock
boundary is absent.

### Repository hygiene and final scope review

```bash
git status --short
git diff --check
git diff --name-only
git diff -- \
  api/pyproject.toml \
  api/app/sources \
  api/tests/fixtures/greenhouse \
  api/tests/test_ats_contracts.py \
  api/tests/test_greenhouse_adapter.py
git diff --exit-code -- \
  AGENTS.md \
  docs \
  tasks \
  docker-compose.yml \
  api/migrations \
  api/app/main.py \
  api/app/resume
```

The implementation diff must be limited to the constrained Markdown conversion
runtime dependency in `api/pyproject.toml`, the new
`api/app/sources/ats/` package, its package initializers, the four synthetic
Greenhouse fixtures, and the two focused test modules named above. No existing
migration, task, specification, application entrypoint, résumé module, test
support module, or deployment file may change.
