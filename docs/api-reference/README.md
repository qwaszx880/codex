# API reference

This is the complete reference for the HTTP surface implemented by the control-plane
service. The [FastAPI guide](../fastapi-guide/README.md) explains its implementation;
the [IAM guide](../iam/README.md) explains role inheritance and administration flows.

## Conventions

- Application endpoints are rooted at `/v1` and require
  `Authorization: Bearer <token>`.
- UUID parameters use their standard hyphenated string form. JSON is used for request
  and response bodies; a `204 No Content` response has no body.
- Callers may send `X-Request-ID`. The service generates one when absent and records it
  on operations and audit events created by the request.
- OpenAPI is at `/openapi.json`, Swagger UI at `/docs`, and ReDoc at `/redoc`. These
  generated documentation routes are not versioned.

## Endpoint index

These tables list every explicitly implemented application route. Permissions are
*effective* permissions: organization grants may be inherited by projects.

### Identity and organizations

| Method | Path | Permission | Success | Response or behavior |
|---|---|---|---|---|
| `GET` | `/v1/principals/me` | authenticated | `200` | Current OIDC-backed principal |
| `GET` | `/v1/organizations` | authenticated | `200` | Organizations where the caller has a role |
| `GET` | `/v1/organizations/{organization_id}/principals` | organization `project.admin` | `200` | Organization principal mappings |
| `POST` | `/v1/organizations/{organization_id}/principals` | organization `project.admin` | `201` | Provision a mapping and initial organization role |
| `PATCH` | `/v1/organizations/{organization_id}/principals/{principal_id}` | organization `project.admin` | `200` | Update profile fields or enabled state |
| `GET` | `/v1/organizations/{organization_id}/roles` | organization `project.admin` | `200` | Organization roles and grants |
| `GET` | `/v1/organizations/{organization_id}/projects` | organization `project.admin` | `200` | All projects in the organization |
| `POST` | `/v1/organizations/{organization_id}/projects` | organization `project.admin` | `201` | Create a project |

Principal creation only maps an existing external OIDC identity. It never creates or
returns identity-provider credentials.

### Projects and placement configuration

| Method | Path | Permission | Success | Response or behavior |
|---|---|---|---|---|
| `GET` | `/v1/projects` | authenticated | `200` | Projects visible through direct or organization membership |
| `PATCH` | `/v1/projects/{project_id}` | `project.admin` | `200` | Change name or enabled state |
| `GET` | `/v1/projects/{project_id}/roles` | `project.admin` | `200` | Assignable project roles and grants |
| `GET` | `/v1/projects/{project_id}/members` | `project.admin` | `200` | Principals and role assignments |
| `POST` | `/v1/projects/{project_id}/members` | `project.admin` | `201` | Assign one project role |
| `DELETE` | `/v1/projects/{project_id}/members/{principal_id}/roles/{role_id}` | `project.admin` | `204` | Remove one role assignment |
| `GET` | `/v1/projects/{project_id}/provider-references` | `cluster.read` | `200` | Safe provider metadata; secret references are omitted |
| `POST` | `/v1/projects/{project_id}/provider-references` | `provider.configure` | `201` | Create metadata with an opaque secret reference |
| `PATCH` | `/v1/projects/{project_id}/provider-references/{reference_id}` | `provider.configure` | `200` | Update metadata or secret reference |
| `DELETE` | `/v1/projects/{project_id}/provider-references/{reference_id}` | `provider.configure` | `204` | Delete an unused reference |
| `GET` | `/v1/projects/{project_id}/node-profiles` | `cluster.read` | `200` | Reusable machine profiles |
| `POST` | `/v1/projects/{project_id}/node-profiles` | `provider.configure` | `201` | Create a machine profile |
| `PUT` | `/v1/projects/{project_id}/node-profiles/{profile_id}` | `provider.configure` | `200` | Replace a profile specification |
| `DELETE` | `/v1/projects/{project_id}/node-profiles/{profile_id}` | `provider.configure` | `204` | Delete an unused profile |
| `GET` | `/v1/management-clusters` | authenticated | `200` | Enabled placement targets |

Provider responses omit `secret_reference`. Configuration rejects common raw credential
keys at any nesting level; credentials belong behind the opaque secret reference.

### Cluster desired state

| Method | Path | Permission | Success | Response or behavior |
|---|---|---|---|---|
| `POST` | `/v1/clusters` | `cluster.create` | `202` | Create revision 1; return an operation |
| `GET` | `/v1/clusters?project_id={uuid}` | `cluster.read` | `200` | Non-deleted project clusters |
| `GET` | `/v1/clusters/{cluster_id}` | `cluster.read` | `200` | Metadata and revision pointers |
| `PATCH` | `/v1/clusters/{cluster_id}` | `cluster.update` | `202` | Replace the complete spec in a new revision |
| `DELETE` | `/v1/clusters/{cluster_id}` | `cluster.delete` | `202` | Reconcile deletion and retain history |
| `POST` | `/v1/clusters/{cluster_id}/worker-node-types` | `cluster.update` | `202` | Add a worker node type in a new revision |
| `PUT` | `/v1/clusters/{cluster_id}/worker-node-types/{node_type_name}` | `cluster.update` | `202` | Replace a worker node type in a new revision |
| `DELETE` | `/v1/clusters/{cluster_id}/worker-node-types/{node_type_name}` | `cluster.update` | `202` | Remove a worker node type in a new revision |
| `POST` | `/v1/clusters/{cluster_id}/scale` | `cluster.scale` | `202` | Change replicas in a new revision |
| `POST` | `/v1/clusters/{cluster_id}/upgrade` | `cluster.upgrade` | `202` | Change Kubernetes version in a new revision |
| `GET` | `/v1/clusters/{cluster_id}/revisions` | `cluster.read` | `200` | Immutable history, oldest first |

Every cluster mutation is asynchronous. Success means revision, operation, audit, and
outbox intent committed atomically—not that the management plane has converged.

### Operations and observed state

| Method | Path | Permission | Success | Response or behavior |
|---|---|---|---|---|
| `GET` | `/v1/clusters/{cluster_id}/operations` | `cluster.read` | `200` | Operation history, newest first |
| `GET` | `/v1/operations/{operation_id}` | `cluster.read` on its cluster | `200` | One operation for polling |
| `GET` | `/v1/clusters/{cluster_id}/health` | `cluster.read` | `200` | Normalized health, or `UNKNOWN` before observation |
| `GET` | `/v1/clusters/{cluster_id}/resources` | `cluster.read` | `200` | Raw current resource observations |
| `GET` | `/v1/clusters/{cluster_id}/conditions` | `cluster.read` | `200` | Current conditions grouped by resource |

There is not yet a nodes, audit-read, or tenant-authorized workload-metrics endpoint.
Those are explicit implementation gaps, not undocumented APIs.

### Process endpoints

| Method | Path | Authentication | Success | Response or behavior |
|---|---|---|---|---|
| `GET` | `/healthz` | none | `200` | `{"status":"ok"}` process liveness |
| `GET` | `/metrics` | none in the local stack | `200` | Prometheus exposition |

`/healthz` is not workload-cluster health. Protect `/metrics` before exposing it outside
a trusted production network.

## Request bodies

OpenAPI is the field-by-field schema source. This summary makes writes easy to scan:

| Endpoint | Body |
|---|---|
| Create principal | `issuer`, `external_subject`, `role_id`; optional profile fields and `principal_type` |
| Update principal | Non-null subset of `username`, `display_name`, `email`, `enabled` |
| Create project | `name`, `namespace` |
| Update project | Non-null subset of `name`, `enabled` |
| Add member | `principal_id`, `role_id` |
| Create provider reference | `provider`, `name`, `secret_reference`; optional `configuration` |
| Update provider reference | Non-null subset of `name`, `secret_reference`, `configuration` |
| Create node profile | `provider_reference_id`, `name`, `specification` |
| Replace node profile | `specification` |
| Create cluster | `project_id`, `name`, `management_cluster_id`, `provider_reference_id`, `spec` |
| Replace cluster spec | `spec`; optional `reason` |
| Add/replace worker node type | `worker_node_type` |
| Scale | `pool`, `replicas` (0–1000) |
| Upgrade | `version` in `v1.x.y` or `1.x.y` form |

### Create-cluster example

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
    "worker_node_types": [{
      "name": "general", "role": "worker", "replicas": 3,
      "node_profile": "compute", "labels": {"workload.example/tier": "general"}
    }],
    "scaling": {"autoscaling": false},
    "features": {"audit_logs": true, "metrics": true},
    "machine_health_check": {"enabled": true},
    "addons": [{"name": "workload-cni", "kind": "ConfigMap"}],
    "addon_strategy": "Reconcile"
  }
}
```

Each worker node type compiles to a CAPI `MachineDeployment`, plus its
`KubeadmConfigTemplate` and `OpenStackMachineTemplate`. CAPI owns the corresponding
`MachineSet` lifecycle. The control plane receives a separate machine template. The
executor resolves node profiles into CAPO flavor, image, root-volume,
availability-zone, SSH-key, port, and server-group fields; incomplete profiles fail
compilation. The compiler derives deployment selectors and the reserved CAPI/platform
labels, which callers cannot override. The legacy input key `worker_pools` remains
accepted for stored-revision and client compatibility, but new requests and serialized
specs use `worker_node_types`.

Machine health checks default to enabled with a 10-minute startup timeout and five-minute
`Ready=False`/`Unknown` thresholds. Autoscaling may be global or per worker node type;
enabled deployments receive Cluster Autoscaler bounds and omit `spec.replicas`. Addon
references compile to a `ClusterResourceSet`; referenced ConfigMaps or Secrets must
already exist in the project namespace and contain valid addon manifests.

Scale and upgrade bodies are intentionally small:

```json
{"pool": "general", "replicas": 5}
```

```json
{"version": "v1.32.0"}
```

## Operation polling

An accepted mutation returns an operation rather than a finished cluster:

```json
{
  "id": "7aab2bde-1548-4454-bdc8-467932736c97",
  "cluster_id": "d06a864a-57b3-4a9b-abf2-4b6248e76aec",
  "kind": "CREATE",
  "state": "ACCEPTED",
  "target_revision": 1,
  "created_at": "2026-09-16T12:00:00Z",
  "completed_at": null,
  "failure": null
}
```

Poll `GET /v1/operations/{id}` or list the cluster's operations. The local fake plane
normally advances `ACCEPTED` to `RECONCILING`, then `SUCCEEDED`. Health may remain
`UNKNOWN` until the first observation.

## Errors

| Status | Meaning | Typical cause |
|---|---|---|
| `401` | authentication failed | missing token, or bad signature, issuer, audience, or expiry |
| `403` | forbidden | disabled principal or missing tenant permission |
| `404` | object missing | unknown cluster, operation, project, placement target, or node type |
| `409` | state conflict | duplicate name, object in use, or conflicting mutation |
| `422` | validation failed | malformed JSON, or invalid UUID, version, replicas, name, range, or duplicate node type |

Pydantic errors identify the failing JSON location. Expected domain errors become stable
`403`, `404`, or `409` responses; unexpected faults remain server errors.

## Keeping this reference complete

The implemented source of truth is `src/platform_service/api/routes.py`, together with
process routes and mounts in `src/platform_service/main.py`. Update this reference in
the same change as any route. Inspect the live exact schemas with:

```bash
curl -fsS http://localhost:8000/openapi.json | python -m json.tool
```
