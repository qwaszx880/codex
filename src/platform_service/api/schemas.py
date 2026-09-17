"""HTTP request and response models exposed by the FastAPI adapter."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from platform_service.domain.spec import ClusterSpec


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


class OperationView(BaseModel):
    id: UUID
    cluster_id: UUID
    kind: str
    state: str
    target_revision: int
    created_at: datetime
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


class ProjectView(BaseModel):
    id: UUID
    organization_id: UUID
    name: str
    namespace: str
    enabled: bool
    model_config = {"from_attributes": True}


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
