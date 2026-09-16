from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from platform_service.application.cluster_service import ClusterService
from platform_service.infrastructure.database import Cluster


def test_upgrade_delegates_revision_creation_to_application_service():
    cluster_id = uuid4()
    session = Mock()
    session.get.return_value = SimpleNamespace(desired_revision=4)
    session.scalar.return_value = SimpleNamespace(
        spec={
            "kubernetes": {"version": "v1.30.1"},
            "control_plane": {"replicas": 3, "node_profile": "control"},
            "worker_pools": [{"name": "workers", "replicas": 3, "node_profile": "compute"}],
        }
    )
    service = ClusterService(session)
    service.revise = Mock(return_value=("cluster", "operation"))

    result = service.upgrade(
        cluster_id=cluster_id,
        version="v1.31.2",
        actor_id=uuid4(),
        permissions={"cluster.upgrade"},
        request_id="request-1",
    )

    assert result == ("cluster", "operation")
    session.get.assert_called_once_with(Cluster, cluster_id)
    revision = service.revise.call_args.kwargs
    assert revision["kind"] == "UPGRADE"
    assert revision["reason"] == "upgrade to v1.31.2"
    assert revision["spec"].kubernetes.version == "v1.31.2"
