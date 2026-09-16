"""Idempotently seed a usable local tenant without storing any provider secrets."""

from uuid import UUID

from sqlalchemy import select

from platform_service.config import get_settings
from platform_service.infrastructure.database import (
    ManagementCluster,
    NodeProfile,
    Organization,
    Permission,
    Principal,
    Project,
    ProjectMembership,
    ProviderReference,
    Role,
    RolePermission,
    SessionLocal,
)

IDS = {
    "principal": UUID("10000000-0000-0000-0000-000000000001"),
    "organization": UUID("20000000-0000-0000-0000-000000000001"),
    "project": UUID("30000000-0000-0000-0000-000000000001"),
    "management": UUID("40000000-0000-0000-0000-000000000001"),
    "provider": UUID("50000000-0000-0000-0000-000000000001"),
    "role": UUID("60000000-0000-0000-0000-000000000001"),
}

PERMISSIONS = (
    "cluster.create",
    "cluster.read",
    "cluster.update",
    "cluster.scale",
    "cluster.upgrade",
    "cluster.delete",
    "provider.configure",
    "project.admin",
    "audit.read",
)


def seed() -> None:
    settings = get_settings()
    with SessionLocal.begin() as session:
        principal = session.get(Principal, IDS["principal"])
        if principal is None:
            session.add(
                Principal(
                    id=IDS["principal"],
                    issuer=settings.oidc_issuer,
                    external_subject=str(IDS["principal"]),
                    username="developer",
                    display_name="Local Developer",
                    email="developer@local.test",
                    principal_type="human",
                    enabled=True,
                )
            )
        if session.get(Organization, IDS["organization"]) is None:
            session.add(
                Organization(
                    id=IDS["organization"], name="local", display_name="Local Organization"
                )
            )
        if session.get(Project, IDS["project"]) is None:
            session.add(
                Project(
                    id=IDS["project"],
                    organization_id=IDS["organization"],
                    name="sandbox",
                    namespace="p-30000000",
                    enabled=True,
                )
            )
        if session.get(ManagementCluster, IDS["management"]) is None:
            session.add(
                ManagementCluster(
                    id=IDS["management"],
                    name=settings.management_cluster,
                    provider="fake-capo",
                    region="local",
                    enabled=True,
                    executor_compatibility="0.1.x",
                    capabilities={"fake": True, "capi": "v1beta1", "capo": "v1beta1"},
                )
            )
        if session.get(ProviderReference, IDS["provider"]) is None:
            session.add(
                ProviderReference(
                    id=IDS["provider"],
                    project_id=IDS["project"],
                    provider="openstack",
                    name="local-fake-openstack",
                    secret_reference="dev/fake/openstack",
                    configuration={"region": "local", "simulated": True},
                )
            )
        role = session.get(Role, IDS["role"])
        if role is None:
            role = Role(id=IDS["role"], name="local-project-admin", scope="project", builtin=True)
            session.add(role)
        session.flush()
        for permission_name in PERMISSIONS:
            permission = session.scalar(
                select(Permission).where(Permission.name == permission_name)
            )
            if permission is None:
                permission = Permission(
                    name=permission_name, description="Local built-in permission"
                )
                session.add(permission)
                session.flush()
            if session.get(RolePermission, (role.id, permission.id)) is None:
                session.add(RolePermission(role_id=role.id, permission_id=permission.id))
        membership = session.scalar(
            select(ProjectMembership).where(
                ProjectMembership.project_id == IDS["project"],
                ProjectMembership.principal_id == IDS["principal"],
                ProjectMembership.role_id == role.id,
            )
        )
        if membership is None:
            session.add(
                ProjectMembership(
                    project_id=IDS["project"], principal_id=IDS["principal"], role_id=role.id
                )
            )
        for name, specification in {
            "control": {"flavor": "fake-control", "image": "ubuntu", "volume_gb": 40},
            "compute": {"flavor": "fake-worker", "image": "ubuntu", "volume_gb": 80},
        }.items():
            exists = session.scalar(
                select(NodeProfile).where(
                    NodeProfile.project_id == IDS["project"], NodeProfile.name == name
                )
            )
            if exists is None:
                session.add(
                    NodeProfile(
                        project_id=IDS["project"],
                        provider_reference_id=IDS["provider"],
                        name=name,
                        specification=specification,
                    )
                )


if __name__ == "__main__":
    seed()
