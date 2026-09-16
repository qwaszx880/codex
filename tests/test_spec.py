import pytest
from pydantic import ValidationError

from platform_service.domain.spec import ClusterSpec


def valid():
    return {
        "kubernetes": {"version": "v1.31.1"},
        "control_plane": {"replicas": 3, "node_profile": "control"},
        "worker_pools": [{"name": "workers", "replicas": 3, "node_profile": "compute"}],
    }


def test_spec_accepts_stable_contract():
    assert ClusterSpec.model_validate(valid()).worker_pools[0].replicas == 3


def test_rejects_even_control_plane():
    body = valid()
    body["control_plane"]["replicas"] = 2
    with pytest.raises(ValidationError, match="must be odd"):
        ClusterSpec.model_validate(body)


def test_rejects_duplicate_pool_names():
    body = valid()
    body["worker_pools"].append(body["worker_pools"][0])
    with pytest.raises(ValidationError, match="unique"):
        ClusterSpec.model_validate(body)
