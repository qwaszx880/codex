from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from platform_service.application.errors import NotFound
from platform_service.application.iam_service import IamService
from platform_service.infrastructure.auth import project_permissions
from platform_service.infrastructure.database import (
    Base,
    AuditEvent,
    Organization,
    OrganizationMembership,
    Permission,
    Principal,
    Project,
    ProjectMembership,
    Role,
    RolePermission,
)


@pytest.fixture
def iam():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        principal = Principal(
            issuer="https://issuer.example",
            external_subject="user-1",
            username="user",
            display_name="Test User",
            email="user@example.test",
            principal_type="human",
            enabled=True,
        )
        organization = Organization(name="org", display_name="Organization")
        session.add_all([principal, organization])
        session.flush()
        project = Project(
            organization_id=organization.id,
            name="project",
            namespace="p-test",
            enabled=True,
        )
        viewer = Role(name="viewer", scope="project", builtin=True)
        organization_admin = Role(name="org-admin", scope="organization", builtin=True)
        read = Permission(name="cluster.read", description="read clusters")
        administer = Permission(name="project.admin", description="administer projects")
        session.add_all([project, viewer, organization_admin, read, administer])
        session.flush()
        session.add_all(
            [
                RolePermission(role_id=viewer.id, permission_id=read.id),
                RolePermission(role_id=organization_admin.id, permission_id=administer.id),
                OrganizationMembership(
                    organization_id=organization.id,
                    principal_id=principal.id,
                    role_id=organization_admin.id,
                ),
            ]
        )
        session.commit()
        yield session, principal, project, viewer, organization_admin


def test_effective_permissions_union_project_and_organization_roles(iam):
    session, principal, project, viewer, _ = iam
    session.add(
        ProjectMembership(project_id=project.id, principal_id=principal.id, role_id=viewer.id)
    )
    session.commit()

    assert project_permissions(session, principal.id, project.id) == {
        "cluster.read",
        "project.admin",
    }


def test_project_membership_only_accepts_enabled_principal_and_project_role(iam):
    session, principal, project, _, organization_admin = iam
    service = IamService(session)

    with pytest.raises(NotFound, match="project role"):
        service.add_project_member(
            project_id=project.id,
            principal_id=principal.id,
            role_id=organization_admin.id,
            permissions={"project.admin"},
            actor_id=principal.id,
            request_id="request-1",
        )


def test_organization_membership_makes_project_visible(iam):
    session, principal, project, _, _ = iam

    assert [item.id for item in IamService(session).visible_projects(principal.id)] == [project.id]


def test_project_membership_change_is_audited(iam):
    session, principal, project, viewer, _ = iam

    membership = IamService(session).add_project_member(
        project_id=project.id,
        principal_id=principal.id,
        role_id=viewer.id,
        permissions={"project.admin"},
        actor_id=principal.id,
        request_id="iam-change-1",
    )

    audit = session.scalar(select(AuditEvent).where(AuditEvent.request_id == "iam-change-1"))
    assert membership.principal_id == principal.id
    assert audit.action == "project.member.role.add"
    assert audit.details == {"role_id": str(viewer.id)}


def test_unknown_principal_cannot_be_added_to_project(iam):
    session, _, project, viewer, _ = iam

    with pytest.raises(NotFound, match="principal"):
        IamService(session).add_project_member(
            project_id=project.id,
            principal_id=uuid4(),
            role_id=viewer.id,
            permissions={"project.admin"},
            actor_id=uuid4(),
            request_id="request-2",
        )
