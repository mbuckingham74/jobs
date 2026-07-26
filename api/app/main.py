"""FastAPI application entrypoint for jobs.forkstech.com.

This module exposes the ASGI import path (`app.main:app`) referenced by the
root Compose file. It intentionally contains only the liveness endpoint; product
routes, database wiring, scheduling, and middleware arrive in later tasks.
"""

from fastapi import FastAPI

app = FastAPI(title="jobs.forkstech.com", docs_url=None, redoc_url=None)


@app.get("/healthz", summary="Liveness check")
async def healthz() -> dict[str, str]:
    """Return a fixed liveness payload.

    This endpoint is the container health check. It must not query PostgreSQL,
    call a provider, read the résumé URL, or depend on any other network
    service.
    """

    return {"status": "ok"}
