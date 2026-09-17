import pytest
from pydantic import ValidationError

from platform_service.domain.spec import ClusterSpec


def valid():
    return {
        "kubernetes": {"version": "v1.31.1"},
        "control_plane": {"replicas": 3, "node_profile": "control"},
        "worker_node_types": [
            {"name": "workers", "replicas": 3, "node_profile": "compute", "role": "compute"}
        ],
    }


def test_spec_accepts_stable_contract():
    assert ClusterSpec.model_validate(valid()).worker_node_types[0].replicas == 3


def test_rejects_even_control_plane():
    body = valid()
    body["control_plane"]["replicas"] = 2
    with pytest.raises(ValidationError, match="must be odd"):
        ClusterSpec.model_validate(body)


def test_rejects_duplicate_pool_names():
    body = valid()
    body["worker_node_types"].append(body["worker_node_types"][0])
    with pytest.raises(ValidationError, match="unique"):
        ClusterSpec.model_validate(body)


def test_rejects_replicas_outside_autoscaling_range():
    body = valid()
    body["worker_node_types"][0]["autoscaling"] = {
        "autoscaling": True,
        "min_replicas": 4,
        "max_replicas": 10,
    }
    with pytest.raises(ValidationError, match="within autoscaling bounds"):
        ClusterSpec.model_validate(body)


def test_rejects_autoscaling_bounds_when_disabled():
    body = valid()
    body["scaling"] = {"min_replicas": 1, "max_replicas": 5}
    with pytest.raises(ValidationError, match="require autoscaling"):
        ClusterSpec.model_validate(body)


def test_accepts_legacy_worker_pools_but_serializes_node_types():
    body = valid()
    body["worker_pools"] = body.pop("worker_node_types")
    spec = ClusterSpec.model_validate(body)

    assert spec.worker_node_types[0].role == "compute"
    assert "worker_node_types" in spec.model_dump()


@pytest.mark.parametrize(
    "label",
    [
        "cluster.x-k8s.io/cluster-name",
        "platform.example/worker-node-type",
        "node-role.kubernetes.io/control-plane",
    ],
)
def test_rejects_labels_owned_by_node_type_compiler(label):
    body = valid()
    body["worker_node_types"][0]["labels"] = {label: "caller-value"}

    with pytest.raises(ValidationError, match="labels are derived"):
        ClusterSpec.model_validate(body)
