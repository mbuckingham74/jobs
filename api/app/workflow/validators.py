"""Pure Task 007 conditional-validator reconstruction."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from app.sources.ats.contracts import ConditionalHeaders


def reconstruct_validators(rows: Iterable[Mapping[str, object]]) -> ConditionalHeaders:
    """Reconstruct each validator independently from newest eligible events.

    ``rows`` must already exclude the current run and null finishes and be
    ordered by ``finished_at DESC, id DESC``.
    """

    unresolved = object()
    etag: object = unresolved
    last_modified: object = unresolved
    for row in rows:
        status = row.get("status")
        http_status = row.get("http_status")
        error = row.get("error")
        is_200 = status == "complete" and http_status == 200 and error is None
        is_304 = status == "incomplete" and http_status == 304 and error is None
        if not (is_200 or is_304):
            continue
        if etag is unresolved and (is_200 or row.get("etag") is not None):
            etag = row.get("etag")
        if last_modified is unresolved and (is_200 or row.get("last_modified") is not None):
            last_modified = row.get("last_modified")
        if etag is not unresolved and last_modified is not unresolved:
            break
    return ConditionalHeaders(
        etag=None if etag is unresolved else etag,  # type: ignore[arg-type]
        last_modified=(
            None if last_modified is unresolved else last_modified  # type: ignore[arg-type]
        ),
    )
