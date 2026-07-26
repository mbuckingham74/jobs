# syntax=docker/dockerfile:1.7

# ---- base ----
FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# psycopg[binary] ships a self-contained wheel with its own bundled libpq, so no
# system package install is required. This keeps the image hermetic and small.

# ---- install ----
COPY pyproject.toml README.md ./
COPY app/ ./app/
COPY migrations/ ./migrations/
COPY alembic.ini ./

# Install the project (runtime deps only). Dev extras are not needed in the
# image because tests run locally in a venv, not inside the service container.
RUN pip install .

# Non-root system user for the service.
RUN groupadd --system app && \
    useradd --system --gid app --home-dir /app --shell /sbin/nologin app && \
    chown -R app:app /app

USER app

# Compose services override the command. Defaults preserve `app.main:app`.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers"]