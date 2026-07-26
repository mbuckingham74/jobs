"""Console script entry point for portfolio résumé sync.

Invoked as ``jobs-resume-sync``. The command runs exactly one sync attempt,
emits exactly one JSON result object to standard output, writes structured
operational logs to standard error, and exits ``0`` for the success statuses
(``not_modified``, ``unchanged_content``, ``activated_initial``,
``staged_for_review``) and nonzero for every failure status (invalid
configuration, fetch rejection/failure, extraction rejection/failure,
database failure, provenance conflict). On every path the CLI closes the HTTP
client, database session, and engine. The CLI never imports FastAPI and is
not exposed as an API route.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from app.resume.config import ConfigError, load_settings
from app.resume.logging import configure_logging, log_event
from app.resume.results import ResumeSyncResult, ResumeSyncStatus
from app.resume.service import sync_resume

SUCCESS_STATUSES = frozenset(
    {
        ResumeSyncStatus.NOT_MODIFIED,
        ResumeSyncStatus.UNCHANGED_CONTENT,
        ResumeSyncStatus.ACTIVATED_INITIAL,
        ResumeSyncStatus.STAGED_FOR_REVIEW,
    }
)

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_CONFIG_ERROR = 2


def result_to_payload(result: ResumeSyncResult) -> dict[str, Any]:
    """Render a result as a JSON-serializable dict.

    Never includes ``content_md``, extracted snippets, PDF bytes, response
    bodies, request/response header dumps, database URLs, or credentials.
    """

    payload: dict[str, Any] = {
        "status": result.status.value,
        "activated": result.activated,
        "content_changed": result.content_changed,
        "raw_changed": result.raw_changed,
        "sent_validators": result.sent_validators,
        "returned_validators": result.returned_validators,
    }
    if result.resume_version_id is not None:
        payload["resume_version_id"] = result.resume_version_id
    if result.http_status is not None:
        payload["http_status"] = result.http_status
    if result.byte_count is not None:
        payload["byte_count"] = result.byte_count
    if result.page_count is not None:
        payload["page_count"] = result.page_count
    if result.content_char_count is not None:
        payload["content_char_count"] = result.content_char_count
    if result.content_token_count is not None:
        payload["content_token_count"] = result.content_token_count
    if result.error is not None:
        payload["error"] = {"code": result.error.code}
        # ``detail`` is intentionally never echoed to stdout: a raw failure
        # suffix from a transport, extractor, or database can echo content or
        # a partial credential safely only in server-side stderr logs.
    return payload


def _exit_code_for(status: ResumeSyncStatus) -> int:
    if status in SUCCESS_STATUSES:
        return EXIT_OK
    if status is ResumeSyncStatus.CONFIG_ERROR:
        return EXIT_CONFIG_ERROR
    return EXIT_FAILURE


def run_once(
    *,
    argv: list[str] | None = None,
    environ: dict[str, str] | None = None,
    stdout: Any | None = None,
    stderr: Any | None = None,
    fetcher: Any | None = None,
    extractor: Any | None = None,
    fetch_outcome_override: Any | None = None,
) -> int:
    """Run one sync attempt and return the process exit code.

    ``argv`` is accepted so a future flag surface can be added without changing
    the entry-point signature. ``environ``, ``stdout``, and ``stderr`` let
    tests exercise the CLI without monkeypatching global state. ``fetcher``,
    ``extractor``, and ``fetch_outcome_override`` are passed straight through to
    :func:`app.resume.service.sync_resume` so tests can replace the HTTP and
    PDF boundaries without live network access or synthetic PDF fixtures.
    """

    out = stdout or sys.stdout
    logger = configure_logging()

    try:
        settings = load_settings(environ)
    except ConfigError as exc:
        log_event(logger, "resume.sync.config.error", status="config_error", reason=str(exc))
        out.write(
            json.dumps(
                {
                    "status": ResumeSyncStatus.CONFIG_ERROR.value,
                    "activated": False,
                    "content_changed": False,
                    "raw_changed": False,
                    "sent_validators": False,
                    "returned_validators": False,
                    "error": {"code": "config.invalid"},
                },
                sort_keys=True,
            )
            + "\n"
        )
        out.flush()
        return EXIT_CONFIG_ERROR

    result = sync_resume(
        settings,
        logger=logger,
        fetcher=fetcher,
        extractor=extractor,
        fetch_outcome_override=fetch_outcome_override,
    )
    out.write(json.dumps(result_to_payload(result), sort_keys=True) + "\n")
    out.flush()
    return _exit_code_for(result.status)


def main(argv: list[str] | None = None) -> int:
    """Console-script entry point returning a process exit code."""

    return run_once(argv=argv)


if __name__ == "__main__":  # pragma: no cover - exercised via the entry point
    raise SystemExit(main(sys.argv[1:]))
