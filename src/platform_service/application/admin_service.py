"""Synchronous administration use cases for tenants and reusable node profiles."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from platform_service.application.errors import Conflict, Forbidden, NotFound
from platform_service.infrastructure.database import (
    AuditEvent,
    Cluster,
    NodeProfile,
    Organization,
    OrganizationMembership,
    Principal,
    Project,
    ProviderReference,
    Role,
)


class AdminService:
    """Keep tenant administration and its audit records in one transaction."""

    def __init__(self, session: Session):
        self.session = session

    @staticmethod
    def _require(permissions: set[str], permission: str) -> None:
        if permission not in permissions:
            raise Forbidden(permission)

    @staticmethod
    def _reject_raw_credentials(configuration: dict) -> None:
        """Keep common credential values out of provider metadata at every nesting level."""

        forbidden = {
            "password",
            "token",
            "secret",
            "client_secret",
            "application_credential_secret",
            "api_key",
            "access_key",
            "secret_key",
        }

        def contains_secret(value) -> bool:
            if isinstance(value, dict):
                return any(
                    str(key).lower() in forbidden or contains_secret(child)
                    for key, child in value.items()
                )
            if isinstance(value, list):
                return any(contains_secret(item) for item in value)
            return False

        if contains_secret(configuration):
            raise Conflict("provider configuration must use secret_reference for credentials")

    def create_principal(
        self,
        *,
        organization_id: UUID,
        role_id: UUID,
        issuer: str,
        external_subject: str,
        username: str | None,
        display_name: str | None,
        email: str | None,
        principal_type: str,
        permissions: set[str],
        actor_id: UUID,
        request_id: str,
        source_ip: str | None = None,
    ) -> Principal:
        """Create an OIDC mapping and initial organization role assignment."""

        self._require(permissions, "project.admin")
        organization = self.session.get(Organization, organization_id)
        if organization is None:
            raise NotFound("organization")
        role = self.session.get(Role, role_id)
        if role is None or role.scope != "organization":
            raise NotFound("organization role")
        existing = self.session.scalar(
            select(Principal).where(
                Principal.issuer == issuer,
                Principal.external_subject == external_subject,
            )
        )
        if existing is not None:
            raise Conflict("principal already exists")
        principal = Principal(
            issuer=issuer,
            external_subject=external_subject,
            username=username,
            display_name=display_name,
            email=email,
            principal_type=principal_type,
            enabled=True,
        )
        self.session.add(principal)
        self.session.flush()
        self.session.add(
            OrganizationMembership(
                organization_id=organization_id,
                principal_id=principal.id,
                role_id=role_id,
            )
        )
        self._audit(
            actor_id=actor_id,
            organization_id=organization_id,
            project_id=None,
            action="principal.create",
            target_type="principal",
            target_id=principal.id,
            request_id=request_id,
            source_ip=source_ip,
            details={"role_id": str(role_id)},
        )
        self.session.commit()
        self.session.refresh(principal)
        return principal

    def update_principal(
        self,
        *,
        organization_id: UUID,
        principal_id: UUID,
        changes: dict,
        permissions: set[str],
        actor_id: UUID,
        request_id: str,
        source_ip: str | None = None,
    ) -> Principal:
        self._require(permissions, "project.admin")
        membership = self.session.scalar(
            select(OrganizationMembership).where(
                OrganizationMembership.organization_id == organization_id,
                OrganizationMembership.principal_id == principal_id,
            )
        )
        principal = self.session.get(Principal, principal_id)
        if membership is None or principal is None:
            raise NotFound("organization principal")
        for field, value in changes.items():
            setattr(principal, field, value)
        self._audit(
            actor_id=actor_id,
            organization_id=organization_id,
            project_id=None,
            action="principal.update",
            target_type="principal",
            target_id=principal.id,
            request_id=request_id,
            source_ip=source_ip,
            details={"fields": sorted(changes)},
        )
        self.session.commit()
        self.session.refresh(principal)
        return principal

    def create_project(
        self,
        *,
        organization_id: UUID,
        name: str,
        namespace: str,
        permissions: set[str],
        actor_id: UUID,
        request_id: str,
        source_ip: str | None = None,
    ) -> Project:
        self._require(permissions, "project.admin")
        if self.session.get(Organization, organization_id) is None:
            raise NotFound("organization")
        duplicate = self.session.scalar(
            select(Project).where(
                (Project.namespace == namespace)
                | ((Project.organization_id == organization_id) & (Project.name == name))
            )
        )
        if duplicate is not None:
            raise Conflict("project name or namespace already exists")
        project = Project(
            organization_id=organization_id,
            name=name,
            namespace=namespace,
            enabled=True,
        )
        self.session.add(project)
        self.session.flush()
        self._audit(
            actor_id=actor_id,
            organization_id=organization_id,
            project_id=project.id,
            action="project.create",
            target_type="project",
            target_id=project.id,
            request_id=request_id,
            source_ip=source_ip,
        )
        self.session.commit()
        self.session.refresh(project)
        return project

    def update_project(
        self,
        *,
        project_id: UUID,
        changes: dict,
        permissions: set[str],
        actor_id: UUID,
        request_id: str,
        source_ip: str | None = None,
    ) -> Project:
        self._require(permissions, "project.admin")
        project = self.session.get(Project, project_id)
        if project is None:
            raise NotFound("project")
        if "name" in changes:
            duplicate = self.session.scalar(
                select(Project).where(
                    Project.organization_id == project.organization_id,
                    Project.name == changes["name"],
                    Project.id != project.id,
                )
            )
            if duplicate is not None:
                raise Conflict("project name already exists")
        for field, value in changes.items():
            setattr(project, field, value)
        self._audit(
            actor_id=actor_id,
            organization_id=project.organization_id,
            project_id=project.id,
            action="project.update",
            target_type="project",
            target_id=project.id,
            request_id=request_id,
            source_ip=source_ip,
            details={"fields": sorted(changes)},
        )
        self.session.commit()
        self.session.refresh(project)
        return project

    def create_node_profile(
        self,
        *,
        project_id: UUID,
        provider_reference_id: UUID,
        name: str,
        specification: dict,
        permissions: set[str],
        actor_id: UUID,
        request_id: str,
        source_ip: str | None = None,
    ) -> NodeProfile:
        self._require(permissions, "provider.configure")
        project = self.session.get(Project, project_id)
        provider = self.session.get(ProviderReference, provider_reference_id)
        if project is None or not project.enabled:
            raise NotFound("project")
        if provider is None or provider.project_id != project_id:
            raise NotFound("provider reference")
        duplicate = self.session.scalar(
            select(NodeProfile).where(
                NodeProfile.project_id == project_id,
                NodeProfile.name == name,
            )
        )
        if duplicate is not None:
            raise Conflict("node profile already exists")
        profile = NodeProfile(
            project_id=project_id,
            provider_reference_id=provider_reference_id,
            name=name,
            specification=specification,
        )
        self.session.add(profile)
        self.session.flush()
        self._audit_profile(project, profile, actor_id, request_id, source_ip, "create")
        self.session.commit()
        self.session.refresh(profile)
        return profile

    def create_provider_reference(
        self,
        *,
        project_id: UUID,
        provider: str,
        name: str,
        secret_reference: str,
        configuration: dict,
        permissions: set[str],
        actor_id: UUID,
        request_id: str,
        source_ip: str | None = None,
    ) -> ProviderReference:
        self._require(permissions, "provider.configure")
        self._reject_raw_credentials(configuration)
        project = self.session.get(Project, project_id)
        if project is None or not project.enabled:
            raise NotFound("project")
        duplicate = self.session.scalar(
            select(ProviderReference).where(
                ProviderReference.project_id == project_id,
                ProviderReference.name == name,
            )
        )
        if duplicate is not None:
            raise Conflict("provider reference already exists")
        reference = ProviderReference(
            project_id=project_id,
            provider=provider,
            name=name,
            secret_reference=secret_reference,
            configuration=configuration,
        )
        self.session.add(reference)
        self.session.flush()
        self._audit_provider(project, reference, actor_id, request_id, source_ip, "create")
        self.session.commit()
        self.session.refresh(reference)
        return reference

    def update_provider_reference(
        self,
        *,
        project_id: UUID,
        reference_id: UUID,
        changes: dict,
        permissions: set[str],
        actor_id: UUID,
        request_id: str,
        source_ip: str | None = None,
    ) -> ProviderReference:
        self._require(permissions, "provider.configure")
        if "configuration" in changes:
            self._reject_raw_credentials(changes["configuration"])
        project = self.session.get(Project, project_id)
        reference = self.session.get(ProviderReference, reference_id)
        if project is None or reference is None or reference.project_id != project_id:
            raise NotFound("provider reference")
        if "name" in changes:
            duplicate = self.session.scalar(
                select(ProviderReference).where(
                    ProviderReference.project_id == project_id,
                    ProviderReference.name == changes["name"],
                    ProviderReference.id != reference_id,
                )
            )
            if duplicate is not None:
                raise Conflict("provider reference already exists")
        for field, value in changes.items():
            setattr(reference, field, value)
        self._audit_provider(project, reference, actor_id, request_id, source_ip, "update")
        self.session.commit()
        self.session.refresh(reference)
        return reference

    def delete_provider_reference(
        self,
        *,
        project_id: UUID,
        reference_id: UUID,
        permissions: set[str],
        actor_id: UUID,
        request_id: str,
        source_ip: str | None = None,
    ) -> None:
        self._require(permissions, "provider.configure")
        project = self.session.get(Project, project_id)
        reference = self.session.get(ProviderReference, reference_id)
        if project is None or reference is None or reference.project_id != project_id:
            raise NotFound("provider reference")
        in_use = self.session.scalar(
            select(Cluster.id).where(Cluster.provider_reference_id == reference_id).limit(1)
        ) or self.session.scalar(
            select(NodeProfile.id).where(NodeProfile.provider_reference_id == reference_id).limit(1)
        )
        if in_use is not None:
            raise Conflict("provider reference is in use")
        self._audit_provider(project, reference, actor_id, request_id, source_ip, "delete")
        self.session.delete(reference)
        self.session.commit()

    def update_node_profile(
        self,
        *,
        project_id: UUID,
        profile_id: UUID,
        specification: dict,
        permissions: set[str],
        actor_id: UUID,
        request_id: str,
        source_ip: str | None = None,
    ) -> NodeProfile:
        self._require(permissions, "provider.configure")
        project = self.session.get(Project, project_id)
        profile = self.session.get(NodeProfile, profile_id)
        if project is None or profile is None or profile.project_id != project_id:
            raise NotFound("node profile")
        profile.specification = specification
        self._audit_profile(project, profile, actor_id, request_id, source_ip, "update")
        self.session.commit()
        self.session.refresh(profile)
        return profile

    def delete_node_profile(
        self,
        *,
        project_id: UUID,
        profile_id: UUID,
        permissions: set[str],
        actor_id: UUID,
        request_id: str,
        source_ip: str | None = None,
    ) -> None:
        self._require(permissions, "provider.configure")
        project = self.session.get(Project, project_id)
        profile = self.session.get(NodeProfile, profile_id)
        if project is None or profile is None or profile.project_id != project_id:
            raise NotFound("node profile")
        self._audit_profile(project, profile, actor_id, request_id, source_ip, "delete")
        self.session.delete(profile)
        self.session.commit()

    def _audit_profile(self, project, profile, actor_id, request_id, source_ip, action):
        self._audit(
            actor_id=actor_id,
            organization_id=project.organization_id,
            project_id=project.id,
            action=f"node-profile.{action}",
            target_type="node_profile",
            target_id=profile.id,
            request_id=request_id,
            source_ip=source_ip,
        )

    def _audit_provider(self, project, reference, actor_id, request_id, source_ip, action):
        self._audit(
            actor_id=actor_id,
            organization_id=project.organization_id,
            project_id=project.id,
            action=f"provider-reference.{action}",
            target_type="provider_reference",
            target_id=reference.id,
            request_id=request_id,
            source_ip=source_ip,
        )

    def _audit(
        self,
        *,
        actor_id,
        organization_id,
        project_id,
        action,
        target_type,
        target_id,
        request_id,
        source_ip,
        details=None,
    ):
        self.session.add(
            AuditEvent(
                actor_id=actor_id,
                organization_id=organization_id,
                project_id=project_id,
                action=action,
                target_type=target_type,
                target_id=str(target_id),
                request_id=request_id,
                source_ip=source_ip,
                result="SUCCEEDED",
                details=details or {},
            )
        )
