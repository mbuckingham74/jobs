"""Typed results for portfolio résumé sync.

The orchestration function, CLI, and tests exchange values from one small set
of frozen dataclasses rather than passing dicts or raising. No result carries
``content_md``, extracted snippets, PDF bytes, request/response payloads,
header dumps, database URLs, or credentials. Only safe counts and identifiers
travel on a result, so any result may be serialized to a CLI stdout JSON
object or to a structured log event without separate redaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ResumeSyncStatus(str, Enum):
    """Closed status set for the resume-sync result object."""

    # Success outcomes (CLI exits 0).
    NOT_MODIFIED = "not_modified"
    UNCHANGED_CONTENT = "unchanged_content"
    ACTIVATED_INITIAL = "activated_initial"
    STAGED_FOR_REVIEW = "staged_for_review"

    # Failure outcomes (CLI exits nonzero).
    CONFIG_ERROR = "config_error"
    FETCH_REJECTED = "fetch_rejected"
    FETCH_ERROR = "fetch_error"
    EXTRACTION_REJECTED = "extraction_rejected"
    EXTRACTION_ERROR = "extraction_error"
    DB_ERROR = "db_error"
    PROVENANCE_CONFLICT = "provenance_conflict"


class FetchOutcomeKind(str, Enum):
    """Closed status set for the conditional-fetch component."""

    NOT_MODIFIED = "not_modified"
    OK = "ok"
    REJECTED = "rejected"
    ERROR = "error"


@dataclass(frozen=True)
class ResumeSyncResultError:
    """A safe failure-reason value.

    ``reason`` is a stable, closed reason code. ``detail`` may carry a short
    human-readable suffix but must never contain résumé text, bytes, headers,
    credentials, or a database URL. The CLI never echoes ``detail`` if it
    cannot prove it is safe.
    """

    code: str
    detail: str = ""


@dataclass(frozen=True)
class ResumeSyncResult:
    """The single structured outcome of one resume-sync attempt.

    Safe fields documented by Task 003:

    - ``status`` — closed status set above;
    - ``resume_version_id`` — created or matched ``resume_version.id``;
    - ``http_status`` — observed HTTP status code;
    - ``byte_count`` — raw PDF response byte count for a body-bearing response;
    - ``page_count`` — number of PDF pages accounted for by extraction;
    - ``content_char_count`` — number of Unicode letter-or-number characters
      in the normalized Markdown (a safe count, never an excerpt);
    - ``content_token_count`` — count of maximal contiguous letter-or-number
      runs in the normalized Markdown;
    - ``activated`` — whether a resume_version was left active by this run;
    - ``content_changed`` — whether the run produced/inserted normalized
      content that differs from the matched current version;
    - ``raw_changed`` — whether the raw PDF bytes differ from the previously
      observed ``last_body_sha256``;
    - ``sent_validators`` / ``returned_validators`` — booleans describing
      whether conditional validators were sent or returned;
    - ``error`` — optional structured failure reason.
    """

    status: ResumeSyncStatus
    resume_version_id: int | None = None
    http_status: int | None = None
    byte_count: int | None = None
    page_count: int | None = None
    content_char_count: int | None = None
    content_token_count: int | None = None
    activated: bool = False
    content_changed: bool = False
    raw_changed: bool = False
    sent_validators: bool = False
    returned_validators: bool = False
    error: ResumeSyncResultError | None = None

    @property
    def ok(self) -> bool:
        return self.status in {
            ResumeSyncStatus.NOT_MODIFIED,
            ResumeSyncStatus.UNCHANGED_CONTENT,
            ResumeSyncStatus.ACTIVATED_INITIAL,
            ResumeSyncStatus.STAGED_FOR_REVIEW,
        }
