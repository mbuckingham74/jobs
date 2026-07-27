# Task 005: Phase 1 Lever adapter

**Status:** Ready

**Depends on:** Task 004 — Phase 1 Greenhouse adapter

**Specification:** `docs/jobs-forkstech-spec.html`, especially sections 04
“Data model,” 05 “Source layer,” and 13 “Phase 1”

**External source:** Lever's official
[Postings API documentation](https://github.com/lever/postings-api)

## Outcome

Implement the second Phase 1 ATS adapter: one asynchronous `LeverAdapter` for
Lever's documented, unauthenticated public Postings API, using the frozen
source-layer contract introduced by Task 004.

After this task, application code can pass a typed global or EU Lever endpoint
and optional HTTP validators to the adapter. The adapter validates the endpoint
before network I/O, fetches all published postings through bounded
`skip`/`limit` pagination, maps independently valid rows to deterministic
`RawPosting` values, and marks every partial, inconsistent, rate-limited, or
otherwise unsafe result incomplete so it cannot authorize later close
reconciliation.

The slice includes pure recognition of global and EU Lever job-site and API
URLs plus synthetic recorded response-shape fixtures. It does not add database
access, ingestion, endpoint seeding, workflow integration, or live-board
validation.

## Required work

### 1. Lever adapter on the existing frozen contract

Add `api/app/sources/ats/lever.py` with one `LeverAdapter` that implements the
existing `ATSAdapter` protocol and consumes the existing frozen
`SourceEndpoint`, `ConditionalHeaders`, `FetchResult`, `RawPosting`, and
`RawLocation` values from `contracts.py`.

The adapter must expose:

```python
slug = "lever"
rate_limit_per_min = 60
```

The rate value is a conservative local scheduling hint, not a
provider-published GET quota. This task does not add throttling, sleeping, or
retry behavior.

Do not change the shared ATS contract. In particular, do not add pagination
state, a not-modified result variant, region metadata, or Lever-specific fields
to a shared dataclass. If implementation reveals a concrete contract blocker,
stop and request human approval rather than editing `contracts.py` or
`test_ats_contracts.py`.

The adapter must not import FastAPI, SQLAlchemy, repositories, pipeline code,
or résumé modules. Update the ATS package description or exports only if
needed to expose the Lever adapter; preserve all existing contract exports and
Greenhouse behavior.

### 2. Exact region-specific endpoints and validation before I/O

`LeverAdapter.list_postings(...)` must reject the endpoint before constructing
an HTTP client or issuing a request unless all of these conditions hold:

- `kind == "lever"`;
- `region` is exactly `"global"` or `"eu"`;
- `base_url is None`; and
- `token` is a string that fully matches the proposed conservative allowlist
  `[A-Za-z0-9][A-Za-z0-9_-]{0,127}`.

Lever's official documentation names this path value `SITE` but does not
publish an exact SITE/token grammar. The allowlist above is therefore a
review-gated application policy, not a vendor-authoritative grammar. It must
receive the explicit approval required in “Specification questions requiring
human review” before T005 implementation begins.

No case folding, URL decoding, whitespace stripping, path normalization, or
best-effort repair is allowed for endpoint fields. Reject empty, oversized,
credential-bearing, path-like, query-bearing, fragment-bearing, percent-
encoded, Unicode, dot-containing, or otherwise out-of-grammar tokens before
network I/O.

The validated region selects exactly one origin:

```text
global -> https://api.lever.co
eu     -> https://api.eu.lever.co
```

The documented logical listing endpoints are:

```text
https://api.lever.co/v0/postings/{token}?mode=json
https://api.eu.lever.co/v0/postings/{token}?mode=json
```

The adapter must add only the pagination parameters defined below. Its page
requests must have the exact region-selected scheme and host, the exact path
`/v0/postings/{token}`, no fragment or credentials, and these query parameters
exactly once:

```text
mode=json&skip={skip}&limit=100
```

Tests must parse the request URL and assert the exact scheme, host, effective
default HTTPS port, path, and query multiset. Do not accept a caller-supplied
host, URL, path, query, credentials, or API key. Do not silently switch a
global endpoint to EU, or vice versa.

### 3. Injectable, bounded asynchronous HTTPS

Use the existing constrained `httpx` dependency. Keep Lever's HTTP policy and
test-injection seam inside `lever.py`, parallel to the Greenhouse module.
Do not refactor the Greenhouse or résumé fetchers and do not create a generic
application-wide HTTP framework.

Every production page request must:

- be asynchronous and stream decoded response bytes;
- use explicit `httpx.Timeout` values of 5 seconds for connect, write, and
  pool, and 30 seconds for read;
- send the fixed user agent
  `ForksTech-Jobs/0.1 (+https://jobs.forkstech.com)`;
- send `Accept: application/json`;
- disable automatic redirects and follow redirects explicitly;
- make no automatic retry; and
- close every response and any adapter-owned client deterministically on
  success, failure, cancellation, redirect rejection, page inconsistency, and
  size rejection.

At most three redirects may be followed across the entire paginated fetch.
Only `301`, `302`, `303`, `307`, and `308` are redirect statuses. Resolve a
relative `Location` against the current URL, then require the resolved target
to preserve all of the following:

- HTTPS;
- the exact origin selected by `SourceEndpoint.region`, accepting only an
  omitted port or explicit port `443`;
- no username or password;
- the exact `/v0/postings/{token}` path; and
- exactly the current page's `mode=json`, `skip`, and `limit=100` query
  parameters, with no extra parameter.

A missing or malformed `Location`, downgrade, alternate port, cross-region or
cross-origin target, lookalike host, credential-bearing target, changed token
or path, changed pagination query, or fourth redirect must not be followed.
Return the valid postings accumulated before that response with
`complete=False` and the actual redirect status.

For each response, reject a declared `Content-Length` greater than 10,485,760
decoded bytes before consuming the body. Abort streaming as soon as that page
exceeds the same ceiling when the header is missing, malformed, negative, or
false. Also enforce one 10,485,760-byte cumulative decoded-body ceiling across
all pages in the fetch so pagination cannot multiply memory use without bound.
The exact per-page and cumulative ceilings are inclusive. Content
decompression must not bypass either decoded-byte bound.

Transport, timeout, TLS, and streaming failures must raise a small typed Lever
adapter exception with a bounded privacy-safe category. Its public string must
not include a token, URL, validator, header dump, response body, posting
content, or raw provider exception. No `FetchResult` is produced for a
transport failure, so it cannot authorize reconciliation. Allow task
cancellation to propagate after deterministic cleanup.

### 4. Bounded `skip`/`limit` pagination

The official Postings API returns a bare JSON array and documents `skip` and
`limit`, but no total count, next-page cursor, snapshot token, default limit,
or maximum limit. Use these explicit bounds:

- fixed `limit=100`;
- first page `skip=0`;
- advance `skip` by the decoded row count of each full page;
- fetch a next page only when the current page contains exactly 100 rows;
- stop successfully on the first page containing fewer than 100 rows,
  including a zero-row page after a full page;
- maximum 100 page responses;
- maximum 10,000 decoded posting rows; and
- maximum 103 HTTP responses in total: 100 page responses plus three followed
  redirects.

The adapter must keep pagination sequential. It must not prefetch pages in
parallel because `skip` offsets have no snapshot semantics and the previous
page determines whether another request is permitted.

An empty first page is a valid empty board. A full final data page followed by
an empty page is also a valid terminal sequence. Reaching a page, row,
response, or cumulative-byte bound without observing a short terminal page is
incomplete.

Detect and fail closed on every inconsistency visible within one bounded fetch:

- a page containing more rows than the requested limit;
- an exact decoded page repeated at a different `skip`;
- an `id` repeated within one page or across pages, even if the rest of the
  posting changed; or
- arithmetic that would repeat, decrease, overflow, or exceed the permitted
  `skip`/row bound.

On a repeated or inconsistent page, do not append the repeated/conflicting
rows. Return independently valid postings accumulated before the inconsistency,
sorted by the final ordering rule, with `complete=False` and
`http_status=200`. This fails closed for reconciliation while preserving the
source-layer contract's permitted partial-observation behavior.

Malformed rows still count toward `skip` because pagination operates on vendor
array positions, not normalized postings. Continue bounded pagination after
individually malformed rows so independently valid later rows can be returned,
but the final result remains incomplete.

The adapter cannot prove that Lever's offset view stayed stable when postings
are inserted, removed, or reordered between requests. The review section below
records that specification gap; do not invent a total or snapshot guarantee.

### 5. Conditional headers and response validators

Build request headers from a defensive copy of `ConditionalHeaders`. Forward a
non-null `etag` exactly as `If-None-Match` and a non-null `last_modified`
exactly as `If-Modified-Since`; omit each header when its value is `None`.
Never mutate the caller's value or invent a validator.

Because `FetchResult` can store only one validator pair, and a validator for
one `skip` URL is not safely reusable for a different page, send conditional
headers only on the first page (`skip=0`) and its approved same-page redirect
hops. Later page requests must carry the fixed user agent and `Accept` header
but no conditional validators.

Copy `ETag` and `Last-Modified` into the returned `FetchResult` only from the
first page's final non-redirect response. Preserve those two header values
exactly when present, otherwise use `None`. Redirect-hop validators and all
later-page validators must not replace or supplement the first-page values.

This policy keeps a stored validator associated with the same page URL to
which it will be sent on the next run. It is a proposed interpretation of the
frozen contract and requires human review below.

### 6. HTTP and payload outcome semantics

Handle provider outcomes as follows:

- A sequence of `200` responses whose pages are JSON arrays, whose rows are
  all valid, and which ends with a short page returns all normalized postings
  with `complete=True`.
- A first `200` with an empty array returns `postings=[]` and
  `complete=True`. Historical zero-row anomaly detection belongs to later
  source-fetch orchestration.
- A `200` with malformed JSON or a non-array top level returns valid postings
  accumulated from prior pages with `complete=False` and `http_status=200`.
- A `200` page with one or more invalid rows retains all independently valid
  rows, continues within the pagination bounds, and makes the final result
  `complete=False`.
- A first-page `304 Not Modified` returns no postings with `complete=False`
  and `http_status=304`. No later page is requested.
- An unexpected later-page `304` returns prior valid postings with
  `complete=False` and `http_status=304`.
- A `429` on any page returns prior valid postings with `complete=False` and
  `http_status=429`. Do not parse its body, sleep, or retry.
- Any other final non-success response returns prior valid postings with
  `complete=False` and its actual HTTP status. Do not parse its body.
- An unsafe or exhausted redirect returns prior valid postings with
  `complete=False` and the actual redirect status.
- An oversized page, cumulative-byte overflow, or exhausted pagination bound
  returns prior valid postings with `complete=False`; use the actual final
  response status, which is `200` for size and pagination-bound failures.

For every returned `FetchResult`, sort retained postings using the deterministic
ordering rule below. No malformed JSON, row, URL, timestamp, redirect,
`Content-Length`, or rate-limit body may escape as an uncaught JSON, key,
Unicode, URL, or `httpx` exception. No incomplete outcome may return
`complete=True`.

### 7. Deterministic Lever normalization

Normalize each posting independently from the fields documented by Lever's
official v0 Postings API:

- `external_id` comes from `id`. Require a non-empty string after trimming
  leading and trailing whitespace. Preserve its internal text and case; do
  not derive identity from a URL, title, or array position.
- `title` comes from `text`. Require a non-empty string after trimming leading
  and trailing whitespace. Preserve internal text and case.
- `description_md` is assembled from `description`, `lists`, and `additional`
  exactly as described in section 8 below. Do not append `opening` or
  `descriptionBody` because Lever documents `description` as their combined
  content.
- `posting_url` comes only from `hostedUrl`.
- `apply_url` comes only from `applyUrl`.
- Both URL fields are independently required. Each must be an absolute HTTPS
  URL with no embedded credentials, the default HTTPS port, the region's exact
  hosted-jobs origin (`jobs.lever.co` for global or `jobs.eu.lever.co` for
  EU), and a path beginning with the exact endpoint token and posting ID.
  Preserve an accepted query string and fragment exactly. Never copy one field
  into the other, append `/apply`, or construct an application-submission API.
  Both region-specific normal fixtures must use distinct values and tests must
  prove each source field maps to the corresponding contract field.
- `locations` comes from `categories.allLocations` when it is a list of
  strings. Trim each non-empty label, remove exact duplicates while preserving
  first vendor order, and create one `RawLocation` per retained label. If
  there is no usable `allLocations`, preserve a non-empty string
  `categories.location` as the single fallback label. Do not parse labels into
  city or region.
- Preserve a top-level `country` on each emitted `RawLocation.country_code`
  only when it is already an uppercase two-ASCII-letter ISO-shaped string.
  Do not uppercase, geocode, validate membership by inference, or derive it
  from the label. A missing, null, or malformed value becomes `None`.
- Preserve `workplaceType` on each emitted
  `RawLocation.workplace_type` only when it is exactly one of
  `unspecified`, `on-site`, `remote`, or `hybrid`. Any other value becomes
  `None`. Do not infer workplace type from location, title, description, or
  category text.
- Set every emitted location's `region` and `city` to `None`.
- If there is no usable location label, return an empty location list.
  Country and workplace type remain available only in `raw` because the frozen
  contract has no posting-level slot for them; do not synthesize a label.
- `department` comes from a non-empty string `categories.department`, trimmed,
  or `None`. Do not substitute `categories.team`, `categories.commitment`, or
  `categories.level`.
- Set `source_published_at` and `source_updated_at` to `None`. The official v0
  list-field documentation does not define a publication or update timestamp.
  Any undocumented timestamp-like field remains only in `raw`; do not borrow
  the authenticated v1 API's timestamp semantics or guess units/timezones.
- `raw` is a defensive deep copy of the complete decoded posting object,
  including unknown fields. Normalization must not mutate the decoded page,
  the fixture object, or nested caller-visible values.

An invalid `id`, `text`, `description` shape, `lists` shape, `additional`
shape, `hostedUrl`, or `applyUrl` makes that posting invalid. A missing or
malformed optional `categories`, location, country, department,
`workplaceType`, or undocumented timestamp-like field degrades only the
corresponding optional normalized value.

After all pages finish or fail, sort the returned postings lexically by
`(external_id, title, posting_url)`. Never rely on provider page order,
mapping iteration order, a set, or fixture order.

### 8. Deterministic Markdown assembly

Use the existing constrained `markdownify` dependency through a small
Lever-local pure wrapper with fixed options. Lever documents its description
fields as styled HTML, so pass each HTML fragment directly to the parser-backed
converter. Do not apply Greenhouse's preliminary `html.unescape` step and do
not import a private Greenhouse conversion helper.

Build one ordered list of Markdown blocks:

1. Convert the required string `description` as the first block. An empty
   string produces no visible block but is still a valid string value.
2. Treat an absent or null `lists` as empty. When present, require a list in
   which every item is an object with string `text` and string `content`.
   Process items in vendor order. For each item, trim and collapse horizontal
   whitespace in `text`; when the result is non-empty, emit it as an ATX level-
   two heading. Wrap the item's documented unstyled HTML list elements in one
   `<ul>` before conversion so its `content` becomes a Markdown unordered
   list. A malformed item invalidates the row rather than silently dropping
   potentially consequential description content.
3. Treat an absent, null, or empty-string `additional` as no closing block.
   When present and non-empty, require a string and convert it as the final
   block. A present non-string value invalidates the row.

For every converted block and for the final joined result:

- use fixed parser options matching Task 004's ATX headings, `*` bullets,
  emphasis behavior, and script/style suppression;
- normalize CRLF and bare CR to LF;
- remove trailing horizontal whitespace from each line;
- collapse three or more consecutive blank lines to exactly two;
- strip leading and trailing blank lines;
- join non-empty blocks with exactly two LF characters; and
- emit no terminal newline.

Do not summarize, reorder, correct, enrich, classify, or infer description
content. Do not include `opening`, `openingPlain`, `descriptionPlain`,
`descriptionBody`, `descriptionBodyPlain`, `additionalPlain`, salary fields,
or category metadata in `description_md`.

Golden tests must lock the output for paragraphs, headings, links, emphasis,
ordered and unordered lists, list-section headings, multiple list sections,
entities, `<br>`, script/style suppression, CRLF, repeated blank lines, empty
optional sections, and non-ASCII text.

### 9. Global and EU board-URL recognition

Implement `token_patterns()` as a stable deterministic list of compiled
regular expressions. Every pattern must have a named `token` capture and a
named `region` capture with these adapter-local values:

```text
""   -> SourceEndpoint.region == "global"
"eu" -> SourceEndpoint.region == "eu"
```

The empty global capture is deliberate: the global hostname contains no region
label, and changing the shared pattern contract to return a richer recognition
object is out of scope. Tests must assert both capture values so downstream
discovery cannot silently lose the EU distinction.

Recognize these HTTPS shapes:

```text
https://jobs.lever.co/{token}
https://jobs.eu.lever.co/{token}
https://api.lever.co/v0/postings/{token}
https://api.eu.lever.co/v0/postings/{token}
```

Allow an optional trailing slash, a single documented posting-ID path segment,
an optional final `/apply` only on hosted job-site URLs, and optional query or
fragment suffixes. Token recognition must use the same proposed conservative
token allowlist as endpoint validation after that allowlist receives explicit
human approval.

Patterns must be anchored and host-boundary safe. They must reject non-HTTPS
URLs, credential-bearing URLs, alternate ports, lookalike hosts, missing
tokens, extra path segments, path traversal including percent-encoded dot
segments, invalid tokens, and mixing an EU job-site/API host with a global
region result. Recognition performs no network call and does not seed or
persist an endpoint.

### 10. Privacy-safe behavior

The adapter may log only a bounded event category, region, HTTP status,
pagination page count, redirect count, decoded byte count, normalized posting
count, and completeness flag. It must never log:

- an endpoint token or full request/redirect URL;
- a response body, decoded page, raw posting, or description Markdown;
- request or response header dumps;
- conditional or response validator values;
- title, location, country, department, or candidate information; or
- a raw exception string which may contain any of those values.

Public exception strings and serialized failure values must remain equally
bounded. Tests must use unique fixture sentinels for descriptions, tokens,
validators, error bodies, and unsafe redirect URLs and prove that none appears
in logs or exception strings.

## Specification questions requiring human review

These are specification or official-documentation gaps, not permission to
expand implementation. The proposals below are the smallest safe behaviors.
A reviewer who wants different semantics must revise this task or the
specification before T005 is implemented.

### 1. Conservative SITE/token grammar

**Human review — proposed behavior:** use
`[A-Za-z0-9][A-Za-z0-9_-]{0,127}` as a conservative SITE/token allowlist for
both endpoint validation and URL recognition.

Lever's official documentation does not publish an exact grammar for the
`SITE` path value. This proposed allowlist prevents URL, path, and query
injection, but it may reject valid Lever SITE values that contain characters
outside the allowlist. T005 implementation must not begin until a human
reviewer explicitly approves this grammar. If implementation discovers a
concrete valid Lever SITE counterexample, stop the task and request review;
do not expand, normalize, or otherwise weaken the grammar without approval.

### 2. Pagination bounds and consistency

**Human review — proposed behavior:** use `limit=100`, at most 100 page
responses and 10,000 decoded rows, stop only on a page shorter than the limit,
and return `complete=False` on a repeated page, repeated ID, impossible offset,
or exhausted bound.

Lever documents `skip` and `limit` but publishes no supported maximum, total,
cursor, snapshot, or stability guarantee. Sequential requests and duplicate-
page/ID checks catch visible loops and some concurrent changes, but cannot
prove there was no insertion, removal, or reorder between offsets. The
proposal fails closed on every detectable inconsistency and leaves the
remaining concurrency risk visible rather than inventing a complete snapshot.

### 3. Region representation during token recognition

**Human review — proposed behavior:** every Lever regex has both `token` and
`region` named captures; `region == ""` means global and `region == "eu"`
means EU.

The frozen `ATSAdapter.token_patterns()` contract returns only regex patterns
and standardizes only the `token` capture. The empty-string convention retains
region without changing shared types or coupling region to list position. A
richer shared recognition result may be cleaner later, but it would be a
contract change and is not approved for this task.

### 4. Combining `description`, `lists`, and `additional`

**Human review — proposed behavior:** convert Lever's combined `description`
first, then each `lists` item in vendor order as a level-two heading plus an
unordered list, then `additional`; join canonical non-empty blocks with one
blank line.

This avoids duplicating `opening` or `descriptionBody`, preserves the official
field order, and makes Lever's unstyled list elements deterministic Markdown.
Malformed present content invalidates the row rather than silently omitting
requirements or closing text. Use the existing parser dependency through a
Lever-local wrapper; do not pre-unescape styled HTML as Greenhouse does.

### 5. Optional normalized metadata

**Human review — proposed behavior:** preserve labels from
`categories.allLocations` with `categories.location` as a fallback; preserve
an already uppercase two-letter `country`, exact documented
`workplaceType`, and `categories.department`; infer no city or region; and set
both source timestamps to `None`.

The frozen contract has no posting-level country or workplace field, so those
values can be carried only on an existing vendor-supplied location. The
official v0 field table does not document timestamps. Undocumented fields stay
in `raw` instead of borrowing authenticated v1 semantics or guessing units.

### 6. Shared HTTP helper

**Human review — proposed behavior:** keep Lever's HTTP, redirect, pagination,
validator, and Markdown helpers isolated in `lever.py`, even where small pieces
resemble Greenhouse.

The two adapters have materially different origin sets, pagination, validator
scope, payload shape, and content conversion. No third adapter has established
a stable shared abstraction. A generic helper would expand T005 and risk
Greenhouse behavior changes; revisit extraction only after another adapter
demonstrates a genuinely identical policy.

### 7. Conditional validators across pagination

**Human review — proposed behavior:** send stored validators only to `skip=0`
and return only that page's final-response validators.

The frozen `FetchResult` has one validator pair, while each paginated URL may
have distinct validators. Returning the terminal page's validator and sending
it to the first page on the next run would associate it with the wrong
resource. Per-page validator persistence is outside this adapter slice, so the
proposal keeps the one stored pair bound to the first-page URL.

### 8. Representation of `304 Not Modified`

**Human review — proposed behavior:** match Task 004 and return
`FetchResult(postings=[], complete=False, http_status=304, ...)`.

The shared contract has no not-modified variant. Treating an empty unchanged
result as complete could authorize incorrect closing. Later source-fetch
orchestration may interpret `304`; this adapter must fail closed until then.

## Out of scope

- changes to `api/app/sources/ats/contracts.py` or shared ATS contract behavior
- changes to Greenhouse code, tests, fixtures, normalization, HTTP policy, or
  behavior
- database access, SQLAlchemy models, repositories, transaction management,
  migrations, or schema changes
- `pipeline_run`, `source_fetch`, posting, or posting-version persistence
- ingestion, content hashing, hash gates, deduplication, reconciliation,
  close/reopen behavior, or source-fetch state persistence
- company or source-endpoint seeding, upserts, discovery-event persistence, or
  token probing
- CLI commands, FastAPI routes, scheduling, workflow orchestration,
  notifications, or rate limiters
- Ashby or any other ATS, feed, or adapter
- live-network tests, live Lever boards, captured production payloads, real
  board tokens, or request/response dumps
- generic scraping, HTML career-page crawling, browser automation, fallback
  scraping, or robots policy
- application submission, candidate forms, API keys, authenticated Lever v1
  endpoints, or POST rate-limit behavior
- retries, backoff, sleeping, circuit breakers, caches, or pagination
  persistence
- generic HTTP frameworks or refactoring the résumé, Greenhouse, FastAPI,
  database, Compose, deployment, authentication, or infrastructure modules

Do not add placeholders for out-of-scope behavior. Do not revise the
specification to resolve a gap; keep the smallest safe proposal in the review
section and request approval for any contract change.

## Test requirements

Add only invented response-shape fixtures under
`api/tests/fixtures/lever/`:

- `normal-global.json` — at least three valid postings in non-sorted provider
  order, with distinct `hostedUrl` and `applyUrl`, multiple locations,
  country, department, workplace type, description/list/additional content,
  unknown fields, and only synthetic `jobs.lever.co` hosted/application URLs;
- `normal-eu.json` — at least three valid postings in non-sorted provider
  order, with distinct `hostedUrl` and `applyUrl`, multiple locations,
  country, department, workplace type, description/list/additional content,
  unknown fields, and only synthetic `jobs.eu.lever.co` hosted/application
  URLs;
- `empty.json` — exactly an empty top-level JSON array;
- `paginated-page-1.json`, `paginated-page-2.json`, and
  `paginated-terminal.json` — synthetic pages that require at least two
  requests and terminate with a short or empty page;
- `malformed.json` — intentionally invalid JSON bytes; and
- `rate_limited.json` — a small invented error body used with HTTP `429`.

These are synthetic recorded response shapes, not captured provider data.
Keep statuses and response headers in test cases rather than encoding them as
production-like dumps. Do not include a real employer, board token, job text,
URL, validator, candidate detail, or response-header capture.

Use `httpx.MockTransport` or the same small in-process async seam used by
Greenhouse. No Lever test may resolve DNS, open a socket, call a loopback HTTP
server, contact a live board, or depend on internet access. Keep the existing
autouse network guard active and unchanged.

Tests must cover:

- `ATSAdapter` protocol conformance, frozen input values, defensive raw
  snapshots, and no mutation of `SourceEndpoint` or `ConditionalHeaders`;
- exact global and EU initial/page URLs, exact query parameters, fixed user
  agent, `Accept`, timeouts, and allowed token punctuation;
- every invalid endpoint condition, including invalid kind, region, base URL,
  non-string token, token injection, and rejection before client/network use;
- both `normal-global.json` and `normal-eu.json`, plus the empty, paginated,
  malformed, and rate-limited fixtures;
- region-correct fixture URLs: every global normal URL uses
  `jobs.lever.co`, every EU normal URL uses `jobs.eu.lever.co`, and neither
  fixture can pass through cross-region URL acceptance;
- a short first page, full page plus short page, full page plus empty terminal
  page, and stable final sorting independent of provider/page order;
- exact `skip` progression based on decoded row counts and fixed `limit=100`;
- repeated whole pages, repeated IDs within and across pages, a changed row
  reusing an ID, oversized pages, page/row/response/cumulative-byte caps, and
  the absence of a short terminal page at a cap;
- malformed rows on early and later pages, valid rows after malformed rows,
  all-invalid rows, and the rule that malformed rows still advance offsets;
- no conditional headers, each validator independently, both together,
  first-page redirect propagation, no later-page propagation, first-page
  final validators, redirect-hop validator exclusion, and later-page
  validator exclusion;
- `200`, first- and later-page `304`, `429`, representative `4xx`/`5xx`
  statuses, and exact `http_status` plus `complete=False` assertions for every
  unsuccessful returned outcome;
- every supported redirect status, relative redirects, exactly three
  redirects, a fourth redirect, missing/malformed `Location`, HTTPS downgrade,
  cross-region/cross-origin redirects, alternate ports, credentials,
  lookalike hosts, and changes to token/path/query;
- a declared oversized page, streamed oversized page without usable
  `Content-Length`, an exact per-page ceiling, a cumulative overflow, and an
  exact cumulative ceiling;
- invalid UTF-8, malformed JSON, wrong top-level shapes, non-object rows, and
  provider arrays larger than the requested limit;
- required ID/title/content/URL validation and independently mapped,
  distinct `hostedUrl` and `applyUrl`;
- multiple/fallback/empty/malformed locations, exact-order de-duplication,
  country preservation without case repair, recognized and unrecognized
  workplace types, department preservation, and no city/region inference;
- both source timestamps remain `None` even when synthetic unknown timestamp
  fields are present in `raw`;
- exact preservation of unknown nested fields in a defensive `raw` copy;
- golden Markdown assembly for `description`, ordered `lists`, and
  `additional`, including malformed present sections;
- deterministic lexical posting order independent of page and fixture order;
- global and EU job-site/API token patterns, exact region captures, optional
  posting/apply/query/fragment suffixes, and every boundary-negative shape;
- deterministic response/client cleanup on success, failure, size rejection,
  redirect rejection, pagination failure, transport failure, and cancellation;
  and
- privacy-safe logs and exceptions using unique synthetic sentinels.

Typed transport-failure tests must assert that no `FetchResult` is returned.
Tests for incomplete returned outcomes must assert the exact status,
`complete=False`, the expected retained-posting subset, and that no missing
posting could be closed.

## Acceptance criteria

- [ ] `LeverAdapter` implements the existing asynchronous `ATSAdapter`
      protocol with `slug == "lever"` and `rate_limit_per_min == 60`; the
      shared contract is unchanged.
- [ ] Only valid `lever` endpoints in exact `global` or `eu` regions with no
      custom base URL reach the HTTP boundary.
- [ ] Region selects exactly `api.lever.co` or `api.eu.lever.co`; token and
      endpoint injection fail before network I/O.
- [ ] Page requests use only the official public v0 listing path with
      `mode=json`, bounded `skip`, and fixed `limit=100`.
- [ ] HTTPS, timeouts, redirects, decoded page bytes, cumulative bytes, page
      count, row count, and total response count are explicitly bounded.
- [ ] No redirect can change region, origin, credentials, token, path, or
      pagination query, and a fourth redirect is not followed.
- [ ] Conditional validators are sent exactly on the first page and its safe
      redirects only; returned validators come only from that page's final
      response.
- [ ] Sequential pagination terminates only on a short page; visible repeated
      or inconsistent pages and exhausted bounds fail closed.
- [ ] Valid global-normal, EU-normal, or empty terminal sequences are
      complete; every malformed, partial, inconsistent, rate-limited,
      non-success, unsafe-redirect, oversized, or bound-exhausted returned
      outcome is incomplete.
- [ ] `304` follows the reviewed fail-closed representation and cannot be
      mistaken for a complete empty board.
- [ ] Transport failures use a typed redacted exception, cancellation
      propagates, and all responses/owned clients close deterministically.
- [ ] Valid rows from safe prior pages may survive an incomplete later
      outcome, but no incomplete result can authorize reconciliation.
- [ ] IDs, titles, locations, country, department, workplace type, timestamps,
      URLs, and raw payloads follow the explicit no-inference mapping rules.
- [ ] `hostedUrl` and `applyUrl` remain separately required and separately
      mapped; neither is synthesized or copied from the other.
- [ ] `description`, `lists`, and `additional` produce deterministic golden
      Markdown without duplicating opening/body fields.
- [ ] Returned postings use stable lexical ordering independent of provider
      page or fixture order.
- [ ] Token patterns recognize global and EU Lever job-site/API URLs, preserve
      region through the reviewed named capture, and reject unsafe boundary
      cases without I/O.
- [ ] Synthetic fixtures separately cover global normal, EU normal, empty,
      paginated, malformed, and rate-limited responses without production data
      or live-board tests; normal fixture URLs match their own region.
- [ ] Focused tests and the complete pytest suite pass under the existing
      network guard; Ruff lint and format checks pass for all Python under
      `api/`.
- [ ] No database, migration, ingestion, persistence, endpoint seeding, CLI,
      API, workflow, schedule, notification, Greenhouse change, other adapter,
      scraping, or application-submission behavior is introduced.
- [ ] No secret, `.env`, API key, real token, captured payload, private résumé,
      database dump, cache, or generated test artifact is added to Git.

## Validation commands

Run from the repository root unless a command changes directory. These
commands are requirements for the future T005 implementation; this planning
document does not claim that T005 has been implemented.

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

The complete suite must not require a live Lever board or any other external
network service. PostgreSQL-marked tests inherited from earlier tasks must use
only their documented disposable local database setup. Focused fixture tests
must exercise both `normal-global.json` and `normal-eu.json` and reject any
cross-region hosted/application URL.

### Focused network-isolated adapter checks

```bash
cd api
.venv/bin/pytest -m "not postgres" \
  tests/test_ats_contracts.py \
  tests/test_greenhouse_adapter.py \
  tests/test_lever_adapter.py
cd ..
```

Run this with the existing autouse socket guard enabled. Removing a mock
transport must fail the test instead of falling through to DNS, a socket, a
loopback server, or a live Lever endpoint.

### Repository-hygiene checks

```bash
git status --short
git diff --check
git diff --name-only
git diff -- \
  api/app/sources/ats/__init__.py \
  api/app/sources/ats/lever.py \
  api/tests/fixtures/lever \
  api/tests/test_lever_adapter.py
git diff --exit-code -- \
  AGENTS.md \
  docs \
  tasks \
  docker-compose.yml \
  api/pyproject.toml \
  api/migrations \
  api/app/main.py \
  api/app/resume \
  api/app/sources/ats/contracts.py \
  api/app/sources/ats/greenhouse.py \
  api/tests/conftest.py \
  api/tests/test_ats_contracts.py \
  api/tests/test_greenhouse_adapter.py \
  api/tests/fixtures/greenhouse
git status --short --ignored
```

The implementation diff must be limited to `lever.py`, a narrow ATS package
export/docstring update if required, the synthetic Lever fixtures, and
`test_lever_adapter.py`. The existing `httpx` and `markdownify` constraints are
sufficient; `api/pyproject.toml` must not change unless implementation finds a
concrete reviewed blocker. Inspect ignored output and ensure no `.env`, API
key, real provider payload, private résumé, database dump, cache, coverage
artifact, or virtual environment is staged.
