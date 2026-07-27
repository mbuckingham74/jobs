"""Network-isolated tests for the bounded Lever public-postings adapter."""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import logging
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
import pytest

from app.sources.ats import lever as lever_module
from app.sources.ats.contracts import ATSAdapter, ConditionalHeaders, FetchResult, SourceEndpoint
from app.sources.ats.lever import (
    LeverAdapter,
    LeverEndpointError,
    LeverError,
    LeverTransportError,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "lever"
GLOBAL_TOKEN = "synthetic-global-board"
EU_TOKEN = "synthetic-eu-board"
PAGE_TOKEN = "synthetic-page-board"
GLOBAL = SourceEndpoint(kind="lever", token=GLOBAL_TOKEN)
EU = SourceEndpoint(kind="lever", token=EU_TOKEN, region="eu")
MAX_BYTES = 10_485_760


def _fixture(name: str) -> bytes:
    return (FIXTURE_DIR / name).read_bytes()


def _client(handler, **kwargs) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)


def _row(
    external_id: str = "synthetic-001",
    *,
    token: str = GLOBAL_TOKEN,
    region: str = "global",
    **overrides,
) -> dict[str, object]:
    host = "jobs.lever.co" if region == "global" else "jobs.eu.lever.co"
    row: dict[str, object] = {
        "id": external_id,
        "text": f"Synthetic Role {external_id}",
        "description": "<p>Invented description.</p>",
        "lists": [{"text": "Responsibilities", "content": "<li>Invented duty</li>"}],
        "additional": "<p>Invented closing.</p>",
        "hostedUrl": f"https://{host}/{token}/{external_id}?src=test#posting",
        "applyUrl": f"https://{host}/{token}/{external_id}/apply?src=test#apply",
        "categories": {
            "allLocations": ["Synthetic City", "Remote", "Remote"],
            "location": "Unused fallback",
            "department": "Synthetic Engineering",
        },
        "country": "US",
        "workplaceType": "hybrid",
        "unknown": {"nested": ["invented", {"value": 7}]},
        "createdAt": 1234567890,
    }
    row.update(overrides)
    return row


def _body(rows: list[object]) -> bytes:
    return json.dumps(rows, ensure_ascii=False).encode()


async def _run(
    handler,
    *,
    endpoint: SourceEndpoint = GLOBAL,
    conditional: ConditionalHeaders | None = None,
    client: httpx.AsyncClient | None = None,
) -> FetchResult:
    adapter = LeverAdapter(client=client if client is not None else _client(handler))
    return await adapter.list_postings(endpoint, conditional or ConditionalHeaders())


def _parsed_request(request: httpx.Request) -> tuple[str, str, int | None, str, Counter]:
    url = request.url
    return (
        url.scheme,
        url.host,
        url.port,
        url.path,
        Counter(parse_qsl(url.query.decode(), keep_blank_values=True)),
    )


def _skip(request: httpx.Request) -> int:
    return int(dict(request.url.params)["skip"])


def _sequence_handler(bodies: dict[int, bytes], captures: list[httpx.Request] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if captures is not None:
            captures.append(request)
        return httpx.Response(200, content=bodies[_skip(request)])

    return handler


def test_adapter_protocol_surface() -> None:
    adapter = LeverAdapter()
    assert adapter.slug == "lever"
    assert adapter.rate_limit_per_min == 60
    assert isinstance(adapter, ATSAdapter)
    assert inspect.iscoroutinefunction(LeverAdapter.list_postings)


def test_errors_are_typed_and_bounded() -> None:
    error = LeverEndpointError("lever.endpoint_invalid_token")
    assert str(error) == "lever.endpoint_invalid_token"
    assert error.category == "lever.endpoint_invalid_token"
    assert issubclass(LeverEndpointError, LeverError)
    assert issubclass(LeverTransportError, LeverError)


@pytest.mark.parametrize(
    "endpoint,host",
    [
        (GLOBAL, "api.lever.co"),
        (EU, "api.eu.lever.co"),
    ],
)
def test_region_builds_exact_initial_url(endpoint: SourceEndpoint, host: str) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b"[]")

    result = asyncio.run(_run(handler, endpoint=endpoint))
    assert result.complete is True
    assert len(requests) == 1
    scheme, actual_host, port, path, query = _parsed_request(requests[0])
    assert scheme == "https"
    assert actual_host == host
    assert port is None
    assert path == f"/v0/postings/{endpoint.token}"
    assert query == Counter([("mode", "json"), ("skip", "0"), ("limit", "100")])
    assert Counter(parse_qsl(requests[0].url.query.decode())) == Counter(
        [("mode", "json"), ("skip", "0"), ("limit", "100")]
    )
    assert requests[0].url.fragment == ""
    assert requests[0].method == "GET"


@pytest.mark.parametrize(
    "token",
    ["f", "Abc012", "under_score", "hyphen-token", "A-_", "a" * 128],
)
def test_allowed_token_grammar_reaches_exact_path(token: str) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, content=b"[]")

    endpoint = SourceEndpoint(kind="lever", token=token)
    asyncio.run(_run(handler, endpoint=endpoint))
    assert captured[0].url.path == f"/v0/postings/{token}"


@pytest.mark.parametrize(
    "endpoint,category",
    [
        (SourceEndpoint(kind="greenhouse", token=GLOBAL_TOKEN), "lever.endpoint_invalid_kind"),
        (
            SourceEndpoint(kind="lever", token=GLOBAL_TOKEN, region="GLOBAL"),
            "lever.endpoint_invalid_region",
        ),
        (
            SourceEndpoint(kind="lever", token=GLOBAL_TOKEN, region=""),
            "lever.endpoint_invalid_region",
        ),
        (
            SourceEndpoint(kind="lever", token=GLOBAL_TOKEN, region="us"),
            "lever.endpoint_invalid_region",
        ),
        (
            SourceEndpoint(kind="lever", token=GLOBAL_TOKEN, base_url="https://api.lever.co"),
            "lever.endpoint_explicit_base_url",
        ),
        (SourceEndpoint(kind="lever", token=""), "lever.endpoint_invalid_token"),
        (SourceEndpoint(kind="lever", token="_bad"), "lever.endpoint_invalid_token"),
        (SourceEndpoint(kind="lever", token="-bad"), "lever.endpoint_invalid_token"),
        (SourceEndpoint(kind="lever", token="bad/token"), "lever.endpoint_invalid_token"),
        (SourceEndpoint(kind="lever", token="../bad"), "lever.endpoint_invalid_token"),
        (SourceEndpoint(kind="lever", token="bad?x=1"), "lever.endpoint_invalid_token"),
        (SourceEndpoint(kind="lever", token="bad#x"), "lever.endpoint_invalid_token"),
        (SourceEndpoint(kind="lever", token="bad%2fpath"), "lever.endpoint_invalid_token"),
        (SourceEndpoint(kind="lever", token="bad token"), "lever.endpoint_invalid_token"),
        (SourceEndpoint(kind="lever", token="bad.token"), "lever.endpoint_invalid_token"),
        (SourceEndpoint(kind="lever", token="tökën"), "lever.endpoint_invalid_token"),
        (SourceEndpoint(kind="lever", token="a" * 129), "lever.endpoint_invalid_token"),
        (
            SourceEndpoint(kind="lever", token=123),  # type: ignore[arg-type]
            "lever.endpoint_invalid_token",
        ),
    ],
)
def test_invalid_endpoint_rejected_before_network(endpoint: SourceEndpoint, category: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request: {request.method}")

    with pytest.raises(LeverEndpointError) as caught:
        asyncio.run(_run(handler, endpoint=endpoint))
    assert caught.value.category == category


def test_invalid_endpoint_does_not_construct_client(monkeypatch) -> None:
    def forbidden_client(*args, **kwargs):
        raise AssertionError("client constructed")

    monkeypatch.setattr(httpx, "AsyncClient", forbidden_client)
    with pytest.raises(LeverEndpointError):
        asyncio.run(
            LeverAdapter().list_postings(
                SourceEndpoint(kind="lever", token="bad/token"),
                ConditionalHeaders(),
            )
        )


def test_fixed_headers_and_owned_client_timeouts() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        captured["timeout"] = request.extensions["timeout"]
        return httpx.Response(200, content=b"[]")

    adapter = LeverAdapter(transport=httpx.MockTransport(handler))
    result = asyncio.run(adapter.list_postings(GLOBAL, ConditionalHeaders()))
    headers = captured["headers"]
    assert isinstance(headers, httpx.Headers)
    assert headers["user-agent"] == "ForksTech-Jobs/0.1 (+https://jobs.forkstech.com)"
    assert headers["accept"] == "application/json"
    assert captured["timeout"] == {
        "connect": 5.0,
        "read": 30.0,
        "write": 5.0,
        "pool": 5.0,
    }
    assert result.complete is True
    assert adapter._client is None


@pytest.mark.parametrize(
    "conditional,expected",
    [
        (ConditionalHeaders(), {}),
        (ConditionalHeaders(etag='"synthetic-etag"'), {"if-none-match": '"synthetic-etag"'}),
        (
            ConditionalHeaders(last_modified="Sat, 25 Jul 2026 00:00:00 GMT"),
            {"if-modified-since": "Sat, 25 Jul 2026 00:00:00 GMT"},
        ),
        (
            ConditionalHeaders(
                etag='"both"',
                last_modified="Sun, 26 Jul 2026 00:00:00 GMT",
            ),
            {
                "if-none-match": '"both"',
                "if-modified-since": "Sun, 26 Jul 2026 00:00:00 GMT",
            },
        ),
    ],
)
def test_conditional_headers_forwarded_exactly_on_first_page(
    conditional: ConditionalHeaders, expected: dict[str, str]
) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, content=b"[]")

    before = dataclasses.asdict(conditional)
    asyncio.run(_run(handler, conditional=conditional))
    assert dataclasses.asdict(conditional) == before
    for name in ("if-none-match", "if-modified-since"):
        if name in expected:
            assert captured[0].headers[name] == expected[name]
        else:
            assert name not in captured[0].headers


def test_validators_only_first_page_and_only_first_final_response() -> None:
    requests: list[httpx.Request] = []
    full = _body([_row(f"id-{index:03}") for index in range(100)])

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                302,
                headers={
                    "location": str(request.url),
                    "etag": '"redirect-hop"',
                    "last-modified": "redirect hop sentinel",
                },
            )
        if _skip(request) == 0:
            return httpx.Response(
                200,
                content=full,
                headers={"etag": '"first-final"', "last-modified": "first final"},
            )
        return httpx.Response(
            200,
            content=b"[]",
            headers={"etag": '"later-page"', "last-modified": "later page"},
        )

    conditional = ConditionalHeaders(etag='"stored"', last_modified="stored date")
    result = asyncio.run(_run(handler, conditional=conditional))
    assert result.complete is True
    assert result.etag == '"first-final"'
    assert result.last_modified == "first final"
    assert requests[0].headers["if-none-match"] == '"stored"'
    assert requests[1].headers["if-none-match"] == '"stored"'
    assert requests[1].headers["if-modified-since"] == "stored date"
    assert "if-none-match" not in requests[2].headers
    assert "if-modified-since" not in requests[2].headers


@pytest.mark.parametrize(
    "fixture_name,endpoint,expected_ids,host",
    [
        (
            "normal-global.json",
            GLOBAL,
            ["g-100", "g-200", "g-300"],
            "jobs.lever.co",
        ),
        (
            "normal-eu.json",
            EU,
            ["eu-10", "eu-20", "eu-30"],
            "jobs.eu.lever.co",
        ),
    ],
)
def test_normal_region_fixture_is_complete_and_separately_maps_urls(
    fixture_name: str,
    endpoint: SourceEndpoint,
    expected_ids: list[str],
    host: str,
) -> None:
    body = _fixture(fixture_name)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"etag": '"fixture-final"'})

    result = asyncio.run(_run(handler, endpoint=endpoint))
    assert result.complete is True
    assert result.http_status == 200
    assert [posting.external_id for posting in result.postings] == expected_ids
    assert all(urlparse_host(posting.posting_url) == host for posting in result.postings)
    assert all(urlparse_host(posting.apply_url) == host for posting in result.postings)
    assert all(posting.posting_url != posting.apply_url for posting in result.postings)
    source_by_id = {row["id"]: row for row in json.loads(body)}
    for posting in result.postings:
        assert posting.posting_url == source_by_id[posting.external_id]["hostedUrl"]
        assert posting.apply_url == source_by_id[posting.external_id]["applyUrl"]
        assert posting.source_published_at is None
        assert posting.source_updated_at is None


def urlparse_host(value: str) -> str:
    return httpx.URL(value).host


def test_empty_fixture_is_complete_empty_board() -> None:
    result = asyncio.run(_run(lambda request: httpx.Response(200, content=_fixture("empty.json"))))
    assert result == FetchResult(postings=[], complete=True, http_status=200)


def test_paginated_fixtures_advance_exact_skip_and_end_on_empty_page() -> None:
    requests: list[httpx.Request] = []
    bodies = {
        0: _fixture("paginated-page-1.json"),
        100: _fixture("paginated-page-2.json"),
        200: _fixture("paginated-terminal.json"),
    }
    endpoint = SourceEndpoint(kind="lever", token=PAGE_TOKEN)
    result = asyncio.run(_run(_sequence_handler(bodies, requests), endpoint=endpoint))
    assert result.complete is True
    assert len(result.postings) == 200
    assert [_skip(request) for request in requests] == [0, 100, 200]
    assert all(dict(request.url.params)["limit"] == "100" for request in requests)


def test_short_first_page_does_not_request_another_page() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=_body([_row("one"), _row("two")]))

    result = asyncio.run(_run(handler))
    assert result.complete is True
    assert calls == 1


def test_full_then_short_sorts_independently_of_page_order() -> None:
    full = [_row(f"z-{index:03}") for index in range(100)]
    short = [_row("a-final"), _row("m-final")]
    requests: list[httpx.Request] = []
    result = asyncio.run(_run(_sequence_handler({0: _body(full), 100: _body(short)}, requests)))
    assert result.complete is True
    ids = [posting.external_id for posting in result.postings]
    assert ids == sorted(ids)
    assert [_skip(request) for request in requests] == [0, 100]


def test_malformed_rows_count_toward_skip_and_later_valid_rows_survive() -> None:
    first = [_row(f"first-{index:03}") for index in range(98)]
    first.extend([None, {"id": "malformed"}])
    second = [_row("later-valid")]
    requests: list[httpx.Request] = []
    result = asyncio.run(_run(_sequence_handler({0: _body(first), 100: _body(second)}, requests)))
    assert result.complete is False
    assert result.http_status == 200
    assert len(result.postings) == 99
    assert result.postings[0].external_id.startswith(("first-", "later-"))
    assert any(posting.external_id == "later-valid" for posting in result.postings)
    assert [_skip(request) for request in requests] == [0, 100]


def test_later_all_invalid_short_page_retains_prior_valid_rows() -> None:
    first = [_row(f"valid-{index:03}") for index in range(100)]
    result = asyncio.run(_run(_sequence_handler({0: _body(first), 100: _body([None, 4, {}])})))
    assert result.complete is False
    assert len(result.postings) == 100


def test_repeated_decoded_page_at_different_skip_is_incomplete_without_append() -> None:
    rows = [_row(f"repeat-{index:03}") for index in range(100)]
    first = _body(rows)
    structurally_same = json.dumps(rows, indent=2).encode()
    result = asyncio.run(_run(_sequence_handler({0: first, 100: structurally_same})))
    assert result.complete is False
    assert result.http_status == 200
    assert len(result.postings) == 100


@pytest.mark.parametrize(
    "pages",
    [
        [[_row("duplicate"), _row("duplicate")]],
        [
            [_row(f"first-{index:03}") for index in range(99)] + [_row("duplicate")],
            [_row("duplicate", text="Changed synthetic title")],
        ],
    ],
)
def test_repeated_id_is_incomplete_and_conflicting_page_not_appended(
    pages: list[list[dict[str, object]]],
) -> None:
    bodies = {index * 100: _body(page) for index, page in enumerate(pages)}
    if len(pages) == 1:
        result = asyncio.run(_run(_sequence_handler(bodies)))
        assert result.postings == []
    else:
        result = asyncio.run(_run(_sequence_handler(bodies)))
        assert len(result.postings) == 100
    assert result.complete is False
    assert result.http_status == 200


def test_page_over_limit_is_incomplete_and_not_appended() -> None:
    result = asyncio.run(
        _run(lambda request: httpx.Response(200, content=_body([_row(str(i)) for i in range(101)])))
    )
    assert result.postings == []
    assert result.complete is False


def test_one_hundred_full_pages_hit_bounds_without_short_terminal() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        skip = _skip(request)
        return httpx.Response(
            200,
            content=_body([_row(f"bounded-{skip + index:05}") for index in range(100)]),
        )

    result = asyncio.run(_run(handler))
    assert calls == 100
    assert len(result.postings) == 10_000
    assert result.complete is False
    assert result.http_status == 200


@pytest.mark.parametrize("status", [304, 400, 404, 500, 503])
def test_first_page_non_success_is_incomplete_without_body_parse(status: int) -> None:
    sentinel = b"not-json-private-body-sentinel"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            content=sentinel,
            headers={"etag": '"status-etag"', "last-modified": "status date"},
        )

    result = asyncio.run(_run(handler))
    assert result.postings == []
    assert result.complete is False
    assert result.http_status == status
    assert result.etag == '"status-etag"'
    assert result.last_modified == "status date"


def test_later_304_retains_prior_rows_and_first_page_validators() -> None:
    full = _body([_row(f"prior-{index:03}") for index in range(100)])

    def handler(request: httpx.Request) -> httpx.Response:
        if _skip(request) == 0:
            return httpx.Response(200, content=full, headers={"etag": '"first"'})
        return httpx.Response(304, headers={"etag": '"later"'})

    result = asyncio.run(_run(handler))
    assert len(result.postings) == 100
    assert result.complete is False
    assert result.http_status == 304
    assert result.etag == '"first"'


def test_rate_limited_body_not_parsed_or_returned() -> None:
    result = asyncio.run(
        _run(
            lambda request: httpx.Response(
                429,
                content=_fixture("rate_limited.json"),
                headers={"content-type": "application/json"},
            )
        )
    )
    assert result == FetchResult(postings=[], complete=False, http_status=429)


def test_malformed_and_wrong_top_level_payloads_are_incomplete() -> None:
    for body in (_fixture("malformed.json"), b"{}", b"null", b'"array"', b"\xff"):
        result = asyncio.run(_run(lambda request, body=body: httpx.Response(200, content=body)))
        assert result.postings == []
        assert result.complete is False
        assert result.http_status == 200


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_each_supported_redirect_is_followed_with_same_page(status: int) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(status, headers={"location": str(request.url)})
        return httpx.Response(200, content=b"[]")

    result = asyncio.run(_run(handler))
    assert result.complete is True
    assert len(requests) == 2
    assert requests[1].url == requests[0].url


def test_relative_redirect_and_explicit_443_are_accepted() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                302,
                headers={"location": f"https://api.lever.co:443{request.url.raw_path.decode()}"},
            )
        return httpx.Response(200, content=b"[]")

    result = asyncio.run(_run(handler))
    assert result.complete is True
    assert len(requests) == 2
    assert requests[1].url.port is None


def test_exactly_three_redirects_are_followed() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls <= 3:
            return httpx.Response(307, headers={"location": str(request.url)})
        return httpx.Response(200, content=b"[]")

    result = asyncio.run(_run(handler))
    assert calls == 4
    assert result.complete is True


def test_fourth_redirect_is_not_followed() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(308, headers={"location": str(request.url)})

    result = asyncio.run(_run(handler))
    assert calls == 4
    assert result.complete is False
    assert result.http_status == 308


@pytest.mark.parametrize(
    "location",
    [
        "",
        "   ",
        "http://api.lever.co/v0/postings/synthetic-global-board?mode=json&skip=0&limit=100",
        "https://api.eu.lever.co/v0/postings/synthetic-global-board?mode=json&skip=0&limit=100",
        "https://api.lever.co:444/v0/postings/synthetic-global-board?mode=json&skip=0&limit=100",
        "https://user:pass@api.lever.co/v0/postings/synthetic-global-board?mode=json&skip=0&limit=100",
        "https://api.lever.co.example/v0/postings/synthetic-global-board?mode=json&skip=0&limit=100",
        "https://api.lever.co/v0/postings/changed?mode=json&skip=0&limit=100",
        "https://api.lever.co/v0/postings/synthetic-global-board/extra?mode=json&skip=0&limit=100",
        "https://api.lever.co/v0/postings/synthetic-global-board?mode=json&skip=1&limit=100",
        "https://api.lever.co/v0/postings/synthetic-global-board?mode=json&skip=0&limit=99",
        "https://api.lever.co/v0/postings/synthetic-global-board?mode=json&skip=0&limit=100&extra=1",
        "https://api.lever.co/v0/postings/synthetic-global-board?mode=json&mode=json&skip=0&limit=100",
        "https://api.lever.co/v0/postings/synthetic-global-board?mode=json&skip=0&limit=100#fragment",
        "https://api.lever.co:bad/v0/postings/synthetic-global-board?mode=json&skip=0&limit=100",
        "https://[broken/v0/postings/synthetic-global-board?mode=json&skip=0&limit=100",
    ],
)
def test_unsafe_redirect_is_rejected_without_second_request(location: str) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        headers = {"location": location} if location else {}
        return httpx.Response(302, headers=headers)

    result = asyncio.run(_run(handler))
    assert calls == 1
    assert result.complete is False
    assert result.http_status == 302


class _TrackingStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def test_declared_oversized_body_rejected_without_streaming() -> None:
    stream = _TrackingStream([b"must-not-be-read"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-length": str(MAX_BYTES + 1)},
            stream=stream,
        )

    result = asyncio.run(_run(handler))
    assert result.complete is False
    assert stream.closed is True


@pytest.mark.parametrize("declared", [None, "malformed", "-1", "0"])
def test_streamed_oversized_body_rejected_with_unusable_length(
    declared: str | None,
) -> None:
    stream = _TrackingStream([b"x" * MAX_BYTES, b"x"])
    headers = {} if declared is None else {"content-length": declared}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=headers, stream=stream)

    result = asyncio.run(_run(handler))
    assert result.complete is False
    assert result.http_status == 200
    assert stream.closed is True


def test_exact_per_page_ceiling_is_inclusive() -> None:
    body = b"[]" + b" " * (MAX_BYTES - 2)
    result = asyncio.run(_run(lambda request: httpx.Response(200, content=body)))
    assert result.complete is True
    assert result.postings == []


def test_exact_cumulative_ceiling_is_inclusive() -> None:
    first_json = _body([_row(f"exact-{index:03}") for index in range(100)])
    first = first_json + b" " * (MAX_BYTES - len(first_json) - 2)
    result = asyncio.run(_run(_sequence_handler({0: first, 100: b"[]"})))
    assert result.complete is True
    assert len(result.postings) == 100


def test_cumulative_overflow_retains_prior_page_and_is_incomplete() -> None:
    first_json = _body([_row(f"cumulative-{index:03}") for index in range(100)])
    first = first_json + b" " * (MAX_BYTES - len(first_json) - 1)
    result = asyncio.run(_run(_sequence_handler({0: first, 100: b"[]"})))
    assert result.complete is False
    assert result.http_status == 200
    assert len(result.postings) == 100


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", None),
        ("id", ""),
        ("id", "   "),
        ("text", None),
        ("text", ""),
        ("description", None),
        ("lists", {}),
        ("lists", [None]),
        ("lists", [{"text": 2, "content": "<li>x</li>"}]),
        ("lists", [{"text": "x", "content": 2}]),
        ("additional", 4),
        ("hostedUrl", None),
        ("applyUrl", None),
        ("hostedUrl", "http://jobs.lever.co/synthetic-global-board/synthetic-001"),
        (
            "applyUrl",
            "https://jobs.eu.lever.co/synthetic-global-board/synthetic-001/apply",
        ),
    ],
)
def test_required_row_fields_fail_independently(field: str, value: object) -> None:
    row = _row()
    row[field] = value
    result = asyncio.run(_run(lambda request: httpx.Response(200, content=_body([row]))))
    assert result.postings == []
    assert result.complete is False


@pytest.mark.parametrize(
    "url",
    [
        "https://user:pass@jobs.lever.co/synthetic-global-board/synthetic-001",
        "https://jobs.lever.co:444/synthetic-global-board/synthetic-001",
        "https://jobs.lever.co.example/synthetic-global-board/synthetic-001",
        "https://jobs.lever.co/other-board/synthetic-001",
        "https://jobs.lever.co/synthetic-global-board/other-id",
        "https://[broken/synthetic-global-board/synthetic-001",
    ],
)
def test_hosted_and_apply_urls_are_region_and_identity_bound(url: str) -> None:
    for field in ("hostedUrl", "applyUrl"):
        row = _row()
        row[field] = url
        result = asyncio.run(
            _run(
                lambda request, row=row: httpx.Response(
                    200,
                    content=_body([row]),
                )
            )
        )
        assert result.postings == []
        assert result.complete is False


def test_ids_titles_locations_and_optional_metadata_normalize_without_inference() -> None:
    row = _row(
        "  Case ID  ",
        text="  Senior   Synthetic Role  ",
        categories={
            "allLocations": ["  Alpha  ", "", "Alpha", "Beta", "Beta"],
            "location": "Fallback",
            "department": "  Product Programs  ",
            "team": "Ignored",
        },
        country="CA",
        workplaceType="remote",
    )
    row["hostedUrl"] = "https://jobs.lever.co/synthetic-global-board/Case ID?keep=yes#posting"
    row["applyUrl"] = "https://jobs.lever.co/synthetic-global-board/Case ID/apply?keep=yes#apply"
    result = asyncio.run(_run(lambda request: httpx.Response(200, content=_body([row]))))
    posting = result.postings[0]
    assert posting.external_id == "Case ID"
    assert posting.title == "Senior   Synthetic Role"
    assert posting.department == "Product Programs"
    normalized_locations = [
        (location.label, location.country_code, location.workplace_type)
        for location in posting.locations
    ]
    assert normalized_locations == [
        ("Alpha", "CA", "remote"),
        ("Beta", "CA", "remote"),
    ]
    assert all(location.city is None and location.region is None for location in posting.locations)


@pytest.mark.parametrize("country", [None, "us", "USA", "U1", "ÉU", 7])
@pytest.mark.parametrize("workplace", [None, "Remote", "distributed", 7])
def test_malformed_optional_country_and_workplace_degrade(
    country: object, workplace: object
) -> None:
    row = _row(country=country, workplaceType=workplace)
    result = asyncio.run(_run(lambda request: httpx.Response(200, content=_body([row]))))
    assert result.complete is True
    assert result.postings[0].locations[0].country_code is None
    assert result.postings[0].locations[0].workplace_type is None


@pytest.mark.parametrize("workplace", ["unspecified", "on-site", "remote", "hybrid"])
def test_documented_workplace_types_preserved(workplace: str) -> None:
    row = _row(workplaceType=workplace)
    result = asyncio.run(_run(lambda request: httpx.Response(200, content=_body([row]))))
    assert result.postings[0].locations[0].workplace_type == workplace


def test_location_fallback_and_no_synthetic_label() -> None:
    fallback = _row(categories={"allLocations": [None], "location": "  Fallback  "})
    no_label = _row(
        "no-location",
        categories={"allLocations": [], "location": " "},
        country="US",
        workplaceType="remote",
    )
    result = asyncio.run(
        _run(lambda request: httpx.Response(200, content=_body([fallback, no_label])))
    )
    assert result.complete is True
    postings = {posting.external_id: posting for posting in result.postings}
    assert [location.label for location in postings["synthetic-001"].locations] == ["Fallback"]
    assert postings["no-location"].locations == []


def test_raw_is_complete_deep_defensive_copy_and_timestamps_stay_none() -> None:
    row = _row()
    normalized = lever_module._normalize_posting(
        row,
        token=GLOBAL_TOKEN,
        region="global",
    )
    assert normalized is not None
    assert normalized.raw == row
    assert normalized.raw is not row
    assert normalized.raw["unknown"] is not row["unknown"]
    normalized.raw["unknown"]["nested"][1]["value"] = 99  # type: ignore[index]
    assert row["unknown"]["nested"][1]["value"] == 7  # type: ignore[index]
    assert normalized.source_published_at is None
    assert normalized.source_updated_at is None
    assert normalized.raw["createdAt"] == 1234567890


def test_markdown_golden_assembly_and_ignored_fields() -> None:
    sentinel = "SHOULD_NOT_APPEAR"
    row = _row(
        description=(
            "<h1>Résumé &amp; rôle</h1>\r\n"
            "<p>A <strong>bold</strong> and <em>soft</em> "
            '<a href="https://example.invalid/synthetic">link</a>.<br>Next</p>'
            "<ol><li>First</li><li>Second</li></ol>"
            "<script>SCRIPT_SENTINEL</script><style>STYLE_SENTINEL</style>"
        ),
        lists=[
            {
                "text": "  First\t section  ",
                "content": "<li>Alpha &amp; beta</li><li>Gamma</li>",
            },
            {"text": "Second section", "content": "<li>Delta</li>"},
        ],
        additional="<p>Closing<br>非ASCII</p>",
        opening=sentinel,
        descriptionBody=sentinel,
        additionalPlain=sentinel,
    )
    result = asyncio.run(_run(lambda request: httpx.Response(200, content=_body([row]))))
    markdown = result.postings[0].description_md
    assert markdown == (
        "# Résumé & rôle\n\n"
        "A **bold** and *soft* [link](https://example.invalid/synthetic).\n"
        "Next\n\n"
        "1. First\n"
        "2. Second\n\n"
        "## First section\n\n"
        "* Alpha & beta\n"
        "* Gamma\n\n"
        "## Second section\n\n"
        "* Delta\n\n"
        "Closing\n"
        "非ASCII"
    )
    assert sentinel not in markdown
    assert "SCRIPT_SENTINEL" not in markdown
    assert "STYLE_SENTINEL" not in markdown
    assert "\r" not in markdown
    assert not markdown.endswith("\n")


def test_empty_optional_markdown_sections_are_valid() -> None:
    row = _row(description="", lists=None, additional="")
    result = asyncio.run(_run(lambda request: httpx.Response(200, content=_body([row]))))
    assert result.complete is True
    assert result.postings[0].description_md == ""


@pytest.mark.parametrize(
    "exception,category",
    [
        (httpx.ConnectTimeout("PRIVATE_SENTINEL"), "lever.transport.connect_timeout"),
        (httpx.ReadTimeout("PRIVATE_SENTINEL"), "lever.transport.read_timeout"),
        (httpx.ConnectError("PRIVATE_SENTINEL"), "lever.transport.connect_error"),
        (httpx.ReadError("PRIVATE_SENTINEL"), "lever.transport.read_error"),
        (httpx.RemoteProtocolError("PRIVATE_SENTINEL"), "lever.transport.protocol_error"),
    ],
)
def test_transport_failures_raise_typed_redacted_exception(
    exception: httpx.HTTPError, category: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exception

    with pytest.raises(LeverTransportError) as caught:
        asyncio.run(_run(handler))
    assert str(caught.value) == category
    assert "PRIVATE_SENTINEL" not in str(caught.value)


def test_every_response_closes_on_success_and_redirect_rejection() -> None:
    responses: list[httpx.Response] = []

    def success_handler(request: httpx.Request) -> httpx.Response:
        response = httpx.Response(200, content=b"[]")
        responses.append(response)
        return response

    asyncio.run(_run(success_handler))

    def redirect_handler(request: httpx.Request) -> httpx.Response:
        response = httpx.Response(
            302,
            headers={"location": "https://example.invalid/private-sentinel"},
        )
        responses.append(response)
        return response

    asyncio.run(_run(redirect_handler))
    assert all(response.is_closed for response in responses)


class _CancellingStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        self.started.set()
        await asyncio.Event().wait()
        yield b"never"

    async def aclose(self) -> None:
        self.closed = True


async def _cancel_fetch() -> tuple[_CancellingStream, LeverAdapter]:
    stream = _CancellingStream()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    adapter = LeverAdapter(transport=httpx.MockTransport(handler))
    task = asyncio.create_task(adapter.list_postings(GLOBAL, ConditionalHeaders()))
    await stream.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    return stream, adapter


def test_cancellation_propagates_and_closes_response_and_owned_client() -> None:
    stream, adapter = asyncio.run(_cancel_fetch())
    assert stream.closed is True
    assert adapter._client is None


def test_injected_client_remains_open_but_responses_close() -> None:
    response = httpx.Response(200, content=b"[]")
    client = _client(lambda request: response)
    result = asyncio.run(_run(lambda request: response, client=client))
    assert result.complete is True
    assert response.is_closed is True
    assert client.is_closed is False
    asyncio.run(client.aclose())


def test_token_patterns_are_stable_and_capture_region() -> None:
    first = LeverAdapter().token_patterns()
    second = LeverAdapter().token_patterns()
    assert len(first) == 4
    assert first == second
    assert first is not second
    for pattern in first:
        assert "token" in pattern.groupindex
        assert "region" in pattern.groupindex


@pytest.mark.parametrize(
    "url,token,region",
    [
        ("https://jobs.lever.co/abc", "abc", ""),
        ("https://jobs.lever.co/abc/", "abc", ""),
        ("https://jobs.lever.co/abc/post-1", "abc", ""),
        ("https://jobs.lever.co/abc/post-1/apply", "abc", ""),
        ("https://jobs.lever.co/abc/post-1/apply/?q=1#x", "abc", ""),
        ("https://jobs.eu.lever.co/eu_board", "eu_board", "eu"),
        ("https://jobs.eu.lever.co/eu_board/id-2/apply?x=1#y", "eu_board", "eu"),
        ("https://api.lever.co/v0/postings/abc", "abc", ""),
        ("https://api.lever.co/v0/postings/abc/id-1?q=1#x", "abc", ""),
        ("https://api.eu.lever.co/v0/postings/eu_board/", "eu_board", "eu"),
        ("https://api.eu.lever.co/v0/postings/eu_board/id-2?mode=json", "eu_board", "eu"),
    ],
)
def test_token_pattern_positive_shapes(url: str, token: str, region: str) -> None:
    matches = [pattern.fullmatch(url) for pattern in LeverAdapter().token_patterns()]
    retained = [match for match in matches if match is not None]
    assert len(retained) == 1
    assert retained[0].group("token") == token
    assert retained[0].group("region") == region


@pytest.mark.parametrize(
    "url",
    [
        "http://jobs.lever.co/abc",
        "https://user@jobs.lever.co/abc",
        "https://jobs.lever.co:443/abc",
        "https://jobs.lever.co.example/abc",
        "https://jobslever.co/abc",
        "https://jobs.lever.co/",
        "https://jobs.lever.co/_bad",
        "https://jobs.lever.co/bad.token",
        "https://jobs.lever.co/abc/.",
        "https://jobs.lever.co/abc/..",
        "https://jobs.lever.co/abc/%2e",
        "https://jobs.lever.co/abc/%2E%2e",
        "https://jobs.lever.co/abc/id/extra",
        "https://jobs.lever.co/abc/id/apply/extra",
        "https://api.lever.co/v0/postings/abc/id/apply",
        "https://api.lever.co:444/v0/postings/abc",
        "https://api.lever.co.example/v0/postings/abc",
        "https://api.eu.lever.co/v0/postings/",
        "https://api.lever.co/v0/postings/abc/../other",
        "https://api.lever.co/v0/postings/abc/%2e%2e",
    ],
)
def test_token_pattern_boundary_negative_shapes(url: str) -> None:
    assert not any(pattern.fullmatch(url) for pattern in LeverAdapter().token_patterns())


def test_logs_and_exceptions_do_not_leak_private_sentinels(caplog) -> None:
    token_sentinel = "privateTokenSentinel"
    validator_sentinel = '"private-validator-sentinel"'
    body_sentinel = "private-body-sentinel"
    endpoint = SourceEndpoint(kind="lever", token=token_sentinel)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            content=body_sentinel.encode(),
            headers={"etag": validator_sentinel},
        )

    with caplog.at_level(logging.INFO, logger="app.sources.ats.lever"):
        result = asyncio.run(
            _run(
                handler,
                endpoint=endpoint,
                conditional=ConditionalHeaders(etag=validator_sentinel),
            )
        )
    assert result.complete is False
    rendered = "\n".join(f"{record.getMessage()} {record.__dict__!r}" for record in caplog.records)
    assert token_sentinel not in rendered
    assert validator_sentinel not in rendered
    assert body_sentinel not in rendered
    assert "https://api.lever.co" not in rendered
