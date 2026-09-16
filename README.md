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
- [API](#api)
- [Repository layout](#repository-layout)
- [Compose-only local development](#compose-only-local-development)
- [Local failure testing](#local-failure-testing)
- [Production boundaries](#production-boundaries)

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
| `postgres` | Authoritative platform database | `localhost:5432` |
| `adminer` | Browser UI for PostgreSQL | <http://localhost:8080> |
| `rabbitmq` | Celery command transport and management UI | AMQP `:5672`, UI <http://localhost:15672> |
| `keycloak` | Local OIDC issuer and administration UI | <http://localhost:8081> |
| `migrate` | One-shot Alembic schema migration | no host port |
| `bootstrap` | One-shot idempotent local tenant/IAM seed | no host port |
| `api` | FastAPI control-plane and Prometheus endpoint | <http://localhost:8000> |
| `outbox` | Relays committed outbox rows to Celery/RabbitMQ | no host port |
| `executor` | Consumes commands, takes leases, compiles and applies resources | scalable, no host port |
| `fake-observer` | Simulates CAPI/CAPO observations and convergence | no host port |
| `flower` | Browser UI for Celery workers and tasks | <http://localhost:5555> |

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
  lease before calling the provider-neutral compiler/adapter boundary.
- **Fake observer** produces the resource types and condition changes a real observer
  would report, derives health, and completes or fails the corresponding operation.
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
`KubeadmControlPlane`, `MachineDeployment`, `MachineSet`, `Machine`, and
`OpenStackMachine`. Each observation contains its namespace/name/UID, generation,
resource version, conditions, raw status, and observation time. Condition transitions
are also written to history; normalized health remains a separate projection.

## Desired and observed state

The platform-owned input model is JSON rather than raw provider YAML:

```text
ClusterSpec
├── kubernetes       # Kubernetes version
├── networking       # pod and service CIDRs
├── control_plane    # replicas and node profile
├── worker_pools     # named pools, replicas, profiles, labels and taints
├── scaling          # autoscaling intent/range
└── features         # platform feature flags
```

The CAPO compiler turns the stable input into provider resources. Provider references
in PostgreSQL contain configuration and a secret-store reference only; provider
passwords and application user passwords are not stored in ordinary platform tables.

## API

All application endpoints except `/healthz` and `/metrics` are below `/v1` and require
`Authorization: Bearer <token>`. Send an optional `X-Request-ID`; the API generates one
when absent and records it with operations/audit events.

| Method | Path | Permission | Behavior |
|---|---|---|---|
| `POST` | `/v1/clusters` | `cluster.create` | Create revision 1 and return an accepted operation (`202`) |
| `GET` | `/v1/clusters?project_id={uuid}` | `cluster.read` | List non-deleted project clusters |
| `GET` | `/v1/clusters/{cluster_id}` | `cluster.read` | Read revision pointers and cluster metadata |
| `POST` | `/v1/clusters/{cluster_id}/scale` | `cluster.scale` | Change one worker-pool replica count in a new revision (`202`) |
| `POST` | `/v1/clusters/{cluster_id}/upgrade` | `cluster.upgrade` | Change Kubernetes version in a new revision (`202`) |
| `GET` | `/v1/clusters/{cluster_id}/revisions` | `cluster.read` | Return immutable desired-state history |
| `GET` | `/v1/clusters/{cluster_id}/operations` | `cluster.read` | Return operation history newest first |
| `GET` | `/v1/operations/{operation_id}` | `cluster.read` | Read a single operation |
| `GET` | `/v1/clusters/{cluster_id}/health` | `cluster.read` | Read normalized health or `UNKNOWN` before observation |
| `GET` | `/v1/clusters/{cluster_id}/resources` | `cluster.read` | Read raw resource observations |
| `GET` | `/v1/clusters/{cluster_id}/conditions` | `cluster.read` | Read conditions grouped by resource |
| `GET` | `/healthz` | none | Process liveness response |
| `GET` | `/metrics` | none in local setup | Prometheus-formatted application metrics endpoint |

OpenAPI is available at <http://localhost:8000/docs>. The currently implemented
mutation surface is create, scale, and upgrade. General patch and asynchronous delete
remain explicit follow-up work rather than silently pretending to be supported.

### Example payloads

Create:

```json
{
  "project_id": "30000000-0000-0000-0000-000000000001",
  "management_cluster_id": "40000000-0000-0000-0000-000000000001",
  "provider_reference_id": "50000000-0000-0000-0000-000000000001",
  "name": "demo",
  "spec": {
    "kubernetes": {"version": "v1.31.1"},
    "networking": {"pod_cidr": "10.244.0.0/16", "service_cidr": "10.96.0.0/12"},
    "control_plane": {"replicas": 3, "node_profile": "control"},
    "worker_pools": [{"name": "workers", "replicas": 3, "node_profile": "compute"}],
    "scaling": {"autoscaling": false},
    "features": {"audit_logs": true, "metrics": true}
  }
}
```

Scale and upgrade:

```json
{"pool": "workers", "replicas": 5}
```

```json
{"version": "v1.32.0"}
```

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
├── dev/keycloak/realm.json      # imported local realm, client and developer user
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
│   │   ├── cluster_service.py   # transactional intent and revision use cases
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
    ├── test_spec.py
    ├── test_compiler.py
    └── test_local_stack.py
```

`api` depends on application services; application/domain code depends on ports;
infrastructure and workers implement those boundaries. Keep business decisions out of
routes and Celery task functions.

## Compose-only local development

### Prerequisites

Install Docker Engine/Desktop with the Compose v2 plugin. The commands below require
only `docker compose`; host Python, PostgreSQL, RabbitMQ, Keycloak, and Kubernetes are
not required. `curl` is useful for API examples; `jq` is optional.

### 1. Start everything

From the repository root:

```bash
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
  http://localhost:8081/realms/platform/protocol/openid-connect/token \
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
OPERATION=$(curl -fsS http://localhost:8000/v1/clusters \
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
      "worker_pools":[{"name":"workers","replicas":3,"node_profile":"compute"}]
    }
  }')
echo "$OPERATION"
```

Copy `cluster_id` from the response into `CLUSTER_ID`, wait a few seconds, then query:

```bash
CLUSTER_ID=<cluster UUID from the response>
curl -fsS -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/v1/clusters/$CLUSTER_ID/operations"
curl -fsS -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/v1/clusters/$CLUSTER_ID/health"
curl -fsS -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/v1/clusters/$CLUSTER_ID/resources"
```

The create endpoint actually returns an operation model containing `cluster_id`, so no
additional parsing endpoint is needed.

### 4. Scale or upgrade

```bash
curl -fsS -X POST "http://localhost:8000/v1/clusters/$CLUSTER_ID/scale" \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"pool":"workers","replicas":5}'

curl -fsS -X POST "http://localhost:8000/v1/clusters/$CLUSTER_ID/upgrade" \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"version":"v1.32.0"}'
```

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
