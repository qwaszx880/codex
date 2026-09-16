# Test guidance

- Add focused regression coverage for behavior changes, especially transaction payloads,
  revision semantics, idempotency, stale commands, leases, authorization, and tenancy.
- Unit tests must not require PostgreSQL, RabbitMQ, Kubernetes, OpenStack, or internet
  access. Put real-service scenarios in explicitly marked integration tests.
- Keep cross-file local configuration contracts in `test_local_stack.py`; keep pure spec
  and compiler behavior in their dedicated test modules.
