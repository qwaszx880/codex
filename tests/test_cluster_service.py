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
            "worker_node_types": [{"name": "workers", "replicas": 3, "node_profile": "compute"}],
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


def test_scale_normalizes_legacy_worker_pool_revision():
    cluster_id = uuid4()
    session = Mock()
    session.get.return_value = SimpleNamespace(desired_revision=2)
    session.execute.return_value.scalar_one.return_value = SimpleNamespace(
        spec={
            "kubernetes": {"version": "v1.30.1"},
            "control_plane": {"replicas": 3, "node_profile": "control"},
            "worker_pools": [{"name": "compute", "replicas": 2, "node_profile": "compute"}],
        }
    )
    service = ClusterService(session)
    service.revise = Mock(return_value=("cluster", "operation"))

    service.scale(
        cluster_id=cluster_id,
        pool="compute",
        replicas=5,
        actor_id=uuid4(),
        permissions={"cluster.scale"},
        request_id="request-2",
    )

    revision = service.revise.call_args.kwargs
    assert revision["spec"].worker_node_types[0].replicas == 5
    assert "worker_node_types" in revision["spec"].model_dump()
