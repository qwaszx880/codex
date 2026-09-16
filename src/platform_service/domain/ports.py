"""Replaceable repository, provider, messaging, metrics, and secret-store ports."""

from abc import ABC, abstractmethod
from typing import Any, Protocol
from uuid import UUID

from .spec import ClusterSpec


class ClusterRepository(Protocol):
    def get(self, cluster_id: UUID): ...
    def list_for_project(self, project_id: UUID): ...
    def add(self, cluster): ...


class OperationRepository(Protocol):
    def add(self, operation): ...


class IAMRepository(Protocol):
    def permissions_for(self, principal_id: UUID, project_id: UUID) -> set[str]: ...


class AuditRepository(Protocol):
    def add(self, event): ...


class EventPublisher(Protocol):
    def publish(self, event_id: UUID, routing_key: str, payload: dict[str, Any]) -> None: ...


class TaskConsumer(Protocol):
    def consume(self) -> None: ...


class ClusterCompiler(ABC):
    @abstractmethod
    def compile(self, name: str, namespace: str, spec: ClusterSpec) -> list[dict[str, Any]]: ...


class ManagementClusterAdapter(Protocol):
    def apply(self, resources: list[dict[str, Any]]) -> None: ...
    def delete(self, namespace: str, name: str) -> None: ...


class MetricsBackend(Protocol):
    def query(self, project_id: UUID, cluster_id: UUID, query: str) -> dict[str, Any]: ...


class SecretStore(Protocol):
    def read(self, reference: str) -> dict[str, str]: ...
