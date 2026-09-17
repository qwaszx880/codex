# Python code walkthrough

This guide is a map of every Python module in the repository, how a request moves
through them, and where to make common changes. Read the root [README](../../README.md)
first for the system architecture and runnable local workflow. Read the
[project goals](../project-goals/README.md) for the intended production end state and
known gaps.

## Start here

The code follows a lightweight ports-and-adapters layout:

```text
HTTP request
  -> api/routes.py                 HTTP parsing and authorization boundary
  -> application/cluster_service.py
  -> infrastructure/database.py   current persistence adapter
  -> outbox row
  -> workers/outbox.py             post-commit delivery
  -> RabbitMQ / Celery
  -> workers/tasks.py              transport adapter
  -> application/reconciliation.py
  -> domain/ports.py               compiler and management adapter contracts
  -> infrastructure/compiler.py   provider resource translation
  -> management plane (fake locally)

fake observation
  -> workers/fake_observer.py
  -> raw ResourceStatus + ConditionHistory
  -> derived ClusterStatus + operation completion
```

The important boundary is that FastAPI never calls Kubernetes. API mutations persist
intent and an outbox event; an executor consumes that event on the management side.
Desired state, operation history, and observed health are deliberately separate.

## Directory tree

```text
src/platform_service/
├── __init__.py
├── main.py
├── config.py
├── api/
│   ├── routes.py
│   └── schemas.py
├── application/
│   ├── cluster_service.py
│   ├── errors.py
│   ├── iam_service.py
│   └── reconciliation.py
├── domain/
│   ├── errors.py
│   ├── ports.py
│   └── spec.py
├── infrastructure/
│   ├── auth.py
│   ├── bootstrap.py
│   ├── compiler.py
│   └── database.py
└── workers/
    ├── celery_app.py
    ├── fake_observer.py
    ├── heartbeat.py
    ├── outbox.py
    └── tasks.py

alembic/
├── env.py
├── script.py.mako
└── versions/0001_initial.py

tests/
├── test_cluster_service.py
├── test_compiler.py
├── test_iam.py
├── test_local_stack.py
└── test_spec.py
```

Directories use namespace packages, so most do not require an `__init__.py`.

## Package root and configuration

### `src/platform_service/__init__.py`

Marks the installable application package. It intentionally performs no startup work;
importing the package must not connect to external systems.

### `src/platform_service/main.py`

The FastAPI composition root. It creates `app`, mounts all versioned routes below
`/v1`, mounts the Prometheus ASGI application at `/metrics`, and exposes `/healthz`.
Uvicorn imports this module through:

```bash
uvicorn platform_service.main:app --host 0.0.0.0 --port 8000
```

Add process-wide middleware here (for example request logging or tracing). Add business
endpoints in `api/routes.py`, not directly in this file.

### `src/platform_service/config.py`

Defines `Settings`, whose fields come from `PLATFORM_` environment variables and an
optional `.env` file. `get_settings()` is cached so one process uses a consistent
snapshot.

```python
from platform_service.config import get_settings

settings = get_settings()
print(settings.management_cluster)
```

Tests that change settings must clear `get_settings.cache_clear()` before constructing a
new settings-dependent object.

## HTTP adapter

### `src/platform_service/api/schemas.py`

Contains Pydantic models that describe HTTP bodies and responses:

- `ClusterCreate` accepts tenant/placement references and a validated `ClusterSpec`;
- `ClusterView` exposes revision pointers without provider secrets;
- `OperationView` exposes asynchronous user intent;
- `ScaleRequest` and `UpgradeRequest` model action-specific input.

These are transport models. Provider-neutral desired state belongs in
`domain/spec.py`; SQLAlchemy mappings belong in `infrastructure/database.py`.

### `src/platform_service/api/routes.py`

Contains the FastAPI router. Routes should do only HTTP-boundary work:

1. parse and validate the request;
2. resolve the OIDC identity and database session through dependencies;
3. check access to the owning project;
4. call an application service for mutations;
5. translate expected application errors into HTTP responses.

`resolve_request_id()` preserves `X-Request-ID` or creates a correlation ID.
`authorized_cluster()` combines cluster lookup with project permission checking.
`translate_domain_errors()` maps known use-case errors without swallowing unexpected
failures.

Example route-to-service call:

```python
_, operation = translate_domain_errors(
    lambda: ClusterService(session).upgrade(
        cluster_id=cluster_id,
        version=body.version,
        actor_id=identity.principal_id,
        permissions=permissions,
        request_id=request_id,
    )
)
```

Read routes currently query SQLAlchemy directly. The project-goals review records moving
those queries behind repository/application boundaries as follow-up work.

IAM routes expose the current principal, visible projects, assignable project roles,
and project membership management. `project.admin` gates role and membership details,
and public responses omit the issuer and external-subject identity mapping keys.

## Application layer

### `src/platform_service/application/cluster_service.py`

Owns desired-state mutation use cases:

- `create()` validates placement records, creates revision 1, and records intent;
- `revise()` locks the cluster row, advances `desired_revision`, and appends a spec;
- `scale()` copies the current spec and changes one worker node type;
- `upgrade()` copies the current spec and changes the Kubernetes version;
- `_intent()` constructs the operation, audit event, and outbox event.

A mutation commits revision, operation, audit, and outbox records in one database
transaction. `_intent()` flushes twice for a specific reason: the database-generated
operation ID is required in the command payload, and the database-generated event ID
is both the delivery identity and executor idempotency key.

Example direct use (normally called by a route):

```python
cluster, operation = ClusterService(session).scale(
    cluster_id=cluster_id,
    pool="workers",
    replicas=5,
    actor_id=principal_id,
    permissions={"cluster.scale"},
    request_id="demo-scale-1",
)
```

Do not publish to RabbitMQ from this service. Queue availability must remain outside the
API transaction.

### `src/platform_service/application/iam_service.py`

Owns project membership mutations and IAM reads. It accepts only enabled projects and
principals, permits only project-scoped roles in project memberships, rejects duplicate
assignments, and lists projects visible through direct or organization membership.
Inspecting or changing project RBAC requires effective `project.admin` permission.
Membership writes commit their audit record in the same transaction.

### `src/platform_service/application/reconciliation.py`

`CommandProcessor` is executor-side application orchestration. `process()`:

1. parses event, operation, cluster, and revision identifiers;
2. rejects an already processed event;
3. locks the cluster and rejects a stale target revision;
4. obtains or renews the per-cluster lease;
5. marks the operation `RECONCILING`;
6. loads the immutable target revision;
7. compiles and applies provider resources through ports;
8. records the applied revision and processed-event marker atomically.

The processor is independent of Celery. A synchronous test or another task transport can
construct it with any `ClusterCompiler` and `ManagementClusterAdapter` implementations:

```python
processor = CommandProcessor(session, compiler, adapter, executor_id="executor-1")
processor.process(command_payload)
```

Convergence is intentionally not awaited here; observation completes the operation.

## Domain layer

### `src/platform_service/domain/spec.py`

Defines the stable user-facing desired-state model:

- `KubernetesSpec` validates the version format;
- `NetworkingSpec` supplies pod/service CIDR defaults;
- `ControlPlaneSpec` constrains replica count and node profile;
- `WorkerNodeTypeSpec` describes homogeneous worker groups, including their role and
  profile, while protecting labels owned by the compiler;
- `ScalingSpec` validates autoscaling bounds;
- `FeatureSpec` carries optional platform features;
- `ClusterSpec` assembles the contract and enforces unique node types and odd control-plane
  replica counts.

Example validation:

```python
spec = ClusterSpec.model_validate(
    {
        "kubernetes": {"version": "v1.31.1"},
        "control_plane": {"replicas": 3, "node_profile": "control"},
        "worker_node_types": [
            {"name": "workers", "role": "worker", "replicas": 3, "node_profile": "compute"}
        ],
    }
)
```

Keep this model provider-neutral. OpenStack-only fields belong in provider references,
node profiles, or a provider validation/compilation adapter.

### `src/platform_service/domain/errors.py`

Defines reconciliation classifications independent of Celery or HTTP:
`TRANSIENT`, `PERMANENT`, `STALE`, `CONFLICT`, and `DUPLICATE`. Specialized exceptions
carry a classification that the task adapter converts into retry, failure, or safe
no-op behavior.

Add a new classification only when every transport adapter can give it a clear policy.
Do not catch arbitrary exceptions and classify them as retryable by default.

### `src/platform_service/domain/ports.py`

Declares replaceable boundaries using `Protocol` or abstract base classes:

- repository ports: `ClusterRepository`, `OperationRepository`, `IAMRepository`, and
  `AuditRepository`;
- messaging ports: `EventPublisher` and `TaskConsumer`;
- provider ports: `ClusterCompiler` and `ManagementClusterAdapter`;
- external service ports: `MetricsBackend` and `SecretStore`.

The protocols describe architectural direction. Some current paths still use SQLAlchemy
or Celery directly; see the alignment review before assuming every port has an adapter.

Example adapter shape:

```python
class KubernetesManagementAdapter:
    def apply(self, resources: list[dict]) -> None:
        ...  # server-side apply using the executor's local ServiceAccount

    def delete(self, namespace: str, name: str) -> None:
        ...
```

## Infrastructure adapters

### `src/platform_service/infrastructure/database.py`

Defines the SQLAlchemy declarative base, engine/session factory, and authoritative data
model. Models are grouped conceptually as follows:

| Concern | Models |
|---|---|
| IAM and tenancy | `Principal`, `Organization`, `Project`, `Role`, `Permission`, memberships and role permissions |
| Placement/provider | `ProviderReference`, `NodeProfile`, `ManagementCluster` |
| Desired state | `Cluster`, `ClusterRevision` |
| Operations/delivery | `Operation`, `OperationStage`, `OutboxEvent`, `ReconciliationLease`, `ProcessedEvent` |
| Observation | `ClusterStatus`, `ResourceStatus`, `ConditionHistory` |
| Executors/audit | `ExecutorInstance`, `AuditEvent` |

Use `SessionLocal()` for an explicitly managed session and `SessionLocal.begin()` when a
context-managed transaction should commit on success and roll back on error:

```python
with SessionLocal.begin() as session:
    cluster = session.get(Cluster, cluster_id)
```

Application startup never calls `metadata.create_all()`. Schema changes require an
Alembic revision.

### `src/platform_service/infrastructure/auth.py`

Implements FastAPI authentication/session dependencies:

- `db_session()` creates and closes the request-scoped session; application services
  explicitly commit mutation transactions, while closing discards uncommitted work;
- `current_identity()` validates bearer-token signature, issuer, audience, and claims
  against OIDC JWKS, then finds or provisions the internal principal;
- `project_permissions()` unions direct project-role grants with organization-role
  grants inherited by projects in that organization, while enforcing role scope;
- `require_project_permission()` is the shared fail-closed route authorization helper.

The external `(issuer, subject)` pair is identity authority; username and email are
profile attributes, not authorization keys. Disabling a principal blocks access even if
the external token remains valid.

### `src/platform_service/infrastructure/bootstrap.py`

Idempotently seeds the local organization, project, principal, roles/permissions,
management cluster, provider reference, and node profiles. Stable UUIDs intentionally
match the imported Keycloak development identity. It stores only a fake secret
reference—never provider credentials.

Run it manually inside the application image with:

```bash
python -m platform_service.infrastructure.bootstrap
```

Compose normally runs it once after migrations, so developers should not need to invoke
it during a clean startup.

### `src/platform_service/infrastructure/compiler.py`

`CapoCompiler` is a pure translation from `ClusterSpec` to dictionaries representing
CAPI/CAPO resources. It performs no network or database I/O. Purity makes the compiler
fast to unit test and keeps Kubernetes credentials outside the central platform.
Each worker node type becomes a `MachineDeployment` referencing a
`KubeadmConfigTemplate` and `OpenStackMachineTemplate`. Its selector exactly matches
the machine-template labels, allowing CAPI to create and roll compatible
`MachineSet`s. The compiler derives cluster ownership, node-type, and node-role labels.

```python
resources = CapoCompiler().compile("demo", "p-a83f", spec)
assert all(item["metadata"]["namespace"] == "p-a83f" for item in resources)
```

A future provider should implement `ClusterCompiler` rather than adding provider
branches to HTTP routes or the domain spec.

## Worker processes

### `src/platform_service/workers/celery_app.py`

Creates the shared Celery application and direct exchange. Commands route to the queue
named for `PLATFORM_MANAGEMENT_CLUSTER`. Late acknowledgement and worker-loss rejection
support at-least-once delivery; publisher confirms improve enqueue reliability; quorum
queue and dead-letter exchange arguments establish the intended production semantics.

This module configures transport. Reconciliation policy belongs in
`application/reconciliation.py`.

### `src/platform_service/workers/outbox.py`

`OutboxPublisher` is the database-to-Celery relay. `_claim()` selects unpublished or
stale-claimed rows with `FOR UPDATE SKIP LOCKED`, marks ownership, and commits quickly.
`publish_batch()` publishes each claimed payload, then records success or the last
error. The loop sleeps only when no rows were processed.

Run one publisher with:

```bash
python -m platform_service.workers.outbox
```

The publish-confirm/database-update boundary can still produce duplicate delivery after
a crash; executor idempotency is therefore mandatory.

### `src/platform_service/workers/tasks.py`

Defines the thin `platform.reconcile` Celery task and the local-only
`FakeManagementAdapter`. The task creates a database session, delegates to
`CommandProcessor`, translates known classifications into no-op/retry behavior, rolls
back on failure, and always closes the session.

`FAKE_FAILURE_MODE=transient` exercises bounded retry. `permanent` makes the fake
adapter reject application without pretending the error is transient.

A production deployment should wire a real local Kubernetes adapter while keeping the
task and command contract unchanged.

### `src/platform_service/workers/fake_observer.py`

Simulates the management-side status observer without Kubernetes/OpenStack. For each
`RECONCILING` operation it compiles the same desired revision, synthesizes the required
resource kinds, stores raw status and condition history, derives normalized health,
and completes the operation with a convergence stage.

```bash
FAKE_CAPI_FAILURE=true python -m platform_service.workers.fake_observer
```

That flag simulates failed conditions. This process is a local development adapter, not
a replacement for a production watch/resync implementation.

### `src/platform_service/workers/heartbeat.py`

Periodically upserts an `ExecutorInstance` for the configured management cluster. It
reports executor identity, executor/CAPI/CAPO versions, status, and last-seen time.
`EXECUTOR_ID` overrides the hostname-derived identity.

```bash
EXECUTOR_ID=executor-debug python -m platform_service.workers.heartbeat
```

The current versions are local placeholders. A production executor must report its
actual compatibility metadata and use an authenticated reporting path appropriate to
the network boundary.

## Database migrations

### `alembic/env.py`

Connects Alembic to `Base.metadata` and supports offline SQL generation and online
migration execution. `compare_type=True` makes column type changes visible to
autogeneration.

### `alembic/versions/0001_initial.py`

Creates and drops the initial authoritative schema in dependency order. It derives the
initial table set from version-pinned metadata. Future schema changes must use explicit
new revision files rather than editing a migration that has already shipped.

### `alembic/script.py.mako`

Template used by `alembic revision` to create subsequent migration modules.

Common commands:

```bash
alembic upgrade head
alembic current
alembic revision --autogenerate -m "describe schema change"
```

Review autogenerated migrations before committing them.

## Tests

### `tests/test_spec.py`

Covers accepted `ClusterSpec` data, legacy-key compatibility, reserved derived labels,
and cross-field rejection for even control-plane replicas and duplicate worker-node-type
names.

### `tests/test_compiler.py`

Verifies that compilation produces the expected CAPI/CAPO kinds, project namespace,
worker replica count, matching selectors, derived labels, and template references
without external I/O.

### `tests/test_cluster_service.py`

Verifies that the upgrade use case copies current desired state, changes only the
Kubernetes version, and delegates immutable revision creation with correct metadata.

### `tests/test_local_stack.py`

Checks cross-file local-stack contracts: Keycloak identity matches the seeded principal,
the audience mapper exists, expected permissions are seeded, VM host/bind substitutions
remain present, and the fake observer emits both success and failure conditions.

Run all tests and static checks:

```bash
pytest -q
ruff check .
ruff format --check .
```

## End-to-end walkthrough

A cluster creation is the best path for learning the repository:

1. `ClusterCreate` validates the HTTP body, including nested `ClusterSpec`.
2. `routes.create()` resolves the principal and `cluster.create` permission.
3. `ClusterService.create()` validates project/management placement and creates the
   cluster and first immutable revision.
4. `_intent()` adds the operation, audit event, and routed outbox payload.
5. The request-scoped transaction commits before a queue call occurs.
6. `OutboxPublisher` claims the row and sends `platform.reconcile` to the management
   cluster queue.
7. `tasks.reconcile()` delegates to `CommandProcessor`.
8. The processor checks idempotency, target revision, and lease, compiles resources,
   calls the management adapter, and records `applied_revision`.
9. The fake observer stores raw resources/conditions and normalized health, sets
   `observed_revision`, adds a convergence stage, and completes the operation.
10. Read routes return operation history and observed state independently.

Use the root README's Compose tutorial to execute this path and inspect PostgreSQL,
RabbitMQ, Celery, and API state in their browser interfaces.

## Recipes for extending the code

### Add a new mutation

1. Extend or add the request schema in `api/schemas.py`.
2. Add provider-neutral validation to `domain/spec.py` if the desired-state contract
   changes.
3. Implement the use case in `application/cluster_service.py`; append a revision and
   call `_intent()` in the same transaction.
4. Add a thin route that authenticates, authorizes, and delegates.
5. Teach the compiler/adapter only if new provider resources are required.
6. Add service, route, compiler, and concurrency tests.

### Add a provider

1. Implement `ClusterCompiler` for the new provider.
2. Implement `ManagementClusterAdapter` in the executor deployment.
3. Add provider capability validation outside the HTTP layer.
4. Resolve only secret references through `SecretStore`.
5. Add contract tests that run the same `CommandProcessor` against the new adapters.

### Add a database field or table

1. Update `infrastructure/database.py`.
2. Generate a new Alembic revision; do not modify `0001_initial.py` after release.
3. Review upgrade and downgrade behavior.
4. Update schemas/services only at the boundary that owns the new data.
5. Add migration and behavior tests plus this walkthrough if ownership changes.

### Add a worker

Keep the process entry point small. Put transport-independent decisions in an
application service, depend on domain ports, manage session rollback/close explicitly,
and provide bounded retry behavior for classified failures.

## Current limitations to keep visible

This walkthrough describes existing code, not fictional completeness. Notable current
limitations include direct SQLAlchemy access instead of implemented repository adapters,
missing PATCH/DELETE/nodes and administration APIs, a fake rather than Kubernetes-backed
management adapter/observer, incomplete operation-stage lifecycle, and absent production
telemetry/tracing/deployment assets. The project-goals alignment table is authoritative
for the complete gap list.
