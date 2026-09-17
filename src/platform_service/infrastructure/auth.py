"""FastAPI dependencies for OIDC authentication and project authorization lookup."""

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from platform_service.config import Settings, get_settings
from platform_service.infrastructure.database import (
    OrganizationMembership,
    Permission,
    Principal,
    Project,
    ProjectMembership,
    Role,
    RolePermission,
    SessionLocal,
)


@dataclass(frozen=True)
class Identity:
    principal_id: UUID
    issuer: str
    subject: str


security = HTTPBearer(auto_error=True)


def db_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def current_identity(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    settings: Settings = Depends(get_settings),
    session: Session = Depends(db_session),
) -> Identity:
    if settings.auth_disabled:
        raise HTTPException(503, "authentication bypass is not available through the public API")
    jwks = jwt.PyJWKClient(
        settings.oidc_jwks_url or f"{settings.oidc_issuer.rstrip('/')}/.well-known/jwks.json"
    )
    try:
        key = jwks.get_signing_key_from_jwt(credentials.credentials)
        claims = jwt.decode(
            credentials.credentials,
            key.key,
            algorithms=["RS256", "ES256"],
            audience=settings.oidc_audience,
            issuer=settings.oidc_issuer,
            options={"require": ["exp", "iss", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(401, "invalid identity token") from exc
    principal = session.execute(
        select(Principal).where(
            Principal.issuer == claims["iss"], Principal.external_subject == claims["sub"]
        )
    ).scalar_one_or_none()
    if principal is None:
        principal = Principal(
            issuer=claims["iss"],
            external_subject=claims["sub"],
            username=claims.get("preferred_username"),
            display_name=claims.get("name"),
            email=claims.get("email"),
            principal_type="human",
            enabled=True,
        )
        session.add(principal)
    if not principal.enabled:
        raise HTTPException(403, "principal disabled")
    principal.last_login = datetime.now(timezone.utc)
    session.commit()
    return Identity(principal.id, claims["iss"], claims["sub"])


def project_permissions(session: Session, principal_id: UUID, project_id: UUID) -> set[str]:
    project_role_permissions = (
        select(Permission.name)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .join(ProjectMembership, ProjectMembership.role_id == RolePermission.role_id)
        .join(Role, Role.id == ProjectMembership.role_id)
        .where(
            ProjectMembership.principal_id == principal_id,
            ProjectMembership.project_id == project_id,
            Role.scope == "project",
        )
    )
    organization_role_permissions = (
        select(Permission.name)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .join(OrganizationMembership, OrganizationMembership.role_id == RolePermission.role_id)
        .join(Role, Role.id == OrganizationMembership.role_id)
        .join(Project, Project.organization_id == OrganizationMembership.organization_id)
        .where(
            OrganizationMembership.principal_id == principal_id,
            Project.id == project_id,
            Role.scope == "organization",
        )
    )
    return set(session.scalars(project_role_permissions)) | set(
        session.scalars(organization_role_permissions)
    )


def require_project_permission(
    session: Session, principal_id: UUID, project_id: UUID, permission: str
) -> set[str]:
    """Return effective permissions or fail closed at the shared RBAC boundary."""

    permissions = project_permissions(session, principal_id, project_id)
    if permission not in permissions:
        raise HTTPException(403, f"missing permission: {permission}")
    return permissions


def organization_permissions(
    session: Session, principal_id: UUID, organization_id: UUID
) -> set[str]:
    """Resolve only organization-scoped grants for tenant administration."""

    statement = (
        select(Permission.name)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .join(OrganizationMembership, OrganizationMembership.role_id == RolePermission.role_id)
        .join(Role, Role.id == OrganizationMembership.role_id)
        .where(
            OrganizationMembership.principal_id == principal_id,
            OrganizationMembership.organization_id == organization_id,
            Role.scope == "organization",
        )
    )
    return set(session.scalars(statement))


def require_organization_permission(
    session: Session, principal_id: UUID, organization_id: UUID, permission: str
) -> set[str]:
    permissions = organization_permissions(session, principal_id, organization_id)
    if permission not in permissions:
        raise HTTPException(403, f"missing permission: {permission}")
    return permissions
