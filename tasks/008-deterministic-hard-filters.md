# Task 008: Deterministic Phase 1 hard filters

**Status:** Ready

**Depends on:** Task 007 — One-endpoint fetch-to-ingestion runner

**Specification:** `docs/jobs-forkstech-spec.html`, especially sections 02–04,
07, 08, 13, and 14

## Approved product boundary

Task 008 is one deterministic eligibility evaluation over one persisted posting and its authoritative current
version. It runs before research, embeddings, semantic deduplication, model calls, and deep scoring.

It evaluates one posting, accumulates all reasons, and performs no network, model, embedding, scoring, workflow, or
batch work. Pure rule evaluation neither reads nor writes a database.

Candidate-specific state eligibility, including “excluding Alaska,” remains a deep-scoring concern. Adding it later
requires a different stage or new approved policy boundary.

## Evaluated input identity

The immutable evaluation identity is:

```text
(
  posting_id,
  posting.current_version_id,
  posting_version.content_hash,
  mutable_state_hash,
  policy_version,
  policy_manifest_hash,
  evaluation_as_of_utc
)
```

- `posting.current_version_id` is the sole content selector and must identify a version owned by the posting;
  timestamp and row order never select content.
- Immutable inputs are `posting_version.title`, `locations`, and `description_md`; never read `raw_payload`,
  embeddings, or incidental fields.
- Mutable posting inputs are `source_published_at`, `first_seen_at`, `closed_at`, and the current pointer; mutable
  content projections are never filter inputs.
- Mutable company input is exactly `company.blocked`.
- Action-state inputs are only the privacy-safe `digest_item` and linked `score.posting_version_id` facts below.
- `mutable_state_hash` covers canonical mutable values evaluated, not provider data.
- `evaluation_as_of_utc` is an aware UTC instant fixed by the caller for retries; evaluation never reads the clock.

An `A → B → A` transition reuses historical A. A null, missing, or cross-posting pointer is invalid persisted state;
filtering repairs nothing. A content pointer, mutable state, policy, or evaluation-time change creates a new
identity and never overwrites history. Closed postings fail the callable precondition, not a tenth rule.

`posting.department` is excluded because Task 006 does not version it or
refresh it for a hash-stable observation; using it in a later deterministic
policy requires a separately approved ingestion/versioning change.

## Public callable

```python
def evaluate_hard_filters(
    *,
    engine: Engine,
    posting_id: int,
    policy_version: str,
    evaluation_as_of: datetime,
) -> HardFilterResult: ...
```

The caller owns the supplied synchronous `Engine`; Task 008 never calls `engine.dispose()`. It owns and closes every
connection, transaction, cursor/result object, and other resource it opens on success, replay, typed failure, stale
input, database failure, cancellation, or unexpected exception. Cleanup must leave the Engine reusable.
`posting_id` is a positive non-Boolean integer. `policy_version` matches
`[A-Za-z0-9][A-Za-z0-9._-]{0,127}` and Task 008 supports exactly
`phase1-hard-filters-v1`. `evaluation_as_of` is timezone-aware with zero UTC
offset and is normalized without a clock. Unknown policy versions or
code/manifest/hash disagreement raise `filter.policy_unsupported`. No other
user-owned input is selected implicitly.

## Canonical result vocabulary

### Reasons, unknowns, sources, and statuses

`HardFilterReason` is a closed ordered enum containing exactly:

```text
title_no_match, seniority_low, wrong_discipline, not_remote, geo_excluded,
clearance_required, stale, already_actioned, company_blocked
```

`HardFilterUnknown` is the closed ordered enum:

```text
remote_arrangement_unresolved, remote_geography_unresolved,
clearance_not_stated, clearance_ambiguous
```

Only rules that can complete with insufficient evidence may emit an unknown.
An unknown never independently rejects, implies the opposite fact, or silently
becomes a pass; it appears at most once, uses the order above, and is persisted
and replayed exactly.

`StalenessSource` is closed and contains exactly:

```text
source_published_at, first_seen_at
```

Every completed evaluation has exactly one source. Use
`source_published_at` when usable; otherwise use `first_seen_at`. Future-time
behavior follows approved decision 9.

`RuleStatus` is closed and contains exactly:

```text
pass, reject, unknown
```

### Per-rule outcomes and evidence

`HardFilterRule` has exactly the same nine members and order as
`HardFilterReason`.

`HardFilterEvidence` is one closed ordered enum, ordered by rule and then by
evidence precedence within that rule, containing exactly:

```text
allowed_title_match, allowed_title_absent,
excluded_seniority_absent, excluded_seniority_match,
excluded_discipline_absent, excluded_discipline_match,
remote_permitted, attendance_required, remote_arrangement_unresolved,
us_not_excluded, remote_scope_excludes_us, remote_geography_unresolved,
active_clearance_not_required, active_clearance_required, clearance_not_stated, clearance_ambiguous,
within_45_days, older_than_45_days,
no_qualifying_action, prior_skipped, prior_applied, current_version_expired,
company_not_blocked, company_blocked
```

No arbitrary evidence string is valid. `HardFilterRuleOutcome` contains
exactly:

```python
rule: HardFilterRule
status: RuleStatus
evidence: HardFilterEvidence
```

`rule_outcomes` always contains exactly nine entries in filter order, one per
rule. Evidence is restricted by this complete policy-owned matrix:

| Rule | `pass` evidence | `reject` evidence | `unknown` evidence |
| --- | --- | --- | --- |
| `title_no_match` | `allowed_title_match` | `allowed_title_absent` | forbidden |
| `seniority_low` | `excluded_seniority_absent` | `excluded_seniority_match` | forbidden |
| `wrong_discipline` | `excluded_discipline_absent` | `excluded_discipline_match` | forbidden |
| `not_remote` | `remote_permitted` | `attendance_required` | `remote_arrangement_unresolved` |
| `geo_excluded` | `us_not_excluded` | `remote_scope_excludes_us` | `remote_geography_unresolved` |
| `clearance_required` | `active_clearance_not_required` | `active_clearance_required` | `clearance_not_stated` or `clearance_ambiguous` |
| `stale` | `within_45_days` | `older_than_45_days` | forbidden |
| `already_actioned` | `no_qualifying_action` | `prior_skipped`, `prior_applied`, or `current_version_expired` | forbidden |
| `company_blocked` | `company_not_blocked` | `company_blocked` | forbidden |

The approved manifest freezes the enum and allowed rule/status/evidence
triples. Every completed outcome has non-null evidence, and every evidence
value is legal only for the combinations shown in its own matrix row. If
multiple action facts qualify, the evidence precedence is
`prior_skipped`, `prior_applied`, `current_version_expired`; the reason still
appears once.

For each outcome:

- `reject` projects its `rule` into `rejection_reasons`;
- `unknown` projects its evidence into `unknowns`;
- `pass` projects neither; and
- any disallowed triple, missing entry, duplicate entry, order error, or
  disagreement with either projection is invalid persisted output.

No status may use evidence from another rule. Replay validates enum membership
and the exact rule/status/evidence triple. The projections use their enum
orders, not discovery order. No matched text, title, location label, body
excerpt, regex capture, URL, provider value, or arbitrary diagnostic is stored.

### HardFilterResult

`HardFilterResult` contains exactly:

```python
evaluation_id: int
posting_id: int
posting_version_id: int
policy_version: str
policy_manifest_hash: str
mutable_state_hash: str
input_hash: str
result_hash: str
evaluation_as_of: datetime
eligible: bool
rejection_reasons: tuple[HardFilterReason, ...]
unknowns: tuple[HardFilterUnknown, ...]
staleness_source: StalenessSource
rule_outcomes: tuple[HardFilterRuleOutcome, ...]
replayed: bool
```

IDs are positive and hashes are lowercase 64-character hexadecimal SHA-256.

`eligible` is true exactly when `rejection_reasons` is empty and evaluation
completed without a typed error. Unknowns do not make `eligible` false.

The result contains no content, names, labels, URLs, external IDs, matched phrases, raw provider data, or arbitrary diagnostics.

## Normalization and deterministic matching

For immutable title, description, and location strings, require a string or use its missing rule; apply NFKC and
`casefold()`; map `’`, `‘`, `ʼ`, and `＇` to ASCII `'`; treat whitespace, Unicode dashes, `/`, `\`, `_`, and other
punctuation as boundaries except apostrophes between letters; form maximal Unicode Letter-or-Number tokens; compare
ordered token sequences only.

Never use unrestricted substrings. Hyphenated/slash compounds form separate tokens. There is no accent stripping,
stemming, singularization, implicit abbreviation expansion, fuzzy/semantic matching, locale lookup, or taxonomy.
Matching never depends on original capitalization. Free-text two-letter tokens are never looked up as country or
state/DC codes; only complete manifest-listed token sequences and templates may match. For geography, an approved
exclusion template owns its matched token span: an inclusion sequence contained wholly inside that same span does
not independently match, while a separate non-overlapping inclusion occurrence remains independent evidence. For
remote arrangement, an approved denial template likewise owns its matched token span: a positive remote sequence
contained wholly inside that span does not independently match, while separate structured positive evidence or a
separate non-overlapping positive text occurrence remains independent evidence.

Every phrase, abbreviation, precedence, matched-span ownership rule, evidence mapping, and declared input field
lives in the manifest. Content patterns may declare only immutable title, description, and locations, and every
pattern declares which of those fields it may inspect. Generated regex uses escaped approved tokens and is tested
against the token matcher.

Every rule is independently decisive, runs without short-circuiting, and emits
its reason at most once. No rule exists for sponsorship, general work
authorization, citizenship, residency, compensation, employment type,
contract/temporary status, company activity, endpoint kind, or source kind.

## Missing and malformed inputs

Legend: P = pass, R = reject, U = completed unknown, I = typed invalid
persisted state with no evaluation row.

| Rule | Definite P | Definite R | Missing or unresolved | Malformed |
| --- | --- | --- | --- | --- |
| Title match | approved sequence | none matched | I; version title required | I |
| Seniority | no excluded token | excluded token | I; title required | I |
| Discipline | no title-only or compound exclusion | approved title-only or compound exclusion | description-unavailable compound branch contributes nothing; the rule still completes P or R | I for an invalid required field |
| Remote reality | remote permitted | attendance required | U | U |
| Geography | US not excluded | remote scope excludes US | U | U |
| Clearance | explicit no requirement | active clearance required | U: not stated or ambiguous | U |
| Staleness | age at most 45 days | age over 45 days | fallback to `first_seen_at` | I if both unusable or selected time is future |
| Already actioned | no qualifying digest fact | qualifying digest fact | P when no digest fact exists | I for corrupt existing relationships |
| Blocked company | false | true | I; non-null column | I |

Unknowns never silently become passes. Unsupported JSON produces the approved unknown or a closed persisted-input
error; it never creates a rejection or open-ended exception.

For discipline, the five adjacent-discipline sequences inspect only a valid immutable title. Unavailable usable
description text contributes nothing to the special product-owner/business-analyst branch and emits no unknown.
The discipline rule always completes as `pass` with `excluded_discipline_absent` or `reject` with
`excluded_discipline_match`; an independently invalid required immutable field still raises the existing closed
persisted-input error. For remote reality, a missing, malformed, or unrecognized structured
`workplace_type` and text that satisfies no complete positive, denial, or mandatory template contribute no definite
evidence; unresolved results use `remote_arrangement_unresolved`. For geography, a missing, malformed, or
unrecognized structured `country_code`, including a value outside the recognized ISO country-code inventory,
contributes no definite evidence. Absent other decisive geography evidence, the result is
`remote_geography_unresolved`.

## Canonical JSON and hashes

Canonical JSON is UTF-8, recursive lexical object-key order, compact separators `(",", ":")`,
`ensure_ascii=False`, no floats, and lowercase SHA-256. Timestamps are UTC RFC 3339 with exactly six fractional
digits and terminal `Z`. Arrays retain policy-defined orders.

The canonical mutable-state object has exactly `action_state`, `closed_at`, `company_blocked`, `current_version_id`,
`first_seen_at`, and `source_published_at`. `action_state` is sorted by `digest_item_id`; each entry has exactly
`digest_item_id`, `posting_id`, `score_posting_version_id`, and `state`. This privacy-safe semantic projection omits
`score_id`, skip reason, and text evidence, but the stale fence retains and rechecks the exact digest-to-score
relationship that produced each evaluated version ID. `mutable_state_hash` hashes these exact canonical bytes.

Canonical input JSON has exactly the lexical top-level keys `evaluation_as_of`, `mutable_state`,
`mutable_state_hash`, `policy_manifest`, `policy_manifest_hash`, `policy_version`, `posting_id`,
`posting_version_content_hash`, `posting_version_id`, and `schema_version`.

`schema_version` is integer `1`. Both embedded objects are complete canonical objects. `input_hash` hashes this JSON,
and both embedded hashes must recompute exactly.

Canonical output JSON has exactly `eligible`, `rejection_reasons`, `rule_outcomes`, `schema_version`,
`staleness_source`, and `unknowns`.

Reason, outcome, and unknown arrays use their defined orders. Each outcome object has exactly `evidence`, `rule`, and
`status` in lexical order. `result_hash` hashes this JSON. Replay validates every identity field, JSON shape,
projection, hash, and result; persisted output containing any removed legacy discipline unknown or evidence value is
invalid, and any disagreement raises the closed conflict error.

## Approved decisions

All twelve decisions below are authoritative for `phase1-hard-filters-v1`.

### 1. Allowed title families

Hard filtering decides only whether immutable `posting_version.title` belongs to an approved management family.
After normalization, the title passes when one exact token sequence occurs contiguously:

```text
product manager, technical product manager, platform product manager,
product operations manager, program manager, technical program manager,
project manager, technical project manager, delivery manager,
technical delivery manager, product lead, program lead, delivery lead,
product management, program management, technical program management, tpm
```

`tpm` is a full token. A sequence may be surrounded by `senior`, `sr`, `principal`, `staff`, `lead`, `group`,
`director`, `head`, `technical`, or `platform`; modifiers need not be stripped. Do not accept bare `pm`, partial
tokens, inferred synonyms, fuzzy/semantic matches, or a family found only in description text. Thus
`Director, Product Management` passes, while `Product Marketing Manager` does not match `product manager`.

The seniority floor rejects full title tokens `intern`, `junior`, `associate`, `coordinator`, or `analyst`.
Technical depth, software/platform relevance, industry fit, and résumé comparison belong to later scoring.

### 2. Adjacent disciplines

Reject these exact normalized token sequences only when they occur in immutable
`posting_version.title`:

```text
product marketing, product design, product designer,
sales engineer, sales engineering
```

Description text is not searched for those five sequences. Collaboration phrases such as `partner with product
design and sales engineering` or `support product marketing launches` therefore do not reject an otherwise valid
management title.

The special product-owner-as-business-analyst branch rejects only when either normalized title contains both exact
sequences `product owner` and `business analyst`, or one normalized description sentence contains both exact
sequences. The description is read for this compound branch only; when usable description text is unavailable, that
branch contributes nothing. The sequences in different description sentences do not combine. Bare `ba`,
requirements gathering, backlog ownership, stakeholder work, agile terminology, or collaboration does not imply
either sequence or the compound branch. A role may pass the title-family rule and independently reject discipline.

### 3. Remote evidence

Structured `workplace_type` means: `remote` is positive; `hybrid` and `on-site` require attendance; `unspecified`,
null, malformed, or unrecognized values have no definite conclusion. Approved positive body sequences are:

```text
fully remote, 100 remote, remote position, remote role,
work remotely, work from home
```

The exact remote-denial body sequences are:

```text
this role is not remote, this position is not remote, this job is not remote,
not a remote role, not a remote position, not a remote job,
no remote option, no remote work option, no work from home option,
remote work is not offered, work from home is not offered,
employees cannot work remotely, employees cannot work from home,
employees can not work remotely, employees can not work from home
```

A matched denial template owns its complete token span. A positive sequence wholly contained in that same span does
not independently count: `not a remote role` does not also match positive `remote role`; `employees cannot work
remotely` does not also match `work remotely`; and `no work from home option` does not also match `work from home`.
Structured `remote` or a positive text occurrence outside the denial span remains separate positive evidence.

The exact role-arrangement sequences that require attendance are:

```text
hybrid role, hybrid position, hybrid schedule,
onsite role, onsite position, on site role, on site position,
in office role, in office position
```

The exact normalized mandatory-attendance sequences are:

```text
required to work from the office
must work from the office
required to work from our office
must work from our office
required to work from a company office
must work from a company office
required to be in the office
must be in the office
required to report to the office
must report to the office
required to report onsite
must report onsite
required to report on site
must report on site
required to work onsite
must work onsite
required to work on site
must work on site
required onsite
must be onsite
required on site
must be on site
```

A weekly-attendance template requires all five conditions in one normalized sentence:

1. a number token from the closed set `1`, `2`, `3`, `4`, `5`, `6`, `7`, `one`, `two`, `three`, `four`, `five`,
   `six`, or `seven`;
2. `day` or `days` within two tokens of that number;
3. `per week`, `a week`, `each week`, or `weekly` within four tokens of the `day`/`days` token;
4. `office`, `in office`, `in the office`, `onsite`, or `on site` within eight tokens, in either direction, of the
   `day`/`days` token; and
5. at least one of `required`, `must`, `expected`, `work`, `working`, `report`, or `attendance` in that same sentence.

If any condition is absent, the weekly template emits no `attendance_required`; another approved mandatory template
may still decide the rule. Bare `office`, `workplace`, `location`, `onsite`, `on site`, `in office`, or `in the
office` independently emits no mandatory evidence. A `home office` occurrence never satisfies an attendance
obligation merely because it contains `office`.

Apply the remote-arrangement rule in this exact order:

1. structured `hybrid` or structured `on-site` rejects with `attendance_required`;
2. any approved mandatory-attendance role, obligation, or weekly template rejects with `attendance_required`;
3. approved remote-denial evidence with no separate structured or non-overlapping text positive evidence rejects
   with `attendance_required`;
4. approved denial evidence plus separate structured or non-overlapping text positive evidence, with no stronger
   mandatory evidence from steps 1–2, is unknown with `remote_arrangement_unresolved`;
5. positive structured or text remote evidence with no denial or mandatory evidence passes with
   `remote_permitted`; and
6. no decisive evidence is unknown with `remote_arrangement_unresolved`.

For multiple locations, structured `remote` is positive and an optional office alone does not reject. Mandatory
evidence from steps 1–2 retains precedence over generic or structured positive evidence. A positive sequence
contained inside a denial span never creates a conflict.

### 4. Non-US geography

Apply this exact order: (1) determine positive remote evidence under decision 3; (2) collect explicit US inclusion;
(3) collect explicit US exclusion; (4) collect broad worldwide/international evidence; (5) collect recognized
non-US-only scopes; (6) apply the closed matrix. Mixed structured/body evidence uses the same precedence.

Two-letter ISO country codes are interpreted only from the existing dedicated structured `country_code` field, after
case normalization. Structured `country_code == "US"` is explicit US inclusion; any other recognized ISO country
code is non-US-only scope evidence. A malformed or unrecognized structured code contributes no definite conclusion.
The persisted location object has `country_code` and a generic `region`, but no dedicated state/region-code field.
Therefore this policy recognizes no US state/DC postal abbreviation from structured `region`, free-text location
labels, title, or description, and adds no field.

Free-text explicit US inclusion is limited to exactly these normalized sequences or templates:

```text
usa, united states, united states of america, us based, based in the us,
within the us, remote in the us, available in the us,
open to candidates in the us, us only
```

Country-level explicit US-exclusion patterns are exactly:

```text
excluding the us, excluding the usa, excluding usa, excluding the united states,
except the us, except the usa, except usa, except the united states,
outside the us, outside the usa, outside the united states,
not available in the us, not available in the usa, not available in usa,
not available in the united states, not available to us applicants,
not open to candidates in the us, not open to candidates in the united states,
us applicants excluded, usa applicants excluded, united states applicants excluded,
usa excluded, united states excluded
```

An approved exclusion template owns its matched token span. Thus `except the United States` supplies exclusion but
does not also supply inclusion from its contained `united states`; a separate non-overlapping inclusion such as
`Remote in the US, but not available to US applicants` still supplies both kinds of evidence and is unknown.

Broad evidence is `global`, `worldwide`, `anywhere`, `North America`, or
`Americas`. Named non-US-only free-text evidence is:

```text
EMEA, EU, European Union, Europe, UK, United Kingdom, APAC, Asia Pacific,
LATAM, Latin America, Canada, Australia, New Zealand, India
```

Recognized two-letter non-US ISO country codes count only through structured `country_code`; no general country-code
or state-code lookup is performed on free text. The closed manifest-listed `EU` and `UK` sequences remain named
aliases, not inferred ISO codes. In particular, bare free-text `us`, `in`, `or`, `me`, `ca`, `hi`, `wa`, or any
other unlisted two-letter token is not geographic evidence; token `us` is evidence only inside an approved
contextual template above.

| Evidence | Geography outcome |
| --- | --- |
| explicit US inclusion, no explicit exclusion | pass: `us_not_excluded` |
| explicit US exclusion, no explicit inclusion | reject: `remote_scope_excludes_us` |
| explicit US inclusion and exclusion | unknown: `remote_geography_unresolved` |
| broad evidence, no explicit exclusion | pass: `us_not_excluded` |
| broad evidence plus explicit exclusion, no explicit inclusion | reject: `remote_scope_excludes_us` |
| every explicit scope is recognized non-US-only | reject: `remote_scope_excludes_us` |
| otherwise unscoped, contradictory, malformed, or unrecognized | unknown: `remote_geography_unresolved` |

Generic broad scope never overrides explicit exclusion, and explicit inclusion never silently overrides exclusion.
Required exact tests are:

- `Join us in building the platform` does not establish US eligibility;
- `Work in a collaborative environment` identifies neither India nor Indiana;
- `Choose X or Y` does not identify Oregon;
- `Contact me for details` does not identify Maine;
- free-text `CA` alone identifies neither California nor Canada;
- structured `country_code="US"` is explicit US inclusion;
- structured `country_code="IN"` is non-US-only scope evidence;
- `Remote in the US` is explicit US inclusion;
- `Remote worldwide` passes;
- `Remote worldwide, except the United States` rejects;
- `Remote worldwide, excluding us from consideration` does not match an exclusion template merely because it
  contains pronoun `us`, so its broad scope passes;
- `Remote in the US and Canada` passes;
- `Remote in the US, but not available to US applicants` is unknown;
- `Remote across Europe and Canada` rejects;
- `Remote, excluding Alaska` produces no country rejection;
- structured remote US plus body `excluding the United States` is unknown;
- structured remote CA-only scope rejects;
- an unscoped remote role is unknown; and
- malformed or absent structured codes fall through to the approved unknown behavior when no other evidence decides.

`excluding Alaska`, `excluding AK`, `excluding certain states`, and other state-only restrictions remain deep
scoring. Add no candidate comparison, geocoder, legal interpretation, reason, unknown, status, or evidence value.

### 5. Active security clearance

Reject only a definite currently held active-clearance requirement. Exact
requirement patterns are `active security clearance`, `current security
clearance`, `active secret clearance`, `current secret clearance`,
`active top secret clearance`, `current top secret clearance`,
`active ts sci clearance`, and `current ts sci clearance`, plus `must possess`,
`requires`, or `required` followed by one of those recognized clearance
phrases.

Exact no-requirement patterns `no security clearance required`,
`security clearance not required`, and `no active clearance required` produce
`active_clearance_not_required`. These obtainability phrases do not reject:
`ability to obtain`, `able to obtain`, `eligible to obtain`,
`willing to obtain`, `must be able to obtain`, and `can obtain`.

Requirement conflicting with no-requirement or obtainability evidence produces `clearance_ambiguous`; no approved
conclusion produces `clearance_not_stated`. Public trust, background checks, export controls, citizenship,
residency, sponsorship, and work authorization do not reject.

### 6. Unknown semantics

The exact four-code inventory is the `HardFilterUnknown` enum above. The discipline rule cannot produce `unknown`.
Unknown outcomes complete successfully, never independently reject or imply an
opposite fact, never become passes, remain eligible absent rejection reasons,
and persist/replay exactly. The rule/status/evidence matrix is authoritative.

### 7. Applied digest state

Existing digest history follows
`digest_item.score_id → score.posting_version_id → posting_version.posting_id`.
Each materialized fact contains `digest_item.id`, `posting_id`, `state`,
`score_id`, and linked `score.posting_version_id`, ordered by `digest_item.id`;
the two reached posting IDs must agree.

`skipped` rejects with `prior_skipped`; `applied` rejects with `prior_applied`;
`restored` and `recommended` do not reject; `expired` rejects with
`current_version_expired` only when linked to `posting.current_version_id`.
Expiry on an older version does not reject changed content. One qualifying row
suffices; another qualifying row is not canceled by `restored`, and evidence
precedence is `prior_skipped`, `prior_applied`, `current_version_expired`.

No `application` table exists or contributes evidence. Adding it later requires
a new policy version.

### 8. Closed postings

Non-null `posting.closed_at` raises `PostingClosedError` with
`filter.posting_closed` and persists no evaluation. Closed state is a callable
precondition, not a rejection rule.

### 9. Future publication timestamps

Select valid non-null `source_published_at`, otherwise `first_seen_at`.
Staleness rejects only when the selected timestamp is strictly earlier than
`evaluation_as_of - 45 days`; exactly 45 days passes. A selected timestamp
later than `evaluation_as_of` raises `FilterPersistedInputError` with
`filter.persisted_input_invalid` and persists nothing.

### 10. Dedicated persistence table

Add only migration `revision = "0004_hard_filter_evaluation"` with
`down_revision = "0003_posting_current_version"`. It creates only
`hard_filter_evaluation`:

| Column | Definition |
| --- | --- |
| `id` | bigint identity primary key |
| `posting_id`, `posting_version_id` | bigint, not null |
| `policy_version` | text, not null |
| `policy_manifest_hash`, `mutable_state_hash`, `result_hash` | text, not null |
| `input_hash` | text, not null, unique |
| `policy_manifest`, `input_payload`, `output_payload` | JSONB, not null |
| `evaluation_as_of` | timestamptz, not null |
| `eligible` | boolean, not null |
| `rejection_reasons`, `unknowns` | JSONB, not null |
| `staleness_source` | text, not null |
| `rule_outcomes` | JSONB, not null |
| `created_at` | timestamptz, not null, default `now()` |

The composite `hard_filter_evaluation_posting_version_owner_fkey` maps `(posting_id, posting_version_id)` to
`posting_version(posting_id, id)` with `ON DELETE RESTRICT`. Stable object names are:

```text
hard_filter_evaluation_pkey, hard_filter_evaluation_posting_version_owner_fkey, hard_filter_evaluation_input_hash_key,
hard_filter_evaluation_positive_ids_check, hard_filter_evaluation_policy_version_check,
hard_filter_evaluation_policy_manifest_hash_check, hard_filter_evaluation_mutable_state_hash_check,
hard_filter_evaluation_input_hash_check, hard_filter_evaluation_result_hash_check,
hard_filter_evaluation_policy_manifest_object_check, hard_filter_evaluation_input_payload_object_check,
hard_filter_evaluation_output_payload_object_check, hard_filter_evaluation_rejection_reasons_array_check,
hard_filter_evaluation_unknowns_array_check, hard_filter_evaluation_rule_outcomes_array_check,
hard_filter_evaluation_rule_outcomes_count_check, hard_filter_evaluation_staleness_source_check,
hard_filter_evaluation_eligible_reasons_check, hard_filter_evaluation_current_lookup_idx
```

Checks enforce positive IDs, policy-version format, lowercase 64-hex hashes, object manifest/input/output, array
reasons/unknowns/outcomes, nine outcomes, closed staleness source, and
`eligible == (rejection_reasons is empty)`. Application validation enforces exact enums, order, projections,
canonical bytes/hashes, and rule/status/evidence triples before insert/replay.

The unique `input_hash` and
`hard_filter_evaluation_current_lookup_idx` on
`(posting_id, posting_version_id, policy_version, evaluation_as_of DESC)` are
the only required indexes. Task 008 exposes no update/delete operation and
never mutates an evaluation; add no append-only trigger. Concurrent identical
inserts use the unique identity: the loser rolls back, validates the winner,
and returns `replayed=True`. Do not write filter-stage `score` or dual-write.

### 11. Database-enforced locking

Use no advisory-lock option. Task 008 owns and closes the transaction and all resources opened for this short
persistence/replay phase; acquire locks and act in this exact order:

1. `LOCK TABLE digest_item IN SHARE MODE`;
2. re-read the complete joined action fact set;
3. lock every linked `score` row `FOR SHARE` in ascending `score.id`;
4. validate every linked `score.posting_version_id`;
5. lock the target `posting` row `FOR UPDATE`;
6. lock its selected `posting_version` row `FOR SHARE`;
7. lock the linked `company` row `FOR SHARE`;
8. recheck every canonical mutable and immutable identity input;
9. insert or validate/replay the evaluation; and
10. commit and release every lock.

The table lock fences digest inserts, updates, and deletes; score locks fence
target and version changes. Table-lock-first prevents holding posting/company
locks while waiting for a digest writer's normal table-level lock.

Recheck that no qualifying digest item was inserted or removed; no evaluated
state or joined `score_id` fact changed; every linked score still exists with
its evaluated version; posting IDs still agree through the join; and pointer,
posting/company value, policy, and every evaluated input remain identical.
Any mismatch raises `StaleFilterInputError` with `filter.input_stale` and
persists nothing. The coarse lock is appropriate for this short, single-user,
single-posting phase and may change only through separate review backed by
operational evidence. Add no lock trigger or digest schema.

### 12. Policy ownership

The sole policy is the code-owned immutable manifest
`phase1-hard-filters-v1`. Load exactly that built-in manifest, canonicalize and
hash it, copy the complete canonical manifest into every evaluation, and verify
version/hash on execution and replay. Unknown versions or disagreement raise
`filter.policy_unsupported`.

The manifest freezes every phrase, rule, normalization, field ownership, evidence mapping, four-code unknown
behavior, threshold, structured field interpretation, decision 2 title-only and compound-discipline boundaries,
decision 3 positive and denial inventories, the closed mandatory-attendance sequence inventory, weekly-template
token-distance conditions, remote span ownership and precedence, and decision 4 phrase inventory, code-source
restriction, exclusion-span ownership, and precedence matrix. The manifest contains no removed legacy enum or
evidence value. Any change requires a new policy version. Add no policy table, environment phrase list, mutable
global setting, or fallback version.

## Relationship to later scoring

Later scoring requires current posting/version, policy version/hash, evaluation instant, mutable-state hash, and
`eligible=True`. A stale or missing evaluation requests a new one; it is not rejection. Scoring cannot reinterpret
unknowns.

Deep scoring owns individual-state eligibility and candidate-specific geography. Task 008 builds no orchestrator,
model score, verdict, similarity, research, ranking, queue membership, or digest selection.

## Closed error model and observability

The exception/code mapping is exact:

| Exception | `FilterErrorCode` |
| --- | --- |
| `FilterInputError` | `filter.invalid_input` |
| `PostingNotFoundError` | `filter.posting_not_found` |
| `PostingClosedError` | `filter.posting_closed` |
| `CurrentVersionError` | `filter.current_version_invalid` |
| `FilterPersistedInputError` | `filter.persisted_input_invalid` |
| `UnsupportedFilterPolicyError` | `filter.policy_unsupported` |
| `StaleFilterInputError` | `filter.input_stale` |
| `FilterEvaluationConflictError` | `filter.evaluation_conflict` |
| `FilterDatabaseError` | `filter.database_error` |
| `FilterEvaluatorDefectError` | `filter.evaluator_defect` |

Exceptions expose only their enum code. Invalid input/state, stale reads, policy mismatches, defects, and database
failures never become reasons or persist a result.

Safe logs contain only event name, database IDs, policy version, Boolean, closed codes, replay, counts, and duration.
Never log titles, descriptions,
locations, company names, external IDs, URLs, payloads, matched phrases,
compensation or clearance text, database URLs, SQL parameters, raw exception
text, or stack traces.

## Required implementation and tests

Add only migration `0004_hard_filter_evaluation`, `api/app/filters/`, focused tests, and migration-chain test
updates. Use pure evaluation, typed policy, SQLAlchemy Core, parameterized SQL, explicit transactions, deterministic
cleanup on every exit, declared columns only, no settings-owned criteria, and never dispose the caller's Engine.

Deterministic tests cover every rule/evidence triple, canonical/hash/error/privacy boundary, every geography matrix
branch and adversarial example above, and these discipline cases:

- title `Product Marketing Manager` rejects;
- title `Product Design Manager` rejects;
- title `Product Designer` rejects;
- title `Sales Engineer` rejects;
- title `Sales Engineering Manager` rejects;
- description-only `partner with product design` does not reject;
- description-only `work closely with product marketing` does not reject;
- description-only `collaborate with sales engineers` does not reject;
- one description sentence containing both `product owner` and `business analyst` rejects;
- the two sequences in different description sentences do not trigger the compound branch;
- bare `BA` does not trigger the compound branch;
- generic requirements, backlog, or agile language does not trigger the compound branch;
- discipline pass uses `excluded_discipline_absent`;
- discipline rejection uses `excluded_discipline_match`;
- unavailable usable description contributes nothing to the compound branch and emits no unknown;
- replay rejects persisted output containing the removed legacy discipline-unknown value; and
- the manifest contains no removed legacy value.

Remote-evidence tests include:

- `Employees are expected to work in the office three days per week` rejects;
- `Must be onsite 2 days a week` rejects;
- `Hybrid role` rejects;
- structured `workplace_type="hybrid"` rejects;
- `Must work from the office` rejects;
- `Required to report onsite` rejects;
- `Must work on site` rejects;
- `Must work from a location within the United States` does not match an attendance obligation;
- `Must work from any approved location` does not match an attendance obligation;
- `Must work from a quiet home office` does not match an attendance obligation;
- `Must work from your home office` does not match an attendance obligation;
- bare `workplace` does not match an attendance obligation;
- `30 days to set up your home office` does not reject;
- `The office is open three days per week` does not reject through the weekly-attendance template;
- `Onsite experience is helpful` does not independently reject;
- `Customer on site documentation` does not independently reject;
- bare `in office` without an approved arrangement or obligation template does not independently reject;
- `Work with us in office technology` does not independently reject;
- `This role is not remote` rejects;
- `This position is not remote` rejects;
- `Not a remote role` rejects;
- `No remote work option` rejects;
- `Employees cannot work remotely` rejects;
- `Employees cannot work from home` rejects;
- contained `remote role`, `work remotely`, and `work from home` do not produce positive evidence inside a matched
  denial span;
- `This role is not remote. A different division offers a fully remote role` emits
  `remote_arrangement_unresolved`;
- structured `workplace_type="hybrid"` plus positive remote text still rejects;
- an approved weekly-attendance template plus positive remote text still rejects;
- positive remote evidence without denial or attendance evidence passes; and
- unresolved remote evidence emits the existing `remote_arrangement_unresolved`.

Engine-ownership tests call the service, prove its connection/result resources closed, reuse the same Engine
successfully, and prove `engine.dispose()` was not called. Patch network/model entry points to fail. PostgreSQL 16 +
pgvector tests cover migration cycles, identity/replay, digest/score relations, structured/body geography conflicts,
the lock sequence, stale rollback, cleanup paths, currentness, and privacy. Use guarded `TEST_DATABASE_URL`, never
SQLite, and only invented rows.

## Out of scope

Batch/workflow invocation, scheduling/retries, discovery/adapters/ingestion,
posting mutation, deduplication, embeddings, research/models/scoring,
ranking/digests, notifications/routes/UI, future application/outcomes,
deployment/auth/infrastructure, résumé work, compensation, exchange rates,
geocoding, and legal interpretation remain out of scope.

## Acceptance criteria

- [ ] The implementation matches all 12 approved decisions and immutable `phase1-hard-filters-v1` manifest.
- [ ] The callable has exactly the signature above, evaluates one posting, and selects no batch or scoring input.
- [ ] `current_version_id` selects content, including `A → B → A`.
- [ ] Exactly nine ordered outcomes project all ordered, deduplicated reasons and unknowns.
- [ ] `HardFilterEvidence` is exact, non-null, rule/status-legal, and insert/replay rejects arbitrary or invalid triples.
- [ ] `HardFilterUnknown` contains exactly the four approved members in order; replay rejects the removed legacy
      discipline-unknown value, and the manifest contains no removed enum or evidence value.
- [ ] Unknowns never reject, imply an opposite fact, or silently become passes; `wrong_discipline` cannot produce
      `unknown`, always completes with the approved pass or reject evidence, and unavailable description emits no
      unknown.
- [ ] Only immutable version title, description, and locations supply content; mutable content projections are unread.
- [ ] The five adjacent-discipline sequences inspect title only; within the discipline rule, description contributes
      only to the exact same-sentence product-owner/business-analyst compound branch and collaboration language does
      not reject.
- [ ] Remote evidence implements every exact arrangement, closed mandatory-attendance sequence, and five-condition
      weekly template; no open-ended attendance matcher exists, bare workplace/location words do not independently
      reject, and home-office wording cannot produce attendance evidence merely because it contains `office`.
- [ ] Remote denial implements the exact inventory and precedence; each denial owns its span, contained positive
      sequences are suppressed, separate positive evidence conflicts to unknown, and stronger mandatory evidence
      still rejects.
- [ ] Geography implements the closed inclusion/exclusion/broad/non-US precedence matrix and every exact phrase.
- [ ] A matched geography-exclusion span does not also emit a contained inclusion; separate non-overlapping inclusion
      and exclusion occurrences remain conflicting evidence.
- [ ] Only structured `country_code` supplies ISO codes; free text supplies no inferred two-letter country or state/DC
      code, malformed or absent codes follow the unknown contract, and no structured state-code field is invented.
- [ ] State-only exclusions remain later scoring; geography uses no candidate comparison or free-text state-code
      inference.
- [ ] Already-actioned evaluation uses the current schema and follows every digest state/version rule above.
- [ ] The absent future application branch causes no rejection, unknown, error, lock, FK, or migration.
- [ ] `0004_hard_filter_evaluation` creates only the approved table, constraints, and indexes with a reversible downgrade.
- [ ] No filter-stage `score` write, dual write, update/delete repository operation, append-only trigger, or policy table exists.
- [ ] The exact table/row lock sequence fences digest and linked score-version mutation; stale races persist nothing.
- [ ] Task 008 closes every owned resource on every exit, never disposes the caller's Engine, and leaves it reusable.
- [ ] Canonical input/output and mutable-state hashes reproduce exactly.
- [ ] No extra filter, arbitrary evidence, sensitive outcome, or unsafe log value exists.
- [ ] Network/model entry points fail in unit tests; PostgreSQL tests use disposable PostgreSQL 16 + pgvector.
- [ ] Ruff, formatting, focused tests, PostgreSQL tests, and the full suite pass.

## Validation commands

```bash
cd api
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest -m "not postgres"
.venv/bin/pytest -m "not postgres" tests/test_hard_filters_*.py
.venv/bin/python -c "from app.main import app; assert app is not None"
DATABASE_URL="$TEST_DATABASE_URL" .venv/bin/alembic upgrade head
DATABASE_URL="$TEST_DATABASE_URL" .venv/bin/alembic current
DATABASE_URL="$TEST_DATABASE_URL" .venv/bin/alembic downgrade 0003_posting_current_version
DATABASE_URL="$TEST_DATABASE_URL" .venv/bin/alembic current
DATABASE_URL="$TEST_DATABASE_URL" .venv/bin/alembic upgrade head
DATABASE_URL="$TEST_DATABASE_URL" .venv/bin/alembic current
.venv/bin/pytest -m postgres tests/test_hard_filters_*_postgres.py
.venv/bin/pytest
cd ..
```

Use disposable PostgreSQL 16 + pgvector. Head must be
`0004_hard_filter_evaluation`; the downgrade must report
`0003_posting_current_version`, and repeat upgrade must restore the sole head.

## Repository hygiene

```bash
git status --short
git diff --check
git diff --name-only
git diff -- tasks/008-deterministic-hard-filters.md
git status --short --ignored
```

Change only this task; add no secret, private payload, dump, cache, coverage, or generated artifact.
