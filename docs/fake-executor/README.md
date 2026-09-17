# Fake executor

The local `executor` service exercises the production command-processing boundary
without connecting to Kubernetes, CAPI, CAPO, or OpenStack. Celery still consumes the
same durable `platform.reconcile` command that a production executor would consume,
and `CommandProcessor` still enforces the same database-backed safety rules. Only the
final management-cluster adapter is fake.

The executor and observer have different responsibilities:

- `workers/tasks.py` consumes a command, invokes `CommandProcessor`, and uses
  `FakeManagementAdapter` to simulate applying the compiled resources;
- `workers/fake_observer.py` later finds the resulting `RECONCILING` operation,
  generates resource observations, and finishes the operation.

The fake adapter deliberately stores no shadow copy of Kubernetes objects. The
authoritative desired revision remains in PostgreSQL, and the fake observer recreates
the local observed-resource snapshot from that revision. See the
[fake observer guide](../fake-observer/README.md) for the status half of the simulation.

## Command path

One accepted mutation follows this executor path:

1. The outbox publisher sends `platform.reconcile` to the direct exchange using the
   management-cluster name as its routing key (`local-mgmt` in Compose).
2. A Celery worker listening to that queue receives the payload. Late acknowledgement,
   rejection on worker loss, and a prefetch multiplier of one support safe
   at-least-once delivery.
3. The task opens a database session and passes the payload to `CommandProcessor` with
   `CapoCompiler`, `FakeManagementAdapter`, and the container hostname as executor ID.
4. The processor rejects an already completed event, rejects a target revision that is
   no longer desired, and acquires or renews the workload cluster's two-minute lease.
5. For ordinary reconciliation it loads the immutable target revision and compilation
   context, then builds the CAPI/CAPO graph. For deletion it calls the adapter with only
   the project namespace and cluster name.
6. `FakeManagementAdapter.apply()` or `.delete()` returns without contacting an external
   service, unless a configured failure mode asks it to raise an error.
7. A successful database commit records the target as `applied_revision`, leaves the
   operation ready for observation in `RECONCILING`, and inserts the event ID into
   `processed_events`.

The payload uses these fields:

| Field | Purpose |
|---|---|
| `event_id` | Durable delivery identity and `processed_events` idempotency key |
| `operation_id` | Operation moved into reconciliation |
| `cluster_id` | Workload cluster locked and leased by the processor |
| `target_revision` | Exact immutable revision that may be applied |

The API service does not execute any of these steps and has no management-cluster
kubeconfig. In the local stack, both the executor and central services can reach the
development database; that convenience is not the intended production network model.

## Delivery and concurrency behavior

Celery may deliver a command more than once. A successfully committed event gets a
`processed_events` row, so redelivery becomes a classified duplicate and the task
returns a safe no-op. A delayed command whose target is older than
`cluster.desired_revision` similarly becomes a classified stale no-op. These checks
happen before provider application.

Leases serialize work **per workload cluster**, not across the whole platform. If a
different executor holds an unexpired lease, the task retries with exponential backoff
capped at 60 seconds. Consequently, scaled executor replicas can process unrelated
clusters concurrently without applying two mutations to one cluster at the same time.
The current local lease duration is two minutes and is updated in the same transaction
as the rest of command processing.

The worker acknowledges tasks late and rejects a task if its worker process is lost.
The queue is durable and configured as a RabbitMQ quorum queue. These settings reduce
message loss, but they do not create exactly-once delivery; the processor's database
checks are the correctness boundary.

## Success does not mean convergence

The fake adapter returning successfully means only that the resource graph was
accepted at the management-adapter boundary. It does not mean that any CAPI resource is
ready. The transaction advances `applied_revision` and commits `RECONCILING`; the fake
observer independently advances `observed_revision` and records `SUCCEEDED` or
`FAILED` after simulating status.

This distinction is useful when testing outages: stopping the observer leaves a
successfully applied operation in `RECONCILING`, while stopping the executor leaves an
accepted command in RabbitMQ or before the application handoff.

## Running and scaling it

Compose starts one executor after bootstrap and RabbitMQ are ready. The same container
also starts the local heartbeat process in the background.

```bash
docker compose ps executor
docker compose logs -f executor
```

Scale the worker pool to exercise queue distribution and per-cluster leases:

```bash
docker compose up -d --scale executor=3
docker compose ps executor
docker compose logs -f executor
```

Return to one replica with:

```bash
docker compose up -d --scale executor=1
```

Each replica reports a heartbeat using its hostname by default. Heartbeat state
describes executor presence and compatibility; it is separate from workload-cluster
health.

## Failure modes

`FakeManagementAdapter` reads `FAKE_FAILURE_MODE` for every apply or delete call:

| Value | Adapter behavior | Celery behavior |
|---|---|---|
| unset or any other value | Returns successfully | Command transaction commits |
| `transient` | Raises `ConnectionError` | Transaction rolls back; task retries up to six times with exponential backoff capped at 300 seconds |
| `permanent` | Raises `ValueError` | Transaction rolls back; task fails without an application retry |

Because the failure occurs before the command transaction commits, neither mode records
`applied_revision` or a processed-event marker. The attempted `RECONCILING` state and
lease update also roll back. The fake executor currently does not persist a terminal
operation failure for adapter rejection, so use Celery/Flower logs to inspect this
exercise; do not confuse it with `FAKE_CAPI_FAILURE`, which belongs to the observer and
does persist a failed convergence result.

To run a temporary failure-enabled worker without changing Compose configuration, stop
the normal replicas and run one foreground replacement:

```bash
docker compose stop executor
docker compose run --rm -e FAKE_FAILURE_MODE=transient executor
```

Stop that foreground worker with `Ctrl-C`, then restore the normal worker:

```bash
docker compose up -d executor
```

Use `FAKE_FAILURE_MODE=permanent` in the same command to exercise non-retryable adapter
rejection. Create a new cluster mutation after the failure-enabled worker is listening;
already completed events remain duplicates and will not call the adapter again.

## Troubleshooting

- **Operation remains `ACCEPTED`:** check `docker compose ps executor outbox rabbitmq`,
  then inspect their logs. Confirm that the outbox event was published and that the
  executor listens on `local-mgmt`.
- **Task repeatedly retries:** look for a lease conflict or `FAKE_FAILURE_MODE=transient`
  in executor logs. Multiple replicas contending for the same cluster should resolve
  after the current transaction or lease expires.
- **Task fails immediately:** check for `FAKE_FAILURE_MODE=permanent`, malformed stored
  data, or an unexpected compiler/database exception. Arbitrary exceptions are not
  treated as transient.
- **Operation remains `RECONCILING`:** executor application already committed; inspect
  the fake observer rather than retrying the command.
- **A redelivered task does nothing:** a duplicate or stale command is intentionally a
  successful no-op. Check `processed_events` and the cluster's desired revision.
- **Failure mode has no effect:** recreate or replace the executor with the environment
  variable set, and submit a new mutation that has not already been processed.

Flower at `http://<PLATFORM_PUBLIC_HOST>:5555` shows task state, and RabbitMQ management
at `http://<PLATFORM_PUBLIC_HOST>:15672` shows the exchange and queue. Both are
development interfaces and must not be exposed publicly with the local configuration.

## Production boundary

`FakeManagementAdapter` validates orchestration, not Kubernetes behavior. It does not
perform server-side apply, resolve secrets, contact a management API, detect field
ownership conflicts, or implement deletion. Its `delete()` method is currently a no-op,
and the application has no deletion command path.

A production executor must run by the target management cluster, implement
`ManagementClusterAdapter` with a least-privilege local ServiceAccount, apply the same
compiled resource graph idempotently, and map provider/API failures into the shared
error policy. Replacing the adapter must not move kubeconfig access into FastAPI or
change the durable command, revision, idempotency, and per-cluster serialization
contracts described above.
