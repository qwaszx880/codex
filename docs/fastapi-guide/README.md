# Hands-on FastAPI guide for this control plane

This guide explains FastAPI by following the application that is actually in this
repository. It is aimed at readers who already know Python and want to understand the
framework choices, important functions, request lifecycle, and surrounding libraries
without first building an unrelated tutorial application.

Start with the root [README](../../README.md) for architecture and local setup. Keep the
[code walkthrough](../code-walkthrough/README.md) open for a module-by-module map. This
guide concentrates on **how** the HTTP application works and **why** each FastAPI
feature is used.

## What you will learn

By tracing the current code you will see how to:

- create and run a FastAPI application;
- group endpoints with an `APIRouter` and a shared URL prefix;
- declare path, query, header, body, and request-object inputs;
- use Pydantic models for validation, serialization, and OpenAPI documentation;
- compose authentication, authorization, configuration, and database lifetimes with
  dependency injection;
- choose response models and HTTP status codes;
- turn expected application errors into HTTP errors;
- keep routes thin and transactions in application services;
- work with synchronous SQLAlchemy safely in synchronous route functions;
- explore and test the API locally.

The guide describes the repository as it exists. It does not suggest that the local
Keycloak credentials or permissive development settings are suitable for production.

## 1. The building blocks at a glance

The project requires Python 3.12 or newer. Its HTTP path uses these main libraries:

| Library | Role here | Representative code |
|---|---|---|
| FastAPI | routing, dependency injection, HTTP errors, response validation, OpenAPI | `main.py`, `api/routes.py` |
| Uvicorn | ASGI server that imports and serves the FastAPI object | `platform_service.main:app` |
| Pydantic | request/response and domain validation | `api/schemas.py`, `domain/spec.py` |
| pydantic-settings | typed environment configuration | `config.py` |
| SQLAlchemy 2 | sessions, mappings, queries, and transactions | `infrastructure/database.py` |
| Psycopg 3 | PostgreSQL driver underneath SQLAlchemy | database URL in `config.py` |
| PyJWT | OIDC JWT verification and JWKS key lookup | `infrastructure/auth.py` |
| Prometheus client | ASGI metrics endpoint | `main.py` |
| Celery and Kombu | asynchronous command transport after database commit | `workers/` |
| Alembic | versioned database schema migrations | `alembic/` |
| httpx | HTTP client dependency and useful ASGI test client | project dependency |
| structlog | structured logging dependency | process/application infrastructure |
| pytest | tests | `tests/` |
| Ruff | linting and formatting | `pyproject.toml` |

Not all of these libraries execute inside an HTTP request. FastAPI accepts intent;
PostgreSQL records it; the outbox publisher and Celery workers perform asynchronous
work later. That separation is the central architectural lesson in this application.

```text
HTTP client
  -> Uvicorn (ASGI server)
  -> FastAPI application
  -> route + dependencies
  -> application service
  -> SQLAlchemy session / PostgreSQL transaction
  -> HTTP 202 with an operation

after commit:
  outbox publisher -> RabbitMQ -> Celery executor -> provider adapter
```

## 2. The composition root: creating the app

Open `src/platform_service/main.py`:

```python
from fastapi import FastAPI
from prometheus_client import make_asgi_app

from platform_service.api.routes import router

app = FastAPI(title="Cluster Control Plane", version="0.1.0")
app.include_router(router, prefix="/v1")
app.mount("/metrics", make_asgi_app())
```

`app` is an ASGI application object. Uvicorn finds it from the conventional
`module:attribute` string:

```bash
uvicorn platform_service.main:app --host 0.0.0.0 --port 8000
```

This string means: import the `platform_service.main` module, then serve its `app`
attribute. Uvicorn owns sockets and the event loop; FastAPI handles ASGI requests.

The constructor metadata appears in generated OpenAPI and Swagger UI. FastAPI exposes
the interactive documentation at `/docs`, alternative ReDoc documentation at `/redoc`,
and the raw schema at `/openapi.json` unless configured otherwise. With the Compose
defaults, the first URL is `http://localhost:8000/docs`.

### Router inclusion versus mounting

These two lines intentionally do different jobs:

```python
app.include_router(router, prefix="/v1")
app.mount("/metrics", make_asgi_app())
```

- `include_router` copies FastAPI route definitions into the main application. The
  `/v1` prefix versions every route in `api/routes.py`, so `@router.get("/projects")`
  becomes `GET /v1/projects`.
- `mount` delegates a whole path subtree to another ASGI application. Prometheus owns
  the metrics response rather than defining ordinary FastAPI route operations.

### A minimal process-level endpoint

The health endpoint is deliberately small:

```python
@app.get("/healthz")
def healthz():
    return {"status": "ok"}
```

The decorator registers the path and HTTP method. The dictionary is serialized to
JSON. This endpoint only demonstrates that the API process can answer; it does not
prove that PostgreSQL, RabbitMQ, executors, or management clusters are healthy.

## 3. Routing: decorators turn functions into operations

`src/platform_service/api/routes.py` creates one router:

```python
router = APIRouter()
```

Functions decorated with `@router.get`, `@router.post`, or `@router.delete` are called
*path operations*. FastAPI inspects their signatures and type annotations to decide
where input comes from and how to validate it.

Consider the cluster list route in shortened form:

```python
@router.get("/clusters", response_model=list[ClusterView])
def clusters(
    project_id: UUID,
    identity: Identity = Depends(current_identity),
    session: Session = Depends(db_session),
):
    ...
```

FastAPI derives four pieces of behavior:

1. The decorator selects `GET /v1/clusters`.
2. `project_id` is not in the path and is not a Pydantic body, so it is a required
   query parameter: `/v1/clusters?project_id=<uuid>`.
3. The `UUID` annotation validates and converts the incoming string before the
   function runs. Bad input produces a `422` response automatically.
4. The two `Depends(...)` defaults ask FastAPI to resolve authentication and a database
   session. Clients do not supply those parameters directly.

`response_model=list[ClusterView]` validates and filters the outgoing value. This is
not merely documentation: only fields declared by `ClusterView` are returned, helping
prevent accidental exposure of persistence fields.

## 4. Where FastAPI gets function arguments

FastAPI uses the decorator, parameter name, type, and default to infer an input source.
This repository contains useful examples of every common source.

### Path parameters

The braces in a route match a function argument:

```python
@router.get("/clusters/{cluster_id}", response_model=ClusterView)
def cluster(
    cluster_id: UUID,
    identity: Identity = Depends(current_identity),
    session: Session = Depends(db_session),
):
    return authorized_cluster(cluster_id, identity, session, "cluster.read")
```

For `/v1/clusters/8a...`, FastAPI converts the path segment into a `UUID`. The annotation
also makes the OpenAPI schema precise.

### Query parameters

As shown by `clusters(project_id: UUID, ...)`, a simple typed value not present in the
path becomes a query parameter. Use this for filtering or pagination-style input, not
for a complex mutation document.

### JSON request bodies

A Pydantic model parameter becomes a JSON body:

```python
@router.post("/clusters", response_model=OperationView, status_code=status.HTTP_202_ACCEPTED)
def create(body: ClusterCreate, ...):
    ...
```

FastAPI parses JSON, validates it as `ClusterCreate`, and only then calls `create`.
The nested `ClusterSpec` is validated too. The handler therefore receives useful
Python objects rather than manually indexing untrusted dictionaries.

### Headers

The request ID dependency explicitly names an HTTP header:

```python
def resolve_request_id(value: str | None = Header(None, alias="X-Request-ID")) -> str:
    return value or __import__("uuid").uuid4().hex
```

`Header` tells FastAPI where to read the value. `alias` preserves the conventional
hyphenated wire name while allowing an ordinary Python identifier. If no value is
sent, the boundary creates a correlation ID used by operations and audit records.

### The raw request object

Annotating a parameter as `Request` asks FastAPI for its request wrapper:

```python
source_ip=request.client.host if request.client else None
```

Use `Request` for metadata that is not naturally a validated business input. Most
code should prefer typed parameters because those are validated and documented.

### Dependencies

A default such as `Depends(current_identity)` means “call this provider and inject its
return value.” Dependencies can have dependencies of their own. Here,
`current_identity` needs the bearer credentials, settings, and the same request-scoped
database session:

```text
route
├── resolve_request_id
├── current_identity
│   ├── HTTPBearer security scheme
│   ├── get_settings
│   └── db_session
└── db_session (reused within this request)
```

FastAPI caches dependency results per request by default. Consequently, repeated use
of the same dependency does not create an unnecessary second value during one request.

## 5. Pydantic models: validation and API contracts

Transport models live in `src/platform_service/api/schemas.py`. For example:

```python
class ScaleRequest(BaseModel):
    pool: str
    replicas: int = Field(ge=0, le=1000)


class UpgradeRequest(BaseModel):
    version: str = Field(pattern=r"^v?1\.\d+\.\d+$")
```

`Field` adds constraints that run before application code:

- replica counts must be between 0 and 1000;
- versions must have the supported Kubernetes version shape;
- cluster names use length limits in `ClusterCreate`.

These constraints also appear in OpenAPI, so documentation and runtime checks share a
source. Pydantic reports all input problems in a structured `422 Unprocessable Entity`
response.

### Request models versus response models

`ClusterCreate` models client intent. `ClusterView` models the public representation:

```python
class ClusterView(BaseModel):
    id: UUID
    project_id: UUID
    name: str
    desired_revision: int
    applied_revision: int | None
    observed_revision: int | None
    model_config = {"from_attributes": True}
```

`from_attributes` lets Pydantic read attributes from a SQLAlchemy `Cluster` instance.
The route can return an ORM object while the HTTP response remains an explicit API
contract. In particular, IDs and revision pointers are exposed, but the model does not
expose internal relationships or provider secret references.

### Transport, domain, and persistence are distinct

There are three kinds of models in this codebase:

1. **API schemas** in `api/schemas.py` define JSON contracts.
2. **Domain specifications** in `domain/spec.py` validate provider-neutral desired
   cluster state and invariants.
3. **SQLAlchemy mappings** in `infrastructure/database.py` define stored records.

They sometimes contain similar fields, but they have different reasons to change.
Avoid turning one enormous model into all three layers.

Useful Pydantic operations seen in the application service are:

```python
validated = ClusterSpec.model_validate(stored_json)
json_compatible_dict = validated.model_dump(mode="json")
```

The first revalidates JSON loaded from a revision; the second emits values suitable
for a JSON database column.

## 6. Dependency injection and resource lifetime

FastAPI dependency injection is ordinary call composition driven from annotations and
`Depends`. It is especially useful for resources that must be created and cleaned up
once per request.

### Database sessions with `yield`

`db_session` is a generator dependency:

```python
def db_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
```

Code before `yield` is setup. The yielded `Session` is injected into downstream
dependencies and the route. The `finally` block runs after request handling, including
when an exception occurs, so the connection is returned to the pool.

Closing is not committing. Mutation use cases call `commit()` only after all intent
records have been added. That explicit transaction boundary is necessary to atomically
persist the revision, operation, audit event, and outbox event.

### Cached application settings

`Settings` extends `pydantic_settings.BaseSettings`:

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PLATFORM_", env_file=".env", extra="ignore"
    )
```

A field such as `database_url` maps to `PLATFORM_DATABASE_URL`. Defaults support the
Compose network. `get_settings()` uses `functools.lru_cache`, giving each process a
consistent settings object rather than reparsing the environment for every request.

When a test modifies environment variables, clear the process cache before resolving
settings again:

```python
get_settings.cache_clear()
```

### Dependency overrides in focused tests

FastAPI supports replacing dependencies without patching global functions. A compact
pattern for a future route test is:

```python
from fastapi.testclient import TestClient

from platform_service.infrastructure.auth import db_session
from platform_service.main import app


def override_db_session():
    yield fake_session


app.dependency_overrides[db_session] = override_db_session
client = TestClient(app)
response = client.get("/v1/clusters", params={"project_id": str(project_id)})
app.dependency_overrides.clear()
```

Authentication also needs an override or a valid local OIDC token. Always clear
overrides in teardown (a fixture with `try/finally` is preferable in real tests) so
tests do not leak state into each other.

## 7. Authentication and authorization

Authentication answers **who is calling**; authorization answers **what that principal
may do**. The repository deliberately keeps both steps visible.

### Bearer-token extraction

```python
security = HTTPBearer(auto_error=True)
```

Used through `Depends(security)`, this extracts the `Authorization: Bearer ...` header
and describes bearer authentication in OpenAPI. Missing or malformed credentials are
rejected before the route runs.

### OIDC token validation

`current_identity`:

1. obtains the issuer's signing key through JWKS;
2. verifies the JWT signature using allowed asymmetric algorithms;
3. verifies audience, issuer, expiry, and required claims;
4. maps `(issuer, subject)` to a persisted principal;
5. rejects disabled principals;
6. updates login metadata and returns an immutable `Identity` dataclass.

It returns `401` for an invalid identity token and `403` for a known but disabled
principal. The public API intentionally does not offer an authentication bypass even
when an internal setting is enabled.

### Project-scoped authorization

Routes resolve effective permissions from project and organization memberships.
`authorized_cluster` first loads the cluster to identify its owning project, then
requires a permission such as `cluster.read`, `cluster.scale`, or `cluster.upgrade`.

The application service checks mutation permissions again. This is useful defense in
depth: calling `ClusterService` from a non-HTTP adapter must not bypass the use-case
authorization rule.

## 8. Status codes, response models, and errors

Use status codes to communicate the actual lifecycle rather than defaulting every
successful mutation to `200`.

```python
@router.post(
    "/clusters",
    response_model=OperationView,
    status_code=status.HTTP_202_ACCEPTED,
)
```

Cluster creation returns `202 Accepted`, not `201 Created`, because the API has durably
accepted asynchronous work but the management plane has not necessarily converged.
The response is an `OperationView` clients can poll. Membership creation uses
`201 Created`; membership deletion uses `204 No Content`.

### `HTTPException`

Expected HTTP failures are raised, not returned:

```python
raise HTTPException(403, "missing permission: cluster.read")
```

FastAPI stops the operation and serializes a response with the requested status.
Input validation errors are handled automatically.

Application services do not import FastAPI. They raise `NotFound`, `Forbidden`, or
`Conflict`, and the HTTP adapter translates them:

```python
def translate_domain_errors(call):
    try:
        return call()
    except NotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except Forbidden as exc:
        raise HTTPException(403, f"missing permission: {exc}") from exc
    except Conflict as exc:
        raise HTTPException(409, str(exc)) from exc
```

Unexpected exceptions are intentionally not swallowed. Central logging/middleware can
record them and FastAPI can produce a server error, while programming faults remain
visible. A larger application may replace this helper with registered exception
handlers, but the separation principle remains the same.

## 9. Why these route functions are synchronous

Most routes use `def`, not `async def`, and SQLAlchemy uses a synchronous `Session`.
FastAPI runs synchronous path operations in a thread pool so blocking database calls do
not directly block the event loop.

Do not mechanically change a handler to `async def` while leaving synchronous database
or network calls inside it. That would block the event loop. Adopt SQLAlchemy's async
engine/session throughout a path if asynchronous database I/O is genuinely needed.
For this service, consistency and simple transaction boundaries matter more than an
unnecessary mix of sync and async APIs.

## 10. Thin routes and the application-service boundary

The create route performs HTTP-boundary work and delegates the use case:

```python
permissions = project_permissions(session, identity.principal_id, body.project_id)
_, operation = translate_domain_errors(
    lambda: ClusterService(session).create(
        project_id=body.project_id,
        name=body.name,
        management_cluster_id=body.management_cluster_id,
        provider_reference_id=body.provider_reference_id,
        spec=body.spec,
        actor_id=identity.principal_id,
        permissions=permissions,
        request_id=request_id,
        source_ip=request.client.host if request.client else None,
    )
)
return operation
```

The route owns HTTP concepts: body parsing, injected identity, source IP, response
model, and error translation. `ClusterService.create` owns the business transaction.
It validates referenced records, creates the cluster and first immutable revision,
constructs operation/audit/outbox intent, commits once, and returns the operation.

This boundary gives other adapters a reusable use case and prevents HTTP handlers from
becoming transaction scripts.

### The transactional outbox is part of the FastAPI design

The route never publishes directly to RabbitMQ. Publishing before commit could send a
command for state that later rolls back; publishing after commit in the request could
lose the command if the process crashes between those actions.

Instead, one transaction stores:

- the desired cluster revision;
- the user-visible operation;
- the audit record;
- an unpublished outbox event.

The outbox process publishes later. The HTTP response therefore means “intent is
durable,” while executors tolerate at-least-once delivery. The central FastAPI process
also never receives a management-cluster kubeconfig or calls Kubernetes.

## 11. SQLAlchemy usage you meet from the routes

The project uses SQLAlchemy 2-style APIs:

```python
cluster = session.get(Cluster, cluster_id)

clusters = session.scalars(
    select(Cluster).where(
        Cluster.project_id == project_id,
        Cluster.deleted_at.is_(None),
    )
).all()
```

- `session.get(Model, primary_key)` is the direct primary-key lookup.
- `select(Model)` constructs a typed query.
- `session.scalars(...)` returns model instances rather than row tuples.
- `.all()` materializes the result collection.

Mutation services also use `with_for_update()` when advancing a revision. The row lock
prevents concurrent writers from choosing the same next revision number. Mappings use
modern `Mapped[...]` annotations and `mapped_column(...)` definitions.

Schema creation does **not** happen when FastAPI starts. Alembic migrations own schema
changes, and Compose runs the migration service before normal application use.

## 12. A complete request walkthrough

Follow `POST /v1/clusters/{cluster_id}/scale`:

1. Uvicorn receives the request and invokes the FastAPI ASGI app.
2. FastAPI matches the route and converts `cluster_id` to `UUID`.
3. Pydantic parses the body into `ScaleRequest`; `replicas` outside 0–1000 fails early.
4. `resolve_request_id` preserves `X-Request-ID` or generates one.
5. `current_identity` validates the bearer token and resolves a principal.
6. `db_session` supplies a session and guarantees cleanup.
7. `authorized_cluster` checks `cluster.scale` in the owning project.
8. `ClusterService.scale` loads and validates the latest desired spec, changes the
   selected worker node type, and delegates revision creation to `revise`.
9. `revise` locks the cluster, creates the new immutable revision and intent records,
   then commits the transaction.
10. FastAPI converts the returned SQLAlchemy operation through `OperationView` and
    sends `202` JSON.
11. After the response, the session dependency closes the session.
12. Independently, the outbox publisher and executor reconcile that exact revision.

Notice what is absent: the HTTP request does not wait for OpenStack or Kubernetes. A
client follows the operation and health endpoints to observe progress.

## 13. Hands-on local exploration

### Start the stack

From the repository root:

```bash
docker compose up --build
```

Wait for migrations and bootstrap to finish, then check the process endpoint:

```bash
curl -sS http://localhost:8000/healthz
```

Expected response:

```json
{"status":"ok"}
```

Open `http://localhost:8000/docs` to inspect operations, schemas, parameter locations,
status codes, and bearer authentication. The root README contains the current Keycloak
token command and seeded IDs; use that canonical workflow rather than copying local
credentials into scripts.

### Inspect OpenAPI directly

```bash
curl -sS http://localhost:8000/openapi.json \
  | python -m json.tool \
  | less
```

Look for `/v1/clusters`, `ClusterCreate`, and `OperationView`. Relate every schema back
to `api/schemas.py` and every operation back to `api/routes.py`.

### Exercise validation before business logic

With a valid token in `TOKEN`, an invalid version demonstrates request validation:

```bash
curl -i -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"version":"not-a-kubernetes-version"}' \
  http://localhost:8000/v1/clusters/00000000-0000-0000-0000-000000000000/upgrade
```

The body fails `UpgradeRequest` validation. Try an invalid UUID in the URL too and
compare the structured `422` details.

### Follow correlation data

For a valid mutation, provide your own request ID:

```bash
-H "X-Request-ID: tutorial-scale-001"
```

Then inspect the returned operation and the database audit/outbox records using the
local tools described in the root README. This connects the HTTP header dependency to
durable operational data.

## 14. A practical recipe for adding an endpoint

Suppose a new use case is required. Work from the inside out:

1. **Confirm the boundary.** Decide whether this is identity, desired state,
   reconciliation, or observed state. Do not make FastAPI call a management Kubernetes
   API.
2. **Define domain behavior.** Add provider-neutral validation or a port where needed.
3. **Add the application use case.** Put authorization and transaction orchestration
   in an application service, not in the route.
4. **Add persistence via Alembic.** If storage changes, create a migration; never call
   `metadata.create_all()` on startup.
5. **Define API schemas.** Add explicit request and response models in
   `api/schemas.py`. Avoid returning unrestricted ORM objects.
6. **Add the route.** Parse inputs, inject dependencies, call the service, and translate
   expected errors.
7. **Choose semantics.** Select an honest status code (`202` for accepted async work),
   and make tenant authorization explicit.
8. **Test the narrowest layer.** Unit-test domain/application behavior, then add route
   coverage for parameter binding, auth, status, and response shape.
9. **Update documentation.** Keep the root workflow and code walkthrough accurate.

A deliberately generic route shape is:

```python
@router.post(
    "/clusters/{cluster_id}/example-action",
    response_model=OperationView,
    status_code=status.HTTP_202_ACCEPTED,
)
def example_action(
    cluster_id: UUID,
    body: ExampleRequest,
    request_id: str = Depends(resolve_request_id),
    identity: Identity = Depends(current_identity),
    session: Session = Depends(db_session),
):
    cluster = authorized_cluster(cluster_id, identity, session, "cluster.example")
    return translate_domain_errors(
        lambda: ExampleService(session).run(
            cluster_id=cluster.id,
            body=body,
            actor_id=identity.principal_id,
            permissions=project_permissions(
                session, identity.principal_id, cluster.project_id
            ),
            request_id=request_id,
        )
    )
```

This is a shape, not a request to create `ExampleService`: use names and permissions
from the actual domain.

## 15. Common mistakes in this codebase

- **Publishing from a route.** It breaks the atomic outbox guarantee.
- **Calling Kubernetes from FastAPI.** The central service must not have management
  credentials; use executor-side adapters.
- **Putting orchestration in a route.** Routes should remain HTTP adapters.
- **Returning an unrestricted ORM object.** Declare a response model to constrain the
  public contract.
- **Confusing `yield` cleanup with commit.** Session cleanup does not define a valid
  business transaction.
- **Using blocking I/O inside `async def`.** Stay synchronous with the current sync
  SQLAlchemy stack or migrate the entire I/O path deliberately.
- **Trusting only UI authorization.** Every API path must authenticate and enforce
  tenant-scoped permissions.
- **Treating `202` as completion.** It means accepted; inspect the operation and
  observed health.
- **Changing tables without Alembic.** Runtime model declarations are not migration
  history.
- **Storing provider credentials.** Store and expose secret references only.
- **Overriding dependencies without cleanup in tests.** Overrides are application
  state and can contaminate later tests.

## 16. Suggested reading path through the code

For a focused two-hour tour:

1. `src/platform_service/main.py` — composition, versioned router, metrics, health.
2. `src/platform_service/api/schemas.py` — public request/response contracts.
3. `src/platform_service/api/routes.py` — parameter binding and thin route patterns.
4. `src/platform_service/infrastructure/auth.py` — nested dependencies, OIDC, session
   lifetime, and RBAC.
5. `src/platform_service/application/cluster_service.py` — transaction and outbox
   orchestration behind the route.
6. `src/platform_service/domain/spec.py` — richer Pydantic domain validation.
7. `src/platform_service/infrastructure/database.py` — SQLAlchemy mappings and session
   factory.
8. `src/platform_service/workers/outbox.py` and `workers/tasks.py` — what happens after
   an HTTP `202`.
9. `tests/` — examples of testing services and contracts without starting the stack.

Then use the [full code walkthrough](../code-walkthrough/README.md) to explore the
compiler, executor, observer, IAM service, migrations, and tests in greater depth.

## 17. Commands to run before committing

The repository's standard checks are:

```bash
ruff check .
ruff format --check .
pytest -q
git diff --check
```

Run `docker compose config` as well when Compose configuration changes. Documentation
examples should remain consistent with the local stack, but local credentials and
permissive identity-provider settings must never be presented as production defaults.
