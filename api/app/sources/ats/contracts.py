"""Shared frozen ATS source-layer contract.

Defines the specification's frozen value types for one source fetch and the
asynchronous :class:`ATSAdapter` protocol that every ATS adapter implements.
This module is a source-layer contract, not an ORM model:

- it does not import SQLAlchemy, repository code, FastAPI, pipeline code, or
  the résumé modules;
- ``RawPosting.raw`` is a defensive snapshot of the complete decoded vendor
  posting object, including unknown fields;
- normalization consumers must not mutate a returned ``RawPosting``, a decoded
  response object, or a caller-supplied :class:`ConditionalHeaders` value.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class SourceEndpoint:
    """A frozen source-layer endpoint descriptor.

    Only the endpoint fields an adapter needs are exposed:

    - ``kind`` — the adapter slug (e.g. ``"greenhouse"``);
    - ``token`` — the vendor board token, validated by the adapter before any
      network I/O;
    - ``region`` — the adapter-specific region (e.g. Lever's ``"eu"``); Phase 1
      Greenhouse is ``"global"`` only; and
    - ``base_url`` — an optional caller-supplied base URL, which the Greenhouse
      adapter must reject in favour of the exact documented origin.
    """

    kind: str
    token: str
    region: str = "global"
    base_url: str | None = None


@dataclass(frozen=True)
class RawLocation:
    """A normalized posting location.

    Only the vendor-supplied label is preserved by the Greenhouse adapter. The
    remaining fields are ``None`` because the adapter must not infer
    geography, remote status, or workplace type from punctuation, offices,
    title, or description text.
    """

    label: str
    country_code: str | None = None
    region: str | None = None
    city: str | None = None
    workplace_type: str | None = None


@dataclass(frozen=True)
class RawPosting:
    """One normalized ATS posting.

    ``raw`` is a defensive copy of the complete decoded vendor posting object,
    including unknown fields, retained for debugging; it is not a reduced
    projection and not a serialized log field. ``datetime`` values use
    timezone-aware datetimes; an absent or malformed vendor timestamp is
    represented as ``None`` rather than a guessed time.
    """

    external_id: str
    title: str
    locations: list[RawLocation]
    description_md: str
    posting_url: str
    apply_url: str
    source_published_at: datetime | None
    source_updated_at: datetime | None
    department: str | None
    raw: dict[str, object]


@dataclass(frozen=True)
class FetchResult:
    """The outcome of one adapter fetch.

    Only a fetch with ``complete=True`` may authorize later close-reconciliation.
    ``304 Not Modified`` uses ``complete=False`` with ``http_status=304`` so a
    downstream orchestrator can distinguish an unchanged board from a complete
    empty board.

    The ``ETag`` and ``Last-Modified`` header values are copied from the final
    response exactly when present, otherwise ``None``. Redirect-hop validator
    headers never leak into the final result.
    """

    postings: list[RawPosting]
    complete: bool
    http_status: int
    etag: str | None = None
    last_modified: str | None = None


@dataclass(frozen=True)
class ConditionalHeaders:
    """The conditional validators carried by an upstream ``source_fetch`` row.

    ``None`` means the validator is absent; the adapter must omit the
    corresponding request header rather than invent one.
    """

    etag: str | None = None
    last_modified: str | None = None


@runtime_checkable
class ATSAdapter(Protocol):
    """The asynchronous adapter protocol every ATS implements.

    Adapters are deterministic separators between vendor JSON and the
    application's :class:`RawPosting` shape. They may log a bounded event
    category, HTTP status, redirect count, byte count, and normalized posting
    count, but must never log request/response bodies, job descriptions,
    header dumps, validator values, endpoint tokens, full redirect URLs, or raw
    exception strings.
    """

    slug: str
    rate_limit_per_min: int

    async def list_postings(
        self, endpoint: SourceEndpoint, conditional: ConditionalHeaders
    ) -> FetchResult:
        """Fetch and normalize postings for one endpoint."""
        ...

    def token_patterns(self) -> list[re.Pattern[str]]:
        """Return stable compiled token-recognition patterns with a ``token``
        named capture, used by source discovery."""
        ...
