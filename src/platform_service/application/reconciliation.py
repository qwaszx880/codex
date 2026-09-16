from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from platform_service.domain.errors import DuplicateEvent, OperationConflict, StaleOperation
from platform_service.domain.ports import ClusterCompiler, ManagementClusterAdapter
from platform_service.domain.spec import ClusterSpec
from platform_service.infrastructure.database import (
    Cluster,
    ClusterRevision,
    Operation,
    ProcessedEvent,
    Project,
    ReconciliationLease,
)


class CommandProcessor:
    """Apply one durable command while enforcing delivery safety invariants."""

    def __init__(
        self,
        session: Session,
        compiler: ClusterCompiler,
        adapter: ManagementClusterAdapter,
        executor_id: str,
    ):
        self.session = session
        self.compiler = compiler
        self.adapter = adapter
        self.executor_id = executor_id

    def process(self, message: dict):
        event_id = UUID(message["event_id"])
        operation_id = UUID(message["operation_id"])
        cluster_id = UUID(message["cluster_id"])
        target = int(message["target_revision"])
        now = datetime.now(timezone.utc)
        # RabbitMQ/Celery delivery is at least once. A completed event is a
        # successful no-op rather than a second infrastructure mutation.
        if self.session.get(ProcessedEvent, event_id):
            raise DuplicateEvent(str(event_id))
        cluster = self.session.execute(
            select(Cluster).where(Cluster.id == cluster_id).with_for_update()
        ).scalar_one()
        if target != cluster.desired_revision:
            # Never let a delayed command overwrite a newer desired revision.
            raise StaleOperation(f"revision {target} is not desired {cluster.desired_revision}")
        lease = self.session.get(ReconciliationLease, cluster_id)
        # Serialize mutations per workload cluster while allowing unrelated
        # clusters to reconcile concurrently on other executor replicas.
        if lease and lease.expires_at > now and lease.holder != self.executor_id:
            raise OperationConflict(lease.holder)
        if lease:
            lease.holder = self.executor_id
            lease.acquired_at = now
            lease.expires_at = now + timedelta(minutes=2)
        else:
            self.session.add(
                ReconciliationLease(
                    cluster_id=cluster_id,
                    holder=self.executor_id,
                    acquired_at=now,
                    expires_at=now + timedelta(minutes=2),
                )
            )
        op = self.session.get(Operation, operation_id)
        op.state = "RECONCILING"
        rev = self.session.scalar(
            select(ClusterRevision).where(
                ClusterRevision.cluster_id == cluster_id, ClusterRevision.number == target
            )
        )
        project = self.session.get(Project, cluster.project_id)
        self.adapter.apply(
            self.compiler.compile(
                cluster.name, project.namespace, ClusterSpec.model_validate(rev.spec)
            )
        )
        cluster.applied_revision = target
        self.session.add(
            ProcessedEvent(event_id=event_id, operation_id=operation_id, target_revision=target)
        )
        self.session.commit()
