import json
from pathlib import Path

from platform_service.infrastructure.bootstrap import IDS, PERMISSIONS
from platform_service.main import app
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


def test_compose_supports_a_public_vm_host_and_configurable_bind_address():
    compose = Path("compose.yaml").read_text()
    realm = json.loads(Path("dev/keycloak/realm.json").read_text())
    client = next(client for client in realm["clients"] if client["clientId"] == "cluster-platform")

    assert "http://${PLATFORM_PUBLIC_HOST:-localhost}:8081/realms/platform" in compose
    assert "--hostname=http://${PLATFORM_PUBLIC_HOST:-localhost}:8081" in compose
    assert compose.count("${PLATFORM_BIND_ADDRESS:-0.0.0.0}:") == 7
    assert client["redirectUris"] == ["*"]
    assert client["webOrigins"] == ["*"]


def test_local_browser_client_uses_authorization_code_with_pkce_and_api_audience():
    realm = json.loads(Path("dev/keycloak/realm.json").read_text())
    client = next(
        client for client in realm["clients"] if client["clientId"] == "cluster-platform-frontend"
    )
    frontend_config = json.loads(Path("dev/frontend/oidc-config.example.json").read_text())

    assert client["publicClient"] is True
    assert client["standardFlowEnabled"] is True
    assert client["implicitFlowEnabled"] is False
    assert client["directAccessGrantsEnabled"] is False
    assert client["attributes"]["pkce.code.challenge.method"] == "S256"
    assert client["redirectUris"] == [
        "http://localhost:3000/*",
        "http://127.0.0.1:3000/*",
    ]
    assert client["webOrigins"] == [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]
    assert any(
        mapper["config"].get("included.client.audience") == "cluster-platform"
        for mapper in client["protocolMappers"]
    )
    assert frontend_config["client_id"] == client["clientId"]
    assert frontend_config["response_type"] == "code"


def test_fake_observer_emits_success_and_failure_conditions():
    ready = _condition("Cluster", failed=False)
    failed = _condition("OpenStackCluster", failed=True)

    assert ready["status"] == "True"
    assert ready["reason"] == "Reconciled"
    assert failed["status"] == "False"
    assert failed["reason"] == "SimulatedFailure"


def test_openapi_exposes_administration_and_async_cluster_mutations():
    paths = app.openapi()["paths"]

    assert paths["/v1/organizations/{organization_id}/principals"]["post"]["responses"]["201"]
    assert paths["/v1/organizations/{organization_id}/roles"]["get"]["responses"]["200"]
    assert paths["/v1/organizations/{organization_id}/projects"]["post"]["responses"]["201"]
    assert paths["/v1/projects/{project_id}/provider-references"]["post"]["responses"]["201"]
    assert paths["/v1/projects/{project_id}/node-profiles"]["post"]["responses"]["201"]
    assert paths["/v1/clusters/{cluster_id}"]["patch"]["responses"]["202"]
    assert paths["/v1/clusters/{cluster_id}"]["delete"]["responses"]["202"]
    assert paths["/v1/clusters/{cluster_id}/worker-node-types"]["post"]["responses"]["202"]

    provider_fields = app.openapi()["components"]["schemas"]["ProviderReferenceView"]["properties"]
    assert "secret_reference" not in provider_fields
