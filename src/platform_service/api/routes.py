"""HTTP adapters for cluster lifecycle and observed-state queries.

Routes authenticate and authorize requests, then delegate mutations to the
application service.  They deliberately do not publish messages or talk to a
management-cluster Kubernetes API.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from platform_service.api.schemas import (
    ClusterCreate,
    ClusterView,
    OperationView,
    ScaleRequest,
    UpgradeRequest,
)
from platform_service.application.cluster_service import ClusterService, Forbidden, NotFound
from platform_service.infrastructure.auth import (
    Identity,
    current_identity,
    db_session,
    project_permissions,
)
from platform_service.infrastructure.database import (
    Cluster,
    ClusterRevision,
    ClusterStatus,
    Operation,
    ResourceStatus,
)

router = APIRouter()


def resolve_request_id(value: str | None = Header(None, alias="X-Request-ID")) -> str:
    """Preserve a caller's correlation ID or create one at the HTTP boundary."""

    return value or __import__("uuid").uuid4().hex


def translate_domain_errors(call):
    """Convert expected application errors into stable HTTP responses."""

    try:
        return call()
    except NotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except Forbidden as exc:
        raise HTTPException(403, f"missing permission: {exc}") from exc


@router.post("/clusters", response_model=OperationView, status_code=status.HTTP_202_ACCEPTED)
def create(
    body: ClusterCreate,
    request: Request,
    request_id: str = Depends(resolve_request_id),
    identity: Identity = Depends(current_identity),
    session: Session = Depends(db_session),
):
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


@router.get("/clusters", response_model=list[ClusterView])
def clusters(
    project_id: UUID,
    identity: Identity = Depends(current_identity),
    session: Session = Depends(db_session),
):
    if "cluster.read" not in project_permissions(session, identity.principal_id, project_id):
        raise HTTPException(403, "missing permission: cluster.read")
    return session.scalars(
        select(Cluster).where(Cluster.project_id == project_id, Cluster.deleted_at.is_(None))
    ).all()


def authorized_cluster(
    cluster_id: UUID, identity: Identity, session: Session, permission: str
) -> Cluster:
    """Load a cluster only after checking access in its owning project."""

    cluster = session.get(Cluster, cluster_id)
    if not cluster:
        raise HTTPException(404, "cluster")
    if permission not in project_permissions(session, identity.principal_id, cluster.project_id):
        raise HTTPException(403, f"missing permission: {permission}")
    return cluster


@router.get("/clusters/{cluster_id}", response_model=ClusterView)
def cluster(
    cluster_id: UUID,
    identity: Identity = Depends(current_identity),
    session: Session = Depends(db_session),
):
    return authorized_cluster(cluster_id, identity, session, "cluster.read")


@router.post("/clusters/{cluster_id}/scale", response_model=OperationView, status_code=202)
def scale(
    cluster_id: UUID,
    body: ScaleRequest,
    request: Request,
    request_id: str = Depends(resolve_request_id),
    identity: Identity = Depends(current_identity),
    session: Session = Depends(db_session),
):
    cluster = authorized_cluster(cluster_id, identity, session, "cluster.scale")
    _, operation = translate_domain_errors(
        lambda: ClusterService(session).scale(
            cluster_id=cluster_id,
            pool=body.pool,
            replicas=body.replicas,
            actor_id=identity.principal_id,
            permissions=project_permissions(session, identity.principal_id, cluster.project_id),
            request_id=request_id,
            source_ip=request.client.host if request.client else None,
        )
    )
    return operation


@router.post("/clusters/{cluster_id}/upgrade", response_model=OperationView, status_code=202)
def upgrade(
    cluster_id: UUID,
    body: UpgradeRequest,
    request_id: str = Depends(resolve_request_id),
    identity: Identity = Depends(current_identity),
    session: Session = Depends(db_session),
):
    cluster = authorized_cluster(cluster_id, identity, session, "cluster.upgrade")
    _, operation = translate_domain_errors(
        lambda: ClusterService(session).upgrade(
            cluster_id=cluster_id,
            version=body.version,
            actor_id=identity.principal_id,
            permissions=project_permissions(session, identity.principal_id, cluster.project_id),
            request_id=request_id,
        )
    )
    return operation


@router.get("/clusters/{cluster_id}/revisions")
def revisions(
    cluster_id: UUID,
    identity: Identity = Depends(current_identity),
    session: Session = Depends(db_session),
):
    authorized_cluster(cluster_id, identity, session, "cluster.read")
    return session.scalars(
        select(ClusterRevision)
        .where(ClusterRevision.cluster_id == cluster_id)
        .order_by(ClusterRevision.number)
    ).all()


@router.get("/clusters/{cluster_id}/operations", response_model=list[OperationView])
def operations(
    cluster_id: UUID,
    identity: Identity = Depends(current_identity),
    session: Session = Depends(db_session),
):
    authorized_cluster(cluster_id, identity, session, "cluster.read")
    return session.scalars(
        select(Operation)
        .where(Operation.cluster_id == cluster_id)
        .order_by(Operation.created_at.desc())
    ).all()


@router.get("/operations/{operation_id}", response_model=OperationView)
def operation(
    operation_id: UUID,
    identity: Identity = Depends(current_identity),
    session: Session = Depends(db_session),
):
    op = session.get(Operation, operation_id)
    if not op:
        raise HTTPException(404, "operation")
    authorized_cluster(op.cluster_id, identity, session, "cluster.read")
    return op


@router.get("/clusters/{cluster_id}/health")
def health(
    cluster_id: UUID,
    identity: Identity = Depends(current_identity),
    session: Session = Depends(db_session),
):
    authorized_cluster(cluster_id, identity, session, "cluster.read")
    value = session.get(ClusterStatus, cluster_id)
    return value.health if value else {"management_connectivity": "UNKNOWN", "status": "UNKNOWN"}


@router.get("/clusters/{cluster_id}/resources")
def resources(
    cluster_id: UUID,
    identity: Identity = Depends(current_identity),
    session: Session = Depends(db_session),
):
    authorized_cluster(cluster_id, identity, session, "cluster.read")
    return session.scalars(
        select(ResourceStatus).where(ResourceStatus.cluster_id == cluster_id)
    ).all()


@router.get("/clusters/{cluster_id}/conditions")
def conditions(
    cluster_id: UUID,
    identity: Identity = Depends(current_identity),
    session: Session = Depends(db_session),
):
    return [
        {"resource": r.name, "kind": r.kind, "conditions": r.conditions}
        for r in resources(cluster_id, identity, session)
    ]
