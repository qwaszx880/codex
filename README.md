# Cluster Control Plane

A security-boundary-first reference implementation for a multi-tenant CAPI/CAPO
control plane. The central service owns identity, authorization, desired state,
operations, audit and observations. **It has no management-cluster kubeconfig and
never calls a management Kubernetes API.** Executors are the only components behind
the `ManagementClusterAdapter` boundary.

## Fully local quick start (no Kubernetes or OpenStack required)

The default Compose profile is self-contained. It includes a fake management plane,
local OIDC provider, seeded tenant, API, PostgreSQL, RabbitMQ, workers, observer, and
browser UIs:

```bash
docker compose up --build
# Horizontal executor behavior uses the same queue, lease and idempotency paths:
docker compose up --build --scale executor=3
```

| Component | URL / address | Credentials |
|---|---|---|
| FastAPI / OpenAPI | http://localhost:8000/docs | Bearer token below |
| Adminer database UI | http://localhost:8080 | system `PostgreSQL`, server `postgres`, db/user/password `platform` |
| RabbitMQ management | http://localhost:15672 | `platform` / `platform` |
| Flower worker UI | http://localhost:5555 | none (local only) |
| Keycloak OIDC UI | http://localhost:8081 | `admin` / `admin` |
| PostgreSQL | localhost:5432 | `platform` / `platform` |

Compose imports a Keycloak realm and seeds a project administrator, organization,
project, fake management cluster, provider reference, node profiles, roles, and
permissions. Fetch a token for the local `developer` / `developer` user:

```bash
TOKEN=$(curl -fsS -X POST \
  http://localhost:8081/realms/platform/protocol/openid-connect/token \
  -H 'content-type: application/x-www-form-urlencoded' \
  -d grant_type=password -d client_id=cluster-platform \
  -d username=developer -d password=developer | jq -r .access_token)
```

Create a cluster entirely against the simulator:

```bash
curl -fsS http://localhost:8000/v1/clusters \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
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
  }'
```

The outbox publishes a real Celery message to RabbitMQ. A fake executor consumes the
same command contract as production, while the fake observer creates raw Cluster,
OpenStackCluster, KubeadmControlPlane, MachineDeployment, MachineSet, Machine and
OpenStackMachine observations, condition history, normalized health, and operation
convergence. No remote management cluster is contacted.

Set `FAKE_CAPI_FAILURE=true` on `fake-observer` to simulate a permanent CAPI condition
failure. Set `FAKE_FAILURE_MODE=transient` or `permanent` on executors to exercise
retry behavior. Stop `outbox`, `executor`, or `fake-observer` to test publisher,
executor, or observer outages. Remove all local state with `docker compose down -v`.

## Architecture and invariants

```text
OIDC -> FastAPI -> PostgreSQL -> transactional outbox -> RabbitMQ
                                                       -> Celery executor
                                                          -> local adapter -> CAPI/CAPO
observer -> raw conditions + derived health -> PostgreSQL -> FastAPI
workload metrics -> remote-write backend -> MetricsBackend -> FastAPI
```

The implementation keeps four models distinct:

* **identity/authorization:** principals, organization/project membership and role permissions;
* **desired state:** immutable `ClusterRevision` records and desired/applied/observed pointers;
* **operations/reconciliation:** operations, stages, outbox delivery, idempotency and per-cluster leases;
* **observed state/health:** raw resource conditions, condition history and separately derived health.

Ports in `domain/ports.py` keep persistence, messaging, compiler, secret store,
metrics and management adapters replaceable. PostgreSQL stores only provider secret
references. JWT signatures, issuer and audience are validated from OIDC JWKS. Reads
and mutations enforce project permissions.

Revision, operation, audit and outbox rows commit atomically. Publication is a
separate process using Celery protocol messages, publisher confirms, bounded retry,
attempt/error tracking and `FOR UPDATE SKIP LOCKED`. Executors use late acknowledgement,
quorum queues, dead-letter exchange configuration, exact revision checks, event
idempotency and per-cluster leases.

Production must replace the fake adapter/observer with in-cluster implementations,
use a proper secret store, TLS, HA PostgreSQL and clustered RabbitMQ. Run schema
changes through Alembic; application startup never calls `metadata.create_all()`.

## Development checks

```bash
python -m pip install -e '.[dev]'
pytest
ruff check .
python -m compileall -q src
```
