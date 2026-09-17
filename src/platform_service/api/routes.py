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
    ClusterUpdate,
    ClusterView,
    ManagementClusterView,
    NodeProfileCreate,
    NodeProfileUpdate,
    NodeProfileView,
    OperationView,
    OrganizationView,
    PrincipalCreate,
    PrincipalUpdate,
    PrincipalView,
    ProviderReferenceCreate,
    ProviderReferenceUpdate,
    ProviderReferenceView,
    ProjectCreate,
    ProjectMembershipCreate,
    ProjectMembershipView,
    ProjectUpdate,
    ProjectView,
    RoleView,
    ScaleRequest,
    UpgradeRequest,
    WorkerNodeTypeCreate,
)
from platform_service.application.admin_service import AdminService
from platform_service.application.cluster_service import ClusterService
from platform_service.application.errors import Conflict, Forbidden, NotFound
from platform_service.application.iam_service import IamService
from platform_service.infrastructure.auth import (
    Identity,
    current_identity,
    db_session,
    project_permissions,
    require_organization_permission,
    require_project_permission,
)
from platform_service.infrastructure.database import (
    Cluster,
    ClusterRevision,
    ClusterStatus,
    ManagementCluster,
    NodeProfile,
    Operation,
    Organization,
    OrganizationMembership,
    Principal,
    Project,
    ProviderReference,
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


@router.get("/organizations", response_model=list[OrganizationView])
def organizations(
    identity: Identity = Depends(current_identity), session: Session = Depends(db_session)
):
    """List organizations in which the principal has an organization role."""

    return session.scalars(
        select(Organization)
        .join(
            OrganizationMembership,
            OrganizationMembership.organization_id == Organization.id,
        )
        .where(OrganizationMembership.principal_id == identity.principal_id)
        .distinct()
        .order_by(Organization.name)
    ).all()


@router.get("/organizations/{organization_id}/principals", response_model=list[PrincipalView])
def organization_principals(
    organization_id: UUID,
    identity: Identity = Depends(current_identity),
    session: Session = Depends(db_session),
):
    require_organization_permission(
        session, identity.principal_id, organization_id, "project.admin"
    )
    return session.scalars(
        select(Principal)
        .join(
            OrganizationMembership,
            OrganizationMembership.principal_id == Principal.id,
        )
        .where(OrganizationMembership.organization_id == organization_id)
        .distinct()
        .order_by(Principal.username, Principal.id)
    ).all()


@router.get("/organizations/{organization_id}/roles", response_model=list[RoleView])
def organization_roles(
    organization_id: UUID,
    identity: Identity = Depends(current_identity),
    session: Session = Depends(db_session),
):
    permissions = require_organization_permission(
        session, identity.principal_id, organization_id, "project.admin"
    )
    return [
        role_view(role, grants)
        for role, grants in IamService(session).organization_roles(permissions)
    ]


@router.post(
    "/organizations/{organization_id}/principals",
    response_model=PrincipalView,
    status_code=status.HTTP_201_CREATED,
)
def create_principal(
    organization_id: UUID,
    body: PrincipalCreate,
    request: Request,
    request_id: str = Depends(resolve_request_id),
    identity: Identity = Depends(current_identity),
    session: Session = Depends(db_session),
):
    permissions = require_organization_permission(
        session, identity.principal_id, organization_id, "project.admin"
    )
    return translate_domain_errors(
        lambda: AdminService(session).create_principal(
            organization_id=organization_id,
            permissions=permissions,
            actor_id=identity.principal_id,
            request_id=request_id,
            source_ip=request.client.host if request.client else None,
            **body.model_dump(),
        )
    )


@router.patch(
    "/organizations/{organization_id}/principals/{principal_id}",
    response_model=PrincipalView,
)
def update_principal(
    organization_id: UUID,
    principal_id: UUID,
    body: PrincipalUpdate,
    request: Request,
    request_id: str = Depends(resolve_request_id),
    identity: Identity = Depends(current_identity),
    session: Session = Depends(db_session),
):
    permissions = require_organization_permission(
        session, identity.principal_id, organization_id, "project.admin"
    )
    return translate_domain_errors(
        lambda: AdminService(session).update_principal(
            organization_id=organization_id,
            principal_id=principal_id,
            changes=body.model_dump(exclude_unset=True, exclude_none=True),
            permissions=permissions,
            actor_id=identity.principal_id,
            request_id=request_id,
            source_ip=request.client.host if request.client else None,
        )
    )


@router.get("/organizations/{organization_id}/projects", response_model=list[ProjectView])
def organization_projects(
    organization_id: UUID,
    identity: Identity = Depends(current_identity),
    session: Session = Depends(db_session),
):
    require_organization_permission(
        session, identity.principal_id, organization_id, "project.admin"
    )
    return session.scalars(
        select(Project).where(Project.organization_id == organization_id).order_by(Project.name)
    ).all()


@router.post(
    "/organizations/{organization_id}/projects",
    response_model=ProjectView,
    status_code=status.HTTP_201_CREATED,
)
def create_project(
    organization_id: UUID,
    body: ProjectCreate,
    request: Request,
    request_id: str = Depends(resolve_request_id),
    identity: Identity = Depends(current_identity),
    session: Session = Depends(db_session),
):
    permissions = require_organization_permission(
        session, identity.principal_id, organization_id, "project.admin"
    )
    return translate_domain_errors(
        lambda: AdminService(session).create_project(
            organization_id=organization_id,
            permissions=permissions,
            actor_id=identity.principal_id,
            request_id=request_id,
            source_ip=request.client.host if request.client else None,
            **body.model_dump(),
        )
    )


@router.get("/projects", response_model=list[ProjectView])
def projects(
    identity: Identity = Depends(current_identity), session: Session = Depends(db_session)
):
    return IamService(session).visible_projects(identity.principal_id)


@router.patch("/projects/{project_id}", response_model=ProjectView)
def update_project(
    project_id: UUID,
    body: ProjectUpdate,
    request: Request,
    request_id: str = Depends(resolve_request_id),
    identity: Identity = Depends(current_identity),
    session: Session = Depends(db_session),
):
    permissions = require_project_permission(
        session, identity.principal_id, project_id, "project.admin"
    )
    return translate_domain_errors(
        lambda: AdminService(session).update_project(
            project_id=project_id,
            changes=body.model_dump(exclude_unset=True, exclude_none=True),
            permissions=permissions,
            actor_id=identity.principal_id,
            request_id=request_id,
            source_ip=request.client.host if request.client else None,
        )
    )


@router.get("/projects/{project_id}/node-profiles", response_model=list[NodeProfileView])
def node_profiles(
    project_id: UUID,
    identity: Identity = Depends(current_identity),
    session: Session = Depends(db_session),
):
    require_project_permission(session, identity.principal_id, project_id, "cluster.read")
    return session.scalars(
        select(NodeProfile).where(NodeProfile.project_id == project_id).order_by(NodeProfile.name)
    ).all()


@router.get(
    "/projects/{project_id}/provider-references",
    response_model=list[ProviderReferenceView],
)
def provider_references(
    project_id: UUID,
    identity: Identity = Depends(current_identity),
    session: Session = Depends(db_session),
):
    require_project_permission(session, identity.principal_id, project_id, "cluster.read")
    return session.scalars(
        select(ProviderReference)
        .where(ProviderReference.project_id == project_id)
        .order_by(ProviderReference.name)
    ).all()


@router.post(
    "/projects/{project_id}/provider-references",
    response_model=ProviderReferenceView,
    status_code=status.HTTP_201_CREATED,
)
def create_provider_reference(
    project_id: UUID,
    body: ProviderReferenceCreate,
    request: Request,
    request_id: str = Depends(resolve_request_id),
    identity: Identity = Depends(current_identity),
    session: Session = Depends(db_session),
):
    permissions = require_project_permission(
        session, identity.principal_id, project_id, "provider.configure"
    )
    return translate_domain_errors(
        lambda: AdminService(session).create_provider_reference(
            project_id=project_id,
            permissions=permissions,
            actor_id=identity.principal_id,
            request_id=request_id,
            source_ip=request.client.host if request.client else None,
            **body.model_dump(),
        )
    )


@router.patch(
    "/projects/{project_id}/provider-references/{reference_id}",
    response_model=ProviderReferenceView,
)
def update_provider_reference(
    project_id: UUID,
    reference_id: UUID,
    body: ProviderReferenceUpdate,
    request: Request,
    request_id: str = Depends(resolve_request_id),
    identity: Identity = Depends(current_identity),
    session: Session = Depends(db_session),
):
    permissions = require_project_permission(
        session, identity.principal_id, project_id, "provider.configure"
    )
    return translate_domain_errors(
        lambda: AdminService(session).update_provider_reference(
            project_id=project_id,
            reference_id=reference_id,
            changes=body.model_dump(exclude_unset=True, exclude_none=True),
            permissions=permissions,
            actor_id=identity.principal_id,
            request_id=request_id,
            source_ip=request.client.host if request.client else None,
        )
    )


@router.delete("/projects/{project_id}/provider-references/{reference_id}", status_code=204)
def delete_provider_reference(
    project_id: UUID,
    reference_id: UUID,
    request: Request,
    request_id: str = Depends(resolve_request_id),
    identity: Identity = Depends(current_identity),
    session: Session = Depends(db_session),
):
    permissions = require_project_permission(
        session, identity.principal_id, project_id, "provider.configure"
    )
    translate_domain_errors(
        lambda: AdminService(session).delete_provider_reference(
            project_id=project_id,
            reference_id=reference_id,
            permissions=permissions,
            actor_id=identity.principal_id,
            request_id=request_id,
            source_ip=request.client.host if request.client else None,
        )
    )


@router.get("/management-clusters", response_model=list[ManagementClusterView])
def management_clusters(
    identity: Identity = Depends(current_identity),
    session: Session = Depends(db_session),
):
    """List enabled placement targets for authenticated cluster forms."""

    del identity
    return session.scalars(
        select(ManagementCluster)
        .where(ManagementCluster.enabled.is_(True))
        .order_by(ManagementCluster.name)
    ).all()


@router.post(
    "/projects/{project_id}/node-profiles",
    response_model=NodeProfileView,
    status_code=status.HTTP_201_CREATED,
)
def create_node_profile(
    project_id: UUID,
    body: NodeProfileCreate,
    request: Request,
    request_id: str = Depends(resolve_request_id),
    identity: Identity = Depends(current_identity),
    session: Session = Depends(db_session),
):
    permissions = require_project_permission(
        session, identity.principal_id, project_id, "provider.configure"
    )
    return translate_domain_errors(
        lambda: AdminService(session).create_node_profile(
            project_id=project_id,
            permissions=permissions,
            actor_id=identity.principal_id,
            request_id=request_id,
            source_ip=request.client.host if request.client else None,
            **body.model_dump(),
        )
    )


@router.put("/projects/{project_id}/node-profiles/{profile_id}", response_model=NodeProfileView)
def update_node_profile(
    project_id: UUID,
    profile_id: UUID,
    body: NodeProfileUpdate,
    request: Request,
    request_id: str = Depends(resolve_request_id),
    identity: Identity = Depends(current_identity),
    session: Session = Depends(db_session),
):
    permissions = require_project_permission(
        session, identity.principal_id, project_id, "provider.configure"
    )
    return translate_domain_errors(
        lambda: AdminService(session).update_node_profile(
            project_id=project_id,
            profile_id=profile_id,
            specification=body.specification,
            permissions=permissions,
            actor_id=identity.principal_id,
            request_id=request_id,
            source_ip=request.client.host if request.client else None,
        )
    )


@router.delete("/projects/{project_id}/node-profiles/{profile_id}", status_code=204)
def delete_node_profile(
    project_id: UUID,
    profile_id: UUID,
    request: Request,
    request_id: str = Depends(resolve_request_id),
    identity: Identity = Depends(current_identity),
    session: Session = Depends(db_session),
):
    permissions = require_project_permission(
        session, identity.principal_id, project_id, "provider.configure"
    )
    translate_domain_errors(
        lambda: AdminService(session).delete_node_profile(
            project_id=project_id,
            profile_id=profile_id,
            permissions=permissions,
            actor_id=identity.principal_id,
            request_id=request_id,
            source_ip=request.client.host if request.client else None,
        )
    )


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


def mutation_context(cluster: Cluster, identity: Identity, session: Session, request_id: str):
    """Build the repeated authorization/correlation arguments for cluster writes."""

    return {
        "actor_id": identity.principal_id,
        "permissions": project_permissions(session, identity.principal_id, cluster.project_id),
        "request_id": request_id,
    }


@router.patch("/clusters/{cluster_id}", response_model=OperationView, status_code=202)
def update_cluster(
    cluster_id: UUID,
    body: ClusterUpdate,
    request_id: str = Depends(resolve_request_id),
    identity: Identity = Depends(current_identity),
    session: Session = Depends(db_session),
):
    cluster = authorized_cluster(cluster_id, identity, session, "cluster.update")
    _, operation = translate_domain_errors(
        lambda: ClusterService(session).replace_spec(
            cluster_id=cluster_id,
            spec=body.spec,
            reason=body.reason,
            **mutation_context(cluster, identity, session, request_id),
        )
    )
    return operation


@router.delete("/clusters/{cluster_id}", response_model=OperationView, status_code=202)
def delete_cluster(
    cluster_id: UUID,
    request_id: str = Depends(resolve_request_id),
    identity: Identity = Depends(current_identity),
    session: Session = Depends(db_session),
):
    cluster = authorized_cluster(cluster_id, identity, session, "cluster.delete")
    _, operation = translate_domain_errors(
        lambda: ClusterService(session).delete(
            cluster_id=cluster_id,
            **mutation_context(cluster, identity, session, request_id),
        )
    )
    return operation


@router.post(
    "/clusters/{cluster_id}/worker-node-types",
    response_model=OperationView,
    status_code=202,
)
def create_worker_node_type(
    cluster_id: UUID,
    body: WorkerNodeTypeCreate,
    request_id: str = Depends(resolve_request_id),
    identity: Identity = Depends(current_identity),
    session: Session = Depends(db_session),
):
    cluster = authorized_cluster(cluster_id, identity, session, "cluster.update")
    _, operation = translate_domain_errors(
        lambda: ClusterService(session).add_worker_node_type(
            cluster_id=cluster_id,
            worker_node_type=body.worker_node_type,
            **mutation_context(cluster, identity, session, request_id),
        )
    )
    return operation


@router.put(
    "/clusters/{cluster_id}/worker-node-types/{node_type_name}",
    response_model=OperationView,
    status_code=202,
)
def replace_worker_node_type(
    cluster_id: UUID,
    node_type_name: str,
    body: WorkerNodeTypeCreate,
    request_id: str = Depends(resolve_request_id),
    identity: Identity = Depends(current_identity),
    session: Session = Depends(db_session),
):
    cluster = authorized_cluster(cluster_id, identity, session, "cluster.update")
    _, operation = translate_domain_errors(
        lambda: ClusterService(session).replace_worker_node_type(
            cluster_id=cluster_id,
            node_type_name=node_type_name,
            worker_node_type=body.worker_node_type,
            **mutation_context(cluster, identity, session, request_id),
        )
    )
    return operation


@router.delete(
    "/clusters/{cluster_id}/worker-node-types/{node_type_name}",
    response_model=OperationView,
    status_code=202,
)
def delete_worker_node_type(
    cluster_id: UUID,
    node_type_name: str,
    request_id: str = Depends(resolve_request_id),
    identity: Identity = Depends(current_identity),
    session: Session = Depends(db_session),
):
    cluster = authorized_cluster(cluster_id, identity, session, "cluster.update")
    _, operation = translate_domain_errors(
        lambda: ClusterService(session).remove_worker_node_type(
            cluster_id=cluster_id,
            node_type_name=node_type_name,
            **mutation_context(cluster, identity, session, request_id),
        )
    )
    return operation


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
