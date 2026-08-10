from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from rabbitmq_management_sdk.topology import ClusterTopology

import definitions.router as definitions_router
from core.security import AppRole, AuthMode, Principal, get_principal
from core.session import OwnerContext
from core.state import AnalysisCatalogueEntry, AnalysisSnapshot, SessionPublication
from definitions.sdk_bridge import LoadedDefinitions
from main import app

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def client() -> Iterator[TestClient]:
    app.dependency_overrides.clear()
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _principal_with(*roles: AppRole) -> Principal:
    return Principal(
        principal_id="test-principal",
        roles=frozenset(roles),
        auth_mode=AuthMode.JWT,
    )


def test_catalogue_allows_viewer_without_browser_session(client: TestClient) -> None:
    async def viewer_principal() -> Principal:
        return _principal_with(AppRole.VIEWER)

    app.dependency_overrides[get_principal] = viewer_principal

    response = client.get("/api/analyses")

    assert response.status_code == 200
    assert response.json()["session"] == []


def test_access_reports_disabled_mode_analyst_capabilities(client: TestClient) -> None:
    response = client.get("/api/access")

    assert response.status_code == 200
    assert response.json() == {
        "authMode": "none",
        "roles": ["analyst"],
        "capabilities": {
            "viewTopology": True,
            "runGraphOperations": True,
            "manageUploads": True,
        },
    }


def test_access_flattens_viewer_permissions_into_capabilities(client: TestClient) -> None:
    async def viewer_principal() -> Principal:
        return _principal_with(AppRole.VIEWER)

    app.dependency_overrides[get_principal] = viewer_principal

    response = client.get("/api/access")

    assert response.status_code == 200
    assert response.json() == {
        "authMode": "jwt",
        "roles": ["viewer"],
        "capabilities": {
            "viewTopology": True,
            "runGraphOperations": True,
            "manageUploads": False,
        },
    }


def test_catalogue_rejects_principal_without_viewer_access(client: TestClient) -> None:
    async def unauthorized_principal() -> Principal:
        return _principal_with()

    app.dependency_overrides[get_principal] = unauthorized_principal

    response = client.get("/api/analyses")

    assert response.status_code == 403


def test_catalogue_rejects_malformed_supplied_browser_session(client: TestClient) -> None:
    response = client.get(
        "/api/analyses",
        headers={"X-Session-Token": "not-a-uuid"},
    )

    assert response.status_code == 400


def test_upload_rejects_viewer_before_calling_sdk(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def viewer_principal() -> Principal:
        return _principal_with(AppRole.VIEWER)

    def unexpected_load(_dump_path: Path, **_kwargs: object) -> LoadedDefinitions:
        pytest.fail("The SDK bridge must not run for a forbidden upload.")

    app.dependency_overrides[get_principal] = viewer_principal
    monkeypatch.setattr(definitions_router, "load_definitions", unexpected_load)

    response = client.post(
        "/api/definitions/upload",
        data={"analysis_id": "forbidden-upload"},
        files={"definitions_file": ("definitions.json", b"{}", "application/json")},
        headers={"X-Session-Token": str(uuid4())},
    )

    assert response.status_code == 403


def test_upload_requires_browser_session_for_analyst(client: TestClient) -> None:
    response = client.post(
        "/api/definitions/upload",
        data={"analysis_id": "missing-owner"},
        files={"definitions_file": ("definitions.json", b"{}", "application/json")},
    )

    assert response.status_code == 400


def test_upload_passes_principal_bound_owner_to_storage(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topology = ClusterTopology(
        exchanges=frozenset(),
        queues=frozenset(),
        shovels=frozenset(),
        edges=frozenset(),
        cluster_id="rabbitmq-cluster-id",
        cluster_name="Test cluster",
    )
    published: list[tuple[OwnerContext, AnalysisSnapshot]] = []
    browser_session_id = str(uuid4())

    async def analyst_principal() -> Principal:
        return _principal_with(AppRole.ANALYST)

    def load_definitions(_dump_path: Path, **_kwargs: object) -> LoadedDefinitions:
        return LoadedDefinitions(
            topology=topology,
            vhost_count=3,
            cluster_tags={"environment": "test"},
        )

    def publish_session_analysis(
        owner_context: OwnerContext,
        snapshot: AnalysisSnapshot,
    ) -> SessionPublication:
        published.append((owner_context, snapshot))
        return SessionPublication(
            entry=AnalysisCatalogueEntry(
                snapshot=snapshot,
                expires_at=datetime.now(UTC) + timedelta(hours=8),
            ),
            replaced=False,
        )

    app.dependency_overrides[get_principal] = analyst_principal
    monkeypatch.setattr(definitions_router, "load_definitions", load_definitions)
    monkeypatch.setattr(
        definitions_router,
        "publish_session_analysis",
        publish_session_analysis,
    )

    response = client.post(
        "/api/definitions/upload",
        data={"analysis_id": "owned-upload"},
        files={"definitions_file": ("definitions.json", b"{}", "application/json")},
        headers={"X-Session-Token": browser_session_id.upper()},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["replaced"] is False
    assert body["analysis"]["analysisId"] == "owned-upload"
    assert body["analysis"]["source"] == "session"
    assert body["analysis"]["captureCompleteness"] == "definitions_only"
    assert body["analysis"]["cluster"] == {
        "clusterId": "rabbitmq-cluster-id",
        "clusterName": "Test cluster",
        "clusterTags": {"environment": "test"},
        "vhostCount": 3,
        "nodeCount": 0,
        "edgeCount": 0,
    }
    assert len(published) == 1
    owner, snapshot = published[0]
    assert owner == OwnerContext(
        principal_id="test-principal",
        session_id=browser_session_id,
    )
    assert snapshot.analysis_id == "owned-upload"
    assert snapshot.topology is topology
