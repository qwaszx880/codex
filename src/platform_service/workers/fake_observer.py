"""Local CAPI/CAPO simulator.

It observes the same RECONCILING operations that a real in-cluster observer would,
then writes raw resources, condition transitions and derived health through the
central database contract. It never introduces a second command format.
"""

import os
import time
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select

from platform_service.domain.spec import ClusterSpec
from platform_service.infrastructure.compiler import CapoCompiler
from platform_service.infrastructure.database import (
    Cluster,
    ClusterRevision,
    ClusterStatus,
    ConditionHistory,
    Operation,
    OperationStage,
    Project,
    ResourceStatus,
    SessionLocal,
)


def _condition(kind: str, failed: bool) -> dict:
    return {
        "type": "Ready",
        "status": "False" if failed else "True",
        "reason": "SimulatedFailure" if failed else "Reconciled",
        "message": f"fake {kind} failed" if failed else f"fake {kind} is ready",
        "observedGeneration": 1,
        "lastTransitionTime": datetime.now(timezone.utc).isoformat(),
    }


def observe_once() -> int:
    failure = os.getenv("FAKE_CAPI_FAILURE", "").lower() in {"1", "true", "yes"}
    with SessionLocal.begin() as session:
        operations = session.scalars(
            select(Operation)
            .where(Operation.state == "RECONCILING")
            .with_for_update(skip_locked=True)
        ).all()
        for operation in operations:
            cluster = session.get(Cluster, operation.cluster_id)
            project = session.get(Project, cluster.project_id)
            revision = session.scalar(
                select(ClusterRevision).where(
                    ClusterRevision.cluster_id == cluster.id,
                    ClusterRevision.number == operation.target_revision,
                )
            )
            spec = ClusterSpec.model_validate(revision.spec)
            resources = CapoCompiler().compile(cluster.name, project.namespace, spec)
            for pool in spec.worker_node_types:
                resources.append(
                    {
                        "kind": "MachineSet",
                        "metadata": {
                            "name": f"{cluster.name}-{pool.name}-set",
                            "namespace": project.namespace,
                        },
                    }
                )
                for index in range(pool.replicas):
                    resources.extend(
                        [
                            {
                                "kind": "Machine",
                                "metadata": {
                                    "name": f"{cluster.name}-{pool.name}-{index}",
                                    "namespace": project.namespace,
                                },
                            },
                            {
                                "kind": "OpenStackMachine",
                                "metadata": {
                                    "name": f"{cluster.name}-{pool.name}-{index}",
                                    "namespace": project.namespace,
                                },
                            },
                        ]
                    )
            for resource in resources:
                metadata = resource["metadata"]
                uid = str(
                    uuid5(NAMESPACE_URL, f"{cluster.id}/{resource['kind']}/{metadata['name']}")
                )
                condition = _condition(resource["kind"], failure)
                status = session.scalar(select(ResourceStatus).where(ResourceStatus.uid == uid))
                if status is None:
                    status = ResourceStatus(
                        cluster_id=cluster.id,
                        kind=resource["kind"],
                        namespace=metadata["namespace"],
                        name=metadata["name"],
                        uid=uid,
                        generation=1,
                        observed_generation=1,
                        resource_version="1",
                        conditions=[condition],
                        raw_status={"ready": not failure, "simulated": True},
                    )
                    session.add(status)
                    session.flush()
                    session.add(
                        ConditionHistory(
                            resource_status_id=status.id,
                            condition_type="Ready",
                            status=condition["status"],
                            reason=condition["reason"],
                            message=condition["message"],
                            observed_generation=1,
                            transition_time=datetime.now(timezone.utc),
                        )
                    )
                else:
                    status.conditions = [condition]
                    status.raw_status = {"ready": not failure, "simulated": True}
                    status.observed_at = datetime.now(timezone.utc)
            health = session.get(ClusterStatus, cluster.id)
            normalized = {
                "infrastructure": "FAILED" if failure else "READY",
                "control_plane": "FAILED" if failure else "READY",
                "workers": "DEGRADED" if failure else "READY",
                "machines": "DEGRADED" if failure else "READY",
                "availability": "UNAVAILABLE" if failure else "AVAILABLE",
                "scaling": False,
                "rolling_out": False,
                "remediating": failure,
                "deleting": False,
                "paused": False,
            }
            if health is None:
                session.add(
                    ClusterStatus(
                        cluster_id=cluster.id,
                        health=normalized,
                        management_connectivity="CONNECTED",
                    )
                )
            else:
                health.health = normalized
                health.management_connectivity = "CONNECTED"
                health.updated_at = datetime.now(timezone.utc)
            cluster.observed_revision = operation.target_revision
            operation.state = "FAILED" if failure else "SUCCEEDED"
            operation.completed_at = datetime.now(timezone.utc)
            operation.failure = (
                {"classification": "PERMANENT", "reason": "SimulatedFailure"} if failure else None
            )
            session.add(
                OperationStage(
                    operation_id=operation.id,
                    name="convergence",
                    state="FAILED" if failure else "SUCCEEDED",
                    started_at=operation.created_at,
                    completed_at=datetime.now(timezone.utc),
                    reason="SimulatedFailure" if failure else "Converged",
                    message="local fake management plane observation",
                    related_resource={"kind": "Cluster", "name": cluster.name},
                    retry_count=0,
                    failure=operation.failure,
                )
            )
        return len(operations)


def main() -> None:
    while True:
        observe_once()
        time.sleep(float(os.getenv("FAKE_OBSERVER_INTERVAL", "1")))


if __name__ == "__main__":
    main()
