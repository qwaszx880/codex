from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator


class KubernetesSpec(BaseModel):
    version: str = Field(pattern=r"^v?1\.\d+\.\d+$")


class NetworkingSpec(BaseModel):
    pod_cidr: str = "10.244.0.0/16"
    service_cidr: str = "10.96.0.0/12"


class MachinePoolSpec(BaseModel):
    name: str = Field(min_length=1, max_length=63, pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
    replicas: Annotated[int, Field(ge=0, le=1000)]
    node_profile: str = Field(min_length=1)
    labels: dict[str, str] = Field(default_factory=dict)
    taints: list[str] = Field(default_factory=list)


class ControlPlaneSpec(BaseModel):
    replicas: Annotated[int, Field(ge=1, le=9)] = 3
    node_profile: str


class ScalingSpec(BaseModel):
    autoscaling: bool = False
    min_replicas: int | None = Field(default=None, ge=0)
    max_replicas: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def valid_range(self):
        if self.autoscaling and (self.min_replicas is None or self.max_replicas is None):
            raise ValueError("autoscaling requires min_replicas and max_replicas")
        if (
            self.min_replicas is not None
            and self.max_replicas is not None
            and self.min_replicas > self.max_replicas
        ):
            raise ValueError("min_replicas cannot exceed max_replicas")
        return self


class FeatureSpec(BaseModel):
    audit_logs: bool = True
    metrics: bool = True


class ClusterSpec(BaseModel):
    kubernetes: KubernetesSpec
    networking: NetworkingSpec = Field(default_factory=NetworkingSpec)
    control_plane: ControlPlaneSpec
    worker_pools: list[MachinePoolSpec] = Field(min_length=1)
    scaling: ScalingSpec = Field(default_factory=ScalingSpec)
    features: FeatureSpec = Field(default_factory=FeatureSpec)

    @model_validator(mode="after")
    def unique_pools(self):
        names = [pool.name for pool in self.worker_pools]
        if len(names) != len(set(names)):
            raise ValueError("worker pool names must be unique")
        if self.control_plane.replicas % 2 == 0:
            raise ValueError("control plane replicas must be odd")
        return self


OperationKind = Literal["CREATE", "UPDATE", "SCALE", "UPGRADE", "DELETE"]
