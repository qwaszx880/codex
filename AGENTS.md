# Repository context for agents

## Purpose

This repository is a tenant-aware CAPI/CAPO control-plane reference implementation.
Start with `README.md`, then use `docs/code-walkthrough/README.md` for file ownership and
`docs/project-goals/README.md` for the target architecture and explicit implementation
gaps.

## Non-negotiable boundaries

- The central FastAPI service must never hold a management-cluster kubeconfig or call a
  management Kubernetes API.
- Keep identity/authorization, desired state, operations/reconciliation, and observed
  state/health separate.
- API mutations atomically persist revision, operation, audit, and outbox intent;
  RabbitMQ publication happens only after commit.
- Assume at-least-once delivery. Preserve event idempotency, exact target-revision
  checks, and per-cluster (not global) serialization.
- Keep FastAPI routes and Celery tasks thin. Put orchestration in application services
  and isolate provider behavior behind domain ports.
- Never store or return raw provider credentials; persist secret references only.

## Working conventions

- Preserve the detailed root README and update relevant docs when behavior, structure,
  configuration, or developer workflows change.
- Use Alembic for schema changes; application startup must not call
  `metadata.create_all()`.
- Keep the local fake executor/observer on the same logical command and status contracts
  as production adapters.
- Before committing, run `ruff check .`, `ruff format --check .`, `pytest -q`, and
  `git diff --check`. Run `docker compose config` when Docker is available and Compose
  configuration changes.
