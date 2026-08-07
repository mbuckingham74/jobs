"""The code-owned, immutable Phase 1 hard-filter policy."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

POLICY_VERSION = "phase1-hard-filters-v1"
SCHEMA_VERSION = 1

REASONS = (
    "title_no_match",
    "seniority_low",
    "wrong_discipline",
    "not_remote",
    "geo_excluded",
    "clearance_required",
    "stale",
    "already_actioned",
    "company_blocked",
)
UNKNOWNS = (
    "remote_arrangement_unresolved",
    "remote_geography_unresolved",
    "clearance_not_stated",
    "clearance_ambiguous",
)
EVIDENCE = (
    "allowed_title_match",
    "allowed_title_absent",
    "excluded_seniority_absent",
    "excluded_seniority_match",
    "excluded_discipline_absent",
    "excluded_discipline_match",
    "remote_permitted",
    "attendance_required",
    "remote_arrangement_unresolved",
    "us_not_excluded",
    "remote_scope_excludes_us",
    "remote_geography_unresolved",
    "active_clearance_not_required",
    "active_clearance_required",
    "clearance_not_stated",
    "clearance_ambiguous",
    "within_45_days",
    "older_than_45_days",
    "no_qualifying_action",
    "prior_skipped",
    "prior_applied",
    "current_version_expired",
    "company_not_blocked",
    "company_blocked",
)

TITLE_FAMILIES = (
    "product manager",
    "technical product manager",
    "platform product manager",
    "product operations manager",
    "program manager",
    "technical program manager",
    "project manager",
    "technical project manager",
    "delivery manager",
    "technical delivery manager",
    "product lead",
    "program lead",
    "delivery lead",
    "product management",
    "program management",
    "technical program management",
    "tpm",
)
SENIORITY = ("intern", "junior", "associate", "coordinator", "analyst")
DISCIPLINES = (
    "product marketing",
    "product design",
    "product designer",
    "sales engineer",
    "sales engineering",
)

REMOTE_POSITIVE = (
    "fully remote",
    "100 remote",
    "remote position",
    "remote role",
    "work remotely",
    "work from home",
)
REMOTE_DENIAL = (
    "this role is not remote",
    "this position is not remote",
    "this job is not remote",
    "not a remote role",
    "not a remote position",
    "not a remote job",
    "no remote option",
    "no remote work option",
    "no work from home option",
    "remote work is not offered",
    "work from home is not offered",
    "employees cannot work remotely",
    "employees cannot work from home",
    "employees can not work remotely",
    "employees can not work from home",
)
ATTENDANCE_ROLE = (
    "hybrid role",
    "hybrid position",
    "hybrid schedule",
    "onsite role",
    "onsite position",
    "on site role",
    "on site position",
    "in office role",
    "in office position",
)
ATTENDANCE_MANDATORY = (
    "required to work from the office",
    "must work from the office",
    "required to work from our office",
    "must work from our office",
    "required to work from a company office",
    "must work from a company office",
    "required to be in the office",
    "must be in the office",
    "required to report to the office",
    "must report to the office",
    "required to report onsite",
    "must report onsite",
    "required to report on site",
    "must report on site",
    "required to work onsite",
    "must work onsite",
    "required to work on site",
    "must work on site",
    "required onsite",
    "must be onsite",
    "required on site",
    "must be on site",
)

US_INCLUSION = (
    "usa",
    "united states",
    "united states of america",
    "within the us",
    "remote in the us",
    "available in the us",
    "open to candidates in the us",
    "us only",
)
US_EXCLUSION = (
    "excluding the us",
    "excluding the usa",
    "excluding usa",
    "excluding the united states",
    "except the us",
    "except the usa",
    "except usa",
    "except the united states",
    "outside the us",
    "outside the usa",
    "outside the united states",
    "not available in the us",
    "not available in the usa",
    "not available in usa",
    "not available in the united states",
    "not available to us applicants",
    "not open to candidates in the us",
    "not open to candidates in the united states",
    "us applicants excluded",
    "usa applicants excluded",
    "united states applicants excluded",
    "usa excluded",
    "united states excluded",
)
BROAD = ("global", "globally", "worldwide", "anywhere", "north america", "americas")
NAMED_US = (
    "us",
    "the us",
    "usa",
    "the usa",
    "united states",
    "the united states",
    "united states of america",
    "the united states of america",
)
NAMED_NON_US = (
    "emea",
    "eu",
    "the eu",
    "european union",
    "the european union",
    "europe",
    "uk",
    "the uk",
    "united kingdom",
    "the united kingdom",
    "apac",
    "asia pacific",
    "latam",
    "latin america",
    "canada",
    "australia",
    "new zealand",
    "india",
)

CLEARANCE_PHRASES = (
    "active security clearance",
    "current security clearance",
    "active secret clearance",
    "current secret clearance",
    "active top secret clearance",
    "current top secret clearance",
    "active ts sci clearance",
    "current ts sci clearance",
)
CLEARANCE_OBLIGATION_PREFIXES = (
    "must possess",
    "must hold",
    "must have",
    "requires",
    "required to possess",
    "required to hold",
    "required to have",
)
CLEARANCE_NO_PREFIXES = ("no", "does not require")
CLEARANCE_OBTAIN_PREFIXES = (
    "ability to obtain",
    "able to obtain",
    "eligible to obtain",
    "willing to obtain",
    "must be able to obtain",
    "can obtain",
)

# The inventory is deliberately a literal, rather than an ISO package lookup.
ASSIGNED_COUNTRY_CODES = (
    "AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ "
    "BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ "
    "CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ "
    "DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR "
    "GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY "
    "HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP "
    "KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY "
    "MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ "
    "NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA "
    "RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ "
    "TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY UZ "
    "VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW"
).split()
assert len(ASSIGNED_COUNTRY_CODES) == 249
assert sorted(ASSIGNED_COUNTRY_CODES) == ASSIGNED_COUNTRY_CODES


def _manifest() -> dict[str, Any]:
    return {
        "version": POLICY_VERSION,
        "schema_version": SCHEMA_VERSION,
        "rules": list(REASONS),
        "unknowns": list(UNKNOWNS),
        "evidence": list(EVIDENCE),
        "allowed_outcomes": {
            "title_no_match": {"pass": "allowed_title_match", "reject": "allowed_title_absent"},
            "seniority_low": {
                "pass": "excluded_seniority_absent",
                "reject": "excluded_seniority_match",
            },
            "wrong_discipline": {
                "pass": "excluded_discipline_absent",
                "reject": "excluded_discipline_match",
            },
            "not_remote": {
                "pass": "remote_permitted",
                "reject": "attendance_required",
                "unknown": "remote_arrangement_unresolved",
            },
            "geo_excluded": {
                "pass": "us_not_excluded",
                "reject": "remote_scope_excludes_us",
                "unknown": "remote_geography_unresolved",
            },
            "clearance_required": {
                "pass": "active_clearance_not_required",
                "reject": "active_clearance_required",
                "unknown": ["clearance_not_stated", "clearance_ambiguous"],
            },
            "stale": {"pass": "within_45_days", "reject": "older_than_45_days"},
            "already_actioned": {
                "pass": "no_qualifying_action",
                "reject": ["prior_skipped", "prior_applied", "current_version_expired"],
            },
            "company_blocked": {"pass": "company_not_blocked", "reject": "company_blocked"},
        },
        "title_families": list(TITLE_FAMILIES),
        "seniority": list(SENIORITY),
        "disciplines": list(DISCIPLINES),
        "remote": {
            "positive": list(REMOTE_POSITIVE),
            "denial": list(REMOTE_DENIAL),
            "attendance_role": list(ATTENDANCE_ROLE),
            "attendance_mandatory": list(ATTENDANCE_MANDATORY),
            "structured_workplace_type": (
                "'remote' is positive; 'hybrid' and 'on-site' require attendance; "
                "'unspecified', null, malformed, or unrecognized values have no definite conclusion"
            ),
            "weekly": {
                "numbers": [
                    "1",
                    "2",
                    "3",
                    "4",
                    "5",
                    "6",
                    "7",
                    "one",
                    "two",
                    "three",
                    "four",
                    "five",
                    "six",
                    "seven",
                ],
                "obligations": ["required", "must", "expected", "attendance"],
                "day_tokens": ["day", "days"],
                "day_number_distance": 2,
                "day_week_distance": 4,
                "day_office_distance": 8,
                "week_phrases": [
                    ["per", "week"],
                    ["a", "week"],
                    ["each", "week"],
                    ["weekly"],
                ],
                "office_phrases": [
                    ["office"],
                    ["in", "office"],
                    ["in", "the", "office"],
                    ["onsite"],
                    ["on", "site"],
                ],
                "presence_linking": [
                    "report to the office",
                    "report onsite",
                    "report on site",
                ],
                "home_office_exclusion": (
                    "an 'office' token belonging to a contiguous normalized "
                    "'home office' span never satisfies the office condition"
                ),
                "same_sentence": (
                    "all five conditions must hold in one normalized sentence segment"
                ),
            },
            "span_ownership": {
                "denial": (
                    "an approved denial template owns its complete matched token span; a "
                    "positive sequence wholly contained inside that span does not "
                    "independently match"
                ),
                "positive_outside_span": (
                    "structured 'remote' or a positive text occurrence outside the denial "
                    "span remains separate positive evidence"
                ),
            },
            "application_order": [
                "structured hybrid or on-site rejects with attendance_required",
                (
                    "any approved mandatory-attendance role, obligation, or weekly "
                    "template rejects with attendance_required"
                ),
                (
                    "approved remote-denial evidence with no separate structured or "
                    "non-overlapping text positive evidence rejects with attendance_required"
                ),
                (
                    "approved denial evidence plus separate structured or non-overlapping "
                    "text positive evidence, with no stronger mandatory evidence from the "
                    "first two steps, is unknown with remote_arrangement_unresolved"
                ),
                (
                    "positive structured or text remote evidence with no denial or "
                    "mandatory evidence passes with remote_permitted"
                ),
                "no decisive evidence is unknown with remote_arrangement_unresolved",
            ],
        },
        "geography": {
            "us_inclusion": list(US_INCLUSION),
            "us_exclusion": list(US_EXCLUSION),
            "broad": list(BROAD),
            "named_us": list(NAMED_US),
            "named_non_us": list(NAMED_NON_US),
            "assigned_country_codes": list(ASSIGNED_COUNTRY_CODES),
            "remote_applicability": "only pass remote_permitted",
            "code_source": (
                "two-letter ISO codes are recognized only from the structured country_code "
                "field after case normalization; free text never infers a country or "
                "US state/DC postal abbreviation, and no structured state-code field exists"
            ),
            "code_recognition": (
                "membership in the exact closed assigned-country-code inventory only; "
                "values outside the inventory, including 'ZZ', contribute no definite "
                "geography evidence"
            ),
            "exclusion_span_ownership": (
                "an approved exclusion template owns its matched token span; an inclusion "
                "sequence wholly contained inside that span does not independently match"
            ),
            "overrides": (
                "generic broad scope never overrides explicit exclusion; explicit "
                "inclusion never silently overrides exclusion"
            ),
            "broad_connectors": ["in", "within", "across", "throughout", "from"],
            "broad_templates": [
                (
                    "remote-role scope: one exact prefix 'remote', 'remote role', "
                    "'remote position', 'remote job', 'this role is remote', "
                    "'this position is remote', or 'this job is remote', followed by "
                    "zero connector tokens or exactly one connector token from the "
                    "broad_connectors set, followed by one broad sequence"
                ),
                (
                    "applicant opening: 'open' or 'available', followed by 'to', then "
                    "'candidates' or 'applicants', followed by zero connector tokens or "
                    "exactly one connector token from the broad_connectors set, followed "
                    "by one broad sequence"
                ),
                (
                    "applicant eligibility: 'candidates' or 'applicants', followed by "
                    "'may', 'can', or 'must', then 'be located', 'be based', 'reside', "
                    "or 'apply', followed by zero connector tokens or exactly one "
                    "connector token from the broad_connectors set, followed by one "
                    "broad sequence"
                ),
            ],
            "named_scope_templates": [
                (
                    "remote scope: 'remote' followed by 'in', 'within', 'across', or "
                    "'throughout', then a named-scope list"
                ),
                (
                    "candidate scope: 'open' or 'available', followed by 'to', then "
                    "'candidates' or 'applicants', then 'in' or 'within', then a "
                    "named-scope list"
                ),
                (
                    "residence requirement: 'candidates', 'applicants', or 'this role', "
                    "followed by 'must' or 'required to', then 'be located', 'be based', "
                    "or 'reside', then 'in' or 'within', then a named-scope list"
                ),
            ],
            "named_scope_list": {
                "contains": "one or more manifest-listed named sequences and nothing else",
                "separators": ["comma", "semicolon", "slash"],
                "joiners": ["and", "or"],
                "joiner_rule": ("a comma, semicolon, or slash may be followed by 'and' or 'or'"),
                "maximal": (
                    "matching consumes the complete maximal list immediately "
                    "following the template"
                ),
                "same_field_same_sentence": (
                    "the template prefix and its complete list occur in one field "
                    "and one sentence segment"
                ),
            },
            "precedence_matrix": [
                "explicit US inclusion, no explicit exclusion: pass us_not_excluded",
                (
                    "explicit US exclusion, no explicit inclusion, and positive remote "
                    "evidence: reject remote_scope_excludes_us"
                ),
                ("explicit US inclusion and exclusion: " "unknown remote_geography_unresolved"),
                "broad evidence, no explicit exclusion: pass us_not_excluded",
                (
                    "broad evidence plus explicit exclusion, no explicit inclusion, and "
                    "positive remote evidence: reject remote_scope_excludes_us"
                ),
                (
                    "every explicit scope is recognized non-US-only, and positive "
                    "remote evidence: reject remote_scope_excludes_us"
                ),
                (
                    "any otherwise-rejecting exclusion or non-US-only row without "
                    "positive remote evidence: unknown remote_geography_unresolved"
                ),
                (
                    "otherwise unscoped, contradictory, malformed, or unrecognized: "
                    "unknown remote_geography_unresolved"
                ),
            ],
        },
        "clearance": {
            "phrases": list(CLEARANCE_PHRASES),
            "obligation_prefixes": list(CLEARANCE_OBLIGATION_PREFIXES),
            "no_requirement_prefixes": list(CLEARANCE_NO_PREFIXES),
            "obtainability_prefixes": list(CLEARANCE_OBTAIN_PREFIXES),
            "determiners": ["a", "an", "the"],
            "obligation_suffixes": ["is required", "required"],
            "no_requirement_suffixes": [
                "required",
                "is required",
                "not required",
                "is not required",
            ],
            "preference_suffixes": ["preferred", "is preferred"],
            "prefix_adjacency": "zero or one permitted determiner",
            "fixed_suffix_adjacency": "exact and filler-free",
            "span_ownership": (
                "no-requirement, preference, and obtainability suppress contained phrases"
            ),
        },
        "normalization": {
            "newline": "CRLF and CR become LF",
            "segmentation": {
                "paragraph_boundary": (
                    "an LF followed by zero or more non-LF Unicode whitespace characters "
                    "and another LF; consume the complete maximal run"
                ),
                "paragraph_precedence": "paragraph boundaries take precedence",
                "sentence_terminators": [".", "!", "?", "。", "！", "？"],
                "terminator_run": (
                    "a maximal consecutive run of sentence terminators is one " "sentence boundary"
                ),
                "boundary_effect": "each boundary closes the current segment",
                "empty_segments": "empty segments are discarded",
                "segment_numbering": (
                    "non-empty segments are numbered from zero in encounter order"
                ),
            },
            "token_formation": {
                "order": [
                    "CRLF and CR become LF",
                    "scan boundaries before NFKC or punctuation flattening",
                    "per-segment NFKC then casefold",
                    "map the four approved apostrophes to ASCII '",
                    "form maximal Unicode Letter-or-Number tokens",
                ],
                "apostrophe_map": ["’", "‘", "ʼ", "＇"],
                "apostrophe_map_to": "'",
                "apostrophe_rule": (
                    "an ASCII apostrophe between letters stays inside a token; "
                    "apostrophes are otherwise token boundaries"
                ),
                "token_class": "maximal Unicode Letter-or-Number",
                "boundaries": [
                    "whitespace",
                    "Unicode dashes",
                    "/",
                    "\\",
                    "_",
                    "all other punctuation",
                ],
                "no_exceptions": (
                    "no abbreviation, decimal, initial, ellipsis, quotation, "
                    "capitalization, locale, or language exception"
                ),
                "comparison": "ordered token sequences only",
            },
            "gap_categories": [
                "whitespace",
                "comma",
                "semicolon",
                "slash",
                "other_punctuation",
                "sentence_boundary",
                "paragraph_boundary",
            ],
            "gap_precedence": [
                (
                    "sentence_boundary and paragraph_boundary take precedence over the "
                    "other gap categories"
                ),
                "paragraph_boundary takes precedence over sentence_boundary",
            ],
            "token_metadata": ["field_kind", "field_ordinal", "sentence_segment", "token_ordinal"],
            "metadata_consumers": [
                "product_owner_business_analyst",
                "weekly_attendance",
                "broad_scope",
                "named_scope",
            ],
            "derived_state": (
                "normalized tokens, sentence segments, and gap metadata are deterministic "
                "derived evaluation state, never provider input"
            ),
        },
        "input_ownership": {
            "content_fields": ["title", "description", "location.label"],
            "location_label_boundary": (
                "each persisted location label is a distinct field occurrence; matching "
                "never crosses between location labels or between a location label and "
                "title or description"
            ),
            "structured_location_fields": {
                "country_code": "geography only",
                "workplace_type": "remote arrangement only",
            },
            "excluded_location_fields": ["region", "city"],
            "region_city_evidence": (
                "region and city are not Phase 1 hard-filter free-text evidence"
            ),
            "excluded_provider_fields": ["raw_payload", "embedding"],
            "staleness_days": 45,
            "discipline_boundary": {
                "adjacent_disciplines": "title only",
                "compound_branch": (
                    "one normalized title or description sentence segment containing both "
                    "'product owner' and 'business analyst'; the two sequences never "
                    "combine across fields, sentence segments, or paragraph boundaries"
                ),
            },
        },
    }


POLICY_MANIFEST = _manifest()
POLICY_MANIFEST_BYTES = json.dumps(
    POLICY_MANIFEST,
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
    allow_nan=False,
).encode("utf-8")
POLICY_MANIFEST_HASH = hashlib.sha256(POLICY_MANIFEST_BYTES).hexdigest()


def manifest_hash(value: dict[str, Any] | None = None) -> str:
    """Hash a canonical manifest object using the policy JSON contract."""

    serialized = json.dumps(
        POLICY_MANIFEST if value is None else value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def manifest_is_intact() -> bool:
    return (
        POLICY_MANIFEST.get("version") == POLICY_VERSION and manifest_hash() == POLICY_MANIFEST_HASH
    )


def manifest_copy() -> dict[str, Any]:
    """Return a detached manifest for embedding in an evaluation."""

    return copy.deepcopy(POLICY_MANIFEST)
