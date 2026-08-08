"""Focused normalization, segmentation, token-metadata, gap, and primitive
matching contract for Task 008's deterministic foundation."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.filters.evaluator import PureFilterInput, evaluate_pure
from app.filters.normalization import (
    normalize_field,
    normalize_fields,
    sequence_matches,
    span_contains,
)

AS_OF = datetime(2026, 8, 1, 12, tzinfo=UTC)


def _tokens(field):
    return [
        (
            token.value,
            token.field_kind,
            token.field_ordinal,
            token.sentence_segment,
            token.token_ordinal,
        )
        for token in field.tokens
    ]


def _gaps(field):
    return [(gap.left_token_ordinal, gap.right_token_ordinal, gap.category) for gap in field.gaps]


# ----------------------------------------------------------------------
# Newline normalization
# ----------------------------------------------------------------------


def test_crlf_and_cr_normalize_to_lf() -> None:
    field = normalize_field("A\r\nB", field_kind="description")
    assert [token.value for token in field.tokens] == ["a", "b"]
    assert all(token.sentence_segment == 0 for token in field.tokens)
    assert field.gaps[0].category == "whitespace"
    field = normalize_field("A\rB", field_kind="description")
    assert [token.value for token in field.tokens] == ["a", "b"]
    assert field.gaps[0].category == "whitespace"
    field = normalize_field("A\r\n\r\nB", field_kind="description")
    assert [token.sentence_segment for token in field.tokens] == [0, 1]
    assert field.gaps[0].category == "paragraph_boundary"
    field = normalize_field("A\r\rB", field_kind="description")
    assert [token.sentence_segment for token in field.tokens] == [0, 1]
    assert field.gaps[0].category == "paragraph_boundary"


# ----------------------------------------------------------------------
# Sentence and paragraph segmentation
# ----------------------------------------------------------------------


def test_single_lf_is_not_a_sentence_boundary() -> None:
    field = normalize_field("A\nB", field_kind="description")
    assert all(token.sentence_segment == 0 for token in field.tokens)
    assert field.gaps[0].category == "whitespace"


@pytest.mark.parametrize("text", ["A\n\nB", "A\n \nB", "A\n\t\nB", "A\n\n\nB"])
def test_paragraph_boundaries_close_segments(text: str) -> None:
    field = normalize_field(text, field_kind="description")
    assert [token.sentence_segment for token in field.tokens] == [0, 1]
    assert field.gaps[0].category == "paragraph_boundary"


@pytest.mark.parametrize("terminator", [".", "!", "?", "。", "！", "？"])
def test_each_of_the_six_sentence_terminators_closes_a_segment(terminator: str) -> None:
    field = normalize_field(f"A{terminator}B", field_kind="description")
    assert [token.sentence_segment for token in field.tokens] == [0, 1]
    assert field.gaps[0].category == "sentence_boundary"


@pytest.mark.parametrize("run", ["A!!B", "A!?B", "A。！B", "A.。!B", "A??B"])
def test_terminator_runs_are_one_sentence_boundary(run: str) -> None:
    field = normalize_field(run, field_kind="description")
    assert [token.sentence_segment for token in field.tokens] == [0, 1]
    assert len(field.gaps) == 1
    assert field.gaps[0].category == "sentence_boundary"


@pytest.mark.parametrize(
    "punctuation",
    [":", ";", "-", "—", "/", "\\", "_", ",", "(", ")", "&", "+", "="],
)
def test_non_terminating_punctuation_is_not_a_sentence_boundary(punctuation: str) -> None:
    field = normalize_field(f"A{punctuation} B", field_kind="description")
    assert all(token.sentence_segment == 0 for token in field.tokens)
    assert len(field.gaps) == 1


def test_leading_boundaries_discard_empty_segments() -> None:
    field = normalize_field("。A!!B", field_kind="description")
    assert _tokens(field) == [
        ("a", "description", 0, 0, 0),
        ("b", "description", 0, 1, 1),
    ]
    assert field.gaps[0].category == "sentence_boundary"


def test_paragraph_boundary_takes_precedence_over_terminator() -> None:
    field = normalize_field("A.\n\nB", field_kind="description")
    assert [token.sentence_segment for token in field.tokens] == [0, 1]
    assert field.gaps[0].category == "paragraph_boundary"


# ----------------------------------------------------------------------
# Gap categories
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, category",
    [
        ("A, B", "comma"),
        ("A; B", "semicolon"),
        ("A/ B", "slash"),
        ("A\\ B", "slash"),
        ("A - B", "other_punctuation"),
        ("A—B", "other_punctuation"),
        ("A: B", "other_punctuation"),
        ("A&B", "other_punctuation"),
        ("A B", "whitespace"),
        ("A\nB", "whitespace"),
        ("A. B", "sentence_boundary"),
        ("A\n\nB", "paragraph_boundary"),
    ],
)
def test_gap_categories_are_exact_and_distinguishable(text: str, category: str) -> None:
    field = normalize_field(text, field_kind="description")
    assert len(field.tokens) == 2
    assert field.gaps[0].category == category


def test_named_scope_separator_gaps_remain_distinguishable() -> None:
    field = normalize_field("Europe / Canada", field_kind="description")
    assert field.gaps[0].category == "slash"
    field = normalize_field("Europe - Canada", field_kind="description")
    assert field.gaps[0].category == "other_punctuation"
    field = normalize_field("Europe Canada", field_kind="description")
    assert field.gaps[0].category == "whitespace"


# ----------------------------------------------------------------------
# Token metadata
# ----------------------------------------------------------------------


def test_token_metadata_is_exact_and_deterministic() -> None:
    fields = normalize_fields(
        title="Product Manager.",
        description="A, B\nC. D\n\nE",
        locations=({"label": "Remote, US"}, {"label": "Canada"}),
    )
    assert _tokens(fields[0]) == [
        ("product", "title", 0, 0, 0),
        ("manager", "title", 0, 0, 1),
    ]
    assert _tokens(fields[1]) == [
        ("a", "description", 0, 0, 0),
        ("b", "description", 0, 0, 1),
        ("c", "description", 0, 0, 2),
        ("d", "description", 0, 1, 3),
        ("e", "description", 0, 2, 4),
    ]
    assert _gaps(fields[1]) == [
        (0, 1, "comma"),
        (1, 2, "whitespace"),
        (2, 3, "sentence_boundary"),
        (3, 4, "paragraph_boundary"),
    ]
    assert _tokens(fields[2]) == [
        ("remote", "location", 0, 0, 0),
        ("us", "location", 0, 0, 1),
    ]
    assert _tokens(fields[3]) == [
        ("canada", "location", 1, 0, 0),
    ]

    first = normalize_field("Product Manager", field_kind="title")
    second = normalize_field("Product Manager", field_kind="title")
    assert _tokens(first) == _tokens(second)
    assert _gaps(first) == _gaps(second)


def test_nfkc_casefold_and_apostrophe_handling_are_closed() -> None:
    field = normalize_field("ＰＲＯＤＵＣＴ ＭＡＮＡＧＥＲ", field_kind="title")
    assert [token.value for token in field.tokens] == ["product", "manager"]
    field = normalize_field("Don’t Stop", field_kind="title")
    assert [token.value for token in field.tokens] == ["dont", "stop"]
    field = normalize_field("Don‘t Stop", field_kind="title")
    assert [token.value for token in field.tokens] == ["dont", "stop"]
    field = normalize_field("Donʼt Stop", field_kind="title")
    assert [token.value for token in field.tokens] == ["dont", "stop"]
    field = normalize_field("Don＇t Stop", field_kind="title")
    assert [token.value for token in field.tokens] == ["dont", "stop"]
    field = normalize_field("'Stop", field_kind="title")
    assert [token.value for token in field.tokens] == ["stop"]
    field = normalize_field("Café", field_kind="title")
    assert [token.value for token in field.tokens] == ["café"]
    field = normalize_field("Cafe\u0301", field_kind="title")
    assert [token.value for token in field.tokens] == ["café"]
    field = normalize_field("e\u0301x", field_kind="title")
    assert [token.value for token in field.tokens] == ["éx"]


# ----------------------------------------------------------------------
# Primitive matching helpers
# ----------------------------------------------------------------------


def test_sequence_matching_is_contiguous_and_per_field() -> None:
    fields = normalize_fields(
        title="Product",
        description="owner business analyst",
        locations=(),
    )
    assert not sequence_matches(fields, "product owner")
    assert not sequence_matches(fields, "product owner", field_kinds=frozenset({"description"}))
    assert len(sequence_matches(fields, "business analyst")) == 1
    assert (
        len(sequence_matches(fields, "business analyst", field_kinds=frozenset({"description"})))
        == 1
    )
    field = normalize_field("Product owner. Business analyst", field_kind="description")
    assert len(sequence_matches((field,), "product owner", same_sentence=True)) == 1
    assert len(sequence_matches((field,), "business analyst", same_sentence=True)) == 1
    assert sequence_matches((field,), "owner business")
    assert not sequence_matches((field,), "owner business", same_sentence=True)


def test_matching_never_crosses_field_or_sentence_boundaries() -> None:
    field = normalize_field("Product owner\n\nBusiness analyst", field_kind="description")
    assert [token.sentence_segment for token in field.tokens] == [0, 0, 1, 1]
    assert not sequence_matches((field,), "owner business", same_sentence=True)
    fields = normalize_fields(
        title="Product owner",
        description="Business analyst",
        locations=(),
    )
    assert not sequence_matches(fields, "product owner business analyst")
    assert len(sequence_matches(fields, "product owner")) == 1


def test_matching_never_crosses_location_labels_or_fields() -> None:
    fields = normalize_fields(
        title="X",
        description=None,
        locations=({"label": "Remote in"}, {"label": "the US"}),
    )
    location_fields = tuple(field for field in fields if field.field_kind == "location")
    assert not sequence_matches(location_fields, "remote in the us")
    fields = normalize_fields(
        title="X",
        description=None,
        locations=({"label": "Remote in the US"}, {"label": "Canada"}),
    )
    location_fields = tuple(field for field in fields if field.field_kind == "location")
    matches = sequence_matches(location_fields, "remote in the us")
    assert len(matches) == 1
    assert matches[0][0].field_ordinal == 0
    assert matches[0][1:] == (0, 4)
    fields = normalize_fields(
        title="X",
        description="Remote",
        locations=({"label": "in the US"},),
    )
    assert not sequence_matches(fields, "remote in the us")


def test_span_contains_is_inclusive_and_exact() -> None:
    assert span_contains((1, 5), (1, 5))
    assert span_contains((1, 5), (2, 4))
    assert not span_contains((1, 5), (0, 4))
    assert not span_contains((1, 5), (2, 6))
    assert not span_contains((1, 5), (5, 6))


def test_region_and_city_are_not_tokenized_as_location_labels() -> None:
    fields = normalize_fields(
        title="Product Manager",
        description=None,
        locations=({"region": "US", "city": "New York"},),
    )
    assert len(fields) == 1
    assert fields[0].field_kind == "title"
    fields = normalize_fields(
        title="Product Manager",
        description=None,
        locations=({"label": "New York", "region": "US", "city": "New York"},),
    )
    assert len(fields) == 2
    assert _tokens(fields[1]) == [("new", "location", 0, 0, 0), ("york", "location", 0, 0, 1)]


def test_derived_metadata_cannot_change_evaluation_identity() -> None:
    first = evaluate_pure(
        PureFilterInput(
            posting_id=1,
            posting_version_id=2,
            content_hash="a" * 64,
            title="Product Manager",
            description_md="Remote in the US",
            locations=(),
            source_published_at=None,
            first_seen_at=AS_OF,
            current_version_id=2,
            closed_at=None,
            company_blocked=False,
            action_state=(),
        ),
        evaluation_as_of=AS_OF,
    )
    second = evaluate_pure(
        PureFilterInput(
            posting_id=1,
            posting_version_id=2,
            content_hash="a" * 64,
            title="Product Manager",
            description_md="Remote in the US",
            locations=(),
            source_published_at=None,
            first_seen_at=AS_OF,
            current_version_id=2,
            closed_at=None,
            company_blocked=False,
            action_state=(),
        ),
        evaluation_as_of=AS_OF,
    )
    assert first.rejection_reasons == second.rejection_reasons
    assert first.unknowns == second.unknowns
    assert first.rule_outcomes == second.rule_outcomes
