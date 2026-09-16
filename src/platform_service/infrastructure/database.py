from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Text,
    UniqueConstraint,
    create_engine,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from platform_service.config import get_settings


class Base(DeclarativeBase):
    pass


class IdMixin:
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)


class TimeMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Principal(Base, IdMixin):
    __tablename__ = "principals"
    __table_args__ = (UniqueConstraint("issuer", "external_subject"),)
    issuer: Mapped[str]
    external_subject: Mapped[str]
    username: Mapped[str | None]
    display_name: Mapped[str | None]
    email: Mapped[str | None]
    principal_type: Mapped[str] = mapped_column(default="human")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Organization(Base, IdMixin, TimeMixin):
    __tablename__ = "organizations"
    name: Mapped[str] = mapped_column(unique=True)
    display_name: Mapped[str]


class Project(Base, IdMixin, TimeMixin):
    __tablename__ = "projects"
    organization_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    name: Mapped[str]
    namespace: Mapped[str] = mapped_column(unique=True)
    enabled: Mapped[bool] = mapped_column(default=True)
    __table_args__ = (UniqueConstraint("organization_id", "name"),)


class Role(Base, IdMixin):
    __tablename__ = "roles"
    name: Mapped[str] = mapped_column(unique=True)
    scope: Mapped[str]
    builtin: Mapped[bool] = mapped_column(default=False)


class Permission(Base, IdMixin):
    __tablename__ = "permissions"
    name: Mapped[str] = mapped_column(unique=True)
    description: Mapped[str | None]


class RolePermission(Base):
    __tablename__ = "role_permissions"
    role_id: Mapped[UUID] = mapped_column(ForeignKey("roles.id"), primary_key=True)
    permission_id: Mapped[UUID] = mapped_column(ForeignKey("permissions.id"), primary_key=True)


class OrganizationMembership(Base, IdMixin):
    __tablename__ = "organization_memberships"
    organization_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    principal_id: Mapped[UUID] = mapped_column(ForeignKey("principals.id"), index=True)
    role_id: Mapped[UUID] = mapped_column(ForeignKey("roles.id"))
    __table_args__ = (UniqueConstraint("organization_id", "principal_id", "role_id"),)


class ProjectMembership(Base, IdMixin):
    __tablename__ = "project_memberships"
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    principal_id: Mapped[UUID] = mapped_column(ForeignKey("principals.id"), index=True)
    role_id: Mapped[UUID] = mapped_column(ForeignKey("roles.id"))
    __table_args__ = (UniqueConstraint("project_id", "principal_id", "role_id"),)


class ProviderReference(Base, IdMixin, TimeMixin):
    __tablename__ = "provider_references"
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    provider: Mapped[str]
    name: Mapped[str]
    secret_reference: Mapped[str]
    configuration: Mapped[dict] = mapped_column(JSON, default=dict)


class NodeProfile(Base, IdMixin, TimeMixin):
    __tablename__ = "node_profiles"
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    provider_reference_id: Mapped[UUID] = mapped_column(ForeignKey("provider_references.id"))
    name: Mapped[str]
    specification: Mapped[dict] = mapped_column(JSON)
    __table_args__ = (UniqueConstraint("project_id", "name"),)


class ManagementCluster(Base, IdMixin, TimeMixin):
    __tablename__ = "management_clusters"
    name: Mapped[str] = mapped_column(unique=True)
    provider: Mapped[str]
    region: Mapped[str]
    enabled: Mapped[bool] = mapped_column(default=True)
    executor_compatibility: Mapped[str]
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict)


class Cluster(Base, IdMixin, TimeMixin):
    __tablename__ = "clusters"
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    management_cluster_id: Mapped[UUID] = mapped_column(ForeignKey("management_clusters.id"))
    provider_reference_id: Mapped[UUID] = mapped_column(ForeignKey("provider_references.id"))
    name: Mapped[str]
    desired_revision: Mapped[int] = mapped_column(default=1)
    applied_revision: Mapped[int | None]
    observed_revision: Mapped[int | None]
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("project_id", "name"),)
    revisions: Mapped[list["ClusterRevision"]] = relationship(
        back_populates="cluster", order_by="ClusterRevision.number"
    )


class ClusterRevision(Base, IdMixin):
    __tablename__ = "cluster_revisions"
    cluster_id: Mapped[UUID] = mapped_column(ForeignKey("clusters.id"), index=True)
    number: Mapped[int]
    spec: Mapped[dict] = mapped_column(JSON)
    reason: Mapped[str]
    created_by: Mapped[UUID] = mapped_column(ForeignKey("principals.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("cluster_id", "number"),)
    cluster: Mapped[Cluster] = relationship(back_populates="revisions")


class Operation(Base, IdMixin):
    __tablename__ = "operations"
    cluster_id: Mapped[UUID] = mapped_column(ForeignKey("clusters.id"), index=True)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    kind: Mapped[str]
    state: Mapped[str] = mapped_column(default="ACCEPTED", index=True)
    target_revision: Mapped[int]
    requested_by: Mapped[UUID] = mapped_column(ForeignKey("principals.id"))
    request_id: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure: Mapped[dict | None] = mapped_column(JSON)


class OperationStage(Base, IdMixin):
    __tablename__ = "operation_stages"
    operation_id: Mapped[UUID] = mapped_column(ForeignKey("operations.id"), index=True)
    name: Mapped[str]
    state: Mapped[str]
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None]
    message: Mapped[str | None] = mapped_column(Text)
    related_resource: Mapped[dict | None] = mapped_column(JSON)
    retry_count: Mapped[int] = mapped_column(default=0)
    failure: Mapped[dict | None] = mapped_column(JSON)


class OutboxEvent(Base, IdMixin):
    __tablename__ = "outbox_events"
    topic: Mapped[str]
    routing_key: Mapped[str]
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    attempts: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None]


class AuditEvent(Base, IdMixin):
    __tablename__ = "audit_events"
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("principals.id"), index=True)
    organization_id: Mapped[UUID | None] = mapped_column(ForeignKey("organizations.id"))
    project_id: Mapped[UUID | None] = mapped_column(ForeignKey("projects.id"), index=True)
    action: Mapped[str]
    target_type: Mapped[str]
    target_id: Mapped[str]
    request_id: Mapped[str]
    previous_revision: Mapped[int | None]
    new_revision: Mapped[int | None]
    source_ip: Mapped[str | None]
    result: Mapped[str]
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    details: Mapped[dict] = mapped_column(JSON, default=dict)


class ReconciliationLease(Base):
    __tablename__ = "reconciliation_leases"
    cluster_id: Mapped[UUID] = mapped_column(ForeignKey("clusters.id"), primary_key=True)
    holder: Mapped[str]
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class ProcessedEvent(Base):
    __tablename__ = "processed_events"
    event_id: Mapped[UUID] = mapped_column(primary_key=True)
    operation_id: Mapped[UUID]
    target_revision: Mapped[int]
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ClusterStatus(Base):
    __tablename__ = "cluster_status"
    cluster_id: Mapped[UUID] = mapped_column(ForeignKey("clusters.id"), primary_key=True)
    health: Mapped[dict] = mapped_column(JSON)
    management_connectivity: Mapped[str] = mapped_column(default="UNKNOWN")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ResourceStatus(Base, IdMixin):
    __tablename__ = "resource_status"
    cluster_id: Mapped[UUID] = mapped_column(ForeignKey("clusters.id"), index=True)
    kind: Mapped[str]
    namespace: Mapped[str]
    name: Mapped[str]
    uid: Mapped[str]
    generation: Mapped[int | None]
    observed_generation: Mapped[int | None]
    resource_version: Mapped[str | None]
    conditions: Mapped[list] = mapped_column(JSON, default=list)
    raw_status: Mapped[dict] = mapped_column(JSON, default=dict)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    __table_args__ = (UniqueConstraint("uid"),)


class ConditionHistory(Base, IdMixin):
    __tablename__ = "condition_history"
    resource_status_id: Mapped[UUID] = mapped_column(ForeignKey("resource_status.id"), index=True)
    condition_type: Mapped[str]
    status: Mapped[str]
    reason: Mapped[str | None]
    message: Mapped[str | None] = mapped_column(Text)
    observed_generation: Mapped[int | None]
    transition_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ExecutorInstance(Base, IdMixin):
    __tablename__ = "executor_instances"
    management_cluster_id: Mapped[UUID] = mapped_column(
        ForeignKey("management_clusters.id"), index=True
    )
    executor_id: Mapped[str] = mapped_column(unique=True)
    executor_version: Mapped[str]
    capi_version: Mapped[str]
    capo_version: Mapped[str]
    status: Mapped[str]
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


engine = create_engine(get_settings().database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(engine, expire_on_commit=False)
