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
    PrincipalView,
    ProjectMembershipCreate,
    ProjectMembershipView,
    ProjectView,
    RoleView,
    ScaleRequest,
    UpgradeRequest,
)
from platform_service.application.cluster_service import ClusterService
from platform_service.application.errors import Conflict, Forbidden, NotFound
from platform_service.application.iam_service import IamService
from platform_service.infrastructure.auth import (
    Identity,
    current_identity,
    db_session,
    project_permissions,
    require_project_permission,
)
from platform_service.infrastructure.database import (
    Cluster,
    ClusterRevision,
    ClusterStatus,
    Operation,
    Principal,
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
    except Conflict as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/principals/me", response_model=PrincipalView)
def principal_me(
    identity: Identity = Depends(current_identity), session: Session = Depends(db_session)
):
    """Return the platform principal mapped from the authenticated OIDC identity."""

    return session.get(Principal, identity.principal_id)


@router.get("/projects", response_model=list[ProjectView])
def projects(
    identity: Identity = Depends(current_identity), session: Session = Depends(db_session)
):
    return IamService(session).visible_projects(identity.principal_id)


def role_view(role, permissions: list[str]) -> RoleView:
    return RoleView(
        id=role.id,
        name=role.name,
        scope=role.scope,
        builtin=role.builtin,
        permissions=permissions,
    )


@router.get("/projects/{project_id}/roles", response_model=list[RoleView])
def project_roles(
    project_id: UUID,
    identity: Identity = Depends(current_identity),
    session: Session = Depends(db_session),
):
    permissions = require_project_permission(
        session, identity.principal_id, project_id, "project.admin"
    )
    return [
        role_view(role, grants) for role, grants in IamService(session).project_roles(permissions)
    ]


@router.get("/projects/{project_id}/members", response_model=list[ProjectMembershipView])
def project_members(
    project_id: UUID,
    identity: Identity = Depends(current_identity),
    session: Session = Depends(db_session),
):
    permissions = require_project_permission(
        session, identity.principal_id, project_id, "project.admin"
    )
    return [
        ProjectMembershipView(
            id=membership.id,
            project_id=membership.project_id,
            principal=PrincipalView.model_validate(principal),
            role=role_view(role, grants),
        )
        for membership, principal, role, grants in IamService(session).project_members(
            project_id=project_id, permissions=permissions
        )
    ]


@router.post(
    "/projects/{project_id}/members",
    response_model=ProjectMembershipView,
    status_code=status.HTTP_201_CREATED,
)
def add_project_member(
    project_id: UUID,
    body: ProjectMembershipCreate,
    request: Request,
    request_id: str = Depends(resolve_request_id),
    identity: Identity = Depends(current_identity),
    session: Session = Depends(db_session),
):
    permissions = project_permissions(session, identity.principal_id, project_id)
    membership = translate_domain_errors(
        lambda: IamService(session).add_project_member(
            project_id=project_id,
            principal_id=body.principal_id,
            role_id=body.role_id,
            permissions=permissions,
            actor_id=identity.principal_id,
            request_id=request_id,
            source_ip=request.client.host if request.client else None,
        )
    )
    principal = session.get(Principal, body.principal_id)
    role, grants = next(
        item
        for item in IamService(session).project_roles(permissions)
        if item[0].id == body.role_id
    )
    return ProjectMembershipView(
        id=membership.id,
        project_id=membership.project_id,
        principal=PrincipalView.model_validate(principal),
        role=role_view(role, grants),
    )


@router.delete("/projects/{project_id}/members/{principal_id}/roles/{role_id}", status_code=204)
def remove_project_member(
    project_id: UUID,
    principal_id: UUID,
    role_id: UUID,
    request: Request,
    request_id: str = Depends(resolve_request_id),
    identity: Identity = Depends(current_identity),
    session: Session = Depends(db_session),
):
    permissions = project_permissions(session, identity.principal_id, project_id)
    translate_domain_errors(
        lambda: IamService(session).remove_project_member(
            project_id=project_id,
            principal_id=principal_id,
            role_id=role_id,
            permissions=permissions,
            actor_id=identity.principal_id,
            request_id=request_id,
            source_ip=request.client.host if request.client else None,
        )
    )


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
