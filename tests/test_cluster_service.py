from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from platform_service.application.cluster_service import ClusterService
from platform_service.application.reconciliation import CommandProcessor
from platform_service.domain.spec import ClusterSpec, WorkerNodeTypeSpec
from platform_service.infrastructure.database import (
    Base,
    Cluster,
    ClusterRevision,
    ClusterStatus,
    ConditionHistory,
    ManagementCluster,
    NodeProfile,
    Operation,
    Organization,
    OutboxEvent,
    Principal,
    Project,
    ProviderReference,
    ResourceStatus,
)
from platform_service.workers import fake_observer


def persisted_cluster_service():
    """Build the smallest relational graph needed for lifecycle service tests."""

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = Session(engine)
    principal = Principal(
        issuer="https://issuer.example",
        external_subject="cluster-admin",
        principal_type="human",
        enabled=True,
    )
    organization = Organization(name="org", display_name="Organization")
    management = ManagementCluster(
        name="management",
        provider="openstack",
        region="region",
        executor_compatibility="0.1",
    )
    session.add_all([principal, organization, management])
    session.flush()
    project = Project(
        organization_id=organization.id,
        name="project",
        namespace="project",
        enabled=True,
    )
    session.add(project)
    session.flush()
    provider = ProviderReference(
        project_id=project.id,
        provider="openstack",
        name="provider",
        secret_reference="secret/ref",
        configuration={},
    )
    session.add(provider)
    session.flush()
    session.add_all(
        [
            NodeProfile(
                project_id=project.id,
                provider_reference_id=provider.id,
                name="control",
                specification={},
            ),
            NodeProfile(
                project_id=project.id,
                provider_reference_id=provider.id,
                name="compute",
                specification={},
            ),
            NodeProfile(
                project_id=project.id,
                provider_reference_id=provider.id,
                name="gpu",
                specification={},
            ),
        ]
    )
    session.commit()
    return session, principal, project, management, provider


def desired_spec():
    return ClusterSpec.model_validate(
        {
            "kubernetes": {"version": "v1.31.1"},
            "control_plane": {"node_profile": "control"},
            "worker_node_types": [{"name": "workers", "replicas": 2, "node_profile": "compute"}],
        }
    )


def test_upgrade_delegates_revision_creation_to_application_service():
    cluster_id = uuid4()
    session = Mock()
    session.get.return_value = SimpleNamespace(desired_revision=4)
    session.scalar.return_value = SimpleNamespace(
        spec={
            "kubernetes": {"version": "v1.30.1"},
            "control_plane": {"replicas": 3, "node_profile": "control"},
            "worker_node_types": [{"name": "workers", "replicas": 3, "node_profile": "compute"}],
        }
    )
    service = ClusterService(session)
    service.revise = Mock(return_value=("cluster", "operation"))

    result = service.upgrade(
        cluster_id=cluster_id,
        version="v1.31.2",
        actor_id=uuid4(),
        permissions={"cluster.upgrade"},
        request_id="request-1",
    )

    assert result == ("cluster", "operation")
    session.get.assert_called_once_with(Cluster, cluster_id)
    revision = service.revise.call_args.kwargs
    assert revision["kind"] == "UPGRADE"
    assert revision["reason"] == "upgrade to v1.31.2"
    assert revision["spec"].kubernetes.version == "v1.31.2"


def test_scale_normalizes_legacy_worker_pool_revision():
    cluster_id = uuid4()
    session = Mock()
    session.get.return_value = SimpleNamespace(desired_revision=2)
    session.execute.return_value.scalar_one.return_value = SimpleNamespace(
        spec={
            "kubernetes": {"version": "v1.30.1"},
            "control_plane": {"replicas": 3, "node_profile": "control"},
            "worker_pools": [{"name": "compute", "replicas": 2, "node_profile": "compute"}],
        }
    )
    service = ClusterService(session)
    service.revise = Mock(return_value=("cluster", "operation"))

    service.scale(
        cluster_id=cluster_id,
        pool="compute",
        replicas=5,
        actor_id=uuid4(),
        permissions={"cluster.scale"},
        request_id="request-2",
    )

    revision = service.revise.call_args.kwargs
    assert revision["spec"].worker_node_types[0].replicas == 5
    assert "worker_node_types" in revision["spec"].model_dump()


def test_intent_persists_generated_event_id_in_outbox_payload():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    management_cluster_id = uuid4()
    cluster = SimpleNamespace(
        id=uuid4(),
        project_id=uuid4(),
        management_cluster_id=management_cluster_id,
    )

    with Session(engine) as session:
        session.add(
            ManagementCluster(
                id=management_cluster_id,
                name="local-mgmt",
                provider="openstack",
                region="local",
                executor_compatibility="0.1",
            )
        )
        session.commit()

        ClusterService(session)._intent(
            cluster=cluster,
            organization_id=None,
            kind="CREATE",
            revision=1,
            actor_id=uuid4(),
            request_id="request-3",
            source_ip=None,
        )
        session.commit()
        session.expire_all()

        event = session.scalar(select(OutboxEvent))

        assert event.payload["event_id"] == str(event.id)


def test_worker_node_type_creation_appends_revision_and_operation():
    session, principal, project, management, provider = persisted_cluster_service()
    service = ClusterService(session)
    cluster, _ = service.create(
        project_id=project.id,
        name="demo",
        management_cluster_id=management.id,
        provider_reference_id=provider.id,
        spec=desired_spec(),
        actor_id=principal.id,
        permissions={"cluster.create"},
        request_id="create",
    )

    _, operation = service.add_worker_node_type(
        cluster_id=cluster.id,
        worker_node_type=WorkerNodeTypeSpec(
            name="gpu", replicas=1, node_profile="gpu", role="worker"
        ),
        actor_id=principal.id,
        permissions={"cluster.update"},
        request_id="add-gpu",
    )

    revision = session.scalar(
        select(ClusterRevision).where(
            ClusterRevision.cluster_id == cluster.id,
            ClusterRevision.number == 2,
        )
    )
    assert operation.kind == "UPDATE"
    assert [item["name"] for item in revision.spec["worker_node_types"]] == ["workers", "gpu"]


def test_delete_is_an_asynchronous_revisioned_operation():
    session, principal, project, management, provider = persisted_cluster_service()
    service = ClusterService(session)
    cluster, _ = service.create(
        project_id=project.id,
        name="demo",
        management_cluster_id=management.id,
        provider_reference_id=provider.id,
        spec=desired_spec(),
        actor_id=principal.id,
        permissions={"cluster.create"},
        request_id="create",
    )

    _, operation = service.delete(
        cluster_id=cluster.id,
        actor_id=principal.id,
        permissions={"cluster.delete"},
        request_id="delete",
    )

    assert operation.kind == "DELETE"
    assert operation.state == "ACCEPTED"
    assert operation.target_revision == 2
    assert session.scalar(select(Operation).where(Operation.id == operation.id)) is operation
    assert session.get(Cluster, cluster.id).deleted_at is None


def test_delete_command_uses_adapter_delete_instead_of_compile():
    session, principal, project, management, provider = persisted_cluster_service()
    service = ClusterService(session)
    cluster, _ = service.create(
        project_id=project.id,
        name="demo",
        management_cluster_id=management.id,
        provider_reference_id=provider.id,
        spec=desired_spec(),
        actor_id=principal.id,
        permissions={"cluster.create"},
        request_id="create",
    )
    _, operation = service.delete(
        cluster_id=cluster.id,
        actor_id=principal.id,
        permissions={"cluster.delete"},
        request_id="delete",
    )
    event = session.scalar(
        select(OutboxEvent).where(
            OutboxEvent.payload["operation_id"].as_string() == str(operation.id)
        )
    )
    compiler = Mock()
    adapter = Mock()

    CommandProcessor(session, compiler, adapter, executor_id="executor").process(event.payload)

    adapter.delete.assert_called_once_with(project.namespace, cluster.name)
    adapter.apply.assert_not_called()
    compiler.compile.assert_not_called()
    assert operation.state == "RECONCILING"
    assert cluster.applied_revision == operation.target_revision


def test_fake_observer_finishes_delete_and_retains_tombstone(monkeypatch):
    session, principal, project, management, provider = persisted_cluster_service()
    service = ClusterService(session)
    cluster, _ = service.create(
        project_id=project.id,
        name="demo",
        management_cluster_id=management.id,
        provider_reference_id=provider.id,
        spec=desired_spec(),
        actor_id=principal.id,
        permissions={"cluster.create"},
        request_id="create",
    )
    _, operation = service.delete(
        cluster_id=cluster.id,
        actor_id=principal.id,
        permissions={"cluster.delete"},
        request_id="delete",
    )
    operation.state = "RECONCILING"
    resource = ResourceStatus(
        cluster_id=cluster.id,
        kind="Cluster",
        namespace=project.namespace,
        name=cluster.name,
        uid="resource-uid",
        conditions=[],
        raw_status={},
    )
    session.add_all(
        [
            resource,
            ClusterStatus(
                cluster_id=cluster.id,
                health={"availability": "AVAILABLE"},
                management_connectivity="CONNECTED",
            ),
        ]
    )
    session.flush()
    session.add(
        ConditionHistory(
            resource_status_id=resource.id,
            condition_type="Ready",
            status="True",
        )
    )
    session.commit()
    cluster_id = cluster.id
    operation_id = operation.id
    monkeypatch.setattr(fake_observer, "SessionLocal", sessionmaker(session.get_bind()))
    session.close()

    assert fake_observer.observe_once() == 1

    with fake_observer.SessionLocal() as check:
        deleted_cluster = check.get(Cluster, cluster_id)
        completed_operation = check.get(Operation, operation_id)
        assert deleted_cluster.deleted_at is not None
        assert deleted_cluster.observed_revision == completed_operation.target_revision
        assert completed_operation.state == "SUCCEEDED"
        assert check.get(ClusterStatus, cluster_id) is None
        assert (
            check.scalar(select(ResourceStatus).where(ResourceStatus.cluster_id == cluster_id))
            is None
        )
