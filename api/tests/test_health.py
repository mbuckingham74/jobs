"""Health endpoint tests.

The liveness endpoint is the container health check and must remain independent
of database, provider, and résumé URL availability.
"""

from fastapi.testclient import TestClient

from app.main import app


def test_healthz_returns_200_with_ok_payload() -> None:
    client = TestClient(app)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_healthz_is_independent_of_state() -> None:
    client = TestClient(app)
    first = client.get("/healthz")
    second = client.get("/healthz")
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == {"status": "ok"}
