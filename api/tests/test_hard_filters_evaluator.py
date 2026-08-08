"""Focused pure-evaluator contract tests for Task 008's nine rules."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta

import pytest

import app.filters.evaluator as evaluator_module
from app.filters.evaluator import (
    ActionFact,
    HardFilterEvidence,
    HardFilterReason,
    HardFilterRule,
    HardFilterRuleOutcome,
    HardFilterUnknown,
    PureFilterInput,
    PureFilterResult,
    RuleStatus,
    StalenessSource,
    evaluate_pure,
    validate_pure_result,
)
from app.filters.policy import CLEARANCE_OBTAIN_PREFIXES, CLEARANCE_PHRASES
from app.filters.results import FilterErrorCode, FilterPersistedInputError

AS_OF = datetime(2026, 8, 1, 12, tzinfo=UTC)


def evaluate(
    *,
    title: str = "Product Manager",
    description: str | None = None,
    locations=(),
    source_published_at=None,
    first_seen_at=AS_OF,
    current_version_id: int = 2,
    company_blocked: bool = False,
    action_state=(),
    as_of=AS_OF,
):
    return evaluate_pure(
        PureFilterInput(
            posting_id=1,
            posting_version_id=2,
            content_hash="a" * 64,
            title=title,
            description_md=description,
            locations=tuple(locations),
            source_published_at=source_published_at,
            first_seen_at=first_seen_at,
            current_version_id=current_version_id,
            closed_at=None,
            company_blocked=company_blocked,
            action_state=tuple(action_state),
        ),
        evaluation_as_of=as_of,
    )


def outcome(result, rule: HardFilterRule):
    return next(item for item in result.rule_outcomes if item.rule is rule)


def fact(state: str, *, score_posting_version_id: int = 2, digest_item_id: int = 1) -> ActionFact:
    return ActionFact(digest_item_id, 1, score_posting_version_id, state)


def _result_with_outcomes(outcomes, **overrides) -> PureFilterResult:
    reasons = tuple(
        HardFilterReason(item.rule.value) for item in outcomes if item.status is RuleStatus.REJECT
    )
    unknowns = tuple(
        HardFilterUnknown(item.evidence.value)
        for item in outcomes
        if item.status is RuleStatus.UNKNOWN
    )
    values = {
        "eligible": not reasons,
        "rejection_reasons": reasons,
        "unknowns": unknowns,
        "staleness_source": StalenessSource.SOURCE_PUBLISHED_AT,
        "rule_outcomes": outcomes,
    }
    values.update(overrides)
    return PureFilterResult(**values)


# ----------------------------------------------------------------------
# Outcome structure and projections
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"title": "Analyst", "description": "Hybrid role"},
        {"title": "Marketing Director", "company_blocked": True},
    ],
)
def test_exactly_nine_ordered_outcomes_always_exist(kwargs) -> None:
    result = evaluate(**kwargs)
    assert len(result.rule_outcomes) == 9
    assert tuple(item.rule for item in result.rule_outcomes) == tuple(HardFilterRule)
    for item in result.rule_outcomes:
        assert isinstance(item.evidence, HardFilterEvidence)


def test_all_rules_run_without_short_circuiting() -> None:
    result = evaluate(
        title="Analyst Product Marketing",
        description="Requires an active security clearance",
        locations=({"label": "US", "country_code": "CA", "workplace_type": "remote"},),
        source_published_at=AS_OF - timedelta(days=46),
        action_state=(fact("skipped"),),
        company_blocked=True,
    )
    assert result.rejection_reasons == (
        HardFilterReason.TITLE_NO_MATCH,
        HardFilterReason.SENIORITY_LOW,
        HardFilterReason.WRONG_DISCIPLINE,
        HardFilterReason.GEO_EXCLUDED,
        HardFilterReason.CLEARANCE_REQUIRED,
        HardFilterReason.STALE,
        HardFilterReason.ALREADY_ACTIONED,
        HardFilterReason.COMPANY_BLOCKED,
    )
    assert outcome(result, HardFilterRule.NOT_REMOTE).status is RuleStatus.PASS
    assert result.eligible is False


def test_unknowns_accumulate_in_canonical_order_and_remain_eligible() -> None:
    result = evaluate()
    assert result.unknowns == (
        HardFilterUnknown.REMOTE_ARRANGEMENT_UNRESOLVED,
        HardFilterUnknown.REMOTE_GEOGRAPHY_UNRESOLVED,
        HardFilterUnknown.CLEARANCE_NOT_STATED,
    )
    assert result.eligible is True
    assert result.rejection_reasons == ()
    assert outcome(result, HardFilterRule.NOT_REMOTE).status is RuleStatus.UNKNOWN
    assert outcome(result, HardFilterRule.GEO_EXCLUDED).status is RuleStatus.UNKNOWN


# ----------------------------------------------------------------------
# Title and seniority
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "Marketing Director",
        "PM",
        "Product Marketing Manager",
        "Director of Engineering",
    ],
)
def test_title_family_requires_an_approved_sequence(title: str) -> None:
    result = evaluate(title=title, description="Product Manager")
    assert outcome(result, HardFilterRule.TITLE_NO_MATCH).status is RuleStatus.REJECT
    assert (
        outcome(result, HardFilterRule.TITLE_NO_MATCH).evidence
        is HardFilterEvidence.ALLOWED_TITLE_ABSENT
    )


def test_title_family_never_accepts_description_evidence() -> None:
    result = evaluate(title="Marketing Director", description="Product Manager")
    assert outcome(result, HardFilterRule.TITLE_NO_MATCH).status is RuleStatus.REJECT


@pytest.mark.parametrize("token", ["intern", "junior", "associate", "coordinator", "analyst"])
def test_seniority_full_tokens_reject(token: str) -> None:
    result = evaluate(title=f"{token} Product Manager")
    assert outcome(result, HardFilterRule.SENIORITY_LOW).status is RuleStatus.REJECT
    assert (
        outcome(result, HardFilterRule.SENIORITY_LOW).evidence
        is HardFilterEvidence.EXCLUDED_SENIORITY_MATCH
    )


def test_seniority_never_matches_partial_tokens() -> None:
    result = evaluate(title="Internship Program Manager")
    assert outcome(result, HardFilterRule.SENIORITY_LOW).status is RuleStatus.PASS
    assert (
        outcome(result, HardFilterRule.SENIORITY_LOW).evidence
        is HardFilterEvidence.EXCLUDED_SENIORITY_ABSENT
    )


# ----------------------------------------------------------------------
# Discipline
# ----------------------------------------------------------------------


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
    result = evaluate(title=title, description="partner with product design")
    assert (
        outcome(result, HardFilterRule.WRONG_DISCIPLINE).evidence
        is HardFilterEvidence.EXCLUDED_DISCIPLINE_MATCH
    )


@pytest.mark.parametrize(
    "description",
    [
        "partner with product design",
        "work closely with product marketing",
        "collaborate with sales engineers",
    ],
)
def test_description_collaboration_wording_does_not_reject(description: str) -> None:
    result = evaluate(title="Product Manager", description=description)
    assert outcome(result, HardFilterRule.WRONG_DISCIPLINE).status is RuleStatus.PASS
    assert (
        outcome(result, HardFilterRule.WRONG_DISCIPLINE).evidence
        is HardFilterEvidence.EXCLUDED_DISCIPLINE_ABSENT
    )


def test_compound_branch_matches_within_one_title_sentence() -> None:
    result = evaluate(title="Product Owner, Business Analyst", description=None)
    assert outcome(result, HardFilterRule.WRONG_DISCIPLINE).status is RuleStatus.REJECT


def test_compound_branch_never_crosses_fields() -> None:
    result = evaluate(title="Product owner", description="Business analyst")
    assert outcome(result, HardFilterRule.WRONG_DISCIPLINE).status is RuleStatus.PASS
    result = evaluate(title="Business analyst", description="Product owner")
    assert outcome(result, HardFilterRule.WRONG_DISCIPLINE).status is RuleStatus.PASS


def test_compound_branch_never_uses_location_labels() -> None:
    result = evaluate(
        title="Product Manager",
        description=None,
        locations=({"label": "Product owner, business analyst"},),
    )
    assert outcome(result, HardFilterRule.WRONG_DISCIPLINE).status is RuleStatus.PASS


@pytest.mark.parametrize(
    "description",
    [
        "Bare BA responsibilities are not a discipline signal",
        "Gather requirements, own the backlog, and facilitate agile ceremonies",
    ],
)
def test_bare_ba_and_generic_language_do_not_trigger_compound(description: str) -> None:
    result = evaluate(title="Product Manager", description=description)
    assert outcome(result, HardFilterRule.WRONG_DISCIPLINE).status is RuleStatus.PASS


def test_unavailable_description_contributes_nothing_and_emits_no_unknown() -> None:
    result = evaluate(title="Product Manager", description=None)
    assert outcome(result, HardFilterRule.WRONG_DISCIPLINE).status is RuleStatus.PASS
    assert (
        outcome(result, HardFilterRule.WRONG_DISCIPLINE).evidence
        is HardFilterEvidence.EXCLUDED_DISCIPLINE_ABSENT
    )
    assert not any("discipline" in item.value for item in result.unknowns)


# ----------------------------------------------------------------------
# Remote arrangement
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "description",
    [
        "Employees are expected to work in the office three days per week",
        "Must be onsite 2 days a week",
        "Must be onsite two days a week",
        "Expected to work from the office three days per week",
        "Employees must be in the office three days per week",
        (
            "Employees must work from their home office three days per week "
            "and be in the office one day per week"
        ),
        "Hybrid role",
        "Must work from the office",
        "Required to report onsite",
        "Must work on site",
        "This role is not remote",
        "This position is not remote",
        "This job is not remote",
        "Not a remote role",
        "Not a remote position",
        "Not a remote job",
        "No remote option",
        "No remote work option",
        "No work from home option",
        "Remote work is not offered",
        "Work from home is not offered",
        "Employees cannot work remotely",
        "Employees cannot work from home",
        "Employees can not work remotely",
        "Employees can not work from home",
    ],
)
def test_remote_rejection_examples(description: str) -> None:
    result = evaluate(title="Product Manager", description=description)
    assert (
        outcome(result, HardFilterRule.NOT_REMOTE).evidence
        is HardFilterEvidence.ATTENDANCE_REQUIRED
    )


@pytest.mark.parametrize(
    "description",
    [
        "Expected at the office days 3 per week",
        "Expected at the office days of 3 per week",
    ],
)
def test_weekly_attendance_number_may_follow_the_day_token(description: str) -> None:
    result = evaluate(title="Product Manager", description=description)
    current = outcome(result, HardFilterRule.NOT_REMOTE)
    assert current.rule is HardFilterRule.NOT_REMOTE
    assert current.status is RuleStatus.REJECT
    assert current.evidence is HardFilterEvidence.ATTENDANCE_REQUIRED


@pytest.mark.parametrize(
    "description",
    [
        "Must work from a location within the United States",
        "Must work from any approved location",
        "Must work from a quiet home office",
        "Must work from your home office",
        "Employees must work from their home office three days per week",
        "30 days to set up your home office",
        "The office is open three days per week",
        "Onsite experience is helpful",
        "Customer on site documentation",
        "Work with us in office technology",
        "You will work with the New York office three days per week",
        "Bare office and workplace wording does not reject",
    ],
)
def test_remote_non_rejection_examples(description: str) -> None:
    result = evaluate(title="Product Manager", description=description)
    assert outcome(result, HardFilterRule.NOT_REMOTE).status is not RuleStatus.REJECT


def test_weekly_template_with_positive_remote_still_passes() -> None:
    result = evaluate(
        title="Product Manager",
        description=(
            "Team members typically work from the office two days a week, "
            "but this role is fully remote"
        ),
    )
    assert (
        outcome(result, HardFilterRule.NOT_REMOTE).evidence is HardFilterEvidence.REMOTE_PERMITTED
    )


@pytest.mark.parametrize(
    "description",
    [
        "Not a remote role",
        "Employees cannot work remotely",
        "No work from home option",
    ],
)
def test_denial_span_suppresses_contained_positive_sequences(description: str) -> None:
    result = evaluate(title="Product Manager", description=description)
    assert (
        outcome(result, HardFilterRule.NOT_REMOTE).evidence
        is HardFilterEvidence.ATTENDANCE_REQUIRED
    )


def test_denial_plus_separate_positive_evidence_is_unknown() -> None:
    result = evaluate(
        title="Product Manager",
        description="This role is not remote. A different division offers a fully remote role",
    )
    assert (
        outcome(result, HardFilterRule.NOT_REMOTE).evidence
        is HardFilterEvidence.REMOTE_ARRANGEMENT_UNRESOLVED
    )


def test_structured_hybrid_beats_positive_text() -> None:
    result = evaluate(
        title="Product Manager",
        description="Fully remote",
        locations=({"label": "HQ", "workplace_type": "hybrid"},),
    )
    assert (
        outcome(result, HardFilterRule.NOT_REMOTE).evidence
        is HardFilterEvidence.ATTENDANCE_REQUIRED
    )


def test_weekly_template_beats_positive_text() -> None:
    result = evaluate(
        title="Product Manager",
        description="Employees must be in the office three days per week. Fully remote",
    )
    assert (
        outcome(result, HardFilterRule.NOT_REMOTE).evidence
        is HardFilterEvidence.ATTENDANCE_REQUIRED
    )


def test_positive_remote_alone_passes() -> None:
    result = evaluate(title="Product Manager", description="Fully remote")
    assert (
        outcome(result, HardFilterRule.NOT_REMOTE).evidence is HardFilterEvidence.REMOTE_PERMITTED
    )


def test_unresolved_remote_emits_the_approved_unknown() -> None:
    result = evaluate(title="Product Manager", description="We value flexible ways of working")
    assert (
        outcome(result, HardFilterRule.NOT_REMOTE).evidence
        is HardFilterEvidence.REMOTE_ARRANGEMENT_UNRESOLVED
    )


# ----------------------------------------------------------------------
# Geography
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "workplace_type, expected_remote, expected_geo, expected_evidence",
    [
        ("remote", RuleStatus.PASS, RuleStatus.REJECT, HardFilterEvidence.REMOTE_SCOPE_EXCLUDES_US),
        (
            "on-site",
            RuleStatus.REJECT,
            RuleStatus.UNKNOWN,
            HardFilterEvidence.REMOTE_GEOGRAPHY_UNRESOLVED,
        ),
        (
            "hybrid",
            RuleStatus.REJECT,
            RuleStatus.UNKNOWN,
            HardFilterEvidence.REMOTE_GEOGRAPHY_UNRESOLVED,
        ),
        (
            None,
            RuleStatus.UNKNOWN,
            RuleStatus.UNKNOWN,
            HardFilterEvidence.REMOTE_GEOGRAPHY_UNRESOLVED,
        ),
        (
            "unspecified",
            RuleStatus.UNKNOWN,
            RuleStatus.UNKNOWN,
            HardFilterEvidence.REMOTE_GEOGRAPHY_UNRESOLVED,
        ),
    ],
)
def test_geography_rejection_is_gated_exclusively_by_remote_permitted(
    workplace_type, expected_remote, expected_geo, expected_evidence
) -> None:
    location = {"label": "HQ", "country_code": "CA"}
    if workplace_type is not None:
        location["workplace_type"] = workplace_type
    result = evaluate(title="Product Manager", description=None, locations=(location,))
    assert outcome(result, HardFilterRule.NOT_REMOTE).status is expected_remote
    assert outcome(result, HardFilterRule.GEO_EXCLUDED).status is expected_geo
    assert outcome(result, HardFilterRule.GEO_EXCLUDED).evidence is expected_evidence


def test_geography_can_pass_while_remote_rejects() -> None:
    result = evaluate(
        title="Product Manager",
        description="Must work from the office. Remote in the US",
    )
    assert outcome(result, HardFilterRule.NOT_REMOTE).status is RuleStatus.REJECT
    assert outcome(result, HardFilterRule.GEO_EXCLUDED).status is RuleStatus.PASS
    assert (
        outcome(result, HardFilterRule.GEO_EXCLUDED).evidence is HardFilterEvidence.US_NOT_EXCLUDED
    )


@pytest.mark.parametrize(
    "description",
    [
        "Join us in building the platform",
        "Work in a collaborative environment",
        "Choose X or Y",
        "Contact me for details",
        "Join our global company",
        "Collaborate with teams worldwide",
        "Serve global customers",
        "Grow in global markets",
        "Collaborate with our team in Canada",
        "Support customers in India",
        "Our company is based in the United Kingdom",
    ],
)
def test_incidental_wording_contributes_no_geography_evidence(description: str) -> None:
    result = evaluate(
        title="Product Manager",
        description=description,
        locations=({"label": "Remote", "workplace_type": "remote"},),
    )
    assert outcome(result, HardFilterRule.GEO_EXCLUDED).status is RuleStatus.UNKNOWN
    assert (
        outcome(result, HardFilterRule.GEO_EXCLUDED).evidence
        is HardFilterEvidence.REMOTE_GEOGRAPHY_UNRESOLVED
    )


@pytest.mark.parametrize("label", ["CA", "us", "ME", "OR", "hi", "wa"])
def test_free_text_two_letter_tokens_are_never_codes(label: str) -> None:
    result = evaluate(
        title="Product Manager",
        description=None,
        locations=({"label": label, "workplace_type": "remote"},),
    )
    assert outcome(result, HardFilterRule.GEO_EXCLUDED).status is RuleStatus.UNKNOWN


@pytest.mark.parametrize(
    "code, expected, expected_evidence",
    [
        ("US", RuleStatus.PASS, HardFilterEvidence.US_NOT_EXCLUDED),
        ("us", RuleStatus.PASS, HardFilterEvidence.US_NOT_EXCLUDED),
        ("IN", RuleStatus.REJECT, HardFilterEvidence.REMOTE_SCOPE_EXCLUDES_US),
        ("CA", RuleStatus.REJECT, HardFilterEvidence.REMOTE_SCOPE_EXCLUDES_US),
        ("ZZ", RuleStatus.UNKNOWN, HardFilterEvidence.REMOTE_GEOGRAPHY_UNRESOLVED),
        ("XX", RuleStatus.UNKNOWN, HardFilterEvidence.REMOTE_GEOGRAPHY_UNRESOLVED),
        ("", RuleStatus.UNKNOWN, HardFilterEvidence.REMOTE_GEOGRAPHY_UNRESOLVED),
        ("us-", RuleStatus.UNKNOWN, HardFilterEvidence.REMOTE_GEOGRAPHY_UNRESOLVED),
    ],
)
def test_structured_country_codes_follow_the_closed_inventory(
    code: str, expected, expected_evidence
) -> None:
    result = evaluate(
        title="Product Manager",
        description=None,
        locations=({"label": "HQ", "country_code": code, "workplace_type": "remote"},),
    )
    assert outcome(result, HardFilterRule.GEO_EXCLUDED).status is expected
    assert outcome(result, HardFilterRule.GEO_EXCLUDED).evidence is expected_evidence


@pytest.mark.parametrize(
    "description",
    [
        "Remote in the US",
        "Available in the US",
        "Open to candidates in the US",
        "Within the US",
        "US only",
    ],
)
def test_explicit_us_inclusion_templates_pass(description: str) -> None:
    result = evaluate(
        title="Product Manager",
        description=description,
        locations=({"label": "Remote", "workplace_type": "remote"},),
    )
    assert (
        outcome(result, HardFilterRule.GEO_EXCLUDED).evidence is HardFilterEvidence.US_NOT_EXCLUDED
    )


def test_country_null_remote_with_exclusion_body_rejects() -> None:
    result = evaluate(
        title="Product Manager",
        description="US-based; not available to US applicants",
        locations=({"label": "US", "country_code": None, "workplace_type": "remote"},),
    )
    assert (
        outcome(result, HardFilterRule.GEO_EXCLUDED).evidence
        is HardFilterEvidence.REMOTE_SCOPE_EXCLUDES_US
    )


@pytest.mark.parametrize(
    "description",
    [
        "Remote worldwide, excluding us from consideration",
        "Remote in the US and Canada",
    ],
)
def test_geography_pass_examples(description: str) -> None:
    result = evaluate(title="Product Manager", description=description)
    assert (
        outcome(result, HardFilterRule.GEO_EXCLUDED).evidence is HardFilterEvidence.US_NOT_EXCLUDED
    )


@pytest.mark.parametrize(
    "description",
    [
        "Remote across Europe and Canada",
        "Remote worldwide, except the United States",
    ],
)
def test_geography_rejection_examples(description: str) -> None:
    result = evaluate(title="Product Manager", description=description)
    assert (
        outcome(result, HardFilterRule.GEO_EXCLUDED).evidence
        is HardFilterEvidence.REMOTE_SCOPE_EXCLUDES_US
    )


def test_remote_in_us_but_not_available_to_us_applicants_is_unknown() -> None:
    result = evaluate(
        title="Product Manager",
        description="Remote in the US, but not available to US applicants",
    )
    assert (
        outcome(result, HardFilterRule.GEO_EXCLUDED).evidence
        is HardFilterEvidence.REMOTE_GEOGRAPHY_UNRESOLVED
    )


def test_excluding_alaska_is_not_a_country_rejection() -> None:
    result = evaluate(title="Product Manager", description="Remote, excluding Alaska")
    assert outcome(result, HardFilterRule.GEO_EXCLUDED).status is RuleStatus.UNKNOWN


def test_structured_remote_us_plus_body_exclusion_is_unknown() -> None:
    result = evaluate(
        title="Product Manager",
        description="Excluding the United States",
        locations=({"label": "HQ", "country_code": "US", "workplace_type": "remote"},),
    )
    assert (
        outcome(result, HardFilterRule.GEO_EXCLUDED).evidence
        is HardFilterEvidence.REMOTE_GEOGRAPHY_UNRESOLVED
    )


def test_broad_scope_never_crosses_a_sentence_boundary() -> None:
    result = evaluate(
        title="Product Manager",
        description="Remote. Worldwide",
        locations=({"label": "Remote", "workplace_type": "remote"},),
    )
    assert outcome(result, HardFilterRule.GEO_EXCLUDED).status is RuleStatus.UNKNOWN


def test_named_scope_never_crosses_a_sentence_boundary() -> None:
    result = evaluate(
        title="Product Manager",
        description="Open to candidates in. Europe",
        locations=({"label": "Remote", "workplace_type": "remote"},),
    )
    assert outcome(result, HardFilterRule.GEO_EXCLUDED).status is RuleStatus.UNKNOWN


def test_location_label_is_one_complete_field_occurrence() -> None:
    result = evaluate(
        title="Product Manager",
        description=None,
        locations=({"label": "Remote in the US", "workplace_type": "remote"},),
    )
    assert (
        outcome(result, HardFilterRule.GEO_EXCLUDED).evidence is HardFilterEvidence.US_NOT_EXCLUDED
    )
    result = evaluate(
        title="Product Manager",
        description=None,
        locations=({"label": "Remote in", "workplace_type": "remote"}, {"label": "the US"}),
    )
    assert outcome(result, HardFilterRule.GEO_EXCLUDED).status is RuleStatus.UNKNOWN


# ----------------------------------------------------------------------
# Clearance
# ----------------------------------------------------------------------


@pytest.mark.parametrize("phrase", CLEARANCE_PHRASES)
@pytest.mark.parametrize("trigger", CLEARANCE_OBTAIN_PREFIXES)
@pytest.mark.parametrize("determiner", ["", "a ", "an ", "the "])
def test_obtainability_covers_every_phrase_and_adjacency(
    determiner: str, trigger: str, phrase: str
) -> None:
    result = evaluate(title="Product Manager", description=f"{trigger} {determiner}{phrase}")
    assert (
        outcome(result, HardFilterRule.CLEARANCE_REQUIRED).evidence
        is HardFilterEvidence.ACTIVE_CLEARANCE_NOT_REQUIRED
    )


@pytest.mark.parametrize(
    "description",
    [
        "Must possess active security clearance",
        "Must possess an active security clearance",
        "Must hold the current secret clearance",
        "Must have active top secret clearance",
        "Requires active security clearance",
        "This role requires a current secret clearance",
        "Required to possess current security clearance",
        "Required to hold an active top secret clearance",
        "Required to have the current ts sci clearance",
        "A current ts sci clearance is required",
    ],
)
def test_clearance_obligation_templates_reject(description: str) -> None:
    result = evaluate(title="Product Manager", description=description)
    assert (
        outcome(result, HardFilterRule.CLEARANCE_REQUIRED).evidence
        is HardFilterEvidence.ACTIVE_CLEARANCE_REQUIRED
    )


@pytest.mark.parametrize(
    "description",
    [
        "An active security clearance is preferred",
        "Current secret clearance is preferred",
        "No active security clearance is required",
        "Active security clearance is not required",
        "Does not require active security clearance",
        "Does not require an active security clearance",
        "Current ts sci clearance is not required",
    ],
)
def test_clearance_suppression_templates_pass(description: str) -> None:
    result = evaluate(title="Product Manager", description=description)
    assert (
        outcome(result, HardFilterRule.CLEARANCE_REQUIRED).evidence
        is HardFilterEvidence.ACTIVE_CLEARANCE_NOT_REQUIRED
    )


@pytest.mark.parametrize(
    "description",
    [
        "Requires strong communication skills and a current secret clearance",
        "Requires possession of active security clearance",
        "Does not require prior possession of active security clearance",
        "Active security clearance would be strongly preferred",
        "Ability to obtain within six months active security clearance",
        "Active security clearance",
    ],
)
def test_clearance_without_a_closed_template_is_not_stated(description: str) -> None:
    result = evaluate(title="Product Manager", description=description)
    assert (
        outcome(result, HardFilterRule.CLEARANCE_REQUIRED).evidence
        is HardFilterEvidence.CLEARANCE_NOT_STATED
    )


def test_clearance_independent_conflicting_evidence_is_ambiguous() -> None:
    result = evaluate(
        title="Product Manager",
        description=(
            "Must possess an active security clearance. "
            "No active security clearance is required for this role."
        ),
    )
    assert (
        outcome(result, HardFilterRule.CLEARANCE_REQUIRED).evidence
        is HardFilterEvidence.CLEARANCE_AMBIGUOUS
    )
    assert HardFilterReason.CLEARANCE_REQUIRED not in result.rejection_reasons


# ----------------------------------------------------------------------
# Staleness
# ----------------------------------------------------------------------


def test_staleness_exactly_45_days_passes() -> None:
    result = evaluate(source_published_at=AS_OF - timedelta(days=45))
    assert outcome(result, HardFilterRule.STALE).status is RuleStatus.PASS
    assert outcome(result, HardFilterRule.STALE).evidence is HardFilterEvidence.WITHIN_45_DAYS


def test_staleness_strictly_older_rejects_and_records_source() -> None:
    result = evaluate(source_published_at=AS_OF - timedelta(days=45) - timedelta(seconds=1))
    assert outcome(result, HardFilterRule.STALE).evidence is HardFilterEvidence.OLDER_THAN_45_DAYS
    assert result.staleness_source is StalenessSource.SOURCE_PUBLISHED_AT


def test_staleness_prefers_source_published_at() -> None:
    result = evaluate(
        source_published_at=AS_OF - timedelta(days=46),
        first_seen_at=AS_OF,
    )
    assert outcome(result, HardFilterRule.STALE).status is RuleStatus.REJECT
    assert result.staleness_source is StalenessSource.SOURCE_PUBLISHED_AT


def test_staleness_falls_back_to_first_seen_at() -> None:
    result = evaluate(
        source_published_at=None,
        first_seen_at=AS_OF - timedelta(days=46),
    )
    assert outcome(result, HardFilterRule.STALE).status is RuleStatus.REJECT
    assert result.staleness_source is StalenessSource.FIRST_SEEN_AT


def test_future_selected_timestamp_is_typed_invalid_input() -> None:
    with pytest.raises(FilterPersistedInputError) as caught:
        evaluate(source_published_at=AS_OF + timedelta(days=1))
    assert caught.value.code is FilterErrorCode.PERSISTED_INPUT_INVALID
    with pytest.raises(FilterPersistedInputError) as caught:
        evaluate(source_published_at=AS_OF + timedelta(seconds=1), first_seen_at=AS_OF)
    assert caught.value.code is FilterErrorCode.PERSISTED_INPUT_INVALID


# ----------------------------------------------------------------------
# Already actioned
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "state, evidence",
    [
        ("skipped", HardFilterEvidence.PRIOR_SKIPPED),
        ("applied", HardFilterEvidence.PRIOR_APPLIED),
    ],
)
def test_qualifying_action_states_reject(state: str, evidence: HardFilterEvidence) -> None:
    result = evaluate(title="Product Manager", description=None, action_state=(fact(state),))
    assert outcome(result, HardFilterRule.ALREADY_ACTIONED).evidence is evidence


def test_expired_current_version_rejects() -> None:
    result = evaluate(
        title="Product Manager",
        description=None,
        action_state=(fact("expired", score_posting_version_id=2),),
    )
    assert (
        outcome(result, HardFilterRule.ALREADY_ACTIONED).evidence
        is HardFilterEvidence.CURRENT_VERSION_EXPIRED
    )


def test_expired_older_version_does_not_reject() -> None:
    result = evaluate(
        title="Product Manager",
        description=None,
        action_state=(fact("expired", score_posting_version_id=1),),
    )
    assert (
        outcome(result, HardFilterRule.ALREADY_ACTIONED).evidence
        is HardFilterEvidence.NO_QUALIFYING_ACTION
    )


@pytest.mark.parametrize("state", ["restored", "recommended"])
def test_non_qualifying_action_states_do_not_reject(state: str) -> None:
    result = evaluate(title="Product Manager", description=None, action_state=(fact(state),))
    assert (
        outcome(result, HardFilterRule.ALREADY_ACTIONED).evidence
        is HardFilterEvidence.NO_QUALIFYING_ACTION
    )


def test_action_evidence_precedence_is_skipped_then_applied_then_expired() -> None:
    result = evaluate(
        title="Product Manager",
        description=None,
        action_state=(fact("expired"), fact("applied"), fact("skipped")),
    )
    assert (
        outcome(result, HardFilterRule.ALREADY_ACTIONED).evidence
        is HardFilterEvidence.PRIOR_SKIPPED
    )
    result = evaluate(
        title="Product Manager",
        description=None,
        action_state=(fact("expired"), fact("applied")),
    )
    assert (
        outcome(result, HardFilterRule.ALREADY_ACTIONED).evidence
        is HardFilterEvidence.PRIOR_APPLIED
    )


# ----------------------------------------------------------------------
# Company blocked
# ----------------------------------------------------------------------


def test_company_blocked_true_rejects_and_false_passes() -> None:
    result = evaluate(title="Product Manager", description=None, company_blocked=True)
    assert (
        outcome(result, HardFilterRule.COMPANY_BLOCKED).evidence
        is HardFilterEvidence.COMPANY_BLOCKED
    )
    result = evaluate(title="Product Manager", description=None, company_blocked=False)
    assert (
        outcome(result, HardFilterRule.COMPANY_BLOCKED).evidence
        is HardFilterEvidence.COMPANY_NOT_BLOCKED
    )


# ----------------------------------------------------------------------
# Result validation
# ----------------------------------------------------------------------


def test_result_validation_accepts_every_produced_result() -> None:
    validate_pure_result(evaluate(title="Product Manager", description="Remote in the US"))
    validate_pure_result(evaluate(title="Analyst Product Marketing", company_blocked=True))


def test_result_validation_rejects_wrong_rule_order_and_count() -> None:
    result = evaluate()
    with pytest.raises(ValueError):
        validate_pure_result(_result_with_outcomes(tuple(reversed(result.rule_outcomes))))
    with pytest.raises(ValueError):
        validate_pure_result(_result_with_outcomes(result.rule_outcomes[:8]))
    with pytest.raises(ValueError):
        validate_pure_result(_result_with_outcomes(result.rule_outcomes[:1] + result.rule_outcomes))


def test_result_validation_rejects_illegal_triples() -> None:
    result = evaluate()
    outcomes = list(result.rule_outcomes)
    outcomes[0] = HardFilterRuleOutcome(
        HardFilterRule.TITLE_NO_MATCH, RuleStatus.PASS, HardFilterEvidence.REMOTE_PERMITTED
    )
    with pytest.raises(ValueError):
        validate_pure_result(_result_with_outcomes(tuple(outcomes)))
    outcomes = list(result.rule_outcomes)
    outcomes[0] = HardFilterRuleOutcome(
        HardFilterRule.TITLE_NO_MATCH, RuleStatus.UNKNOWN, HardFilterEvidence.ALLOWED_TITLE_ABSENT
    )
    with pytest.raises(ValueError):
        validate_pure_result(_result_with_outcomes(tuple(outcomes)))


def test_result_validation_rejects_projection_disagreement() -> None:
    result = evaluate()
    with pytest.raises(ValueError):
        validate_pure_result(
            _result_with_outcomes(result.rule_outcomes, rejection_reasons=(HardFilterReason.STALE,))
        )
    with pytest.raises(ValueError):
        validate_pure_result(_result_with_outcomes(result.rule_outcomes, unknowns=()))
    with pytest.raises(ValueError):
        validate_pure_result(_result_with_outcomes(result.rule_outcomes, eligible=False))


def test_result_validation_rejects_legacy_and_arbitrary_values() -> None:
    result = evaluate()
    with pytest.raises(ValueError):
        validate_pure_result(
            _result_with_outcomes(result.rule_outcomes, unknowns=("discipline_unknown",))
        )
    outcomes = list(result.rule_outcomes)
    outcomes[0] = HardFilterRuleOutcome(
        HardFilterRule.TITLE_NO_MATCH, "pass", HardFilterEvidence.ALLOWED_TITLE_MATCH
    )
    with pytest.raises(ValueError):
        validate_pure_result(_result_with_outcomes(tuple(outcomes)))
    with pytest.raises(ValueError):
        validate_pure_result(object())  # type: ignore[arg-type]


# ----------------------------------------------------------------------
# Determinism, external access, and privacy
# ----------------------------------------------------------------------


def test_evaluation_is_deterministic_across_repeat_runs() -> None:
    inputs = dict(
        title="Director Product Manager Technical Platform",
        description="Remote worldwide, except the United States",
        source_published_at=AS_OF - timedelta(days=46),
    )
    assert evaluate(**inputs) == evaluate(**inputs)


def test_staleness_depends_only_on_the_supplied_clock() -> None:
    result = evaluate(
        source_published_at=AS_OF - timedelta(days=60),
        as_of=AS_OF,
    )
    assert outcome(result, HardFilterRule.STALE).status is RuleStatus.REJECT
    result = evaluate(
        source_published_at=AS_OF - timedelta(days=60),
        as_of=AS_OF - timedelta(days=20),
    )
    assert outcome(result, HardFilterRule.STALE).status is RuleStatus.PASS


def test_pure_evaluator_has_no_external_dependencies() -> None:
    source = inspect.getsource(evaluator_module)
    for banned in (
        "import os",
        "import socket",
        "httpx",
        "requests",
        "environ",
        ".now(",
        "open(",
        "sqlalchemy",
    ):
        assert banned not in source


def test_results_and_exceptions_expose_no_content() -> None:
    result = evaluate(
        title="Analyst Product Marketing",
        description="Must work from the office. Requires an active security clearance",
    )
    for text in ("Analyst", "Product", "office", "security", "Must work"):
        assert text not in repr(result)
    with pytest.raises(FilterPersistedInputError) as caught:
        evaluate(
            title="Top Secret Internal Role",
            description="Confidential",
            source_published_at=AS_OF + timedelta(days=1),
        )
    assert str(caught.value) == "filter.persisted_input_invalid"
    assert "Top Secret" not in str(caught.value)
    assert "Confidential" not in str(caught.value)
