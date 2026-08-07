"""Pure contract coverage for Task 008's closed policy surface."""

from __future__ import annotations

import socket
from datetime import UTC, datetime, timedelta
from inspect import signature
from types import SimpleNamespace

import httpx
import pytest

import app.filters.service as filter_service
from app.filters.evaluator import (
    ActionFact,
    HardFilterEvidence,
    HardFilterReason,
    HardFilterRule,
    HardFilterUnknown,
    PureFilterInput,
    RuleStatus,
    evaluate_pure,
)
from app.filters.normalization import normalize_field
from app.filters.policy import (
    ASSIGNED_COUNTRY_CODES,
    POLICY_MANIFEST,
    POLICY_MANIFEST_HASH,
    UNKNOWNS,
)
from app.filters.results import (
    CurrentVersionError,
    FilterDatabaseError,
    FilterErrorCode,
    FilterEvaluationConflictError,
    FilterEvaluatorDefectError,
    FilterInputError,
    FilterPersistedInputError,
    PostingClosedError,
    PostingNotFoundError,
    StaleFilterInputError,
    UnsupportedFilterPolicyError,
)

AS_OF = datetime(2026, 8, 1, 12, tzinfo=UTC)


def evaluate(
    *,
    title: str = "Product Manager",
    description: str | None = "Remote worldwide",
    locations=(),
    **kwargs,
):
    return evaluate_pure(
        PureFilterInput(
            posting_id=1,
            posting_version_id=2,
            content_hash="a" * 64,
            title=title,
            description_md=description,
            locations=tuple(locations),
            source_published_at=kwargs.get("source_published_at"),
            first_seen_at=kwargs.get("first_seen_at", AS_OF),
            current_version_id=2,
            closed_at=None,
            company_blocked=kwargs.get("company_blocked", False),
            action_state=kwargs.get("action_state", ()),
        ),
        evaluation_as_of=AS_OF,
    )


def outcome(result, rule: HardFilterRule):
    return next(item for item in result.rule_outcomes if item.rule is rule)


def test_closed_vocabularies_and_order() -> None:
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
    assert tuple(item.value for item in HardFilterUnknown) == UNKNOWNS
    assert len(tuple(HardFilterEvidence)) == 24
    assert tuple(item.rule for item in evaluate().rule_outcomes) == tuple(HardFilterRule)


@pytest.mark.parametrize(
    "title",
    [
        "Marketing Director, Senior Group Product Manager",
        "Director Product Manager Technical Platform",
        "Technical Program Manager",
    ],
)
def test_title_family_is_contiguous_and_modifiers_are_allowed(title: str) -> None:
    assert outcome(evaluate(title=title), HardFilterRule.TITLE_NO_MATCH).status is RuleStatus.PASS


@pytest.mark.parametrize(
    "title",
    [
        "Product Marketing Manager",
        "Product Design Manager",
        "Product Designer",
        "Sales Engineer",
        "Sales Engineering Manager",
    ],
)
def test_adjacent_disciplines_reject_title_only(title: str) -> None:
    result = evaluate(title=title, description="partner with product design and sales engineering")
    assert (
        outcome(result, HardFilterRule.WRONG_DISCIPLINE).evidence
        is HardFilterEvidence.EXCLUDED_DISCIPLINE_MATCH
    )


def test_compound_discipline_requires_one_sentence_and_field() -> None:
    assert (
        outcome(
            evaluate(description="Product owner, business analyst"), HardFilterRule.WRONG_DISCIPLINE
        ).status
        is RuleStatus.REJECT
    )
    for separator in (".", "!", "?", "。", "！", "？", "\n\n"):
        assert (
            outcome(
                evaluate(description=f"Product owner{separator}Business analyst"),
                HardFilterRule.WRONG_DISCIPLINE,
            ).status
            is RuleStatus.PASS
        )
    assert (
        outcome(
            evaluate(description="Product owner\nBusiness analyst"), HardFilterRule.WRONG_DISCIPLINE
        ).status
        is RuleStatus.REJECT
    )
    assert (
        outcome(
            evaluate(description="Product owner", title="Product Manager"),
            HardFilterRule.WRONG_DISCIPLINE,
        ).status
        is RuleStatus.PASS
    )


@pytest.mark.parametrize(
    "description",
    [
        "Employees are expected to work in the office three days per week",
        "Must be onsite 2 days a week",
        "Hybrid role",
        "Must work from the office",
        "Required to report onsite",
        "Must work on site",
        "This role is not remote",
        "Employees cannot work remotely",
    ],
)
def test_attendance_and_denial_evidence_rejects(description: str) -> None:
    assert (
        outcome(evaluate(description=description), HardFilterRule.NOT_REMOTE).evidence
        is HardFilterEvidence.ATTENDANCE_REQUIRED
    )


def test_home_office_is_not_weekly_physical_attendance() -> None:
    result = evaluate(description="Employees must work from their home office three days per week")
    assert outcome(result, HardFilterRule.NOT_REMOTE).status is RuleStatus.UNKNOWN
    result = evaluate(description="Employees must be in the office three days per week")
    assert outcome(result, HardFilterRule.NOT_REMOTE).status is RuleStatus.REJECT


def test_weekly_presence_link_must_share_day_sentence() -> None:
    result = evaluate(
        description=(
            "You will work three days per week in the office. "
            "Occasionally staff report to the office to pick up equipment."
        )
    )
    assert outcome(result, HardFilterRule.NOT_REMOTE).status is RuleStatus.UNKNOWN


def test_denial_span_ownership_and_conflict() -> None:
    result = evaluate(description="This role is not remote")
    assert outcome(result, HardFilterRule.NOT_REMOTE).status is RuleStatus.REJECT
    result = evaluate(
        description="This role is not remote. A different division offers a fully remote role"
    )
    assert (
        outcome(result, HardFilterRule.NOT_REMOTE).evidence
        is HardFilterEvidence.REMOTE_ARRANGEMENT_UNRESOLVED
    )


@pytest.mark.parametrize(
    "description, remote_evidence",
    [
        ("Remote worldwide", HardFilterEvidence.REMOTE_PERMITTED),
        ("Open to applicants worldwide", HardFilterEvidence.REMOTE_ARRANGEMENT_UNRESOLVED),
        ("Remote in the US", HardFilterEvidence.REMOTE_PERMITTED),
    ],
)
def test_scoped_remote_geography_passes(
    description: str, remote_evidence: HardFilterEvidence
) -> None:
    result = evaluate(description=description)
    assert outcome(result, HardFilterRule.NOT_REMOTE).evidence is remote_evidence
    assert (
        outcome(result, HardFilterRule.GEO_EXCLUDED).evidence is HardFilterEvidence.US_NOT_EXCLUDED
    )


def test_non_us_scope_requires_remote_gate_and_exclusion_span_owns_inclusion() -> None:
    result = evaluate(description="Remote worldwide, except the United States")
    assert (
        outcome(result, HardFilterRule.GEO_EXCLUDED).evidence
        is HardFilterEvidence.REMOTE_SCOPE_EXCLUDES_US
    )
    result = evaluate(description="Remote in the US, but not available to US applicants")
    assert outcome(result, HardFilterRule.GEO_EXCLUDED).status is RuleStatus.UNKNOWN
    result = evaluate(description="Join our global company")
    assert outcome(result, HardFilterRule.GEO_EXCLUDED).status is RuleStatus.UNKNOWN


@pytest.mark.parametrize(
    "description",
    [
        "Candidates can be located worldwide",
        "Applicants must reside in North America",
    ],
)
def test_broad_scope_accepts_all_applicant_eligibility_modals(description: str) -> None:
    result = evaluate(description=description)
    assert outcome(result, HardFilterRule.NOT_REMOTE).evidence is (
        HardFilterEvidence.REMOTE_ARRANGEMENT_UNRESOLVED
    )
    assert (
        outcome(result, HardFilterRule.GEO_EXCLUDED).evidence is HardFilterEvidence.US_NOT_EXCLUDED
    )


@pytest.mark.parametrize(
    "description",
    [
        "Candidates required to be located in Europe",
        "Applicants required to reside within Canada",
    ],
)
def test_named_scope_accepts_required_to_residence_modal(description: str) -> None:
    result = evaluate(description=description)
    assert outcome(result, HardFilterRule.NOT_REMOTE).evidence is (
        HardFilterEvidence.REMOTE_ARRANGEMENT_UNRESOLVED
    )
    assert (
        outcome(result, HardFilterRule.GEO_EXCLUDED).evidence
        is HardFilterEvidence.REMOTE_GEOGRAPHY_UNRESOLVED
    )


def test_structured_geography_is_closed_and_remote_specific() -> None:
    result = evaluate(
        description=None,
        locations=({"label": "Remote", "country_code": "CA", "workplace_type": "remote"},),
    )
    assert outcome(result, HardFilterRule.GEO_EXCLUDED).status is RuleStatus.REJECT
    result = evaluate(
        description=None,
        locations=({"label": "Office", "country_code": "CA", "workplace_type": "on-site"},),
    )
    assert outcome(result, HardFilterRule.NOT_REMOTE).status is RuleStatus.REJECT
    assert outcome(result, HardFilterRule.GEO_EXCLUDED).status is RuleStatus.UNKNOWN
    assert "ZZ" not in ASSIGNED_COUNTRY_CODES
    assert len(ASSIGNED_COUNTRY_CODES) == 249


@pytest.mark.parametrize(
    "description, evidence",
    [
        (
            "An active security clearance is preferred",
            HardFilterEvidence.ACTIVE_CLEARANCE_NOT_REQUIRED,
        ),
        (
            "No active security clearance is required",
            HardFilterEvidence.ACTIVE_CLEARANCE_NOT_REQUIRED,
        ),
        (
            "Does not require an active security clearance",
            HardFilterEvidence.ACTIVE_CLEARANCE_NOT_REQUIRED,
        ),
        (
            "Ability to obtain an active security clearance",
            HardFilterEvidence.ACTIVE_CLEARANCE_NOT_REQUIRED,
        ),
        ("Must possess active security clearance", HardFilterEvidence.ACTIVE_CLEARANCE_REQUIRED),
        ("Must possess an active security clearance", HardFilterEvidence.ACTIVE_CLEARANCE_REQUIRED),
        (
            "A current ts sci clearance is required",
            HardFilterEvidence.ACTIVE_CLEARANCE_REQUIRED,
        ),
        (
            "Requires strong communication skills and a current secret clearance",
            HardFilterEvidence.CLEARANCE_NOT_STATED,
        ),
    ],
)
def test_clearance_adjacency_and_suppression(
    description: str, evidence: HardFilterEvidence
) -> None:
    assert (
        outcome(evaluate(description=description), HardFilterRule.CLEARANCE_REQUIRED).evidence
        is evidence
    )


def test_clearance_independent_conflict_is_ambiguous() -> None:
    result = evaluate(
        description=(
            "Must possess an active security clearance. "
            "No active security clearance is required for this role."
        )
    )
    assert (
        outcome(result, HardFilterRule.CLEARANCE_REQUIRED).evidence
        is HardFilterEvidence.CLEARANCE_AMBIGUOUS
    )
    assert HardFilterReason.CLEARANCE_REQUIRED not in result.rejection_reasons


def test_reasons_unknowns_and_action_precedence_are_canonical() -> None:
    result = evaluate(
        title="Analyst",
        description="Must possess an active security clearance",
        company_blocked=True,
        source_published_at=AS_OF - timedelta(days=46),
        action_state=(
            ActionFact(3, 1, 99, "expired"),
            ActionFact(4, 1, 2, "applied"),
            ActionFact(5, 1, 2, "skipped"),
        ),
    )
    assert result.rejection_reasons == (
        HardFilterReason.TITLE_NO_MATCH,
        HardFilterReason.SENIORITY_LOW,
        HardFilterReason.CLEARANCE_REQUIRED,
        HardFilterReason.STALE,
        HardFilterReason.ALREADY_ACTIONED,
        HardFilterReason.COMPANY_BLOCKED,
    )
    assert result.unknowns == (
        HardFilterUnknown.REMOTE_ARRANGEMENT_UNRESOLVED,
        HardFilterUnknown.REMOTE_GEOGRAPHY_UNRESOLVED,
    )
    assert (
        outcome(result, HardFilterRule.ALREADY_ACTIONED).evidence
        is HardFilterEvidence.PRIOR_SKIPPED
    )


def test_normalization_metadata_preserves_sentence_and_gap_boundaries() -> None:
    field = normalize_field("A, B\nC. D\n\nE", field_kind="description")
    assert [
        (
            token.value,
            token.field_kind,
            token.field_ordinal,
            token.sentence_segment,
            token.token_ordinal,
        )
        for token in field.tokens
    ] == [
        ("a", "description", 0, 0, 0),
        ("b", "description", 0, 0, 1),
        ("c", "description", 0, 0, 2),
        ("d", "description", 0, 1, 3),
        ("e", "description", 0, 2, 4),
    ]
    assert [gap.category for gap in field.gaps] == [
        "comma",
        "whitespace",
        "sentence_boundary",
        "paragraph_boundary",
    ]


def test_manifest_is_stable_and_has_no_removed_discipline_unknown() -> None:
    assert POLICY_MANIFEST["version"] == "phase1-hard-filters-v1"
    assert "discipline_unknown" not in repr(POLICY_MANIFEST)
    assert len(POLICY_MANIFEST_HASH) == 64


def test_public_callable_signature_and_input_validation() -> None:
    parameters = tuple(signature(filter_service.evaluate_hard_filters).parameters)
    assert parameters == ("engine", "posting_id", "policy_version", "evaluation_as_of")

    with pytest.raises(FilterInputError) as caught:
        filter_service.evaluate_hard_filters(
            engine=SimpleNamespace(),
            posting_id=True,
            policy_version="phase1-hard-filters-v1",
            evaluation_as_of=AS_OF,
        )
    assert caught.value.code is FilterErrorCode.INVALID_INPUT

    with pytest.raises(FilterInputError) as caught:
        filter_service.evaluate_hard_filters(
            engine=SimpleNamespace(),
            posting_id=1,
            policy_version="not supported",
            evaluation_as_of=AS_OF,
        )
    assert caught.value.code is FilterErrorCode.INVALID_INPUT

    with pytest.raises(UnsupportedFilterPolicyError) as caught:
        filter_service.evaluate_hard_filters(
            engine=SimpleNamespace(),
            posting_id=1,
            policy_version="phase1-hard-filters-v2",
            evaluation_as_of=AS_OF,
        )
    assert caught.value.code is FilterErrorCode.POLICY_UNSUPPORTED


@pytest.mark.parametrize(
    "error",
    [
        PostingNotFoundError(FilterErrorCode.POSTING_NOT_FOUND),
        PostingClosedError(FilterErrorCode.POSTING_CLOSED),
        CurrentVersionError(FilterErrorCode.CURRENT_VERSION_INVALID),
        FilterPersistedInputError(FilterErrorCode.PERSISTED_INPUT_INVALID),
        StaleFilterInputError(FilterErrorCode.INPUT_STALE),
        FilterEvaluationConflictError(FilterErrorCode.EVALUATION_CONFLICT),
        FilterDatabaseError(FilterErrorCode.DATABASE_ERROR),
        FilterEvaluatorDefectError(FilterErrorCode.EVALUATOR_DEFECT),
    ],
)
def test_public_callable_preserves_closed_error_codes(monkeypatch, error) -> None:
    snapshot = {
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
            "locations": [],
            "description_md": "Remote worldwide",
        },
        "company": {"blocked": False},
    }

    def raise_error(*args, **kwargs):
        raise error

    monkeypatch.setattr(filter_service, "_load_snapshot", lambda engine, posting_id: (snapshot, ()))
    monkeypatch.setattr(filter_service, "persist_or_replay", raise_error)
    with pytest.raises(type(error)) as caught:
        filter_service.evaluate_hard_filters(
            engine=SimpleNamespace(),
            posting_id=1,
            policy_version="phase1-hard-filters-v1",
            evaluation_as_of=AS_OF,
        )
    assert caught.value.code is error.code
    assert str(caught.value) == error.code.value


def test_canonical_service_payloads_recompute_their_hashes() -> None:
    snapshot = {
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
            "locations": [],
            "description_md": "Remote worldwide",
        },
        "company": {"blocked": False},
    }
    input_payload, input_hash, mutable_hash = filter_service._input_payload(
        snapshot, (), policy_version="phase1-hard-filters-v1", as_of=AS_OF
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
    assert filter_service._canonical_json(input_payload)[1] == input_hash

    pure = evaluate()
    output_payload, result_hash = filter_service._output_payload(pure)
    assert filter_service._canonical_json(output_payload)[1] == result_hash
    assert "Remote worldwide" not in repr(input_payload)
    assert "Product Manager" not in repr(output_payload)


def test_filter_evaluation_has_no_network_or_model_dependency(monkeypatch) -> None:
    def fail(*args, **kwargs):
        raise AssertionError("network/model entry point called")

    monkeypatch.setattr(socket, "create_connection", fail)
    monkeypatch.setattr(httpx.Client, "request", fail)
    result = evaluate()
    assert result.eligible is True
