-- PostgreSQL write-enforcement tests for the Phase 1 core schema.
--
-- This script is run against the upgraded disposable PostgreSQL 16 + pgvector
-- database (see tasks/002-core-schema.md, "PostgreSQL enforcement tests"). It
-- builds an otherwise-valid prerequisite baseline, then attempts each
-- prohibited write independently. Every attempt must be rejected by the
-- intended constraint with SQLSTATE 23514 (check_violation) for enumerated
-- and deep-score cases, or 23505 (unique_violation) for the partial-unique
-- active-version cases. If a prohibited write SUCCEEDS, or is rejected with a
-- different SQLSTATE (for example an unexpected foreign-key 23503 from setup),
-- the script raises P0001 and aborts with a nonzero exit under
-- `psql -v ON_ERROR_STOP=1`.
--
-- Everything runs inside a single transaction that finishes with ROLLBACK so
-- the catalog and the repeat-upgrade comparison are unaffected: no rows and no
-- helper function persist.

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
-- Baseline prerequisite rows. Inserted with explicit ids so the prohibited
-- foreign keys below reference known parents. Every column not listed has a
-- server-side default or is nullable.
-- ------------------------------------------------------------------
INSERT INTO company (id, name) VALUES (1, 'TestCo');
INSERT INTO company (id, name) VALUES (2, 'TestCoTwo');

INSERT INTO source_endpoint (id, company_id, kind, token, region, status)
VALUES (1, 1, 'greenhouse', 'tok-1', 'global', 'active');
INSERT INTO source_endpoint (id, company_id, kind, token, region, status)
VALUES (2, 2, 'lever', 'tok-2', 'global', 'active');

INSERT INTO pipeline_run (id, run_kind, status, config_version)
VALUES (1, 'daily', 'running', 'cfg-1');

INSERT INTO source_fetch (id, run_id, endpoint_id, status)
VALUES (1, 1, 1, 'complete');

INSERT INTO posting (id, company_id, source_endpoint_id, external_id, title, title_norm, posting_url, apply_url)
VALUES (1, 1, 1, 'ext-1', 't', 't', 'https://u/1', 'https://a/1');
INSERT INTO posting (id, company_id, source_endpoint_id, external_id, title, title_norm, posting_url, apply_url)
VALUES (2, 1, 1, 'ext-2', 't2', 't2', 'https://u/2', 'https://a/2');

INSERT INTO posting_version (id, posting_id, observed_in_run_id, content_hash, title, raw_payload)
VALUES (1, 1, 1, 'ph-1', 't', '{}'::jsonb);

INSERT INTO resume_version (id, variant, source_kind, content_hash, content_md, active)
VALUES (1, 'base', 'manual', 'rh-1', 'm', TRUE);

INSERT INTO candidate_profile_version (id, content_hash, location, active)
VALUES (1, 'cph-1', '{"city":"X","country":"US"}'::jsonb, TRUE);

INSERT INTO company_research (
    id, company_id, run_id, content_hash,
    model_id, prompt_version, web_search_tool_version, payload
) VALUES (1, 1, 1, 'crh-1', 'm', 'p', 'w', '{}'::jsonb);

INSERT INTO daily_digest (id, digest_on, run_id)
VALUES (1, '2026-07-25', 1);
INSERT INTO daily_digest (id, digest_on, run_id)
VALUES (2, '2026-07-26', 1);

-- A valid deep score as the digest_item.score_id target. Satisfies both named
-- deep-score constraints and the unique (posting_version_id, stage, input_hash).
INSERT INTO score (
    id, run_id, posting_version_id, resume_version_id,
    candidate_profile_version_id, company_research_id, stage, input_hash,
    location_eligibility
) VALUES (1, 1, 1, 1, 1, 1, 'deep', 'deep-base', '{"status":"eligible"}'::jsonb);

-- ------------------------------------------------------------------
-- Section 3 enumerated domains: an invalid value for every domain.
-- ------------------------------------------------------------------
SELECT _jobs_expect_violation(
    $_$ INSERT INTO source_endpoint (company_id, kind, token) VALUES (1, 'bogus', 'tok-x') $_$,
    '23514', 'source_endpoint.kind invalid');
SELECT _jobs_expect_violation(
    $_$ INSERT INTO source_endpoint (company_id, kind, token, status) VALUES (1, 'greenhouse', 'tok-st', 'bogus') $_$,
    '23514', 'source_endpoint.status invalid');
SELECT _jobs_expect_violation(
    $_$ INSERT INTO pipeline_run (run_kind, status, config_version) VALUES ('bogus', 'running', 'cfg-x') $_$,
    '23514', 'pipeline_run.run_kind invalid');
SELECT _jobs_expect_violation(
    $_$ INSERT INTO pipeline_run (run_kind, status, config_version) VALUES ('daily', 'bogus', 'cfg-y') $_$,
    '23514', 'pipeline_run.status invalid');
SELECT _jobs_expect_violation(
    $_$ INSERT INTO source_fetch (run_id, endpoint_id, status) VALUES (1, 2, 'bogus') $_$,
    '23514', 'source_fetch.status invalid');
SELECT _jobs_expect_violation(
    $_$ INSERT INTO resume_version (variant, source_kind, content_hash, content_md, active) VALUES ('bogus', 'manual', 'rh-v', 'm', FALSE) $_$,
    '23514', 'resume_version.variant invalid');
SELECT _jobs_expect_violation(
    $_$ INSERT INTO resume_version (variant, source_kind, content_hash, content_md, active) VALUES ('base', 'bogus', 'rh-sk', 'm', FALSE) $_$,
    '23514', 'resume_version.source_kind invalid');
SELECT _jobs_expect_violation(
    $_$ INSERT INTO score (run_id, posting_version_id, stage, input_hash) VALUES (1, 1, 'bogus', 'sh-st') $_$,
    '23514', 'score.stage invalid');
SELECT _jobs_expect_violation(
    $_$ INSERT INTO score (run_id, posting_version_id, stage, input_hash, verdict) VALUES (1, 1, 'filter', 'sh-v', 'definitely') $_$,
    '23514', 'score.verdict invalid');
SELECT _jobs_expect_violation(
    $_$ INSERT INTO digest_item (digest_id, posting_id, score_id, rank, state) VALUES (2, 1, 1, 2, 'bogus') $_$,
    '23514', 'digest_item.state invalid');

-- ------------------------------------------------------------------
-- Section 3 resume provenance checks.
-- ------------------------------------------------------------------
SELECT _jobs_expect_violation(
    $_$ INSERT INTO resume_version (variant, source_kind, content_hash, content_md, source_url, source_sha256, active) VALUES ('product', 'portfolio_pdf', 'rh-pp1', 'm', 'https://x', 'sha', FALSE) $_$,
    '23514', 'portfolio_pdf must be base variant');
SELECT _jobs_expect_violation(
    $_$ INSERT INTO resume_version (variant, source_kind, content_hash, content_md, active) VALUES ('base', 'portfolio_pdf', 'rh-pp2', 'm', FALSE) $_$,
    '23514', 'portfolio_pdf requires source_url and source_sha256');
SELECT _jobs_expect_violation(
    $_$ INSERT INTO resume_version (variant, source_kind, content_hash, content_md, active) VALUES ('technical', 'n8n', 'rh-n8n', 'm', FALSE) $_$,
    '23514', 'n8n requires parent_resume_version_id');

-- ------------------------------------------------------------------
-- digest_item ranks outside 1..3.
-- ------------------------------------------------------------------
SELECT _jobs_expect_violation(
    $_$ INSERT INTO digest_item (digest_id, posting_id, score_id, rank, state) VALUES (2, 1, 1, 0, 'recommended') $_$,
    '23514', 'digest_item.rank below 1');
SELECT _jobs_expect_violation(
    $_$ INSERT INTO digest_item (digest_id, posting_id, score_id, rank, state) VALUES (2, 1, 1, 4, 'recommended') $_$,
    '23514', 'digest_item.rank above 3');

-- ------------------------------------------------------------------
-- deep_score_version_inputs: omit each required foreign key independently.
-- ------------------------------------------------------------------
SELECT _jobs_expect_violation(
    $_$ INSERT INTO score (run_id, posting_version_id, candidate_profile_version_id, company_research_id, stage, input_hash, location_eligibility) VALUES (1, 1, 1, 1, 'deep', 'deep-noresume', '{"status":"eligible"}'::jsonb) $_$,
    '23514', 'deep score missing resume_version_id');
SELECT _jobs_expect_violation(
    $_$ INSERT INTO score (run_id, posting_version_id, resume_version_id, company_research_id, stage, input_hash, location_eligibility) VALUES (1, 1, 1, 1, 'deep', 'deep-nocv', '{"status":"eligible"}'::jsonb) $_$,
    '23514', 'deep score missing candidate_profile_version_id');
SELECT _jobs_expect_violation(
    $_$ INSERT INTO score (run_id, posting_version_id, resume_version_id, candidate_profile_version_id, stage, input_hash, location_eligibility) VALUES (1, 1, 1, 1, 'deep', 'deep-noresearch', '{"status":"eligible"}'::jsonb) $_$,
    '23514', 'deep score missing company_research_id');

-- ------------------------------------------------------------------
-- deep_score_location_status: SQL-null, missing status key, JSON null status,
-- and a status outside the allowed values. Each must be rejected by the
-- fail-closed coalesce expression, not pass through a NULL check result.
-- ------------------------------------------------------------------
SELECT _jobs_expect_violation(
    $_$ INSERT INTO score (run_id, posting_version_id, resume_version_id, candidate_profile_version_id, company_research_id, stage, input_hash) VALUES (1, 1, 1, 1, 1, 'deep', 'deep-nullloc') $_$,
    '23514', 'deep score SQL-null location_eligibility');
SELECT _jobs_expect_violation(
    $_$ INSERT INTO score (run_id, posting_version_id, resume_version_id, candidate_profile_version_id, company_research_id, stage, input_hash, location_eligibility) VALUES (1, 1, 1, 1, 1, 'deep', 'deep-nostatuskey', '{"evidence":"x"}'::jsonb) $_$,
    '23514', 'deep score missing JSON status key');
SELECT _jobs_expect_violation(
    $_$ INSERT INTO score (run_id, posting_version_id, resume_version_id, candidate_profile_version_id, company_research_id, stage, input_hash, location_eligibility) VALUES (1, 1, 1, 1, 1, 'deep', 'deep-jsonnullstatus', '{"status":null}'::jsonb) $_$,
    '23514', 'deep score JSON null status');
SELECT _jobs_expect_violation(
    $_$ INSERT INTO score (run_id, posting_version_id, resume_version_id, candidate_profile_version_id, company_research_id, stage, input_hash, location_eligibility) VALUES (1, 1, 1, 1, 1, 'deep', 'deep-badstatus', '{"status":"maybe"}'::jsonb) $_$,
    '23514', 'deep score status outside allowed values');

-- ------------------------------------------------------------------
-- Partial unique indexes: a second active resume_version for the same variant,
-- and a second active candidate_profile_version overall.
-- ------------------------------------------------------------------
SELECT _jobs_expect_violation(
    $_$ INSERT INTO resume_version (variant, source_kind, content_hash, content_md, active) VALUES ('base', 'manual', 'rh-2nd', 'm', TRUE) $_$,
    '23505', 'second active resume_version for variant');
SELECT _jobs_expect_violation(
    $_$ INSERT INTO candidate_profile_version (content_hash, location, active) VALUES ('cph-2nd', '{"city":"Y","country":"US"}'::jsonb, TRUE) $_$,
    '23505', 'second active candidate_profile_version');

DROP FUNCTION _jobs_expect_violation(text, text, text);

\echo 'All Phase 1 core-schema enforcement cases observed expected rejections.'

ROLLBACK;