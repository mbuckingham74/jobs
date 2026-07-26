"""Alembic migration environment for jobs-api.

The database URL is read only from the runtime ``DATABASE_URL`` environment
variable (see ``app.migration_support``) and passed directly to the offline
migration configuration and to a directly created SQLAlchemy engine for online
migrations. It is never written to the Alembic ConfigParser, and connection
errors are converted to a redacted ``SystemExit`` so the unmasked connection
string cannot leak through logs or stack traces.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool

# Ensure the local package is importable so app.* is available to migrations.
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app.migration_support import connect_or_exit, require_database_url  # noqa: E402

config = context.config

if config is not None and config.config_file_name is not None:
    fileConfig(config.config_file_name)

# A future schema task will assign the declarative metadata's target_metadata.
# Until then there are no application tables; offline and online migrations both
# proceed against the existing (empty) revision graph.
target_metadata = None


def run_migrations_offline() -> None:
    url = require_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = require_database_url()
    engine, connection = connect_or_exit(url, pool.NullPool)
    try:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )

        with context.begin_transaction():
            context.run_migrations()
    finally:
        connection.close()
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
