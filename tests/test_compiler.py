from platform_service.domain.ports import CompilationContext
from platform_service.domain.spec import ClusterSpec
from platform_service.infrastructure.compiler import CapoCompiler


def context():
    return CompilationContext(
        provider={"cloud_name": "test", "clouds_secret_name": "clouds"},
        node_profiles={
            "cp": {"flavor": "control", "image": "ubuntu", "volume_gb": 40},
            "worker": {"flavor": "compute", "image": "ubuntu", "volume_gb": 80},
        },
    )


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
    resources = CapoCompiler().compile(
        "demo",
        "p-a83f",
        spec,
        context(),
    )
    assert {x["kind"] for x in resources} == {
        "Cluster",
        "OpenStackCluster",
        "KubeadmControlPlane",
        "MachineDeployment",
        "KubeadmConfigTemplate",
        "OpenStackMachineTemplate",
        "MachineHealthCheck",
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

    cluster = next(x for x in resources if x["kind"] == "OpenStackCluster")
    assert cluster["spec"]["identityRef"] == {"cloudName": "test", "name": "clouds"}
    control_plane = next(x for x in resources if x["kind"] == "KubeadmControlPlane")
    assert control_plane["spec"]["machineTemplate"]["infrastructureRef"]["name"] == (
        "demo-control-plane"
    )
    templates = [x for x in resources if x["kind"] == "OpenStackMachineTemplate"]
    worker_template = next(x for x in templates if x["metadata"]["name"] == "demo-blue")
    assert worker_template["spec"]["template"]["spec"]["rootVolume"] == {"sizeGiB": 80}


def test_compiles_autoscaling_addons_taints_and_health_checks():
    spec = ClusterSpec.model_validate(
        {
            "kubernetes": {"version": "v1.31.1"},
            "control_plane": {"node_profile": "cp"},
            "worker_node_types": [
                {
                    "name": "blue",
                    "replicas": 2,
                    "node_profile": "worker",
                    "taints": ["dedicated=batch:NoSchedule"],
                    "autoscaling": {
                        "autoscaling": True,
                        "min_replicas": 1,
                        "max_replicas": 5,
                    },
                }
            ],
            "addons": [{"name": "cni"}, {"name": "credentials", "kind": "Secret"}],
        }
    )

    resources = CapoCompiler().compile("demo", "p-a83f", spec, context())
    deployment = next(x for x in resources if x["kind"] == "MachineDeployment")
    assert "replicas" not in deployment["spec"]
    assert deployment["metadata"]["annotations"] == {
        "cluster.x-k8s.io/cluster-api-autoscaler-node-group-min-size": "1",
        "cluster.x-k8s.io/cluster-api-autoscaler-node-group-max-size": "5",
    }
    bootstrap = next(x for x in resources if x["kind"] == "KubeadmConfigTemplate")
    args = bootstrap["spec"]["template"]["spec"]["joinConfiguration"]["nodeRegistration"][
        "kubeletExtraArgs"
    ]
    assert args["register-with-taints"] == "dedicated=batch:NoSchedule"
    health_checks = [x for x in resources if x["kind"] == "MachineHealthCheck"]
    assert {x["metadata"]["name"] for x in health_checks} == {
        "demo-control-plane",
        "demo-blue",
    }
    addons = next(x for x in resources if x["kind"] == "ClusterResourceSet")
    assert addons["spec"]["strategy"] == "Reconcile"
    assert addons["spec"]["resources"] == [
        {"name": "cni", "kind": "ConfigMap"},
        {"name": "credentials", "kind": "Secret"},
    ]
