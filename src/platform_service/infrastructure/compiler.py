"""Pure CAPO resource compiler for the provider-neutral cluster specification."""

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
        for node_type in spec.worker_node_types:
            deployment_name = f"{name}-{node_type.name}"
            worker_labels = {
                **node_type.labels,
                "cluster.x-k8s.io/cluster-name": name,
                "platform.example/worker-node-type": node_type.name,
                f"node-role.kubernetes.io/{node_type.role}": "",
            }
            resources.extend(
                [
                    obj(
                        "bootstrap.cluster.x-k8s.io/v1beta1",
                        "KubeadmConfigTemplate",
                        deployment_name,
                        {
                            "template": {
                                "spec": {
                                    "joinConfiguration": {
                                        "nodeRegistration": {
                                            "name": "{{ local_hostname }}",
                                            "kubeletExtraArgs": {
                                                "node-labels": ",".join(
                                                    f"{key}={value}"
                                                    for key, value in worker_labels.items()
                                                )
                                            },
                                        }
                                    }
                                }
                            }
                        },
                    ),
                    obj(
                        "infrastructure.cluster.x-k8s.io/v1beta1",
                        "OpenStackMachineTemplate",
                        deployment_name,
                        {
                            "template": {
                                "metadata": {
                                    "annotations": {
                                        "platform.example/node-profile": node_type.node_profile
                                    }
                                },
                                "spec": {},
                            }
                        },
                    ),
                ]
            )
            resources.append(
                obj(
                    "cluster.x-k8s.io/v1beta1",
                    "MachineDeployment",
                    deployment_name,
                    {
                        "clusterName": name,
                        "replicas": node_type.replicas,
                        "selector": {"matchLabels": worker_labels},
                        "template": {
                            "metadata": {"labels": worker_labels},
                            "spec": {
                                "clusterName": name,
                                "version": spec.kubernetes.version,
                                "bootstrap": {
                                    "configRef": {
                                        "apiVersion": "bootstrap.cluster.x-k8s.io/v1beta1",
                                        "kind": "KubeadmConfigTemplate",
                                        "name": deployment_name,
                                    }
                                },
                                "infrastructureRef": {
                                    "apiVersion": "infrastructure.cluster.x-k8s.io/v1beta1",
                                    "kind": "OpenStackMachineTemplate",
                                    "name": deployment_name,
                                },
                            },
                        },
                    },
                )
            )
        return resources
