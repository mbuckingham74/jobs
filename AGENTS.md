# Repository instructions

These instructions apply to the entire repository.

## Project

`jobs` is the private, single-user service for `jobs.forkstech.com`. It collects
structured job postings and narrows them to a frozen daily queue of at most
three recommendations.

Read `docs/jobs-forkstech-spec.html` before making architectural or product
decisions. It is the product and infrastructure source of truth. Implement the
current file in `tasks/` as the narrower unit of work; if a task and the
specification appear to conflict, stop and surface the conflict instead of
silently expanding scope.

This repository must remain private. Never commit secrets, credentials,
production environment files, private résumé copies, model request payloads, or
database dumps.

## Architecture invariants

- Keep one FastAPI service, one PostgreSQL 16 + pgvector database, and Alembic
  migrations. Do not add a queue broker, separate frontend service, or
  application-owned authentication without an explicit task.
- The root `docker-compose.yml` is the deployment definition. The application
  build context is `./api`, and the import path used by Compose is
  `app.main:app`.
- Keep Postgres private: do not publish its port.
- Do not publish the FastAPI port in production Compose. Nginx Proxy Manager is
  the only ingress and reaches `jobs-api` over the dedicated external
  `jobs_ingress` network.
- `jobs-api` must not join the shared `npm_network`; `jobs-postgres` must not
  join an ingress network.
- Preserve the single Uvicorn worker unless scheduling is moved to a dedicated
  worker in a later, explicit task.
- Authelia and Nginx Proxy Manager are the sole authentication boundary. Do not
  add users, passwords, login pages, or application sessions. State-changing
  routes will still require same-origin and CSRF protection when those routes
  are introduced.
- Put schema changes in reviewed Alembic revisions. Do not create or mutate
  schema from application startup code.
- Keep API routers thin. Source, pipeline, scoring, and workflow logic belong in
  their respective modules as described by the technical specification.
- Treat external source fetches and workflow steps as retryable and
  idempotent. An incomplete fetch must never close postings it did not observe.
- Persist exact, versioned inputs for consequential decisions. Scores and
  frozen digest membership are append-only; do not overwrite history.

## Development conventions

- Use Python 3.13 for the initial service unless a later task deliberately
  changes the project runtime.
- Keep runtime and development dependency declarations in
  `api/pyproject.toml`.
- Use Ruff for linting and formatting and pytest for tests. Add or update tests
  with each behavior change.
- Keep the health endpoint lightweight and independent of external services so
  it can serve as the container liveness check.
- Read configuration from environment variables. Safe examples may be tracked;
  real `.env` files may not.
- Use structured logs and never log credentials, candidate location, résumé
  text, prompts, full provider requests, or Authelia identity data.
- Prefer small modules with explicit ownership. Deterministic filters must
  remain pure and must not perform model or network calls.
- Preserve unrelated user changes. Do not reformat or rewrite files outside the
  active task.

## Task workflow

1. Read this file, the active task, and the relevant portions of the technical
   specification before editing.
2. Confirm the worktree state and keep changes within the active task's scope.
3. Implement the smallest complete vertical change that meets every acceptance
   criterion.
4. Run the task's validation commands. If an environment-dependent command
   cannot run, report exactly what was not verified and why.
5. Review the final diff for secrets, accidental generated files, and scope
   creep.

Do not deploy to the VPS, mutate shared infrastructure, create GitHub resources,
or push changes unless the user explicitly requests it.
