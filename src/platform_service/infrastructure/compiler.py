from typing import Any

from platform_service.domain.ports import ClusterCompiler
from platform_service.domain.spec import ClusterSpec


class CapoCompiler(ClusterCompiler):
    """Pure translation boundary: no Kubernetes or OpenStack calls occur here."""

    def compile(self, name: str, namespace: str, spec: ClusterSpec) -> list[dict[str, Any]]:
        labels = {"platform.example/cluster": name}

        def obj(api, kind, obj_name, specification):
            return {
                "apiVersion": api,
                "kind": kind,
                "metadata": {"name": obj_name, "namespace": namespace, "labels": labels},
                "spec": specification,
            }

        resources = [
            obj(
                "cluster.x-k8s.io/v1beta1",
                "Cluster",
                name,
                {
                    "clusterNetwork": {
                        "pods": {"cidrBlocks": [spec.networking.pod_cidr]},
                        "services": {"cidrBlocks": [spec.networking.service_cidr]},
                    },
                    "infrastructureRef": {
                        "apiVersion": "infrastructure.cluster.x-k8s.io/v1beta1",
                        "kind": "OpenStackCluster",
                        "name": name,
                    },
                    "controlPlaneRef": {
                        "apiVersion": "controlplane.cluster.x-k8s.io/v1beta1",
                        "kind": "KubeadmControlPlane",
                        "name": f"{name}-control-plane",
                    },
                },
            ),
            obj("infrastructure.cluster.x-k8s.io/v1beta1", "OpenStackCluster", name, {}),
            obj(
                "controlplane.cluster.x-k8s.io/v1beta1",
                "KubeadmControlPlane",
                f"{name}-control-plane",
                {"replicas": spec.control_plane.replicas, "version": spec.kubernetes.version},
            ),
        ]
        for pool in spec.worker_pools:
            resources.append(
                obj(
                    "cluster.x-k8s.io/v1beta1",
                    "MachineDeployment",
                    f"{name}-{pool.name}",
                    {
                        "clusterName": name,
                        "replicas": pool.replicas,
                        "template": {
                            "metadata": {"labels": pool.labels},
                            "spec": {"version": spec.kubernetes.version},
                        },
                    },
                )
            )
        return resources
