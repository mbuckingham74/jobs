"""Portfolio résumé bootstrap slice (Task 003).

This package implements the smallest end-to-end Phase 1 path that imports the
configured portfolio résumé PDF into ``resume_version`` and uses the mutable
``resume_source_state`` table added by revision ``0002_resume_source_state``
to hold current HTTP/source observation state separately from immutable
``resume_version`` provenance.

The package is deliberately independent of FastAPI: only the console script
``jobs-resume-sync`` imports it, so the application's ``app.main:app`` import
path stays usable without database, résumé URL, or HTTP configuration.
"""

from app.resume.results import (
    FetchOutcomeKind,
    ResumeSyncResult,
    ResumeSyncResultError,
    ResumeSyncStatus,
)

__all__ = [
    "FetchOutcomeKind",
    "ResumeSyncStatus",
    "ResumeSyncResult",
    "ResumeSyncResultError",
]
