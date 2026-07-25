# jobs

A private, single-user job pipeline for `jobs.forkstech.com`. Polls applicant
tracking systems, discovers companies on its own, and narrows roughly twelve
hundred daily postings down to no more than three worth a focused application.

> This repository must never be made public. Prompts, fixtures, and
> candidate-specific logic may be sensitive even when secrets are excluded.

## Contents

- `docs/jobs-forkstech-spec.html` — the technical specification (v0.12 draft)
- `docker-compose.yml` — root Compose file for the FastAPI service and Postgres 16 + pgvector

## Status

Pre-Phase 1. Only the repository bootstrap has been performed; no application
code exists yet. See the specification for the five build phases.