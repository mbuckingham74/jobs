"""Tests for the bounded conditional HTTPS fetch component.

Every test uses ``httpx.MockTransport`` so no live network request is made.
Covers conditional-header construction (none, either, both), 304, valid 200,
redirects, non-HTTPS redirect, redirect overflow, timeouts, non-200 statuses,
partial responses, wrong/missing media type, missing PDF signature, empty
body, oversized ``Content-Length``, and streamed overflow without
``Content-Length``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import format_datetime

import httpx

from app.resume.fetch import (
    ConditionalValidators,
    FetchOutcome,
    FetchOutcomeKind,
    fetch_resume,
)
from tests.pdf_fixtures import build_pdf

URL = "https://example.invalid/portfolio.pdf"

# A small PDF whose first page clears the completeness gate is irrelevant for
# the fetch tests; here we just need a non-empty body whose prefix is the PDF
# signature.
_GOOD_BODY = build_pdf(
    [
        [
            "Page one line one text content here made up",
            "Page one line two continues here with content",
        ]
    ]
)


def _client(handler, **kw) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), **kw)


def test_no_validators_sends_no_conditional_headers() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.headers)
        return httpx.Response(200, content=_GOOD_BODY, headers={"content-type": "application/pdf"})

    client = _client(handler)
    outcome = fetch_resume(URL, max_bytes=10 * 1024 * 1024, client=client)
    assert outcome.kind is FetchOutcomeKind.OK
    assert "if-none-match" not in captured
    assert "if-modified-since" not in captured
    assert outcome.sent_validators is False


def test_etag_only_sends_if_none_match() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.headers)
        return httpx.Response(304)

    client = _client(handler)
    outcome = fetch_resume(
        URL,
        max_bytes=10 * 1024 * 1024,
        validators=ConditionalValidators(etag='"abc"'),
        client=client,
    )
    assert outcome.kind is FetchOutcomeKind.NOT_MODIFIED
    assert captured["if-none-match"] == '"abc"'
    assert "if-modified-since" not in captured
    assert outcome.sent_validators is True


def test_last_modified_only_sends_if_modified_since() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.headers)
        return httpx.Response(304)

    client = _client(handler)
    lm = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)
    outcome = fetch_resume(
        URL,
        max_bytes=10 * 1024 * 1024,
        validators=ConditionalValidators(last_modified=lm),
        client=client,
    )
    assert outcome.kind is FetchOutcomeKind.NOT_MODIFIED
    assert captured["if-modified-since"].endswith("GMT")
    assert "if-none-match" not in captured
    assert outcome.sent_validators is True


def test_both_validators_sent() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.headers)
        return httpx.Response(304)

    client = _client(handler)
    lm = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)
    outcome = fetch_resume(
        URL,
        max_bytes=10 * 1024 * 1024,
        validators=ConditionalValidators(etag='"v1"', last_modified=lm),
        client=client,
    )
    assert outcome.kind is FetchOutcomeKind.NOT_MODIFIED
    assert captured["if-none-match"] == '"v1"'
    assert captured["if-modified-since"].endswith("GMT")
    assert outcome.sent_validators is True


def test_not_modified_returns_validators_returned() -> None:
    lm = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            304, headers={"etag": '"v2"', "last-modified": format_datetime(lm, usegmt=True)}
        )

    client = _client(handler)
    outcome = fetch_resume(URL, max_bytes=10 * 1024 * 1024, client=client)
    assert outcome.kind is FetchOutcomeKind.NOT_MODIFIED
    assert outcome.http_status == 304
    assert outcome.etag == '"v2"'
    assert outcome.last_modified is not None
    assert outcome.returned_validators is True
    assert outcome.body is None


def test_not_modified_no_validators_returned() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(304)

    client = _client(handler)
    outcome = fetch_resume(URL, max_bytes=10 * 1024 * 1024, client=client)
    assert outcome.kind is FetchOutcomeKind.NOT_MODIFIED
    assert outcome.etag is None
    assert outcome.last_modified is None
    assert outcome.returned_validators is False


def test_valid_200_returns_body_and_byte_count() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_GOOD_BODY, headers={"content-type": "application/pdf"})

    client = _client(handler)
    outcome = fetch_resume(URL, max_bytes=10 * 1024 * 1024, client=client)
    assert outcome.kind is FetchOutcomeKind.OK
    assert outcome.http_status == 200
    assert outcome.body == _GOOD_BODY
    assert outcome.byte_count == len(_GOOD_BODY)
    assert outcome.fetched_at is not None
    assert outcome.body is not None and outcome.body.startswith(b"%PDF-")


def test_follows_at_most_three_https_redirects() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(302, headers={"location": "https://example.invalid/r1"})
        if calls["n"] == 2:
            return httpx.Response(302, headers={"location": "https://example.invalid/r2"})
        if calls["n"] == 3:
            return httpx.Response(302, headers={"location": "https://example.invalid/r3"})
        return httpx.Response(200, content=_GOOD_BODY, headers={"content-type": "application/pdf"})

    client = _client(handler)
    outcome = fetch_resume(URL, max_bytes=10 * 1024 * 1024, client=client)
    assert outcome.kind is FetchOutcomeKind.OK
    assert calls["n"] == 4  # initial + 3 redirects


def test_non_https_redirect_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://example.invalid/r"})

    client = _client(handler)
    outcome = fetch_resume(URL, max_bytes=10 * 1024 * 1024, client=client)
    assert outcome.kind is FetchOutcomeKind.REJECTED
    assert outcome.http_status == 302
    assert outcome.reason == "fetcher.http_not_https"


def test_redirect_with_credentials_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://user:pw@example.invalid/r"})

    client = _client(handler)
    outcome = fetch_resume(URL, max_bytes=10 * 1024 * 1024, client=client)
    assert outcome.kind is FetchOutcomeKind.REJECTED
    assert outcome.reason == "fetcher.redirect_has_credentials"


def test_redirect_overflow_is_rejected() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        # fourth redirect exceeds MAX_REDIRECTS==3 after the three follows.
        return httpx.Response(302, headers={"location": f"https://example.invalid/r{calls['n']}"})

    client = _client(handler)
    outcome = fetch_resume(URL, max_bytes=10 * 1024 * 1024, client=client)
    assert outcome.kind is FetchOutcomeKind.REJECTED
    assert outcome.reason == "fetcher.redirect_overflow"
    assert calls["n"] == 4  # initial + 3 followed redirects then blocked at 4th


def test_connect_timeout_is_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timeout", request=request)

    client = _client(handler)
    outcome = fetch_resume(URL, max_bytes=10 * 1024 * 1024, client=client)
    assert outcome.kind is FetchOutcomeKind.ERROR
    assert outcome.reason == "fetcher.connect_timeout"


def test_read_timeout_is_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout", request=request)

    client = _client(handler)
    outcome = fetch_resume(URL, max_bytes=10 * 1024 * 1024, client=client)
    assert outcome.kind is FetchOutcomeKind.ERROR
    assert outcome.reason == "fetcher.read_timeout"


def test_non_200_status_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = _client(handler)
    outcome = fetch_resume(URL, max_bytes=10 * 1024 * 1024, client=client)
    assert outcome.kind is FetchOutcomeKind.REJECTED
    assert outcome.http_status == 500
    assert outcome.reason == "fetcher.non_200_status"


def test_partial_content_206_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(206, content=_GOOD_BODY, headers={"content-type": "application/pdf"})

    client = _client(handler)
    outcome = fetch_resume(URL, max_bytes=10 * 1024 * 1024, client=client)
    assert outcome.kind is FetchOutcomeKind.REJECTED
    assert outcome.http_status == 206


def test_wrong_media_type_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_GOOD_BODY, headers={"content-type": "text/html"})

    client = _client(handler)
    outcome = fetch_resume(URL, max_bytes=10 * 1024 * 1024, client=client)
    assert outcome.kind is FetchOutcomeKind.REJECTED
    assert outcome.reason == "fetcher.wrong_media_type"


def test_missing_media_type_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_GOOD_BODY)

    client = _client(handler)
    outcome = fetch_resume(URL, max_bytes=10 * 1024 * 1024, client=client)
    assert outcome.kind is FetchOutcomeKind.REJECTED
    assert outcome.reason == "fetcher.wrong_media_type"


def test_missing_pdf_signature_is_rejected() -> None:
    body = b"not a pdf body but with lots of bytes here"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "application/pdf"})

    client = _client(handler)
    outcome = fetch_resume(URL, max_bytes=10 * 1024 * 1024, client=client)
    assert outcome.kind is FetchOutcomeKind.REJECTED
    assert outcome.reason == "fetcher.missing_pdf_signature"


def test_empty_body_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"", headers={"content-type": "application/pdf"})

    client = _client(handler)
    outcome = fetch_resume(URL, max_bytes=10 * 1024 * 1024, client=client)
    assert outcome.kind is FetchOutcomeKind.REJECTED
    assert outcome.reason == "fetcher.empty_body"


def test_oversized_content_length_rejected_before_body_read() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"",
            headers={
                "content-type": "application/pdf",
                "content-length": str(10 * 1024 * 1024 + 1),
            },
        )

    client = _client(handler)
    outcome = fetch_resume(URL, max_bytes=10 * 1024 * 1024, client=client)
    assert outcome.kind is FetchOutcomeKind.REJECTED
    assert outcome.reason == "fetcher.content_length_exceeds_limit"
    assert outcome.byte_count == 10 * 1024 * 1024 + 1


def test_streamed_overflow_without_content_length_aborts() -> None:
    # Build a body larger than max_bytes; the streaming reader must abort as
    # soon as it exceeds the limit even when Content-Length is absent. We strip
    # the auto-set Content-Length header so the pre-read declared-length check
    # cannot short-circuit the streamed-overflow path.
    big_body = b"%PDF-1.4\n" + b"x" * (1024 + 10)

    def handler(request: httpx.Request) -> httpx.Response:
        response = httpx.Response(
            200, content=big_body, headers={"content-type": "application/pdf"}
        )
        # Force the body to be read as a stream with no declared length.
        response.headers.pop("content-length", None)
        return response

    client = _client(handler)
    outcome = fetch_resume(URL, max_bytes=1024, client=client)
    assert outcome.kind is FetchOutcomeKind.REJECTED
    assert outcome.reason == "fetcher.streamed_overflow"
    assert outcome.byte_count > 1024


def test_url_must_be_https() -> None:
    outcome = fetch_resume("http://example.invalid/x.pdf", max_bytes=10 * 1024 * 1024)
    assert outcome.kind is FetchOutcomeKind.REJECTED
    assert outcome.reason == "fetcher.http_not_https"


def test_user_agent_is_sent() -> None:
    from app.resume.config import USER_AGENT

    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.headers)
        return httpx.Response(304)

    client = _client(handler)
    fetch_resume(URL, max_bytes=10 * 1024 * 1024, client=client)
    assert captured.get("user-agent") == USER_AGENT


def test_outcome_does_not_carry_response_headers_dict() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=_GOOD_BODY, headers={"content-type": "application/pdf", "x-secret": "leak"}
        )

    client = _client(handler)
    outcome = fetch_resume(URL, max_bytes=10 * 1024 * 1024, client=client)
    # No field exposes a response-headers dump or the secret header.
    assert not hasattr(outcome, "headers")
    assert not any("leak" in str(v) for v in vars(outcome).values())


def test_an_invalid_content_length_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=_GOOD_BODY,
            headers={"content-type": "application/pdf", "content-length": "not-a-number"},
        )

    client = _client(handler)
    outcome = fetch_resume(URL, max_bytes=10 * 1024 * 1024, client=client)
    assert outcome.kind is FetchOutcomeKind.REJECTED
    assert outcome.reason == "fetcher.invalid_content_length"


def test_media_type_with_charset_is_accepted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=_GOOD_BODY, headers={"content-type": "application/pdf; charset=binary"}
        )

    client = _client(handler)
    outcome = fetch_resume(URL, max_bytes=10 * 1024 * 1024, client=client)
    assert outcome.kind is FetchOutcomeKind.OK
    assert isinstance(outcome, FetchOutcome)
