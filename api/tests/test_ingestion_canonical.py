"""Pure canonicalization and validation tests for Task 006."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from app.ingestion.canonical import (
    InvalidObservation,
    canonical_description,
    canonical_json_and_hash,
    canonical_locations,
    canonical_title,
    prepare_fetch_result,
)
from app.sources.ats.contracts import FetchResult, RawLocation, RawPosting


def _posting(**overrides) -> RawPosting:
    values = {
        "external_id": " ext-1 ",
        "title": "Senior Product Manager",
        "locations": [
            RawLocation(
                label="Remote — US",
                country_code="US",
                workplace_type="remote",
            )
        ],
        "description_md": "Build things.\r\n\r\nShip them.",
        "posting_url": "https://example.invalid/job",
        "apply_url": "https://example.invalid/apply",
        "source_published_at": datetime(2026, 7, 1, tzinfo=UTC),
        "source_updated_at": None,
        "department": "Product",
        "raw": {"id": "ext-1", "nested": [True, None, 1, 1.5]},
    }
    values.update(overrides)
    return RawPosting(**values)


def test_golden_canonical_json_and_hash() -> None:
    title, title_norm = canonical_title("Senior Product Manager")
    locations = canonical_locations(
        [
            RawLocation(
                label="Remote — US",
                country_code="US",
                workplace_type="remote",
            )
        ]
    )
    canonical_json, digest = canonical_json_and_hash(
        title_norm=title_norm,
        description_md=canonical_description("Build things.\r\n\r\nShip them."),
        locations=locations,
    )
    assert title == "Senior Product Manager"
    assert canonical_json == (
        '{"description_md":"Build things.\\n\\nShip them.",'
        '"locations":[{"city":null,"country_code":"US","label":"Remote — US",'
        '"region":null,"workplace_type":"remote"}],'
        '"title":"senior product manager"}'
    )
    assert digest == "5695c190b2412ffbbde7ef1728e01bde6fc2a10f1234d189adc265df82b0a8e7"


def test_hash_stability_and_change_boundaries() -> None:
    base = prepare_fetch_result(
        FetchResult(postings=[_posting()], complete=True, http_status=200)
    ).postings[0]
    stable_variants = [
        _posting(title="senior product manager"),
        _posting(title="Ｓｅｎｉｏｒ Product Manager"),
        _posting(description_md="Build things.\n\nShip them."),
        _posting(
            locations=[
                RawLocation(label="Remote — US", country_code="US", workplace_type="remote"),
                RawLocation(label="Remote — US", country_code="US", workplace_type="remote"),
            ]
        ),
    ]
    for posting in stable_variants:
        prepared = prepare_fetch_result(
            FetchResult(postings=[posting], complete=True, http_status=200)
        )
        assert prepared.postings[0].content_hash == base.content_hash

    changed = [
        _posting(title="Principal Product Manager"),
        _posting(description_md="Build different things."),
        _posting(locations=[RawLocation(label="Remote — CA", country_code="US")]),
    ]
    for posting in changed:
        prepared = prepare_fetch_result(
            FetchResult(postings=[posting], complete=True, http_status=200)
        )
        assert prepared.postings[0].content_hash != base.content_hash


def test_location_sorting_deduplication_and_optional_empty_to_null() -> None:
    result = canonical_locations(
        [
            RawLocation(label=" Zed ", country_code=""),
            RawLocation(label="Alpha", country_code=" US "),
            RawLocation(label="Alpha", country_code=" US "),
        ]
    )
    assert [item["label"] for item in result] == ["Alpha", "Zed"]
    assert result[1]["country_code"] is None
    assert tuple(result[0]) == ("label", "country_code", "region", "city", "workplace_type")


@pytest.mark.parametrize(
    "value",
    [
        FetchResult(postings=[], complete=1, http_status=200),  # type: ignore[arg-type]
        FetchResult(postings=[], complete=True, http_status=201),
        FetchResult(postings=[_posting()], complete=False, http_status=304),
        FetchResult(postings=[], complete=False, http_status=True),  # type: ignore[arg-type]
        replace(
            FetchResult(postings=[_posting()], complete=True, http_status=200),
            postings=(_posting(),),  # type: ignore[arg-type]
        ),
    ],
)
def test_invalid_fetch_shapes_are_rejected(value: object) -> None:
    with pytest.raises(InvalidObservation):
        prepare_fetch_result(value)


def test_duplicate_trimmed_identity_is_rejected() -> None:
    result = FetchResult(
        postings=[_posting(external_id="same"), _posting(external_id=" same ")],
        complete=True,
        http_status=200,
    )
    with pytest.raises(InvalidObservation):
        prepare_fetch_result(result)


@pytest.mark.parametrize(
    "raw",
    [
        {"bad": float("nan")},
        {"bad": float("inf")},
        {"bad": b"bytes"},
        {"bad": datetime(2026, 7, 1, tzinfo=UTC)},
        {"bad": (1, 2)},
        {1: "non-string key"},
    ],
)
def test_non_json_raw_values_are_rejected(raw: dict) -> None:
    with pytest.raises(InvalidObservation):
        prepare_fetch_result(
            FetchResult(postings=[_posting(raw=raw)], complete=True, http_status=200)
        )


def test_cyclic_raw_payload_is_rejected() -> None:
    raw: dict[str, object] = {}
    raw["self"] = raw
    with pytest.raises(InvalidObservation):
        prepare_fetch_result(
            FetchResult(postings=[_posting(raw=raw)], complete=True, http_status=200)
        )


def test_raw_payload_is_defensively_copied() -> None:
    raw = {"nested": [{"value": "before"}]}
    prepared = prepare_fetch_result(
        FetchResult(postings=[_posting(raw=raw)], complete=True, http_status=200)
    )
    raw["nested"][0]["value"] = "after"
    assert prepared.postings[0].raw_payload == {"nested": [{"value": "before"}]}


def test_optional_types_and_timezone_awareness_are_enforced() -> None:
    with pytest.raises(InvalidObservation):
        prepare_fetch_result(
            FetchResult(
                postings=[_posting(department=7)],  # type: ignore[arg-type]
                complete=True,
                http_status=200,
            )
        )
    with pytest.raises(InvalidObservation):
        prepare_fetch_result(
            FetchResult(
                postings=[_posting(source_updated_at=datetime(2026, 7, 1))],
                complete=True,
                http_status=200,
            )
        )
