"""Bounded conditional HTTPS fetch for the portfolio résumé PDF.

One fetch component whose input is the configured URL, maximum byte count, and
the exact matching ``(base, portfolio_pdf, BASE_RESUME_URL)``
``resume_source_state`` row, if one exists. Its ``source_etag`` and
``source_last_modified`` values are the only authoritative source for the next
request's conditional headers.

The component uses a pluggable ``httpx.BaseTransport`` so tests can replace
the HTTP boundary without live network access. The production client is an
``httpx.Client`` with explicit connect/read/write/pool timeouts, at most three
redirects that are followed manually so every hop is validated as absolute
HTTPS without credentials, a fixed descriptive user agent, streaming reads,
and an enforced byte ceiling that aborts as soon as streamed bytes exceed the
limit (including when ``Content-Length`` is missing or false).

Redirects are followed manually (httpx's automatic ``follow_redirects`` is
disabled) so the initial request and every redirect target can be validated
as absolute HTTPS without embedded credentials, and the redirect budget stays
explicit and tested.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse

import httpx

from app.resume.config import USER_AGENT
from app.resume.results import FetchOutcomeKind

# Explicit timeout budget. Connect/read/write/pool timeouts are kept small
# because the body is read incrementally and the byte limit is the primary
# defence; the pool timeout bounds obtaining a connection.
CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 30.0
WRITE_TIMEOUT = 10.0
POOL_TIMEOUT = 30.0

MAX_REDIRECTS = 3

PDF_SIGNATURE = b"%PDF-"

# Only these 3xx responses are followed as URL redirects. httpx marks 304 and
# other 3xx responses as ``is_redirect`` even when they are not URL redirects
# (see httpx Response.is_redirect); 304 is handled as ``NOT_MODIFIED`` before
# this check, so trailing 3xx statuses (305/306/uncached) fall through to the
# non-200 rejection path explicitly.
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


@dataclass(frozen=True)
class ConditionalValidators:
    """The conditional validators carried by a ``resume_source_state`` row.

    ``etag`` is retained exactly as received for HTTP semantics.
    ``last_modified`` is a timezone-aware ``datetime`` that is formatted as a
    valid HTTP date when sent and stored back as a datetime when received.
    """

    etag: str | None = None
    last_modified: datetime | None = None

    @property
    def has_any(self) -> bool:
        return self.etag is not None or self.last_modified is not None


@dataclass(frozen=True)
class FetchOutcome:
    """Typed outcome of one conditional fetch.

    ``body`` is only set for a successful ``200 OK`` outcome and is consumed
    immediately by the extraction step; CLI/log serialization never touches it.

    Validator presence is tracked *independently* so a 304 that returns only an
    ETag (or only a Last-Modified) updates only the validator the response
    actually returned and preserves the other stored value. ``etag_returned``
    is True iff the response carried an ``ETag`` header; ``last_modified_returned``
    is True iff the response carried a parseable ``Last-Modified`` value. The
    :attr:`returned_validators` aggregate property is kept for stable log
    fields and matches the closed boolean the CLI/JSON payload exposes.
    """

    kind: FetchOutcomeKind
    http_status: int | None = None
    etag: str | None = None
    etag_returned: bool = False
    last_modified: datetime | None = None
    last_modified_returned: bool = False
    byte_count: int = 0
    body: bytes | None = None
    fetched_at: datetime | None = None
    reason: str = ""
    sent_validators: bool = False

    @property
    def returned_validators(self) -> bool:
        """Aggregate boolean: either validator was returned by the response."""

        return self.etag_returned or self.last_modified_returned


class _RedirectLoop(Exception):
    pass


def _validate_https(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError("fetcher.http_not_https")
    if not parsed.netloc:
        raise ValueError("fetcher.invalid_url")
    if parsed.username or parsed.password:
        raise ValueError("fetcher.redirect_has_credentials")


def _http_date(value: datetime) -> str:
    dt = value.astimezone(UTC)
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    months = [
        "Jan",
        "Feb",
        "Mar",
        "Apr",
        "May",
        "Jun",
        "Jul",
        "Aug",
        "Sep",
        "Oct",
        "Nov",
        "Dec",
    ]
    weekday = days[dt.weekday()]
    month = months[dt.month - 1]
    return (
        f"{weekday}, {dt.day:02d} {month} {dt.year:04d} "
        f"{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d} GMT"
    )


def _parse_http_date(value: str) -> datetime | None:
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _build_headers(validators: ConditionalValidators) -> tuple[dict[str, str], bool]:
    headers: dict[str, str] = {}
    sent = False
    if validators.etag is not None:
        headers["If-None-Match"] = validators.etag
        sent = True
    if validators.last_modified is not None:
        headers["If-Modified-Since"] = _http_date(validators.last_modified)
        sent = True
    return headers, sent


def _validator_returned_flags(
    etag: str | None, last_modified: datetime | None
) -> tuple[bool, bool]:
    """Return ``(etag_returned, last_modified_returned)`` for an HTTP response.

    ``etag_returned`` is True iff the ETag header was present (``etag`` is not
    None — httpx returns ``None`` for an absent header). ``last_modified_returned``
    is True iff the ``Last-Modified`` header was present AND parseable to a
    timezone-aware datetime (a malformed timestamp carries no usable validator
    and is treated as not-returned so the existing stored value is preserved).
    """

    return etag is not None, last_modified is not None


def _safe_http_error_reason(exc: httpx.HTTPError) -> str:
    """Return a closed reason code for an httpx error.

    Never echoes URL, headers, or body content. Class-level mapping so a raw
    exception string cannot escape through a log or result object.
    """

    if isinstance(exc, httpx.ConnectTimeout):
        return "fetcher.connect_timeout"
    if isinstance(exc, httpx.ReadTimeout):
        return "fetcher.read_timeout"
    if isinstance(exc, httpx.ConnectError):
        return "fetcher.connect_error"
    if isinstance(exc, httpx.RemoteProtocolError):
        return "fetcher.protocol_error"
    if isinstance(exc, httpx.TooManyRedirects):
        return "fetcher.too_many_redirects"
    return "fetcher.http_error"


def fetch_resume(
    url: str,
    *,
    max_bytes: int,
    validators: ConditionalValidators | None = None,
    transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None = None,
    client: httpx.Client | None = None,
) -> FetchOutcome:
    """Perform one bounded conditional HTTPS fetch.

    ``client`` lets tests pass an already-constructed ``httpx.Client`` with a
    mock transport. When ``client`` is ``None`` a streaming ``httpx.Client`` is
    constructed with the production timeout/redirect/transport settings and is
    closed before return. The configured ``url`` is validated as absolute
    HTTPS without credentials before any network call.

    Redirects are followed manually with a hard ceiling of :data:`MAX_REDIRECTS`
    so every hop is revalidated as absolute HTTPS without credentials.
    """

    try:
        _validate_https(url)
    except ValueError as exc:
        return FetchOutcome(
            kind=FetchOutcomeKind.REJECTED,
            reason=str(exc.args[0]) if exc.args else "fetcher.invalid_url",
        )

    conditional = validators or ConditionalValidators()
    headers, sent_validators = _build_headers(conditional)
    request_headers = {"User-Agent": USER_AGENT, **headers}

    owns_client = client is None
    if client is None:
        client = httpx.Client(
            transport=transport if isinstance(transport, httpx.BaseTransport) else None,
            timeout=httpx.Timeout(
                connect=CONNECT_TIMEOUT,
                read=READ_TIMEOUT,
                write=WRITE_TIMEOUT,
                pool=POOL_TIMEOUT,
            ),
            follow_redirects=False,
        )

    try:
        return _follow_and_consume(client, url, request_headers, max_bytes, sent_validators)
    except ValueError as exc:
        return FetchOutcome(
            kind=FetchOutcomeKind.REJECTED,
            reason=str(exc.args[0]) if exc.args else "fetcher.invalid_redirect",
        )
    except _RedirectLoop as exc:
        return FetchOutcome(
            kind=FetchOutcomeKind.REJECTED,
            reason=str(exc.args[0]) if exc.args else "fetcher.redirect_overflow",
        )
    except httpx.HTTPError as exc:
        return FetchOutcome(
            kind=FetchOutcomeKind.ERROR,
            reason=_safe_http_error_reason(exc),
        )
    finally:
        if owns_client:
            client.close()


def _follow_and_consume(
    client: httpx.Client,
    url: str,
    headers: dict[str, str],
    max_bytes: int,
    sent_validators: bool,
) -> FetchOutcome:
    current = url
    redirects = 0
    while True:
        # The request must always be sent with the descriptive user agent and
        # the conditional headers. httpx's ``stream`` opens a streaming reader.
        with client.stream("GET", current, headers=headers) as response:
            if response.status_code in _REDIRECT_STATUSES:
                if redirects >= MAX_REDIRECTS:
                    raise _RedirectLoop("fetcher.redirect_overflow")
                location = response.headers.get("location", "")
                if not location:
                    return FetchOutcome(
                        kind=FetchOutcomeKind.REJECTED,
                        http_status=response.status_code,
                        reason="fetcher.missing_redirect_location",
                    )
                next_url = urljoin(current, location)
                try:
                    _validate_https(next_url)
                except ValueError as exc:
                    return FetchOutcome(
                        kind=FetchOutcomeKind.REJECTED,
                        http_status=response.status_code,
                        reason=str(exc.args[0]),
                    )
                redirects += 1
                current = next_url
                # Close this streamed response and loop. ``with`` releases it.
                continue
            return _consume_stream(response, max_bytes, sent_validators)


def _consume_stream(
    response: httpx.Response,
    max_bytes: int,
    sent_validators: bool,
) -> FetchOutcome:
    status = response.status_code
    if status == 304:
        return _not_modified(response, sent_validators)
    if status != 200:
        return FetchOutcome(
            kind=FetchOutcomeKind.REJECTED,
            http_status=status,
            reason="fetcher.non_200_status",
        )

    media_type = response.headers.get("content-type", "")
    if not _is_pdf_media_type(media_type):
        return FetchOutcome(
            kind=FetchOutcomeKind.REJECTED,
            http_status=status,
            reason="fetcher.wrong_media_type",
        )

    declared = response.headers.get("content-length")
    if declared is not None:
        try:
            declared_len = int(declared)
        except ValueError:
            return FetchOutcome(
                kind=FetchOutcomeKind.REJECTED,
                http_status=status,
                reason="fetcher.invalid_content_length",
            )
        if declared_len > max_bytes:
            return FetchOutcome(
                kind=FetchOutcomeKind.REJECTED,
                http_status=status,
                byte_count=declared_len,
                reason="fetcher.content_length_exceeds_limit",
            )

    body = bytearray()
    overflow = False
    try:
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) > max_bytes:
                overflow = True
                break
    except httpx.HTTPError as exc:
        return FetchOutcome(
            kind=FetchOutcomeKind.ERROR,
            http_status=status,
            reason=_safe_http_error_reason(exc),
        )

    if overflow:
        return FetchOutcome(
            kind=FetchOutcomeKind.REJECTED,
            http_status=status,
            byte_count=len(body),
            reason="fetcher.streamed_overflow",
        )

    if not body:
        return FetchOutcome(
            kind=FetchOutcomeKind.REJECTED,
            http_status=status,
            byte_count=0,
            reason="fetcher.empty_body",
        )

    if not body.startswith(PDF_SIGNATURE):
        return FetchOutcome(
            kind=FetchOutcomeKind.REJECTED,
            http_status=status,
            byte_count=len(body),
            reason="fetcher.missing_pdf_signature",
        )

    return _ok(response, bytes(body), sent_validators)


def _is_pdf_media_type(media_type: str) -> bool:
    base = media_type.split(";", 1)[0].strip().lower()
    return base == "application/pdf"


def _not_modified(response: httpx.Response, sent_validators: bool) -> FetchOutcome:
    etag = response.headers.get("etag")
    lm_header = response.headers.get("last-modified")
    last_modified = _parse_http_date(lm_header) if lm_header else None
    etag_returned, last_modified_returned = _validator_returned_flags(etag, last_modified)
    return FetchOutcome(
        kind=FetchOutcomeKind.NOT_MODIFIED,
        http_status=304,
        etag=etag,
        etag_returned=etag_returned,
        last_modified=last_modified,
        last_modified_returned=last_modified_returned,
        sent_validators=sent_validators,
        fetched_at=datetime.now(UTC),
    )


def _ok(
    response: httpx.Response,
    body: bytes,
    sent_validators: bool,
) -> FetchOutcome:
    etag = response.headers.get("etag")
    lm_header = response.headers.get("last-modified")
    last_modified = _parse_http_date(lm_header) if lm_header else None
    etag_returned, last_modified_returned = _validator_returned_flags(etag, last_modified)
    return FetchOutcome(
        kind=FetchOutcomeKind.OK,
        http_status=200,
        etag=etag,
        etag_returned=etag_returned,
        last_modified=last_modified,
        last_modified_returned=last_modified_returned,
        byte_count=len(body),
        body=body,
        sent_validators=sent_validators,
        fetched_at=datetime.now(UTC),
    )
