"""Alembic migration environment for jobs-api.

The database URL is read only from the ``DATABASE_URL`` environment variable at
runtime so no real connection string is tracked. When the variable is absent the
command fails with a clear message instead of falling back to a default.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Ensure the local package is importable so app.* is available to migrations.
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit(
            "DATABASE_URL is not set. Alembic reads the runtime database URL "
            "from the environment; no connection string is tracked."
        )
    return url


# Alembic reads this attribute set on the config object.
config.set_main_option("sqlalchemy.url", _database_url())

# A future schema task will assign the declarative metadata's target_metadata.
# Until then there are no application tables; offline and online migrations both
# proceed against the existing (empty) revision graph.
target_metadata = None


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
