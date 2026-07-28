"""PostgreSQL integration tests for the one-endpoint workflow."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.ingestion import (
    IngestionDatabaseError,
    IngestionInputError,
    IngestionTargetError,
    TransportFailureCode,
)
from app.sources.ats.contracts import ConditionalHeaders, FetchResult
from app.sources.ats.greenhouse import (
    GreenhouseAdapter,
    GreenhouseEndpointError,
    GreenhouseTransportError,
)
from app.sources.ats.lever import LeverAdapter, LeverTransportError
from app.workflow import (
    AdapterRegistry,
    RunnerDatabaseError,
    RunnerFailureCode,
    RunnerInputError,
    RunnerTargetError,
    run_one_endpoint,
)
from app.workflow import service as workflow_service
from app.workflow.repository import reserved_counts

pytestmark = pytest.mark.postgres

BASE = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)


class _Clock:
    def __init__(self, *values: datetime) -> None:
        self.values = iter(values)

    def now(self) -> datetime:
        return next(self.values)


class _Adapter:
    slug = "greenhouse"

    def __init__(self, outcome=None, *, slug: str = "greenhouse") -> None:
        self.outcome = outcome or FetchResult(postings=[], complete=True, http_status=200)
        self.slug = slug
        self.calls = []

    async def list_postings(self, endpoint, conditional):
        self.calls.append((endpoint, conditional))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def _seed(
    engine,
    *,
    status: str = "active",
    kind: str = "greenhouse",
    region: str = "global",
    token: str = "invented-runner-token",
    base_url: str | None = None,
    company_name: str = "Invented Runner Co",
) -> int:
    with engine.begin() as conn:
        company_id = conn.execute(
            text("INSERT INTO company (name) VALUES (:name) RETURNING id"),
            {"name": company_name},
        ).scalar_one()
        return int(
            conn.execute(
                text(
                    """
                    INSERT INTO source_endpoint
                        (company_id, kind, token, region, base_url, status)
                    VALUES (:company_id, :kind, :token, :region, :base_url, :status)
                    RETURNING id
                    """
                ),
                {
                    "company_id": company_id,
                    "kind": kind,
                    "token": token,
                    "region": region,
                    "base_url": base_url,
                    "status": status,
                },
            ).scalar_one()
        )


def _clock() -> _Clock:
    return _Clock(
        BASE,
        BASE + timedelta(seconds=1),
        BASE + timedelta(seconds=2),
        BASE + timedelta(seconds=3),
    )


def _registry(adapter: _Adapter, factory_calls: list[int]) -> AdapterRegistry:
    def factory():
        factory_calls.append(1)
        return adapter

    return AdapterRegistry(greenhouse=factory, lever=lambda: pytest.fail("wrong factory"))


def _registry_for_kind(
    kind: str,
    adapter: _Adapter,
    selected_calls: list[int],
) -> AdapterRegistry:
    def selected():
        selected_calls.append(1)
        return adapter

    def forbidden():
        pytest.fail("wrong factory")

    return AdapterRegistry(
        greenhouse=selected if kind == "greenhouse" else forbidden,
        lever=selected if kind == "lever" else forbidden,
    )


def _run_row(engine, run_id: int):
    with engine.connect() as conn:
        return dict(
            conn.execute(text("SELECT * FROM pipeline_run WHERE id=:id"), {"id": run_id})
            .mappings()
            .one()
        )


def test_new_run_fetches_once_ingests_and_finalizes(ingestion_engine) -> None:
    endpoint_id = _seed(ingestion_engine)
    adapter = _Adapter()
    factory_calls = []
    result = asyncio.run(
        run_one_endpoint(
            engine=ingestion_engine,
            source_endpoint_id=endpoint_id,
            run_id=None,
            config_version="runner-v1",
            adapters=_registry(adapter, factory_calls),
            clock=_clock(),
        )
    )
    assert result.run_status == "succeeded"
    assert result.finalized is True
    assert result.failure_code is None
    assert result.ingestion_result is not None
    assert factory_calls == [1]
    assert len(adapter.calls) == 1
    endpoint, conditional = adapter.calls[0]
    assert endpoint.kind == "greenhouse"
    assert endpoint.token == "invented-runner-token"
    assert conditional == ConditionalHeaders()
    with ingestion_engine.connect() as conn:
        row = (
            conn.execute(text("SELECT * FROM pipeline_run WHERE id=:id"), {"id": result.run_id})
            .mappings()
            .one()
        )
    assert row["run_kind"] == "manual"
    assert row["status"] == "succeeded"
    assert row["last_completed_step"] == "source_fetch_recorded"
    assert row["counts"]["attempt_recorded"] is True
    assert type(row["counts"]["postings_seen"]) is int


@pytest.mark.parametrize(
    ("kind", "region", "status"),
    [
        ("greenhouse", "global", "active"),
        ("greenhouse", "global", "failing"),
        ("lever", "global", "active"),
        ("lever", "eu", "active"),
    ],
)
def test_supported_endpoint_matrix_selects_exact_factory_and_snapshot(
    ingestion_engine,
    kind: str,
    region: str,
    status: str,
) -> None:
    endpoint_id = _seed(
        ingestion_engine,
        kind=kind,
        region=region,
        status=status,
        token=f"invented-{kind}-{region}-token",
    )
    adapter = _Adapter(slug=kind)
    factory_calls = []
    result = asyncio.run(
        run_one_endpoint(
            engine=ingestion_engine,
            source_endpoint_id=endpoint_id,
            run_id=None,
            config_version="runner-v1",
            adapters=_registry_for_kind(kind, adapter, factory_calls),
            clock=_clock(),
        )
    )
    assert result.run_status == "succeeded"
    assert result.endpoint_kind == kind
    assert factory_calls == [1]
    assert len(adapter.calls) == 1
    endpoint, conditional = adapter.calls[0]
    assert endpoint.kind == kind
    assert endpoint.token == f"invented-{kind}-{region}-token"
    assert endpoint.region == region
    assert endpoint.base_url is None
    assert conditional == ConditionalHeaders()


@pytest.mark.parametrize(
    ("kind", "region", "expected_host"),
    [
        ("greenhouse", "global", "boards-api.greenhouse.io"),
        ("lever", "global", "api.lever.co"),
        ("lever", "eu", "api.eu.lever.co"),
    ],
)
def test_real_adapter_boundary_performs_one_mock_transport_request(
    ingestion_engine,
    kind: str,
    region: str,
    expected_host: str,
) -> None:
    endpoint_id = _seed(
        ingestion_engine,
        kind=kind,
        region=region,
        token=f"invented-real-{kind}-{region}-token",
    )
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = {"jobs": []} if kind == "greenhouse" else []
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(handler)
    factory_calls = []

    def factory():
        factory_calls.append(1)
        if kind == "greenhouse":
            return GreenhouseAdapter(transport=transport)
        return LeverAdapter(transport=transport)

    def forbidden():
        pytest.fail("wrong factory")

    result = asyncio.run(
        run_one_endpoint(
            engine=ingestion_engine,
            source_endpoint_id=endpoint_id,
            run_id=None,
            config_version="runner-v1",
            adapters=AdapterRegistry(
                greenhouse=factory if kind == "greenhouse" else forbidden,
                lever=factory if kind == "lever" else forbidden,
            ),
            clock=_clock(),
        )
    )
    assert result.run_status == "succeeded"
    assert factory_calls == [1]
    assert len(requests) == 1
    assert requests[0].url.host == expected_host


def test_real_adapter_endpoint_policy_calls_no_mock_transport(
    ingestion_engine,
) -> None:
    endpoint_id = _seed(
        ingestion_engine,
        token="invalid token private-policy-sentinel",
    )
    network_calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        network_calls.append(request)
        return httpx.Response(500)

    factory_calls = []

    def factory():
        factory_calls.append(1)
        return GreenhouseAdapter(transport=httpx.MockTransport(handler))

    result = asyncio.run(
        run_one_endpoint(
            engine=ingestion_engine,
            source_endpoint_id=endpoint_id,
            run_id=None,
            config_version="runner-v1",
            adapters=AdapterRegistry(
                greenhouse=factory,
                lever=lambda: pytest.fail("wrong factory"),
            ),
            clock=_Clock(
                BASE,
                BASE + timedelta(seconds=1),
                BASE + timedelta(seconds=2),
            ),
        )
    )
    assert factory_calls == [1]
    assert network_calls == []
    assert result.failure_code is RunnerFailureCode.ENDPOINT_POLICY
    assert result.ingestion_result is None


def test_each_new_invocation_creates_a_distinct_manual_run(ingestion_engine) -> None:
    endpoint_id = _seed(ingestion_engine)
    results = [
        asyncio.run(
            run_one_endpoint(
                engine=ingestion_engine,
                source_endpoint_id=endpoint_id,
                run_id=None,
                config_version="runner-v1",
                adapters=_registry(_Adapter(), []),
                clock=_Clock(
                    BASE + timedelta(minutes=offset),
                    BASE + timedelta(minutes=offset, seconds=1),
                    BASE + timedelta(minutes=offset, seconds=2),
                    BASE + timedelta(minutes=offset, seconds=3),
                ),
            )
        )
        for offset in (0, 1)
    ]
    assert results[0].run_id != results[1].run_id
    with ingestion_engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM source_fetch WHERE run_id IN (:first, :second)"),
                {"first": results[0].run_id, "second": results[1].run_id},
            ).scalar_one()
            == 2
        )


def test_invalid_initial_clock_writes_nothing_and_raises_closed_input_error(
    ingestion_engine,
) -> None:
    endpoint_id = _seed(ingestion_engine)
    with pytest.raises(RunnerInputError) as caught:
        asyncio.run(
            run_one_endpoint(
                engine=ingestion_engine,
                source_endpoint_id=endpoint_id,
                run_id=None,
                config_version="invalid-clock-v1",
                adapters=_registry(_Adapter(), []),
                clock=_Clock(datetime(2026, 7, 28, 10, 0)),
            )
        )
    assert caught.value.code is RunnerFailureCode.INVALID_CLOCK
    with ingestion_engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM pipeline_run WHERE config_version='invalid-clock-v1'")
            ).scalar_one()
            == 0
        )


def test_invalid_clock_after_binding_finalizes_closed_without_adapter_call(
    ingestion_engine,
) -> None:
    endpoint_id = _seed(ingestion_engine)
    with ingestion_engine.begin() as conn:
        run_id = int(
            conn.execute(
                text(
                    """
                    INSERT INTO pipeline_run
                        (run_kind,status,config_version,counts,started_at)
                    VALUES
                        ('manual','running','runner-v1',CAST(:counts AS jsonb),:started)
                    RETURNING id
                    """
                ),
                {
                    "counts": json.dumps(reserved_counts(endpoint_id, "greenhouse")),
                    "started": BASE,
                },
            ).scalar_one()
        )
    adapter = _Adapter()
    result = asyncio.run(
        run_one_endpoint(
            engine=ingestion_engine,
            source_endpoint_id=endpoint_id,
            run_id=run_id,
            config_version="runner-v1",
            adapters=_registry(adapter, []),
            clock=_Clock(datetime(2026, 7, 28, 10, 0)),
        )
    )
    assert result.failure_code is RunnerFailureCode.CLOCK_INVALID
    assert result.run_status == "failed"
    assert adapter.calls == []
    run = _run_row(ingestion_engine, run_id)
    assert run["started_at"] == run["finished_at"]
    assert run["error"] == {"code": RunnerFailureCode.CLOCK_INVALID.value}


def test_retry_replays_without_factory_or_network(ingestion_engine) -> None:
    endpoint_id = _seed(ingestion_engine)
    first_adapter = _Adapter()
    first = asyncio.run(
        run_one_endpoint(
            engine=ingestion_engine,
            source_endpoint_id=endpoint_id,
            run_id=None,
            config_version="runner-v1",
            adapters=_registry(first_adapter, []),
            clock=_clock(),
        )
    )
    forbidden = AdapterRegistry(
        greenhouse=lambda: pytest.fail("factory called on replay"),
        lever=lambda: pytest.fail("factory called on replay"),
    )
    replay = asyncio.run(
        run_one_endpoint(
            engine=ingestion_engine,
            source_endpoint_id=endpoint_id,
            run_id=first.run_id,
            config_version="runner-v1",
            adapters=forbidden,
            clock=_Clock(),
        )
    )
    assert replay.run_id == first.run_id
    assert replay.run_status == "succeeded"
    assert replay.ingestion_result is not None
    assert replay.ingestion_result.replayed is True


@pytest.mark.parametrize(
    ("status", "kind", "expected"),
    [
        ("paused", "greenhouse", RunnerFailureCode.ENDPOINT_PAUSED),
        ("retired", "greenhouse", RunnerFailureCode.ENDPOINT_RETIRED),
        ("active", "ashby", RunnerFailureCode.ADAPTER_DEFERRED),
        ("active", "workday", RunnerFailureCode.ADAPTER_NOT_IMPLEMENTED),
        ("active", "workable", RunnerFailureCode.ADAPTER_NOT_IMPLEMENTED),
        ("active", "smartrecruiters", RunnerFailureCode.ADAPTER_NOT_IMPLEMENTED),
        ("active", "recruitee", RunnerFailureCode.ADAPTER_NOT_IMPLEMENTED),
        ("active", "custom", RunnerFailureCode.ADAPTER_UNSUPPORTED),
    ],
)
def test_excluded_status_never_constructs_adapter(
    ingestion_engine,
    status: str,
    kind: str,
    expected: RunnerFailureCode,
) -> None:
    endpoint_id = _seed(ingestion_engine, status=status, kind=kind)
    result = asyncio.run(
        run_one_endpoint(
            engine=ingestion_engine,
            source_endpoint_id=endpoint_id,
            run_id=None,
            config_version="runner-v1",
            adapters=AdapterRegistry(
                greenhouse=lambda: pytest.fail("factory called"),
                lever=lambda: pytest.fail("factory called"),
            ),
            clock=_Clock(BASE, BASE + timedelta(seconds=1)),
        )
    )
    assert result.run_status == "failed"
    assert result.failure_code is expected
    assert result.ingestion_result is None


def test_validator_events_are_reconstructed_per_field(ingestion_engine) -> None:
    endpoint_id = _seed(ingestion_engine)
    with ingestion_engine.begin() as conn:
        prior_run = conn.execute(
            text(
                """
                INSERT INTO pipeline_run
                    (run_kind,status,config_version,started_at)
                VALUES ('manual','succeeded','prior',:started) RETURNING id
                """
            ),
            {"started": BASE - timedelta(hours=1)},
        ).scalar_one()
        conn.execute(
            text(
                """
                INSERT INTO source_fetch
                    (run_id,endpoint_id,status,http_status,postings_seen,
                     postings_new,postings_changed,etag,last_modified,started_at,finished_at)
                VALUES (:run_id,:endpoint_id,'complete',200,1,0,0,
                        'prior-etag','prior-last',:started,:finished)
                """
            ),
            {
                "run_id": prior_run,
                "endpoint_id": endpoint_id,
                "started": BASE - timedelta(minutes=59),
                "finished": BASE - timedelta(minutes=58),
            },
        )
    adapter = _Adapter()
    asyncio.run(
        run_one_endpoint(
            engine=ingestion_engine,
            source_endpoint_id=endpoint_id,
            run_id=None,
            config_version="runner-v1",
            adapters=_registry(adapter, []),
            clock=_clock(),
        )
    )
    assert adapter.calls[0][1] == ConditionalHeaders(etag="prior-etag", last_modified="prior-last")


def test_validator_query_orders_ties_and_excludes_unsafe_or_foreign_rows(
    ingestion_engine,
) -> None:
    endpoint_id = _seed(ingestion_engine)
    other_endpoint_id = _seed(ingestion_engine, token="invented-other-endpoint-token")

    def insert_fetch(
        conn,
        *,
        selected_endpoint_id: int,
        status: str,
        http_status: int,
        etag: str | None,
        last_modified: str | None,
        finished_at,
        error: str | None = None,
    ) -> None:
        run_id = conn.execute(
            text(
                """
                INSERT INTO pipeline_run
                    (run_kind,status,config_version,started_at)
                VALUES ('manual','succeeded','prior',:started)
                RETURNING id
                """
            ),
            {"started": BASE - timedelta(hours=1)},
        ).scalar_one()
        conn.execute(
            text(
                """
                INSERT INTO source_fetch
                    (run_id,endpoint_id,status,http_status,postings_seen,
                     postings_new,postings_changed,etag,last_modified,error,
                     started_at,finished_at)
                VALUES
                    (:run_id,:endpoint_id,:status,:http_status,0,0,0,
                     :etag,:last_modified,
                         CASE
                             WHEN CAST(:error AS text) IS NULL THEN NULL
                             ELSE jsonb_build_object('code', CAST(:error AS text))
                         END,
                     :started,:finished)
                """
            ),
            {
                "run_id": run_id,
                "endpoint_id": selected_endpoint_id,
                "status": status,
                "http_status": http_status,
                "etag": etag,
                "last_modified": last_modified,
                "error": error,
                "started": BASE - timedelta(minutes=30),
                "finished": finished_at,
            },
        )

    tied_finish = BASE - timedelta(minutes=10)
    with ingestion_engine.begin() as conn:
        insert_fetch(
            conn,
            selected_endpoint_id=endpoint_id,
            status="complete",
            http_status=200,
            etag=None,
            last_modified=None,
            finished_at=tied_finish,
        )
        insert_fetch(
            conn,
            selected_endpoint_id=endpoint_id,
            status="incomplete",
            http_status=304,
            etag=None,
            last_modified="new-last-modified",
            finished_at=tied_finish,
        )
        insert_fetch(
            conn,
            selected_endpoint_id=endpoint_id,
            status="complete",
            http_status=200,
            etag="old-etag",
            last_modified="old-last-modified",
            finished_at=BASE - timedelta(minutes=20),
        )
        insert_fetch(
            conn,
            selected_endpoint_id=endpoint_id,
            status="failed",
            http_status=500,
            etag="private-unsafe-etag",
            last_modified="private-unsafe-last-modified",
            error="ingestion.invalid_observation",
            finished_at=BASE - timedelta(minutes=5),
        )
        insert_fetch(
            conn,
            selected_endpoint_id=endpoint_id,
            status="complete",
            http_status=200,
            etag="private-null-finish-etag",
            last_modified="private-null-finish-last-modified",
            finished_at=None,
        )
        insert_fetch(
            conn,
            selected_endpoint_id=other_endpoint_id,
            status="complete",
            http_status=200,
            etag="private-other-endpoint-etag",
            last_modified="private-other-endpoint-last-modified",
            finished_at=BASE - timedelta(minutes=1),
        )
    adapter = _Adapter()
    asyncio.run(
        run_one_endpoint(
            engine=ingestion_engine,
            source_endpoint_id=endpoint_id,
            run_id=None,
            config_version="runner-v1",
            adapters=_registry(adapter, []),
            clock=_clock(),
        )
    )
    assert adapter.calls[0][1] == ConditionalHeaders(
        etag=None,
        last_modified="new-last-modified",
    )


@pytest.mark.parametrize(
    ("error", "expected", "attempt"),
    [
        (
            GreenhouseEndpointError("greenhouse.endpoint_invalid_token"),
            RunnerFailureCode.ENDPOINT_POLICY,
            False,
        ),
        (
            GreenhouseTransportError("greenhouse.transport.connect_timeout"),
            None,
            True,
        ),
        (
            GreenhouseTransportError("greenhouse.transport.unknown"),
            RunnerFailureCode.ADAPTER_DEFECT,
            False,
        ),
    ],
)
def test_adapter_errors_use_exact_closed_mapping(
    ingestion_engine, error: Exception, expected, attempt: bool
) -> None:
    endpoint_id = _seed(ingestion_engine)
    adapter = _Adapter(error)
    result = asyncio.run(
        run_one_endpoint(
            engine=ingestion_engine,
            source_endpoint_id=endpoint_id,
            run_id=None,
            config_version="runner-v1",
            adapters=_registry(adapter, []),
            clock=_clock(),
        )
    )
    if expected is None:
        assert result.failure_code.value == "adapter.connect_timeout"
    else:
        assert result.failure_code is expected
    assert (result.ingestion_result is not None) is attempt


@pytest.mark.parametrize(
    ("category", "expected"),
    [
        ("greenhouse.transport.connect_timeout", TransportFailureCode.CONNECT_TIMEOUT),
        ("greenhouse.transport.read_timeout", TransportFailureCode.READ_TIMEOUT),
        ("greenhouse.transport.connect_error", TransportFailureCode.CONNECT_ERROR),
        ("greenhouse.transport.protocol_error", TransportFailureCode.PROTOCOL_ERROR),
        ("greenhouse.transport_error", TransportFailureCode.TRANSPORT_ERROR),
        ("lever.transport.connect_timeout", TransportFailureCode.CONNECT_TIMEOUT),
        ("lever.transport.read_timeout", TransportFailureCode.READ_TIMEOUT),
        ("lever.transport.write_timeout", TransportFailureCode.WRITE_TIMEOUT),
        ("lever.transport.pool_timeout", TransportFailureCode.POOL_TIMEOUT),
        ("lever.transport.connect_error", TransportFailureCode.CONNECT_ERROR),
        ("lever.transport.read_error", TransportFailureCode.READ_ERROR),
        ("lever.transport.write_error", TransportFailureCode.WRITE_ERROR),
        ("lever.transport.protocol_error", TransportFailureCode.PROTOCOL_ERROR),
        ("lever.transport_error", TransportFailureCode.TRANSPORT_ERROR),
    ],
)
def test_every_typed_transport_category_reaches_task006_exactly(
    ingestion_engine,
    category: str,
    expected: TransportFailureCode,
) -> None:
    kind = category.split(".", maxsplit=1)[0]
    error = (
        GreenhouseTransportError(category)
        if kind == "greenhouse"
        else LeverTransportError(category)
    )
    endpoint_id = _seed(ingestion_engine, kind=kind)
    adapter = _Adapter(error, slug=kind)
    calls = []
    result = asyncio.run(
        run_one_endpoint(
            engine=ingestion_engine,
            source_endpoint_id=endpoint_id,
            run_id=None,
            config_version="runner-v1",
            adapters=_registry_for_kind(kind, adapter, calls),
            clock=_clock(),
        )
    )
    assert calls == [1]
    assert len(adapter.calls) == 1
    assert result.run_status == "failed"
    assert result.failure_code is expected
    assert result.ingestion_result is not None
    assert result.ingestion_result.reason_code == expected.value


@pytest.mark.parametrize(
    "factory",
    [
        lambda: (_ for _ in ()).throw(RuntimeError("private-factory-sentinel")),
        lambda: object(),
        lambda: _Adapter(slug="lever"),
    ],
)
def test_registry_construction_defects_never_call_list_postings(
    ingestion_engine,
    factory,
) -> None:
    endpoint_id = _seed(ingestion_engine)
    result = asyncio.run(
        run_one_endpoint(
            engine=ingestion_engine,
            source_endpoint_id=endpoint_id,
            run_id=None,
            config_version="runner-v1",
            adapters=AdapterRegistry(
                greenhouse=factory,
                lever=lambda: pytest.fail("wrong factory"),
            ),
            clock=_Clock(BASE, BASE + timedelta(seconds=1)),
        )
    )
    assert result.failure_code is RunnerFailureCode.ADAPTER_REGISTRY_DEFECT
    assert result.ingestion_result is None
    with ingestion_engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM source_fetch WHERE run_id=:run_id"),
                {"run_id": result.run_id},
            ).scalar_one()
            == 0
        )


def test_unrelated_manual_run_with_empty_counts_is_rejected_without_mutation(
    ingestion_engine,
) -> None:
    endpoint_id = _seed(ingestion_engine)
    with ingestion_engine.begin() as conn:
        run_id = int(
            conn.execute(
                text(
                    """
                    INSERT INTO pipeline_run
                        (run_kind,status,config_version,counts,started_at)
                    VALUES ('manual','running','runner-v1','{}'::jsonb,:started)
                    RETURNING id
                    """
                ),
                {"started": BASE},
            ).scalar_one()
        )
    before = _run_row(ingestion_engine, run_id)
    with pytest.raises(RunnerTargetError) as caught:
        asyncio.run(
            run_one_endpoint(
                engine=ingestion_engine,
                source_endpoint_id=endpoint_id,
                run_id=run_id,
                config_version="runner-v1",
                adapters=AdapterRegistry(
                    greenhouse=lambda: pytest.fail("factory called"),
                    lever=lambda: pytest.fail("factory called"),
                ),
                clock=_Clock(),
            )
        )
    assert caught.value.code is RunnerFailureCode.PIPELINE_RUN_INCOMPATIBLE
    assert _run_row(ingestion_engine, run_id) == before


@pytest.mark.parametrize(
    "case",
    [
        "foreign_run_kind",
        "foreign_config",
        "foreign_schema",
        "foreign_endpoint",
        "foreign_kind",
        "extra_count",
        "missing_count",
        "boolean_integer",
    ],
)
def test_every_foreign_or_malformed_owned_projection_is_rejected_without_mutation(
    ingestion_engine,
    case: str,
) -> None:
    endpoint_id = _seed(ingestion_engine)
    counts = reserved_counts(endpoint_id, "greenhouse")
    run_kind = "manual"
    config_version = "runner-v1"
    if case == "foreign_run_kind":
        run_kind = "daily"
    elif case == "foreign_config":
        config_version = "other-v1"
    elif case == "foreign_schema":
        counts["schema_version"] = 2
    elif case == "foreign_endpoint":
        counts["endpoint_id"] = endpoint_id + 1
    elif case == "foreign_kind":
        counts["adapter_kind"] = "lever"
    elif case == "extra_count":
        counts["private_extra"] = 1
    elif case == "missing_count":
        del counts["postings_changed"]
    elif case == "boolean_integer":
        counts["postings_seen"] = False
    with ingestion_engine.begin() as conn:
        run_id = int(
            conn.execute(
                text(
                    """
                    INSERT INTO pipeline_run
                        (run_kind,status,config_version,counts,started_at)
                    VALUES
                        (:run_kind,'running',:config_version,CAST(:counts AS jsonb),:started)
                    RETURNING id
                    """
                ),
                {
                    "run_kind": run_kind,
                    "config_version": config_version,
                    "counts": json.dumps(counts),
                    "started": BASE,
                },
            ).scalar_one()
        )
    before = _run_row(ingestion_engine, run_id)
    with pytest.raises(RunnerTargetError) as caught:
        asyncio.run(
            run_one_endpoint(
                engine=ingestion_engine,
                source_endpoint_id=endpoint_id,
                run_id=run_id,
                config_version="runner-v1",
                adapters=AdapterRegistry(
                    greenhouse=lambda: pytest.fail("factory called"),
                    lever=lambda: pytest.fail("factory called"),
                ),
                clock=_Clock(),
            )
        )
    assert caught.value.code is RunnerFailureCode.PIPELINE_RUN_INCOMPATIBLE
    assert _run_row(ingestion_engine, run_id) == before


def test_terminal_without_fetch_rejects_wrong_step_without_mutation(
    ingestion_engine,
) -> None:
    endpoint_id = _seed(ingestion_engine, status="paused")
    first = asyncio.run(
        run_one_endpoint(
            engine=ingestion_engine,
            source_endpoint_id=endpoint_id,
            run_id=None,
            config_version="runner-v1",
            adapters=AdapterRegistry(
                greenhouse=lambda: pytest.fail("factory called"),
                lever=lambda: pytest.fail("factory called"),
            ),
            clock=_Clock(BASE, BASE + timedelta(seconds=1)),
        )
    )
    with ingestion_engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE pipeline_run
                SET last_completed_step='adapter_fetch_completed'
                WHERE id=:id
                """
            ),
            {"id": first.run_id},
        )
    before = _run_row(ingestion_engine, first.run_id)
    with pytest.raises(RunnerTargetError) as caught:
        asyncio.run(
            run_one_endpoint(
                engine=ingestion_engine,
                source_endpoint_id=endpoint_id,
                run_id=first.run_id,
                config_version="runner-v1",
                adapters=AdapterRegistry(
                    greenhouse=lambda: pytest.fail("factory called"),
                    lever=lambda: pytest.fail("factory called"),
                ),
                clock=_Clock(),
            )
        )
    assert caught.value.code is RunnerFailureCode.PIPELINE_RUN_INCOMPATIBLE
    assert _run_row(ingestion_engine, first.run_id) == before


def test_valid_terminal_without_fetch_replays_and_preserves_timestamps(
    ingestion_engine,
) -> None:
    endpoint_id = _seed(ingestion_engine, status="paused")
    first = asyncio.run(
        run_one_endpoint(
            engine=ingestion_engine,
            source_endpoint_id=endpoint_id,
            run_id=None,
            config_version="runner-v1",
            adapters=AdapterRegistry(
                greenhouse=lambda: pytest.fail("factory called"),
                lever=lambda: pytest.fail("factory called"),
            ),
            clock=_Clock(BASE, BASE + timedelta(seconds=1)),
        )
    )
    before = _run_row(ingestion_engine, first.run_id)
    replay = asyncio.run(
        run_one_endpoint(
            engine=ingestion_engine,
            source_endpoint_id=endpoint_id,
            run_id=first.run_id,
            config_version="runner-v1",
            adapters=AdapterRegistry(
                greenhouse=lambda: pytest.fail("factory called"),
                lever=lambda: pytest.fail("factory called"),
            ),
            clock=_Clock(),
        )
    )
    assert replay.failure_code is RunnerFailureCode.ENDPOINT_PAUSED
    assert replay.finalized is True
    assert _run_row(ingestion_engine, first.run_id) == before


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("change", RunnerFailureCode.ENDPOINT_CHANGED),
        ("delete", RunnerFailureCode.ENDPOINT_DELETED),
    ],
)
def test_snapshot_fence_blocks_task006_after_endpoint_mutation(
    ingestion_engine, mutation: str, expected: RunnerFailureCode
) -> None:
    endpoint_id = _seed(ingestion_engine)

    class MutatingAdapter(_Adapter):
        async def list_postings(self, endpoint, conditional):
            self.calls.append((endpoint, conditional))
            with ingestion_engine.begin() as conn:
                if mutation == "change":
                    conn.execute(
                        text("UPDATE source_endpoint SET token='changed-token' WHERE id=:id"),
                        {"id": endpoint_id},
                    )
                else:
                    conn.execute(
                        text("DELETE FROM source_endpoint WHERE id=:id"),
                        {"id": endpoint_id},
                    )
            return self.outcome

    adapter = MutatingAdapter()
    result = asyncio.run(
        run_one_endpoint(
            engine=ingestion_engine,
            source_endpoint_id=endpoint_id,
            run_id=None,
            config_version="runner-v1",
            adapters=_registry(adapter, []),
            clock=_clock(),
        )
    )
    assert result.failure_code is expected
    assert result.ingestion_result is None
    with ingestion_engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM source_fetch WHERE run_id=:run_id"),
                {"run_id": result.run_id},
            ).scalar_one()
            == 0
        )


def test_no_database_connection_is_checked_out_during_adapter_await(
    ingestion_engine,
) -> None:
    endpoint_id = _seed(ingestion_engine)

    class InspectingAdapter(_Adapter):
        async def list_postings(self, endpoint, conditional):
            assert ingestion_engine.pool.checkedout() == 0
            await asyncio.sleep(0)
            assert ingestion_engine.pool.checkedout() == 0
            return self.outcome

    result = asyncio.run(
        run_one_endpoint(
            engine=ingestion_engine,
            source_endpoint_id=endpoint_id,
            run_id=None,
            config_version="runner-v1",
            adapters=_registry(InspectingAdapter(), []),
            clock=_clock(),
        )
    )
    assert result.run_status == "succeeded"


def test_binding_failure_rolls_back_new_run_and_returns_closed_database_error(
    ingestion_engine,
    monkeypatch,
) -> None:
    endpoint_id = _seed(ingestion_engine)

    def fail_validators(*args, **kwargs):
        raise OperationalError("validator", {}, Exception("private-db-sentinel"))

    monkeypatch.setattr(workflow_service, "validator_rows", fail_validators)
    with pytest.raises(RunnerDatabaseError) as caught:
        asyncio.run(
            run_one_endpoint(
                engine=ingestion_engine,
                source_endpoint_id=endpoint_id,
                run_id=None,
                config_version="binding-rollback-v1",
                adapters=_registry(_Adapter(), []),
                clock=_Clock(BASE),
            )
        )
    assert caught.value.code is RunnerFailureCode.DATABASE_ERROR
    assert caught.value.run_id is None
    assert "private-db-sentinel" not in str(caught.value)
    with ingestion_engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM pipeline_run WHERE config_version='binding-rollback-v1'")
            ).scalar_one()
            == 0
        )


def test_recheck_database_failure_leaves_reserved_run_retryable(
    ingestion_engine,
    monkeypatch,
) -> None:
    endpoint_id = _seed(ingestion_engine)
    original = workflow_service.lock_endpoint
    calls = 0

    def fail_second_lock(conn, selected_endpoint_id):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OperationalError("recheck", {}, Exception("private-db-sentinel"))
        return original(conn, selected_endpoint_id)

    monkeypatch.setattr(workflow_service, "lock_endpoint", fail_second_lock)
    with pytest.raises(RunnerDatabaseError) as caught:
        asyncio.run(
            run_one_endpoint(
                engine=ingestion_engine,
                source_endpoint_id=endpoint_id,
                run_id=None,
                config_version="recheck-rollback-v1",
                adapters=_registry(_Adapter(), []),
                clock=_Clock(
                    BASE,
                    BASE + timedelta(seconds=1),
                    BASE + timedelta(seconds=2),
                ),
            )
        )
    assert caught.value.code is RunnerFailureCode.DATABASE_ERROR
    assert caught.value.run_id is not None
    run = _run_row(ingestion_engine, caught.value.run_id)
    assert run["status"] == "running"
    assert run["counts"] == reserved_counts(endpoint_id, "greenhouse")
    with ingestion_engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM source_fetch WHERE run_id=:run_id"),
                {"run_id": caught.value.run_id},
            ).scalar_one()
            == 0
        )


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            IngestionDatabaseError("ingestion.database_error"),
            RunnerFailureCode.INGESTION_DATABASE_ERROR,
        ),
        (
            IngestionInputError("ingestion.invalid_run_id"),
            RunnerFailureCode.INGESTION_INPUT_ERROR,
        ),
        (
            IngestionTargetError("ingestion.run_not_found"),
            RunnerFailureCode.INGESTION_TARGET_ERROR,
        ),
    ],
)
def test_task006_failures_finalize_closed_without_attempt(
    ingestion_engine,
    monkeypatch,
    error: Exception,
    expected: RunnerFailureCode,
) -> None:
    endpoint_id = _seed(ingestion_engine)

    def fail_ingestion(*args, **kwargs):
        raise error

    monkeypatch.setattr(
        workflow_service.IngestionService,
        "ingest_fetch_result",
        fail_ingestion,
    )
    result = asyncio.run(
        run_one_endpoint(
            engine=ingestion_engine,
            source_endpoint_id=endpoint_id,
            run_id=None,
            config_version="runner-v1",
            adapters=_registry(_Adapter(), []),
            clock=_clock(),
        )
    )
    assert result.run_status == "failed"
    assert result.failure_code is expected
    assert result.ingestion_result is None
    run = _run_row(ingestion_engine, result.run_id)
    assert run["last_completed_step"] == "adapter_fetch_completed"
    assert run["error"] == {"code": expected.value}
    with ingestion_engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM source_fetch WHERE run_id=:run_id"),
                {"run_id": result.run_id},
            ).scalar_one()
            == 0
        )


def test_committed_attempt_survives_finalization_failure_and_retry_replays(
    ingestion_engine, monkeypatch
) -> None:
    endpoint_id = _seed(ingestion_engine)
    adapter = _Adapter()

    def fail_finalization(*args, **kwargs):
        raise OperationalError("finalize", {}, Exception("private-db-sentinel"))

    monkeypatch.setattr(workflow_service, "finish_run", fail_finalization)
    first = asyncio.run(
        run_one_endpoint(
            engine=ingestion_engine,
            source_endpoint_id=endpoint_id,
            run_id=None,
            config_version="runner-v1",
            adapters=_registry(adapter, []),
            clock=_clock(),
        )
    )
    assert first.run_status == "running"
    assert first.finalized is False
    assert first.failure_code is RunnerFailureCode.FINALIZATION_DATABASE_ERROR
    assert first.ingestion_result is not None
    run_id = first.run_id
    assert _run_row(ingestion_engine, run_id)["status"] == "running"

    monkeypatch.undo()
    replay = asyncio.run(
        run_one_endpoint(
            engine=ingestion_engine,
            source_endpoint_id=endpoint_id,
            run_id=run_id,
            config_version="runner-v1",
            adapters=AdapterRegistry(
                greenhouse=lambda: pytest.fail("factory called on retry"),
                lever=lambda: pytest.fail("factory called on retry"),
            ),
            clock=_Clock(BASE + timedelta(seconds=4)),
        )
    )
    assert replay.run_status == "succeeded"
    assert replay.finalized is True
    assert replay.ingestion_result is not None
    assert replay.ingestion_result.replayed is True


def test_logs_results_and_run_errors_redact_prohibited_sentinels(
    ingestion_engine,
    caplog,
) -> None:
    sentinels = (
        "private-company-sentinel",
        "private-token-sentinel",
        "https://private-url-sentinel.invalid/path",
        "private-provider-body-sentinel",
    )
    endpoint_id = _seed(
        ingestion_engine,
        company_name=sentinels[0],
        token=sentinels[1],
        base_url=sentinels[2],
    )
    adapter = _Adapter(RuntimeError(sentinels[3]))
    with caplog.at_level(logging.INFO):
        result = asyncio.run(
            run_one_endpoint(
                engine=ingestion_engine,
                source_endpoint_id=endpoint_id,
                run_id=None,
                config_version="runner-v1",
                adapters=_registry(adapter, []),
                clock=_Clock(
                    BASE,
                    BASE + timedelta(seconds=1),
                    BASE + timedelta(seconds=2),
                ),
            )
        )
    assert result.failure_code is RunnerFailureCode.ADAPTER_DEFECT
    run = _run_row(ingestion_engine, result.run_id)
    surfaced = f"{result!r}\n{json.dumps(run['counts'])}\n{json.dumps(run['error'])}\n{caplog.text}"
    for sentinel in sentinels:
        assert sentinel not in surfaced


def test_cancellation_finalizes_failed_and_propagates_original(
    ingestion_engine,
) -> None:
    endpoint_id = _seed(ingestion_engine)
    started = asyncio.Event()

    class BlockingAdapter(_Adapter):
        async def list_postings(self, endpoint, conditional):
            started.set()
            await asyncio.Event().wait()
            return self.outcome

    async def driver():
        task = asyncio.create_task(
            run_one_endpoint(
                engine=ingestion_engine,
                source_endpoint_id=endpoint_id,
                run_id=None,
                config_version="runner-v1",
                adapters=_registry(BlockingAdapter(), []),
                clock=_Clock(
                    BASE,
                    BASE + timedelta(seconds=1),
                    BASE + timedelta(seconds=2),
                ),
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(driver())
    with ingestion_engine.connect() as conn:
        run = dict(
            conn.execute(
                text(
                    """
                    SELECT * FROM pipeline_run
                    WHERE config_version='runner-v1'
                    ORDER BY id DESC LIMIT 1
                    """
                )
            )
            .mappings()
            .one()
        )
        fetch_count = conn.execute(
            text("SELECT count(*) FROM source_fetch WHERE run_id=:id"), {"id": run["id"]}
        ).scalar_one()
    assert run["status"] == "failed"
    assert run["last_completed_step"] == "endpoint_snapshotted"
    assert run["error"] == {"code": RunnerFailureCode.CANCELLED.value}
    assert fetch_count == 0


def test_concurrent_same_key_may_fetch_twice_but_task006_writes_once(
    ingestion_engine,
) -> None:
    endpoint_id = _seed(ingestion_engine)
    with ingestion_engine.begin() as conn:
        run_id = int(
            conn.execute(
                text(
                    """
                    INSERT INTO pipeline_run
                        (run_kind,status,config_version,counts,started_at)
                    VALUES
                        ('manual','running','runner-v1',CAST(:counts AS jsonb),:started)
                    RETURNING id
                    """
                ),
                {
                    "counts": json.dumps(reserved_counts(endpoint_id, "greenhouse")),
                    "started": BASE,
                },
            ).scalar_one()
        )
    barrier = threading.Barrier(2)
    adapter_calls = []
    factory_calls = []

    class ConcurrentAdapter(_Adapter):
        async def list_postings(self, endpoint, conditional):
            adapter_calls.append(1)
            barrier.wait(timeout=10)
            return self.outcome

    def factory():
        factory_calls.append(1)
        return ConcurrentAdapter()

    results = []
    errors = []

    def invoke(offset: int) -> None:
        try:
            results.append(
                asyncio.run(
                    run_one_endpoint(
                        engine=ingestion_engine,
                        source_endpoint_id=endpoint_id,
                        run_id=run_id,
                        config_version="runner-v1",
                        adapters=AdapterRegistry(
                            greenhouse=factory,
                            lever=lambda: pytest.fail("wrong factory"),
                        ),
                        clock=_Clock(
                            BASE + timedelta(seconds=1, milliseconds=offset),
                            BASE + timedelta(seconds=2, milliseconds=offset),
                            BASE + timedelta(seconds=3, milliseconds=offset),
                        ),
                    )
                )
            )
        except Exception as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=invoke, args=(offset,)) for offset in (0, 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
        assert not thread.is_alive()
    assert errors == []
    assert len(factory_calls) == 2
    assert len(adapter_calls) == 2
    assert len(results) == 2
    assert all(result.run_status == "succeeded" for result in results)
    assert sorted(result.ingestion_result.replayed for result in results) == [False, True]
    with ingestion_engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM source_fetch WHERE run_id=:id"), {"id": run_id}
            ).scalar_one()
            == 1
        )


def test_two_endpoints_racing_one_owned_run_allow_only_bound_endpoint(
    ingestion_engine,
) -> None:
    bound_endpoint_id = _seed(ingestion_engine)
    other_endpoint_id = _seed(ingestion_engine, token="invented-racing-endpoint-token")
    with ingestion_engine.begin() as conn:
        run_id = int(
            conn.execute(
                text(
                    """
                    INSERT INTO pipeline_run
                        (run_kind,status,config_version,counts,started_at)
                    VALUES
                        ('manual','running','runner-v1',CAST(:counts AS jsonb),:started)
                    RETURNING id
                    """
                ),
                {
                    "counts": json.dumps(reserved_counts(bound_endpoint_id, "greenhouse")),
                    "started": BASE,
                },
            ).scalar_one()
        )
    start = threading.Barrier(2)
    results = []
    errors = []

    def invoke(endpoint_id: int) -> None:
        start.wait(timeout=10)
        try:
            results.append(
                asyncio.run(
                    run_one_endpoint(
                        engine=ingestion_engine,
                        source_endpoint_id=endpoint_id,
                        run_id=run_id,
                        config_version="runner-v1",
                        adapters=_registry(_Adapter(), []),
                        clock=_Clock(
                            BASE + timedelta(seconds=1),
                            BASE + timedelta(seconds=2),
                            BASE + timedelta(seconds=3),
                        ),
                    )
                )
            )
        except Exception as exc:  # noqa: BLE001 - exact closed error asserted below
            errors.append(exc)

    threads = [
        threading.Thread(target=invoke, args=(bound_endpoint_id,)),
        threading.Thread(target=invoke, args=(other_endpoint_id,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
        assert not thread.is_alive()
    assert len(results) == 1
    assert results[0].source_endpoint_id == bound_endpoint_id
    assert results[0].run_status == "succeeded"
    assert len(errors) == 1
    assert isinstance(errors[0], RunnerTargetError)
    assert errors[0].code is RunnerFailureCode.PIPELINE_RUN_INCOMPATIBLE
    with ingestion_engine.connect() as conn:
        rows = list(
            conn.execute(
                text("SELECT endpoint_id FROM source_fetch WHERE run_id=:run_id"),
                {"run_id": run_id},
            ).scalars()
        )
    assert rows == [bound_endpoint_id]
