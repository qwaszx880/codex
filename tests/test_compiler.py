from platform_service.domain.spec import ClusterSpec
from platform_service.infrastructure.compiler import CapoCompiler


def test_capo_compiler_is_pure_and_namespaced():
    spec = ClusterSpec.model_validate(
        {
            "kubernetes": {"version": "v1.31.1"},
            "control_plane": {"node_profile": "cp"},
            "worker_node_types": [
                {
                    "name": "blue",
                    "replicas": 2,
                    "node_profile": "worker",
                    "role": "compute",
                    "labels": {"topology.kubernetes.io/zone": "az-1"},
                }
            ],
        }
    )
    resources = CapoCompiler().compile("demo", "p-a83f", spec)
    assert {x["kind"] for x in resources} == {
        "Cluster",
        "OpenStackCluster",
        "KubeadmControlPlane",
        "MachineDeployment",
        "KubeadmConfigTemplate",
        "OpenStackMachineTemplate",
    }
    assert all(x["metadata"]["namespace"] == "p-a83f" for x in resources)
    deployment = next(x for x in resources if x["kind"] == "MachineDeployment")
    assert deployment["spec"]["replicas"] == 2
    labels = deployment["spec"]["template"]["metadata"]["labels"]
    assert labels == deployment["spec"]["selector"]["matchLabels"]
    assert labels["cluster.x-k8s.io/cluster-name"] == "demo"
    assert labels["platform.example/worker-node-type"] == "blue"
    assert labels["node-role.kubernetes.io/compute"] == ""
    assert labels["topology.kubernetes.io/zone"] == "az-1"
    machine_spec = deployment["spec"]["template"]["spec"]
    assert machine_spec["bootstrap"]["configRef"]["name"] == "demo-blue"
    assert machine_spec["infrastructureRef"]["name"] == "demo-blue"
