"""HTTP request and response models exposed by the FastAPI adapter."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from platform_service.domain.spec import ClusterSpec, WorkerNodeTypeSpec


class NonEmptyPatch(BaseModel):
    """Reject empty PATCH documents and explicit null-only updates."""

    @model_validator(mode="after")
    def contains_change(self):
        if not any(getattr(self, name) is not None for name in self.model_fields_set):
            raise ValueError("at least one non-null field is required")
        return self


class ClusterCreate(BaseModel):
    project_id: UUID
    name: str = Field(min_length=1, max_length=63)
    management_cluster_id: UUID
    provider_reference_id: UUID
    spec: ClusterSpec


class ClusterView(BaseModel):
    id: UUID
    project_id: UUID
    name: str
    desired_revision: int
    applied_revision: int | None
    observed_revision: int | None
    model_config = {"from_attributes": True}


class ClusterUpdate(BaseModel):
    """Replace the complete provider-neutral desired specification."""

    spec: ClusterSpec
    reason: str = Field(default="cluster specification updated", min_length=1, max_length=255)


class OperationView(BaseModel):
    id: UUID
    cluster_id: UUID
    kind: str
    state: str
    target_revision: int
    created_at: datetime
    completed_at: datetime | None
    failure: dict | None
    model_config = {"from_attributes": True}


class ScaleRequest(BaseModel):
    pool: str
    replicas: int = Field(ge=0, le=1000)


class UpgradeRequest(BaseModel):
    version: str = Field(pattern=r"^v?1\.\d+\.\d+$")


class PrincipalView(BaseModel):
    id: UUID
    username: str | None
    display_name: str | None
    email: str | None
    principal_type: str
    enabled: bool
    model_config = {"from_attributes": True}


class PrincipalCreate(BaseModel):
    """Provision an external identity mapping, never an identity-provider password."""

    issuer: str = Field(min_length=1)
    external_subject: str = Field(min_length=1)
    role_id: UUID
    username: str | None = Field(default=None, max_length=255)
    display_name: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=320)
    principal_type: str = Field(default="human", min_length=1, max_length=50)


class PrincipalUpdate(NonEmptyPatch):
    username: str | None = Field(default=None, max_length=255)
    display_name: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=320)
    enabled: bool | None = None


class ProjectView(BaseModel):
    id: UUID
    organization_id: UUID
    name: str
    namespace: str
    enabled: bool
    model_config = {"from_attributes": True}


class OrganizationView(BaseModel):
    id: UUID
    name: str
    display_name: str
    model_config = {"from_attributes": True}


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=63)
    namespace: str = Field(
        min_length=1,
        max_length=63,
        pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$",
    )


class ProjectUpdate(NonEmptyPatch):
    name: str | None = Field(default=None, min_length=1, max_length=63)
    enabled: bool | None = None


class NodeProfileCreate(BaseModel):
    provider_reference_id: UUID
    name: str = Field(
        min_length=1,
        max_length=63,
        pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$",
    )
    specification: dict


class NodeProfileUpdate(BaseModel):
    specification: dict


class NodeProfileView(BaseModel):
    id: UUID
    project_id: UUID
    provider_reference_id: UUID
    name: str
    specification: dict
    model_config = {"from_attributes": True}


class ProviderReferenceCreate(BaseModel):
    provider: str = Field(min_length=1, max_length=63)
    name: str = Field(min_length=1, max_length=255)
    secret_reference: str = Field(min_length=1, max_length=1024)
    configuration: dict = Field(default_factory=dict)


class ProviderReferenceUpdate(NonEmptyPatch):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    secret_reference: str | None = Field(default=None, min_length=1, max_length=1024)
    configuration: dict | None = None


class ProviderReferenceView(BaseModel):
    """Safe provider metadata; the credential reference is intentionally omitted."""

    id: UUID
    project_id: UUID
    provider: str
    name: str
    configuration: dict
    model_config = {"from_attributes": True}


class ManagementClusterView(BaseModel):
    id: UUID
    name: str
    provider: str
    region: str
    enabled: bool
    executor_compatibility: str
    capabilities: dict
    model_config = {"from_attributes": True}


class WorkerNodeTypeCreate(BaseModel):
    worker_node_type: WorkerNodeTypeSpec


class RoleView(BaseModel):
    id: UUID
    name: str
    scope: str
    builtin: bool
    permissions: list[str] = Field(default_factory=list)


class ProjectMembershipCreate(BaseModel):
    principal_id: UUID
    role_id: UUID


class ProjectMembershipView(BaseModel):
    id: UUID
    project_id: UUID
    principal: PrincipalView
    role: RoleView
