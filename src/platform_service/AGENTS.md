# Python application guidance

- Follow the dependency direction: `api` -> `application` -> `domain`; infrastructure
  and workers implement or wire outer adapters.
- `domain/spec.py` stays provider-neutral. Provider-specific translation belongs in a
  compiler/adapter, not routes or the stable API contract.
- Application mutations must append immutable revisions and commit revision, operation,
  audit, and outbox rows together. Flush database-generated IDs before placing them in
  durable payloads.
- Reconciliation must remain transport-independent in `application/reconciliation.py`.
  Celery code handles sessions, retries, and classification mapping only.
- Do not collapse operation state, raw resource observations, and normalized health into
  one model. A later health failure must not rewrite historical operation success.
- Prefer focused docstrings and comments that explain architectural reasons, race
  protection, or transaction boundaries; do not narrate obvious syntax.
