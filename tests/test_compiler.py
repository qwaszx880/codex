from platform_service.domain.spec import ClusterSpec
from platform_service.infrastructure.compiler import CapoCompiler
def test_capo_compiler_is_pure_and_namespaced():
    spec=ClusterSpec.model_validate({"kubernetes":{"version":"v1.31.1"},"control_plane":{"node_profile":"cp"},"worker_pools":[{"name":"blue","replicas":2,"node_profile":"worker"}]})
    resources=CapoCompiler().compile("demo","p-a83f",spec)
    assert {x["kind"] for x in resources}=={"Cluster","OpenStackCluster","KubeadmControlPlane","MachineDeployment"}
    assert all(x["metadata"]["namespace"]=="p-a83f" for x in resources)
    assert next(x for x in resources if x["kind"]=="MachineDeployment")["spec"]["replicas"]==2
