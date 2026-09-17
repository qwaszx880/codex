"""Transactional application use cases for cluster desired-state mutations."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from platform_service.application.errors import Forbidden, NotFound
from platform_service.domain.spec import ClusterSpec
from platform_service.infrastructure.database import (
    AuditEvent,
    Cluster,
    ClusterRevision,
    ManagementCluster,
    Operation,
    OutboxEvent,
    Project,
)


PERMISSIONS = {
    "CREATE": "cluster.create",
    "UPDATE": "cluster.update",
    "SCALE": "cluster.scale",
    "UPGRADE": "cluster.upgrade",
    "DELETE": "cluster.delete",
}


class ClusterService:
    """Persist lifecycle intent atomically without depending on RabbitMQ.

    A successful mutation always stores the immutable revision, user-visible
    operation, security audit record, and outbox command in one transaction.
    The separate outbox worker publishes that command after commit.
    """

    def __init__(self, session: Session):
        self.session = session

    def _authorize(self, permissions: set[str], action: str):
        if PERMISSIONS[action] not in permissions:
            raise Forbidden(PERMISSIONS[action])

    def create(
        self,
        *,
        project_id: UUID,
        name: str,
        management_cluster_id: UUID,
        provider_reference_id: UUID,
        spec: ClusterSpec,
        actor_id: UUID,
        permissions: set[str],
        request_id: str,
        source_ip: str | None = None,
    ):
        self._authorize(permissions, "CREATE")
        project = self.session.get(Project, project_id)
        management = self.session.get(ManagementCluster, management_cluster_id)
        if not project or not project.enabled:
            raise NotFound("project")
        if not management or not management.enabled:
            raise NotFound("management cluster")
        cluster = Cluster(
            project_id=project_id,
            name=name,
            management_cluster_id=management_cluster_id,
            provider_reference_id=provider_reference_id,
            desired_revision=1,
        )
        self.session.add(cluster)
        self.session.flush()
        revision = ClusterRevision(
            cluster_id=cluster.id,
            number=1,
            spec=spec.model_dump(mode="json"),
            reason="initial create",
            created_by=actor_id,
        )
        operation = self._intent(
            cluster, project.organization_id, "CREATE", 1, actor_id, request_id, source_ip
        )
        self.session.add(revision)
        self.session.commit()
        self.session.refresh(operation)
        return cluster, operation

    def revise(
        self,
        *,
        cluster_id: UUID,
        spec: ClusterSpec,
        kind: str,
        reason: str,
        actor_id: UUID,
        permissions: set[str],
        request_id: str,
        source_ip: str | None = None,
    ):
        self._authorize(permissions, kind)
        cluster = self.session.execute(
            select(Cluster).where(Cluster.id == cluster_id).with_for_update()
        ).scalar_one_or_none()
        if not cluster or cluster.deleted_at:
            raise NotFound("cluster")
        project = self.session.get(Project, cluster.project_id)
        previous = cluster.desired_revision
        number = previous + 1
        cluster.desired_revision = number
        self.session.add(
            ClusterRevision(
                cluster_id=cluster.id,
                number=number,
                spec=spec.model_dump(mode="json"),
                reason=reason,
                created_by=actor_id,
            )
        )
        operation = self._intent(
            cluster,
            project.organization_id,
            kind,
            number,
            actor_id,
            request_id,
            source_ip,
            previous,
        )
        self.session.commit()
        self.session.refresh(operation)
        return cluster, operation

    def scale(self, *, cluster_id: UUID, pool: str, replicas: int, **context):
        cluster = self.session.get(Cluster, cluster_id)
        if not cluster:
            raise NotFound("cluster")
        current = self.session.execute(
            select(ClusterRevision).where(
                ClusterRevision.cluster_id == cluster_id,
                ClusterRevision.number == cluster.desired_revision,
            )
        ).scalar_one()
        # Normalize revisions written with the legacy ``worker_pools`` key before
        # selecting a node type, then persist only the canonical contract.
        data = ClusterSpec.model_validate(current.spec).model_dump(mode="json")
        target = next(
            (node_type for node_type in data["worker_node_types"] if node_type["name"] == pool),
            None,
        )
        if target is None:
            raise NotFound("worker node type")
        target["replicas"] = replicas
        return self.revise(
            cluster_id=cluster_id,
            spec=ClusterSpec.model_validate(data),
            kind="SCALE",
            reason=f"scale {pool} to {replicas}",
            **context,
        )

    def upgrade(self, *, cluster_id: UUID, version: str, **context):
        """Create an upgrade revision from the cluster's current desired spec."""

        cluster = self.session.get(Cluster, cluster_id)
        if not cluster:
            raise NotFound("cluster")
        current = self.session.scalar(
            select(ClusterRevision).where(
                ClusterRevision.cluster_id == cluster_id,
                ClusterRevision.number == cluster.desired_revision,
            )
        )
        data = ClusterSpec.model_validate(current.spec).model_dump(mode="json")
        data["kubernetes"]["version"] = version
        return self.revise(
            cluster_id=cluster_id,
            spec=ClusterSpec.model_validate(data),
            kind="UPGRADE",
            reason=f"upgrade to {version}",
            **context,
        )

    def _intent(
        self,
        cluster,
        organization_id,
        kind,
        revision,
        actor_id,
        request_id,
        source_ip,
        previous=None,
    ):
        operation = Operation(
            cluster_id=cluster.id,
            project_id=cluster.project_id,
            kind=kind,
            state="ACCEPTED",
            target_revision=revision,
            requested_by=actor_id,
            request_id=request_id,
        )
        self.session.add(operation)
        # Database-generated identifiers are part of the durable command, so
        # materialize the operation ID before building its payload.
        self.session.flush()
        payload = {
            "event_id": None,
            "operation_id": str(operation.id),
            "cluster_id": str(cluster.id),
            "project_id": str(cluster.project_id),
            "target_revision": revision,
            "kind": kind,
        }
        management = self.session.get(ManagementCluster, cluster.management_cluster_id)
        event = OutboxEvent(topic="cluster.commands", routing_key=management.name, payload=payload)
        self.session.add(event)
        # The event ID is the executor's idempotency key and must be embedded in
        # the exact payload that the outbox publisher later sends.
        self.session.flush()
        payload["event_id"] = str(event.id)
        event.payload = payload
        self.session.add(
            AuditEvent(
                actor_id=actor_id,
                organization_id=organization_id,
                project_id=cluster.project_id,
                action=f"cluster.{kind.lower()}",
                target_type="cluster",
                target_id=str(cluster.id),
                request_id=request_id,
                previous_revision=previous,
                new_revision=revision,
                source_ip=source_ip,
                result="ACCEPTED",
                details={"operation_id": str(operation.id)},
            )
        )
        return operation
