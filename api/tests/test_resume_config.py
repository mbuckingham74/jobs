"""Tests for resume runtime configuration resolution.

Covers defaults, a lower configured byte limit, the hard 10 MiB ceiling,
invalid limits, non-HTTPS URLs, and credential-bearing URLs. The health
endpoint and FastAPI app remain importable and reachable without any résumé or
database configuration, asserted separately.
"""

from __future__ import annotations

import pytest

from app.resume.config import (
    BASE_RESUME_URL_ENV,
    DATABASE_URL_ENV,
    MAX_BYTES_HARD_CEILING,
    RESUME_MAX_BYTES_ENV,
    ConfigError,
    ResumeSettings,
    load_settings,
)


def _base_environ(**overrides: str) -> dict[str, str]:
    env = {
        BASE_RESUME_URL_ENV: "https://example.invalid/portfolio.pdf",
        DATABASE_URL_ENV: "postgresql+psycopg://jobs:dev@127.0.0.1:5432/jobs",
    }
    env.update(overrides)
    return env


def test_defaults_to_hard_ceiling_when_unset() -> None:
    settings = load_settings(environ=_base_environ())
    assert settings.resume_max_bytes == MAX_BYTES_HARD_CEILING
    assert settings.resume_max_bytes == 10 * 1024 * 1024
    assert settings.variant == "base"
    assert settings.source_kind == "portfolio_pdf"


def test_lower_max_bytes_is_honored() -> None:
    settings = load_settings(environ=_base_environ(**{RESUME_MAX_BYTES_ENV: "2048"}))
    assert settings.resume_max_bytes == 2048


def test_max_bytes_above_ceiling_is_clamped() -> None:
    settings = load_settings(
        environ=_base_environ(**{RESUME_MAX_BYTES_ENV: str(MAX_BYTES_HARD_CEILING + 1)})
    )
    assert settings.resume_max_bytes == MAX_BYTES_HARD_CEILING


def test_zero_max_bytes_is_rejected() -> None:
    with pytest.raises(ConfigError):
        load_settings(environ=_base_environ(**{RESUME_MAX_BYTES_ENV: "0"}))


def test_negative_max_bytes_is_rejected() -> None:
    with pytest.raises(ConfigError):
        load_settings(environ=_base_environ(**{RESUME_MAX_BYTES_ENV: "-1024"}))


def test_non_integer_max_bytes_is_rejected() -> None:
    with pytest.raises(ConfigError):
        load_settings(environ=_base_environ(**{RESUME_MAX_BYTES_ENV: "10 MB"}))


def test_missing_database_url_is_config_error() -> None:
    env = {
        BASE_RESUME_URL_ENV: "https://example.invalid/portfolio.pdf",
    }
    with pytest.raises(ConfigError):
        load_settings(environ=env)


def test_missing_base_resume_url_is_config_error() -> None:
    env = {DATABASE_URL_ENV: "postgresql+psycopg://jobs:dev@127.0.0.1:5432/jobs"}
    with pytest.raises(ConfigError):
        load_settings(environ=env)


def test_http_url_is_rejected() -> None:
    with pytest.raises(ConfigError):
        load_settings(
            environ=_base_environ(**{BASE_RESUME_URL_ENV: "http://example.invalid/x.pdf"})
        )


def test_non_absolute_path_is_rejected() -> None:
    with pytest.raises(ConfigError):
        load_settings(environ=_base_environ(**{BASE_RESUME_URL_ENV: "https://example.invalid/"}))


def test_username_in_url_is_rejected() -> None:
    with pytest.raises(ConfigError):
        load_settings(
            environ=_base_environ(**{BASE_RESUME_URL_ENV: "https://user@example.invalid/x.pdf"})
        )


def test_password_in_url_is_rejected() -> None:
    with pytest.raises(ConfigError):
        load_settings(
            environ=_base_environ(**{BASE_RESUME_URL_ENV: "https://user:pw@example.invalid/x.pdf"})
        )


def test_settings_is_frozen() -> None:
    settings = load_settings(environ=_base_environ())
    with pytest.raises((AttributeError, TypeError)):
        settings.database_url = "x"  # type: ignore[misc]


def test_health_endpoint_remains_importable_without_config() -> None:
    # Importing app.main and hitting /healthz must not require BASE_RESUME_URL
    # or DATABASE_URL (spec/AGENTS.md invariant).
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_settings_preserves_url_value() -> None:
    env = _base_environ(**{BASE_RESUME_URL_ENV: "https://example.invalid/path/portfolio.pdf"})
    settings = load_settings(environ=env)
    assert settings.base_resume_url == "https://example.invalid/path/portfolio.pdf"
    assert isinstance(settings, ResumeSettings)
