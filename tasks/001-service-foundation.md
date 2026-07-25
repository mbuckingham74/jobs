# Task 001: Runnable FastAPI service foundation

**Status:** Ready

**Depends on:** Repository bootstrap

**Specification:** `docs/jobs-forkstech-spec.html`, especially sections 03, 09,
11, and 12

## Outcome

Establish the smallest runnable and testable FastAPI service that satisfies the
existing root Compose contract. After this task, a developer can lint and test
the Python project locally, build the service image, run the Alembic command
against PostgreSQL, start the Compose stack, and receive a successful response
from `GET /healthz`.

This task creates infrastructure for later vertical slices. It does not create
the jobs data model.

## Required work

### 1. Python service package

Create the `api/` service directory expected by `docker-compose.yml`, with this
minimum structure:

```text
api/
├── .dockerignore
├── Dockerfile
├── alembic.ini
├── pyproject.toml
├── app/
│   ├── __init__.py
│   └── main.py
├── migrations/
│   ├── env.py
│   ├── script.py.mako
│   └── versions/
└── tests/
    └── test_health.py
```

Additional small support modules are allowed only when they make configuration
or database wiring materially clearer. Keep `app.main:app` as the ASGI import
path because the root Compose file already depends on it.

Use Python 3.13. Package metadata must allow the project and its development
dependencies to be installed from `api/` without relying on untracked local
files.

### 2. Health endpoint

Create the FastAPI application and implement:

```http
GET /healthz
```

The endpoint must return HTTP `200` and this JSON body:

```json
{"status": "ok"}
```

This is a liveness endpoint. It must not query PostgreSQL, call a provider, read
the résumé URL, or depend on any other network service. Add a pytest using
FastAPI's test client that asserts both the status code and exact JSON response.

Do not add the product API routes from section 09 in this task.

### 3. Project and tool configuration

Create `api/pyproject.toml` as the single source for package metadata,
dependencies, and tool configuration.

Runtime dependencies must cover:

- FastAPI
- Uvicorn, at version 0.31.0 or newer so the later trusted-proxy CIDR
  configuration remains supported
- SQLAlchemy
- Alembic
- Psycopg 3 with the binary distribution suitable for the service image

Development dependencies must cover pytest, an HTTP/test-client dependency
compatible with FastAPI, and Ruff.

Configure:

- pytest to discover tests under `tests/`
- Ruff linting for at least pycodestyle errors, Pyflakes, import sorting,
  pyupgrade, and bugbear checks
- Ruff formatting with a single documented line-length setting
- the Ruff target version for Python 3.13

Keep dependency constraints reproducible: do not use unconstrained `*` or
floating VCS dependencies. A dedicated lockfile is not required by this task.

### 4. Alembic wiring without application schema

Wire Alembic under `api/` so `alembic upgrade head` reads `DATABASE_URL` at
runtime and can connect using the Compose URL:

```text
postgresql+psycopg://jobs:<password>@db:5432/jobs
```

The Alembic environment must fail with a clear message when `DATABASE_URL` is
absent; no real connection string belongs in tracked files.

Provide the standard revision template and versions directory, but create no
domain migration revision in this task. In particular, do not create the
`vector` extension or any `company`, `source_endpoint`, `pipeline_run`,
`posting`, résumé, score, digest, or application table. Those belong to a later
schema task.

Running `alembic upgrade head` with no revisions must still exit successfully
against the Compose database. Application startup must not call
`create_all()` or otherwise create schema.

### 5. Container image

Create `api/Dockerfile` using a Python 3.13 slim base. It must:

- install the project from `pyproject.toml`
- copy the application and Alembic files needed by both the `api` and
  `migrate` Compose services
- run as a non-root user
- use `/app` (or another explicit, consistent path) as its working directory
- avoid copying local virtual environments, caches, test results, secrets, or
  `.env` files into the image
- remain compatible with the commands already declared in the root
  `docker-compose.yml`

Do not bake configuration or secrets into the image.

### 6. Docker Compose validation

Review the existing root `docker-compose.yml` and make only the corrections
strictly required for this foundation to run. Preserve these deployment
properties:

- `api` and `migrate` build from `./api`
- `migrate` runs `alembic upgrade head`
- the API waits for healthy PostgreSQL and successful migration
- the existing container health check calls `/healthz`
- PostgreSQL uses `pgvector/pgvector:pg16`
- neither the API nor PostgreSQL publishes a host port
- PostgreSQL remains only on the project-default network
- only the API joins the external `jobs_ingress` network
- Uvicorn uses one worker and the `app.main:app` import path

The local validation stack may use a manually created internal
`jobs_ingress` network. Do not weaken the production network model or add a
development port mapping to the tracked Compose file merely to make local
testing easier.

## Out of scope

- Any application database model or domain table
- The `vector` extension or an initial schema migration
- ATS adapters, discovery, ingestion, reconciliation, filters, model calls,
  research, scoring, digests, scheduling, or notifications
- The product routes listed in specification section 09
- HTML, Jinja, HTMX, static assets, `robots.txt`, or dashboard work
- Application-owned authentication, CSRF middleware, or proxy-identity logic
- Production deployment, NPM, Authelia, DNS, firewall, backup, or VPS changes

Do not add placeholders for these features beyond what is necessary for the
foundation to import and run.

## Acceptance criteria

- [ ] `api/` is an installable Python 3.13 project and `app.main:app` imports
      successfully.
- [ ] `GET /healthz` returns HTTP `200` with exactly `{"status": "ok"}` and
      remains independent of database availability.
- [ ] The health endpoint has a passing pytest.
- [ ] Ruff lint and format checks pass for all Python files under `api/`.
- [ ] Pytest configuration and Ruff configuration live in
      `api/pyproject.toml`.
- [ ] Alembic reads only the runtime `DATABASE_URL`, uses Psycopg 3, and can run
      `upgrade head` against the Compose PostgreSQL service.
- [ ] No domain revision, domain table, PostgreSQL extension, SQLAlchemy
      `create_all()`, or startup-time schema mutation is introduced.
- [ ] The API image builds from `./api`, contains the migration environment,
      and runs as a non-root user.
- [ ] `docker compose config --quiet` succeeds.
- [ ] The full Compose startup completes with PostgreSQL healthy, `migrate`
      exited successfully, and `api` healthy.
- [ ] The internal container health request receives the exact expected JSON
      response.
- [ ] The root Compose file still publishes no API or database port and
      preserves the dedicated-ingress topology.
- [ ] No secret, `.env` file, generated cache, or local virtual environment is
      added to Git.

## Validation commands

Run from the repository root unless a command changes directory.

### Local Python checks

```bash
cd api
python3.13 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest
.venv/bin/python -c "from app.main import app; assert app is not None"
cd ..
```

### Alembic configuration check

This command must fail clearly because no database URL was supplied:

```bash
cd api
env -u DATABASE_URL .venv/bin/alembic upgrade head
cd ..
```

### Compose checks

Use a disposable development password supplied only to the process. The
external network creation is a one-time local prerequisite and does not alter
the tracked Compose topology.

```bash
docker network inspect jobs_ingress >/dev/null 2>&1 || \
  docker network create --driver bridge --internal jobs_ingress
JOBS_DB_PASSWORD=jobs-dev docker compose config --quiet
JOBS_DB_PASSWORD=jobs-dev docker compose build api migrate
JOBS_DB_PASSWORD=jobs-dev docker compose up --build --wait
docker compose ps
docker compose exec -T api python -c "import json, urllib.request; r = urllib.request.urlopen('http://127.0.0.1:8000/healthz'); assert r.status == 200; assert json.load(r) == {'status': 'ok'}"
docker compose exec -T db psql -U jobs -d jobs -c "\dt public.*"
docker compose down
```

For the `psql` command, the only acceptable table is Alembic's own version
table, if Alembic creates it for an empty revision graph. No application-domain
table may exist.

Finally, verify scope and repository hygiene:

```bash
git status --short
git diff --check
git diff -- AGENTS.md tasks/001-service-foundation.md api docker-compose.yml
```
