-- PostgreSQL write-enforcement tests for the resume_source_state table.
--
-- Parallel in structure to api/tests/core_schema_enforcement.sql. Run against
-- the upgraded disposable PostgreSQL 16 + pgvector database after
-- ``alembic upgrade head``. It builds a baseline prerequisite row, exercises
-- each prohibited write independently, and asserts the catalog names exist
-- exactly as specified by Task 003 and specification v0.13 section 04.
--
-- Every attempt must be rejected by the intended constraint:
--   * SQLSTATE 23514 (check_violation) for the two enumerated checks,
--   * SQLSTATE 23505 (unique_violation) for the composite unique constraint,
--   * SQLSTATE 23503 (foreign_key_violation) for an invalid
--     ``current_resume_version_id`` pointer and for deletion through the
--     restricted foreign key.
-- If a prohibited write SUCCEEDS, or is rejected with a different SQLSTATE,
-- the script raises P0001 and aborts with a nonzero exit under
-- ``psql -v ON_ERROR_STOP=1``.
--
-- Everything runs inside a single transaction that finishes with ROLLBACK so
-- the catalog and the repeat-upgrade comparison are unaffected.

BEGIN;

CREATE OR REPLACE FUNCTION _jobs_expect_violation(
    p_stmt text,
    p_expected text,
    p_label text
) RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    raised boolean := FALSE;
BEGIN
    BEGIN
        EXECUTE p_stmt;
    EXCEPTION WHEN OTHERS THEN
        IF SQLSTATE <> p_expected THEN
            RAISE EXCEPTION '% -> expected SQLSTATE %, got % (% )',
                p_label, p_expected, SQLSTATE, SQLERRM
                USING ERRCODE = 'P0001';
        END IF;
        raised := TRUE;
    END;
    IF NOT raised THEN
        RAISE EXCEPTION '% -> prohibited write SUCCEEDED (expected %): % ',
            p_label, p_expected, p_stmt
            USING ERRCODE = 'P0001';
    END IF;
END
$$;

-- ------------------------------------------------------------------
-- Baseline prerequisite row: one base/portfolio_pdf resume_version the
-- foreign key can validly point at.
-- ------------------------------------------------------------------
INSERT INTO resume_version (id, variant, source_kind, content_hash, content_md, active)
VALUES (100, 'base', 'manual', 'rh-rss-100', 'm', FALSE);

-- ------------------------------------------------------------------
-- Section A: a valid ``resume_source_state`` row.
-- ------------------------------------------------------------------
INSERT INTO resume_source_state (
    id, variant, source_kind, source_url,
    source_etag, source_last_modified, last_checked_at,
    last_body_fetched_at, last_body_sha256, current_resume_version_id
) VALUES (
    1, 'base', 'portfolio_pdf',
    'https://example.invalid/portfolio.pdf', 'etag-1',
    now() - interval '1 hour', now(),
    now() - interval '1 hour', 'sha-prev', 100
);

-- ------------------------------------------------------------------
-- Section B: enumerated check violations.
-- ------------------------------------------------------------------
SELECT _jobs_expect_violation(
    $_$ INSERT INTO resume_source_state (variant, source_kind, source_url, last_checked_at)
        VALUES ('bogus', 'portfolio_pdf', 'https://example.invalid/x', now()) $_$,
    '23514', 'resume_source_state.variant invalid');
SELECT _jobs_expect_violation(
    $_$ INSERT INTO resume_source_state (variant, source_kind, source_url, last_checked_at)
        VALUES ('base', 'bogus', 'https://example.invalid/y', now()) $_$,
    '23514', 'resume_source_state.source_kind invalid');

-- ------------------------------------------------------------------
-- Section C: the composite unique constraint.
-- ------------------------------------------------------------------
SELECT _jobs_expect_violation(
    $_$ INSERT INTO resume_source_state (variant, source_kind, source_url, last_checked_at)
        VALUES ('base', 'portfolio_pdf',
                'https://example.invalid/portfolio.pdf', now()) $_$,
    '23505', 'resume_source_state composite unique (variant, source_kind, source_url)');

-- ------------------------------------------------------------------
-- Section D: the ``ON DELETE RESTRICT`` foreign key.
-- ------------------------------------------------------------------
-- An invalid ``current_resume_version_id`` pointer.
SELECT _jobs_expect_violation(
    $_$ INSERT INTO resume_source_state (variant, source_kind, source_url, last_checked_at, current_resume_version_id)
        VALUES ('base', 'portfolio_pdf',
                'https://example.invalid/dangling', now(), 999999) $_$,
    '23503', 'resume_source_state.current_resume_version_id dangling pointer');
-- Deleting a resume_version that is referenced through the restricted
-- foreign key must fail.
SELECT _jobs_expect_violation(
    $_$ DELETE FROM resume_version WHERE id = 100 $_$,
    '23503', 'resume_source_state ON DELETE RESTRICT blocks delete');

-- ------------------------------------------------------------------
-- Section E: every named constraint and index exists exactly as specified.
-- ------------------------------------------------------------------
DO $$
DECLARE
    expected_names text[] := ARRAY[
        'resume_source_state_pkey',
        'resume_source_state_variant_check',
        'resume_source_state_source_kind_check',
        'resume_source_state_variant_source_kind_source_url_key',
        'resume_source_state_current_resume_version_id_fkey'
    ];
    missing text[];
BEGIN
    SELECT array_agg(n) INTO missing
    FROM unnest(expected_names) AS n
    WHERE NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = n
    );
    IF missing IS NOT NULL THEN
        RAISE EXCEPTION 'missing named constraints: %', array_to_string(missing, ', ')
            USING ERRCODE = 'P0001';
    END IF;

    -- The named primary-key and composite unique constraints supply backing
    -- indexes with those same stable names.
    IF NOT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = 'resume_source_state_pkey') THEN
        RAISE EXCEPTION 'resume_source_state_pkey backing index missing' USING ERRCODE = 'P0001';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = 'resume_source_state_variant_source_kind_source_url_key') THEN
        RAISE EXCEPTION 'resume_source_state_variant_source_kind_source_url_key backing index missing'
            USING ERRCODE = 'P0001';
    END IF;

    -- No second or speculative index exists on resume_source_state.
    IF EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'public' AND tablename = 'resume_source_state'
          AND indexname NOT IN (
              'resume_source_state_pkey',
              'resume_source_state_variant_source_kind_source_url_key'
          )
    ) THEN
        RAISE EXCEPTION 'unexpected index on resume_source_state' USING ERRCODE = 'P0001';
    END IF;
END $$;

DROP FUNCTION _jobs_expect_violation(text, text, text);

\echo 'All resume_source_state enforcement cases observed expected rejections.'

ROLLBACK;