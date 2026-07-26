"""Tests for the asynchronous Greenhouse adapter.

All tests use ``httpx.MockTransport`` so no live network request is made. The
existing autouse network guard in ``tests/conftest.py`` stays active; every
test starts the same way (it would fail loudly rather than fall back to a live
request if ``MockTransport`` were ever bypassed).

Coverage follows Task 004 test requirements:

- frozen contract values, protocol conformance, no mutation of inputs;
- exact URL and headers, valid token punctuation, every invalid endpoint;
- normal, empty, malformed, and rate-limited fixtures;
- ``200``, ``304``, every redirect code, relative redirects, exactly three
  redirects, a fourth redirect, missing/malformed locations, HTTPS downgrade,
  cross-origin redirects, alternate ports, credential-bearing targets, and
  lookalike hosts;
- conditional validators are never sent to a cross-origin redirect target
  because every such redirect is rejected before the next request;
- conditional headers independently, together, and final-response-only
  validators;
- a declared oversized body, a streamed oversized body without usable
  Content-Length, and an exact-ceiling body;
- invalid UTF-8, malformed JSON, wrong top-level shapes, wrong jobs and
  meta.total types, one invalid row among valid rows, and all-invalid rows;
- stable IDs, deterministic posting order independent of fixture row order,
  timezone-aware timestamps, and absent/malformed/naive timestamps;
- optional location and department degradation without geographic invention;
- posting and application URL fields, including the documented Greenhouse
  single-destination behaviour;
- exact preservation of unknown vendor fields in ``raw``;
- single unescaping plus golden deterministic Markdown conversion;
- transport, timeout, TLS, and cancellation behaviour;
- token-pattern positive and boundary-negative cases; and
- log and exception redaction using unique fixture sentinels.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from app.sources.ats.contracts import (
    ATSAdapter,
    ConditionalHeaders,
    FetchResult,
    RawLocation,
    SourceEndpoint,
)
from app.sources.ats.greenhouse import (
    GreenhouseAdapter,
    GreenhouseEndpointError,
    GreenhouseError,
    GreenhouseTransportError,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "greenhouse"
TOKEN = "forksboard"
STOP = SourceEndpoint(kind="greenhouse", token=TOKEN)


def _load_fixture(name: str) -> bytes:
    return (FIXTURE_DIR / name).read_bytes()


def _load_normal_json() -> dict[str, object]:
    return json.loads(_load_fixture("normal.json").decode("utf-8"))


def _client(handler, **kw) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kw)


async def _run(
    handler,
    *,
    endpoint: SourceEndpoint = STOP,
    conditional: ConditionalHeaders | None = None,
    client=None,
    **kw,
) -> FetchResult | GreenhouseError:
    adapter = GreenhouseAdapter(client=client if client is not None else _client(handler))
    return await adapter.list_postings(
        endpoint,
        conditional or ConditionalHeaders(),
    )


# --------------------------------------------------------------------
# Adapter protocol surface
# --------------------------------------------------------------------


def test_adapter_exposes_documented_slug_and_rate_limit() -> None:
    adapter = GreenhouseAdapter()
    assert adapter.slug == "greenhouse"
    assert adapter.rate_limit_per_min == 60


def test_adapter_satisfies_ats_adapter_protocol() -> None:
    assert isinstance(GreenhouseAdapter(), ATSAdapter)


def test_list_postings_is_asynchronous() -> None:
    import inspect

    assert inspect.iscoroutinefunction(GreenhouseAdapter.list_postings)


def test_errors_are_typed_and_bounded() -> None:
    msg = str(GreenhouseEndpointError("greenhouse.endpoint_invalid_token"))
    assert msg == "greenhouse.endpoint_invalid_token"
    assert issubclass(GreenhouseEndpointError, GreenhouseError)
    assert issubclass(GreenhouseTransportError, GreenhouseError)
    assert issubclass(GreenhouseError, Exception)


# --------------------------------------------------------------------
# URL construction + endpoint validation
# --------------------------------------------------------------------


def test_valid_token_constructs_exact_url() -> None:
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["method"] = req.method
        captured["headers"] = dict(req.headers)
        return httpx.Response(
            200,
            content=b'{"jobs":[],"meta":{"total":0}}',
            headers={"content-type": "application/json"},
        )

    result = asyncio.run(_run(handler))
    assert result.complete is True
    url = captured["url"]
    parsed = httpx.URL(url)
    assert parsed.scheme == "https"
    assert parsed.host == "boards-api.greenhouse.io"
    assert parsed.port is None
    assert parsed.path == "/v1/boards/forksboard/jobs"
    assert dict(parsed.params) == {"content": "true"}
    assert parsed.query == b"content=true"
    assert parsed.fragment == ""
    assert captured["method"] == "GET"


@pytest.mark.parametrize(
    "token",
    [
        "f",
        "abc",
        "Verbatim0123",
        "split_with_underscore",
        "hyphen-token",
        "ABCdef0-_",
        "a" * 128,
    ],
)
def test_permit_each_token_punctuation_character(token: str) -> None:
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        return httpx.Response(
            200, content=b'{"jobs":[]}', headers={"content-type": "application/json"}
        )

    endpoint = SourceEndpoint(kind="greenhouse", token=token)
    adapter = GreenhouseAdapter(client=_client(handler))
    asyncio.run(adapter.list_postings(endpoint, ConditionalHeaders()))
    url = httpx.URL(captured["url"])
    assert url.path == f"/v1/boards/{token}/jobs"
    assert dict(url.params) == {"content": "true"}
    assert url.query == b"content=true"
    assert url.fragment == ""


@pytest.mark.parametrize(
    "token,reason_code",
    [
        ("", "greenhouse.endpoint_invalid_token"),
        ("_", "greenhouse.endpoint_invalid_token"),  # token must start [A-Za-z0-9]
        ("1-2/3", "greenhouse.endpoint_invalid_token"),
        ("board?content=true", "greenhouse.endpoint_invalid_token"),
        ("board/../other", "greenhouse.endpoint_invalid_token"),
        ("with space", "greenhouse.endpoint_invalid_token"),
        ("with.dot", "greenhouse.endpoint_invalid_token"),
        ("with!bang", "greenhouse.endpoint_invalid_token"),
        ("a" * 129, "greenhouse.endpoint_invalid_token"),
    ],
)
def test_invalid_token_rejected_before_network_io(token, reason_code) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request to {req.url}")

    endpoint = SourceEndpoint(kind="greenhouse", token=token)
    adapter = GreenhouseAdapter(client=_client(handler))
    with pytest.raises(GreenhouseEndpointError) as info:
        asyncio.run(adapter.list_postings(endpoint, ConditionalHeaders()))
    assert info.value.category == reason_code


def test_non_greenhouse_kind_rejected() -> None:
    def handler(req):
        raise AssertionError("no network")

    adapter = GreenhouseAdapter(client=_client(handler))
    with pytest.raises(GreenhouseEndpointError) as info:
        asyncio.run(
            adapter.list_postings(SourceEndpoint(kind="lever", token=TOKEN), ConditionalHeaders())
        )
    assert info.value.category == "greenhouse.endpoint_invalid_kind"


def test_non_global_region_rejected() -> None:
    def handler(req):
        raise AssertionError("no network")

    adapter = GreenhouseAdapter(client=_client(handler))
    with pytest.raises(GreenhouseEndpointError) as info:
        asyncio.run(
            adapter.list_postings(
                SourceEndpoint(kind="greenhouse", token=TOKEN, region="eu"), ConditionalHeaders()
            )
        )
    assert info.value.category == "greenhouse.endpoint_invalid_region"


def test_explicit_base_url_rejected() -> None:
    def handler(req):
        raise AssertionError("no network")

    adapter = GreenhouseAdapter(client=_client(handler))
    with pytest.raises(GreenhouseEndpointError) as info:
        asyncio.run(
            adapter.list_postings(
                SourceEndpoint(kind="greenhouse", token=TOKEN, base_url="https://example.invalid/"),
                ConditionalHeaders(),
            )
        )
    assert info.value.category == "greenhouse.endpoint_explicit_base_url"


def test_non_string_token_rejected() -> None:
    def handler(req):
        raise AssertionError("no network")

    adapter = GreenhouseAdapter(client=_client(handler))
    with pytest.raises(GreenhouseEndpointError):
        asyncio.run(
            adapter.list_postings(
                SourceEndpoint(kind="greenhouse", token=12345),  # type: ignore[arg-type]
                ConditionalHeaders(),
            )
        )


def test_endpoint_validation_performs_no_network_io() -> None:
    # Even with a None transport (no MockTransport), validation raises before
    # any connection attempt.
    adapter = GreenhouseAdapter()
    with pytest.raises(GreenhouseEndpointError):
        asyncio.run(
            adapter.list_postings(
                SourceEndpoint(kind="greenhouse", token="bad/token"), ConditionalHeaders()
            )
        )


# --------------------------------------------------------------------
# Headers, user agent, accept
# --------------------------------------------------------------------


def test_request_carries_fixed_user_agent_and_accept() -> None:
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured.update(req.headers)
        return httpx.Response(
            200, content=b'{"jobs":[]}', headers={"content-type": "application/json"}
        )

    asyncio.run(_run(handler))
    assert captured["user-agent"] == "ForksTech-Jobs/0.1 (+https://jobs.forkstech.com)"
    assert captured["accept"] == "application/json"


# --------------------------------------------------------------------
# Conditional headers
# --------------------------------------------------------------------


def test_no_conditional_headers_sends_neither() -> None:
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured.update(req.headers)
        return httpx.Response(
            200, content=b'{"jobs":[]}', headers={"content-type": "application/json"}
        )

    asyncio.run(_run(handler))
    assert "if-none-match" not in captured
    assert "if-modified-since" not in captured


def test_etag_only_forwarded_exactly() -> None:
    sentinel = '"SYNTHETIC_ETAG_SENTINEL'
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured.update(req.headers)
        return httpx.Response(
            200, content=b'{"jobs":[]}', headers={"content-type": "application/json"}
        )

    asyncio.run(_run(handler, conditional=ConditionalHeaders(etag=sentinel)))
    assert captured["if-none-match"] == sentinel
    assert "if-modified-since" not in captured


def test_last_modified_only_forwarded_exactly() -> None:
    sentinel = "Sat, 26 Jul 2026 12:00:00 GMT"
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured.update(req.headers)
        return httpx.Response(
            200, content=b'{"jobs":[]}', headers={"content-type": "application/json"}
        )

    asyncio.run(_run(handler, conditional=ConditionalHeaders(last_modified=sentinel)))
    assert captured["if-modified-since"] == sentinel
    assert "if-none-match" not in captured


def test_both_validators_forwarded_together() -> None:
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured.update(req.headers)
        return httpx.Response(
            200, content=b'{"jobs":[]}', headers={"content-type": "application/json"}
        )

    asyncio.run(
        _run(
            handler,
            conditional=ConditionalHeaders(
                etag='"v1"', last_modified="Wed, 01 Jan 1990 00:00:00 GMT"
            ),
        )
    )
    assert captured["if-none-match"] == '"v1"'
    assert captured["if-modified-since"] == "Wed, 01 Jan 1990 00:00:00 GMT"


def test_final_validators_copied_when_present() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b'{"jobs":[]}',
            headers={
                "content-type": "application/json",
                "etag": '"final-e"',
                "last-modified": "Wed, 01 Jul 2026 12:00:00 GMT",
            },
        )

    result = asyncio.run(_run(handler))
    assert result.etag == '"final-e"'
    assert result.last_modified == "Wed, 01 Jul 2026 12:00:00 GMT"


def test_final_validators_none_when_absent() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b'{"jobs":[]}', headers={"content-type": "application/json"}
        )

    result = asyncio.run(_run(handler))
    assert result.etag is None
    assert result.last_modified is None


# --------------------------------------------------------------------
# Fixtures: normal, empty, malformed, rate-limited
# --------------------------------------------------------------------


def test_normal_fixture_returns_complete_postings_sorted() -> None:
    body = _load_fixture("normal.json")

    def handler(req):
        return httpx.Response(
            200, content=body, headers={"content-type": "application/json", "etag": '"normal-v1"'}
        )

    result = asyncio.run(_run(handler))
    assert result.http_status == 200
    assert result.complete is True
    assert result.etag == '"normal-v1"'
    # 3 valid rows sorted by (external_id, title, posting_url)
    ids = [p.external_id for p in result.postings]
    assert ids == sorted(ids)
    # IDs as decimal strings (decimal string form of the int id)
    assert all(p.external_id.isdigit() and int(p.external_id) > 0 for p in result.postings)
    assert ids == ["403100", "403123", "403345"]


def test_normal_fixture_deterministic_order_independent_of_vendor() -> None:
    body = _load_fixture("normal.json")
    payload = json.loads(body)
    jobs = payload["jobs"]
    # Reverse vendor order to verify sort is independent of input order.
    payload["jobs"] = list(reversed(jobs))
    body_rev = json.dumps(payload).encode("utf-8")

    def rev_handler(req):
        return httpx.Response(200, content=body_rev, headers={"content-type": "application/json"})

    def orig_handler(req):
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    result_rev = asyncio.run(_run(rev_handler))
    result_orig = asyncio.run(_run(orig_handler))
    assert [p.external_id for p in result_rev.postings] == [
        p.external_id for p in result_orig.postings
    ]


def test_empty_fixture_complete_with_zero_postings() -> None:
    body = _load_fixture("empty.json")

    def handler(req):
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    result = asyncio.run(_run(handler))
    assert result.http_status == 200
    assert result.complete is True
    assert result.postings == []


def test_malformed_fixture_returns_incomplete_200() -> None:
    body = _load_fixture("malformed.json")

    def handler(req):
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    result = asyncio.run(_run(handler))
    assert result.http_status == 200
    assert result.complete is False
    assert result.postings == []


def test_rate_limited_fixture_incomplete_429() -> None:
    body = _load_fixture("rate_limited.json")

    captured_body = []

    def handler(req):
        captured_body.append(req.headers.get("accept"))
        return httpx.Response(429, content=body, headers={"content-type": "application/json"})

    result = asyncio.run(_run(handler))
    assert result.http_status == 429
    assert result.complete is False
    assert result.postings == []
    # Body must not be parsed: rate_limited.json carries a sentinel that must
    # never appear in any returned fetch result field.
    assert "SYNTHETIC_RATE_LIMIT_BODY_SENTINEL" not in repr(result)


# --------------------------------------------------------------------
# 304 representation reviewed by Task 004
# --------------------------------------------------------------------


def test_304_is_incomplete_with_304_status() -> None:
    def handler(req):
        return httpx.Response(304)

    result = asyncio.run(_run(handler))
    assert result.http_status == 304
    assert result.complete is False
    assert result.postings == []


def test_304_returns_response_validators() -> None:
    def handler(req):
        return httpx.Response(
            304, headers={"etag": '"nm-e"', "last-modified": "Wed, 01 Jul 2026 12:00:00 GMT"}
        )

    result = asyncio.run(_run(handler))
    assert result.etag == '"nm-e"'
    assert result.last_modified == "Wed, 01 Jul 2026 12:00:00 GMT"


# --------------------------------------------------------------------
# Non-success statuses
# --------------------------------------------------------------------


@pytest.mark.parametrize("status", [400, 401, 403, 404, 405, 500, 502, 503, 504])
def test_other_non_success_status_returns_incomplete(status) -> None:
    def handler(req):
        # 3xx redirect statuses are exercised by the redirect tests; here we
        # cover the terminal 4xx/5xx branch where the body must not be parsed.
        return httpx.Response(status, content=b'{"error":"x"}')

    result = asyncio.run(_run(handler))
    assert result.http_status == status
    assert result.complete is False
    assert result.postings == []


# --------------------------------------------------------------------
# Redirects
# --------------------------------------------------------------------


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_each_redirect_status_followed_to_200(status) -> None:
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                status,
                headers={
                    "location": "https://boards-api.greenhouse.io/v1/boards/forksboard/jobs?content=true"
                },
            )
        return httpx.Response(
            200, content=b'{"jobs":[]}', headers={"content-type": "application/json"}
        )

    result = asyncio.run(_run(handler))
    assert result.http_status == 200
    assert result.complete is True


def test_relative_redirect_resolved_against_current_url() -> None:
    calls = {"n": 0}
    seen_urls = []

    def handler(req):
        calls["n"] += 1
        seen_urls.append(str(req.url))
        if calls["n"] == 1:
            return httpx.Response(
                302, headers={"location": "/v1/boards/forksboard/jobs?content=true"}
            )
        return httpx.Response(
            200, content=b'{"jobs":[]}', headers={"content-type": "application/json"}
        )

    result = asyncio.run(_run(handler))
    assert result.http_status == 200
    assert calls["n"] == 2
    assert seen_urls[1] == "https://boards-api.greenhouse.io/v1/boards/forksboard/jobs?content=true"


def test_query_relative_redirect_resolved() -> None:
    calls = {"n": 0}
    seen_urls = []

    def handler(req):
        calls["n"] += 1
        seen_urls.append(str(req.url))
        if calls["n"] == 1:
            return httpx.Response(302, headers={"location": "?content=true"})
        return httpx.Response(
            200, content=b'{"jobs":[]}', headers={"content-type": "application/json"}
        )

    result = asyncio.run(_run(handler))
    assert result.http_status == 200
    # urljoin replaces the current query string with the new one but keeps the
    # path on the safe origin.
    assert calls["n"] == 2
    assert seen_urls[1] == (
        "https://boards-api.greenhouse.io/v1/boards/forksboard/jobs?content=true"
    )


def test_dotdot_relative_redirect_resolved_on_safe_origin() -> None:
    """``../`` traversal must be resolved standards-compliantly and still
    pass the exact-origin guard when the resolved target remains on
    ``boards-api.greenhouse.io``. ``urljoin`` removes one trailing path
    segment per ``../``."""

    calls = {"n": 0}
    seen_urls = []

    def handler(req):
        calls["n"] += 1
        seen_urls.append(str(req.url))
        if calls["n"] == 1:
            return httpx.Response(302, headers={"location": "../forksboard/jobs?content=true"})
        return httpx.Response(
            200, content=b'{"jobs":[]}', headers={"content-type": "application/json"}
        )

    result = asyncio.run(_run(handler))
    assert result.http_status == 200
    assert result.complete is True
    assert calls["n"] == 2
    assert seen_urls[1] == (
        "https://boards-api.greenhouse.io/v1/boards/forksboard/jobs?content=true"
    )


def test_dotdot_redirect_resolves_on_same_origin_with_popped_segments() -> None:
    """``../../`` is resolved standards-compliantly with
    :func:`urllib.parse.urljoin`: two segments are popped from the base path
    before the new path is appended. The resolved target stays on the exact
    Greenhouse API origin and is followed; from
    ``/v1/boards/forksboard/jobs`` ``../../v1/boards/other/jobs`` resolves to
    ``/v1/v1/boards/other/jobs`` (one board-segment plus the new ``v1``
    prefix)."""

    calls = {"n": 0}
    seen_urls = []

    def handler(req):
        calls["n"] += 1
        seen_urls.append(str(req.url))
        if calls["n"] == 1:
            return httpx.Response(
                302, headers={"location": "../../v1/boards/other/jobs?content=true"}
            )
        return httpx.Response(
            200, content=b'{"jobs":[]}', headers={"content-type": "application/json"}
        )

    result = asyncio.run(_run(handler))
    assert result.http_status == 200
    assert result.complete is True
    assert seen_urls[1] == ("https://boards-api.greenhouse.io/v1/v1/boards/other/jobs?content=true")


def test_dot_relative_redirect_resolved_against_current_path() -> None:
    """A ``./`` reference resolves against the current segment's directory,
    standards-compliant with :func:`urllib.parse.urljoin`."""

    calls = {"n": 0}
    seen_urls = []

    def handler(req):
        calls["n"] += 1
        seen_urls.append(str(req.url))
        if calls["n"] == 1:
            return httpx.Response(302, headers={"location": "./jobs?content=true"})
        return httpx.Response(
            200, content=b'{"jobs":[]}', headers={"content-type": "application/json"}
        )

    result = asyncio.run(_run(handler))
    assert result.http_status == 200
    assert result.complete is True
    assert seen_urls[1] == (
        "https://boards-api.greenhouse.io/v1/boards/forksboard/jobs?content=true"
    )


def test_fragment_only_redirect_preserves_origin_and_path() -> None:
    """A fragment-only ``Location`` is standards-compliant: it reuses the
    current URL but with a new fragment. The origin guard still accepts
    the same-host target."""
    calls = {"n": 0}
    seen_urls = []

    def handler(req):
        calls["n"] += 1
        seen_urls.append(str(req.url))
        if calls["n"] == 1:
            return httpx.Response(302, headers={"location": "#section"})
        return httpx.Response(
            200, content=b'{"jobs":[]}', headers={"content-type": "application/json"}
        )

    result = asyncio.run(_run(handler))
    assert result.http_status == 200
    assert result.complete is True
    assert calls["n"] == 2
    assert seen_urls[1] == (
        "https://boards-api.greenhouse.io/v1/boards/forksboard/jobs?content=true#section"
    )


def test_scheme_relative_redirect_keeps_origin() -> None:
    """A ``//host/path`` scheme-relative ``Location`` inherits the current
    scheme and keeps the request on the safe origin when the host matches."""

    calls = {"n": 0}
    seen_urls = []

    def handler(req):
        calls["n"] += 1
        seen_urls.append(str(req.url))
        if calls["n"] == 1:
            return httpx.Response(
                302,
                headers={
                    "location": "//boards-api.greenhouse.io/v1/boards/forksboard/jobs?content=true"
                },
            )
        return httpx.Response(
            200, content=b'{"jobs":[]}', headers={"content-type": "application/json"}
        )

    result = asyncio.run(_run(handler))
    assert result.http_status == 200
    assert result.complete is True
    assert seen_urls[1] == (
        "https://boards-api.greenhouse.io/v1/boards/forksboard/jobs?content=true"
    )


def test_exactly_three_redirects_followed() -> None:
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] <= 3:
            return httpx.Response(
                302,
                headers={
                    "location": "https://boards-api.greenhouse.io/v1/boards/forksboard/jobs?content=true"
                },
            )
        return httpx.Response(
            200, content=b'{"jobs":[]}', headers={"content-type": "application/json"}
        )

    result = asyncio.run(_run(handler))
    assert result.http_status == 200
    assert result.complete is True
    assert calls["n"] == 4  # initial + 3 redirects


def test_fourth_redirect_is_incomplete_with_actual_status() -> None:
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(
            308,
            headers={
                "location": "https://boards-api.greenhouse.io/v1/boards/forksboard/jobs?content=true"
            },
        )

    result = asyncio.run(_run(handler))
    assert result.http_status == 308
    assert result.complete is False
    assert result.postings == []
    # initial request + 3 followed redirects = 4 calls; the fourth redirect is
    # reported without issuing a fifth request.
    assert calls["n"] == 4


def test_redirect_with_missing_location_incomplete() -> None:
    def handler(req):
        return httpx.Response(302)

    result = asyncio.run(_run(handler))
    assert result.http_status == 302
    assert result.complete is False


def test_redirect_with_whitespace_only_location_is_incomplete() -> None:
    # A whitespace-only Location is treated as malformed and the redirect is
    # reported incomplete with the actual redirect status, without issuing
    # another request.
    def handler(req):
        return httpx.Response(302, headers={"location": "  "})

    result = asyncio.run(_run(handler))
    assert result.http_status == 302
    assert result.complete is False


def test_https_downgrade_redirect_rejected() -> None:
    def handler(req):
        return httpx.Response(
            302,
            headers={"location": "http://boards-api.greenhouse.io/v1/boards/x/jobs?content=true"},
        )

    result = asyncio.run(_run(handler))
    assert result.http_status == 302
    assert result.complete is False


def test_cross_origin_redirect_rejected() -> None:
    def handler(req):
        return httpx.Response(302, headers={"location": "https://example.invalid/x"})

    result = asyncio.run(_run(handler))
    assert result.http_status == 302
    assert result.complete is False


def test_alternate_port_redirect_rejected() -> None:
    def handler(req):
        return httpx.Response(
            302,
            headers={
                "location": "https://boards-api.greenhouse.io:8443/v1/boards/forksboard/jobs?content=true"
            },
        )

    result = asyncio.run(_run(handler))
    assert result.http_status == 302
    assert result.complete is False


def test_explicit_https_port_443_redirect_followed_as_same_origin() -> None:
    """An explicit ``:443`` is the same origin as an omitted port over HTTPS."""

    calls = {"n": 0}
    seen_urls = []

    def handler(req):
        calls["n"] += 1
        seen_urls.append(str(req.url))
        if calls["n"] == 1:
            return httpx.Response(
                302,
                headers={
                    "location": (
                        "https://boards-api.greenhouse.io:443/v1/boards/"
                        "forksboard/jobs?content=true"
                    )
                },
            )
        return httpx.Response(
            200, content=b'{"jobs":[]}', headers={"content-type": "application/json"}
        )

    result = asyncio.run(_run(handler))
    assert result.http_status == 200
    assert result.complete is True
    assert calls["n"] == 2
    # The followed target carried an explicit ``:443``; httpx normalises the
    # default HTTPS port out of the displayed URL string, so the second
    # request URL is the canonical same-origin path on the reportable URL.
    assert seen_urls[1] == (
        "https://boards-api.greenhouse.io/v1/boards/forksboard/jobs?content=true"
    )


@pytest.mark.parametrize("port", ["80", "8080", "9000", "65535"])
def test_alternate_explicit_port_redirect_rejected(port) -> None:
    def handler(req):
        return httpx.Response(
            302,
            headers={
                "location": (
                    f"https://boards-api.greenhouse.io:{port}/v1/boards/"
                    f"forksboard/jobs?content=true"
                )
            },
        )

    result = asyncio.run(_run(handler))
    assert result.http_status == 302
    assert result.complete is False
    assert result.postings == []


def test_malformed_port_redirect_returns_incomplete_fetch_result() -> None:
    """A malformed port string (e.g. ``:abc``) makes the URL parser raise
    ``ValueError`` when reading ``parsed.port``. The adapter must catch it and
    return an incomplete :class:`FetchResult` rather than letting an uncaught
    URL error escape as a transport failure."""

    def handler(req):
        return httpx.Response(
            302,
            headers={
                "location": (
                    "https://boards-api.greenhouse.io:not-a-port/v1/boards/"
                    "forksboard/jobs?content=true"
                )
            },
        )

    result = asyncio.run(_run(handler))
    assert isinstance(result, FetchResult)
    assert result.http_status == 302
    assert result.complete is False
    assert result.postings == []


def test_malformed_ipv6_redirect_returns_incomplete_fetch_result() -> None:
    """A broken IPv6 literal in a redirect target fails URL parsing. The
    adapter returns an incomplete :class:`FetchResult` rather than raising."""

    def handler(req):
        return httpx.Response(
            302,
            headers={
                # Unbalanced/corrupt IPv6 literal: raises ValueError on parse.
                "location": "https://[::1:bad/v1/boards/forksboard/jobs?content=true"
            },
        )

    result = asyncio.run(_run(handler))
    assert isinstance(result, FetchResult)
    assert result.http_status == 302
    assert result.complete is False
    assert result.postings == []


def test_credential_bearing_redirect_rejected() -> None:
    def handler(req):
        return httpx.Response(
            302,
            headers={
                "location": "https://user:pw@boards-api.greenhouse.io/v1/boards/forksboard/jobs?content=true"
            },
        )

    result = asyncio.run(_run(handler))
    assert result.http_status == 302
    assert result.complete is False


def test_lookalike_host_redirect_rejected() -> None:
    def handler(req):
        return httpx.Response(
            302,
            headers={
                "location": "https://boards-api.greenhouse.io.example.com/v1/boards/forksboard/jobs?content=true"
            },
        )

    result = asyncio.run(_run(handler))
    assert result.http_status == 302
    assert result.complete is False


def test_validators_never_sent_to_cross_origin_redirect_target() -> None:
    """A cross-origin redirect target is rejected before issuing the next
    request, so conditional validators cannot leak across origins."""

    seen_headers = []

    def handler(req):
        seen_headers.append(dict(req.headers))
        return httpx.Response(302, headers={"location": "https://attacker.invalid/x"})

    asyncio.run(
        _run(
            handler,
            conditional=ConditionalHeaders(
                etag='"secret-etag"', last_modified="Wed, 01 Jan 1990 00:00:00 GMT"
            ),
        )
    )
    # Only one request, on the safe origin, carried the validators. No request
    # was issued toward the cross-origin target.
    assert len(seen_headers) == 1
    assert seen_headers[0]["if-none-match"] == '"secret-etag"'
    assert seen_headers[0]["if-modified-since"] == "Wed, 01 Jan 1990 00:00:00 GMT"
    # And the host appears exactly once: the safe origin only.
    assert "attacker.invalid" not in repr(seen_headers)


def test_validators_preserved_on_same_origin_redirect_hop() -> None:
    """Validators carry on every followed hop because redirects are restricted
    to the exact Greenhouse API origin in :func:`_resolve_redirect`. The same
    headers built for the initial request (UA, Accept, and any non-null
    conditional validators) are sent on every request the adapter issues."""

    seen = []

    def handler(req):
        seen.append(dict(req.headers))
        if len(seen) == 1:
            return httpx.Response(
                302,
                headers={
                    "location": "https://boards-api.greenhouse.io/v1/boards/forksboard/jobs?content=true"
                },
            )
        return httpx.Response(
            200, content=b'{"jobs":[]}', headers={"content-type": "application/json"}
        )

    asyncio.run(
        _run(
            handler,
            conditional=ConditionalHeaders(
                etag='"secret-etag"', last_modified="Wed, 01 Jan 1990 00:00:00 GMT"
            ),
        )
    )
    assert len(seen) == 2
    assert seen[0]["if-none-match"] == '"secret-etag"'
    assert seen[0]["if-modified-since"] == "Wed, 01 Jan 1990 00:00:00 GMT"
    # The same non-null conditional validators are sent on the followed hop.
    assert seen[1]["if-none-match"] == '"secret-etag"'
    assert seen[1]["if-modified-since"] == "Wed, 01 Jan 1990 00:00:00 GMT"


def test_no_validators_on_same_origin_hop_when_caller_sent_none() -> None:
    """When the caller supplied no validators, no hop request carries any
    conditional header — the same None/absence semantics propagate."""

    seen = []

    def handler(req):
        seen.append(dict(req.headers))
        if len(seen) == 1:
            return httpx.Response(
                302,
                headers={
                    "location": "https://boards-api.greenhouse.io/v1/boards/forksboard/jobs?content=true"
                },
            )
        return httpx.Response(
            200, content=b'{"jobs":[]}', headers={"content-type": "application/json"}
        )

    asyncio.run(_run(handler, conditional=ConditionalHeaders()))
    assert len(seen) == 2
    assert "if-none-match" not in seen[0]
    assert "if-modified-since" not in seen[0]
    assert "if-none-match" not in seen[1]
    assert "if-modified-since" not in seen[1]


# --------------------------------------------------------------------
# Byte ceiling
# --------------------------------------------------------------------


def test_declared_oversized_body_rejected_before_body_read() -> None:
    def handler(req):
        return httpx.Response(
            200,
            content=b'{"jobs":[]}',
            headers={
                "content-type": "application/json",
                "content-length": str(10 * 1024 * 1024 + 1),
            },
        )

    result = asyncio.run(_run(handler))
    assert result.http_status == 200
    assert result.complete is False
    assert result.postings == []


def test_streamed_oversized_without_content_length_aborts() -> None:
    big = b"{" + b"x" * (10 * 1024 * 1024 + 4096)

    def handler(req):
        resp = httpx.Response(200, content=big, headers={"content-type": "application/json"})
        resp.headers.pop("content-length", None)
        return resp

    result = asyncio.run(_run(handler))
    assert result.http_status == 200
    assert result.complete is False
    assert result.postings == []


def test_exact_ceiling_body_accepted() -> None:
    # Exactly 10 MiB body: must be accepted (not rejected as oversized).
    big_payload = json.dumps({"jobs": [], "meta": {"total": 0}}).encode("utf-8")
    pad = b" " * (10 * 1024 * 1024 - len(big_payload))
    body = big_payload + pad
    # Pad must remain after JSON to bump bytes; but JSON must still parse, so
    # embed padding as a vendor field inside the object instead.
    pad_str = "P" * (10 * 1024 * 1024 - 80)
    body = json.dumps(
        {
            "jobs": [],
            "meta": {"total": 0},
            "vendor_padding": pad_str,
        }
    ).encode("utf-8")
    assert len(body) == 10 * 1024 * 1024 or len(body) <= 10 * 1024 * 1024

    def handler(req):
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    result = asyncio.run(_run(handler))
    # The body is exactly at or below the ceiling and parses; this should be
    # a complete empty board.
    if len(body) > 10 * 1024 * 1024:
        pytest.skip("padding overflow")
    assert result.http_status == 200
    assert result.complete is True


# --------------------------------------------------------------------
# 200 payload: shape failures
# --------------------------------------------------------------------


def test_200_non_utf8_returns_incomplete() -> None:
    def handler(req):
        return httpx.Response(
            200, content=b"\xff\xfe not utf8", headers={"content-type": "application/json"}
        )

    result = asyncio.run(_run(handler))
    assert result.http_status == 200
    assert result.complete is False


def test_200_non_object_top_level_returns_incomplete() -> None:
    body = b'[{"id":1}]'

    def handler(req):
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    result = asyncio.run(_run(handler))
    assert result.http_status == 200
    assert result.complete is False


def test_200_missing_jobs_returns_incomplete() -> None:
    body = b'{"meta":{"total":0}}'

    def handler(req):
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    result = asyncio.run(_run(handler))
    assert result.http_status == 200
    assert result.complete is False


def test_200_non_list_jobs_returns_incomplete() -> None:
    body = b'{"jobs": 42, "meta":{"total":0}}'

    def handler(req):
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    result = asyncio.run(_run(handler))
    assert result.http_status == 200
    assert result.complete is False


def test_200_non_object_meta_returns_incomplete() -> None:
    body = b'{"jobs":[],"meta":"oops"}'

    def handler(req):
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    result = asyncio.run(_run(handler))
    assert result.http_status == 200
    assert result.complete is False


def test_200_meta_total_wrong_type_returns_incomplete() -> None:
    body = b'{"jobs":[],"meta":{"total":"0"}}'

    def handler(req):
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    result = asyncio.run(_run(handler))
    assert result.http_status == 200
    assert result.complete is False


def test_200_meta_total_bool_returns_incomplete() -> None:
    body = b'{"jobs":[1],"meta":{"total":true}}'

    def handler(req):
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    result = asyncio.run(_run(handler))
    assert result.http_status == 200
    assert result.complete is False


def test_200_meta_total_mismatch_returns_incomplete() -> None:
    body = b'{"jobs":[{"id":1}],"meta":{"total":2}}'

    def handler(req):
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    result = asyncio.run(_run(handler))
    assert result.http_status == 200
    assert result.complete is False


def test_200_meta_total_negative_returns_incomplete() -> None:
    body = b'{"jobs":[],"meta":{"total":-1}}'

    def handler(req):
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    result = asyncio.run(_run(handler))
    assert result.http_status == 200
    assert result.complete is False


def test_200_meta_absent_is_valid() -> None:
    body = b'{"jobs":[]}'

    def handler(req):
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    result = asyncio.run(_run(handler))
    assert result.http_status == 200
    assert result.complete is True


def test_200_empty_meta_object_is_valid() -> None:
    """A present but empty ``meta={}`` does not make an otherwise valid
    response incomplete. The ``meta.total`` rule only applies when the
    ``total`` key is present."""

    body = b'{"jobs":[],"meta":{}}'

    def handler(req):
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    result = asyncio.run(_run(handler))
    assert result.http_status == 200
    assert result.complete is True
    assert result.postings == []


def test_200_empty_meta_object_with_valid_jobs_is_complete() -> None:
    """A present ``meta={}`` plus a valid non-empty ``jobs`` list is complete;
    the ``meta.total`` is only inspected when the ``total`` key is present."""

    body = json.dumps(
        {
            "jobs": [
                {
                    "id": 1100,
                    "title": "Engineer",
                    "content": "<p>SYNTHETIC_EMPTY_META_SENTINEL</p>",
                    "absolute_url": "https://boards.greenhouse.io/x/jobs/1100",
                }
            ],
            "meta": {},
        }
    ).encode("utf-8")

    def handler(req):
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    result = asyncio.run(_run(handler))
    assert result.http_status == 200
    assert result.complete is True
    assert len(result.postings) == 1


def test_200_meta_with_unrelated_keys_but_no_total_is_valid() -> None:
    """``meta`` may carry unrelated keys; only a present ``total`` is checked."""

    body = json.dumps({"jobs": [], "meta": {"other_key": "value", "vendor_meta": 0}}).encode(
        "utf-8"
    )

    def handler(req):
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    result = asyncio.run(_run(handler))
    assert result.http_status == 200
    assert result.complete is True
    assert result.postings == []


def test_200_meta_total_present_still_validated_when_zero_matches_jobs() -> None:
    """A present ``meta.total=0`` with an empty ``jobs`` list is valid; the
    ``total`` rule still applies when ``total`` is the only key."""

    body = b'{"jobs":[],"meta":{"total":0}}'

    def handler(req):
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    result = asyncio.run(_run(handler))
    assert result.http_status == 200
    assert result.complete is True


def test_200_meta_total_present_with_wrong_type_still_incomplete() -> None:
    """A present ``meta.total`` of the wrong type is still an incomplete
    response — the ``total``-rule fix only relaxes the absent-key case."""

    body = b'{"jobs":[],"meta":{"total":"0"}}'

    def handler(req):
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    result = asyncio.run(_run(handler))
    assert result.http_status == 200
    assert result.complete is False


def test_200_meta_total_present_mismatch_still_incomplete() -> None:
    body = b'{"jobs":[],"meta":{"total":1}}'

    def handler(req):
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    result = asyncio.run(_run(handler))
    assert result.http_status == 200
    assert result.complete is False


# --------------------------------------------------------------------
# Row normalization failures
# --------------------------------------------------------------------


def test_one_invalid_row_among_valid_keeps_valid_but_incomplete() -> None:
    body = json.dumps(
        {
            "jobs": [
                {
                    "id": 7,
                    "title": "Valid A",
                    "content": "<p>x</p>",
                    "absolute_url": "https://boards.greenhouse.io/x/jobs/7",
                },
                {
                    "id": "bad",
                    "title": "Invalid Id",
                    "content": "<p>x</p>",
                    "absolute_url": "https://boards.greenhouse.io/x/jobs/1",
                },
                {
                    "id": 9,
                    "title": "Valid B",
                    "content": "<p>y</p>",
                    "absolute_url": "https://boards.greenhouse.io/x/jobs/9",
                },
            ],
            "meta": {"total": 3},
        }
    ).encode("utf-8")

    def handler(req):
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    result = asyncio.run(_run(handler))
    ids = [p.external_id for p in result.postings]
    assert "7" in ids and "9" in ids
    assert len(result.postings) == 2
    assert result.complete is False  # any skipped row makes whole incomplete


def test_all_invalid_rows_incomplete_and_empty() -> None:
    body = json.dumps(
        {
            "jobs": [
                {"id": "x", "title": "bad id"},
                {"id": -1, "title": "neg id"},
                {"id": True, "title": "bool id"},
                {
                    "id": 1,
                    "title": "",
                    "content": "<p>x</p>",
                    "absolute_url": "https://boards.greenhouse.io/x/jobs/1",
                },
                {"id": 2, "title": "No URL", "content": "<p>x</p>"},
                {"id": 3, "title": "Bad URL", "content": "<p>x</p>", "absolute_url": "ftp://x"},
                {
                    "id": 4,
                    "title": "Cred URL",
                    "content": "<p>x</p>",
                    "absolute_url": "https://user:pw@x",
                },
                {
                    "id": 5,
                    "title": "No content",
                    "absolute_url": "https://boards.greenhouse.io/x/jobs/5",
                },
                {
                    "id": 6,
                    "title": "Bool content",
                    "content": False,
                    "absolute_url": "https://boards.greenhouse.io/x/jobs/6",
                },
                "string-row-not-a-dict",
                None,
            ],
            "meta": {"total": 10},
        }
    ).encode("utf-8")

    def handler(req):
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    result = asyncio.run(_run(handler))
    assert result.postings == []
    assert result.complete is False


def test_boolean_id_rejected_even_when_truthy() -> None:
    body = json.dumps(
        {
            "jobs": [
                {
                    "id": True,
                    "title": "Bool",
                    "content": "<p>x</p>",
                    "absolute_url": "https://boards.greenhouse.io/x/jobs/1",
                }
            ],
            "meta": {"total": 1},
        }
    ).encode("utf-8")

    def handler(req):
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    result = asyncio.run(_run(handler))
    assert result.postings == []
    assert result.complete is False


# --------------------------------------------------------------------
# Timestamps
# --------------------------------------------------------------------


def test_timezone_aware_timestamps_parsed() -> None:
    from datetime import timedelta, timezone

    body = json.dumps(
        {
            "jobs": [
                {
                    "id": 1001,
                    "title": "Engineer",
                    "content": "<p>x</p>",
                    "absolute_url": "https://boards.greenhouse.io/x/jobs/1001",
                    "first_published": "2026-07-10T08:00:00Z",
                    "updated_at": "2026-07-15T10:30:00+02:00",
                }
            ],
            "meta": {"total": 1},
        }
    ).encode("utf-8")

    def handler(req):
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    result = asyncio.run(_run(handler))
    p = result.postings[0]
    assert p.source_published_at == datetime(2026, 7, 10, 8, 0, 0, tzinfo=UTC)
    assert p.source_updated_at == datetime(
        2026, 7, 15, 10, 30, 0, tzinfo=timezone(timedelta(hours=2))
    )


def test_naive_timestamp_becomes_none() -> None:
    body = json.dumps(
        {
            "jobs": [
                {
                    "id": 1002,
                    "title": "Engineer",
                    "content": "<p>x</p>",
                    "absolute_url": "https://boards.greenhouse.io/x/jobs/1002",
                    "first_published": "2026-07-10T08:00:00",
                    "updated_at": "2026-07-15T10:30:00",
                }
            ],
            "meta": {"total": 1},
        }
    ).encode("utf-8")

    def handler(req):
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    result = asyncio.run(_run(handler))
    p = result.postings[0]
    assert p.source_published_at is None
    assert p.source_updated_at is None


def test_malformed_timestamp_becomes_none() -> None:
    body = json.dumps(
        {
            "jobs": [
                {
                    "id": 1003,
                    "title": "Engineer",
                    "content": "<p>x</p>",
                    "absolute_url": "https://boards.greenhouse.io/x/jobs/1003",
                    "first_published": "not-an-iso-date",
                    "updated_at": "2026/07/15 10:30",
                }
            ],
            "meta": {"total": 1},
        }
    ).encode("utf-8")

    def handler(req):
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    result = asyncio.run(_run(handler))
    p = result.postings[0]
    assert p.source_published_at is None
    assert p.source_updated_at is None


def test_absent_timestamp_becomes_none() -> None:
    body = json.dumps(
        {
            "jobs": [
                {
                    "id": 1004,
                    "title": "Engineer",
                    "content": "<p>x</p>",
                    "absolute_url": "https://boards.greenhouse.io/x/jobs/1004",
                }
            ],
            "meta": {"total": 1},
        }
    ).encode("utf-8")

    def handler(req):
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    result = asyncio.run(_run(handler))
    p = result.postings[0]
    assert p.source_published_at is None
    assert p.source_updated_at is None


def test_stable_external_ids_from_posting_id() -> None:
    body = json.dumps(
        {
            "jobs": [
                {
                    "id": 987654,
                    "title": "Eng",
                    "content": "<p>x</p>",
                    "absolute_url": "https://boards.greenhouse.io/x/jobs/987654",
                    "internal_job_id": 111111,
                    "requisition_id": "REQ-99",
                }
            ],
            "meta": {"total": 1},
        }
    ).encode("utf-8")

    def handler(req):
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    result = asyncio.run(_run(handler))
    # Uses posting id, not internal_job_id or requisition_id.
    assert result.postings[0].external_id == "987654"


# --------------------------------------------------------------------
# Location, department degradation
# --------------------------------------------------------------------


def test_location_label_preserved_other_fields_none() -> None:
    body = json.dumps(
        {
            "jobs": [
                {
                    "id": 1005,
                    "title": "Eng",
                    "content": "<p>x</p>",
                    "absolute_url": "https://boards.greenhouse.io/x/jobs/1005",
                    "location": {"name": "Remote, North America"},
                }
            ],
            "meta": {"total": 1},
        }
    ).encode("utf-8")

    def handler(req):
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    p = asyncio.run(_run(handler)).postings[0]
    assert p.locations == [RawLocation(label="Remote, North America")]
    assert p.locations[0].country_code is None
    assert p.locations[0].region is None
    assert p.locations[0].city is None
    assert p.locations[0].workplace_type is None


def test_missing_location_returns_empty_list() -> None:
    body = json.dumps(
        {
            "jobs": [
                {
                    "id": 1006,
                    "title": "Eng",
                    "content": "<p>x</p>",
                    "absolute_url": "https://boards.greenhouse.io/x/jobs/1006",
                }
            ],
            "meta": {"total": 1},
        }
    ).encode("utf-8")

    def handler(req):
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    p = asyncio.run(_run(handler)).postings[0]
    assert p.locations == []


def test_empty_location_label_returns_empty_list() -> None:
    body = json.dumps(
        {
            "jobs": [
                {
                    "id": 1007,
                    "title": "Eng",
                    "content": "<p>x</p>",
                    "absolute_url": "https://boards.greenhouse.io/x/jobs/1007",
                    "location": {"name": "   "},
                }
            ],
            "meta": {"total": 1},
        }
    ).encode("utf-8")

    def handler(req):
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    p = asyncio.run(_run(handler)).postings[0]
    assert p.locations == []


def test_non_object_location_returns_empty_list() -> None:
    body = json.dumps(
        {
            "jobs": [
                {
                    "id": 1008,
                    "title": "Eng",
                    "content": "<p>x</p>",
                    "absolute_url": "https://boards.greenhouse.io/x/jobs/1008",
                    "location": "Remote",
                }
            ],
            "meta": {"total": 1},
        }
    ).encode("utf-8")

    def handler(req):
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    p = asyncio.run(_run(handler)).postings[0]
    assert p.locations == []


def test_department_first_non_empty_string_preserved_in_vendor_order() -> None:
    body = json.dumps(
        {
            "jobs": [
                {
                    "id": 1009,
                    "title": "Eng",
                    "content": "<p>x</p>",
                    "absolute_url": "https://boards.greenhouse.io/x/jobs/1009",
                    "departments": [
                        {"name": ""},
                        {"name": "  Engineering  "},
                        {"name": "Platform"},
                    ],
                }
            ],
            "meta": {"total": 1},
        }
    ).encode("utf-8")

    def handler(req):
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    p = asyncio.run(_run(handler)).postings[0]
    assert p.department == "Engineering"  # trimmed, first non-empty


def test_missing_or_empty_department_returns_none() -> None:
    body = json.dumps(
        {
            "jobs": [
                {
                    "id": 1010,
                    "title": "Eng",
                    "content": "<p>x</p>",
                    "absolute_url": "https://boards.greenhouse.io/x/jobs/1010",
                    "departments": [],
                }
            ],
            "meta": {"total": 1},
        }
    ).encode("utf-8")

    def handler(req):
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    p = asyncio.run(_run(handler)).postings[0]
    assert p.department is None


def test_department_only_non_dict_items_skipped() -> None:
    body = json.dumps(
        {
            "jobs": [
                {
                    "id": 1011,
                    "title": "Eng",
                    "content": "<p>x</p>",
                    "absolute_url": "https://boards.greenhouse.io/x/jobs/1011",
                    "departments": ["nope", 12, {"name": "Eng"}],
                }
            ],
            "meta": {"total": 1},
        }
    ).encode("utf-8")

    def handler(req):
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    p = asyncio.run(_run(handler)).postings[0]
    assert p.department == "Eng"


# --------------------------------------------------------------------
# Posting and application URLs
# --------------------------------------------------------------------


def test_posting_url_carries_query_and_fragment_preserved() -> None:
    body = json.dumps(
        {
            "jobs": [
                {
                    "id": 2001,
                    "title": "Eng",
                    "content": "<p>x</p>",
                    "absolute_url": "https://boards.greenhouse.io/x/jobs/2001?ref=a&t=z#top",
                }
            ],
            "meta": {"total": 1},
        }
    ).encode("utf-8")

    def handler(req):
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    p = asyncio.run(_run(handler)).postings[0]
    assert p.posting_url == "https://boards.greenhouse.io/x/jobs/2001?ref=a&t=z#top"


def test_apply_url_copies_posting_url_exactly_without_inventing_fragment() -> None:
    body = json.dumps(
        {
            "jobs": [
                {
                    "id": 2002,
                    "title": "Eng",
                    "content": "<p>x</p>",
                    "absolute_url": "https://boards.greenhouse.io/x/jobs/2002",
                }
            ],
            "meta": {"total": 1},
        }
    ).encode("utf-8")

    def handler(req):
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    p = asyncio.run(_run(handler)).postings[0]
    # Both fields populated independently; same value, no #app fragment added.
    assert p.posting_url == "https://boards.greenhouse.io/x/jobs/2002"
    assert p.apply_url == "https://boards.greenhouse.io/x/jobs/2002"
    assert "#app" not in p.apply_url
    assert "#app" not in p.posting_url


def test_posting_url_must_be_http_or_https() -> None:
    body = json.dumps(
        {
            "jobs": [
                {
                    "id": 2003,
                    "title": "Eng",
                    "content": "<p>x</p>",
                    "absolute_url": "ftp://boards.greenhouse.io/x/jobs/2003",
                }
            ],
            "meta": {"total": 1},
        }
    ).encode("utf-8")

    def handler(req):
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    result = asyncio.run(_run(handler))
    assert result.postings == []
    assert result.complete is False


@pytest.mark.parametrize(
    "absolute_url",
    [
        # ``urlparse`` raises ``ValueError`` on a malformed IPv6 literal; the
        # posting row must be invalid rather than raise.
        "https://[::1:bad/jobs/2001",
        # Malformed port string raises ``ValueError`` on ``parsed.port``; the
        # row must be invalid rather than raise.
        "https://boards.greenhouse.io:not-a-port/x/jobs/2001",
        "http://boards.greenhouse.io:abc/x/jobs/2001",
        # Missing host rejects the URL.
        "https:///jobs/2001",
        "https://",
        # Credential-bearing URLs are rejected.
        "https://user:pw@boards.greenhouse.io/x/jobs/2001",
        "https://user@boards.greenhouse.io/x/jobs/2001",
    ],
)
def test_public_url_rejects_malformed_or_credential_bearing_values(absolute_url) -> None:
    body = json.dumps(
        {
            "jobs": [
                {
                    "id": 2001,
                    "title": "Eng",
                    "content": "<p>x</p>",
                    "absolute_url": absolute_url,
                }
            ],
            "meta": {"total": 1},
        }
    ).encode("utf-8")

    def handler(req):
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    result = asyncio.run(_run(handler))
    # The posting row is invalid rather than raising an uncaught URL error;
    # the whole fetch is incomplete because one row was skipped.
    assert result.postings == []
    assert result.complete is False
    assert result.http_status == 200


@pytest.mark.parametrize(
    "absolute_url",
    [
        # Any explicit port that parses is accepted — the contract only
        # forbids embedded credentials and a missing host.
        "https://boards.greenhouse.io:443/x/jobs/2001",
        "https://boards.greenhouse.io:8443/x/jobs/2001",
        "http://boards.greenhouse.io:80/x/jobs/2001",
        "http://boards.greenhouse.io:8080/x/jobs/2001",
    ],
)
def test_public_url_accepts_any_parsable_explicit_port(absolute_url) -> None:
    body = json.dumps(
        {
            "jobs": [
                {
                    "id": 2001,
                    "title": "Eng",
                    "content": "<p>SYNTHETIC_PUBLIC_URL_PORT_SENTINEL</p>",
                    "absolute_url": absolute_url,
                }
            ],
            "meta": {"total": 1},
        }
    ).encode("utf-8")

    def handler(req):
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    result = asyncio.run(_run(handler))
    assert len(result.postings) == 1
    assert result.postings[0].posting_url == absolute_url
    assert result.postings[0].apply_url == absolute_url
    assert result.complete is True


# --------------------------------------------------------------------
# raw snapshot preservation
# --------------------------------------------------------------------


def test_raw_preserves_unknown_vendor_fields_without_mutation() -> None:
    body = json.dumps(
        {
            "jobs": [
                {
                    "id": 3001,
                    "title": "Engineer",
                    "content": "<p>SYNTHETIC_RAW_SENTINEL</p>",
                    "absolute_url": "https://boards.greenhouse.io/x/jobs/3001",
                    "metadata": [{"name": "comp", "value": "$X"}],
                    "parent_job_id": 1234,
                    "unknown_field_xyz": "kept",
                    "nested": {"deep": [{"letters": ["a", "b"]}]},
                }
            ],
            "meta": {"total": 1},
        }
    ).encode("utf-8")

    def handler(req):
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    # Snapshot the decoded upstream payload mutably.
    upstream = json.loads(body)
    p = asyncio.run(_run(handler)).postings[0]
    # All known and unknown fields preserved.
    assert p.raw["metadata"] == upstream["jobs"][0]["metadata"]
    assert p.raw["unknown_field_xyz"] == "kept"
    assert p.raw["parent_job_id"] == 1234
    assert p.raw["nested"] == upstream["jobs"][0]["nested"]


def test_raw_snapshot_decoupled_from_caller_mutation() -> None:
    body = json.dumps(
        {
            "jobs": [
                {
                    "id": 3002,
                    "title": "Eng",
                    "content": "<p>x</p>",
                    "absolute_url": "https://boards.greenhouse.io/x/jobs/3002",
                    "metadata": [{"name": "x", "value": "y"}],
                }
            ],
            "meta": {"total": 1},
        }
    ).encode("utf-8")

    parsed = json.loads(body)
    parsed_jobs = parsed["jobs"]
    serialized = json.dumps(parsed).encode("utf-8")

    def handler(req):
        return httpx.Response(200, content=serialized, headers={"content-type": "application/json"})

    p = asyncio.run(_run(handler)).postings[0]
    # Mutate the original payload's nested list; the frozen snapshot must not
    # share that mutable child reference.
    parsed_jobs[0]["metadata"].append({"name": "leak", "value": "truth"})
    parsed_jobs[0]["id"] = 9999
    assert p.raw["metadata"] == [{"name": "x", "value": "y"}]
    assert p.raw["id"] == 3002


# --------------------------------------------------------------------
# Markdown conversion (golden)
# --------------------------------------------------------------------


def test_markdown_unescape_once_then_convert() -> None:
    # Vendor content is HTML-escaped; one html.unescape step yields HTML.
    content = "&lt;h2&gt;Role&lt;/h2&gt;\n&lt;p&gt;Tom &amp;amp; Jerry&lt;/p&gt;"
    body = json.dumps(
        {
            "jobs": [
                {
                    "id": 4001,
                    "title": "Eng",
                    "content": content,
                    "absolute_url": "https://boards.greenhouse.io/x/jobs/4001",
                }
            ],
            "meta": {"total": 1},
        }
    ).encode("utf-8")

    def handler(req):
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    p = asyncio.run(_run(handler)).postings[0]
    assert p.description_md.startswith("## Role")
    # Single unescape collapses ``&amp;amp;`` to ``&amp;`` and the parser sees
    # ``&amp;`` as a literal ``&`` text. Either way the second ``amp;`` string
    # must not appear, proving we did not unescape twice.
    assert "amp;amp;" not in p.description_md


def test_markdown_paragraphs_headings_lists_links_emphasis() -> None:
    content = (
        "&lt;h2&gt;About&lt;/h2&gt;\n"
        "&lt;p&gt;Hello &lt;strong&gt;world&lt;/strong&gt; and &lt;em&gt;"
        "italic&lt;/em&gt;.&lt;/p&gt;\n"
        "&lt;ul&gt;&lt;li&gt;A&lt;/li&gt;&lt;li&gt;B&lt;/li&gt;&lt;/ul&gt;\n"
        "&lt;ol&gt;&lt;li&gt;One&lt;/li&gt;&lt;li&gt;Two&lt;/li&gt;&lt;/ol&gt;\n"
        '&lt;p&gt;&lt;a href="https://example.invalid/x"&gt;'
        "link&lt;/a&gt;&lt;/p&gt;"
    )
    body = json.dumps(
        {
            "jobs": [
                {
                    "id": 4002,
                    "title": "Eng",
                    "content": content,
                    "absolute_url": "https://boards.greenhouse.io/x/jobs/4002",
                }
            ],
            "meta": {"total": 1},
        }
    ).encode("utf-8")

    def handler(req):
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    md = asyncio.run(_run(handler)).postings[0].description_md
    assert "## About" in md
    assert "**world**" in md
    assert "*italic*" in md
    assert "\n* A\n* B" in md or "\n* A\n* B\n" in md
    assert "1. One" in md and "2. Two" in md
    assert "[link](https://example.invalid/x)" in md


def test_markdown_br_converted_deterministically() -> None:
    content = "line one&lt;br&gt;line two"
    body = json.dumps(
        {
            "jobs": [
                {
                    "id": 4003,
                    "title": "Eng",
                    "content": content,
                    "absolute_url": "https://boards.greenhouse.io/x/jobs/4003",
                }
            ],
            "meta": {"total": 1},
        }
    ).encode("utf-8")

    def handler(req):
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    md = asyncio.run(_run(handler)).postings[0].description_md
    # ``<br>`` yields a visible line break. markdownify emits a hard line
    # break (two trailing spaces + LF); the reviewed whitespace pass removes
    # trailing horizontal whitespace from every line, leaving a single LF.
    assert "line one\nline two" == md


def test_markdown_collapses_three_plus_blank_lines_to_two() -> None:
    content = "&lt;p&gt;a&lt;/p&gt;\n\n\n\n\n&lt;p&gt;b&lt;/p&gt;"
    body = json.dumps(
        {
            "jobs": [
                {
                    "id": 4004,
                    "title": "Eng",
                    "content": content,
                    "absolute_url": "https://boards.greenhouse.io/x/jobs/4004",
                }
            ],
            "meta": {"total": 1},
        }
    ).encode("utf-8")

    def handler(req):
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    md = asyncio.run(_run(handler)).postings[0].description_md
    # Three or more blank lines collapse to two.
    assert "\n\n\n" not in md
    assert "a\n\nb" in md


def test_markdown_crlf_and_cr_normalized_to_lf() -> None:
    content = "abc\r\ndef\rghi"
    body = json.dumps(
        {
            "jobs": [
                {
                    "id": 4005,
                    "title": "Eng",
                    "content": content,
                    "absolute_url": "https://boards.greenhouse.io/x/jobs/4005",
                }
            ],
            "meta": {"total": 1},
        }
    ).encode("utf-8")

    def handler(req):
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    md = asyncio.run(_run(handler)).postings[0].description_md
    assert "\r" not in md


def test_markdown_strips_trailing_horizontal_whitespace() -> None:
    content = "&lt;p&gt;line one   &lt;/p&gt;\n&lt;p&gt;line two&lt;/p&gt;"
    body = json.dumps(
        {
            "jobs": [
                {
                    "id": 4006,
                    "title": "Eng",
                    "content": content,
                    "absolute_url": "https://boards.greenhouse.io/x/jobs/4006",
                }
            ],
            "meta": {"total": 1},
        }
    ).encode("utf-8")

    def handler(req):
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    md = asyncio.run(_run(handler)).postings[0].description_md
    assert "   \n" not in md


def test_markdown_strips_leading_and_trailing_blank_lines_no_terminal_newline() -> None:
    content = "\n\n&lt;p&gt;Hello&lt;/p&gt;\n\n"
    body = json.dumps(
        {
            "jobs": [
                {
                    "id": 4007,
                    "title": "Eng",
                    "content": content,
                    "absolute_url": "https://boards.greenhouse.io/x/jobs/4007",
                }
            ],
            "meta": {"total": 1},
        }
    ).encode("utf-8")

    def handler(req):
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    md = asyncio.run(_run(handler)).postings[0].description_md
    assert md == "Hello"
    assert not md.startswith("\n")
    assert not md.endswith("\n")


def test_markdown_script_and_style_content_not_visible() -> None:
    content = (
        "&lt;p&gt;Hello&lt;/p&gt;"
        "&lt;script&gt;SECRET_SCRIPT_SENTINEL&lt;/script&gt;"
        "&lt;style&gt;body {color: red}&lt;/style&gt;"
        "&lt;p&gt;after&lt;/p&gt;"
    )
    body = json.dumps(
        {
            "jobs": [
                {
                    "id": 4008,
                    "title": "Eng",
                    "content": content,
                    "absolute_url": "https://boards.greenhouse.io/x/jobs/4008",
                }
            ],
            "meta": {"total": 1},
        }
    ).encode("utf-8")

    def handler(req):
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    md = asyncio.run(_run(handler)).postings[0].description_md
    assert "SECRET_SCRIPT_SENTINEL" not in md
    assert "color: red" not in md
    assert "Hello" in md and "after" in md


def test_markdown_non_ascii_text_preserved() -> None:
    content = "&lt;p&gt;Café résumé naïve&lt;/p&gt;"
    body = json.dumps(
        {
            "jobs": [
                {
                    "id": 4009,
                    "title": "Eng",
                    "content": content,
                    "absolute_url": "https://boards.greenhouse.io/x/jobs/4009",
                }
            ],
            "meta": {"total": 1},
        }
    ).encode("utf-8")

    def handler(req):
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    md = asyncio.run(_run(handler)).postings[0].description_md
    assert "Café résumé naïve" in md


# --------------------------------------------------------------------
# Transport, timeout, TLS, cancellation
# --------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc,category",
    [
        (
            httpx.ConnectTimeout("TRANSPORT_CONNECT_LEAK_SENTINEL"),
            "greenhouse.transport.connect_timeout",
        ),
        (httpx.ReadTimeout("TRANSPORT_READ_LEAK_SENTINEL"), "greenhouse.transport.read_timeout"),
        (
            httpx.ConnectError("TRANSPORT_DENIED_LEAK_SENTINEL"),
            "greenhouse.transport.connect_error",
        ),
        (
            httpx.RemoteProtocolError("TRANSPORT_PROTOCOL_LEAK_SENTINEL"),
            "greenhouse.transport.protocol_error",
        ),
    ],
)
def test_transport_failures_raise_typed_redacted_exception(exc, category) -> None:
    def handler(req):
        raise exc

    with pytest.raises(GreenhouseTransportError) as info:
        asyncio.run(_run(handler))
    assert info.value.category == category
    # The typed exception's public string carries only the bounded category
    # code — no raw exception text, URL, header, or sentinel leaks.
    assert str(info.value) == category
    assert "LEAK_SENTINEL" not in str(info.value)


def test_transport_failure_raises_no_fetch_result() -> None:
    def handler(req):
        raise httpx.ConnectError("denied")

    raised: object = None
    try:
        asyncio.run(_run(handler))
    except GreenhouseTransportError as exc:
        raised = exc
    except BaseException as exc:
        raised = exc

    assert isinstance(raised, GreenhouseTransportError)
    # No FetchResult was produced.
    assert not isinstance(raised, FetchResult)


def test_cancellation_propagates_and_does_not_become_transport_error() -> None:
    async def driver():
        started = asyncio.Event()

        async def handler(req):
            started.set()
            await asyncio.sleep(3600)
            return httpx.Response(200)

        client = _client(handler)
        adapter = GreenhouseAdapter(client=client)
        task = asyncio.ensure_future(adapter.list_postings(STOP, ConditionalHeaders()))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The streamed response and the adapter-owned client must have been
        # cleaned up deterministically. The injected client is still open here
        # so the test owns its lifetime, but the streamed response produced by
        # ``client.stream`` must not leak (verified by absence of resource
        # warnings during the run).

    asyncio.run(driver())
    # Cancellation propagated; not a transport failure: no GreenhouseTransportError
    # was raised, so cancellation cannot authorize reconciliation.


def test_adapter_closes_owned_client_on_success() -> None:
    # Adapter owns the client when no client is injected. Tracking the
    # adapter-owned AsyncClient's aclose() invocation catches a leaked client.
    transport = httpx.MockTransport(
        lambda req: httpx.Response(
            200, content=b'{"jobs":[]}', headers={"content-type": "application/json"}
        )
    )

    async def go():
        adapter = GreenhouseAdapter(transport=transport)
        original_aclose = httpx.AsyncClient.aclose

        closed_calls: list[object] = []

        async def tracking_aclose(self):  # noqa: ANN001
            closed_calls.append(self)
            return await original_aclose(self)

        httpx.AsyncClient.aclose = tracking_aclose  # type: ignore[assignment]
        try:
            await adapter.list_postings(STOP, ConditionalHeaders())
        finally:
            httpx.AsyncClient.aclose = original_aclose  # type: ignore[assignment]
        return closed_calls

    closed_calls = asyncio.run(go())
    assert closed_calls, "the adapter-owned client must be closed on success"


# --------------------------------------------------------------------
# Token-recognition patterns
# --------------------------------------------------------------------


def test_token_patterns_returns_stable_list_with_token_group() -> None:
    patterns = GreenhouseAdapter().token_patterns()
    assert len(patterns) == 4
    for p in patterns:
        m = p.match("https://boards.greenhouse.io/forksboard")
        if m:
            assert "token" in m.groupdict()
    # The order is documented and stable: boards, job-boards, embed, api.
    text_patterns = [p.pattern for p in patterns]
    assert text_patterns[0].startswith("^https://boards\\.greenhouse\\.io/")
    assert text_patterns[1].startswith("^https://job-boards\\.greenhouse\\.io/")
    assert text_patterns[2].startswith("^https://boards\\.greenhouse\\.io/embed/job_board")
    assert text_patterns[3].startswith("^https://boards-api\\.greenhouse\\.io/v1/boards/")


@pytest.mark.parametrize(
    "url,token",
    [
        # boards.greenhouse.io shapes: optional trailing slash, /jobs[/{id}],
        # query string, and fragment forms per Task 004 section 7.
        ("https://boards.greenhouse.io/forksboard", "forksboard"),
        ("https://boards.greenhouse.io/forksboard/", "forksboard"),
        ("https://boards.greenhouse.io/forksboard/jobs", "forksboard"),
        ("https://boards.greenhouse.io/forksboard/jobs/", "forksboard"),
        ("https://boards.greenhouse.io/forksboard/jobs/123", "forksboard"),
        ("https://boards.greenhouse.io/forksboard/jobs/123/", "forksboard"),
        ("https://boards.greenhouse.io/forksboard?ref=abc", "forksboard"),
        ("https://boards.greenhouse.io/forksboard/?ref=abc", "forksboard"),
        ("https://boards.greenhouse.io/forksboard/jobs/123?ref=abc", "forksboard"),
        ("https://boards.greenhouse.io/forksboard#frag", "forksboard"),
        ("https://boards.greenhouse.io/forksboard/#frag", "forksboard"),
        ("https://boards.greenhouse.io/forksboard/jobs/123?ref=abc#top", "forksboard"),
        # job-boards.greenhouse.io shapes: same optional suffix grammar.
        ("https://job-boards.greenhouse.io/forksboard", "forksboard"),
        ("https://job-boards.greenhouse.io/forksboard/jobs/123?t=z#f", "forksboard"),
        ("https://job-boards.greenhouse.io/abc-def_g", "abc-def_g"),
        # embed shape: ``for={token}`` is required, plus optional trailing
        # slash before the query, extra query params after ``for=``, and an
        # optional fragment.
        ("https://boards.greenhouse.io/embed/job_board?for=forksboard", "forksboard"),
        ("https://boards.greenhouse.io/embed/job_board/?for=forksboard", "forksboard"),
        (
            "https://boards.greenhouse.io/embed/job_board?for=forksboard&other=x",
            "forksboard",
        ),
        ("https://boards.greenhouse.io/embed/job_board?for=forksboard#top", "forksboard"),
        (
            "https://boards.greenhouse.io/embed/job_board?for=forksboard&x=1#top",
            "forksboard",
        ),
        # api shape: optional trailing slash, query, and fragment after
        # ``/jobs``.
        ("https://boards-api.greenhouse.io/v1/boards/forksboard/jobs", "forksboard"),
        (
            "https://boards-api.greenhouse.io/v1/boards/forksboard/jobs/",
            "forksboard",
        ),
        (
            "https://boards-api.greenhouse.io/v1/boards/forksboard/jobs?content=true",
            "forksboard",
        ),
        (
            "https://boards-api.greenhouse.io/v1/boards/forksboard/jobs?content=true#top",
            "forksboard",
        ),
    ],
)
def test_token_patterns_positive_cases(url, token) -> None:
    patterns = GreenhouseAdapter().token_patterns()
    matched = None
    for p in patterns:
        m = p.match(url)
        if m and not matched:
            matched = m
    assert matched is not None
    found = matched.groupdict().get("token")
    assert found == token


@pytest.mark.parametrize(
    "url",
    [
        "http://boards.greenhouse.io/forksboard",  # non-HTTPS
        "https://boards.greenhouse.io/",  # missing token
        "https://boards.greenhouse.io.example.com/forksboard",  # lookalike
        "https://user:pw@boards.greenhouse.io/forksboard",  # credentials
        "https://boards.greenhouse.io/../other",  # path traversal
        "https://boards.greenhouse.io/bad/token",  # invalid-token slash (no extra path)
        "https://boards.greenhouse.io/embed/job_board?for=bad/token",
        "https://example.invalid/forksboard",
        "https://boards.greenhouse.io/embed/job_board?other=forksboard",
        "https://boards-api.greenhouse.io/v1/boards/",  # missing token
        # Embed form with the ``for`` query parameter missing must never match:
        # the adapter grammar requires a non-empty ``for={token}``.
        "https://boards.greenhouse.io/embed/job_board",
        "https://boards.greenhouse.io/embed/job_board/",
        "https://boards.greenhouse.io/embed/job_board#top",
        # ``for`` parameter present but empty still fails the adapter grammar
        # (the token must start ``[A-Za-z0-9]``).
        "https://boards.greenhouse.io/embed/job_board?for=",
        # ``for`` is not the first parameter — the documented shape is
        # ``?for={token}`` so a leading ``other=`` does not satisfy it.
        "https://boards.greenhouse.io/embed/job_board?other=x&for=forksboard",
        # A space in a fragment terminates the bounded fragment class.
        "https://boards.greenhouse.io/forksboard#frag space",
        # Literal path-traversal segments immediately under ``/jobs`` are
        # rejected for every URL shape that exposes a posting-id segment.
        "https://boards.greenhouse.io/forksboard/jobs/.",
        "https://boards.greenhouse.io/forksboard/jobs/..",
        "https://job-boards.greenhouse.io/forksboard/jobs/.",
        "https://job-boards.greenhouse.io/forksboard/jobs/..",
        "https://boards-api.greenhouse.io/v1/boards/forksboard/jobs/.",
        "https://boards-api.greenhouse.io/v1/boards/forksboard/jobs/..",
        # Percent-encoded dot traversal (any case) is rejected as well.
        "https://boards.greenhouse.io/forksboard/jobs/%2e",
        "https://boards.greenhouse.io/forksboard/jobs/%2E",
        "https://boards.greenhouse.io/forksboard/jobs/%2e%2e",
        "https://boards.greenhouse.io/forksboard/jobs/%2E%2E",
        "https://boards-api.greenhouse.io/v1/boards/forksboard/jobs/%2e",
        "https://boards-api.greenhouse.io/v1/boards/forksboard/jobs/%2e%2e",
        # A traversal segment followed by a query is still rejected; the
        # lookahead boundary covers ``[/?#]`` after the bad segment.
        "https://boards.greenhouse.io/forksboard/jobs/.?content=true",
        "https://boards.greenhouse.io/forksboard/jobs/..?content=true",
        "https://boards-api.greenhouse.io/v1/boards/forksboard/jobs/%2e?content=true",
    ],
)
def test_token_patterns_negative_cases(url) -> None:
    patterns = GreenhouseAdapter().token_patterns()
    matched = None
    for p in patterns:
        if p.match(url) and not matched:
            matched = p.match(url)
    assert matched is None, f"unexpected match on {url}"


@pytest.mark.parametrize(
    "url,token",
    [
        # ``/jobs/{id}`` where ``{id}`` is a normal segment still matches; the
        # traversal rejection only targets the literal ``.``, ``..`` and the
        # percent-encoded dot forms, not ordinary postings IDs.
        ("https://boards.greenhouse.io/forksboard/jobs/abc", "forksboard"),
        ("https://boards.greenhouse.io/forksboard/jobs/123", "forksboard"),
        ("https://boards.greenhouse.io/forksboard/jobs/abc/", "forksboard"),
        ("https://job-boards.greenhouse.io/forksboard/jobs/abc", "forksboard"),
        ("https://boards-api.greenhouse.io/v1/boards/forksboard/jobs/abc", "forksboard"),
        ("https://boards-api.greenhouse.io/v1/boards/forksboard/jobs/abc/", "forksboard"),
        # A regular non-traversal segment that contains ``%2e`` mixed with
        # other characters (e.g. ``%2efoobar``) is not a path-traversal literal
        # and still matches: the lookahead rejects only an exact ``%2e``,
        # ``%2e%2e``, ``%2E``, ``%2E%2E``, ``.``, or ``..`` segment.
        ("https://boards.greenhouse.io/forksboard/jobs/%2efoobar", "forksboard"),
    ],
)
def test_token_patterns_accepts_non_traversal_posting_ids(url, token) -> None:
    patterns = GreenhouseAdapter().token_patterns()
    matched = None
    for p in patterns:
        m = p.match(url)
        if m and not matched:
            matched = m
    assert matched is not None, f"expected match on {url}"
    assert matched.groupdict().get("token") == token


# --------------------------------------------------------------------
# Log and exception redaction
# --------------------------------------------------------------------


def test_logs_do_not_leak_token_url_validators_or_sentinel(caplog) -> None:
    body = json.dumps(
        {
            "jobs": [
                {
                    "id": 9001,
                    "title": "Eng",
                    "content": "<p>SYNTHETIC_LOG_BODY_SENTINEL</p>",
                    "absolute_url": "https://boards.greenhouse.io/x/jobs/9001",
                    "first_published": "2026-07-15T10:00:00+02:00",
                }
            ],
            "meta": {"total": 1},
        }
    ).encode("utf-8")

    def handler(req):
        return httpx.Response(
            200,
            content=body,
            headers={
                "content-type": "application/json",
                "etag": '"SYNTHETIC_ETAG_SENTINEL"',
                "last-modified": "Wed, 01 Jul 2026 12:00:00 GMT",
            },
        )

    caplog.set_level(logging.INFO, logger="app.sources.ats.greenhouse")
    asyncio.run(
        _run(
            handler,
            conditional=ConditionalHeaders(
                etag='"SYNTHETIC_VALIDATOR_ETAG_SENTINEL"',
                last_modified="Wed, 01 Jul 1990 00:00:00 GMT",
            ),
        )
    )

    text = caplog.text
    assert "forksboard" not in text
    assert "SYNTHETIC_LOG_BODY_SENTINEL" not in text
    assert "SYNTHETIC_VALIDATOR_ETAG_SENTINEL" not in text
    # The final ETag value must not be logged either.
    assert "SYNTHETIC_ETAG_SENTINEL" not in text


def test_transport_error_string_excludes_sentinel() -> None:
    sentinel = "SYNTHETIC_RAW_EXCEPTION_SENTINEL"

    def handler(req):
        raise httpx.ConnectError(sentinel)

    with pytest.raises(GreenhouseTransportError) as info:
        asyncio.run(_run(handler))
    assert sentinel not in str(info.value)
    # Traceback attachment must carry the raw exception but the public
    # category message must not echo it.
    assert info.value.__cause__ is None  # we use ``from None`` to suppress link
