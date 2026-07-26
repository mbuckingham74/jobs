"""Tests for the Alembic migration environment DATABASE_URL handling.

The migration environment must resolve the runtime DATABASE_URL directly,
never echo the unmasked connection string, and redact passwords — including
percent-encoded passwords — from any diagnostic it emits. These tests target
``app.migration_support`` because ``migrations/env.py`` only runs inside an
Alembic command and is not importable as a module.
"""

from __future__ import annotations

import pytest

from app.migration_support import (
    MISSING_URL_MESSAGE,
    connect_error_message,
    connect_or_exit,
    mask_url,
    require_database_url,
)


def test_require_database_url_raises_systemexit_when_missing() -> None:
    with pytest.raises(SystemExit) as exc_info:
        require_database_url(environ={})
    message = str(exc_info.value)
    assert "DATABASE_URL" in message
    # No connection-string-like content may leak through the missing path.
    assert "://" not in message
    assert "@" not in message
    assert message == MISSING_URL_MESSAGE


def test_require_database_url_does_not_leak_unrelated_env_values() -> None:
    environ = {"OTHER_URL": "postgresql+psycopg://leak:hunter2@host:5432/db"}
    with pytest.raises(SystemExit) as exc_info:
        require_database_url(environ=environ)
    message = str(exc_info.value)
    assert "hunter2" not in message
    assert "postgresql" not in message
    assert "host" not in message


def test_require_database_url_returns_value_when_present() -> None:
    url = "postgresql+psycopg://jobs:dev@db:5432/jobs"
    assert require_database_url(environ={"DATABASE_URL": url}) == url


def test_require_database_url_strips_whitespace_only_values() -> None:
    with pytest.raises(SystemExit):
        require_database_url(environ={"DATABASE_URL": "   "})


def test_mask_url_redacts_plain_password() -> None:
    masked = mask_url("postgresql+psycopg://jobs:hunter2@db:5432/jobs")
    assert "hunter2" not in masked
    assert "***" in masked
    assert "db:5432" in masked
    assert "/jobs" in masked


def test_mask_url_redacts_percent_encoded_password() -> None:
    url = "postgresql+psycopg://jobs:p%40ss%40word@db:5432/jobs"
    masked = mask_url(url)
    assert "p%40ss%40word" not in masked
    assert "p@ss@word" not in masked
    assert "***" in masked
    assert "db:5432" in masked
    assert "jobs@" not in masked  # username preserved but password redacted


def test_mask_url_preserves_url_without_password() -> None:
    url = "postgresql+psycopg://db:5432/jobs"
    assert mask_url(url) == url


def test_mask_url_redacts_password_without_username_or_port() -> None:
    masked = mask_url("postgresql+psycopg://:secret@host/db")
    assert "secret" not in masked
    assert "***" in masked


def test_connect_error_message_masks_percent_encoded_password() -> None:
    url = "postgresql+psycopg://jobs:p%40ss%40word@db:5432/jobs"
    message = connect_error_message(url)
    assert "p%40ss%40word" not in message
    assert "p@ss@word" not in message
    assert "***" in message
    assert "db:5432" in message


def test_connect_or_exit_raises_systemexit_with_redacted_message_on_connect_failure() -> None:
    # A percent-encoded password pointing at a closed local port. Connection is
    # refused quickly; psycopg raises OperationalError, which must be converted
    # to a SystemExit whose message never contains the unmasked password.
    url = "postgresql+psycopg://jobs:p%40ss%40word@127.0.0.1:1/jobs"
    with pytest.raises(SystemExit) as exc_info:
        connect_or_exit(url, connect_args={"connect_timeout": 2})
    message = str(exc_info.value)
    assert "p%40ss%40word" not in message
    assert "p@ss@word" not in message
    assert "***" in message
    assert "127.0.0.1:1" in message
    # The masked, not the raw, URL appears.
    assert "jobs:p%40ss" not in message
