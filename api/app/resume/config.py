"""Runtime configuration for portfolio résumé sync.

Resolves ``DATABASE_URL``, ``BASE_RESUME_URL``, and ``RESUME_MAX_BYTES`` from
the environment and validates them against the rules in Task 003 and
specification v0.13 section 11. The health endpoint and FastAPI application do
not import this module, so importing ``app.main:app`` and calling
``GET /healthz`` still works when no résumé or database configuration is set.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

DATABASE_URL_ENV = "DATABASE_URL"
BASE_RESUME_URL_ENV = "BASE_RESUME_URL"
RESUME_MAX_BYTES_ENV = "RESUME_MAX_BYTES"
TEST_DATABASE_URL_ENV = "TEST_DATABASE_URL"

# Hard ceiling for the configured portfolio PDF. Task 003 fixes this at 10 MiB
# and forbids raising it through configuration; a lower configured value is
# honored, an equal or higher value is clamped to the ceiling.
MAX_BYTES_HARD_CEILING = 10 * 1024 * 1024
DEFAULT_MAX_BYTES = MAX_BYTES_HARD_CEILING

# Variant and source kind this slice imports. Task 003 is base/portfolio_pdf
# only; product/program/technical and n8n/manual source kinds stay out of
# scope.
RESUME_VARIANT = "base"
RESUME_SOURCE_KIND = "portfolio_pdf"

# Stable transaction-scoped advisory-lock keys for base résumé sync. Two
# signed 32-bit integers are used (``pg_advisory_xact_lock(int4, int4)``) so the
# lock is uniquely identifiable in a documented namespace regardless of
# Postgres' process-randomized Python ``hash()``.
ADVISORY_LOCK_KEY_1 = 0x4A0B  # 'J' + 0x0B = "jobs" app namespace
ADVISORY_LOCK_KEY_2 = 0x0001  # base/portfolio_pdf source-sync slice

# Single fixed descriptive user agent. Section 11 requires a descriptive user
# agent with a contact address. This constant is the only UA the fetch
# component sends.
USER_AGENT = "jobs-resume-sync/0.1 " "(+https://jobs.forkstech.com; contact: michael@forkstech.com)"


class ConfigError(ValueError):
    """Raised when runtime configuration is missing or invalid.

    The message never contains the offending URL value, a connection string,
    or credentials; only the safe name of the failing environment variable and
    a short reason.
    """


@dataclass(frozen=True)
class ResumeSettings:
    database_url: str
    base_resume_url: str
    resume_max_bytes: int
    variant: str = RESUME_VARIANT
    source_kind: str = RESUME_SOURCE_KIND


def _validate_base_resume_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ConfigError(f"{BASE_RESUME_URL_ENV} must use an absolute https:// URL")
    if not parsed.netloc:
        raise ConfigError(f"{BASE_RESUME_URL_ENV} must include a network location")
    # Reject embedded credentials: userinfo is forbidden by Task 003.
    if parsed.username or parsed.password:
        raise ConfigError(f"{BASE_RESUME_URL_ENV} must not contain embedded credentials")
    if not parsed.path or parsed.path == "/":
        # Require a path: a bare host is not a portfolio PDF URL.
        raise ConfigError(f"{BASE_RESUME_URL_ENV} must point at a specific PDF path")
    return url


def _resolve_max_bytes(value: str) -> int:
    raw = value.strip()
    if not raw:
        return DEFAULT_MAX_BYTES
    try:
        parsed = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{RESUME_MAX_BYTES_ENV} must be a positive integer") from exc
    if parsed <= 0:
        raise ConfigError(f"{RESUME_MAX_BYTES_ENV} must be a positive integer")
    # A lower configured value is honored; the hard ceiling is never raised
    # above MAX_BYTES_HARD_CEILING.
    return min(parsed, MAX_BYTES_HARD_CEILING)


def load_settings(environ: dict[str, str] | None = None) -> ResumeSettings:
    """Build :class:`ResumeSettings` from the environment.

    Pass an explicit mapping for testing; production reads ``os.environ``.
    ``BASE_RESUME_URL`` is required; ``DATABASE_URL`` is required because the
    CLI always persists through PostgreSQL; ``RESUME_MAX_BYTES`` defaults to
    the 10 MiB hard ceiling and is clamped to that ceiling when larger.
    """

    source = environ if environ is not None else _read_environ()
    base_resume_url = source.get(BASE_RESUME_URL_ENV, "").strip()
    if not base_resume_url:
        raise ConfigError(f"{BASE_RESUME_URL_ENV} is required")
    database_url = source.get(DATABASE_URL_ENV, "").strip()
    if not database_url:
        raise ConfigError(f"{DATABASE_URL_ENV} is required")
    resume_max_bytes = _resolve_max_bytes(source.get(RESUME_MAX_BYTES_ENV, ""))
    return ResumeSettings(
        database_url=database_url,
        base_resume_url=_validate_base_resume_url(base_resume_url),
        resume_max_bytes=resume_max_bytes,
    )


def _read_environ() -> dict[str, str]:
    import os

    return dict(os.environ)
