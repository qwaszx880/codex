# Project goals and requirements

This document is the canonical product and architecture target for the cluster control
plane. It records the required end state separately from the root `README.md`, which
explains the runnable implementation. The [alignment review](#implementation-alignment-review)
is a point-in-time comparison with the current repository; **partial** and **planned**
items are not claims of production readiness.

## Architectural invariants

1. **Central control plane.** FastAPI, PostgreSQL, RabbitMQ, the outbox publisher,
   workers, and optional monitoring run centrally. The central service owns identity,
   authorization, validation, desired state, operations, audit, and read APIs. It has
   no management-cluster credentials and never calls a management Kubernetes API.
2. **OIDC/SSO.** Users authenticate through an external OIDC provider such as Keycloak,
   Entra ID, Okta, or Dex. FastAPI validates tokens and maps identities to internal
   principals; human users do not receive Kubernetes service accounts.
3. **Persisted principals.** PostgreSQL stores principal ID, issuer, external subject,
   username/display name, email, human/service type, enabled state, first-seen time,
   and last login. It stores no application passwords.
4. **Organizations.** Organizations are top-level ownership groups. Membership and
   organization roles are persisted, and a principal may join multiple organizations.
5. **Projects.** A project belongs to an organization, is the principal tenancy and
   permission boundary, owns clusters, and is persisted in PostgreSQL.
6. **Platform IAM/RBAC.** The platform—not user-facing Kubernetes RBAC—authorizes
   organization admins, project admins, operators, developers, and viewers. Permission
   vocabulary covers cluster create/read/update/scale/upgrade/delete, provider config,
   project administration, and audit viewing, while permitting custom roles later.
7. **Membership and assignment.** Organization memberships, project memberships, role
   assignments, and service-principal assignments are persisted and drive authorization.
8. **Namespace mapping.** Every project maps to a management-cluster namespace (for
   example `p-a83f`). Its CAPI/CAPO resources live there, using namespace-scoped
   Kubernetes RBAC wherever practical.
9. **Audit.** Security/lifecycle audit events are independent of operational logs and
   record actor, organization, project, action, target, request ID, old/new revisions,
   timestamp, source/IP where appropriate, and result.
10. **PostgreSQL authority.** PostgreSQL is authoritative for IAM, tenants, desired
    state, revisions, operations/stages, delivery/idempotency, audit, provider and
    management-cluster metadata, leases/heartbeats, and observed/normalized state.

## Desired state and provider model

11. **Stable `ClusterSpec`.** Users submit a platform-owned JSON contract containing
    Kubernetes, networking, control-plane, worker-node-type, scaling, and feature intent—not
    raw CAPI/CAPO YAML.
12. **Layered validation.** Input passes JSON parsing, Pydantic validation, domain
    validation, then provider validation. Invalid combinations fail before compilation.
13. **Immutable desired-state history.** Every accepted change appends a revision;
    initial create, scaling, node-profile changes, and upgrades never overwrite history.
14. **Revision semantics.** `desired_revision`, `applied_revision`, and
    `observed_revision` remain distinct. Every operation targets one exact revision so
    delayed work can be classified as stale.
15. **Compiler.** A `ClusterCompiler` translates validated `ClusterSpec` into resources
    such as `Cluster`, `OpenStackCluster`, `KubeadmControlPlane`, `MachineDeployment`,
    `KubeadmConfigTemplate`, and `OpenStackMachineTemplate`. A worker node type maps to
    a `MachineDeployment`; CAPI, rather than the platform, owns its `MachineSet` rollout.
16. **Provider abstraction.** CAPO/OpenStack is the first implementation behind a
    provider boundary, not a permanent coupling of the API or domain.
17. **Node profiles.** Named profiles hide repeated provider details such as flavor,
    image, volume, availability zone, labels, and taints.
18. **Provider references.** PostgreSQL stores OpenStack/provider configuration and
    credential references, never the credential values in ordinary application tables.
19. **Secret store.** Sensitive values live in Vault or an equivalent service behind a
    `SecretStore` interface; PostgreSQL retains references only.

## Delivery and execution

20. **Transactional outbox.** One API transaction atomically commits cluster revision,
    operation, audit event, and outbox event. Queue publication occurs after commit.
21. **Separate publisher.** A separate process reads committed outbox rows and publishes
    to RabbitMQ, so API commits do not depend on queue availability.
22. **Reliable publication.** Publishing supports confirms, retries/backoff, attempt and
    error tracking, published timestamps, stale-claim recovery, `SKIP LOCKED`, and
    multiple publisher replicas.
23. **Routed RabbitMQ transport.** Commands are routed to the intended management
    cluster (for example `mgmt-openstack-01`).
24. **Replaceable messaging.** Business logic uses `EventPublisher` and `TaskConsumer`
    boundaries so RabbitMQ can later be replaced.
25. **Celery workers.** Production execution uses Celery for concurrency, lifecycle,
    acknowledgements, retries, routing, backoff, and monitoring.
26. **Thin tasks.** Celery task functions adapt transport concerns and delegate business
    decisions to domain/reconciliation services.
27. **In-cluster executors.** An executor runs in each CAPI management cluster, consumes
    outbound commands, and uses only its local Kubernetes service account to reach CAPI,
    CAPO, and OpenStack.
28. **No central kubeconfig.** Management credentials never enter the central platform;
    management-side network flow is outbound toward central services where possible.
29. **Horizontal execution.** Multiple executor replicas share work through Celery.
30. **Per-cluster serialization.** A lease/lock prevents two replicas from mutating the
    same workload cluster concurrently.
31. **Cross-cluster concurrency.** The lock is scoped to one cluster, never global.
32. **At-least-once delivery.** Duplicate delivery is an expected transport property.
33. **Idempotency.** Event ID, operation ID, and target revision are tracked; an already
    processed event is a safe no-op.
34. **Dead-lettering.** Poison/permanently failing messages have finite retry limits and
    go to a DLQ rather than requeueing forever.
35. **Error taxonomy.** Reconciliation distinguishes `TRANSIENT`, `PERMANENT`, `STALE`,
    `CONFLICT`, and `DUPLICATE` failures.
36. **Backoff policy.** Transient errors use bounded exponential backoff, permanent
    errors fail immediately, and stale/duplicate commands become no-ops.

## Operations and observations

37. **Operations represent intent.** `CREATE`, `UPDATE`, `SCALE`, `UPGRADE`, and `DELETE`
    record user requests.
38. **Operations are not health.** High-level states are `ACCEPTED`, `DISPATCHED`,
    `RECONCILING`, `SUCCEEDED`, `FAILED`, `CANCELLED`, `TIMED_OUT`, and `SUPERSEDED`;
    none substitutes for CAPI status.
39. **Operation stages.** Progress includes validation, dispatch, resource application,
    infrastructure, control-plane initialization/rollout, workers, and convergence.
40. **Stage history.** Each stage retains state, timestamps, reason/message, related
    resource, retry count, and failure information.
41. **CAPI observations.** Real CAPI status is persisted without reducing it to a single
    healthy/unhealthy flag.
42. **CAPO observations.** Infrastructure-provider status and conditions remain
    first-class and separately inspectable.
43. **Observed resources.** At minimum observe `Cluster`, `OpenStackCluster`,
    `KubeadmControlPlane`, `MachineDeployment`, `MachineSet`, `Machine`, and
    `OpenStackMachine`.
44. **Raw observations.** Store kind, namespace, name, UID, generation,
    observed-generation, resource-version, conditions, raw status, and observation time.
45. **Condition fidelity.** Preserve type, status, reason, message, observed generation,
    and transition time.
46. **Condition history.** Persist meaningful transitions, not only the latest value.
47. **Normalized health.** Derive infrastructure, control-plane, workers, machines,
    availability, scaling, rollout, remediation, deletion, and pause summaries.
48. **Derived means derived.** Normalized health supplements rather than replaces raw
    CAPI/CAPO data.
49. **Independent timelines.** Later health degradation never rewrites the historical
    result of a previously successful operation.
50. **Separate executor concerns.** Command processing and status observation are
    logically distinct.
51. **Command processor.** It validates a command, compiles/applies desired resources,
    and marks reconciliation started without blocking for long-running convergence.
52. **Status observer.** It watches resources, records conditions/health, detects
    convergence or failure, and updates operation stages.
53. **Management-cluster registry.** Store ID/name, provider, region, enabled state,
    executor compatibility, and capability metadata.
54. **Executor heartbeat.** Record management cluster, last seen, executor/CAPI/CAPO
    versions, and status.
55. **Heartbeat semantics.** A missing heartbeat makes management connectivity
    `UNKNOWN`; it does not fail every workload cluster.

## Lifecycle and API

56. **Cluster CRUD.** Support create, read, update, and asynchronous delete.
57. **Manual scaling.** Scaling changes desired replicas through a new revision and
    operation.
58. **Kubernetes upgrades.** Version upgrades create a new desired-state revision.
59. **Profile changes.** Worker/control-plane profile changes create revisions and
    operations.
60. **Autoscaling.** Later compile autoscaling intent to CAPI/Cluster Autoscaler config.
61. **Delete lifecycle.** Deletion is asynchronous and observable and retains sufficient
    tombstone/audit history.
62. **Lifecycle API.** Provide `POST/GET /clusters`, `GET/PATCH/DELETE /clusters/{id}`,
    scale/upgrade actions, revision and operation lists, and `GET /operations/{id}`.
63. **Health API.** Provide cluster health, conditions, resources, and nodes endpoints.

## Telemetry and operability

64. **Runtime metrics stay outside PostgreSQL.** CPU, memory, and node time series do
    not belong in the platform database.
65. **Outbound workload telemetry.** A lightweight Prometheus-compatible agent uses
    remote write to a central metrics backend.
66. **Metrics abstraction.** A `MetricsBackend` supports Prometheus, Mimir,
    VictoriaMetrics, Thanos, or another backend.
67. **Metrics API.** FastAPI may expose tenant-authorized cluster metrics and nodes.
68. **Telemetry/reconciliation separation.** Lifecycle state comes from CAPI/CAPO;
    utilization comes from the metrics backend.
69. **Structured logging.** Production JSON logs carry relevant request, principal,
    tenant, cluster, operation, event, revision, management-cluster, executor, resource,
    and stage correlation fields.
70. **Explicit errors.** Database, RabbitMQ, Kubernetes, CAPO, OpenStack, validation,
    and callback failures receive intentional handling—never infinite generic requeue.
71. **Tracing-ready design.** OpenTelemetry can span FastAPI, database, outbox,
    RabbitMQ, Celery, executor, and Kubernetes boundaries.
72. **Application metrics.** Expose API latency/errors, outbox/queue backlog, operation
    duration/failures, heartbeat age, reconciliation duration, and retry counts.
73. **Migrations.** Production schema changes use Alembic; `metadata.create_all()` is
    local-only.
74. **Repository layer.** Application/domain logic should not scatter direct SQLAlchemy
    queries; it should depend on repositories.
75. **Service/domain layer.** FastAPI routes remain thin: route → application service →
    domain → repository/adapter.
76. **Replaceable ports.** Include `ClusterRepository`, `OperationRepository`,
    `IAMRepository`, `AuditRepository`, `EventPublisher`, `TaskConsumer`,
    `ClusterCompiler`, `ManagementClusterAdapter`, `MetricsBackend`, and `SecretStore`.

## Local development and production boundaries

77. **Compose-only central development.** The central stack runs under Docker Compose
    without host Kubernetes, OpenStack, Python, PostgreSQL, or RabbitMQ.
78. **Fake management plane.** Locally simulate all required CAPI/CAPO resource kinds,
    condition transitions, failures, scaling, upgrades, and deletion.
79. **Shared command contract.** The fake executor consumes the production logical
    command contract rather than using a separate path.
80. **Scalable local executor.** `docker compose up --scale executor=3` is supported.
81. **Database UI.** Local Compose provides Adminer or equivalent.
82. **RabbitMQ UI.** Local Compose enables RabbitMQ management.
83. **Worker UI.** Local Compose provides Flower.
84. **Failure exercises.** Local development can simulate publisher/executor outages,
    duplicate delivery, transient/permanent failures, heartbeat loss, stale revisions,
    conflicting operations, and CAPI condition failures.
85. **Production RabbitMQ.** Production uses a clustered deployment with replicated or
    quorum queues; the single local node is not production topology.
86. **Production PostgreSQL.** Production uses HA PostgreSQL and clients tolerate
    interruptions/failover.
87. **Security boundary.** Central holds platform IAM/data; executors hold minimal local
    Kubernetes privileges; provider secrets remain in a secret mechanism.
88. **No secret responses.** APIs may expose provider references but never raw provider
    credentials.
89. **Project isolation.** Authorization, database ownership, namespaces, provider
    access, operations, and metrics queries all enforce project tenancy.
90. **Multi-provider future.** CAPO is the first provider, not a domain constraint.

## Core domain model

```text
IAM                 PLATFORM              OPERATIONS
principals          clusters              operations
organizations       cluster_revisions     operation_stages
org memberships     provider_references   reconciliation_leases
projects             node_profiles         processed_events
project memberships management_clusters   outbox_events
roles/permissions

OBSERVED STATE       EXECUTORS             SECURITY / AUDIT
cluster_status       executor_instances    audit_events
resource_status
resource_conditions
condition_history
```

## Overall architecture

```text
OIDC/SSO -> CENTRAL FastAPI -> PostgreSQL -> outbox publisher -> RabbitMQ
                         ^                                  |
                         |                                  v
                         |                    MANAGEMENT-CLUSTER EXECUTOR
                         |                    command processor + observer
                         |                                  |
                         +-------- observed state ----------+
                                                            v
                                                   local Kubernetes API
                                                    -> CAPI/CAPO
                                                    -> OpenStack

WORKLOAD metrics agent -> remote_write -> central metrics backend -> FastAPI
```

Identity/authorization, desired state, operations/reconciliation, and observed
state/health are separate concepts throughout the design.

## Implementation alignment review

Review date: **2026-09-17**. Status meanings:

- **Implemented**: represented and exercised in the current local reference stack.
- **Partial**: a model/interface or subset exists, but the production requirement is
  incomplete.
- **Planned**: no meaningful implementation exists yet.

| Requirement area | Status | Current evidence | Remaining work |
|---|---|---|---|
| Central security boundary (1, 27–28, 87) | **Partial** | API has no Kubernetes client; executor calls a `ManagementClusterAdapter`; Compose uses the fake adapter. | Package/deploy a real in-cluster executor with scoped ServiceAccount/RBAC and a Kubernetes adapter. |
| OIDC and principals (2–3) | **Implemented locally** | JWT/JWKS validation maps `(issuer, subject)` to the complete `Principal` record; Keycloak supplies local OIDC. | Add production IdP configuration/runbooks, key rotation and negative integration tests. |
| Organizations, projects and IAM (4–8, 89) | **Partial** | Organization/project/member/role/permission tables and namespace mapping exist. Effective RBAC unions scoped project and inherited organization grants; authenticated users can inspect their principal and visible projects, while project administrators can list roles/members and add or remove project assignments. Local bootstrap includes viewer, operator, project-admin, and organization-admin roles. | Add organization/project creation APIs, organization-membership administration, service-principal flows, richer policy/audit tests, and tenancy enforcement for every future endpoint. |
| Audit (9) | **Partial** | Mutation intent records the required audit fields separately from logs. | Add authorized audit read API, rejected-attempt auditing, retention/export policy, and wider action coverage. |
| Authoritative schema (10, 53–54) | **Partial** | SQLAlchemy and Alembic define the principal core tables, including management clusters and executor instances; current resource conditions are embedded in `resource_status` with transitions in `condition_history`. | Decide whether a separate `resource_conditions` relation is required, expand migrations as features mature, and add migration upgrade/downgrade integration tests. |
| ClusterSpec and validation (11–12) | **Partial** | Stable Pydantic models describe worker node types and enforce structural, uniqueness, role, and compiler-owned-label rules before compilation; legacy `worker_pools` input remains readable. | Add an explicit provider-validation service and richer cross-field/provider capability validation. |
| Revisions (13–14, 57–59) | **Partial** | Create, scale and upgrade append specs and track desired/applied/observed revisions; stale commands are rejected. | Add general update and node-profile-change use cases plus stronger concurrent mutation tests. |
| Compiler/provider abstraction (15–19, 90) | **Partial** | `ClusterCompiler`, `ManagementClusterAdapter`, and `SecretStore` ports exist; each worker node type compiles to a selector-compatible `MachineDeployment`, `KubeadmConfigTemplate`, and `OpenStackMachineTemplate`, with role/ownership labels derived consistently. Node profile/provider-reference tables store secret references. | Resolve profile contents into complete CAPO machine templates, implement a real Kubernetes/CAPO adapter and secret-store adapter, and add a second-provider contract test. |
| Transactional outbox (20–22) | **Implemented locally** | Mutation rows commit together; publisher uses confirms, retries, claim metadata, stale-claim recovery and `FOR UPDATE SKIP LOCKED`. | Add crash-window/integration tests and production observability; document the unavoidable confirm/DB-update duplicate window. |
| Messaging and Celery (23–26, 34–36) | **Partial** | Management-cluster routing, late ack, quorum queue arguments, DLX metadata, bounded retries, classifications, and a thin task exist. | Bind/configure and exercise an actual dead-letter queue; move publisher use behind the declared port; persist terminal task failure consistently. |
| Executor safety (29–33) | **Partial** | Celery can scale, processed-event IDs provide idempotency, target revision is checked, and leases are per cluster. | Make lease acquisition atomic under contention, define renewal/release semantics, and add database-backed multi-worker concurrency tests. |
| Operation model (37–40, 49–52) | **Partial** | Operation and stage schemas are separate from health; processor and fake observer are separate; convergence adds a stage. | Implement every state/stage transition, supersession/timeouts/cancellation, stage history across retries, and a real watch-based observer. |
| Observed state and health (41–48) | **Implemented locally** | Fake observer stores all required simulated kinds, raw status, conditions/history, normalized health, and independent operation outcomes. | Implement real Kubernetes watches, resync/restart behavior, transition deduplication, and CAPO-specific normalization tests. |
| Heartbeats (54–55) | **Partial** | Heartbeat worker upserts executor versions/status; cluster connectivity defaults to `UNKNOWN` before observation. | Add stale-heartbeat evaluation that returns connectivity to `UNKNOWN`, avoid direct shared-DB access from remote management networks if required, and define an authenticated callback/API and cleanup policy. |
| Lifecycle and read APIs (56–63) | **Partial** | Create/list/get, scale, upgrade, revisions, operations, health, conditions, and resources exist. | Add PATCH, DELETE, nodes, full asynchronous deletion/tombstones, and missing response schemas. |
| Metrics and telemetry (64–68, 72) | **Planned/partial** | A `MetricsBackend` port and Prometheus ASGI endpoint exist; runtime time series are not stored in PostgreSQL. | Add workload remote-write deployment, backend adapter, tenant-safe metrics/nodes APIs, and the listed application metrics. |
| Logging, errors and tracing (69–71) | **Planned/partial** | Reconciliation classifications and bounded retry handling exist; `structlog` is a dependency. | Configure structured correlation logging, broaden explicit failure mapping, and instrument end-to-end OpenTelemetry propagation. |
| Layering and replaceable boundaries (74–76) | **Partial** | All requested protocols are declared and mutations use an application service; Celery delegates to `CommandProcessor`. | Implement repository adapters and inject them—application services and read routes still issue SQLAlchemy queries directly. |
| Local development (77–84) | **Mostly implemented** | Compose supplies PostgreSQL/Adminer, RabbitMQ UI, Keycloak, API, publisher, scalable executor, fake observer, and Flower using the shared command contract. | Complete deletion simulation and automate every documented failure scenario as integration tests. |
| Production HA (85–86) | **Planned** | Local config uses durable/quorum queue semantics and DB `pool_pre_ping`; README identifies production boundaries. | Supply production deployment topology, RabbitMQ clustering, HA PostgreSQL, reconnect/failover validation, backup and disaster-recovery procedures. |

### Alignment conclusion

The repository is aligned with the **direction and separation of concerns**, and its
local reference stack demonstrates the central intent/outbox/executor/observer path.
It is **not yet aligned with the full production target**. The highest-priority gaps are:

1. repository adapters and dependency injection rather than direct SQLAlchemy access;
2. complete PATCH/DELETE/node and administration/audit APIs;
3. a real in-cluster Kubernetes/CAPI/CAPO executor and observer;
4. atomic, renewable lease behavior and concurrency integration tests;
5. full operation-stage/state lifecycle and terminal failure persistence;
6. real DLQ verification, observability, structured logs, and tracing;
7. metrics/secret-store adapters and production HA deployment assets.

This gap list is intentional: interfaces or schema alone are marked **partial**, never
as complete functionality.
