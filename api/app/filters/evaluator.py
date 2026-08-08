"""Pure deterministic evaluation for one authoritative posting version."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

from app.filters.normalization import (
    NormalizedField,
    normalize_fields,
    sequence_matches,
    span_contains,
)
from app.filters.policy import (
    ASSIGNED_COUNTRY_CODES,
    ATTENDANCE_MANDATORY,
    ATTENDANCE_ROLE,
    BROAD,
    CLEARANCE_NO_PREFIXES,
    CLEARANCE_OBLIGATION_PREFIXES,
    CLEARANCE_OBTAIN_PREFIXES,
    CLEARANCE_PHRASES,
    DISCIPLINES,
    NAMED_NON_US,
    NAMED_US,
    POLICY_MANIFEST,
    REMOTE_DENIAL,
    REMOTE_POSITIVE,
    SENIORITY,
    TITLE_FAMILIES,
    US_EXCLUSION,
    US_INCLUSION,
)
from app.filters.results import (
    FilterErrorCode,
    FilterEvaluatorDefectError,
    FilterPersistedInputError,
)

_ALLOWED_TRIPLES = frozenset(
    (rule, status, evidence)
    for rule, statuses in POLICY_MANIFEST["allowed_outcomes"].items()
    for status, evidence in statuses.items()
    for evidence in (evidence if isinstance(evidence, list) else [evidence])
)


class HardFilterReason(str, Enum):
    TITLE_NO_MATCH = "title_no_match"
    SENIORITY_LOW = "seniority_low"
    WRONG_DISCIPLINE = "wrong_discipline"
    NOT_REMOTE = "not_remote"
    GEO_EXCLUDED = "geo_excluded"
    CLEARANCE_REQUIRED = "clearance_required"
    STALE = "stale"
    ALREADY_ACTIONED = "already_actioned"
    COMPANY_BLOCKED = "company_blocked"


class HardFilterUnknown(str, Enum):
    REMOTE_ARRANGEMENT_UNRESOLVED = "remote_arrangement_unresolved"
    REMOTE_GEOGRAPHY_UNRESOLVED = "remote_geography_unresolved"
    CLEARANCE_NOT_STATED = "clearance_not_stated"
    CLEARANCE_AMBIGUOUS = "clearance_ambiguous"


class StalenessSource(str, Enum):
    SOURCE_PUBLISHED_AT = "source_published_at"
    FIRST_SEEN_AT = "first_seen_at"


class RuleStatus(str, Enum):
    PASS = "pass"
    REJECT = "reject"
    UNKNOWN = "unknown"


class HardFilterRule(str, Enum):
    TITLE_NO_MATCH = "title_no_match"
    SENIORITY_LOW = "seniority_low"
    WRONG_DISCIPLINE = "wrong_discipline"
    NOT_REMOTE = "not_remote"
    GEO_EXCLUDED = "geo_excluded"
    CLEARANCE_REQUIRED = "clearance_required"
    STALE = "stale"
    ALREADY_ACTIONED = "already_actioned"
    COMPANY_BLOCKED = "company_blocked"


class HardFilterEvidence(str, Enum):
    ALLOWED_TITLE_MATCH = "allowed_title_match"
    ALLOWED_TITLE_ABSENT = "allowed_title_absent"
    EXCLUDED_SENIORITY_ABSENT = "excluded_seniority_absent"
    EXCLUDED_SENIORITY_MATCH = "excluded_seniority_match"
    EXCLUDED_DISCIPLINE_ABSENT = "excluded_discipline_absent"
    EXCLUDED_DISCIPLINE_MATCH = "excluded_discipline_match"
    REMOTE_PERMITTED = "remote_permitted"
    ATTENDANCE_REQUIRED = "attendance_required"
    REMOTE_ARRANGEMENT_UNRESOLVED = "remote_arrangement_unresolved"
    US_NOT_EXCLUDED = "us_not_excluded"
    REMOTE_SCOPE_EXCLUDES_US = "remote_scope_excludes_us"
    REMOTE_GEOGRAPHY_UNRESOLVED = "remote_geography_unresolved"
    ACTIVE_CLEARANCE_NOT_REQUIRED = "active_clearance_not_required"
    ACTIVE_CLEARANCE_REQUIRED = "active_clearance_required"
    CLEARANCE_NOT_STATED = "clearance_not_stated"
    CLEARANCE_AMBIGUOUS = "clearance_ambiguous"
    WITHIN_45_DAYS = "within_45_days"
    OLDER_THAN_45_DAYS = "older_than_45_days"
    NO_QUALIFYING_ACTION = "no_qualifying_action"
    PRIOR_SKIPPED = "prior_skipped"
    PRIOR_APPLIED = "prior_applied"
    CURRENT_VERSION_EXPIRED = "current_version_expired"
    COMPANY_NOT_BLOCKED = "company_not_blocked"
    COMPANY_BLOCKED = "company_blocked"


@dataclass(frozen=True)
class HardFilterRuleOutcome:
    rule: HardFilterRule
    status: RuleStatus
    evidence: HardFilterEvidence


@dataclass(frozen=True)
class ActionFact:
    digest_item_id: int
    posting_id: int
    score_posting_version_id: int
    state: str
    score_id: int = 0


@dataclass(frozen=True)
class PureFilterInput:
    posting_id: int
    posting_version_id: int
    content_hash: str
    title: str
    description_md: str | None
    locations: tuple[dict[str, Any], ...]
    source_published_at: datetime | None
    first_seen_at: datetime
    current_version_id: int
    closed_at: datetime | None
    company_blocked: bool
    action_state: tuple[ActionFact, ...]


@dataclass(frozen=True)
class PureFilterResult:
    eligible: bool
    rejection_reasons: tuple[HardFilterReason, ...]
    unknowns: tuple[HardFilterUnknown, ...]
    staleness_source: StalenessSource
    rule_outcomes: tuple[HardFilterRuleOutcome, ...]


def _tokens(field: NormalizedField) -> tuple[str, ...]:
    return tuple(token.value for token in field.tokens)


def _has_sequence(
    fields: tuple[NormalizedField, ...], phrase: str, *, kinds=None, same_sentence=False
) -> bool:
    return bool(sequence_matches(fields, phrase, field_kinds=kinds, same_sentence=same_sentence))


def _spans(
    fields: tuple[NormalizedField, ...], phrases: tuple[str, ...], *, kinds=None
) -> list[tuple[NormalizedField, int, int]]:
    found: list[tuple[NormalizedField, int, int]] = []
    for phrase in phrases:
        found.extend(sequence_matches(fields, phrase, field_kinds=kinds))
    return found


def _same_sentence_compound(fields: tuple[NormalizedField, ...]) -> bool:
    for field in fields:
        if field.field_kind not in {"title", "description"}:
            continue
        owners: dict[int, set[str]] = {}
        for phrase in ("product owner", "business analyst"):
            for _, start, _ in sequence_matches((field,), phrase, same_sentence=True):
                owners.setdefault(field.tokens[start].sentence_segment, set()).add(phrase)
        for phrases in owners.values():
            if len(phrases) == 2:
                return True
    return False


def _structured_workplace(locations: tuple[dict[str, Any], ...]) -> tuple[bool, bool]:
    positive = False
    mandatory = False
    for location in locations:
        value = location.get("workplace_type")
        if not isinstance(value, str):
            continue
        normalized = value.casefold()
        if normalized == "remote":
            positive = True
        elif normalized in {"hybrid", "on-site"}:
            mandatory = True
    return positive, mandatory


def _weekly_attendance(fields: tuple[NormalizedField, ...]) -> bool:
    numbers = {
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
    }
    obligations = {"required", "must", "expected", "attendance"}
    weekly_phrases = (("per", "week"), ("a", "week"), ("each", "week"), ("weekly",))
    office_phrases = (
        ("office",),
        ("in", "office"),
        ("in", "the", "office"),
        ("onsite",),
        ("on", "site"),
    )
    for field in fields:
        values = _tokens(field)
        for day_index, day in enumerate(values):
            if day not in {"day", "days"}:
                continue
            number_ok = any(
                abs(index - day_index) <= 2
                and values[index] in numbers
                and field.tokens[index].sentence_segment == field.tokens[day_index].sentence_segment
                for index in range(max(0, day_index - 2), min(len(values), day_index + 3))
            )
            if not number_ok:
                continue
            day_segment = field.tokens[day_index].sentence_segment
            week_ok = any(
                any(
                    values[start : start + len(phrase)] == phrase
                    and all(
                        token.sentence_segment == day_segment
                        for token in field.tokens[start : start + len(phrase)]
                    )
                    for start in range(max(0, day_index - 4), min(len(values), day_index + 5))
                )
                for phrase in weekly_phrases
            )
            if not week_ok:
                continue
            office_ok = False
            for phrase in office_phrases:
                for start in range(max(0, day_index - 8), min(len(values), day_index + 9)):
                    if tuple(values[start : start + len(phrase)]) != phrase:
                        continue
                    if (
                        field.tokens[start].sentence_segment
                        != field.tokens[day_index].sentence_segment
                    ):
                        continue
                    if start <= day_index < start + len(phrase):
                        continue
                    if phrase == ("office",) and start > 0 and values[start - 1] == "home":
                        continue
                    if abs(start - day_index) <= 8 or abs(start + len(phrase) - 1 - day_index) <= 8:
                        office_ok = True
            if not office_ok:
                continue
            segment = field.tokens[day_index].sentence_segment
            obligation = any(
                token.value in obligations and token.sentence_segment == segment
                for token in field.tokens
            )
            presence = any(
                any(
                    field.tokens[start].sentence_segment == segment
                    for _, start, _ in sequence_matches((field,), phrase, same_sentence=True)
                )
                for phrase in ("report to the office", "report onsite", "report on site")
            )
            if obligation or presence:
                return True
    return False


def _remote_outcome(
    fields: tuple[NormalizedField, ...], locations: tuple[dict[str, Any], ...]
) -> tuple[RuleStatus, HardFilterEvidence, bool]:
    structured_positive, structured_mandatory = _structured_workplace(locations)
    mandatory = (
        structured_mandatory
        or bool(_spans(fields, ATTENDANCE_ROLE))
        or bool(_spans(fields, ATTENDANCE_MANDATORY))
        or _weekly_attendance(fields)
    )
    if mandatory:
        return RuleStatus.REJECT, HardFilterEvidence.ATTENDANCE_REQUIRED, False
    denial_spans = _spans(fields, REMOTE_DENIAL)
    positives = _spans(fields, REMOTE_POSITIVE)
    scoped_remote = _broad_scope(fields, remote_only=True) or _parse_named_scope(
        fields, remote_only=True
    ) != (False, False)
    independent_positive = structured_positive or (
        bool(positives)
        and any(
            not any(
                field == denial_field
                and span_contains((start, end), (positive_start, positive_end))
                for denial_field, start, end in denial_spans
            )
            for field, positive_start, positive_end in positives
        )
    )
    if denial_spans and independent_positive:
        return RuleStatus.UNKNOWN, HardFilterEvidence.REMOTE_ARRANGEMENT_UNRESOLVED, False
    if denial_spans:
        return RuleStatus.REJECT, HardFilterEvidence.ATTENDANCE_REQUIRED, False
    if structured_positive or positives or scoped_remote:
        return RuleStatus.PASS, HardFilterEvidence.REMOTE_PERMITTED, True
    return RuleStatus.UNKNOWN, HardFilterEvidence.REMOTE_ARRANGEMENT_UNRESOLVED, False


def _broad_scope(fields: tuple[NormalizedField, ...], *, remote_only: bool = False) -> bool:
    prefixes = (
        ("remote",),
        ("remote", "role"),
        ("remote", "position"),
        ("remote", "job"),
        ("this", "role", "is", "remote"),
        ("this", "position", "is", "remote"),
        ("this", "job", "is", "remote"),
        ("open", "to", "candidates"),
        ("open", "to", "applicants"),
        ("available", "to", "candidates"),
        ("available", "to", "applicants"),
        ("candidates", "may", "be", "located"),
        ("candidates", "may", "be", "based"),
        ("candidates", "may", "reside"),
        ("candidates", "may", "apply"),
        ("candidates", "can", "be", "located"),
        ("candidates", "can", "be", "based"),
        ("candidates", "can", "reside"),
        ("candidates", "can", "apply"),
        ("candidates", "must", "be", "located"),
        ("candidates", "must", "be", "based"),
        ("candidates", "must", "reside"),
        ("candidates", "must", "apply"),
        ("applicants", "may", "be", "located"),
        ("applicants", "may", "be", "based"),
        ("applicants", "may", "reside"),
        ("applicants", "may", "apply"),
        ("applicants", "can", "be", "located"),
        ("applicants", "can", "be", "based"),
        ("applicants", "can", "reside"),
        ("applicants", "can", "apply"),
        ("applicants", "must", "be", "located"),
        ("applicants", "must", "be", "based"),
        ("applicants", "must", "reside"),
        ("applicants", "must", "apply"),
    )
    connectors = {"in", "within", "across", "throughout", "from"}
    for field in fields:
        values = _tokens(field)
        for prefix in prefixes:
            if remote_only and prefix[0] != "remote":
                continue
            for start in range(len(values) - len(prefix) + 1):
                if values[start : start + len(prefix)] != prefix:
                    continue
                cursor = start + len(prefix)
                if cursor < len(values) and values[cursor] in connectors:
                    cursor += 1
                for broad in BROAD:
                    broad_tokens = tuple(
                        token.value
                        for token in normalize_fields(title=broad, description=None, locations=())[
                            0
                        ].tokens
                    )
                    if values[cursor : cursor + len(broad_tokens)] == broad_tokens:
                        if (
                            len(
                                {
                                    token.sentence_segment
                                    for token in field.tokens[start : cursor + len(broad_tokens)]
                                }
                            )
                            == 1
                        ):
                            return True
    return False


def _parse_named_scope(
    fields: tuple[NormalizedField, ...], *, remote_only: bool = False
) -> tuple[bool, bool]:
    # Return (US inclusion, non-US-only scope). The parser consumes only a
    # maximal list immediately following one of the three approved prefixes.
    prefixes = (
        ("remote", "in"),
        ("remote", "within"),
        ("remote", "across"),
        ("remote", "throughout"),
        ("open", "to", "candidates", "in"),
        ("open", "to", "candidates", "within"),
        ("open", "to", "applicants", "in"),
        ("open", "to", "applicants", "within"),
        ("available", "to", "candidates", "in"),
        ("available", "to", "candidates", "within"),
        ("available", "to", "applicants", "in"),
        ("available", "to", "applicants", "within"),
        ("candidates", "must", "be", "located", "in"),
        ("candidates", "must", "be", "located", "within"),
        ("candidates", "must", "be", "based", "in"),
        ("candidates", "must", "be", "based", "within"),
        ("candidates", "must", "reside", "in"),
        ("candidates", "must", "reside", "within"),
        ("candidates", "required", "to", "be", "located", "in"),
        ("candidates", "required", "to", "be", "located", "within"),
        ("candidates", "required", "to", "be", "based", "in"),
        ("candidates", "required", "to", "be", "based", "within"),
        ("candidates", "required", "to", "reside", "in"),
        ("candidates", "required", "to", "reside", "within"),
        ("applicants", "must", "be", "located", "in"),
        ("applicants", "must", "be", "located", "within"),
        ("applicants", "must", "be", "based", "in"),
        ("applicants", "must", "be", "based", "within"),
        ("applicants", "must", "reside", "in"),
        ("applicants", "must", "reside", "within"),
        ("applicants", "required", "to", "be", "located", "in"),
        ("applicants", "required", "to", "be", "located", "within"),
        ("applicants", "required", "to", "be", "based", "in"),
        ("applicants", "required", "to", "be", "based", "within"),
        ("applicants", "required", "to", "reside", "in"),
        ("applicants", "required", "to", "reside", "within"),
        ("this", "role", "must", "be", "located", "in"),
        ("this", "role", "must", "be", "located", "within"),
        ("this", "role", "must", "be", "based", "in"),
        ("this", "role", "must", "be", "based", "within"),
        ("this", "role", "must", "reside", "in"),
        ("this", "role", "must", "reside", "within"),
        ("this", "role", "required", "to", "be", "located", "in"),
        ("this", "role", "required", "to", "be", "located", "within"),
        ("this", "role", "required", "to", "be", "based", "in"),
        ("this", "role", "required", "to", "be", "based", "within"),
        ("this", "role", "required", "to", "reside", "in"),
        ("this", "role", "required", "to", "reside", "within"),
    )
    items = [(tuple(item.split()), True) for item in NAMED_US] + [
        (tuple(item.split()), False) for item in NAMED_NON_US
    ]
    inclusion = False
    non_us = False
    for field in fields:
        values = _tokens(field)
        for prefix in prefixes:
            if remote_only and prefix[0] != "remote":
                continue
            for start in range(len(values) - len(prefix) + 1):
                if values[start : start + len(prefix)] != prefix:
                    continue
                prefix_segment = field.tokens[start].sentence_segment
                if any(
                    token.sentence_segment != prefix_segment
                    for token in field.tokens[start : start + len(prefix)]
                ):
                    continue
                cursor = start + len(prefix)
                matched_any = False
                invalid_tail = False
                scope_inclusion = False
                scope_non_us = False
                while cursor < len(values):
                    if cursor > 0 and values[cursor - 1] in {"and", "or"}:
                        if field.gaps[cursor - 1].category != "whitespace":
                            break
                    matched = None
                    for item, is_us in sorted(items, key=lambda item: len(item[0]), reverse=True):
                        if values[cursor : cursor + len(item)] == item:
                            matched = (len(item), is_us)
                            break
                    if matched is None:
                        if matched_any and field.tokens[cursor].sentence_segment == prefix_segment:
                            invalid_tail = True
                        break
                    if field.tokens[cursor].sentence_segment != prefix_segment or any(
                        token.sentence_segment != prefix_segment
                        for token in field.tokens[cursor : cursor + matched[0]]
                    ):
                        break
                    matched_any = True
                    item_length, is_us = matched
                    scope_inclusion |= is_us
                    scope_non_us |= not is_us
                    cursor += item_length
                    if cursor >= len(values):
                        break
                    if (
                        values[cursor] in {"and", "or"}
                        and field.gaps[cursor - 1].category == "whitespace"
                    ):
                        cursor += 1
                    elif cursor < len(values) and field.gaps[cursor - 1].category in {
                        "comma",
                        "semicolon",
                        "slash",
                    }:
                        if cursor < len(values) and values[cursor] in {"and", "or"}:
                            cursor += 1
                    else:
                        if (
                            cursor < len(values)
                            and field.tokens[cursor].sentence_segment == prefix_segment
                        ):
                            invalid_tail = True
                        break
                if matched_any and not invalid_tail:
                    inclusion |= scope_inclusion
                    non_us |= scope_non_us
                    break
    return inclusion, non_us and not inclusion


def _geography_outcome(
    fields: tuple[NormalizedField, ...],
    locations: tuple[dict[str, Any], ...],
    remote_positive: bool,
) -> tuple[RuleStatus, HardFilterEvidence]:
    explicit_us = False
    explicit_exclusion = False
    non_us = False
    for location in locations:
        code = location.get("country_code")
        if isinstance(code, str) and code.casefold().upper() in ASSIGNED_COUNTRY_CODES:
            if code.casefold().upper() == "US":
                explicit_us = True
            else:
                non_us = True
    exclusion_spans = _spans(fields, US_EXCLUSION)
    inclusion_spans = _spans(fields, US_INCLUSION)
    explicit_us |= any(
        not any(
            field == excluded_field and span_contains((excluded_start, excluded_end), (start, end))
            for excluded_field, excluded_start, excluded_end in exclusion_spans
        )
        for field, start, end in inclusion_spans
    )
    explicit_exclusion |= bool(exclusion_spans)
    broad = _broad_scope(fields)
    named_us, named_non_us = _parse_named_scope(fields)
    explicit_us |= named_us
    non_us |= named_non_us
    if explicit_us and (explicit_exclusion or non_us):
        return RuleStatus.UNKNOWN, HardFilterEvidence.REMOTE_GEOGRAPHY_UNRESOLVED
    if explicit_us or (broad and not explicit_exclusion):
        return RuleStatus.PASS, HardFilterEvidence.US_NOT_EXCLUDED
    if explicit_exclusion:
        if remote_positive:
            return RuleStatus.REJECT, HardFilterEvidence.REMOTE_SCOPE_EXCLUDES_US
        return RuleStatus.UNKNOWN, HardFilterEvidence.REMOTE_GEOGRAPHY_UNRESOLVED
    if non_us:
        if remote_positive:
            return RuleStatus.REJECT, HardFilterEvidence.REMOTE_SCOPE_EXCLUDES_US
        return RuleStatus.UNKNOWN, HardFilterEvidence.REMOTE_GEOGRAPHY_UNRESOLVED
    return RuleStatus.UNKNOWN, HardFilterEvidence.REMOTE_GEOGRAPHY_UNRESOLVED


def _clearance_outcome(
    fields: tuple[NormalizedField, ...],
) -> tuple[RuleStatus, HardFilterEvidence]:
    def spans_for_prefixes(prefixes: tuple[str, ...]) -> list[tuple[NormalizedField, int, int]]:
        found = []
        for field in fields:
            values = _tokens(field)
            for prefix in prefixes:
                prefix_tokens = tuple(prefix.split())
                for start in range(len(values) - len(prefix_tokens) + 1):
                    if values[start : start + len(prefix_tokens)] != prefix_tokens:
                        continue
                    cursor = start + len(prefix_tokens)
                    if cursor < len(values) and values[cursor] in {"a", "an", "the"}:
                        cursor += 1
                    for phrase in CLEARANCE_PHRASES:
                        phrase_tokens = tuple(phrase.split())
                        if values[cursor : cursor + len(phrase_tokens)] == phrase_tokens:
                            found.append((field, start, cursor + len(phrase_tokens)))
        return found

    suppressed: list[tuple[NormalizedField, int, int]] = []
    for field in fields:
        values = _tokens(field)
        for phrase in CLEARANCE_PHRASES:
            for _, start, end in sequence_matches((field,), phrase):
                for suffix in (
                    ("preferred",),
                    ("is", "preferred"),
                    ("not", "required"),
                    ("is", "not", "required"),
                ):
                    if values[end : end + len(suffix)] == suffix:
                        suppressed.append((field, start, end + len(suffix)))
                if start and values[start - 1] == "no":
                    if values[end : end + 1] == ("required",):
                        suppressed.append((field, start - 1, end + 1))
                    elif values[end : end + 2] == ("is", "required"):
                        suppressed.append((field, start - 1, end + 2))
                for prefix in ("does not require",):
                    prefix_tokens = tuple(prefix.split())
                    if (
                        start >= len(prefix_tokens)
                        and values[start - len(prefix_tokens) : start] == prefix_tokens
                    ):
                        suppressed.append((field, start - len(prefix_tokens), end))
    for field in fields:
        values = _tokens(field)
        for prefix in CLEARANCE_NO_PREFIXES:
            prefix_tokens = tuple(prefix.split())
            for start in range(len(values) - len(prefix_tokens) + 1):
                if values[start : start + len(prefix_tokens)] != prefix_tokens:
                    continue
                cursor = start + len(prefix_tokens)
                if cursor < len(values) and values[cursor] in {"a", "an", "the"}:
                    cursor += 1
                for phrase in CLEARANCE_PHRASES:
                    phrase_tokens = tuple(phrase.split())
                    if values[cursor : cursor + len(phrase_tokens)] != phrase_tokens:
                        continue
                    end = cursor + len(phrase_tokens)
                    if prefix == "does not require":
                        suppressed.append((field, start, end))
                        continue
                    for suffix in (("required",), ("is", "required")):
                        if values[end : end + len(suffix)] == suffix:
                            suppressed.append((field, start, end + len(suffix)))
    obtainability = spans_for_prefixes(CLEARANCE_OBTAIN_PREFIXES)
    suppressed.extend(obtainability)
    actual = spans_for_prefixes(CLEARANCE_OBLIGATION_PREFIXES)
    actual = [
        item
        for item in actual
        if not any(
            item[0] == sup[0] and span_contains((sup[1], sup[2]), (item[1], item[2]))
            for sup in suppressed
        )
    ]
    # The phrase-first obligation form is a fixed adjacent suffix.
    for field in fields:
        values = _tokens(field)
        for phrase in CLEARANCE_PHRASES:
            for _, start, end in sequence_matches((field,), phrase):
                for suffix in (("is", "required"), ("required",)):
                    if values[end : end + len(suffix)] == suffix:
                        actual.append((field, start, end + len(suffix)))
    actual = [
        item
        for item in actual
        if not any(
            item[0] == sup[0] and span_contains((sup[1], sup[2]), (item[1], item[2]))
            for sup in suppressed
        )
    ]
    suppressed_or_other = bool(suppressed)
    if actual and suppressed_or_other:
        return RuleStatus.UNKNOWN, HardFilterEvidence.CLEARANCE_AMBIGUOUS
    if actual:
        return RuleStatus.REJECT, HardFilterEvidence.ACTIVE_CLEARANCE_REQUIRED
    if suppressed or spans_for_prefixes(CLEARANCE_OBTAIN_PREFIXES):
        return RuleStatus.PASS, HardFilterEvidence.ACTIVE_CLEARANCE_NOT_REQUIRED
    return RuleStatus.UNKNOWN, HardFilterEvidence.CLEARANCE_NOT_STATED


def evaluate_pure(inputs: PureFilterInput, *, evaluation_as_of: datetime) -> PureFilterResult:
    """Evaluate a previously validated immutable/mutable input snapshot."""

    fields = normalize_fields(
        title=inputs.title,
        description=inputs.description_md,
        locations=inputs.locations,
    )
    title_fields = tuple(field for field in fields if field.field_kind == "title")
    title_match = any(_has_sequence(title_fields, phrase) for phrase in TITLE_FAMILIES)
    seniority_match = any(_has_sequence(title_fields, phrase) for phrase in SENIORITY)
    discipline_match = any(
        _has_sequence(title_fields, phrase) for phrase in DISCIPLINES
    ) or _same_sentence_compound(fields)
    remote_status, remote_evidence, remote_positive = _remote_outcome(fields, inputs.locations)
    geo_status, geo_evidence = _geography_outcome(fields, inputs.locations, remote_positive)
    clearance_status, clearance_evidence = _clearance_outcome(fields)

    selected = inputs.source_published_at or inputs.first_seen_at
    source = (
        StalenessSource.SOURCE_PUBLISHED_AT
        if inputs.source_published_at
        else StalenessSource.FIRST_SEEN_AT
    )
    if selected > evaluation_as_of:
        raise FilterPersistedInputError(FilterErrorCode.PERSISTED_INPUT_INVALID)
    stale = selected < evaluation_as_of - timedelta(days=45)
    action_evidence: HardFilterEvidence | None = None
    if any(f.state == "skipped" for f in inputs.action_state):
        action_evidence = HardFilterEvidence.PRIOR_SKIPPED
    elif any(f.state == "applied" for f in inputs.action_state):
        action_evidence = HardFilterEvidence.PRIOR_APPLIED
    elif any(
        f.state == "expired" and f.score_posting_version_id == inputs.current_version_id
        for f in inputs.action_state
    ):
        action_evidence = HardFilterEvidence.CURRENT_VERSION_EXPIRED

    outcomes = (
        HardFilterRuleOutcome(
            HardFilterRule.TITLE_NO_MATCH,
            RuleStatus.REJECT if not title_match else RuleStatus.PASS,
            HardFilterEvidence.ALLOWED_TITLE_ABSENT
            if not title_match
            else HardFilterEvidence.ALLOWED_TITLE_MATCH,
        ),
        HardFilterRuleOutcome(
            HardFilterRule.SENIORITY_LOW,
            RuleStatus.REJECT if seniority_match else RuleStatus.PASS,
            HardFilterEvidence.EXCLUDED_SENIORITY_MATCH
            if seniority_match
            else HardFilterEvidence.EXCLUDED_SENIORITY_ABSENT,
        ),
        HardFilterRuleOutcome(
            HardFilterRule.WRONG_DISCIPLINE,
            RuleStatus.REJECT if discipline_match else RuleStatus.PASS,
            HardFilterEvidence.EXCLUDED_DISCIPLINE_MATCH
            if discipline_match
            else HardFilterEvidence.EXCLUDED_DISCIPLINE_ABSENT,
        ),
        HardFilterRuleOutcome(HardFilterRule.NOT_REMOTE, remote_status, remote_evidence),
        HardFilterRuleOutcome(HardFilterRule.GEO_EXCLUDED, geo_status, geo_evidence),
        HardFilterRuleOutcome(
            HardFilterRule.CLEARANCE_REQUIRED, clearance_status, clearance_evidence
        ),
        HardFilterRuleOutcome(
            HardFilterRule.STALE,
            RuleStatus.REJECT if stale else RuleStatus.PASS,
            HardFilterEvidence.OLDER_THAN_45_DAYS if stale else HardFilterEvidence.WITHIN_45_DAYS,
        ),
        HardFilterRuleOutcome(
            HardFilterRule.ALREADY_ACTIONED,
            RuleStatus.REJECT if action_evidence else RuleStatus.PASS,
            action_evidence or HardFilterEvidence.NO_QUALIFYING_ACTION,
        ),
        HardFilterRuleOutcome(
            HardFilterRule.COMPANY_BLOCKED,
            RuleStatus.REJECT if inputs.company_blocked else RuleStatus.PASS,
            HardFilterEvidence.COMPANY_BLOCKED
            if inputs.company_blocked
            else HardFilterEvidence.COMPANY_NOT_BLOCKED,
        ),
    )
    reasons = tuple(
        HardFilterReason(outcome.rule.value)
        for outcome in outcomes
        if outcome.status is RuleStatus.REJECT
    )
    unknowns = tuple(
        HardFilterUnknown(outcome.evidence.value)
        for outcome in outcomes
        if outcome.status is RuleStatus.UNKNOWN
    )
    result = PureFilterResult(
        eligible=not reasons,
        rejection_reasons=reasons,
        unknowns=unknowns,
        staleness_source=source,
        rule_outcomes=outcomes,
    )
    try:
        validate_pure_result(result)
    except ValueError as exc:
        raise FilterEvaluatorDefectError(FilterErrorCode.EVALUATOR_DEFECT) from exc
    return result


def validate_pure_result(result: PureFilterResult) -> None:
    """Validate a pure result's closed invariants; raise ``ValueError`` on any
    violation. Replay reuses this to reject invalid persisted output."""

    if not isinstance(result, PureFilterResult):
        raise ValueError("result is not a PureFilterResult")
    outcomes = result.rule_outcomes
    if len(outcomes) != len(HardFilterRule):
        raise ValueError("rule_outcomes must contain exactly nine entries")
    if tuple(outcome.rule for outcome in outcomes) != tuple(HardFilterRule):
        raise ValueError("rule_outcomes must follow canonical rule order without duplicates")
    expected_reasons = tuple(
        HardFilterReason(outcome.rule.value)
        for outcome in outcomes
        if outcome.status is RuleStatus.REJECT
    )
    expected_unknowns = tuple(
        HardFilterUnknown(outcome.evidence.value)
        for outcome in outcomes
        if outcome.status is RuleStatus.UNKNOWN
    )
    if result.rejection_reasons != expected_reasons:
        raise ValueError("rejection_reasons disagree with rule_outcomes")
    if result.unknowns != expected_unknowns:
        raise ValueError("unknowns disagree with rule_outcomes")
    if len(set(result.rejection_reasons)) != len(result.rejection_reasons):
        raise ValueError("rejection_reasons contains duplicates")
    if len(set(result.unknowns)) != len(result.unknowns):
        raise ValueError("unknowns contains duplicates")
    if not isinstance(result.eligible, bool) or result.eligible != (not result.rejection_reasons):
        raise ValueError("eligible disagrees with rejection_reasons")
    if not isinstance(result.staleness_source, StalenessSource):
        raise ValueError("staleness_source is not a StalenessSource member")
    for outcome in outcomes:
        if not (
            isinstance(outcome.rule, HardFilterRule)
            and isinstance(outcome.status, RuleStatus)
            and isinstance(outcome.evidence, HardFilterEvidence)
        ):
            raise ValueError("outcome fields are not closed enum members")
        if (
            outcome.rule.value,
            outcome.status.value,
            outcome.evidence.value,
        ) not in _ALLOWED_TRIPLES:
            raise ValueError("rule/status/evidence triple is not manifest-legal")
