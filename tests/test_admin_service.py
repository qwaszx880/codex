from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from pydantic import ValidationError
import pytest

from platform_service.api.schemas import ProjectUpdate, ProviderReferenceView
from platform_service.application.admin_service import AdminService
from platform_service.application.errors import Conflict
from platform_service.infrastructure.database import (
    Base,
    AuditEvent,
    Organization,
    Principal,
    Project,
    ProviderReference,
    Role,
)


def admin_context():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = Session(engine)
    actor = Principal(
        issuer="https://issuer.example",
        external_subject="admin",
        username="admin",
        principal_type="human",
        enabled=True,
    )
    organization = Organization(name="org", display_name="Organization")
    role = Role(name="organization-admin", scope="organization", builtin=True)
    session.add_all([actor, organization, role])
    session.commit()
    return session, actor, organization, role


def test_create_principal_creates_org_membership_and_audit():
    session, actor, organization, role = admin_context()

    principal = AdminService(session).create_principal(
        organization_id=organization.id,
        role_id=role.id,
        issuer="https://issuer.example",
        external_subject="new-user",
        username="new-user",
        display_name="New User",
        email="new@example.test",
        principal_type="human",
        permissions={"project.admin"},
        actor_id=actor.id,
        request_id="create-user",
    )

    audit = session.scalar(select(AuditEvent).where(AuditEvent.request_id == "create-user"))
    assert principal.external_subject == "new-user"
    assert audit.action == "principal.create"
    assert audit.target_id == str(principal.id)


def test_project_provider_and_node_profile_administration():
    session, actor, organization, _ = admin_context()
    service = AdminService(session)
    project = service.create_project(
        organization_id=organization.id,
        name="workloads",
        namespace="workloads",
        permissions={"project.admin"},
        actor_id=actor.id,
        request_id="create-project",
    )
    provider = service.create_provider_reference(
        project_id=project.id,
        provider="openstack",
        name="primary",
        secret_reference="vault/projects/workloads/openstack",
        configuration={"region": "regionOne"},
        permissions={"provider.configure"},
        actor_id=actor.id,
        request_id="create-provider",
    )
    profile = service.create_node_profile(
        project_id=project.id,
        provider_reference_id=provider.id,
        name="compute",
        specification={"flavor": "m1.large", "image": "ubuntu"},
        permissions={"provider.configure"},
        actor_id=actor.id,
        request_id="create-profile",
    )

    safe_response = ProviderReferenceView.model_validate(provider).model_dump()
    assert session.get(Project, project.id) is project
    assert session.get(ProviderReference, provider.id) is provider
    assert profile.provider_reference_id == provider.id
    assert "secret_reference" not in safe_response


def test_empty_patch_is_rejected():
    with pytest.raises(ValidationError, match="at least one non-null field"):
        ProjectUpdate.model_validate({})


def test_provider_configuration_rejects_raw_credentials():
    session, actor, organization, _ = admin_context()
    service = AdminService(session)
    project = service.create_project(
        organization_id=organization.id,
        name="workloads",
        namespace="workloads",
        permissions={"project.admin"},
        actor_id=actor.id,
        request_id="create-project",
    )

    with pytest.raises(Conflict, match="secret_reference"):
        service.create_provider_reference(
            project_id=project.id,
            provider="openstack",
            name="unsafe",
            secret_reference="vault/safe-pointer",
            configuration={"auth": {"password": "must-not-be-stored"}},
            permissions={"provider.configure"},
            actor_id=actor.id,
            request_id="unsafe-provider",
        )
