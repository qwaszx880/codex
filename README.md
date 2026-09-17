# Cluster Control Plane

A tenant-aware control plane for managing Cluster API (CAPI) clusters without giving
the central API credentials for any management Kubernetes cluster. PostgreSQL is the
authoritative platform store, RabbitMQ carries commands, and Celery executors apply
desired state through a provider adapter. CAPO/OpenStack is the first compiler, not a
permanent coupling in the domain model.

The repository includes a **fully local simulation**. You do not need Kubernetes,
OpenStack, Python, PostgreSQL, or RabbitMQ installed on the host; normal development
and tests can run through Docker Compose.

## Contents

- [Architecture](#architecture)
- [Components](#components)
- [Operation lifecycle](#operation-lifecycle)
- [Desired and observed state](#desired-and-observed-state)
- [API reference](docs/api-reference/README.md)
- [Database model](#database-model)
- [Detailed database schema and frontend polling guide](docs/database-schema/README.md)
- [IAM, tenancy, and authorization guide](docs/iam/README.md)
- [Repository layout](#repository-layout)
- [Compose-only local development](#compose-only-local-development)
- [Local failure testing](#local-failure-testing)
- [Production boundaries](#production-boundaries)
- [Canonical project goals and alignment review](docs/project-goals/README.md)
- [Python code walkthrough](docs/code-walkthrough/README.md)
- [Hands-on FastAPI and Python guide](docs/fastapi-guide/README.md)
- [Fake executor guide](docs/fake-executor/README.md)
- [Fake observer guide](docs/fake-observer/README.md)

## Architecture

```text
                              CENTRAL PLATFORM

 Keycloak/OIDC -> FastAPI -> PostgreSQL
                    |          | desired state, IAM, operations,
                    |          | audit, observations, outbox
                    |          v
                    |     Outbox publisher
                    |          |
                    |          v
                    +------ RabbitMQ --------+
                                             |
                              MANAGEMENT SIDE| (simulated locally)
                                             v
                                      Celery executor
                                             |
                                      command processor
                                             |
                                     provider adapter
                                             |
                                  CAPI/CAPO/Kubernetes

 status observer -> raw conditions + normalized health -> PostgreSQL -> FastAPI
```

The design keeps four concepts separate:

1. **Identity and authorization** — external OIDC identity is mapped to a persisted
   principal; organization/project membership and role permissions are platform data.
2. **Desired state** — every accepted change creates an immutable `ClusterRevision`.
3. **Operations and reconciliation** — an operation records user intent; outbox and
   queue delivery cause an executor to apply one exact target revision.
4. **Observed state and health** — raw CAPI/CAPO resource status and condition history
   are retained independently from operations and derived cluster health.

The central API has no kubeconfig and does not call management-cluster Kubernetes
APIs. A production executor uses its local ServiceAccount. The local executor uses a
fake adapter implementing that same boundary.

## Components

### Compose services

| Service | Responsibility | Host access |
|---|---|---|
| `postgres` | Authoritative platform database | `<PLATFORM_PUBLIC_HOST>:5432` |
| `adminer` | Browser UI for PostgreSQL | `http://<PLATFORM_PUBLIC_HOST>:8080` |
| `rabbitmq` | Celery command transport and management UI | AMQP `:5672`, UI `http://<PLATFORM_PUBLIC_HOST>:15672` |
| `keycloak` | Local OIDC issuer and administration UI | `http://<PLATFORM_PUBLIC_HOST>:8081` |
| `migrate` | One-shot Alembic schema migration | no host port |
| `bootstrap` | One-shot idempotent local tenant/IAM seed | no host port |
| `api` | FastAPI control-plane and Prometheus endpoint | `http://<PLATFORM_PUBLIC_HOST>:8000` |
| `outbox` | Relays committed outbox rows to Celery/RabbitMQ | no host port |
| `executor` | Consumes commands, takes leases, compiles and applies resources | scalable, no host port |
| `fake-observer` | Simulates CAPI/CAPO observations and convergence | no host port |
| `flower` | Browser UI for Celery workers and tasks | `http://<PLATFORM_PUBLIC_HOST>:5555` |

### Browser UI credentials

| UI | Credentials / connection details |
|---|---|
| Adminer | system `PostgreSQL`, server `postgres`, database/user/password `platform` |
| RabbitMQ | `platform` / `platform` |
| Keycloak administration | `admin` / `admin` |
| Flower | no authentication in local Compose; never expose this configuration publicly |
| API docs | bearer token obtained from Keycloak as described below |

### Application responsibilities

- **FastAPI** validates OIDC access tokens, maps their issuer/subject to principals,
  resolves project permissions, validates `ClusterSpec`, and calls application services.
- **Cluster service** writes a revision, operation, audit event, and outbox event in a
  single transaction. RabbitMQ availability is not part of the API transaction.
- **Outbox publisher** claims rows with `FOR UPDATE SKIP LOCKED`, tracks attempts and
  errors, recovers stale claims, and emits proper Celery protocol messages.
- **Celery executor** uses late acknowledgements and bounded retries. The command
  processor verifies event idempotency, the exact desired revision, and a per-cluster
  lease before calling the provider-neutral compiler/adapter boundary. See the
  [fake executor guide](docs/fake-executor/README.md) for the local command path,
  concurrency behavior, failure modes, and production boundary.
- **Fake observer** polls reconciling operations, materializes a deterministic local
  resource/status snapshot, derives health, and completes or fails each operation. See
  the [fake observer guide](docs/fake-observer/README.md) for its exact behavior and
  deliberate differences from a production watcher.
- **Heartbeat process** runs with every executor replica and records executor/CAPI/CAPO
  versions, state, and `last_seen` in PostgreSQL.

## Operation lifecycle

A create, scale, or upgrade request follows this path:

1. The API verifies the token and required project permission.
2. Pydantic and domain validation reject malformed or inconsistent `ClusterSpec` data.
3. One database transaction writes:
   - an immutable cluster revision;
   - an `ACCEPTED` operation targeting that revision;
   - a security/lifecycle audit event;
   - an unpublished outbox event.
4. The independent outbox process claims and publishes the event to the queue named
   for its management cluster (`local-mgmt` locally).
5. One executor replica receives it. Different clusters can run concurrently, while
   a PostgreSQL lease serializes mutation of the same cluster.
6. The command processor rejects duplicate event IDs, stale target revisions, and
   conflicting leases; otherwise it compiles and applies resources and marks the
   operation `RECONCILING`.
7. The observer records raw resources and conditions, updates derived health and the
   observed revision, then marks the operation `SUCCEEDED` or `FAILED`.

Delivery is intentionally at least once. `processed_events` makes completed commands
idempotent. An operation's result is historical user intent: a later machine fault may
change health to degraded, but must not rewrite an earlier successful create operation.

### Revisions

- `desired_revision`: newest accepted user intent.
- `applied_revision`: revision accepted by the management adapter.
- `observed_revision`: revision seen as converged by the observer.

Scale and upgrade do not overwrite the old specification. They copy the latest spec,
change the requested field, and append a new immutable revision.

### Local simulated resources

The fake plane creates observations for `Cluster`, `OpenStackCluster`,
`KubeadmControlPlane`, `OpenStackMachineTemplate`, `KubeadmConfigTemplate`,
`MachineDeployment`, `MachineHealthCheck`, optional `ClusterResourceSet`, `MachineSet`,
`Machine`, and `OpenStackMachine`. Each observation contains its namespace/name/UID, generation,
resource version, conditions, raw status, and observation time. The first simulated
`Ready` condition is written to condition history; later simulated revisions update the
latest resource snapshot without appending transition history. Normalized health remains
a separate projection.

## Component flows

### Authentication and authorization flow

```text
client
  -> Keycloak: authenticate
  <- Keycloak: signed access token (iss, sub, aud)
  -> FastAPI: Authorization: Bearer <token>
  -> OIDC JWKS: resolve signing key
  -> FastAPI: verify signature + issuer + audience + expiry
  -> PostgreSQL: find/create principal by (issuer, external_subject)
  -> PostgreSQL: resolve project memberships -> roles -> permissions
  -> application service: execute only when required permission is present
```

No password is sent to or stored by the platform API. The local bootstrap principal's
external subject is deliberately the same UUID as the imported Keycloak user's ID.

Effective project permissions are the union of direct project-role grants and
organization-role grants inherited by projects in that organization. Role scope is
checked while resolving permissions: organization roles cannot be attached as project
memberships, and project roles do not become organization-wide. The local seed provides
project viewer, operator, and administrator roles plus an organization administrator.
Principals are mapped from validated OIDC `(issuer, sub)` claims; membership APIs use
persisted principal IDs and never store passwords or bearer tokens.
Membership changes and their security audit event commit in the same database
transaction and retain the actor, target principal, role, project, and request ID.

For the complete supported IAM API, role inheritance rules, administration examples,
security boundaries, and known limitations, see the
[IAM, tenancy, and authorization guide](docs/iam/README.md).

### Mutation and dispatch flow

```text
POST /v1/clusters
  -> route: parse HTTP model and resolve actor permissions
  -> ClusterService: validate project and management target
  -> PostgreSQL transaction
       clusters / cluster_revisions
       operations
       audit_events
       outbox_events
     COMMIT
  <- 202 Accepted + operation

outbox process
  -> SELECT unpublished rows FOR UPDATE SKIP LOCKED
  -> mark claim owner, time, and attempt
  -> Celery send_task with management-cluster routing key
  -> publisher-confirmed RabbitMQ delivery
  -> mark published_at (or retain last_error for retry)
```

The API response does not wait for RabbitMQ, Kubernetes, OpenStack, or convergence. A
queue outage therefore does not roll back already accepted user intent.

### Executor reconciliation flow

```text
RabbitMQ local-mgmt queue
  -> one Celery executor replica
  -> processed_events lookup             (duplicate => safe no-op)
  -> lock cluster row and compare revision (stale => safe no-op)
  -> acquire/renew per-cluster lease       (conflict => bounded retry)
  -> load immutable target revision
  -> ClusterCompiler.compile(...)
  -> ManagementClusterAdapter.apply(...)
  -> set applied_revision and RECONCILING
  -> record processed event
```

Serialization is per workload cluster, not global. Three executor replicas can process
clusters A, B, and C concurrently, but cannot apply two operations to cluster A at once.
The [fake executor guide](docs/fake-executor/README.md) describes how the local adapter
preserves these production command invariants without contacting Kubernetes.

### Observation and health flow

```text
CAPI/CAPO resources (fake resources locally)
  -> observer
  -> resource_status: latest raw metadata, conditions and raw status
  -> condition_history: meaningful transitions
  -> cluster_status: derived UI/API health projection
  -> operation_stages: convergence progress/failure
  -> operation: SUCCEEDED or FAILED when convergence is determined
```

Executor heartbeat is separate from workload health. Losing an executor changes
management connectivity to unknown after the heartbeat becomes stale; it does not by
itself declare every workload cluster unhealthy.

## Desired and observed state

The platform-owned input model is JSON rather than raw provider YAML:

```text
ClusterSpec
├── kubernetes       # Kubernetes version
├── networking       # pod and service CIDRs
├── control_plane    # replicas and node profile
├── worker_node_types # named worker shapes, roles, replicas, profiles and labels
├── scaling          # default autoscaling intent/range (overridable per worker type)
├── machine_health_check # remediation policy, enabled with safe defaults
├── addons           # references to pre-created ConfigMaps/Secrets
└── features         # platform feature flags
```

The CAPO compiler turns the stable input into provider resources. Provider references
in PostgreSQL contain configuration and a secret-store reference only; provider
passwords and application user passwords are not stored in ordinary platform tables.

## API

The complete endpoint inventory, permissions, status codes, request-body summary,
polling contract, errors, and examples live in the dedicated
[API reference](docs/api-reference/README.md). Swagger UI is available at
`http://<PLATFORM_PUBLIC_HOST>:8000/docs` when the service is running.

All application routes are below `/v1` and require a bearer token. Cluster mutations
return `202 Accepted` with an operation to poll; they do not wait for RabbitMQ,
Kubernetes, OpenStack, or convergence. `/healthz` and the locally unprotected
`/metrics` mount are process-level exceptions.

## Database model

PostgreSQL is authoritative for platform control-plane state, but not runtime metric
time series. The mappings currently live together in `infrastructure/database.py`;
repositories are represented as ports so persistence can be split without changing
domain use cases.

For the maintainable Mermaid entity-relationship diagram, field-level reference,
code-to-table map, and a browser polling example, see the
[database schema and frontend status guide](docs/database-schema/README.md).

| Area | Tables/models | Purpose |
|---|---|---|
| IAM | `principals`, `roles`, `permissions`, `role_permissions` | External identity mapping and extensible permissions |
| Tenancy | `organizations`, `organization_memberships`, `projects`, `project_memberships` | Ownership, project boundary, namespace mapping, role assignment |
| Provider | `provider_references`, `node_profiles`, `management_clusters` | Non-secret provider metadata, reusable machine intent, executor routing target |
| Desired state | `clusters`, `cluster_revisions` | Revision pointers and immutable specifications |
| Operations | `operations`, `operation_stages` | User intent, high-level state, stage progress and failures |
| Delivery | `outbox_events`, `processed_events` | Atomic dispatch intent and consumer idempotency |
| Concurrency | `reconciliation_leases` | Per-cluster mutation serialization across executor replicas |
| Observation | `cluster_status`, `resource_status`, `condition_history` | Derived health, raw provider status, preserved transitions |
| Executors | `executor_instances` | Version, management target, readiness and last-seen heartbeat |
| Security | `audit_events` | Actor, tenant, action, target, request, revisions and result |

Important relationships:

```text
organization 1 --- * projects 1 --- * clusters 1 --- * cluster_revisions
      |                 |                |
      * memberships     * memberships    * operations 1 --- * operation_stages
                                           |
                                           +--- target_revision
cluster 1 --- 1 cluster_status
cluster 1 --- * resource_status 1 --- * condition_history
cluster 1 --- 0..1 reconciliation_lease
management_cluster 1 --- * clusters / executor_instances
```

`provider_references.secret_reference` is an opaque pointer. Secret values do not belong
in provider configuration, audit details, API responses, or ordinary application tables.

## Repository layout

```text
.
├── compose.yaml                 # complete local topology and service dependencies
├── Dockerfile                   # common API/worker/test image
├── pyproject.toml               # runtime/dev dependencies and tool configuration
├── .env.example                 # host-side equivalents of Compose configuration
├── alembic.ini
├── alembic/
│   ├── env.py                   # Alembic runtime configuration
│   └── versions/0001_initial.py # initial authoritative platform schema
├── dev/
│   ├── frontend/oidc-config.example.json # browser frontend OIDC/API settings
│   └── keycloak/realm.json      # imported local realm, clients and developer user
├── src/platform_service/
│   ├── main.py                  # FastAPI application assembly
│   ├── config.py                # environment-backed settings
│   ├── api/
│   │   ├── routes.py            # thin HTTP endpoints and tenancy checks
│   │   └── schemas.py           # HTTP request/response models
│   ├── domain/
│   │   ├── spec.py              # stable ClusterSpec and domain validation
│   │   ├── ports.py             # replaceable repository/provider/metrics/secret ports
│   │   └── errors.py            # reconciliation error classifications
│   ├── application/
│   │   ├── admin_service.py     # audited tenant/provider metadata administration
│   │   ├── cluster_service.py   # transactional intent and revision use cases
│   │   ├── errors.py            # expected application failure vocabulary
│   │   ├── iam_service.py       # project membership and RBAC use cases
│   │   └── reconciliation.py    # idempotent, leased command processor
│   ├── infrastructure/
│   │   ├── database.py          # SQLAlchemy authoritative-state mappings
│   │   ├── auth.py              # OIDC verification and permission resolution
│   │   ├── bootstrap.py         # idempotent local seed data
│   │   └── compiler.py          # pure CAPO compiler implementation
│   └── workers/
│       ├── celery_app.py        # Celery routing/reliability configuration
│       ├── tasks.py             # thin task and local adapter
│       ├── outbox.py            # reliable DB-to-Celery relay
│       ├── fake_observer.py     # local CAPI/CAPO simulation
│       └── heartbeat.py         # executor presence/version reporting
└── tests/
    ├── test_admin_service.py
    ├── test_cluster_service.py
    ├── test_iam.py
    ├── test_spec.py
    ├── test_compiler.py
    └── test_local_stack.py
```

`api` depends on application services; application/domain code depends on ports;
infrastructure and workers implement those boundaries. Keep business decisions out of
routes and Celery task functions.

### File-by-file reference

| File | What to change it for | What should not live there |
|---|---|---|
| `src/platform_service/main.py` | ASGI assembly, routers, middleware, metrics mount | lifecycle business rules |
| `src/platform_service/config.py` | environment-driven process settings | tenant/provider records |
| `api/schemas.py` | public HTTP request and response shapes | SQL queries |
| `api/routes.py` | HTTP mapping, dependency injection, status codes | compilation or reconciliation loops |
| `domain/spec.py` | stable provider-neutral desired-state model and invariants | CAPO manifest details |
| `domain/ports.py` | replaceable boundary protocols | concrete SQLAlchemy/Kubernetes clients |
| `domain/errors.py` | shared retry/error classification vocabulary | transport-specific retry loops |
| `application/admin_service.py` | audited principal, project, provider reference, and node-profile mutations | identity-provider passwords or async cluster work |
| `application/cluster_service.py` | revisioned cluster create/update/delete/scale/upgrade use cases and atomic intent | HTTP or Celery framework code |
| `application/iam_service.py` | project membership and visible-project RBAC use cases | token verification or HTTP serialization |
| `application/reconciliation.py` | idempotency, staleness, lease and command orchestration | long-running observer loops |
| `infrastructure/database.py` | SQLAlchemy persistence mappings and session factory | API serialization |
| `infrastructure/auth.py` | token verification, principal mapping, permission lookup | passwords or Kubernetes RBAC |
| `infrastructure/compiler.py` | pure `ClusterSpec` to CAPO resource translation | live Kubernetes calls |
| `infrastructure/bootstrap.py` | deterministic development-only records | production credential provisioning |
| `workers/celery_app.py` | queues, routing, acknowledgement and broker policy | reconciliation business logic |
| `workers/tasks.py` | thin Celery entry point and local adapter wiring | domain decisions |
| `workers/outbox.py` | claim/publish/retry bookkeeping | creation of user intent |
| `workers/fake_observer.py` | local resource and condition simulation | a separate local command contract |
| `workers/heartbeat.py` | periodic executor presence reporting | workload health decisions |
| `alembic/versions/*` | ordered, reviewable production schema changes | application startup schema creation |
| `dev/keycloak/realm.json` | local realm/client/user import | production identity configuration |
| `tests/test_spec.py` | desired-state validation behavior | external integration assumptions |
| `tests/test_compiler.py` | deterministic compiler output | live provider calls |
| `tests/test_local_stack.py` | local identity/seed/simulator contract | production secrets |


## Compose-only local development

### Prerequisites

Install Docker Engine/Desktop with the Compose v2 plugin. The commands below require
only `docker compose`; host Python, PostgreSQL, RabbitMQ, Keycloak, and Kubernetes are
not required. `curl` is useful for API examples; `jq` is optional.

### 1. Start everything

Compose publishes interfaces on `PLATFORM_BIND_ADDRESS` and uses
`PLATFORM_PUBLIC_HOST` in browser-visible URLs and the OIDC issuer. For development on
the same machine, the defaults are `0.0.0.0` and `localhost`. When Compose runs on a
VM, copy the example environment and set the public host to the VM address that your
browser can reach:

```bash
cp .env.example .env
# Edit .env, for example:
# PLATFORM_PUBLIC_HOST=192.0.2.10
# PLATFORM_BIND_ADDRESS=0.0.0.0
# PLATFORM_OIDC_ISSUER=http://192.0.2.10:8081/realms/platform
# PLATFORM_OIDC_JWKS_URL=http://192.0.2.10:8081/realms/platform/protocol/openid-connect/certs
```

`PLATFORM_PUBLIC_HOST` must be a host or IP without a URL scheme or port. Allow TCP
ports `8000`, `8080`, `8081`, `5555`, and `15672` through the VM and host firewalls as
needed. Ports `5432` and `5672` are also published for development tools; restrict
`PLATFORM_BIND_ADDRESS` or firewall access when those services should not be reachable
remotely. These local credentials and the permissive local Keycloak redirect settings
are for development only and must never be exposed to an untrusted network.

From the repository root, load the same public host into the example shell commands
and start the stack:

```bash
set -a; . ./.env; set +a
PLATFORM_URL="http://${PLATFORM_PUBLIC_HOST}:8000"
OIDC_URL="http://${PLATFORM_PUBLIC_HOST}:8081"
docker compose up --build -d
docker compose ps
```

Wait until `postgres` and `rabbitmq` are healthy and the one-shot `migrate` and
`bootstrap` services show exit code `0`:

```bash
docker compose ps -a
docker compose logs migrate bootstrap
docker compose logs --tail=100 api outbox executor fake-observer
```

Do not run a separate migration or seed command on a normal clean start; Compose
orders those jobs before the API and workers.

### 2. Get a local access token

The imported user is `developer` with password `developer`:

```bash
TOKEN=$(curl -fsS -X POST \
  "$OIDC_URL/realms/platform/protocol/openid-connect/token" \
  -H 'content-type: application/x-www-form-urlencoded' \
  -d grant_type=password \
  -d client_id=cluster-platform \
  -d username=developer \
  -d password=developer \
  | python -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')
```

Using Python for JSON extraction is optional and happens on the host in this example.
If you want a strictly Docker-only token extraction, save the response and paste the
`access_token`, or use the API docs after obtaining a token through your preferred tool.

### Browser frontend login

The imported realm also contains the public `cluster-platform-frontend` client for a
browser-based frontend served from `http://localhost:3000` (or
`http://127.0.0.1:3000`). It uses the OIDC authorization-code flow with PKCE; it has no
client secret, disables the password and implicit grants, and adds the
`cluster-platform` audience required by the API to access tokens.

Pass [`dev/frontend/oidc-config.example.json`](dev/frontend/oidc-config.example.json)
to an OIDC client library, or expose the equivalent values through the frontend's
public runtime configuration. The frontend should redirect to Keycloak for login,
handle the configured `/auth/callback`, and send the resulting access token as
`Authorization: Bearer <token>` when calling `api_base_url`. Do not send the ID token
to the API, and do not add a client secret to browser code.

The example URLs are deliberately fixed to the local development origins allowed by
the imported client. When the frontend runs on another origin, update `redirectUris`,
`webOrigins`, and `post.logout.redirect.uris` in `dev/keycloak/realm.json`, then update
the matching frontend values before importing a fresh realm. Use exact trusted HTTPS
origins and production identity-provider configuration outside local development.

The seeded IDs are stable:

| Object | ID / name |
|---|---|
| organization | `20000000-0000-0000-0000-000000000001` (`local`) |
| project | `30000000-0000-0000-0000-000000000001` (`sandbox`) |
| project namespace | `p-30000000` |
| management cluster | `40000000-0000-0000-0000-000000000001` (`local-mgmt`) |
| provider reference | `50000000-0000-0000-0000-000000000001` |
| node profiles | `control`, `compute` |

### 3. Create and inspect a simulated cluster

```bash
OPERATION=$(curl -fsS "$PLATFORM_URL/v1/clusters" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -H 'X-Request-ID: local-create-demo' \
  -d '{
    "project_id":"30000000-0000-0000-0000-000000000001",
    "management_cluster_id":"40000000-0000-0000-0000-000000000001",
    "provider_reference_id":"50000000-0000-0000-0000-000000000001",
    "name":"demo",
    "spec":{
      "kubernetes":{"version":"v1.31.1"},
      "control_plane":{"replicas":3,"node_profile":"control"},
      "worker_node_types":[{"name":"workers","role":"worker","replicas":3,"node_profile":"compute"}]
    }
  }')
echo "$OPERATION"
```

Copy `cluster_id` from the response into `CLUSTER_ID`, wait a few seconds, then query:

```bash
CLUSTER_ID=<cluster UUID from the response>
curl -fsS -H "Authorization: Bearer $TOKEN" \
  "$PLATFORM_URL/v1/clusters/$CLUSTER_ID/operations"
curl -fsS -H "Authorization: Bearer $TOKEN" \
  "$PLATFORM_URL/v1/clusters/$CLUSTER_ID/health"
curl -fsS -H "Authorization: Bearer $TOKEN" \
  "$PLATFORM_URL/v1/clusters/$CLUSTER_ID/resources"
```

The create endpoint actually returns an operation model containing `cluster_id`, so no
additional parsing endpoint is needed.

### 4. Modify or delete a cluster

```bash
curl -fsS -X POST "$PLATFORM_URL/v1/clusters/$CLUSTER_ID/scale" \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"pool":"workers","replicas":5}'

curl -fsS -X POST "$PLATFORM_URL/v1/clusters/$CLUSTER_ID/upgrade" \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"version":"v1.32.0"}'

curl -fsS -X POST "$PLATFORM_URL/v1/clusters/$CLUSTER_ID/worker-node-types" \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"worker_node_type":{"name":"batch","replicas":2,"node_profile":"compute"}}'

curl -fsS -X DELETE "$PLATFORM_URL/v1/clusters/$CLUSTER_ID" \
  -H "Authorization: Bearer $TOKEN"
```

Each response is an operation. Poll `/v1/operations/{operation_id}` as described in the
[frontend status guide](docs/database-schema/README.md#frontend-polling-follow-an-executed-task).

Each call creates a new revision and operation. Watch it move through RabbitMQ and
Celery in the RabbitMQ and Flower UIs, then inspect revisions and conditions via API.

### 5. Develop and test without host Python

After changing Python, tests, dependencies, or the Dockerfile, rebuild the shared image:

```bash
docker compose build api outbox executor fake-observer migrate bootstrap
```

Run all automated checks inside an ephemeral Compose container:

```bash
docker compose run --rm --no-deps api pytest -q
docker compose run --rm --no-deps api ruff check .
docker compose run --rm --no-deps api python -m compileall -q src
```

Run one test or open a shell:

```bash
docker compose run --rm --no-deps api pytest -q tests/test_compiler.py
docker compose run --rm --no-deps api sh
```

Apply migrations manually after adding a revision:

```bash
docker compose run --rm migrate alembic upgrade head
```

Restart only changed long-running services after rebuilding:

```bash
docker compose up -d --build api outbox executor fake-observer
docker compose logs -f api outbox executor fake-observer
```

### 6. Test executor concurrency

```bash
docker compose up -d --scale executor=3
docker compose ps executor
docker compose logs -f executor
```

RabbitMQ distributes different cluster commands between replicas. Database leases
prevent two replicas from mutating the same cluster concurrently.

### 7. Stop or reset

Stop while retaining PostgreSQL/RabbitMQ volumes:

```bash
docker compose down
```

Delete all local platform state, queues, and imported runtime data, then start clean:

```bash
docker compose down -v --remove-orphans
docker compose up --build -d
```

## Local failure testing

Compose makes failure paths reproducible:

| Scenario | Command / action | Expected behavior |
|---|---|---|
| Publisher outage | `docker compose stop outbox` | API commits continue; unpublished rows accumulate |
| Resume publishing | `docker compose start outbox` | backlog is published after restart |
| Executor outage | `docker compose stop executor` | RabbitMQ retains durable queued commands |
| Observer outage | `docker compose stop fake-observer` | applied operations remain reconciling; observations stop |
| Heartbeat loss | stop executor replicas | heartbeat ages; workload health is not automatically rewritten as failed |
| Transient adapter error | set `FAKE_FAILURE_MODE=transient` for executor and recreate it | task retries with bounded backoff |
| Permanent adapter error | set `FAKE_FAILURE_MODE=permanent` for executor and recreate it | task fails without retry and its transaction rolls back |
| Permanent CAPI condition | set `FAKE_CAPI_FAILURE=true` for observer and recreate it | raw failed conditions and failed operation are persisted |
| Duplicate task | redeliver a completed payload from RabbitMQ UI | processed event becomes a safe no-op |
| Stale revision | delay an older command until a newer desired revision exists | executor classifies it stale and does not apply it |

For a temporary observer failure setting without editing committed Compose configuration:

```bash
docker compose stop fake-observer
docker compose run --rm -e FAKE_CAPI_FAILURE=true fake-observer
```

That foreground process continues polling; stop it with `Ctrl-C`, then restore the normal
observer with `docker compose up -d fake-observer`.

Inspect platform tables using Adminer, RabbitMQ exchanges/queues through the management
UI, and worker/task state through Flower. Logs are available with:

```bash
docker compose logs -f --tail=200 api outbox executor fake-observer rabbitmq postgres
```

The [fake executor](docs/fake-executor/README.md) and
[fake observer](docs/fake-observer/README.md) guides provide focused commands and
troubleshooting for the two halves of the local management-plane simulation.

## Production boundaries

The Compose credentials and unauthenticated Flower UI are development-only. A
production deployment must provide:

- external OIDC and TLS;
- HA PostgreSQL with connection-failover handling;
- clustered RabbitMQ with replicated/quorum queues;
- a real secret-store implementation and provider secret mechanism;
- in-management-cluster executors with least-privilege ServiceAccounts;
- a real watch-based status observer and metrics backend;
- network policy ensuring the central platform cannot reach management kube-apis;
- OpenTelemetry export and production dashboards/alerts.

Run schema changes through Alembic. Application startup deliberately never invokes
`metadata.create_all()`. Replace the fake adapter and observer at their interfaces;
do not add management-cluster kubeconfigs to the central API.
