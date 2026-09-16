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
    Permission,
    Principal,
    ProjectMembership,
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
    stmt = (
        select(Permission.name)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .join(ProjectMembership, ProjectMembership.role_id == RolePermission.role_id)
        .where(
            ProjectMembership.principal_id == principal_id,
            ProjectMembership.project_id == project_id,
        )
    )
    return set(session.scalars(stmt))
