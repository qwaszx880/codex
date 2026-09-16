import json
from pathlib import Path

from platform_service.infrastructure.bootstrap import IDS, PERMISSIONS
from platform_service.workers.fake_observer import _condition


def test_local_oidc_identity_matches_seeded_principal_and_has_api_audience():
    realm = json.loads(Path("dev/keycloak/realm.json").read_text())
    user = next(user for user in realm["users"] if user["username"] == "developer")
    client = next(client for client in realm["clients"] if client["clientId"] == "cluster-platform")

    assert user["id"] == str(IDS["principal"])
    assert any(
        mapper["config"].get("included.client.audience") == "cluster-platform"
        for mapper in client["protocolMappers"]
    )
    assert {"cluster.create", "cluster.read", "cluster.scale", "cluster.upgrade"} <= set(
        PERMISSIONS
    )


def test_fake_observer_emits_success_and_failure_conditions():
    ready = _condition("Cluster", failed=False)
    failed = _condition("OpenStackCluster", failed=True)

    assert ready["status"] == "True"
    assert ready["reason"] == "Reconciled"
    assert failed["status"] == "False"
    assert failed["reason"] == "SimulatedFailure"
