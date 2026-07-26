"""Asynchronous Greenhouse source adapter for the documented Phase 1 endpoint.

The adapter fetches exactly
``GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true``
after validating the supplied :class:`SourceEndpoint`, normalizes every
independently valid Greenhouse job row to a frozen :class:`RawPosting`, and
returns a :class:`FetchResult` marking the response complete only when the
entire payload was structurally valid.

HTTP policy lives entirely in this module:

- explicit timeouts of 5 s for connect/write/pool and 30 s for read;
- a fixed descriptive user agent and ``Accept: application/json``;
- conditional validators forwarded only on the initial request and never to a
  redirect hop;
- manual redirect following capped at three hops of types 301, 302, 303, 307,
  and 308; every followed target must remain on the exact
  ``https://boards-api.greenhouse.io`` origin with the default HTTPS port and
  no username/password;
- a 10 MiB ceiling on decoded bytes enforced before the body is read and
  aborted mid-stream when accumulated bytes exceed the ceiling (including when
  ``Content-Length`` is missing, malformed, or false);
- deterministic cleanup of every streamed response and any adapter-owned
  client on success, failure, cancellation, and size rejection.

Transport, timeout, and TLS failures raise the typed
:class:`GreenhouseTransportError` with a bounded privacy-safe category and no
response body, request headers, token, or raw provider exception in its public
string. Since no :class:`FetchResult` is produced, a transport failure cannot
authorize close-reconciliation. Task cancellation propagates after
deterministic cleanup and is never converted to a provider failure.
"""

from __future__ import annotations

import html
import logging
import re
from datetime import datetime
from typing import Any, Final
from urllib.parse import urlparse, urlunsplit

import httpx
from markdownify import markdownify

from app.sources.ats.contracts import (
    ConditionalHeaders,
    FetchResult,
    RawLocation,
    RawPosting,
    SourceEndpoint,
)

__all__ = [
    "GreenhouseAdapter",
    "GreenhouseEndpointError",
    "GreenhouseError",
    "GreenhouseTransportError",
]

logger = logging.getLogger("app.sources.ats.greenhouse")

# Adapter protocol surface. ``rate_limit_per_min`` is a conservative local
# scheduling hint, not a vendor-published quota. The adapter does not throttle
# or sleep; a later workflow owns the rate limiter.
slug: Final[str] = "greenhouse"
rate_limit_per_min: Final[int] = 60

# Exact origin the adapter may contact. The initial request and every followed
# redirect target must remain on this host over HTTPS with the default port and
# no userinfo; cross-origin, alternate-port, credential-bearing, and lookalike
# hosts are rejected before the next request is issued.
_ORIGIN_SCHEME: Final[str] = "https"
_ORIGIN_HOST: Final[str] = "boards-api.greenhouse.io"
_ORIGIN_NETLOC: Final[str] = _ORIGIN_HOST
_BASE_PATH: Final[str] = "/v1/boards"
_QUERY: Final[str] = "content=true"

# Adapter grammar for vendor board tokens. Validated by a full match before the
# token is used in any URL construction. The grammar is host-side safe: it
# never accepts characters used to inject path/query/host boundaries.
_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")

# Explicit timeout budget (seconds).
_CONNECT_TIMEOUT: Final[float] = 5.0
_READ_TIMEOUT: Final[float] = 30.0
_WRITE_TIMEOUT: Final[float] = 5.0
_POOL_TIMEOUT: Final[float] = 5.0

# At most three redirects; a fourth is reported as incomplete with the actual
# redirect status. 304 is not a URL redirect.
_MAX_REDIRECTS: Final[int] = 3
_REDIRECT_STATUSES: Final[frozenset[int]] = frozenset({301, 302, 303, 307, 308})

# 10 MiB hard ceiling on decoded response bytes handed to the JSON parser.
_MAX_BYTES: Final[int] = 10 * 1024 * 1024

# Fixed descriptive user agent and ``Accept`` header.
_USER_AGENT: Final[str] = "ForksTech-Jobs/0.1 (+https://jobs.forkstech.com)"
_ACCEPT: Final[str] = "application/json"

# Privacy-safe transport-failure category codes. Maps httpx exception types to
# short, closed strings and never echoes URL, headers, token, or raw provider
# exception text.
_TRANSPORT_CATEGORIES: Final[dict[type[httpx.HTTPError], str]] = {
    httpx.ConnectTimeout: "greenhouse.transport.connect_timeout",
    httpx.ReadTimeout: "greenhouse.transport.read_timeout",
    httpx.ConnectError: "greenhouse.transport.connect_error",
    httpx.RemoteProtocolError: "greenhouse.transport.protocol_error",
    httpx.LocalProtocolError: "greenhouse.transport.protocol_error",
}

# Bare redirect-hop headers (UA + Accept only, no conditional validators).
_BARE_HEADERS: Final[dict[str, str]] = {
    "User-Agent": _USER_AGENT,
    "Accept": _ACCEPT,
}

# Token-recognition patterns: shared compiled pattern objects returned (as
# fresh lists) by ``GreenhouseAdapter.token_patterns()``. Defined after the
# class because the patterns reference ``_TOKEN_GROUP`` which captures below.


class GreenhouseError(Exception):
    """Typed adapter error with a bounded, privacy-safe category.

    The public message is one short category code; it never contains a token,
    URL, header value, vendor payload, body bytes, or a raw provider exception
    that could echo any of those.
    """

    category: str = "greenhouse.adapter_error"

    def __init__(self, category: str | None = None) -> None:
        chosen = category if category is not None else self.category
        super().__init__(chosen)
        self.category = chosen


class GreenhouseEndpointError(GreenhouseError):
    """Raised before any network I/O when an endpoint is rejected by adapter
    policy. Produces no :class:`FetchResult` so no fetch can be authorized."""

    category = "greenhouse.endpoint_invalid"


class GreenhouseTransportError(GreenhouseError):
    """Raised for transport, timeout, or TLS failures. No :class:`FetchResult`
    is produced so a transport failure cannot authorize reconciliation."""

    category = "greenhouse.transport_error"


def _is_safe_origin(url: str) -> bool:
    """True iff ``url`` is on the exact origin, default HTTPS port, no creds.

    Hostname comparison is exact so a lookalike host such as
    ``boards-api.greenhouse.io.example.com`` cannot match. An explicit non-
    default port is rejected (alternate ports and lookalike endpoints). The
    presence of userinfo rejects a credential-bearing target.
    """

    parsed = urlparse(url)
    if parsed.scheme != _ORIGIN_SCHEME:
        return False
    if parsed.hostname != _ORIGIN_HOST:
        return False
    if parsed.port is not None:
        return False
    if parsed.username or parsed.password:
        return False
    return True


def _join_location(base: str, location: str) -> str:
    """Resolve a (possibly relative) ``Location`` header against the current URL."""

    if "://" in location:
        # Absolute Location: keep its own scheme/host/path/query/fragment.
        return location.split("#", 1)[0] if "#" in location else location
    parsed_base = urlparse(base)
    if location.startswith("//"):
        target = f"{parsed_base.scheme}:{location}"
        return target.split("#", 1)[0] if "#" in target else target
    if location.startswith("/"):
        roll = f"{parsed_base.scheme}://{parsed_base.netloc}{location}"
    elif location.startswith("?"):
        roll = f"{parsed_base.scheme}://{parsed_base.netloc}{parsed_base.path}{location}"
    else:
        base_dir = parsed_base.path.rsplit("/", 1)[0] + "/"
        roll = f"{parsed_base.scheme}://{parsed_base.netloc}{base_dir}{location}"
    return roll.split("#", 1)[0] if "#" in roll else roll


def _transport_category(exc: httpx.HTTPError) -> str:
    for exc_type, code in _TRANSPORT_CATEGORIES.items():
        if isinstance(exc, exc_type):
            return code
    return "greenhouse.transport_error"


def _final_validators(response: httpx.Response) -> dict[str, str | None]:
    etag = response.headers.get("etag")
    last_modified = response.headers.get("last-modified")
    return {"etag": etag, "last_modified": last_modified}


class GreenhouseAdapter:
    """Asynchronous Greenhouse board fetcher implementing :class:`ATSAdapter`."""

    slug = slug
    rate_limit_per_min = rate_limit_per_min

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Initialize the adapter.

        ``client`` lets tests inject a fully-constructed ``httpx.AsyncClient``
        that may carry an ``httpx.MockTransport`` (an ``AsyncBaseTransport``).
        When ``client`` is ``None`` the adapter owns a client bound to the
        optional ``transport`` with the explicit timeout, redirect, and user
        agent settings from this module and closes it on every return path.
        """

        self._client = client
        self._transport = transport
        self._owns_client = client is None

    # -- Transport seam ------------------------------------------------

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                transport=self._transport,
                timeout=httpx.Timeout(
                    connect=_CONNECT_TIMEOUT,
                    read=_READ_TIMEOUT,
                    write=_WRITE_TIMEOUT,
                    pool=_POOL_TIMEOUT,
                ),
                follow_redirects=False,
            )
        return self._client

    async def _close_owned_client(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- Endpoint validation ------------------------------------------

    @staticmethod
    def _validate_endpoint(endpoint: SourceEndpoint) -> str:
        if endpoint.kind != "greenhouse":
            raise GreenhouseEndpointError("greenhouse.endpoint_invalid_kind")
        if endpoint.region != "global":
            raise GreenhouseEndpointError("greenhouse.endpoint_invalid_region")
        if endpoint.base_url is not None:
            raise GreenhouseEndpointError("greenhouse.endpoint_explicit_base_url")
        token = endpoint.token
        if not isinstance(token, str) or _TOKEN_RE.fullmatch(token) is None:
            # The category carries no part of the supplied token.
            raise GreenhouseEndpointError("greenhouse.endpoint_invalid_token")
        # The URL is constructed only from a token that fully matched the
        # grammar; no string concatenation with an unvalidated token occurs.
        path = f"{_BASE_PATH}/{token}/jobs"
        return urlunsplit((_ORIGIN_SCHEME, _ORIGIN_NETLOC, path, _QUERY, ""))

    # -- Request construction -----------------------------------------

    @staticmethod
    def _initial_request_headers(conditional: ConditionalHeaders) -> dict[str, str]:
        headers: dict[str, str] = {
            "User-Agent": _USER_AGENT,
            "Accept": _ACCEPT,
        }
        # Forward each non-null validator exactly; absent ones are omitted;
        # never invent either header.
        if conditional.etag is not None:
            headers["If-None-Match"] = conditional.etag
        if conditional.last_modified is not None:
            headers["If-Modified-Since"] = conditional.last_modified
        return headers

    # -- ATSAdapter protocol ------------------------------------------

    async def list_postings(
        self,
        endpoint: SourceEndpoint,
        conditional: ConditionalHeaders,
    ) -> FetchResult:
        """Fetch and normalize postings for one validated Greenhouse endpoint."""

        url = self._validate_endpoint(endpoint)
        # Defensive snapshot of the caller's validators so neither this request
        # nor downstream normalization mutates the upstream object.
        headers = self._initial_request_headers(
            ConditionalHeaders(
                etag=conditional.etag,
                last_modified=conditional.last_modified,
            )
        )
        client = self._ensure_client()
        try:
            result = await self._follow_and_read(client, url, headers)
        except httpx.HTTPError as exc:
            raise GreenhouseTransportError(_transport_category(exc)) from None
        finally:
            await self._close_owned_client()
        return result

    def token_patterns(self) -> list[re.Pattern[str]]:
        """Return stable token-recognition patterns with a ``token`` named capture.

        Order is documented and stable: human-facing board URLs are listed
        before the API URL shape. Patterns are host-boundary safe and reject
        non-HTTPS URLs, credential-bearing URLs, lookalike hosts, missing
        tokens, path traversal, and out-of-grammar tokens. Recognition is a
        pure function with no network call and no endpoint persistence.
        """

        return [_PATTERN_BOARDS, _PATTERN_JOB_BOARDS, _PATTERN_EMBED, _PATTERN_API]

    # -- Bounded fetch + manual redirect following --------------------

    async def _follow_and_read(
        self,
        client: httpx.AsyncClient,
        url: str,
        initial_headers: dict[str, str],
    ) -> FetchResult:
        current = url
        redirects = 0
        first = True
        # Open each response as a streaming reader so the byte ceiling can abort
        # mid-body. ``async with``-style entry/finally is manual because the
        # redirect loop spans multiple responses; each one is closed either
        # after a hop or by the ``finally`` for the final/result path.
        while True:
            headers = initial_headers if first else _BARE_HEADERS
            response = await client.stream("GET", current, headers=headers).__aenter__()
            try:
                status = response.status_code
                if status in _REDIRECT_STATUSES:
                    if redirects >= _MAX_REDIRECTS:
                        self._log_outcome(
                            status=status,
                            complete=False,
                            byte_count=0,
                            postings=0,
                            redirect_count=redirects,
                        )
                        return FetchResult(
                            postings=[],
                            complete=False,
                            http_status=status,
                            **_final_validators(response),
                        )
                    self._log_redirect(status, redirects + 1)
                    next_url = self._resolve_redirect(response)
                    if next_url is None:
                        return FetchResult(
                            postings=[],
                            complete=False,
                            http_status=status,
                            **_final_validators(response),
                        )
                    redirects += 1
                    current = next_url
                    first = False
                    continue
                # Non-redirect: read a single terminal body with the byte ceiling.
                result = await self._read_outcome(response, redirect_count=redirects)
                self._log_outcome(
                    status=result.http_status,
                    complete=result.complete,
                    byte_count=0,
                    postings=len(result.postings),
                    redirect_count=redirects,
                )
                return result
            finally:
                # Mid-stream transport failure, cancellation, size rejection, or
                # an unexpected error: close this streamed response before
                # re-raising. ``CancelledError`` is not an ``httpx.HTTPError`` so
                # the outer handler maps only httpx failures to the typed
                # exception; cancellation propagates after deterministic cleanup.
                await response.aclose()

    @staticmethod
    def _resolve_redirect(response: httpx.Response) -> str | None:
        location = response.headers.get("location", "")
        if not location or not location.strip():
            # A missing or whitespace-only Location is malformed; the redirect
            # is treated as incomplete with the actual redirect status.
            return None
        base = str(response.request.url)
        next_url = _join_location(base, location)
        if not _is_safe_origin(next_url):
            return None
        return next_url

    @staticmethod
    async def _read_outcome(
        response: httpx.Response,
        *,
        redirect_count: int,
    ) -> FetchResult:
        status = response.status_code
        if status == 304:
            return FetchResult(
                postings=[],
                complete=False,
                http_status=304,
                **_final_validators(response),
            )
        if status == 429:
            return FetchResult(
                postings=[],
                complete=False,
                http_status=429,
                **_final_validators(response),
            )
        if status != 200:
            return FetchResult(
                postings=[],
                complete=False,
                http_status=status,
                **_final_validators(response),
            )
        return await _read_200(response, redirect_count=redirect_count)

    # -- Logging helpers ----------------------------------------------

    @staticmethod
    def _log_redirect(status: int, hop: int) -> None:
        logger.info(
            "greenhouse.fetch.redirect",
            extra={
                "event_name": "greenhouse.fetch.redirect",
                "http_status": status,
                "redirect_hop": hop,
            },
        )

    @staticmethod
    def _log_outcome(
        *,
        status: int,
        complete: bool,
        byte_count: int,
        postings: int,
        redirect_count: int,
    ) -> None:
        logger.info(
            "greenhouse.fetch.outcome",
            extra={
                "event_name": "greenhouse.fetch.outcome",
                "http_status": status,
                "complete": complete,
                "byte_count": byte_count,
                "postings": postings,
                "redirect_count": redirect_count,
            },
        )

    # -- Token-patterns (declared after use, kept private) -----------


# --------------------------------------------------------------------
# Token-recognition patterns (declared at module scope so the public
# ``token_patterns`` can return a stable fresh list without leaking the
# shared compiled-pattern identity.)
# --------------------------------------------------------------------

_TOKEN_GROUP = r"(?P<token>[A-Za-z0-9][A-Za-z0-9_-]{0,127})"

# Pattern 1: https://boards.greenhouse.io/{token} — anchor host exactly with
# etiquette: optional trailing slash, query, or fragment; the host boundary is
# ``$`` after the token so ``boards.greenhouse.io.example.com`` cannot match.
# We accept optional suffixes (trailing slash, /jobs, /jobs/{id}, query,
# fragment) so a comment may link to a single posting on the board.
_PATTERN_BOARDS: Final[re.Pattern[str]] = re.compile(
    rf"^https://boards\.greenhouse\.io/{_TOKEN_GROUP}" rf"(?:/(?:jobs(?:/[^/?#]+)?)?)?/?$"
)

_PATTERN_JOB_BOARDS: Final[re.Pattern[str]] = re.compile(
    rf"^https://job-boards\.greenhouse\.io/{_TOKEN_GROUP}" rf"(?:/(?:jobs(?:/[^/?#]+)?)?)?/?$"
)

_PATTERN_EMBED: Final[re.Pattern[str]] = re.compile(
    r"^https://boards\.greenhouse\.io/embed/job_board"
    r"(?:\?for=(?P<token>[A-Za-z0-9][A-Za-z0-9_-]{0,127}))?$"
)

_PATTERN_API: Final[re.Pattern[str]] = re.compile(
    rf"^https://boards-api\.greenhouse\.io/v1/boards/{_TOKEN_GROUP}/jobs$"
)


# --------------------------------------------------------------------
# 200 OK payload handling
# --------------------------------------------------------------------


async def _read_200(response: httpx.Response, *, redirect_count: int) -> FetchResult:
    """Enforce the decoded-byte ceiling before parsing and return the parsed
    :class:`FetchResult`. Transport failures during streaming raise
    :class:`httpx.HTTPError` so the caller maps them to the typed exception."""

    declared = response.headers.get("content-length")
    if declared is not None:
        try:
            declared_len = int(declared)
        except ValueError:
            declared_len = None
        # A declared length above the ceiling is rejected before consuming any
        # of the body.
        if declared_len is not None and declared_len > _MAX_BYTES:
            return FetchResult(
                postings=[],
                complete=False,
                http_status=200,
                **_final_validators(response),
            )
    buffer = bytearray()
    overflow = False
    # Streaming reader; ``aiter_bytes`` yields decoded bytes so the byte
    # ceiling applies to decoded content regardless of transfer-encoding.
    async for chunk in response.aiter_bytes():
        buffer.extend(chunk)
        if len(buffer) > _MAX_BYTES:
            overflow = True
            break
    if overflow:
        return FetchResult(
            postings=[],
            complete=False,
            http_status=200,
            **_final_validators(response),
        )
    try:
        text = bytes(buffer).decode("utf-8")
    except UnicodeDecodeError:
        return FetchResult(
            postings=[],
            complete=False,
            http_status=200,
            **_final_validators(response),
        )
    return _parse_payload(text, response)


def _parse_payload(text: str, response: httpx.Response) -> FetchResult:
    import json

    try:
        payload = json.loads(text)
    except ValueError:
        return _incomplete(response)
    if not isinstance(payload, dict):
        return _incomplete(response)
    jobs = payload.get("jobs")
    if not isinstance(jobs, list):
        return _incomplete(response)
    meta = payload.get("meta")
    if meta is not None and not isinstance(meta, dict):
        return _incomplete(response)
    if isinstance(meta, dict):
        total = meta.get("total")
        if isinstance(total, bool) or not isinstance(total, int) or total < 0 or total != len(jobs):
            return _incomplete(response)
    postings: list[RawPosting] = []
    skipped = False
    for job in jobs:
        if not isinstance(job, dict):
            skipped = True
            continue
        normalized = _normalize_job(job)
        if normalized is None:
            skipped = True
            continue
        postings.append(normalized)
    postings.sort(key=lambda p: (p.external_id, p.title, p.posting_url))
    complete = not skipped
    return FetchResult(
        postings=postings,
        complete=complete,
        http_status=200,
        **_final_validators(response),
    )


def _incomplete(response: httpx.Response) -> FetchResult:
    return FetchResult(
        postings=[],
        complete=False,
        http_status=200,
        **_final_validators(response),
    )


# --------------------------------------------------------------------
# Normalization
# --------------------------------------------------------------------


def _normalize_job(job: dict[str, Any]) -> RawPosting | None:
    """Normalize one Greenhouse job row, or ``None`` when the row is invalid.

    A row is invalid when its ``id`` is not a positive non-Boolean JSON
    integer, ``title`` is missing/empty/trimmed-empty, ``content`` is missing
    or non-string, or ``absolute_url`` is missing or not a credential-free
    HTTP/HTTPS URL with a host. Optional malformed locations, departments, and
    timestamps degrade only those optional fields.
    """

    external_id_raw = job.get("id")
    if isinstance(external_id_raw, bool) or not isinstance(external_id_raw, int):
        return None
    if external_id_raw <= 0:
        return None
    title_raw = job.get("title")
    if not isinstance(title_raw, str):
        return None
    title = title_raw.strip()
    if not title:
        return None
    content_raw = job.get("content")
    if not isinstance(content_raw, str):
        return None
    description_md = _convert_content(content_raw)
    posting_url = _public_url(job.get("absolute_url"))
    if posting_url is None:
        return None
    locations = _normalize_locations(job.get("location"))
    department = _normalize_department(job.get("departments"))
    published_at = _parse_time(job.get("first_published"))
    updated_at = _parse_time(job.get("updated_at"))
    # Defensive deep copy: the snapshot must not share mutable children with
    # the caller's decoded object and downstream code cannot mutate it back.
    raw_snapshot = _deep_copy(job)
    return RawPosting(
        external_id=str(external_id_raw),
        title=title,
        locations=locations,
        description_md=description_md,
        posting_url=posting_url,
        # The documented list response exposes one public candidate
        # destination, ``absolute_url``; copy it exactly into both contract
        # fields without inventing an ``#app`` fragment or an authenticated
        # application API URL. The fields are populated and asserted
        # independently so a vendor that supplies distinct URLs later can
        # preserve them.
        apply_url=posting_url,
        source_published_at=published_at,
        source_updated_at=updated_at,
        department=department,
        raw=raw_snapshot,
    )


def _public_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https"):
        return None
    if parsed.username or parsed.password:
        return None
    if not parsed.hostname:
        return None
    # Preserve query string and fragment exactly as supplied.
    return value


def _normalize_locations(value: object) -> list[RawLocation]:
    if not isinstance(value, dict):
        return []
    name = value.get("name")
    if not isinstance(name, str):
        return []
    label = name.strip()
    if not label:
        return []
    # Preserve only the vendor label; do not infer country, region, city,
    # remote status, or workplace type from punctuation, offices, title, or
    # description text.
    return [RawLocation(label=label)]


def _normalize_department(value: object) -> str | None:
    if not isinstance(value, list):
        return None
    for item in value:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if isinstance(name, str):
            trimmed = name.strip()
            if trimmed:
                return trimmed
    return None


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    # Accept ISO 8601 offset forms and a trailing ``Z``; reject naive
    # datetimes rather than assuming a timezone.
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        return None
    return parsed


def _deep_copy(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _deep_copy(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_deep_copy(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(_deep_copy(v) for v in obj)
    return obj


# --------------------------------------------------------------------
# Deterministic HTML -> Markdown conversion (Task 004 section 6).
# --------------------------------------------------------------------


def _convert_content(content: str) -> str:
    """Reviewed six-step conversion from a Greenhouse ``content`` string to
    deterministic Markdown.

    1. ``html.unescape`` exactly once;
    2. ``markdownify`` with fixed options (ATX headings, ``*`` bullets, ``*``
       strong/em emphasis, no asterisk/underscore escaping, ``script`` and
       ``style`` rendering suppressed by the parser's own convert handlers);
    3. normalize CR/CRLF to LF;
    4. remove trailing horizontal whitespace from every line;
    5. collapse runs of three or more blank lines to exactly two; and
    6. strip leading and trailing blank lines without a terminal newline.

    The converter is a pure deterministic function. ``markdownify``'s built-in
    ``convert_script``/``convert_style`` empty handlers ensure script and style
    contents never appear as visible Markdown.
    """

    unescaped = html.unescape(content)
    converted = markdownify(
        unescaped,
        heading_style="ATX",
        bullets="*",
        strong_em_symbol="*",
        escape_asterisks=False,
        escape_underscores=False,
    ).strip()
    normalized = converted.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip("\t ") for line in normalized.split("\n")]
    collapsed = _collapse_blank_runs(lines)
    stripped = "\n".join(collapsed).strip("\n")
    return stripped.strip("\t ").strip("\n")


def _collapse_blank_runs(lines: list[str]) -> list[str]:
    out: list[str] = []
    blank_run = 0
    for line in lines:
        if line == "":
            blank_run += 1
            if blank_run >= 3:
                # Cap consecutive blanks at two.
                continue
            out.append(line)
        else:
            blank_run = 0
            out.append(line)
    return out
