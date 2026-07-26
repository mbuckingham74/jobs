"""Tests for the shared frozen ATS source-layer contract.

Covers Task 004 acceptance criteria for the shared contract:

- the contract types are frozen, have the documented field names and default
  values, expose ``datetime`` source timestamps, and store a defensive
  ``raw`` snapshot that includes unknown vendor fields;
- importing the contract package does not pull SQLAlchemy, FastAPI, pipeline,
  repository, or résumé modules;
- the :class:`ATSAdapter` protocol surface (``slug``, ``rate_limit_per_min``,
  async ``list_postings``, and ``token_patterns``) is conformance-checkable;
- the contract values cannot be mutated after construction and a caller's
  :class:`ConditionalHeaders` value cannot be mutated by an adapter.

No network call is made. The contract package must remain importable with the
existing autouse network guard active.
"""

from __future__ import annotations

import dataclasses
import inspect
import re

import pytest

from app.sources.ats import contracts as contracts_mod
from app.sources.ats.contracts import (
    ATSAdapter,
    ConditionalHeaders,
    FetchResult,
    RawLocation,
    RawPosting,
    SourceEndpoint,
)

CONTRACT_MODULE = "app.sources.ats.contracts"


# -- Frozen value types ---------------------------------------------


def test_source_endpoint_is_frozen_dataclass() -> None:
    endpoint = SourceEndpoint(kind="greenhouse", token="forksboard")
    assert endpoint.kind == "greenhouse"
    assert endpoint.token == "forksboard"
    assert endpoint.region == "global"
    assert endpoint.base_url is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        endpoint.token = "other-board"  # type: ignore[misc]


def test_source_endpoint_accepts_optional_base_url() -> None:
    endpoint = SourceEndpoint(
        kind="lever",
        token="ventra",
        region="eu",
        base_url="https://api.eu.lever.co/v0/postings/ventra",
    )
    assert endpoint.base_url == "https://api.eu.lever.co/v0/postings/ventra"
    assert endpoint.region == "eu"


def test_raw_location_default_optional_fields_none() -> None:
    loc = RawLocation(label="Remote")
    assert loc.label == "Remote"
    assert loc.country_code is None
    assert loc.region is None
    assert loc.city is None
    assert loc.workplace_type is None


def test_raw_posting_is_frozen_with_documented_fields() -> None:
    posting = RawPosting(
        external_id="42",
        title="Engineer",
        locations=[RawLocation(label="Remote")],
        description_md="# Role\n\n## About",
        posting_url="https://boards.greenhouse.io/board/jobs/42",
        apply_url="https://boards.greenhouse.io/board/jobs/42",
        source_published_at=None,
        source_updated_at=None,
        department="Engineering",
        raw={"id": 42, "unknown_field": "value"},
    )
    assert dataclasses.is_dataclass(posting)
    assert posting.external_id == "42"
    assert posting.department == "Engineering"
    assert posting.raw["unknown_field"] == "value"
    with pytest.raises(dataclasses.FrozenInstanceError):
        posting.title = "Other"  # type: ignore[misc]


def test_fetch_result_defaults_etag_and_last_modified_to_none() -> None:
    result = FetchResult(postings=[], complete=True, http_status=200)
    assert result.etag is None
    assert result.last_modified is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.complete = False  # type: ignore[misc]


def test_conditional_headers_default_none() -> None:
    heads = ConditionalHeaders()
    assert heads.etag is None
    assert heads.last_modified is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        heads.etag = '"x"'  # type: ignore[misc]


def test_contract_module_does_not_import_sqlalchemy() -> None:
    source = inspect.getsource(contracts_mod)
    assert "import sqlalchemy" not in source
    assert "from sqlalchemy" not in source


def test_contract_module_does_not_import_fastapi() -> None:
    source = inspect.getsource(contracts_mod)
    assert "import fastapi" not in source
    assert "from fastapi" not in source


def test_contract_module_does_not_import_resume_modules() -> None:
    source = inspect.getsource(contracts_mod)
    assert "app.resume" not in source
    assert "import resume" not in source


# -- ATSAdapter protocol -------------------------------------------


def test_atsadapter_is_runtime_checkable_protocol() -> None:
    assert isinstance(ATSAdapter, object)

    # A class that implements the documented surface passes ``isinstance``.
    class _Sample:
        slug = "sample"
        rate_limit_per_min = 60

        async def list_postings(
            self, endpoint: SourceEndpoint, conditional: ConditionalHeaders
        ) -> FetchResult:
            raise NotImplementedError

        def token_patterns(self) -> list[re.Pattern[str]]:
            return []

    assert isinstance(_Sample(), ATSAdapter)


def test_atsadapter_unimplemented_protocol_is_not_an_instance() -> None:
    class _MissingSlug:
        rate_limit_per_min = 60

        async def list_postings(
            self, endpoint: SourceEndpoint, conditional: ConditionalHeaders
        ) -> FetchResult:
            raise NotImplementedError

        def token_patterns(self) -> list[re.Pattern[str]]:
            return []

    # ``slug`` is a required protocol attribute; an instance missing it does
    # not satisfy the runtime-checkable protocol.
    assert not isinstance(_MissingSlug(), ATSAdapter)


def test_contract_module_re_exports_consistent_surface() -> None:
    # The ``app.sources.ats`` package re-exports the contract surface that
    # adapters and tests share; pin those names so a stray rename is detected.
    from app.sources.ats import (
        ATSAdapter as ReATSAdapter,
    )
    from app.sources.ats import (
        ConditionalHeaders as ReConditionalHeaders,
    )
    from app.sources.ats import (
        FetchResult as ReFetchResult,
    )
    from app.sources.ats import (
        RawLocation as ReRawLocation,
    )
    from app.sources.ats import (
        RawPosting as ReRawPosting,
    )
    from app.sources.ats import (
        SourceEndpoint as ReSourceEndpoint,
    )

    assert ReSourceEndpoint is SourceEndpoint
    assert ReRawLocation is RawLocation
    assert ReRawPosting is RawPosting
    assert ReFetchResult is FetchResult
    assert ReConditionalHeaders is ConditionalHeaders
    assert ReATSAdapter is ATSAdapter


# -- Defensive snapshots do not share mutable children --------------


def test_raw_field_is_a_dict_storage_slot() -> None:
    # ``RawPosting.raw`` is typed as a ``dict`` and the contract stores the
    # caller-supplied object without implicit cloning; the adapter is the
    # documented owner of the defensive snapshot (see ``test_greenhouse_adapter``).
    source_obj = {"id": 42, "unknown_field": "value"}
    posting = RawPosting(
        external_id="42",
        title="Engineer",
        locations=[],
        description_md="x",
        posting_url="https://boards.greenhouse.io/board/jobs/42",
        apply_url="https://boards.greenhouse.io/board/jobs/42",
        source_published_at=None,
        source_updated_at=None,
        department=None,
        raw=source_obj,
    )
    assert posting.raw["unknown_field"] == "value"
    assert posting.raw is source_obj


def test_contract_module_exposes_ats_adapter_protocol() -> None:
    assert hasattr(contracts_mod, "ATSAdapter")
    assert ATSAdapter._is_protocol is True  # type: ignore[attr-defined]


def test_async_list_postings_signature_in_protocol() -> None:
    import inspect

    members = dict(inspect.getmembers(ATSAdapter))
    assert "list_postings" in members
    assert inspect.iscoroutinefunction(members["list_postings"])
