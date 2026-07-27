"""Bounded asynchronous adapter for Lever's public v0 Postings API.

The adapter accepts only reviewed global and EU :class:`SourceEndpoint`
values, fetches bare JSON arrays sequentially with bounded ``skip``/``limit``
pagination, and normalizes independently valid rows onto the frozen ATS
contract. Every partial or inconsistent observation fails closed with
``complete=False``.

HTTP, redirect, pagination, normalization, Markdown, and test-injection policy
is intentionally local to this module. No database, workflow, API, or generic
HTTP abstraction is involved.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections import Counter
from typing import Any, Final
from urllib.parse import parse_qsl, unquote_to_bytes, urljoin, urlparse, urlunsplit

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
    "LeverAdapter",
    "LeverEndpointError",
    "LeverError",
    "LeverTransportError",
]

logger = logging.getLogger("app.sources.ats.lever")

slug: Final[str] = "lever"
rate_limit_per_min: Final[int] = 60

_ORIGINS: Final[dict[str, str]] = {
    "global": "api.lever.co",
    "eu": "api.eu.lever.co",
}
_HOSTED_ORIGINS: Final[dict[str, str]] = {
    "global": "jobs.lever.co",
    "eu": "jobs.eu.lever.co",
}
_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_COUNTRY_RE: Final[re.Pattern[str]] = re.compile(r"[A-Z]{2}", re.ASCII)
_HEX_DIGITS: Final[frozenset[str]] = frozenset("0123456789abcdefABCDEF")

_CONNECT_TIMEOUT: Final[float] = 5.0
_READ_TIMEOUT: Final[float] = 30.0
_WRITE_TIMEOUT: Final[float] = 5.0
_POOL_TIMEOUT: Final[float] = 5.0
_USER_AGENT: Final[str] = "ForksTech-Jobs/0.1 (+https://jobs.forkstech.com)"
_ACCEPT: Final[str] = "application/json"

_PAGE_LIMIT: Final[int] = 100
_MAX_PAGE_RESPONSES: Final[int] = 100
_MAX_ROWS: Final[int] = 10_000
_MAX_REDIRECTS: Final[int] = 3
_MAX_HTTP_RESPONSES: Final[int] = 103
_MAX_BYTES: Final[int] = 10_485_760
_REDIRECT_STATUSES: Final[frozenset[int]] = frozenset({301, 302, 303, 307, 308})
_WORKPLACE_TYPES: Final[frozenset[str]] = frozenset({"unspecified", "on-site", "remote", "hybrid"})

_TRANSPORT_CATEGORIES: Final[dict[type[httpx.HTTPError], str]] = {
    httpx.ConnectTimeout: "lever.transport.connect_timeout",
    httpx.ReadTimeout: "lever.transport.read_timeout",
    httpx.WriteTimeout: "lever.transport.write_timeout",
    httpx.PoolTimeout: "lever.transport.pool_timeout",
    httpx.ConnectError: "lever.transport.connect_error",
    httpx.ReadError: "lever.transport.read_error",
    httpx.WriteError: "lever.transport.write_error",
    httpx.RemoteProtocolError: "lever.transport.protocol_error",
    httpx.LocalProtocolError: "lever.transport.protocol_error",
}


class LeverError(Exception):
    """Typed adapter failure exposing only a bounded category code."""

    category: str = "lever.adapter_error"

    def __init__(self, category: str | None = None) -> None:
        chosen = category if category is not None else self.category
        super().__init__(chosen)
        self.category = chosen


class LeverEndpointError(LeverError):
    """Endpoint rejected before client construction or network I/O."""

    category = "lever.endpoint_invalid"


class LeverTransportError(LeverError):
    """Transport, timeout, TLS, protocol, or streaming failure."""

    category = "lever.transport_error"


def _transport_category(exc: httpx.HTTPError) -> str:
    for exc_type, category in _TRANSPORT_CATEGORIES.items():
        if isinstance(exc, exc_type):
            return category
    return "lever.transport_error"


def _sorted(postings: list[RawPosting]) -> list[RawPosting]:
    return sorted(
        postings,
        key=lambda posting: (posting.external_id, posting.title, posting.posting_url),
    )


def _result(
    postings: list[RawPosting],
    *,
    complete: bool,
    status: int,
    validators: tuple[str | None, str | None],
) -> FetchResult:
    return FetchResult(
        postings=_sorted(postings),
        complete=complete,
        http_status=status,
        etag=validators[0],
        last_modified=validators[1],
    )


def _page_url(host: str, token: str, skip: int) -> str:
    path = f"/v0/postings/{token}"
    query = f"mode=json&skip={skip}&limit={_PAGE_LIMIT}"
    return urlunsplit(("https", host, path, query, ""))


def _safe_redirect_target(
    current_url: str,
    location: str,
    *,
    host: str,
    token: str,
    skip: int,
) -> str | None:
    if not location or not location.strip():
        return None
    try:
        target = urljoin(current_url, location)
        parsed = urlparse(target)
        port = parsed.port
        query = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    except (TypeError, ValueError):
        return None
    if parsed.scheme != "https" or parsed.hostname != host:
        return None
    if port is not None and port != 443:
        return None
    if parsed.username or parsed.password or parsed.fragment:
        return None
    if parsed.path != f"/v0/postings/{token}":
        return None
    expected = Counter(
        [
            ("mode", "json"),
            ("skip", str(skip)),
            ("limit", str(_PAGE_LIMIT)),
        ]
    )
    if len(query) != 3 or Counter(query) != expected:
        return None
    return target


async def _strip_unparseable_redirect_location(response: httpx.Response) -> None:
    """Prevent httpx from parsing a malformed redirect before manual policy.

    With automatic redirects disabled, httpx still constructs
    ``response.next_request`` for a redirect and may reject a malformed
    ``Location`` first. Removing only syntactically unparseable locations lets
    the adapter return the actual redirect status. Well-formed unsafe targets
    remain present and are rejected by the page-aware manual guard.
    """

    if response.status_code not in _REDIRECT_STATUSES:
        return
    location = response.headers.get("location", "")
    if not location or not location.strip():
        if "location" in response.headers:
            del response.headers["location"]
        return
    base = str(response.request.url) if response.request else ""
    try:
        resolved = urljoin(base, location)
        parsed = urlparse(resolved)
        _ = parsed.port
    except (TypeError, ValueError):
        if "location" in response.headers:
            del response.headers["location"]


def _install_location_hook(client: httpx.AsyncClient) -> None:
    hooks = client._event_hooks.setdefault("response", [])
    if _strip_unparseable_redirect_location not in hooks:
        hooks.append(_strip_unparseable_redirect_location)


class LeverAdapter:
    """Lever public-postings fetcher implementing the frozen ATS protocol."""

    slug = slug
    rate_limit_per_min = rate_limit_per_min

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._transport = transport
        self._owns_client = client is None
        if client is None:
            self._client = None
        else:
            _install_location_hook(client)
            self._client = client

    @staticmethod
    def _validate_endpoint(endpoint: SourceEndpoint) -> tuple[str, str, str]:
        if endpoint.kind != "lever":
            raise LeverEndpointError("lever.endpoint_invalid_kind")
        if endpoint.region not in _ORIGINS:
            raise LeverEndpointError("lever.endpoint_invalid_region")
        if endpoint.base_url is not None:
            raise LeverEndpointError("lever.endpoint_explicit_base_url")
        token = endpoint.token
        if not isinstance(token, str) or _TOKEN_RE.fullmatch(token) is None:
            raise LeverEndpointError("lever.endpoint_invalid_token")
        return token, endpoint.region, _ORIGINS[endpoint.region]

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
                event_hooks={"response": [_strip_unparseable_redirect_location]},
            )
        return self._client

    async def _close_owned_client(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    @staticmethod
    def _headers(
        conditional: ConditionalHeaders | None = None,
    ) -> dict[str, str]:
        headers = {"User-Agent": _USER_AGENT, "Accept": _ACCEPT}
        if conditional is not None:
            if conditional.etag is not None:
                headers["If-None-Match"] = conditional.etag
            if conditional.last_modified is not None:
                headers["If-Modified-Since"] = conditional.last_modified
        return headers

    async def list_postings(
        self,
        endpoint: SourceEndpoint,
        conditional: ConditionalHeaders,
    ) -> FetchResult:
        token, region, host = self._validate_endpoint(endpoint)
        conditional_copy = ConditionalHeaders(
            etag=conditional.etag,
            last_modified=conditional.last_modified,
        )
        client = self._ensure_client()
        try:
            return await self._fetch_pages(
                client,
                token=token,
                region=region,
                host=host,
                conditional=conditional_copy,
            )
        except httpx.HTTPError as exc:
            raise LeverTransportError(_transport_category(exc)) from None
        finally:
            await self._close_owned_client()

    def token_patterns(self) -> list[re.Pattern[str]]:
        """Return stable, host-bound patterns retaining global/EU region."""

        return [
            _PATTERN_HOSTED_GLOBAL,
            _PATTERN_HOSTED_EU,
            _PATTERN_API_GLOBAL,
            _PATTERN_API_EU,
        ]

    async def _fetch_pages(
        self,
        client: httpx.AsyncClient,
        *,
        token: str,
        region: str,
        host: str,
        conditional: ConditionalHeaders,
    ) -> FetchResult:
        postings: list[RawPosting] = []
        seen_ids: set[str] = set()
        seen_pages: set[str] = set()
        validators: tuple[str | None, str | None] = (None, None)
        skip = 0
        page_count = 0
        redirect_count = 0
        response_count = 0
        decoded_bytes = 0
        had_invalid_row = False
        status = 200

        while page_count < _MAX_PAGE_RESPONSES:
            current = _page_url(host, token, skip)
            page_headers = self._headers(conditional if skip == 0 else None)

            while True:
                if response_count >= _MAX_HTTP_RESPONSES:
                    return self._logged_result(
                        postings,
                        complete=False,
                        status=status,
                        validators=validators,
                        region=region,
                        page_count=page_count,
                        redirect_count=redirect_count,
                        decoded_bytes=decoded_bytes,
                    )
                async with client.stream("GET", current, headers=page_headers) as response:
                    response_count += 1
                    status = response.status_code
                    if status in _REDIRECT_STATUSES:
                        if redirect_count >= _MAX_REDIRECTS:
                            return self._logged_result(
                                postings,
                                complete=False,
                                status=status,
                                validators=validators,
                                region=region,
                                page_count=page_count,
                                redirect_count=redirect_count,
                                decoded_bytes=decoded_bytes,
                            )
                        target = _safe_redirect_target(
                            current,
                            response.headers.get("location", ""),
                            host=host,
                            token=token,
                            skip=skip,
                        )
                        if target is None:
                            return self._logged_result(
                                postings,
                                complete=False,
                                status=status,
                                validators=validators,
                                region=region,
                                page_count=page_count,
                                redirect_count=redirect_count,
                                decoded_bytes=decoded_bytes,
                            )
                        redirect_count += 1
                        current = target
                        continue

                    page_count += 1
                    if skip == 0:
                        validators = (
                            response.headers.get("etag"),
                            response.headers.get("last-modified"),
                        )
                    if status != 200:
                        return self._logged_result(
                            postings,
                            complete=False,
                            status=status,
                            validators=validators,
                            region=region,
                            page_count=page_count,
                            redirect_count=redirect_count,
                            decoded_bytes=decoded_bytes,
                        )

                    body, page_bytes = await _read_bounded_body(
                        response,
                        cumulative_before=decoded_bytes,
                    )
                    decoded_bytes += page_bytes
                    if body is None:
                        return self._logged_result(
                            postings,
                            complete=False,
                            status=200,
                            validators=validators,
                            region=region,
                            page_count=page_count,
                            redirect_count=redirect_count,
                            decoded_bytes=decoded_bytes,
                        )
                    break

            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                return self._logged_result(
                    postings,
                    complete=False,
                    status=200,
                    validators=validators,
                    region=region,
                    page_count=page_count,
                    redirect_count=redirect_count,
                    decoded_bytes=decoded_bytes,
                )
            if not isinstance(payload, list) or len(payload) > _PAGE_LIMIT:
                return self._logged_result(
                    postings,
                    complete=False,
                    status=200,
                    validators=validators,
                    region=region,
                    page_count=page_count,
                    redirect_count=redirect_count,
                    decoded_bytes=decoded_bytes,
                )
            page_signature = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if skip != 0 and page_signature in seen_pages:
                return self._logged_result(
                    postings,
                    complete=False,
                    status=200,
                    validators=validators,
                    region=region,
                    page_count=page_count,
                    redirect_count=redirect_count,
                    decoded_bytes=decoded_bytes,
                )

            page_ids: set[str] = set()
            duplicate_id = False
            for row in payload:
                if not isinstance(row, dict):
                    continue
                row_id_raw = row.get("id")
                if isinstance(row_id_raw, str) and (row_id := row_id_raw.strip()):
                    if row_id in page_ids or row_id in seen_ids:
                        duplicate_id = True
                        break
                    page_ids.add(row_id)
            if duplicate_id:
                return self._logged_result(
                    postings,
                    complete=False,
                    status=200,
                    validators=validators,
                    region=region,
                    page_count=page_count,
                    redirect_count=redirect_count,
                    decoded_bytes=decoded_bytes,
                )

            page_postings: list[RawPosting] = []
            for row in payload:
                if not isinstance(row, dict):
                    had_invalid_row = True
                    continue
                normalized = _normalize_posting(row, token=token, region=region)
                if normalized is None:
                    had_invalid_row = True
                    continue
                page_postings.append(normalized)
            postings.extend(page_postings)
            seen_ids.update(page_ids)
            seen_pages.add(page_signature)

            row_count = len(payload)
            if row_count < _PAGE_LIMIT:
                return self._logged_result(
                    postings,
                    complete=not had_invalid_row,
                    status=200,
                    validators=validators,
                    region=region,
                    page_count=page_count,
                    redirect_count=redirect_count,
                    decoded_bytes=decoded_bytes,
                )

            next_skip = skip + row_count
            if next_skip <= skip or next_skip > _MAX_ROWS or next_skip != page_count * _PAGE_LIMIT:
                return self._logged_result(
                    postings,
                    complete=False,
                    status=200,
                    validators=validators,
                    region=region,
                    page_count=page_count,
                    redirect_count=redirect_count,
                    decoded_bytes=decoded_bytes,
                )
            skip = next_skip
            if skip >= _MAX_ROWS:
                return self._logged_result(
                    postings,
                    complete=False,
                    status=200,
                    validators=validators,
                    region=region,
                    page_count=page_count,
                    redirect_count=redirect_count,
                    decoded_bytes=decoded_bytes,
                )

        return self._logged_result(
            postings,
            complete=False,
            status=status,
            validators=validators,
            region=region,
            page_count=page_count,
            redirect_count=redirect_count,
            decoded_bytes=decoded_bytes,
        )

    @staticmethod
    def _logged_result(
        postings: list[RawPosting],
        *,
        complete: bool,
        status: int,
        validators: tuple[str | None, str | None],
        region: str,
        page_count: int,
        redirect_count: int,
        decoded_bytes: int,
    ) -> FetchResult:
        result = _result(
            postings,
            complete=complete,
            status=status,
            validators=validators,
        )
        logger.info(
            "lever.fetch.outcome",
            extra={
                "event_name": "lever.fetch.outcome",
                "region": region,
                "http_status": status,
                "page_count": page_count,
                "redirect_count": redirect_count,
                "decoded_byte_count": decoded_bytes,
                "posting_count": len(result.postings),
                "complete": complete,
            },
        )
        return result


async def _read_bounded_body(
    response: httpx.Response,
    *,
    cumulative_before: int,
) -> tuple[bytes | None, int]:
    declared = response.headers.get("content-length")
    if declared is not None:
        try:
            declared_length = int(declared)
        except ValueError:
            declared_length = None
        if declared_length is not None and declared_length >= 0:
            if declared_length > _MAX_BYTES:
                return None, 0
            if cumulative_before + declared_length > _MAX_BYTES:
                return None, 0

    buffer = bytearray()
    consumed = 0
    async for chunk in response.aiter_bytes():
        chunk_length = len(chunk)
        if consumed + chunk_length > _MAX_BYTES:
            return None, consumed
        if cumulative_before + consumed + chunk_length > _MAX_BYTES:
            return None, consumed
        buffer.extend(chunk)
        consumed += chunk_length
    return bytes(buffer), consumed


def _normalize_posting(
    row: dict[str, Any],
    *,
    token: str,
    region: str,
) -> RawPosting | None:
    external_id_raw = row.get("id")
    title_raw = row.get("text")
    description_raw = row.get("description")
    if not isinstance(external_id_raw, str) or not isinstance(title_raw, str):
        return None
    external_id = external_id_raw.strip()
    title = title_raw.strip()
    if not external_id or not title or not isinstance(description_raw, str):
        return None

    description_md = _description_markdown(row)
    if description_md is None:
        return None
    posting_url = _hosted_url(
        row.get("hostedUrl"),
        token=token,
        external_id=external_id,
        region=region,
    )
    apply_url = _hosted_url(
        row.get("applyUrl"),
        token=token,
        external_id=external_id,
        region=region,
    )
    if posting_url is None or apply_url is None:
        return None

    locations = _normalize_locations(row)
    department = _normalize_department(row.get("categories"))
    return RawPosting(
        external_id=external_id,
        title=title,
        locations=locations,
        description_md=description_md,
        posting_url=posting_url,
        apply_url=apply_url,
        source_published_at=None,
        source_updated_at=None,
        department=department,
        raw=_deep_copy(row),
    )


def _hosted_url(
    value: object,
    *,
    token: str,
    external_id: str,
    region: str,
) -> str | None:
    if not isinstance(value, str):
        return None
    if any(character.isspace() or unicodedata.category(character) == "Cc" for character in value):
        return None
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.hostname != _HOSTED_ORIGINS[region]:
        return None
    if port is not None and port != 443:
        return None
    if parsed.username or parsed.password:
        return None
    parts = parsed.path.split("/")
    if len(parts) < 3 or parts[0] != "" or parts[1] != token:
        return None
    decoded_id = _strict_path_segment(parts[2])
    if decoded_id is None or decoded_id != external_id:
        return None
    return value


def _strict_path_segment(segment: str) -> str | None:
    """Decode one posting-ID segment once, rejecting ambiguous path content."""

    for index, character in enumerate(segment):
        if character == "%" and (
            index + 2 >= len(segment)
            or segment[index + 1] not in _HEX_DIGITS
            or segment[index + 2] not in _HEX_DIGITS
        ):
            return None
    try:
        decoded = unquote_to_bytes(segment).decode("utf-8")
    except UnicodeDecodeError:
        return None
    if any(
        character in {"/", "\\"} or character == "\x00" or unicodedata.category(character) == "Cc"
        for character in decoded
    ):
        return None
    return decoded


def _normalize_locations(row: dict[str, Any]) -> list[RawLocation]:
    categories = row.get("categories")
    labels: list[str] = []
    if isinstance(categories, dict):
        all_locations = categories.get("allLocations")
        if isinstance(all_locations, list) and all(
            isinstance(label, str) for label in all_locations
        ):
            seen: set[str] = set()
            for label_raw in all_locations:
                label = label_raw.strip()
                if label and label not in seen:
                    seen.add(label)
                    labels.append(label)
        if not labels:
            fallback = categories.get("location")
            if isinstance(fallback, str) and fallback.strip():
                labels = [fallback.strip()]

    country_raw = row.get("country")
    country = (
        country_raw if isinstance(country_raw, str) and _COUNTRY_RE.fullmatch(country_raw) else None
    )
    workplace_raw = row.get("workplaceType")
    workplace = (
        workplace_raw
        if isinstance(workplace_raw, str) and workplace_raw in _WORKPLACE_TYPES
        else None
    )
    return [
        RawLocation(
            label=label,
            country_code=country,
            region=None,
            city=None,
            workplace_type=workplace,
        )
        for label in labels
    ]


def _normalize_department(categories: object) -> str | None:
    if not isinstance(categories, dict):
        return None
    department = categories.get("department")
    if not isinstance(department, str):
        return None
    trimmed = department.strip()
    return trimmed or None


def _description_markdown(row: dict[str, Any]) -> str | None:
    description = row.get("description")
    if not isinstance(description, str):
        return None
    blocks: list[str] = []
    converted_description = _convert_html(description)
    if converted_description:
        blocks.append(converted_description)

    lists = row.get("lists")
    if lists is not None:
        if not isinstance(lists, list):
            return None
        for item in lists:
            if not isinstance(item, dict):
                return None
            heading_raw = item.get("text")
            content = item.get("content")
            if not isinstance(heading_raw, str) or not isinstance(content, str):
                return None
            heading = re.sub(r"[ \t]+", " ", heading_raw.strip(" \t"))
            section_parts: list[str] = []
            if heading:
                section_parts.append(f"## {heading}")
            converted_list = _convert_html(f"<ul>{content}</ul>")
            if converted_list:
                section_parts.append(converted_list)
            section = _canonical_markdown("\n\n".join(section_parts))
            if section:
                blocks.append(section)

    additional = row.get("additional")
    if additional not in (None, ""):
        if not isinstance(additional, str):
            return None
        converted_additional = _convert_html(additional)
        if converted_additional:
            blocks.append(converted_additional)
    return _canonical_markdown("\n\n".join(blocks))


def _convert_html(fragment: str) -> str:
    converted = markdownify(
        fragment,
        heading_style="ATX",
        bullets="*",
        strong_em_symbol="*",
        escape_asterisks=False,
        escape_underscores=False,
    )
    return _canonical_markdown(converted)


def _canonical_markdown(value: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip(" \t") for line in normalized.split("\n")]
    output: list[str] = []
    blank_count = 0
    for line in lines:
        if line == "":
            blank_count += 1
            if blank_count > 2:
                continue
        else:
            blank_count = 0
        output.append(line)
    return "\n".join(output).strip("\n")


def _deep_copy(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _deep_copy(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_deep_copy(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_deep_copy(item) for item in value)
    return value


_TOKEN_GLOBAL = r"(?P<token>[A-Za-z0-9][A-Za-z0-9_-]{0,127})"
_TOKEN_EU = _TOKEN_GLOBAL
_REGION_GLOBAL = r"(?P<region>)"
_REGION_EU = r"(?P<region>eu)"
_SAFE_ID = r"(?!(?i:(?:\.|%2e){1,2})(?:[/?#]|$))[^/?#\s]+"
_QUERY_FRAGMENT = r"(?:\?[^?#\s]*)?(?:#[^?\s]*)?$"
_HOSTED_SUFFIX = rf"(?:/{_SAFE_ID}(?:/apply)?)?/?{_QUERY_FRAGMENT}"
_API_SUFFIX = rf"(?:/{_SAFE_ID})?/?{_QUERY_FRAGMENT}"

_PATTERN_HOSTED_GLOBAL: Final[re.Pattern[str]] = re.compile(
    rf"^https://jobs{_REGION_GLOBAL}\.lever\.co/{_TOKEN_GLOBAL}{_HOSTED_SUFFIX}"
)
_PATTERN_HOSTED_EU: Final[re.Pattern[str]] = re.compile(
    rf"^https://jobs\.{_REGION_EU}\.lever\.co/{_TOKEN_EU}{_HOSTED_SUFFIX}"
)
_PATTERN_API_GLOBAL: Final[re.Pattern[str]] = re.compile(
    rf"^https://api{_REGION_GLOBAL}\.lever\.co/v0/postings/{_TOKEN_GLOBAL}{_API_SUFFIX}"
)
_PATTERN_API_EU: Final[re.Pattern[str]] = re.compile(
    rf"^https://api\.{_REGION_EU}\.lever\.co/v0/postings/{_TOKEN_EU}{_API_SUFFIX}"
)
