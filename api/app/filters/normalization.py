"""Deterministic Phase 1 text normalization and in-memory metadata."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

BOUNDARY_TERMINATORS = frozenset(".!?。！？")
GAP_CATEGORIES = (
    "whitespace",
    "comma",
    "semicolon",
    "slash",
    "other_punctuation",
    "sentence_boundary",
    "paragraph_boundary",
)


@dataclass(frozen=True)
class NormalizedToken:
    value: str
    field_kind: str
    field_ordinal: int
    sentence_segment: int
    token_ordinal: int
    start: int
    end: int


@dataclass(frozen=True)
class TokenGap:
    left_token_ordinal: int
    right_token_ordinal: int
    category: str


@dataclass(frozen=True)
class NormalizedField:
    field_kind: str
    field_ordinal: int
    source: str
    tokens: tuple[NormalizedToken, ...]
    gaps: tuple[TokenGap, ...]


def _boundary_at(value: str, index: int) -> tuple[int, str] | None:
    if value[index] == "\n":
        cursor = index + 1
        last_newline: int | None = None
        while cursor < len(value):
            candidate = cursor
            while (
                candidate < len(value) and value[candidate] != "\n" and value[candidate].isspace()
            ):
                candidate += 1
            if candidate >= len(value) or value[candidate] != "\n":
                break
            last_newline = candidate + 1
            cursor = candidate + 1
        if last_newline is not None:
            return last_newline, "paragraph_boundary"
    if value[index] in BOUNDARY_TERMINATORS:
        cursor = index + 1
        while cursor < len(value) and value[cursor] in BOUNDARY_TERMINATORS:
            cursor += 1
        return cursor, "sentence_boundary"
    return None


def _segments(value: str) -> list[tuple[int, int, int, str | None]]:
    """Return source ranges and the boundary that follows each range."""

    output: list[tuple[int, int, int, str | None]] = []
    start = 0
    segment_number = 0
    cursor = 0
    while cursor < len(value):
        boundary = _boundary_at(value, cursor)
        if boundary is None:
            cursor += 1
            continue
        end, category = boundary
        if value[start:cursor].strip():
            output.append((start, cursor, segment_number, category))
            segment_number += 1
        start = end
        cursor = end
    if value[start:].strip():
        output.append((start, len(value), segment_number, None))
    return output


def _is_letter_or_number(value: str) -> bool:
    return bool(value) and unicodedata.category(value[0]).startswith(("L", "N"))


def _transform_with_positions(value: str, offset: int) -> list[tuple[str, int]]:
    """NFKC-compose each base character with its trailing combining marks,
    then casefold, preserving each output character's original position."""

    transformed: list[tuple[str, int]] = []
    base: str | None = None
    base_index = 0
    marks = ""
    for original_index, character in enumerate(value, offset):
        if unicodedata.combining(character):
            marks += character
            continue
        if base is not None:
            _append_normalized(transformed, base + marks, base_index)
        base = character
        base_index = original_index
        marks = ""
    if base is not None:
        _append_normalized(transformed, base + marks, base_index)
    return transformed


def _append_normalized(transformed: list[tuple[str, int]], source: str, position: int) -> None:
    normalized = unicodedata.normalize("NFKC", source).casefold()
    if normalized in {"’", "‘", "ʼ", "＇"}:
        normalized = "'"
    transformed.extend((item, position) for item in normalized)


def _tokenize_segment(
    value: str,
    *,
    field_kind: str,
    field_ordinal: int,
    segment_number: int,
    offset: int,
    token_offset: int,
) -> list[NormalizedToken]:
    chars = _transform_with_positions(value, offset)
    tokens: list[NormalizedToken] = []
    cursor = 0
    while cursor < len(chars):
        character = chars[cursor][0]
        previous = chars[cursor - 1][0] if cursor else ""
        following = chars[cursor + 1][0] if cursor + 1 < len(chars) else ""
        apostrophe = (
            character == "'" and _is_letter_or_number(previous) and _is_letter_or_number(following)
        )
        if not _is_letter_or_number(character) and not apostrophe:
            cursor += 1
            continue
        start = cursor
        cursor += 1
        while cursor < len(chars):
            current = chars[cursor][0]
            previous = chars[cursor - 1][0]
            following = chars[cursor + 1][0] if cursor + 1 < len(chars) else ""
            if _is_letter_or_number(current) or (
                current == "'"
                and _is_letter_or_number(previous)
                and _is_letter_or_number(following)
            ):
                cursor += 1
            else:
                break
        token_value = "".join(item[0] for item in chars[start:cursor]).replace("'", "")
        if token_value:
            tokens.append(
                NormalizedToken(
                    value=token_value,
                    field_kind=field_kind,
                    field_ordinal=field_ordinal,
                    sentence_segment=segment_number,
                    token_ordinal=token_offset + len(tokens),
                    start=chars[start][1],
                    end=chars[cursor - 1][1] + 1,
                )
            )
    return tokens


def _gap_category(value: str) -> str:
    if "\n" in value:
        # A paragraph boundary is the only newline sequence that is a
        # sentence-level boundary. A lone LF is ordinary token whitespace.
        for index, character in enumerate(value):
            if character == "\n":
                boundary = _boundary_at(value, index)
                if boundary and boundary[1] == "paragraph_boundary":
                    return "paragraph_boundary"
    if any(character in BOUNDARY_TERMINATORS for character in value):
        return "sentence_boundary"
    if "," in value:
        return "comma"
    if ";" in value:
        return "semicolon"
    if "/" in value or "\\" in value:
        return "slash"
    if all(character.isspace() for character in value):
        return "whitespace"
    return "other_punctuation"


def normalize_field(
    value: str,
    *,
    field_kind: str,
    field_ordinal: int = 0,
) -> NormalizedField:
    if not isinstance(value, str):
        raise TypeError("filter text must be a string")
    normalized_newlines = value.replace("\r\n", "\n").replace("\r", "\n")
    tokens: list[NormalizedToken] = []
    segments = _segments(normalized_newlines)
    for start, end, segment_number, _ in segments:
        tokens.extend(
            _tokenize_segment(
                normalized_newlines[start:end],
                field_kind=field_kind,
                field_ordinal=field_ordinal,
                segment_number=segment_number,
                offset=start,
                token_offset=len(tokens),
            )
        )
    gaps: list[TokenGap] = []
    for left, right in zip(tokens, tokens[1:], strict=False):
        category = _gap_category(normalized_newlines[left.end : right.start])
        if left.sentence_segment != right.sentence_segment:
            # The source span can be empty after a consumed boundary, so use
            # token metadata as the authoritative classification.
            if category != "paragraph_boundary":
                category = "sentence_boundary"
        gaps.append(TokenGap(left.token_ordinal, right.token_ordinal, category))
    return NormalizedField(
        field_kind=field_kind,
        field_ordinal=field_ordinal,
        source=normalized_newlines,
        tokens=tuple(tokens),
        gaps=tuple(gaps),
    )


def normalize_fields(
    *,
    title: str,
    description: str | None,
    locations: tuple[dict[str, object], ...] | list[dict[str, object]],
) -> tuple[NormalizedField, ...]:
    fields = [normalize_field(title, field_kind="title")]
    if description is not None:
        if not isinstance(description, str):
            raise TypeError("filter description must be a string or null")
        fields.append(normalize_field(description, field_kind="description"))
    for ordinal, location in enumerate(locations):
        label = location.get("label")
        if label is None:
            continue
        if not isinstance(label, str):
            raise TypeError("filter location label must be a string")
        fields.append(normalize_field(label, field_kind="location", field_ordinal=ordinal))
    return tuple(fields)


def sequence_matches(
    fields: tuple[NormalizedField, ...],
    phrase: str,
    *,
    field_kinds: frozenset[str] | None = None,
    same_sentence: bool = False,
) -> list[tuple[NormalizedField, int, int]]:
    wanted = tuple(normalize_field(phrase, field_kind="pattern").tokens)
    values = tuple(token.value for token in wanted)
    matches: list[tuple[NormalizedField, int, int]] = []
    for field in fields:
        if field_kinds is not None and field.field_kind not in field_kinds:
            continue
        field_values = tuple(token.value for token in field.tokens)
        for start in range(0, len(field_values) - len(values) + 1):
            if field_values[start : start + len(values)] != values:
                continue
            end = start + len(values)
            if (
                same_sentence
                and len({token.sentence_segment for token in field.tokens[start:end]}) != 1
            ):
                continue
            matches.append((field, start, end))
    return matches


def span_contains(container: tuple[int, int], candidate: tuple[int, int]) -> bool:
    return container[0] <= candidate[0] and candidate[1] <= container[1]
