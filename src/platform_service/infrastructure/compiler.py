"""Pure CAPO resource compiler for the provider-neutral cluster specification."""

from typing import Any

from platform_service.domain.ports import ClusterCompiler, CompilationContext
from platform_service.domain.spec import ClusterSpec


class CapoCompiler(ClusterCompiler):
    """Translate desired state to an apply-ordered, internally linked CAPI graph."""

    def compile(
        self,
        name: str,
        namespace: str,
        spec: ClusterSpec,
        context: CompilationContext | None = None,
    ) -> list[dict[str, Any]]:
        context = context or CompilationContext()
        labels = {"platform.example/cluster": name}

        def obj(api: str, kind: str, obj_name: str, specification: dict[str, Any]):
            return {
                "apiVersion": api,
                "kind": kind,
                "metadata": {"name": obj_name, "namespace": namespace, "labels": labels},
                "spec": specification,
            }

        def machine_template(profile_name: str) -> dict[str, Any]:
            if profile_name not in context.node_profiles:
                raise ValueError(f"node profile {profile_name!r} was not resolved")
            profile = dict(context.node_profiles[profile_name])
            if not profile.get("flavor") and not profile.get("flavor_id"):
                raise ValueError(f"node profile {profile_name!r} requires flavor or flavor_id")
            if not profile.get("image"):
                raise ValueError(f"node profile {profile_name!r} requires an image")
            machine: dict[str, Any] = {}
            for source, target in (
                ("flavor", "flavor"),
                ("flavor_id", "flavorID"),
                ("availability_zone", "failureDomain"),
                ("ssh_key_name", "sshKeyName"),
            ):
                if value := profile.get(source):
                    machine[target] = value
            if image := profile.get("image"):
                machine["image"] = image if isinstance(image, dict) else {"filter": {"name": image}}
            if volume_gb := profile.get("volume_gb"):
                machine["rootVolume"] = {"sizeGiB": volume_gb}
                if volume_type := profile.get("volume_type"):
                    machine["rootVolume"]["type"] = volume_type
            if ports := profile.get("ports"):
                machine["ports"] = ports
            if server_group := profile.get("server_group"):
                machine["serverGroup"] = server_group
            return {
                "template": {
                    "metadata": {"annotations": {"platform.example/node-profile": profile_name}},
                    "spec": machine,
                }
            }

        provider = context.provider
        openstack_cluster: dict[str, Any] = {
            "identityRef": {
                "cloudName": provider.get("cloud_name", "openstack"),
                "name": provider.get("clouds_secret_name", "openstack-cloud-config"),
            },
            "managedSubnets": [
                {
                    "cidr": spec.networking.node_cidr,
                    "dnsNameservers": spec.networking.dns_nameservers,
                }
            ],
        }
        if external_network_id := provider.get("external_network_id"):
            openstack_cluster["externalNetwork"] = {"id": external_network_id}
        if provider.get("disable_api_server_floating_ip", False):
            openstack_cluster["disableAPIServerFloatingIP"] = True

        control_plane_name = f"{name}-control-plane"
        resources = [
            obj(
                "infrastructure.cluster.x-k8s.io/v1beta1",
                "OpenStackCluster",
                name,
                openstack_cluster,
            ),
            obj(
                "infrastructure.cluster.x-k8s.io/v1beta1",
                "OpenStackMachineTemplate",
                control_plane_name,
                machine_template(spec.control_plane.node_profile),
            ),
            obj(
                "controlplane.cluster.x-k8s.io/v1beta1",
                "KubeadmControlPlane",
                control_plane_name,
                {
                    "replicas": spec.control_plane.replicas,
                    "version": spec.kubernetes.version,
                    "rolloutStrategy": {
                        "type": "RollingUpdate",
                        "rollingUpdate": {"maxSurge": spec.control_plane.max_surge},
                    },
                    "machineTemplate": {
                        "infrastructureRef": {
                            "apiVersion": "infrastructure.cluster.x-k8s.io/v1beta1",
                            "kind": "OpenStackMachineTemplate",
                            "name": control_plane_name,
                        }
                    },
                    "kubeadmConfigSpec": {
                        "clusterConfiguration": {
                            "apiServer": {
                                "extraArgs": {"audit-log-path": "/var/log/kubernetes/audit.log"}
                            }
                            if spec.features.audit_logs
                            else {}
                        },
                        "initConfiguration": {"nodeRegistration": {"name": "{{ local_hostname }}"}},
                        "joinConfiguration": {"nodeRegistration": {"name": "{{ local_hostname }}"}},
                    },
                },
            ),
        ]
        if spec.machine_health_check.enabled:
            resources.append(
                obj(
                    "cluster.x-k8s.io/v1beta1",
                    "MachineHealthCheck",
                    control_plane_name,
                    {
                        "clusterName": name,
                        "maxUnhealthy": spec.machine_health_check.max_unhealthy,
                        "nodeStartupTimeout": spec.machine_health_check.node_startup_timeout,
                        "selector": {
                            "matchLabels": {
                                "cluster.x-k8s.io/cluster-name": name,
                                "cluster.x-k8s.io/control-plane": "",
                            }
                        },
                        "unhealthyConditions": [
                            {
                                "type": condition.type,
                                "status": condition.status,
                                "timeout": condition.timeout,
                            }
                            for condition in spec.machine_health_check.unhealthy_conditions
                        ],
                    },
                )
            )

        for node_type in spec.worker_node_types:
            deployment_name = f"{name}-{node_type.name}"
            worker_labels = {
                **node_type.labels,
                "cluster.x-k8s.io/cluster-name": name,
                "platform.example/worker-node-type": node_type.name,
                f"node-role.kubernetes.io/{node_type.role}": "",
            }
            kubelet_args = {
                "node-labels": ",".join(f"{key}={value}" for key, value in worker_labels.items())
            }
            if node_type.taints:
                kubelet_args["register-with-taints"] = ",".join(node_type.taints)
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
                                            "kubeletExtraArgs": kubelet_args,
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
                        machine_template(node_type.node_profile),
                    ),
                ]
            )
            annotations = {}
            scaling = node_type.autoscaling or spec.scaling
            replicas: int | None = node_type.replicas
            if scaling.autoscaling:
                annotations = {
                    "cluster.x-k8s.io/cluster-api-autoscaler-node-group-min-size": str(
                        scaling.min_replicas
                    ),
                    "cluster.x-k8s.io/cluster-api-autoscaler-node-group-max-size": str(
                        scaling.max_replicas
                    ),
                }
                replicas = None
            deployment = obj(
                "cluster.x-k8s.io/v1beta1",
                "MachineDeployment",
                deployment_name,
                {
                    "clusterName": name,
                    "selector": {"matchLabels": worker_labels},
                    "strategy": {
                        "type": "RollingUpdate",
                        "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0},
                    },
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
            if replicas is not None:
                deployment["spec"]["replicas"] = replicas
            if annotations:
                deployment["metadata"]["annotations"] = annotations
            resources.append(deployment)

            if spec.machine_health_check.enabled:
                resources.append(
                    obj(
                        "cluster.x-k8s.io/v1beta1",
                        "MachineHealthCheck",
                        deployment_name,
                        {
                            "clusterName": name,
                            "maxUnhealthy": spec.machine_health_check.max_unhealthy,
                            "nodeStartupTimeout": spec.machine_health_check.node_startup_timeout,
                            "selector": {"matchLabels": worker_labels},
                            "unhealthyConditions": [
                                {
                                    "type": condition.type,
                                    "status": condition.status,
                                    "timeout": condition.timeout,
                                }
                                for condition in spec.machine_health_check.unhealthy_conditions
                            ],
                        },
                    )
                )

        if spec.addons:
            resources.append(
                obj(
                    "addons.cluster.x-k8s.io/v1beta1",
                    "ClusterResourceSet",
                    f"{name}-addons",
                    {
                        "clusterSelector": {"matchLabels": labels},
                        "strategy": spec.addon_strategy,
                        "resources": [addon.model_dump(by_alias=True) for addon in spec.addons],
                    },
                )
            )

        # The graph is declarative; a server-side apply adapter must tolerate references
        # whose targets have not become ready yet.
        resources.insert(
            0,
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
                        "name": control_plane_name,
                    },
                },
            ),
        )
        return resources
