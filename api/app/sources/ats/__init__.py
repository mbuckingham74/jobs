"""Shared ATS adapter contract and one asynchronous Greenhouse adapter.

The :mod:`app.sources.ats.contracts` module defines the frozen source-layer
``SourceEndpoint``, ``RawLocation``, ``RawPosting``, ``FetchResult`` and
``ConditionalHeaders`` dataclasses plus the asynchronous :class:`ATSAdapter`
protocol. :mod:`app.sources.ats.greenhouse` implements the Task-004 Greenhouse
adapter for the documented public ``GET /v1/boards/{token}/jobs?content=true``
endpoint.

This package is deliberately independent of FastAPI, SQLAlchemy, repository
code, pipeline code, and the résumé modules. Importing it never touches the
database or the network.
"""

from app.sources.ats.contracts import (
    ATSAdapter,
    ConditionalHeaders,
    FetchResult,
    RawLocation,
    RawPosting,
    SourceEndpoint,
)

__all__ = [
    "ATSAdapter",
    "ConditionalHeaders",
    "FetchResult",
    "RawLocation",
    "RawPosting",
    "SourceEndpoint",
]
