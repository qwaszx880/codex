"""Stable provider-neutral desired-state contract accepted by the platform API."""

from typing import Annotated, Literal

from pydantic import AliasChoices, BaseModel, Field, model_validator


class KubernetesSpec(BaseModel):
    version: str = Field(pattern=r"^v?1\.\d+\.\d+$")


class NetworkingSpec(BaseModel):
    pod_cidr: str = "10.244.0.0/16"
    service_cidr: str = "10.96.0.0/12"
    node_cidr: str = "10.6.0.0/24"
    dns_nameservers: list[str] = Field(default_factory=lambda: ["1.1.1.1", "8.8.8.8"])


class ScalingSpec(BaseModel):
    autoscaling: bool = False
    min_replicas: int | None = Field(default=None, ge=0)
    max_replicas: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def valid_range(self):
        if self.autoscaling and (self.min_replicas is None or self.max_replicas is None):
            raise ValueError("autoscaling requires min_replicas and max_replicas")
        if not self.autoscaling and (
            self.min_replicas is not None or self.max_replicas is not None
        ):
            raise ValueError("autoscaling bounds require autoscaling to be enabled")
        if (
            self.min_replicas is not None
            and self.max_replicas is not None
            and self.min_replicas > self.max_replicas
        ):
            raise ValueError("min_replicas cannot exceed max_replicas")
        return self


class WorkerNodeTypeSpec(BaseModel):
    """A homogeneous worker group managed by one CAPI MachineDeployment.

    CAPI creates and rolls the underlying MachineSets.  Role and ownership labels are
    derived by the compiler so callers cannot accidentally break its selectors.
    """

    name: str = Field(min_length=1, max_length=63, pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
    replicas: Annotated[int, Field(ge=0, le=1000)]
    node_profile: str = Field(min_length=1)
    role: str = Field(
        default="worker",
        min_length=1,
        max_length=63,
        pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$",
    )
    labels: dict[str, str] = Field(default_factory=dict)
    taints: list[str] = Field(default_factory=list)
    autoscaling: ScalingSpec | None = None

    @model_validator(mode="after")
    def reserved_labels_are_derived(self):
        reserved = {
            "cluster.x-k8s.io/cluster-name",
            "platform.example/worker-node-type",
        }
        if reserved.intersection(self.labels) or any(
            key.startswith("node-role.kubernetes.io/") for key in self.labels
        ):
            raise ValueError("CAPI ownership, worker-node-type, and role labels are derived")
        return self


class ControlPlaneSpec(BaseModel):
    replicas: Annotated[int, Field(ge=1, le=9)] = 3
    node_profile: str
    max_surge: Annotated[int, Field(ge=0, le=3)] = 1


class UnhealthyConditionSpec(BaseModel):
    type: str
    status: Literal["True", "False", "Unknown"]
    timeout: str = Field(pattern=r"^[1-9]\d*[smh]$")


class MachineHealthCheckSpec(BaseModel):
    enabled: bool = True
    max_unhealthy: str = "40%"
    node_startup_timeout: str = Field(default="10m", pattern=r"^[1-9]\d*[smh]$")
    unhealthy_conditions: list[UnhealthyConditionSpec] = Field(
        default_factory=lambda: [
            UnhealthyConditionSpec(type="Ready", status="False", timeout="5m"),
            UnhealthyConditionSpec(type="Ready", status="Unknown", timeout="5m"),
        ]
    )


class AddonReferenceSpec(BaseModel):
    name: str = Field(min_length=1, max_length=253)
    kind: Literal["ConfigMap", "Secret"] = "ConfigMap"


class FeatureSpec(BaseModel):
    audit_logs: bool = True
    metrics: bool = True


class ClusterSpec(BaseModel):
    kubernetes: KubernetesSpec
    networking: NetworkingSpec = Field(default_factory=NetworkingSpec)
    control_plane: ControlPlaneSpec
    worker_node_types: list[WorkerNodeTypeSpec] = Field(
        min_length=1,
        validation_alias=AliasChoices("worker_node_types", "worker_pools"),
    )
    scaling: ScalingSpec = Field(default_factory=ScalingSpec)
    features: FeatureSpec = Field(default_factory=FeatureSpec)
    machine_health_check: MachineHealthCheckSpec = Field(default_factory=MachineHealthCheckSpec)
    addons: list[AddonReferenceSpec] = Field(default_factory=list)
    addon_strategy: Literal["ApplyOnce", "Reconcile"] = "Reconcile"

    @model_validator(mode="after")
    def unique_node_types(self):
        names = [node_type.name for node_type in self.worker_node_types]
        if len(names) != len(set(names)):
            raise ValueError("worker node type names must be unique")
        if self.control_plane.replicas % 2 == 0:
            raise ValueError("control plane replicas must be odd")
        for node_type in self.worker_node_types:
            scaling = node_type.autoscaling or self.scaling
            if scaling.autoscaling and not (
                scaling.min_replicas <= node_type.replicas <= scaling.max_replicas
            ):
                raise ValueError("worker replicas must be within autoscaling bounds")
        return self


OperationKind = Literal["CREATE", "UPDATE", "SCALE", "UPGRADE", "DELETE"]
