"""Pure validation, canonicalization, and hashing for ATS observations."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.sources.ats.contracts import FetchResult, RawLocation, RawPosting

_LOCATION_FIELDS = ("label", "country_code", "region", "city", "workplace_type")


class InvalidObservation(ValueError):
    """A fetched observation cannot be persisted safely."""


@dataclass(frozen=True)
class CanonicalPosting:
    external_id: str
    title: str
    title_norm: str
    locations: tuple[dict[str, str | None], ...]
    description_md: str
    posting_url: str
    apply_url: str
    source_published_at: datetime | None
    source_updated_at: datetime | None
    department: str | None
    raw_payload: dict[str, Any]
    content_hash: str


@dataclass(frozen=True)
class PreparedFetch:
    postings: tuple[CanonicalPosting, ...]
    complete: bool
    http_status: int
    etag: str | None
    last_modified: str | None


def _canonical_inline(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    output: list[str] = []
    in_space = False
    for character in normalized:
        if character.isspace():
            if output:
                in_space = True
            continue
        if in_space:
            output.append(" ")
            in_space = False
        output.append(character)
    return "".join(output).strip()


def canonical_title(value: str) -> tuple[str, str]:
    title = _canonical_inline(value)
    if not title:
        raise InvalidObservation("empty title")
    return title, title.casefold()


def canonical_description(value: str) -> str:
    normalized = unicodedata.normalize(
        "NFKC",
        value.replace("\r\n", "\n").replace("\r", "\n"),
    )
    lines = [line.rstrip(" \t") for line in normalized.split("\n")]
    while lines and lines[0] == "":
        lines.pop(0)
    while lines and lines[-1] == "":
        lines.pop()
    output: list[str] = []
    empty_count = 0
    for line in lines:
        if line == "":
            empty_count += 1
            if empty_count > 2:
                continue
        else:
            empty_count = 0
        output.append(line)
    return "\n".join(output)


def canonical_locations(locations: list[RawLocation]) -> tuple[dict[str, str | None], ...]:
    canonical: list[dict[str, str | None]] = []
    seen: set[tuple[str | None, ...]] = set()
    for location in locations:
        if not isinstance(location, RawLocation):
            raise InvalidObservation("invalid location")
        values: list[str | None] = []
        for field in _LOCATION_FIELDS:
            raw = getattr(location, field)
            if raw is not None and not isinstance(raw, str):
                raise InvalidObservation("invalid location field")
            value = _canonical_inline(raw) if raw is not None else None
            if field == "label" and not value:
                raise InvalidObservation("empty location label")
            if field != "label" and value == "":
                value = None
            values.append(value)
        key = tuple(values)
        if key in seen:
            continue
        seen.add(key)
        canonical.append(dict(zip(_LOCATION_FIELDS, values, strict=True)))
    canonical.sort(key=lambda item: tuple(item[field] or "" for field in _LOCATION_FIELDS))
    return tuple(canonical)


def canonical_json_and_hash(
    *,
    title_norm: str,
    description_md: str,
    locations: tuple[dict[str, str | None], ...],
) -> tuple[str, str]:
    payload = {
        "description_md": description_md,
        "locations": list(locations),
        "title": title_norm,
    }
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return serialized, hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _is_aware(value: datetime) -> bool:
    if value.tzinfo is None:
        return False
    try:
        return value.tzinfo.utcoffset(value) is not None
    except Exception:
        return False


def _json_snapshot(value: object, active: set[int] | None = None) -> object:
    active = active if active is not None else set()
    if value is None or type(value) in {bool, str, int}:  # noqa: E721 - exact JSON types
        return value
    if type(value) is float:  # noqa: E721 - reject numeric subclasses and non-finite floats
        if not math.isfinite(value):
            raise InvalidObservation("non-finite JSON number")
        return value
    if type(value) is list:  # noqa: E721 - tuples and custom sequences are forbidden
        identity = id(value)
        if identity in active:
            raise InvalidObservation("cyclic JSON value")
        active.add(identity)
        try:
            return [_json_snapshot(item, active) for item in value]
        finally:
            active.remove(identity)
    if type(value) is dict:  # noqa: E721 - require an ordinary JSON object
        identity = id(value)
        if identity in active:
            raise InvalidObservation("cyclic JSON value")
        active.add(identity)
        snapshot: dict[str, object] = {}
        try:
            for key, item in value.items():
                if type(key) is not str:  # noqa: E721 - JSON object keys are strings
                    raise InvalidObservation("non-string JSON key")
                snapshot[key] = _json_snapshot(item, active)
            return snapshot
        finally:
            active.remove(identity)
    raise InvalidObservation("non-JSON raw payload")


def _optional_string(value: object) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise InvalidObservation("invalid optional string")


def _canonical_posting(posting: RawPosting) -> CanonicalPosting:
    required_strings = (
        posting.external_id,
        posting.title,
        posting.description_md,
        posting.posting_url,
        posting.apply_url,
    )
    if any(not isinstance(value, str) for value in required_strings):
        raise InvalidObservation("invalid required string")
    external_id = posting.external_id.strip()
    if not external_id:
        raise InvalidObservation("empty external id")
    title, title_norm = canonical_title(posting.title)
    description = canonical_description(posting.description_md)
    if not isinstance(posting.locations, list):
        raise InvalidObservation("invalid locations")
    locations = canonical_locations(posting.locations)
    for timestamp in (posting.source_published_at, posting.source_updated_at):
        if timestamp is not None and (
            not isinstance(timestamp, datetime) or not _is_aware(timestamp)
        ):
            raise InvalidObservation("invalid source timestamp")
    department = _optional_string(posting.department)
    if not isinstance(posting.raw, dict):
        raise InvalidObservation("raw payload is not an object")
    raw_payload = _json_snapshot(posting.raw)
    assert isinstance(raw_payload, dict)
    _, content_hash = canonical_json_and_hash(
        title_norm=title_norm,
        description_md=description,
        locations=locations,
    )
    return CanonicalPosting(
        external_id=external_id,
        title=title,
        title_norm=title_norm,
        locations=locations,
        description_md=description,
        posting_url=posting.posting_url,
        apply_url=posting.apply_url,
        source_published_at=posting.source_published_at,
        source_updated_at=posting.source_updated_at,
        department=department,
        raw_payload=copy.deepcopy(raw_payload),
        content_hash=content_hash,
    )


def prepare_fetch_result(value: object) -> PreparedFetch:
    if not isinstance(value, FetchResult):
        raise InvalidObservation("invalid fetch result")
    if type(value.complete) is not bool:  # noqa: E721 - Boolean is exact
        raise InvalidObservation("invalid completeness")
    if type(value.http_status) is not int or not 100 <= value.http_status <= 599:  # noqa: E721
        raise InvalidObservation("invalid HTTP status")
    if type(value.postings) is not list:  # noqa: E721 - contract requires a list
        raise InvalidObservation("invalid posting collection")
    etag = _optional_string(value.etag)
    last_modified = _optional_string(value.last_modified)
    if value.complete and value.http_status != 200:
        raise InvalidObservation("complete non-200")
    if value.http_status == 304 and (value.complete or value.postings):
        raise InvalidObservation("invalid 304")

    postings: list[CanonicalPosting] = []
    identities: set[str] = set()
    for posting in value.postings:
        if not isinstance(posting, RawPosting):
            raise InvalidObservation("invalid posting")
        canonical = _canonical_posting(posting)
        if canonical.external_id in identities:
            raise InvalidObservation("duplicate external id")
        identities.add(canonical.external_id)
        postings.append(canonical)
    postings.sort(key=lambda item: item.external_id)
    return PreparedFetch(
        postings=tuple(postings),
        complete=value.complete,
        http_status=value.http_status,
        etag=etag,
        last_modified=last_modified,
    )


def retained_invalid_facts(value: object) -> tuple[int | None, str | None, str | None]:
    """Retain independently valid HTTP/validator facts from malformed input."""

    http_status = getattr(value, "http_status", None)
    if type(http_status) is not int or not 100 <= http_status <= 599:  # noqa: E721
        http_status = None
    etag = getattr(value, "etag", None)
    if not isinstance(etag, str):
        etag = None
    last_modified = getattr(value, "last_modified", None)
    if not isinstance(last_modified, str):
        last_modified = None
    return http_status, etag, last_modified
