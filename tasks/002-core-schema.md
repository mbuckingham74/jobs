# Task 002: Phase 1 core schema

**Status:** Ready

**Depends on:** Task 001 — Runnable FastAPI service foundation

**Specification:** `docs/jobs-forkstech-spec.html`, especially sections 04 and
13

## Outcome

Create the first Alembic revision for the PostgreSQL 16 + pgvector data model
needed by the Phase 1 queue path. After this task, a clean database can be
upgraded from Alembic base to head, exposes the specified Phase 1 tables,
constraints, and indexes, can be downgraded back to an empty application
schema, and can be upgraded again without manual repair.

This task defines database structure only. It does not add SQLAlchemy
application models, repositories, adapters, ingestion behavior, résumé
fetching, scoring behavior, or API routes.

## Required work

### 1. First Alembic revision

Add exactly one revision under `api/migrations/versions/` for the Phase 1 core
schema. It must:

- be the first domain revision, with `down_revision = None`
- use Alembic operations and SQLAlchemy/PostgreSQL types rather than mutating
  schema from application startup
- create the `vector` extension before any vector column or index
- provide complete `upgrade()` and `downgrade()` functions
- keep all defaults that establish application behavior as server-side
  PostgreSQL defaults
- use explicit, stable names for check, unique, foreign-key, and non-primary-key
  index objects so later migrations and schema tests can refer to them

The revision may add a constrained `pgvector` Python runtime dependency in
`api/pyproject.toml` if it uses `pgvector.sqlalchemy.Vector` to declare the
`vector(1024)` column. Do not add an ORM or declarative model layer in this
task.

### 2. Phase 1 tables

Create these tables in dependency order with the columns, nullability, types,
server defaults, and relationships shown in specification section 04:

1. `company`
2. `source_endpoint`
3. `pipeline_run`
4. `source_fetch`
5. `posting`
6. `posting_version`
7. `resume_version`
8. `candidate_profile_version`
9. `company_research`
10. `score`
11. `daily_digest`
12. `digest_item`

Use PostgreSQL-native `JSONB`, timezone-aware timestamps, dates, numeric
precision, booleans, and bigint primary keys as specified. The
`posting_version.embedding` column must be `vector(1024)`, matching
`voyage-4` with `output_dimension=1024`.

Keep the `company_research` table in this structural migration because
`score.company_research_id` is a required foreign-key input for deep scores.
Research fetching, web search, enrichment, refresh, caching, budget, and
population behavior remain out of scope.

`discovery_event` is a Phase 4 table and must not be introduced by this
revision.

`application` and `application_event` are not Phase 1 tables. The specification
places manual application entry and outcome tracking in Phase 3, so those
tables must not be introduced by this revision.

### 3. Enumerated checks and decision-input constraints

Implement every value-domain check from section 04:

- `source_endpoint.kind`:
  `greenhouse`, `lever`, `ashby`, `workday`, `workable`,
  `smartrecruiters`, `recruitee`, or `custom`
- `source_endpoint.status`:
  `active`, `paused`, `failing`, or `retired`
- `pipeline_run.run_kind`:
  `daily`, `manual`, `discovery`, or `backfill`
- `pipeline_run.status`:
  `running`, `succeeded`, `partial`, or `failed`
- `source_fetch.status`:
  `complete`, `incomplete`, or `failed`
- `resume_version.variant`:
  `base`, `product`, `program`, or `technical`
- `resume_version.source_kind`:
  `portfolio_pdf`, `manual`, or `n8n`
- `score.stage`:
  `filter`, `triage`, or `deep`
- `score.verdict` when non-null:
  `apply`, `maybe`, or `skip`
- `digest_item.rank`: between 1 and 3 inclusive
- `digest_item.state`:
  `recommended`, `skipped`, `applied`, `expired`, or `restored`

Implement both résumé provenance checks:

- a `portfolio_pdf` row must be a `base` variant and have non-null
  `source_url` and `source_sha256`
- an `n8n` row must have a non-null `parent_resume_version_id`

Preserve the two named deep-score constraints from the specification:

- `deep_score_version_inputs`: a `deep` score requires non-null
  `resume_version_id`, `candidate_profile_version_id`, and
  `company_research_id`
- `deep_score_location_status`: a `deep` score requires
  `location_eligibility`, whose JSON `status` is `eligible`, `ineligible`, or
  `unclear`

The `deep_score_location_status` expression must fail closed when the JSON
`status` key is missing or JSON null. PostgreSQL accepts a check constraint
whose expression evaluates to SQL `NULL`, so the allowed-value test must be
wrapped with `coalesce(..., false)` or use an equivalent expression that
returns `false`, never `NULL`, for a missing status.

Filter and triage score rows must remain able to omit those version inputs.
Do not add stricter score-range, JSON-shape, lifecycle, or cross-table
constraints that are not present in the specification.

### 4. Uniqueness and foreign-key behavior

Implement the specified uniqueness rules:

- `company.canonical_domain`
- `(source_endpoint.kind, source_endpoint.token, source_endpoint.region)`
- `(source_fetch.run_id, source_fetch.endpoint_id)`
- `(posting.source_endpoint_id, posting.external_id)`
- `(posting_version.posting_id, posting_version.content_hash)`
- `resume_version.content_hash`
- `candidate_profile_version.content_hash`
- `(company_research.company_id, company_research.content_hash)`
- `(score.posting_version_id, score.stage, score.input_hash)`
- `daily_digest.digest_on`
- `(digest_item.digest_id, digest_item.rank)`
- `(digest_item.digest_id, digest_item.posting_id)`

Add both specified partial unique indexes:

- one active `resume_version` per `variant`
- one active `candidate_profile_version` in total

Use the exact deletion semantics from section 04:

- cascade only where the specification says `ON DELETE CASCADE`
- use `SET NULL` for duplicate-posting links
- use `RESTRICT` for durable historical inputs and queue references

Do not replace these relationships with implicit ORM cascades or rely on
PostgreSQL's default action where the specification explicitly names an
action.

### 5. Query indexes

Create the indexes specified in section 04:

- `posting_last_seen_idx` on `posting (last_seen_at DESC)`
- partial `posting_open_idx` on `posting (closed_at)` where
  `closed_at IS NULL`
- `posting_version_lookup_idx` on
  `posting_version (posting_id, observed_at DESC)`
- HNSW `posting_version_embed_idx` on
  `posting_version.embedding` with `vector_cosine_ops`
- `score_lookup_idx` on
  `score (posting_version_id, stage, scored_at DESC)`

Do not add speculative indexes for later query patterns.

### 6. Reversible downgrade

`downgrade()` must remove the 12 Phase 1 tables in reverse dependency order,
then remove the `vector` extension after the vector column and HNSW index no
longer exist. It must leave no application table, application-defined index,
application-defined constraint, or `vector` extension behind. Alembic's own
version table may remain.

Validate the full cycle against a disposable PostgreSQL 16 + pgvector database:

```text
base -> head -> base -> head
```

Both upgrades and the downgrade must complete without manual SQL, and the
schema after the second upgrade must match the schema after the first.

### 7. Migration-focused tests

Add focused pytest coverage for any migration helpers or custom type handling
introduced by this task. At minimum, tests must make regressions in the
revision identifiers and the required upgrade/downgrade definitions visible.

Add a deterministic PostgreSQL enforcement test at
`api/tests/core_schema_enforcement.sql`. It must run against the upgraded
disposable database used below, build otherwise-valid prerequisite rows, and
attempt each prohibited write independently. It must fail unless PostgreSQL
rejects the write with the expected constraint and SQLSTATE: `23514` for check
violations and `23505` for partial-unique-index violations. Cover:

- an invalid value for every enumerated domain listed in section 3
- digest ranks below 1 and above 3
- separate deep scores omitting each of `resume_version_id`,
  `candidate_profile_version_id`, and `company_research_id`
- deep scores with SQL-null `location_eligibility`, a missing JSON `status`
  key, a JSON-null `status`, and a status outside the allowed values
- a second active `resume_version` for the same variant
- a second active `candidate_profile_version`

Use transaction-local fixtures and finish with `ROLLBACK` so the catalog and
repeat-upgrade comparison are not affected. A test must not pass because some
unrelated foreign key, not-null constraint, or earlier statement rejected the
row. Catalog inspection remains required, but it is not evidence that these
constraints enforce writes.

Do not use SQLite as evidence that PostgreSQL JSONB, partial indexes, HNSW,
foreign-key actions, or pgvector behavior is correct.

## Out of scope

- SQLAlchemy ORM/declarative application models or repositories
- Seed companies, source endpoints, fixtures, or production data
- ATS adapters or source discovery logic
- Ingestion, deduplication, reconciliation, close/reopen behavior, or workflow
  orchestration
- Résumé download, PDF extraction, normalization, activation, or candidate
  profile upsert behavior
- Deterministic filters, research calls, embeddings, model calls, scoring,
  ranking, or digest freezing behavior
- `application` and `application_event`
- `discovery_event` and discovery-event persistence
- Company-research fetching, web search, enrichment, refresh, caching, budget,
  or population behavior
- API or CLI routes, request/response schemas, HTML, or dashboard work
- Schedulers, notifications, authentication, CSRF, deployment, backup, or VPS
  changes

Do not add placeholders for these features. This task ends at a verified,
reversible database schema.

## Acceptance criteria

- [ ] Exactly one domain revision exists, is Alembic's head, and has
      `down_revision = None`.
- [ ] Upgrading a clean PostgreSQL 16 + pgvector database creates the `vector`
      extension and exactly the 12 Phase 1 tables listed above, in addition to
      Alembic's own version table.
- [ ] `posting_version.embedding` is `vector(1024)` and
      `posting_version_embed_idx` is an HNSW cosine-operator index.
- [ ] All column types, nullability rules, and server defaults match section 04
      of the specification.
- [ ] All enumerated checks, résumé provenance checks, and both named
      deep-score checks are present and enforced by PostgreSQL.
- [ ] All specified unique constraints, partial unique indexes, foreign keys,
      and `ON DELETE` actions are present and enforced by PostgreSQL.
- [ ] The five specified query indexes exist with the correct column order,
      sort direction, predicate, access method, and operator class.
- [ ] A filter or triage score may omit résumé, candidate-profile, and research
      references; a deep score may not omit them or provide a missing/invalid
      location-eligibility status.
- [ ] The deterministic PostgreSQL enforcement test confirms that every
      prohibited write listed in section 7 is rejected by the intended
      constraint and SQLSTATE.
- [ ] Digest ranks outside 1 through 3, values outside every enumerated domain,
      and deep scores whose location JSON omits `status` are rejected by
      PostgreSQL rather than passing through a SQL-null check result.
- [ ] Both partial active-version unique indexes reject conflicting active
      rows.
- [ ] `alembic downgrade base` removes all 12 Phase 1 tables and the `vector`
      extension without dependency errors.
- [ ] A second `alembic upgrade head` succeeds after downgrade and recreates
      the same schema.
- [ ] `discovery_event`, `application`, `application_event`, and all other
      out-of-scope behavior are absent.
- [ ] Ruff lint and format checks pass for all Python files under `api/`.
- [ ] The full pytest suite passes.
- [ ] No secret, `.env` file, generated cache, local virtual environment,
      database dump, or production data is added to Git.

## Validation commands

Run from the repository root unless a command changes directory.

### Local Python checks

Reuse the Task 001 virtual environment when it exists; otherwise create it and
install the project with development dependencies.

```bash
cd api
test -x .venv/bin/python || python3.13 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest
.venv/bin/alembic heads
.venv/bin/alembic history
cd ..
```

`alembic heads` must report exactly the new core-schema revision.

### Disposable migration cycle

Use a dedicated Compose project name so validation does not touch an existing
development or production volume. The external network is a Compose
prerequisite but is not joined by the database or migration service.

```bash
export SCHEMA_PROJECT=jobs-schema-check
export JOBS_DB_PASSWORD=jobs-schema-dev

docker network inspect jobs_ingress >/dev/null 2>&1 || \
  docker network create --driver bridge --internal jobs_ingress

docker compose -p "$SCHEMA_PROJECT" build migrate
docker compose -p "$SCHEMA_PROJECT" up -d --wait db
docker compose -p "$SCHEMA_PROJECT" run --rm migrate alembic upgrade head
docker compose -p "$SCHEMA_PROJECT" run --rm migrate alembic current
```

The first `alembic current` must report the core-schema revision as `(head)`.

### Schema validation queries

List the installed extension and application tables:

```bash
docker compose -p "$SCHEMA_PROJECT" exec -T db \
  psql -X -v ON_ERROR_STOP=1 -U jobs -d jobs <<'SQL'
SELECT extname, extversion
FROM pg_extension
WHERE extname = 'vector';

SELECT tablename
FROM pg_tables
WHERE schemaname = 'public'
ORDER BY tablename;
SQL
```

The extension query must return one `vector` row. The table query must return
`alembic_version` plus exactly these 12 application tables:

```text
candidate_profile_version
company
company_research
daily_digest
digest_item
pipeline_run
posting
posting_version
resume_version
score
source_endpoint
source_fetch
```

Inspect column types, nullability, and defaults:

```bash
docker compose -p "$SCHEMA_PROJECT" exec -T db \
  psql -X -v ON_ERROR_STOP=1 -U jobs -d jobs <<'SQL'
SELECT
  table_name,
  ordinal_position,
  column_name,
  data_type,
  udt_name,
  is_nullable,
  column_default
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name <> 'alembic_version'
ORDER BY table_name, ordinal_position;

SELECT format_type(a.atttypid, a.atttypmod) AS embedding_type
FROM pg_attribute AS a
WHERE a.attrelid = 'public.posting_version'::regclass
  AND a.attname = 'embedding'
  AND NOT a.attisdropped;
SQL
```

The second query must return exactly `vector(1024)`.

Inspect all application-defined constraints, including foreign-key delete
actions:

```bash
docker compose -p "$SCHEMA_PROJECT" exec -T db \
  psql -X -v ON_ERROR_STOP=1 -U jobs -d jobs <<'SQL'
SELECT
  con.conrelid::regclass::text AS table_name,
  con.conname,
  con.contype,
  pg_get_constraintdef(con.oid, true) AS definition
FROM pg_constraint AS con
WHERE con.connamespace = 'public'::regnamespace
  AND con.conrelid <> 'public.alembic_version'::regclass
ORDER BY table_name, con.contype, con.conname;
SQL
```

Inspect index definitions, including partial predicates, descending columns,
the HNSW access method, and vector operator class:

```bash
docker compose -p "$SCHEMA_PROJECT" exec -T db \
  psql -X -v ON_ERROR_STOP=1 -U jobs -d jobs <<'SQL'
SELECT tablename, indexname, indexdef
FROM pg_indexes
WHERE schemaname = 'public'
  AND tablename <> 'alembic_version'
ORDER BY tablename, indexname;
SQL
```

The output must include the two partial unique indexes and all five query
indexes named in this task. Review the catalog output against section 04; do
not accept migration success alone as schema validation.

### PostgreSQL enforcement tests

Run the deterministic write-enforcement test against the upgraded disposable
database:

```bash
docker compose -p "$SCHEMA_PROJECT" exec -T db \
  psql -X -v ON_ERROR_STOP=1 -U jobs -d jobs \
  < api/tests/core_schema_enforcement.sql
```

The command must exit zero only after observing every expected rejection
listed in section 7. Any prohibited write that succeeds, any rejection with a
different constraint or SQLSTATE, or any fixture/setup error must make the
command exit nonzero. The script must finish with `ROLLBACK`; rerun the catalog
queries afterward and confirm that its fixtures left no rows or schema objects
behind.

### Downgrade and repeat-upgrade checks

```bash
docker compose -p "$SCHEMA_PROJECT" run --rm migrate alembic downgrade base

docker compose -p "$SCHEMA_PROJECT" exec -T db \
  psql -X -v ON_ERROR_STOP=1 -U jobs -d jobs <<'SQL'
SELECT tablename
FROM pg_tables
WHERE schemaname = 'public'
  AND tablename <> 'alembic_version'
ORDER BY tablename;

SELECT extname
FROM pg_extension
WHERE extname = 'vector';
SQL

docker compose -p "$SCHEMA_PROJECT" run --rm migrate alembic upgrade head
docker compose -p "$SCHEMA_PROJECT" run --rm migrate alembic current
```

After downgrade, both validation queries must return zero rows. After the
repeat upgrade, rerun the schema validation queries above and confirm the
catalog definitions match the first upgrade.

Clean up only the isolated validation project and its disposable volume:

```bash
docker compose -p "$SCHEMA_PROJECT" down --volumes
unset SCHEMA_PROJECT JOBS_DB_PASSWORD
```

### Final scope and repository hygiene

```bash
git status --short
git diff --check
git diff --name-only
git diff -- api/migrations api/pyproject.toml api/tests
```

The implementation diff must remain limited to the migration revision, any
strictly necessary pgvector dependency declaration, and focused migration
tests. It must not change application modules, Compose topology, or any
out-of-scope feature.
