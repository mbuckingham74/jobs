"""Deterministic foundation contract for Task 008: closed enums, canonical
JSON and hashes, the phase1-hard-filters-v1 manifest, its literal
inventories, and the input-ownership boundary."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import sys
from datetime import UTC, datetime
from typing import Any

import pytest

import app.filters.normalization as normalization_module
import app.filters.policy as policy_module
import app.filters.service as filter_service
from app.filters.evaluator import (
    HardFilterEvidence,
    HardFilterReason,
    HardFilterRule,
    HardFilterRuleOutcome,
    HardFilterUnknown,
    PureFilterInput,
    RuleStatus,
    StalenessSource,
    evaluate_pure,
)
from app.filters.policy import (
    ASSIGNED_COUNTRY_CODES,
    POLICY_MANIFEST,
    POLICY_MANIFEST_BYTES,
    POLICY_MANIFEST_HASH,
    POLICY_VERSION,
    SCHEMA_VERSION,
    manifest_copy,
    manifest_hash,
)

AS_OF = datetime(2026, 8, 1, 12, tzinfo=UTC)

EXPECTED_COUNTRY_CODES = (
    "AD",
    "AE",
    "AF",
    "AG",
    "AI",
    "AL",
    "AM",
    "AO",
    "AQ",
    "AR",
    "AS",
    "AT",
    "AU",
    "AW",
    "AX",
    "AZ",
    "BA",
    "BB",
    "BD",
    "BE",
    "BF",
    "BG",
    "BH",
    "BI",
    "BJ",
    "BL",
    "BM",
    "BN",
    "BO",
    "BQ",
    "BR",
    "BS",
    "BT",
    "BV",
    "BW",
    "BY",
    "BZ",
    "CA",
    "CC",
    "CD",
    "CF",
    "CG",
    "CH",
    "CI",
    "CK",
    "CL",
    "CM",
    "CN",
    "CO",
    "CR",
    "CU",
    "CV",
    "CW",
    "CX",
    "CY",
    "CZ",
    "DE",
    "DJ",
    "DK",
    "DM",
    "DO",
    "DZ",
    "EC",
    "EE",
    "EG",
    "EH",
    "ER",
    "ES",
    "ET",
    "FI",
    "FJ",
    "FK",
    "FM",
    "FO",
    "FR",
    "GA",
    "GB",
    "GD",
    "GE",
    "GF",
    "GG",
    "GH",
    "GI",
    "GL",
    "GM",
    "GN",
    "GP",
    "GQ",
    "GR",
    "GS",
    "GT",
    "GU",
    "GW",
    "GY",
    "HK",
    "HM",
    "HN",
    "HR",
    "HT",
    "HU",
    "ID",
    "IE",
    "IL",
    "IM",
    "IN",
    "IO",
    "IQ",
    "IR",
    "IS",
    "IT",
    "JE",
    "JM",
    "JO",
    "JP",
    "KE",
    "KG",
    "KH",
    "KI",
    "KM",
    "KN",
    "KP",
    "KR",
    "KW",
    "KY",
    "KZ",
    "LA",
    "LB",
    "LC",
    "LI",
    "LK",
    "LR",
    "LS",
    "LT",
    "LU",
    "LV",
    "LY",
    "MA",
    "MC",
    "MD",
    "ME",
    "MF",
    "MG",
    "MH",
    "MK",
    "ML",
    "MM",
    "MN",
    "MO",
    "MP",
    "MQ",
    "MR",
    "MS",
    "MT",
    "MU",
    "MV",
    "MW",
    "MX",
    "MY",
    "MZ",
    "NA",
    "NC",
    "NE",
    "NF",
    "NG",
    "NI",
    "NL",
    "NO",
    "NP",
    "NR",
    "NU",
    "NZ",
    "OM",
    "PA",
    "PE",
    "PF",
    "PG",
    "PH",
    "PK",
    "PL",
    "PM",
    "PN",
    "PR",
    "PS",
    "PT",
    "PW",
    "PY",
    "QA",
    "RE",
    "RO",
    "RS",
    "RU",
    "RW",
    "SA",
    "SB",
    "SC",
    "SD",
    "SE",
    "SG",
    "SH",
    "SI",
    "SJ",
    "SK",
    "SL",
    "SM",
    "SN",
    "SO",
    "SR",
    "SS",
    "ST",
    "SV",
    "SX",
    "SY",
    "SZ",
    "TC",
    "TD",
    "TF",
    "TG",
    "TH",
    "TJ",
    "TK",
    "TL",
    "TM",
    "TN",
    "TO",
    "TR",
    "TT",
    "TV",
    "TW",
    "TZ",
    "UA",
    "UG",
    "UM",
    "US",
    "UY",
    "UZ",
    "VA",
    "VC",
    "VE",
    "VG",
    "VI",
    "VN",
    "VU",
    "WF",
    "WS",
    "YE",
    "YT",
    "ZA",
    "ZM",
    "ZW",
)

EXPECTED_MATRIX: dict[str, dict[str, set[str]]] = {
    "title_no_match": {
        "pass": {"allowed_title_match"},
        "reject": {"allowed_title_absent"},
    },
    "seniority_low": {
        "pass": {"excluded_seniority_absent"},
        "reject": {"excluded_seniority_match"},
    },
    "wrong_discipline": {
        "pass": {"excluded_discipline_absent"},
        "reject": {"excluded_discipline_match"},
    },
    "not_remote": {
        "pass": {"remote_permitted"},
        "reject": {"attendance_required"},
        "unknown": {"remote_arrangement_unresolved"},
    },
    "geo_excluded": {
        "pass": {"us_not_excluded"},
        "reject": {"remote_scope_excludes_us"},
        "unknown": {"remote_geography_unresolved"},
    },
    "clearance_required": {
        "pass": {"active_clearance_not_required"},
        "reject": {"active_clearance_required"},
        "unknown": {"clearance_not_stated", "clearance_ambiguous"},
    },
    "stale": {
        "pass": {"within_45_days"},
        "reject": {"older_than_45_days"},
    },
    "already_actioned": {
        "pass": {"no_qualifying_action"},
        "reject": {"prior_skipped", "prior_applied", "current_version_expired"},
    },
    "company_blocked": {
        "pass": {"company_not_blocked"},
        "reject": {"company_blocked"},
    },
}


def _walk_keys(value: object) -> Any:
    if isinstance(value, dict):
        for key in value:
            yield key
            yield from _walk_keys(value[key])
    elif isinstance(value, list):
        for item in value:
            yield from _walk_keys(item)


# ----------------------------------------------------------------------
# Closed enums
# ----------------------------------------------------------------------


def test_all_enum_memberships_and_orders_are_exact() -> None:
    assert tuple(item.value for item in HardFilterReason) == (
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
    assert tuple(item.value for item in HardFilterUnknown) == (
        "remote_arrangement_unresolved",
        "remote_geography_unresolved",
        "clearance_not_stated",
        "clearance_ambiguous",
    )
    assert tuple(item.value for item in StalenessSource) == (
        "source_published_at",
        "first_seen_at",
    )
    assert tuple(item.value for item in RuleStatus) == ("pass", "reject", "unknown")
    assert tuple(item.value for item in HardFilterRule) == tuple(
        item.value for item in HardFilterReason
    )
    assert tuple(item.value for item in HardFilterEvidence) == (
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
    assert len(tuple(HardFilterEvidence)) == 24
    assert tuple(HardFilterRuleOutcome.__dataclass_fields__) == ("rule", "status", "evidence")
    assert [item.value for item in HardFilterReason] == POLICY_MANIFEST["rules"]
    assert [item.value for item in HardFilterUnknown] == POLICY_MANIFEST["unknowns"]
    assert [item.value for item in HardFilterEvidence] == POLICY_MANIFEST["evidence"]


# ----------------------------------------------------------------------
# Rule / status / evidence matrix
# ----------------------------------------------------------------------


def test_rule_status_evidence_matrix_is_exact() -> None:
    manifest_matrix = POLICY_MANIFEST["allowed_outcomes"]
    assert tuple(manifest_matrix) == tuple(item.value for item in HardFilterRule)
    for rule, statuses in EXPECTED_MATRIX.items():
        for status, evidences in statuses.items():
            stored = manifest_matrix[rule].get(status)
            assert stored is not None
            values = stored if isinstance(stored, list) else [stored]
            assert set(values) == evidences
        stored_statuses = set(manifest_matrix[rule])
        assert stored_statuses == set(statuses)
    for rule in (
        "title_no_match",
        "seniority_low",
        "wrong_discipline",
        "stale",
        "already_actioned",
        "company_blocked",
    ):
        assert "unknown" not in manifest_matrix[rule]


def test_any_rule_status_evidence_triple_outside_the_matrix_is_invalid() -> None:
    legal: set[tuple[str, str, str]] = set()
    for rule, statuses in EXPECTED_MATRIX.items():
        for status, evidences in statuses.items():
            legal.update((rule, status, evidence) for evidence in evidences)
    manifest_legal: set[tuple[str, str, str]] = set()
    for rule, statuses in POLICY_MANIFEST["allowed_outcomes"].items():
        for status, evidence in statuses.items():
            values = evidence if isinstance(evidence, list) else [evidence]
            manifest_legal.update((rule, status, value) for value in values)
    assert manifest_legal == legal
    for rule in (item.value for item in HardFilterRule):
        for status in (item.value for item in RuleStatus):
            for evidence in (item.value for item in HardFilterEvidence):
                assert ((rule, status, evidence) in legal) == (
                    (rule, status, evidence) in manifest_legal
                )
    covered = {triple[2] for triple in legal}
    assert covered == set(item.value for item in HardFilterEvidence)


# ----------------------------------------------------------------------
# Canonical JSON and hashes
# ----------------------------------------------------------------------


GOLDEN_OBJECT = {
    "z": "last",
    "a": {"d": "x", "c": [1, 2], "б": 2, "г": 1},
    "m": None,
    "t": True,
    "s": "café",
}
GOLDEN_BYTES = (
    b'{"a":{"c":[1,2],"d":"x","\xd0\xb1":2,"\xd0\xb3":1},'
    b'"m":null,"s":"caf\xc3\xa9","t":true,"z":"last"}'
)
GOLDEN_HASH = "e8cd063df400603dff20fc869bcb13c42d583156687799858b7def88f0ac7788"


def test_canonical_json_bytes_and_hashes_reproduce_exactly() -> None:
    parsed, digest = filter_service._canonical_json(GOLDEN_OBJECT)
    serialized = json.dumps(
        parsed,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    assert serialized == GOLDEN_BYTES
    assert digest == GOLDEN_HASH
    assert filter_service._canonical_json(GOLDEN_OBJECT)[1] == GOLDEN_HASH
    assert filter_service._hash_json(GOLDEN_OBJECT) == GOLDEN_HASH
    assert len(digest) == 64 and digest == digest.lower()


def test_canonical_timestamp_contract_is_exact() -> None:
    assert (
        filter_service._timestamp(datetime(2026, 8, 1, 12, tzinfo=UTC))
        == "2026-08-01T12:00:00.000000Z"
    )
    assert filter_service._timestamp(None) is None


# ----------------------------------------------------------------------
# Manifest canonicalization and hash stability
# ----------------------------------------------------------------------


def test_manifest_canonicalization_repeats_exactly() -> None:
    first = json.dumps(
        POLICY_MANIFEST,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    second = json.dumps(
        manifest_copy(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    assert first == second == POLICY_MANIFEST_BYTES
    assert manifest_hash() == manifest_hash() == POLICY_MANIFEST_HASH
    assert hashlib.sha256(POLICY_MANIFEST_BYTES).hexdigest() == POLICY_MANIFEST_HASH
    assert POLICY_MANIFEST_HASH == POLICY_MANIFEST_HASH.lower()
    assert len(POLICY_MANIFEST_HASH) == 64


def test_manifest_version_schema_and_intactness_are_exact() -> None:
    assert POLICY_VERSION == "phase1-hard-filters-v1"
    assert SCHEMA_VERSION == 1
    assert POLICY_MANIFEST["version"] == POLICY_VERSION
    assert POLICY_MANIFEST["schema_version"] == SCHEMA_VERSION
    assert policy_module.manifest_is_intact() is True


def _drop_title_family(manifest: dict[str, Any]) -> None:
    manifest["title_families"].pop()


def _add_positive_phrase(manifest: dict[str, Any]) -> None:
    manifest["remote"]["positive"].append("remote anywhere")


def _remove_country_code(manifest: dict[str, Any]) -> None:
    manifest["geography"]["assigned_country_codes"].remove("US")


def _add_sentence_terminator(manifest: dict[str, Any]) -> None:
    manifest["normalization"]["segmentation"]["sentence_terminators"].append(";")


def _change_staleness_days(manifest: dict[str, Any]) -> None:
    manifest["input_ownership"]["staleness_days"] = 44


def _add_seniority_token(manifest: dict[str, Any]) -> None:
    manifest["seniority"].append("entry")


@pytest.mark.parametrize(
    "mutate",
    [
        _drop_title_family,
        _add_positive_phrase,
        _remove_country_code,
        _add_sentence_terminator,
        _change_staleness_days,
        _add_seniority_token,
    ],
)
def test_changing_a_manifest_owned_literal_changes_the_manifest_hash(mutate) -> None:
    altered = manifest_copy()
    mutate(altered)
    assert manifest_hash(altered) != POLICY_MANIFEST_HASH


# ----------------------------------------------------------------------
# Country-code inventory
# ----------------------------------------------------------------------


def test_country_code_inventory_is_exactly_the_249_literal() -> None:
    assert tuple(ASSIGNED_COUNTRY_CODES) == EXPECTED_COUNTRY_CODES
    assert len(ASSIGNED_COUNTRY_CODES) == 249
    assert len(set(ASSIGNED_COUNTRY_CODES)) == 249
    assert all(re.fullmatch(r"[A-Z]{2}", code) for code in ASSIGNED_COUNTRY_CODES)
    assert sorted(ASSIGNED_COUNTRY_CODES) == list(ASSIGNED_COUNTRY_CODES)
    assert ASSIGNED_COUNTRY_CODES[0] == "AD"
    assert ASSIGNED_COUNTRY_CODES[124] == "KZ"
    assert ASSIGNED_COUNTRY_CODES[248] == "ZW"
    assert "US" in ASSIGNED_COUNTRY_CODES
    assert "ZZ" not in ASSIGNED_COUNTRY_CODES
    assert tuple(POLICY_MANIFEST["geography"]["assigned_country_codes"]) == EXPECTED_COUNTRY_CODES


# ----------------------------------------------------------------------
# No runtime ISO / locale / network lookup
# ----------------------------------------------------------------------

_IMPORT_RE = re.compile(r"^\s*(?:import|from)\s+([\w.]+)", re.M)


def test_no_runtime_iso_locale_or_network_lookup_influences_recognition_or_hashing() -> None:
    sources = inspect.getsource(policy_module) + "\n" + inspect.getsource(normalization_module)
    imported = {match.group(1).split(".")[0] for match in _IMPORT_RE.finditer(sources)}
    for banned in (
        "locale",
        "pycountry",
        "iso3166",
        "iso639",
        "babel",
        "langcodes",
        "socket",
        "httpx",
        "requests",
        "urllib",
    ):
        assert banned not in imported, banned
    for module in ("pycountry", "iso3166", "iso639", "babel", "langcodes"):
        assert module not in sys.modules
    assert all(isinstance(code, str) for code in ASSIGNED_COUNTRY_CODES)
    assert "US" in ASSIGNED_COUNTRY_CODES
    assert "ZZ" not in ASSIGNED_COUNTRY_CODES
    assert manifest_hash() == POLICY_MANIFEST_HASH


# ----------------------------------------------------------------------
# Canonical structures never carry derived metadata
# ----------------------------------------------------------------------

DERIVED_KEYS = {
    "tokens",
    "gaps",
    "segments",
    "sentence_segment",
    "token_ordinal",
    "field_kind",
    "gap_category",
}


def _snapshot() -> dict[str, Any]:
    return {
        "posting": {
            "id": 1,
            "company_id": 2,
            "source_published_at": None,
            "first_seen_at": AS_OF,
            "closed_at": None,
            "current_version_id": 3,
        },
        "version": {
            "id": 3,
            "posting_id": 1,
            "observed_in_run_id": 4,
            "content_hash": "a" * 64,
            "title": "Product Manager",
            "locations": [{"label": "Remote, US"}],
            "description_md": "Work from a customer site",
        },
        "company": {"blocked": False},
    }


def test_derived_metadata_is_never_inserted_into_canonical_structures() -> None:
    snapshot = _snapshot()
    input_payload, input_hash, mutable_hash = filter_service._input_payload(
        snapshot, (), policy_version=POLICY_VERSION, as_of=AS_OF
    )
    assert set(input_payload) == {
        "evaluation_as_of",
        "mutable_state",
        "mutable_state_hash",
        "policy_manifest",
        "policy_manifest_hash",
        "policy_version",
        "posting_id",
        "posting_version_content_hash",
        "posting_version_id",
        "schema_version",
    }
    assert input_payload["mutable_state_hash"] == mutable_hash
    assert not (set(_walk_keys(input_payload)) & DERIVED_KEYS)

    pure = evaluate_pure(
        PureFilterInput(
            posting_id=1,
            posting_version_id=3,
            content_hash="a" * 64,
            title="Product Manager",
            description_md="Work from a customer site",
            locations=({"label": "Remote, US"},),
            source_published_at=None,
            first_seen_at=AS_OF,
            current_version_id=3,
            closed_at=None,
            company_blocked=False,
            action_state=(),
        ),
        evaluation_as_of=AS_OF,
    )
    output_payload, result_hash = filter_service._output_payload(pure)
    assert set(output_payload) == {
        "eligible",
        "rejection_reasons",
        "rule_outcomes",
        "schema_version",
        "staleness_source",
        "unknowns",
    }
    assert not (set(_walk_keys(output_payload)) & DERIVED_KEYS)
    assert filter_service._canonical_json(input_payload)[1] == input_hash
    assert filter_service._canonical_json(output_payload)[1] == result_hash


# ----------------------------------------------------------------------
# Input ownership: region and city
# ----------------------------------------------------------------------


def test_region_and_city_supply_no_phase1_free_text_evidence() -> None:
    assert POLICY_MANIFEST["input_ownership"]["excluded_location_fields"] == ["region", "city"]
    result = evaluate_pure(
        PureFilterInput(
            posting_id=1,
            posting_version_id=2,
            content_hash="a" * 64,
            title="Product Manager",
            description_md=None,
            locations=({"region": "US", "city": "New York", "workplace_type": "remote"},),
            source_published_at=None,
            first_seen_at=AS_OF,
            current_version_id=2,
            closed_at=None,
            company_blocked=False,
            action_state=(),
        ),
        evaluation_as_of=AS_OF,
    )
    remote = next(item for item in result.rule_outcomes if item.rule is HardFilterRule.NOT_REMOTE)
    assert remote.status is RuleStatus.PASS
    geo = next(item for item in result.rule_outcomes if item.rule is HardFilterRule.GEO_EXCLUDED)
    assert geo.status is RuleStatus.UNKNOWN
    assert geo.evidence is HardFilterEvidence.REMOTE_GEOGRAPHY_UNRESOLVED


# ----------------------------------------------------------------------
# Removed legacy values
# ----------------------------------------------------------------------


def test_manifest_contains_no_removed_legacy_discipline_value() -> None:
    for legacy in ("discipline_unknown", "discipline_unresolved", "excluded_discipline_unknown"):
        assert legacy not in repr(POLICY_MANIFEST)
    for value in POLICY_MANIFEST["rules"]:
        assert HardFilterReason(value) is not None
    for value in POLICY_MANIFEST["unknowns"]:
        assert HardFilterUnknown(value) is not None
    for value in POLICY_MANIFEST["evidence"]:
        assert HardFilterEvidence(value) is not None
