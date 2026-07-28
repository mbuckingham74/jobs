"""PostgreSQL migration backfill and ownership enforcement for revision 0003."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.postgres


def test_migration_backfill_ownership_and_reversibility(postgres_url, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", postgres_url)
    config = Config("alembic.ini")
    command.downgrade(config, "0002_resume_source_state")
    engine = create_engine(postgres_url)
    observed = datetime(2026, 7, 1, tzinfo=UTC)
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO company (id, name) VALUES (9101, 'MigrationCo')"))
            conn.execute(
                text(
                    """
                    INSERT INTO source_endpoint
                        (id, company_id, kind, token, region, status)
                    VALUES (9201, 9101, 'greenhouse', 'migration-token', 'global', 'active')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO pipeline_run
                        (id, run_kind, status, config_version)
                    VALUES (9301, 'daily', 'running', 'migration-test')
                    """
                )
            )
            for posting_id, external_id in ((9401, "with-versions"), (9402, "legacy-empty")):
                conn.execute(
                    text(
                        """
                        INSERT INTO posting
                            (id, company_id, source_endpoint_id, external_id,
                             title, title_norm, posting_url, apply_url)
                        VALUES
                            (:id, 9101, 9201, :external_id,
                             'Role', 'role', 'https://example.invalid/job',
                             'https://example.invalid/apply')
                        """
                    ),
                    {"id": posting_id, "external_id": external_id},
                )
            versions = [
                (9501, observed),
                (9502, observed + timedelta(hours=1)),
                (9503, observed + timedelta(hours=1)),
            ]
            for version_id, observed_at in versions:
                conn.execute(
                    text(
                        """
                        INSERT INTO posting_version
                            (id, posting_id, observed_in_run_id, content_hash,
                             title, raw_payload, observed_at)
                        VALUES
                            (:id, 9401, 9301, :hash, 'Role', '{}'::jsonb, :observed)
                        """
                    ),
                    {
                        "id": version_id,
                        "hash": f"hash-{version_id}",
                        "observed": observed_at,
                    },
                )
        engine.dispose()
        command.upgrade(config, "head")
        engine = create_engine(postgres_url)
        with engine.connect() as conn:
            rows = dict(
                conn.execute(text("SELECT id, current_version_id FROM posting ORDER BY id")).all()
            )
            assert rows == {9401: 9503, 9402: None}
            fk = conn.execute(
                text(
                    """
                    SELECT pg_get_constraintdef(oid, true)
                    FROM pg_constraint
                    WHERE conname = 'posting_current_version_owner_fkey'
                    """
                )
            ).scalar_one()
            assert "DEFERRABLE INITIALLY DEFERRED" in fk
            assert "FOREIGN KEY (id, current_version_id)" in fk
            column = conn.execute(
                text(
                    """
                    SELECT data_type, is_nullable
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'posting'
                      AND column_name = 'current_version_id'
                    """
                )
            ).one()
            assert column == ("bigint", "YES")
            index_definition = conn.execute(
                text(
                    """
                    SELECT indexdef
                    FROM pg_indexes
                    WHERE schemaname = 'public'
                      AND indexname = 'posting_current_version_id_idx'
                    """
                )
            ).scalar_one()
            assert "USING btree (current_version_id)" in index_definition
            ownership_key = conn.execute(
                text(
                    """
                    SELECT pg_get_constraintdef(oid, true)
                    FROM pg_constraint
                    WHERE conname = 'posting_version_posting_id_id_key'
                    """
                )
            ).scalar_one()
            assert ownership_key == "UNIQUE (posting_id, id)"

        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        INSERT INTO posting_version
                            (id, posting_id, observed_in_run_id, content_hash,
                             title, raw_payload, observed_at)
                        VALUES
                            (9504, 9402, 9301, 'other-owner', 'Role', '{}'::jsonb, :observed)
                        """
                    ),
                    {"observed": observed},
                )
                conn.execute(text("UPDATE posting SET current_version_id = 9504 WHERE id = 9401"))
                conn.execute(text("SET CONSTRAINTS posting_current_version_owner_fkey IMMEDIATE"))

        engine.dispose()
        command.downgrade(config, "0002_resume_source_state")
        engine = create_engine(postgres_url)
        with engine.connect() as conn:
            assert (
                conn.execute(
                    text(
                        """
                    SELECT count(*)
                    FROM information_schema.columns
                    WHERE table_name = 'posting' AND column_name = 'current_version_id'
                    """
                    )
                ).scalar_one()
                == 0
            )
        engine.dispose()
        command.upgrade(config, "head")
        engine = create_engine(postgres_url)
    finally:
        engine.dispose()
        command.upgrade(config, "head")
        engine = create_engine(postgres_url)
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM source_fetch WHERE endpoint_id = 9201"))
            conn.execute(text("DELETE FROM posting_version WHERE posting_id IN (9401, 9402)"))
            conn.execute(text("DELETE FROM posting WHERE id IN (9401, 9402)"))
            conn.execute(text("DELETE FROM pipeline_run WHERE id = 9301"))
            conn.execute(text("DELETE FROM source_endpoint WHERE id = 9201"))
            conn.execute(text("DELETE FROM company WHERE id = 9101"))
        engine.dispose()
