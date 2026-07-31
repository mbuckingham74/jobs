# Task 008: Deterministic Phase 1 hard filters

**Status:** Ready

**Depends on:** Task 007 — One-endpoint fetch-to-ingestion runner

**Specification:** `docs/jobs-forkstech-spec.html`, especially sections 02–04,
07, 08, 13, and 14

## Approved product boundary

Task 008 is one deterministic eligibility evaluation over one persisted posting and its authoritative current
version. It runs before research, embeddings, semantic deduplication, model calls, and deep scoring.

It evaluates one posting, accumulates all reasons, and performs no network, model, embedding, triage/deep-scoring,
workflow, or batch work. Pure rule evaluation neither reads nor writes a database. Persistence projects every
rejected completed evaluation into the existing `score` table solely for the required filter-stage audit record.

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
- Immutable inputs are `posting_version.title`, `locations`, and `description_md`; each persisted location contributes
  exactly one free-text field, its `label`. `country_code` is structured geography evidence only,
  `workplace_type` is structured remote-arrangement evidence only, and `region` and `city` are not Phase 1
  hard-filter text evidence. Never read `raw_payload`, embeddings, or incidental fields.
- The selected `posting_version.observed_in_run_id` is immutable provenance for the required filter-stage `score`
  projection. It is not a rule input and therefore is not added to canonical input JSON; it is selected and
  revalidated with the authoritative version before persistence.
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

For immutable title, description, and location strings, require a string or use its missing rule, then perform this
closed pipeline in order:

1. normalize CRLF and CR to LF;
2. before NFKC or punctuation flattening, scan the field left to right for boundaries. A paragraph boundary is an LF
   followed by zero or more non-LF Unicode whitespace characters and another LF; consume the complete maximal run.
   The closed sentence-terminator code-point inventory is ASCII `.`, `!`, and `?` plus Unicode `。`, `！`, and `？`;
   consume a maximal consecutive run of those code points as one sentence boundary. Paragraph boundaries take
   precedence. Each boundary closes the current segment; discard empty segments and number non-empty segments from
   zero in encounter order. There is no abbreviation, decimal, initial, ellipsis, quotation, capitalization, locale,
   or language exception;
3. independently within each segment, apply NFKC and `casefold()`; map `’`, `‘`, `ʼ`, and `＇` to ASCII `'`; treat
   whitespace, Unicode dashes, `/`, `\`, `_`, and all other punctuation as token boundaries except apostrophes
   between letters; form maximal Unicode Letter-or-Number tokens; and
4. compare ordered token sequences only.

This ordering preserves sentence identity even when NFKC would otherwise fold a terminator. A comma, colon,
semicolon, slash, dash, single LF, or other punctuation is a token boundary but not a sentence boundary.

Each normalized token is represented in memory as its token value plus immutable metadata
`(field_kind, field_ordinal, sentence_segment, token_ordinal)`. `field_kind` is `title`, `description`, or
`location`; `field_ordinal` is zero for title and description and is the persisted location-array index for that
location's `label`. Each label is a distinct field occurrence: matching never crosses between location labels or
between a location label and title or description. `sentence_segment` is the segment number defined above; and
`token_ordinal` is zero-based within that field occurrence. In addition, the scan classifies every original gap
between adjacent tokens as exactly one of
`whitespace`, `comma`, `semicolon`, `slash`, `other_punctuation`, `sentence_boundary`, or `paragraph_boundary`;
sentence and paragraph categories take precedence over the other gap categories. This gap metadata is consulted
only by grammars that explicitly own separators, currently the named-scope list grammar. It preserves the existing
distinction between approved comma, semicolon, and slash separators and disallowed dash, whitespace-only, or other
punctuation separators after tokenization. Sentence metadata is consulted only by rules that explicitly require
same-sentence matching: the product-owner/business-analyst compound branch, the weekly-attendance template, and the
broad-scope and named-scope geography grammars. All other matching continues to compare the same normalized token
values and field boundaries as before.

Never use unrestricted substrings. Hyphenated/slash compounds form separate tokens. There is no accent stripping,
stemming, singularization, implicit abbreviation expansion, fuzzy/semantic matching, locale lookup, or taxonomy.
Matching never depends on original capitalization. Free-text two-letter tokens are never looked up as country or
state/DC codes; only complete manifest-listed token sequences and templates may match. For geography, an approved
exclusion template owns its matched token span: an inclusion sequence contained wholly inside that same span does
not independently match, while a separate non-overlapping inclusion occurrence remains independent evidence. For
remote arrangement, an approved denial template likewise owns its matched token span: a positive remote sequence
contained wholly inside that span does not independently match, while separate structured positive evidence or a
separate non-overlapping positive text occurrence remains independent evidence. For clearance, an approved
no-requirement, preference, or obtainability template owns its matched span: a recognized clearance phrase contained
inside that span does not independently match as requirement evidence, while separate non-overlapping evidence
remains independent.

Every phrase, abbreviation, precedence, matched-span ownership rule, evidence mapping, and declared input field
lives in the manifest. Content patterns may declare only immutable title, description, and location labels, and
every pattern declares which of those fields it may inspect. `country_code` and `workplace_type` retain only their
declared structured interpretations; `region` and `city` are not content-pattern inputs. Generated regex uses
escaped approved tokens and is tested against the token matcher.

All nine rules run without short-circuiting and emit their reason at most
once. Rules are independently decisive except for the geography rule's single
manifest-owned applicability dependency on the completed remote-reality
outcome: geography may reject only when remote reality completed
`pass`/`remote_permitted`. Remote reality is completed first for this
dependency, but the remaining rules and evidence collectors do not depend on
discovery order and still run exactly once. No rule exists for sponsorship, general work
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
| Clearance | no-requirement, preference, or obtainability template | active clearance required | U: not stated or ambiguous | U |
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
unrecognized structured `country_code`, including a value outside the manifest's closed assigned-code inventory,
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

Normalized tokens, sentence segments, and gap metadata are deterministic derived evaluation state, not new provider
input. They are never added to canonical input JSON, persisted output, the provider payload, or a schema column.
Canonical input remains anchored to the exact immutable posting-version content hash. The complete normalization
algorithm, terminator and gap-category inventories, metadata representation, field ownership, and each rule allowed
to consult the metadata are literal manifest content and therefore participate in `policy_manifest_hash`.

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

The special product-owner-as-business-analyst branch rejects only when one normalized title sentence segment or one
normalized description sentence segment contains both exact sequences `product owner` and `business analyst`. In
either field, both sequences must have the same `field_kind`, `field_ordinal`, and `sentence_segment`; their order
and intervening same-segment tokens do not matter. Thus `Product owner, business analyst` may satisfy the branch,
while `Product owner. Business analyst` does not. The description is read for this compound branch only; when usable
description text is unavailable, that branch contributes nothing. The sequences never combine across fields,
sentence segments, or paragraph boundaries. Bare `ba`,
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
   `day`/`days` token, except that an `office` token belonging to a contiguous normalized `home office` span does
   not satisfy this condition; and
5. in that same sentence, at least one obligation token from the closed set `required`, `must`, `expected`, or
   `attendance`, or one exact presence-linking sequence `report to the office`, `report onsite`, or `report on site`.

If any condition is absent, the weekly template emits no `attendance_required`; another approved mandatory template
may still decide the rule. Bare `office`, `workplace`, `location`, `onsite`, `on site`, `in office`, or `in the
office` independently emits no mandatory evidence. Bare `work`, `working`, or `report` does not satisfy condition 5;
`report` qualifies only as part of one of the three exact presence-linking sequences above. A `home office`
occurrence, including one embedded in `their home office` or `quiet home office`, never satisfies condition 4 merely
because it contains `office`; a separate physical-office occurrence in the same sentence remains eligible for
condition 4. This exclusion does not change structured hybrid/on-site or any other approved mandatory
physical-attendance behavior.

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

Apply this exact order: (1) complete the remote-reality rule under decision 3; (2) set positive remote evidence true
if and only if that completed outcome is `pass` with evidence `remote_permitted`; (3) collect explicit US inclusion;
(4) collect explicit US exclusion; (5) collect scoped broad worldwide/international evidence; (6) collect recognized
non-US-only scopes; and (7) apply the closed matrix and remote-applicability gate below. Mixed structured/body
evidence uses the same precedence. Evidence collection never short-circuits because of the remote outcome.

Two-letter ISO country codes are interpreted only from the existing dedicated structured `country_code` field, after
case normalization. Recognition is membership in this exact closed, lexically ordered manifest literal: the 249 ISO
3166-1 alpha-2 codes assigned for this policy version.

```text
AD, AE, AF, AG, AI, AL, AM, AO, AQ, AR, AS, AT, AU, AW, AX, AZ,
BA, BB, BD, BE, BF, BG, BH, BI, BJ, BL, BM, BN, BO, BQ, BR, BS, BT, BV, BW, BY, BZ,
CA, CC, CD, CF, CG, CH, CI, CK, CL, CM, CN, CO, CR, CU, CV, CW, CX, CY, CZ,
DE, DJ, DK, DM, DO, DZ,
EC, EE, EG, EH, ER, ES, ET,
FI, FJ, FK, FM, FO, FR,
GA, GB, GD, GE, GF, GG, GH, GI, GL, GM, GN, GP, GQ, GR, GS, GT, GU, GW, GY,
HK, HM, HN, HR, HT, HU,
ID, IE, IL, IM, IN, IO, IQ, IR, IS, IT,
JE, JM, JO, JP,
KE, KG, KH, KI, KM, KN, KP, KR, KW, KY, KZ,
LA, LB, LC, LI, LK, LR, LS, LT, LU, LV, LY,
MA, MC, MD, ME, MF, MG, MH, MK, ML, MM, MN, MO, MP, MQ, MR, MS, MT, MU, MV, MW, MX, MY, MZ,
NA, NC, NE, NF, NG, NI, NL, NO, NP, NR, NU, NZ,
OM,
PA, PE, PF, PG, PH, PK, PL, PM, PN, PR, PS, PT, PW, PY,
QA,
RE, RO, RS, RU, RW,
SA, SB, SC, SD, SE, SG, SH, SI, SJ, SK, SL, SM, SN, SO, SR, SS, ST, SV, SX, SY, SZ,
TC, TD, TF, TG, TH, TJ, TK, TL, TM, TN, TO, TR, TT, TV, TW, TZ,
UA, UG, UM, US, UY, UZ,
VA, VC, VE, VG, VI, VN, VU,
WF, WS,
YE, YT,
ZA, ZM, ZW
```

The evaluator must not import, query, or defer membership to an ISO package, locale database, operating-system
table, network source, provider taxonomy, or implementation-selected library. Structured `country_code == "US"` is
explicit US inclusion; any other member of the literal inventory is non-US-only scope evidence. Any value outside
the literal inventory, including ISO-shaped `ZZ`, is unrecognized and contributes no definite geography evidence.
A malformed or unrecognized structured code follows the approved unresolved behavior absent other evidence. This
evaluation-only recognition rule does not change Task 005 ingestion: an adapter still accepts and stores an
uppercase two-ASCII-letter ISO-shaped value without membership validation or repair.

The persisted location object has `country_code` and a generic `region`, but no dedicated state/region-code field.
Therefore this policy recognizes no US state/DC postal abbreviation from structured `region`, free-text location
labels, title, or description, and adds no field.

Free-text explicit US inclusion is limited to exactly these normalized sequences or templates, plus a
manifest-listed US item inside a named-scope list governed by one of the closed templates defined below:

```text
usa, united states, united states of america,
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

Broad sequences are exactly `global`, `globally`, `worldwide`, `anywhere`, `North America`, and `Americas`. A broad
sequence contributes evidence only as the final item owned by one of these closed, same-field, same-sentence
templates:

1. remote-role scope: one exact prefix `remote`, `remote role`, `remote position`, `remote job`, `this role is
   remote`, `this position is remote`, or `this job is remote`, followed by zero connector tokens or exactly one
   connector token from `in`, `within`, `across`, `throughout`, or `from`, followed by one broad sequence;
2. applicant opening: `open` or `available`, followed by `to`, then `candidates` or `applicants`, followed by zero
   connector tokens or exactly one connector token from `in`, `within`, `across`, `throughout`, or `from`, followed
   by one broad sequence; or
3. applicant eligibility: `candidates` or `applicants`, followed by `may`, `can`, or `must`, then `be located`, `be
   based`, `reside`, or `apply`, followed by zero connector tokens or exactly one connector token from `in`,
   `within`, `across`, `throughout`, or `from`, followed by one broad sequence.

Every adjacency above is exact. The matched template owns the complete span from its first prefix token through the
broad sequence's final token; matching does not skip filler, cross a field, sentence segment, or paragraph boundary,
or combine a prefix with a broad occurrence outside that span. `Remote worldwide` and `Open to applicants
worldwide` supply scoped broad evidence. `Join our global company`, `collaborate with teams worldwide`, `serve
global customers`, and `grow in global markets` do not match any template and supply no geography evidence.
Organizational reach and the location of a company, coworker, employee, office, team, customer, or market never
imply applicant or role geography.

Manifest-listed named sequences available to the named-scope grammar are the US sequences `US`, `the
US`, `USA`, `the USA`, `United States`, `the United States`, `United States of America`, and `the United States of
America`, plus these named non-US sequences:

```text
EMEA, EU, the EU, European Union, the European Union, Europe,
UK, the UK, United Kingdom, the United Kingdom,
APAC, Asia Pacific, LATAM, Latin America,
Canada, Australia, New Zealand, India
```

The occurrence of a named non-US sequence is scope evidence only when it is an item in a named-scope list governed
by one of these closed, same-field, same-sentence templates:

1. remote scope: `remote` followed by `in`, `within`, `across`, or `throughout`, then a named-scope list;
2. candidate scope: `open` or `available`, followed by `to`, then `candidates` or `applicants`, then `in` or `within`,
   then a named-scope list; or
3. residence requirement: `candidates`, `applicants`, or `this role`, followed by `must` or `required to`, then
   `be located`, `be based`, or `reside`, then `in` or `within`, then a named-scope list.

A named-scope list contains one or more of the manifest-listed named sequences above and nothing else. Multiple
items may be joined only by a comma, a semicolon, a slash, `and`, or `or`; a comma, semicolon, or slash may be
followed by `and` or `or`. Matching consumes the complete maximal list immediately following the template, and each
item contributes its manifest-owned US-inclusion or named-non-US scope category. Company, employee, office, team,
customer, market, and collaboration locations outside these templates contribute no named-scope evidence.

Recognized two-letter non-US ISO country codes count only through structured `country_code`; no general country-code
or state-code lookup is performed on free text. The closed manifest-listed `EU` and `UK` sequences remain named
aliases, not inferred ISO codes. In particular, bare free-text `us`, `in`, `or`, `me`, `ca`, `hi`, `wa`, or any
other unlisted two-letter token is not geographic evidence; token `us` is evidence only inside an approved
contextual template above.

| Evidence | Geography outcome |
| --- | --- |
| explicit US inclusion, no explicit exclusion | pass: `us_not_excluded` |
| explicit US exclusion, no explicit inclusion, and positive remote evidence | reject: `remote_scope_excludes_us` |
| explicit US inclusion and exclusion | unknown: `remote_geography_unresolved` |
| broad evidence, no explicit exclusion | pass: `us_not_excluded` |
| broad evidence plus explicit exclusion, no explicit inclusion, and positive remote evidence | reject: `remote_scope_excludes_us` |
| every explicit scope is recognized non-US-only, and positive remote evidence | reject: `remote_scope_excludes_us` |
| any otherwise-rejecting exclusion or non-US-only row without positive remote evidence | unknown: `remote_geography_unresolved` |
| otherwise unscoped, contradictory, malformed, or unrecognized | unknown: `remote_geography_unresolved` |

Generic broad scope never overrides explicit exclusion, and explicit inclusion never silently overrides exclusion.
Only a completed remote-reality `pass` with `remote_permitted` makes a geography-rejection row applicable. A remote
`reject` or `unknown` can therefore coexist only with geography `pass` or `unknown`, never `geo_excluded`; this
dependency changes no canonical reason order and adds no discovery-order dependence. All nine rules still complete.
Required exact tests are:

- `Join us in building the platform` does not establish US eligibility;
- `Work in a collaborative environment` identifies neither India nor Indiana;
- `Choose X or Y` does not identify Oregon;
- `Contact me for details` does not identify Maine;
- free-text `CA` alone identifies neither California nor Canada;
- structured `country_code="US"` is explicit US inclusion;
- structured `country_code="IN"` is non-US-only scope evidence;
- `remote in the us`, `available in the us`, and `open to candidates in the us` remain explicit US inclusion, as do
  approved US items within named-scope templates;
- with `country_code=null`, `workplace_type="remote"`, and `description_md="US-based; not available to US applicants"`,
  expected geography result: reject with `geo_excluded` / `remote_scope_excludes_us`;
- representative first, middle, and last assigned inventory members are recognized, while `ZZ` is unrecognized;
- `Remote in the US` is explicit US inclusion;
- `Remote worldwide` passes;
- `Open to applicants worldwide` supplies scoped broad evidence and passes;
- `Remote worldwide, except the United States` rejects;
- `Remote worldwide, excluding us from consideration` does not match an exclusion template merely because it
  contains pronoun `us`, so its broad scope passes;
- `Remote in the US and Canada` passes;
- `Join our global company` contributes no geography evidence;
- `Collaborate with teams worldwide` contributes no geography evidence;
- `Serve global customers` contributes no geography evidence;
- `Grow in global markets` contributes no geography evidence;
- `Collaborate with our team in Canada` contributes no geography evidence;
- `Support customers in India` contributes no geography evidence;
- `Our company is based in the United Kingdom` contributes no geography evidence;
- `Remote in the US, but not available to US applicants` is unknown;
- `Remote across Europe and Canada` rejects;
- `Remote, excluding Alaska` produces no country rejection;
- structured remote US plus body `excluding the United States` is unknown;
- structured remote CA-only scope rejects;
- structured `workplace_type="on-site"` plus structured `country_code="CA"` rejects only `not_remote`; geography is
  unknown with `remote_geography_unresolved` and does not emit `geo_excluded`;
- unresolved remote arrangement plus structured `country_code="CA"` leaves geography unknown and does not emit
  `geo_excluded`;
- an unscoped remote role is unknown; and
- malformed, absent, or unrecognized structured codes, including `ZZ`, fall through to the approved unknown behavior
  when no other evidence decides.

`excluding Alaska`, `excluding AK`, `excluding certain states`, and other state-only restrictions remain deep
scoring. Add no candidate comparison, geocoder, legal interpretation, reason, unknown, status, or evidence value.

### 5. Active security clearance

Reject only a definite currently held active-clearance requirement. The eight recognized clearance phrases are:

```text
active security clearance, current security clearance,
active secret clearance, current secret clearance,
active top secret clearance, current top secret clearance,
active ts sci clearance, current ts sci clearance
```

A recognized clearance phrase alone never establishes a requirement. It is requirement evidence only inside one of
these closed actual-obligation templates:

1. `must possess`, `must hold`, or `must have`, followed by a recognized clearance phrase;
2. `requires`, followed by a recognized clearance phrase;
3. `required to possess`, `required to hold`, or `required to have`, followed by a recognized clearance phrase; or
4. a recognized clearance phrase followed by `is required` or `required`.

The closed no-requirement templates are `no` followed by a recognized clearance phrase and then `required` or
`is required`; a recognized clearance phrase followed by `not required` or `is not required`; and `does not
require` followed by a recognized clearance phrase. The closed preference templates are a recognized clearance
phrase followed by `preferred` or `is preferred`. The closed obtainability templates are `ability to obtain`,
`able to obtain`, `eligible to obtain`, `willing to obtain`, `must be able to obtain`, or `can obtain`, followed by
a recognized clearance phrase.

For every prefix trigger followed by a recognized clearance phrase—obligation templates 1–3, the no-requirement
triggers `no` and `does not require`, and all six obtainability triggers—the trigger fragment's final token is
followed either immediately by the recognized clearance phrase's first token or by exactly one intervening
determiner token from the closed set `a`, `an`, or `the`. This is an exact, manifest-owned token-adjacency rule: no
other intervening token is permitted, and the matcher must not search across an unbounded gap elsewhere in the same
sentence.

Every template in which the recognized clearance phrase precedes a fixed suffix retains exact template adjacency:
obligation template 4, the no-requirement suffixes `required`, `is required`, `not required`, and `is not required`,
and the preference suffixes `preferred` and `is preferred` permit no unspecified filler between the recognized
clearance phrase and that suffix or within the suffix. The prefix-trigger adjacency rule above does not otherwise
change these fixed suffixes.

Each no-requirement, preference, or obtainability template owns its complete matched span, and a recognized
clearance phrase contained inside that span cannot also contribute requirement evidence. No-requirement-only,
preference-only, and obtainability-only cases pass with `active_clearance_not_required`. Independent,
non-overlapping actual-requirement and no-requirement, preference, or obtainability evidence produces
`clearance_ambiguous`. An actual-obligation template without such conflicting evidence rejects with
`active_clearance_required`; no approved conclusion produces `clearance_not_stated`. Public trust, background
checks, export controls, citizenship, residency, sponsorship, and work authorization do not reject.

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

A filter-stage `score` row by itself is not an action fact. The
`already_actioned` rule reads only `digest_item` rows and follows each row's
existing `digest_item.score_id` relationship to validate posting-version
provenance. Creating a filter-stage score without a linked digest item
therefore cannot reject a later evaluation; no query may treat the existence
of any unlinked `score` row, regardless of stage, as a prior user action.

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

### 10. Dedicated evaluation and filter-score persistence

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
never mutates an evaluation; add no append-only trigger.

The existing `score` schema already provides every field and uniqueness rule
needed for the filter-stage projection. No `score` schema change is part of
migration `0004`. For each completed evaluation with a non-empty
`rejection_reasons`, insert exactly one `score` row with:

| Existing `score` column | Required filter-stage value |
| --- | --- |
| `run_id` | the selected `posting_version.observed_in_run_id`, revalidated as a valid existing `pipeline_run.id` |
| `posting_version_id` | the evaluation's authoritative `posting_version_id` |
| `stage` | literal `filter` |
| `ruleset_version` | the evaluation's exact `policy_version`, which is exactly `phase1-hard-filters-v1` |
| `input_hash` | the evaluation's exact `input_hash` |
| `reject_reasons` | one JSON array containing every matching rejection reason in canonical `HardFilterReason` order |
| `scored_at` | the evaluation's normalized `evaluation_as_of` |

All nullable model, prompt, résumé, candidate-profile, research, rubric,
numeric score, verdict, rationale, gap, hook, flag, and other deep/triage
columns remain null. In particular, the projection does not manufacture a
model verdict or a second result vocabulary.

The relationship is exact and deterministic. For one
`hard_filter_evaluation` row `H` with non-empty `H.rejection_reasons`, its
rejected result is the validated canonical output stored by `H`, and its sole
filter-stage score projection is the row `S` for which:

```text
S.posting_version_id = H.posting_version_id
S.stage = 'filter'
S.input_hash = H.input_hash
S.ruleset_version = H.policy_version
S.reject_reasons = H.rejection_reasons
S.scored_at = H.evaluation_as_of
S.run_id = selected posting_version.observed_in_run_id
```

`hard_filter_evaluation_input_hash_key` makes `H` unique, and the existing
`score_posting_version_id_stage_input_hash_key` makes `S` unique. These keys
provide deterministic association and deduplication without a new column,
foreign key, constraint, or index. A completed eligible evaluation has empty
reasons and must have no score row at the corresponding
`(posting_version_id, 'filter', input_hash)` key.

For a new rejected identity, insert `H` and `S` in the same database
transaction after all stale-input checks. Neither row may commit unless both
inserts succeed. Any exception, cancellation, constraint failure, or injected
failure between the inserts rolls back both. For a new eligible identity,
insert only `H` in that transaction.

Replay validates the complete immutable evaluation as already required and
also validates the paired projection. A rejected replay requires exactly the
one `S` above and verifies every listed field and the null-only unrelated
columns; an eligible replay verifies that the corresponding score key is
absent. A missing score for a rejected evaluation, a score for an eligible
evaluation, or any mismatched, extra, malformed, or non-null unrelated score
value raises `FilterEvaluationConflictError` with
`filter.evaluation_conflict`; it is never repaired, replaced, or silently
accepted.

Concurrent identical calls converge on one `H` and, when rejected, one `S`.
The posting-row lock normally serializes the final persistence check. If a
unique-key race is nevertheless observed, the losing attempt rolls back its
whole transaction, starts a fresh transaction, reacquires the complete lock
sequence, validates the winner's complete `H`/`S` pair (or eligible `H` plus
score absence), and returns `replayed=True`. It must not retry either insert in
isolation or create a duplicate projection.

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
9. insert or validate/replay the evaluation and its required filter-stage
   score projection as one atomic persistence unit; and
10. commit and release every lock.

The table lock fences digest inserts, updates, and deletes; score locks fence
target and version changes. Table-lock-first prevents holding posting/company
locks while waiting for a digest writer's normal table-level lock.

Recheck that no qualifying digest item was inserted or removed; no evaluated
state or joined `score_id` fact changed; every linked score still exists with
its evaluated version; posting IDs still agree through the join; the selected
posting version's `observed_in_run_id` still names the required score
provenance; and pointer, posting/company value, policy, and every evaluated
input remain identical.
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
the exact sentence/paragraph segmentation algorithm, six sentence terminators, gap categories, token-metadata
representation, location-label-only text boundary, and limited consumers (the product-owner/business-analyst
compound branch, weekly-attendance template, and broad-scope and named-scope geography grammars),
decision 3 positive and denial inventories, the closed mandatory-attendance sequence inventory, weekly-template
token-distance and closed-obligation conditions, remote span ownership and precedence, decision 4 phrase inventory,
the literal 249-member assigned country-code inventory and code-source restriction, broad-scope sequence inventory
and three broad templates, named-scope sequence inventory, separator grammar and three named-scope templates,
exclusion-span ownership, the remote-applicability gate, and the precedence matrix, and decision 5
recognized-phrase inventory, obligation,
no-requirement, preference, and obtainability templates. For obligation templates 1–3 and every no-requirement,
preference, and obtainability template, the manifest explicitly owns each applicable trigger-to-phrase and
phrase-to-suffix adjacency edge: each prefix-trigger edge owns the zero-or-one-token rule and closed permitted
determiner set `a`, `an`, and `the`, and each fixed-suffix edge owns exact adjacency with no filler. The manifest
also owns clearance span ownership and precedence and contains no removed legacy enum or evidence value. Any change
requires a new policy version. Add no policy table, environment phrase list, mutable global setting, or fallback
version.

## Relationship to later scoring

Later triage/deep scoring requires current posting/version, policy version/hash, evaluation instant,
mutable-state hash, and `eligible=True`. A stale or missing evaluation requests
a new one; it is not rejection. Later scoring cannot reinterpret unknowns.
The required filter-stage `score` projection is rejection audit persistence,
not authorization to run model scoring or to advance a rejected posting.

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
The persistence repository writes the existing `score` columns specified
above; it adds no scoring service or score-table migration.

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
- comma-separated `Product owner, business analyst` in one description sentence segment rejects;
- each approved sentence terminator `.`, `!`, `?`, `。`, `！`, and `？` between `Product owner` and `Business
  analyst` creates distinct sentence segments and does not trigger the compound branch;
- the two sequences separated by a paragraph boundary do not trigger the compound branch;
- one sequence in title and the other in description, or sequences in two different fields or location elements, do
  not trigger the compound branch;
- normalized-token fixtures assert exact
  `(field_kind, field_ordinal, sentence_segment, token_ordinal)` metadata and the deterministic distinction between a
  single LF, a paragraph boundary, an approved terminator run, and non-terminating punctuation;
- bare `BA` does not trigger the compound branch;
- generic requirements, backlog, or agile language does not trigger the compound branch;
- discipline pass uses `excluded_discipline_absent`;
- discipline rejection uses `excluded_discipline_match`;
- unavailable usable description contributes nothing to the compound branch and emits no unknown;
- replay rejects persisted output containing the removed legacy discipline-unknown value; and
- the manifest contains no removed legacy value.

Geography-specific deterministic tests include:

- the manifest literal contains exactly 249 unique, lexically ordered uppercase two-letter assigned codes, including
  representative first, middle, and last members plus `US`, and excludes `ZZ`;
- evaluator membership is loaded only from that manifest literal; tests fail an attempted library/locale/network
  lookup and prove installed ISO-library data cannot change recognition or `policy_manifest_hash`;
- changing any literal inventory member changes `policy_manifest_hash`, while repeat canonicalization of the
  unchanged manifest produces identical bytes and hash;
- structured assigned non-US codes contribute non-US-only evidence, structured `US` contributes explicit inclusion,
  and structured `ZZ` contributes no definite evidence and is unresolved absent other evidence;
- Task 005 uppercase two-ASCII-letter ISO-shaped ingestion remains unchanged and stores `ZZ`; only Task 008
  evaluation treats it as unrecognized;
- scoped `Remote worldwide` and `Open to applicants worldwide` contribute broad US-including evidence;
- `Join our global company`, `Collaborate with teams worldwide`, global-customer wording, and global-market wording
  contribute no broad or US-including evidence and are unresolved absent other decisive evidence;
- incidental named-region company, team, customer, and collaboration wording continues to contribute no geography
  evidence, while all three named-scope templates and their separator grammar retain their existing positive and
  negative coverage;
- structured `workplace_type="remote"` plus structured `country_code="CA"` rejects geography with
  `geo_excluded`;
- structured `workplace_type="on-site"` plus structured `country_code="CA"` rejects remote reality with
  `not_remote`, completes geography unknown with `remote_geography_unresolved`, and does not include
  `geo_excluded`;
- unresolved remote arrangement plus structured `country_code="CA"` completes both applicable unknowns and does not
  include `geo_excluded`;
- every full-matrix geography rejection is parameterized against remote `pass`, `reject`, and `unknown` outcomes to
  prove only `pass`/`remote_permitted` permits `geo_excluded`; and
- the resulting single- and multi-reason projections retain canonical `HardFilterReason` order and their exact
  filter-stage score `reject_reasons`.

Normalization/hash tests also prove sentence and gap metadata are derived only, are never added to canonical input
or output or provider input, and that their complete manifest-owned algorithm changes
`policy_manifest_hash` if edited. Existing named-scope separator tests must continue to distinguish `Europe /
Canada` from `Europe - Canada` and `Europe Canada`.

Approved clarification coverage additionally proves:

- with structured `workplace_type="remote"`, `Remote. Worldwide` does not cross a sentence boundary to match a
  broad-scope geography template and remains geography-unresolved absent other decisive evidence;
- with structured `workplace_type="remote"`, `Open to candidates in. Europe` does not cross a sentence boundary to
  match a named-scope geography template and remains geography-unresolved absent other decisive evidence;
- a location `label` of `Remote in the US` supplies one complete free-text location occurrence, while its matching
  never crosses to another label or field;
- `region` and `city` values alone supply no Phase 1 hard-filter free-text evidence; and
- structured `country_code` and `workplace_type` continue to supply only their approved geography and
  remote-arrangement evidence, respectively.

Remote-evidence tests include:

- `Employees are expected to work in the office three days per week` rejects;
- `Must be onsite 2 days a week` rejects;
- `You will work with the New York office three days per week` does not reject;
- `Team members typically work from the office two days a week, but this role is fully remote` does not reject;
- `Expected to work from the office three days per week` rejects;
- `Must be onsite two days a week` rejects;
- `Hybrid role` rejects;
- structured `workplace_type="hybrid"` rejects;
- `Must work from the office` rejects;
- `Required to report onsite` rejects;
- `Must work on site` rejects;
- `Must work from a location within the United States` does not match an attendance obligation;
- `Must work from any approved location` does not match an attendance obligation;
- `Must work from a quiet home office` does not match an attendance obligation;
- `Must work from your home office` does not match an attendance obligation;
- `Employees must work from their home office three days per week` does not trigger the weekly-attendance template;
- `Employees must be in the office three days per week` triggers the weekly-attendance template;
- `Employees must work from their home office three days per week and be in the office one day per week` triggers
  the weekly-attendance template only through the separate physical-office occurrence;
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

Clearance tests include:

- `An active security clearance is preferred` completes with `active_clearance_not_required` and does not reject;
- `No active security clearance is required` completes with `active_clearance_not_required` and does not reject;
- `Ability to obtain an active security clearance` completes with `active_clearance_not_required`, does not reject,
  and is not ambiguous;
- parameterized coverage crosses every one of the eight recognized clearance phrases with every one of `ability to
  obtain`, `able to obtain`, `eligible to obtain`, `willing to obtain`, `must be able to obtain`, and `can obtain`,
  and every case deterministically completes with `active_clearance_not_required` without rejection or ambiguity;
- `Must possess active security clearance` rejects with `active_clearance_required`, proving a zero-gap trigger;
- `Must possess an active security clearance` rejects with `active_clearance_required`, proving the permitted single
  intervening determiner;
- `Does not require active security clearance` and `Does not require an active security clearance` prove,
  respectively, a zero-gap and permitted single-determiner no-requirement prefix trigger;
- `Can obtain active security clearance` and `Ability to obtain an active security clearance` prove, respectively,
  a zero-gap and permitted single-determiner obtainability prefix trigger;
- `This role requires a current secret clearance` rejects with `active_clearance_required`;
- `Required to hold an active top secret clearance` rejects with `active_clearance_required`;
- `Requires strong communication skills and a current secret clearance` does not match an actual-obligation template
  and, absent other evidence, completes with `clearance_not_stated`;
- `Requires possession of active security clearance`, with exactly two intervening tokens between the trigger and
  recognized phrase, does not match an actual-obligation template and, absent other evidence, completes with
  `clearance_not_stated`;
- `Does not require prior possession of active security clearance` has two or more intervening non-determiner tokens,
  does not match a no-requirement template, and, absent other evidence, completes with `clearance_not_stated`;
- `Active security clearance would be strongly preferred` has two or more intervening unrelated tokens, does not
  match a preference template, and, absent other evidence, completes with `clearance_not_stated`;
- `Ability to obtain within six months active security clearance` has two or more intervening unrelated tokens, does
  not match an obtainability template, and, absent other evidence, completes with `clearance_not_stated`;
- `A current ts sci clearance is required` remains a rejection with `active_clearance_required`; these positive
  examples cover every approved actual-obligation template class; and
- `Must possess an active security clearance. No active security clearance is required for this role.` has
  independent, non-overlapping requirement evidence and no-requirement evidence; the no-requirement span suppresses
  only its contained clearance phrase, the separate requirement evidence remains valid, and the combined outcome is
  `clearance_ambiguous`, not rejection and not `active_clearance_not_required`;
- the existing case with independent, non-overlapping actual-requirement plus suppression evidence still completes
  with `clearance_ambiguous`.

Engine-ownership tests call the service, prove its connection/result resources closed, reuse the same Engine
successfully, and prove `engine.dispose()` was not called. Patch network/model entry points to fail. PostgreSQL 16 +
pgvector tests cover migration cycles, identity/replay, digest/score relations, structured/body geography conflicts,
the lock sequence, stale rollback, cleanup paths, currentness, and privacy. They explicitly prove:

- one rejected evaluation writes one filter-stage `score`;
- that score's `reject_reasons` contains every matched reason in canonical reason order;
- that score stores the exact `phase1-hard-filters-v1` ruleset version;
- an eligible evaluation writes no filter-stage rejection score;
- an injected transaction failure between paired writes leaves neither the evaluation nor score row persisted;
- replay validates the existing pair and creates no duplicate score;
- concurrent identical rejected calls converge on one canonical evaluation and one score projection;
- a missing score, a corrupt or mismatched paired score, and an unexpected score for an eligible evaluation are
  detected as `filter.evaluation_conflict`; and
- an unlinked filter-stage score does not trigger `already_actioned`, while the existing digest-linked action
  semantics remain unchanged.

Use guarded `TEST_DATABASE_URL`, never SQLite, and only invented rows.

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
- [ ] Only immutable version title, description, and location labels supply content; each label is a separate field
      occurrence, matching never crosses label or field boundaries, `country_code` and `workplace_type` retain only
      their structured evidence roles, `region` and `city` supply no Phase 1 hard-filter text evidence, and mutable
      content projections are unread.
- [ ] The five adjacent-discipline sequences inspect title only; within the discipline rule, description contributes
      only to the exact same-sentence product-owner/business-analyst compound branch and collaboration language does
      not reject.
- [ ] Normalization deterministically preserves field, field ordinal, sentence segment, token ordinal, and gap
      category before punctuation flattening; the closed `.`, `!`, `?`, `。`, `！`, `？` terminators and paragraph
      algorithm require same-field/same-sentence compound matching, permit comma-separated matching, and forbid
      period-separated, other-terminator-separated, paragraph-separated, and cross-field matching without changing
      unrelated token behavior.
- [ ] Sentence and separator metadata are derived evaluation state only, never new persisted provider/canonical
      input; their complete inventories, algorithm, representation, and permitted consumers—the
      product-owner/business-analyst compound branch, weekly-attendance template, and broad-scope and named-scope
      geography grammars for sentence metadata, and the named-scope list grammar for gap metadata—are
      manifest-owned and hash-stable.
- [ ] Remote evidence implements every exact arrangement, closed mandatory-attendance sequence, and five-condition
      weekly template; its obligation condition admits only the four approved obligation tokens or an exact
      presence-linking `report` sequence, bare `work`, `working`, and `report` do not satisfy that condition, no
      open-ended attendance matcher exists, bare workplace/location words do not independently reject, and
      an `office` token in a contiguous normalized `home office` span cannot satisfy weekly condition 4, while a
      separate physical-office occurrence remains eligible and home-office wording cannot otherwise produce
      attendance evidence merely because it contains `office`.
- [ ] Remote denial implements the exact inventory and precedence; each denial owns its span, contained positive
      sequences are suppressed, separate positive evidence conflicts to unknown, and stronger mandatory evidence
      still rejects.
- [ ] Geography implements the closed inclusion/exclusion/broad/non-US precedence matrix and every exact phrase.
- [ ] Geography recognizes structured country codes only through the manifest's literal, lexically ordered,
      249-member assigned ISO 3166-1 alpha-2 inventory; `US` includes the US, other members are non-US-only evidence,
      and values outside the set including `ZZ` contribute no definite evidence. No runtime library, OS, locale,
      network, or provider inventory can vary membership, and the literal inventory participates in
      `policy_manifest_hash` without changing Task 005 ISO-shaped ingestion.
- [ ] Broad geography sequences contribute evidence only inside one of the three exact remote-role or
      applicant-eligibility templates and their owned token spans; scoped worldwide applicant eligibility counts,
      while global-company, worldwide-team, global-customer, and global-market contexts contribute no evidence.
- [ ] A matched geography-exclusion span does not also emit a contained inclusion; separate non-overlapping inclusion
      and exclusion occurrences remain conflicting evidence.
- [ ] Only structured `country_code` supplies ISO codes; free text supplies no inferred two-letter country or state/DC
      code, malformed or absent codes follow the unknown contract, and no structured state-code field is invented.
- [ ] A named non-US sequence contributes free-text scope evidence only as an item in a complete named-scope list
      owned by one of the three closed remote, candidate, or residence templates; the manifest freezes the sequences
      and list separators, and company, employee, office, team, customer, market, and collaboration locations outside
      those templates contribute nothing.
- [ ] Geography may emit `geo_excluded` only when remote reality completed `pass` with `remote_permitted`; remote CA
      rejects geography, on-site CA produces `not_remote` but not `geo_excluded`, and unresolved-arrangement CA
      leaves geography unknown. All nine rules still complete without short-circuiting, canonical reason order and
      score projection are unchanged, and the explicit dependency has no discovery-order behavior.
- [ ] State-only exclusions remain later scoring; geography uses no candidate comparison or free-text state-code
      inference.
- [ ] Clearance recognizes exactly eight phrases and rejects only through one of the four closed actual-obligation
      template classes; every prefix trigger in obligation templates 1–3, no-requirement templates, and all six
      obtainability templates permits only zero intervening tokens or exactly one manifest-owned determiner token
      from `a`, `an`, or `the`, with no other or unbounded same-sentence gap; all recognized-phrase-first fixed
      suffixes retain exact adjacency with no unspecified filler; no-requirement, preference, and obtainability
      templates own their spans and alone pass with `active_clearance_not_required`, while independent conflicting
      evidence is `clearance_ambiguous`.
- [ ] Already-actioned evaluation uses the current schema and follows every digest state/version rule above; an
      unlinked filter-stage score is not an action and cannot trigger `already_actioned`.
- [ ] The absent future application branch causes no rejection, unknown, error, lock, FK, or migration.
- [ ] `0004_hard_filter_evaluation` creates only the approved evaluation table, constraints, and indexes with a
      reversible downgrade; the existing score schema and unique key require no migration change.
- [ ] Every rejected completed evaluation atomically writes exactly one filter-stage score with the authoritative
      posting version and observed-run provenance, `stage = 'filter'`, every canonical ordered rejection reason,
      the exact ruleset version, the evaluation input hash, and evaluation instant; eligible evaluations write no
      rejection score.
- [ ] Evaluation/score replay validates the exact pair and creates no duplicate; concurrent identical calls converge
      on one canonical evaluation and one required projection, and missing, mismatched, corrupt, or unexpected paired
      persistence raises `filter.evaluation_conflict` without repair.
- [ ] A paired-write failure commits neither row, and no update/delete repository operation, append-only trigger, or
      policy table exists.
- [ ] The exact table/row lock sequence fences digest and linked score-version mutation plus atomic evaluation/score
      persistence; stale races persist nothing.
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
