"""Project-scoped IAM use cases independent of the HTTP transport."""

from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from platform_service.application.errors import Conflict, Forbidden, NotFound
from platform_service.infrastructure.database import (
    OrganizationMembership,
    AuditEvent,
    Permission,
    Principal,
    Project,
    ProjectMembership,
    Role,
    RolePermission,
)


class IamService:
    def __init__(self, session: Session):
        self.session = session

    @staticmethod
    def _authorize(permissions: set[str]) -> None:
        if "project.admin" not in permissions:
            raise Forbidden("project.admin")

    def add_project_member(
        self,
        *,
        project_id: UUID,
        principal_id: UUID,
        role_id: UUID,
        permissions: set[str],
        actor_id: UUID,
        request_id: str,
        source_ip: str | None = None,
    ) -> ProjectMembership:
        self._authorize(permissions)
        project = self.session.get(Project, project_id)
        if project is None or not project.enabled:
            raise NotFound("project")
        principal = self.session.get(Principal, principal_id)
        if principal is None or not principal.enabled:
            raise NotFound("principal")
        role = self.session.get(Role, role_id)
        if role is None or role.scope != "project":
            raise NotFound("project role")
        existing = self.session.scalar(
            select(ProjectMembership).where(
                ProjectMembership.project_id == project_id,
                ProjectMembership.principal_id == principal_id,
                ProjectMembership.role_id == role_id,
            )
        )
        if existing is not None:
            raise Conflict("project membership already exists")
        membership = ProjectMembership(
            project_id=project_id, principal_id=principal_id, role_id=role_id
        )
        self.session.add(membership)
        self.session.add(
            AuditEvent(
                actor_id=actor_id,
                organization_id=project.organization_id,
                project_id=project_id,
                action="project.member.role.add",
                target_type="principal",
                target_id=str(principal_id),
                request_id=request_id,
                source_ip=source_ip,
                result="SUCCEEDED",
                details={"role_id": str(role_id)},
            )
        )
        self.session.commit()
        self.session.refresh(membership)
        return membership

    def visible_projects(self, principal_id: UUID) -> list[Project]:
        """List enabled projects reachable through project or organization roles."""

        direct = (
            select(Project.id)
            .join(ProjectMembership, ProjectMembership.project_id == Project.id)
            .where(ProjectMembership.principal_id == principal_id)
        )
        inherited = (
            select(Project.id)
            .join(
                OrganizationMembership,
                OrganizationMembership.organization_id == Project.organization_id,
            )
            .where(OrganizationMembership.principal_id == principal_id)
        )
        return list(
            self.session.scalars(
                select(Project)
                .where(Project.enabled.is_(True), Project.id.in_(direct.union(inherited)))
                .order_by(Project.name)
            )
        )

    def project_members(
        self, *, project_id: UUID, permissions: set[str]
    ) -> list[tuple[ProjectMembership, Principal, Role, list[str]]]:
        self._authorize(permissions)
        rows = self.session.execute(
            select(ProjectMembership, Principal, Role)
            .join(Principal, Principal.id == ProjectMembership.principal_id)
            .join(Role, Role.id == ProjectMembership.role_id)
            .where(ProjectMembership.project_id == project_id)
            .order_by(Principal.username, Principal.id, Role.name)
        ).all()
        return [
            (membership, principal, role, self.role_permissions(role.id))
            for membership, principal, role in rows
        ]

    def project_roles(self, permissions: set[str]) -> list[tuple[Role, list[str]]]:
        self._authorize(permissions)
        roles = self.session.scalars(
            select(Role).where(Role.scope == "project").order_by(Role.name)
        ).all()
        return [(role, self.role_permissions(role.id)) for role in roles]

    def role_permissions(self, role_id: UUID) -> list[str]:
        return list(
            self.session.scalars(
                select(Permission.name)
                .join(RolePermission, RolePermission.permission_id == Permission.id)
                .where(RolePermission.role_id == role_id)
                .order_by(Permission.name)
            )
        )

    def remove_project_member(
        self,
        *,
        project_id: UUID,
        principal_id: UUID,
        role_id: UUID,
        permissions: set[str],
        actor_id: UUID,
        request_id: str,
        source_ip: str | None = None,
    ) -> None:
        self._authorize(permissions)
        project = self.session.get(Project, project_id)
        if project is None:
            raise NotFound("project")
        result = self.session.execute(
            delete(ProjectMembership).where(
                ProjectMembership.project_id == project_id,
                ProjectMembership.principal_id == principal_id,
                ProjectMembership.role_id == role_id,
            )
        )
        if result.rowcount == 0:
            raise NotFound("project membership")
        self.session.add(
            AuditEvent(
                actor_id=actor_id,
                organization_id=project.organization_id,
                project_id=project_id,
                action="project.member.role.remove",
                target_type="principal",
                target_id=str(principal_id),
                request_id=request_id,
                source_ip=source_ip,
                result="SUCCEEDED",
                details={"role_id": str(role_id)},
            )
        )
        self.session.commit()
