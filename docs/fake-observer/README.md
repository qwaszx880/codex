# Fake observer

`src/platform_service/workers/fake_observer.py` is the local status side of the
simulated management plane. It lets the Compose stack demonstrate observed resources,
health, and operation completion without Kubernetes, CAPI, CAPO, or OpenStack.

It is separate from the fake executor adapter:

- `workers/tasks.py` receives a durable command, compiles and "applies" desired
  resources, records `applied_revision`, and changes the operation to `RECONCILING`;
- `workers/fake_observer.py` notices that state later, constructs an observation, sets
  `observed_revision`, and changes the operation to `SUCCEEDED` or `FAILED`.

The [fake executor guide](../fake-executor/README.md) documents the command and
application half of this local management-plane simulation.

This separation keeps application intent, resource application, and observed health on
different timelines, even though the local simulation usually converges in seconds.

## How one poll works

`observe_once()` performs one database transaction:

1. Read `FAKE_CAPI_FAILURE`. The values `1`, `true`, and `yes` (case-insensitive)
   select the failure snapshot; every other value selects success.
2. Select every operation whose current state is `RECONCILING`, using
   `FOR UPDATE SKIP LOCKED`. Locked rows are skipped, so concurrent observer processes
   do not handle the same operation at the same time.
3. For a successful deletion, remove the simulated observations and normalized health,
   set the cluster tombstone, advance `observed_revision`, and complete a `deletion`
   stage. In failure mode, retain the cluster/observations and fail that stage.
4. For other operations, load the exact target revision, provider reference, and node
   profiles, validate `ClusterSpec`, and run `CapoCompiler` with the executor's context.
5. Add simulated resources that CAPI would normally create from the compiled worker
   declarations.
6. Upsert the current resource snapshot, normalized cluster health, revision pointer,
   terminal operation result, and convergence stage.
7. Commit all selected operations together when the context-managed transaction exits.

`main()` repeats this poll forever. It sleeps for `FAKE_OBSERVER_INTERVAL` seconds after
each poll; the default is `1`.

The observer does **not** consume RabbitMQ commands. `RECONCILING` in PostgreSQL is its
handoff signal, so it does not define a second command or desired-state contract.

## Resources it reports

The compiler supplies the declarative resources:

- one `Cluster`, `OpenStackCluster`, and `KubeadmControlPlane`;
- one control-plane `OpenStackMachineTemplate`;
- per worker node type: `KubeadmConfigTemplate`, `OpenStackMachineTemplate`, and
  `MachineDeployment`;
- control-plane and per-worker `MachineHealthCheck` objects when enabled;
- one `ClusterResourceSet` when addons are configured.

The observer additionally synthesizes one `MachineSet` per worker node type and one
`Machine` plus `OpenStackMachine` pair per requested worker replica. These extra objects
represent resources normally owned by CAPI; they are observations only and are not
compiler output. The simulator currently does not create control-plane `Machine` or
`OpenStackMachine` instances.

Every resource receives a deterministic UUIDv5 derived from cluster ID, kind, and name.
This makes the same logical resource update its existing `resource_status` row on later
revisions instead of creating a duplicate. The simulator uses fixed generation,
observed generation, and resource version values of `1`; it is not modeling Kubernetes
resource-version or generation progression.

## Persisted status and operation result

For a resource seen for the first time, the observer stores:

- kind, namespace, name, and deterministic UID;
- generation, observed generation, and resource version set to `1`;
- one synthetic `Ready` condition;
- raw status containing `ready` and `simulated`;
- one matching `condition_history` row.

For an existing resource, it replaces the latest conditions and raw status and refreshes
`observed_at`. It currently does **not** append a new condition-history row, increment
generation/resource version, or delete status rows for resources absent from a later
revision. Those are explicit simulation limitations, not production observer behavior.
An explicit cluster deletion is different: it removes condition history before resource
rows, removes normalized health, and retains the cluster, revisions, operation, audit,
and outbox history behind a `deleted_at` tombstone.

The cluster-level projection is independent of those raw rows:

| Field | Success poll | Failure poll |
|---|---|---|
| infrastructure | `READY` | `FAILED` |
| control plane | `READY` | `FAILED` |
| workers / machines | `READY` | `DEGRADED` |
| availability | `AVAILABLE` | `UNAVAILABLE` |
| remediating | `false` | `true` |
| management connectivity | `CONNECTED` | `CONNECTED` |

Both modes set scaling, rollout, deletion, and pause flags to `false`. The observer then
sets `cluster.observed_revision` to the operation's target revision, adds a terminal
`convergence` stage, and completes the operation as `SUCCEEDED` or `FAILED`. A failed
operation receives a permanent `SimulatedFailure` payload.

Because only `RECONCILING` operations are selected, a completed operation is not
rewritten by later polls. A later scale or upgrade creates another operation and can
update current resource/cluster health without changing the earlier operation result.

## Running and configuring it

Compose starts it as the `fake-observer` service after local bootstrap:

```bash
docker compose logs -f fake-observer
docker compose restart fake-observer
```

To make the next reconciling operations fail, stop the normal service and run a
foreground replacement:

```bash
docker compose stop fake-observer
docker compose run --rm \
  -e FAKE_CAPI_FAILURE=true \
  -e FAKE_OBSERVER_INTERVAL=1 \
  fake-observer
```

Stop the foreground process with `Ctrl-C`, then restore normal success behavior:

```bash
docker compose up -d fake-observer
```

The failure flag affects operations that are `RECONCILING` when the failure-enabled
process polls. It does not retroactively modify terminal operations. If the regular
observer completes an operation first, create a new scale or upgrade operation to test
the failure path.

## Troubleshooting

- **Operation remains `ACCEPTED`:** inspect the outbox and executor first. The observer
  intentionally ignores operations until the executor changes them to `RECONCILING`.
- **Operation remains `RECONCILING`:** check `docker compose ps fake-observer` and its
  logs. An unhandled compilation/database error rolls back the poll; Compose restarts
  the process under its `unless-stopped` policy.
- **Failure test unexpectedly succeeds:** the success observer probably polled first.
  Stop it before creating the operation or before it reaches `RECONCILING`.
- **Old resource rows remain after scaling down:** stale-row deletion is not implemented
  by this simulator. Treat the resources endpoint as a local snapshot approximation.
- **No new condition-history row appears after an upgrade:** only the first observation
  of a deterministic resource UID is currently recorded in history.

## Production boundary

This worker has direct access to the central PostgreSQL database and never contacts a
Kubernetes API. It is suitable only for the local reference stack. A production
management-side observer must watch/list CAPI and CAPO resources with its local scoped
ServiceAccount, handle watch restarts and periodic resync, preserve meaningful condition
transitions, and report observations through an authenticated boundary. None of those
requirements justify giving the central FastAPI service a management-cluster kubeconfig.
