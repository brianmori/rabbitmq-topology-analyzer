import json
from dataclasses import replace
from time import perf_counter
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from rabbitmq_management_sdk import ClusterAuditor
from rabbitmq_management_sdk.topology import NodeId, NodeKind

from analyses.snapshots import SnapshotMetadata, create_analysis_snapshot
from core import state
from core.security import AppRole, AuthMode, Principal, get_principal
from core.session import OwnerContext
from core.state import (
    AnalysisSource,
    CaptureCompleteness,
    configure_analysis_store,
    publish_session_analysis,
    replace_static_analyses,
)
from main import app
from topology.wire_ids import decode_node_wire_id, node_wire_id

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def isolated_application_state() -> Iterator[None]:
    """Keep graph endpoint state and dependency overrides local to each test."""
    app.dependency_overrides.clear()
    state._session_store.clear()
    replace_static_analyses({})
    configure_analysis_store()
    yield
    app.dependency_overrides.clear()
    state._session_store.clear()
    replace_static_analyses({})


def _definitions_fixture() -> dict[str, object]:
    return {
        "users": [],
        "vhosts": [
            {
                "name": "orders",
                "description": "",
                "tags": [],
                "metadata": {
                    "description": "",
                    "tags": [],
                    "default_queue_type": "classic",
                },
            },
        ],
        "permissions": [],
        "topic_permissions": [],
        "global_parameters": [
            {"name": "internal_cluster_id", "value": "cluster-orders"},
            {"name": "cluster_name", "value": "Orders broker"},
            {"name": "cluster_tags", "value": {"environment": "test"}},
        ],
        "parameters": [
            {
                "vhost": "orders",
                "component": "shovel",
                "name": "ship-orders",
                "value": {
                    "src-protocol": "amqp091",
                    "src-uri": "amqp://user:super-secret@secret.internal/orders",
                    "src-queue": "orders-q",
                    "dest-protocol": "amqp091",
                    "dest-uri": "amqp://user:super-secret@secret.internal/orders",
                    "dest-queue": "orders-q",
                },
            },
        ],
        "policies": [],
        "queues": [
            {
                "name": "orders-q",
                "vhost": "orders",
                "durable": True,
                "auto_delete": False,
                "arguments": {
                    "x-queue-type": "classic",
                    "x-dead-letter-exchange": "missing-target",
                },
            },
            {
                "name": "default-q",
                "vhost": "orders",
                "durable": True,
                "auto_delete": False,
                "arguments": {
                    "x-queue-type": "classic",
                    "x-dead-letter-exchange": "",
                    "x-dead-letter-routing-key": "default-q",
                },
            },
        ],
        "exchanges": [
            {
                "name": "orders-x",
                "vhost": "orders",
                "type": "topic",
                "durable": True,
                "auto_delete": False,
                "internal": False,
                "arguments": {"alternate-exchange": "missing-target"},
            },
        ],
        "bindings": [
            {
                "source": "orders-x",
                "vhost": "orders",
                "destination": "orders-q",
                "destination_type": "queue",
                "routing_key": "orders.*",
                "arguments": {"credential": "binding-secret-a"},
            },
            {
                "source": "orders-x",
                "vhost": "orders",
                "destination": "orders-q",
                "destination_type": "queue",
                "routing_key": "orders.*",
                "arguments": {"credential": "binding-secret-b"},
            },
        ],
    }


def _sdk_loaded_snapshot(tmp_path: Path) -> state.AnalysisSnapshot:
    definitions_path = tmp_path / "definitions.json"
    definitions_path.write_text(json.dumps(_definitions_fixture()), encoding="utf-8")
    auditor = ClusterAuditor.from_files(
        definitions_path=definitions_path,
        in_cluster_amqp_hosts=("secret.internal",),
        cluster_label="Orders fixture",
    )
    return create_analysis_snapshot(
        auditor.topology,
        SnapshotMetadata(
            analysis_id="orders-fixture",
            source=AnalysisSource.STATIC,
            label="Orders fixture",
            capture_completeness=CaptureCompleteness.DEFINITIONS_ONLY,
            snapshot_revision="sha256:" + "a" * 64,
            cluster_tags=dict(auditor.definitions.cluster_tags or {}),
            in_cluster_amqp_hosts=("secret.internal",),
            vhost_count=len(auditor.definitions.vhosts),
        ),
    )


def test_node_wire_ids_are_versioned_reversible_and_missing_safe() -> None:
    sdk_id = NodeId(
        cluster_id=None,
        vhost="租户/東京",
        name="orders queue",
        kind=NodeKind.QUEUE,
    )

    captured = node_wire_id(sdk_id)
    missing = node_wire_id(sdk_id, is_missing=True)

    assert captured.startswith("n1.")
    assert missing.startswith("m1.")
    assert captured != missing
    assert decode_node_wire_id(captured).node_id == sdk_id
    assert decode_node_wire_id(captured).is_missing is False
    assert decode_node_wire_id(missing).node_id == sdk_id
    assert decode_node_wire_id(missing).is_missing is True


def test_topology_endpoint_serializes_complete_safe_sdk_graph(tmp_path: Path) -> None:
    snapshot = _sdk_loaded_snapshot(tmp_path)

    with TestClient(app) as client:
        replace_static_analyses({snapshot.analysis_id: snapshot})
        started_at = perf_counter()
        response = client.get(
            "/api/topology",
            params={"source": "static", "analysis_id": snapshot.analysis_id},
        )
        elapsed = perf_counter() - started_at
        repeated_response = client.get(
            "/api/topology",
            params={"source": "static", "analysis_id": snapshot.analysis_id},
        )

    assert response.status_code == 200
    assert repeated_response.status_code == 200
    assert elapsed < 2
    body = response.json()
    assert repeated_response.json() == body
    assert body["source"] == "static"
    assert body["analysisId"] == snapshot.analysis_id
    assert body["snapshotRevision"] == snapshot.snapshot_revision
    assert body["cluster"] == {
        "clusterId": "cluster-orders",
        "clusterName": "Orders broker",
        "clusterTags": {"environment": "test"},
        "vhostCount": 1,
        "nodeCount": len(snapshot.topology.nodes),
        "edgeCount": len(snapshot.topology.edges),
    }

    assert len(body["edges"]) == len(snapshot.topology.edges)
    assert len({edge["id"] for edge in body["edges"]}) == len(body["edges"])
    assert {edge["id"] for edge in body["edges"]} == {
        f"e1.{index}" for index in range(len(body["edges"]))
    }
    binding_edges = [edge for edge in body["edges"] if edge["type"] == "binding"]
    assert len(binding_edges) == 2
    assert binding_edges[0]["source"] == binding_edges[1]["source"]
    assert binding_edges[0]["target"] == binding_edges[1]["target"]
    assert binding_edges[0]["id"] != binding_edges[1]["id"]

    missing_nodes = [node for node in body["nodes"] if node["data"]["isMissing"]]
    assert len(missing_nodes) == 1
    assert len(body["nodes"]) == len(snapshot.topology.nodes) + 1
    assert missing_nodes[0]["id"].startswith("m1.")
    assert missing_nodes[0]["data"] == {
        "kind": "exchange",
        "name": "missing-target",
        "vhost": "orders",
        "clusterId": "cluster-orders",
        "isMissing": True,
        "exchangeType": None,
        "durable": None,
        "internal": None,
        "isDefault": None,
    }
    assert (
        sum(missing_nodes[0]["id"] in {edge["source"], edge["target"]} for edge in body["edges"])
        == 2
    )

    default_exchange = next(
        node for node in body["nodes"] if node["type"] == "exchange" and node["data"]["name"] == ""
    )
    assert default_exchange["data"]["isDefault"] is True
    assert all(node["position"] == {"x": 0.0, "y": 0.0} for node in body["nodes"])

    shovel = next(node for node in body["nodes"] if node["type"] == "shovel")
    assert shovel["data"]["source"] == {
        "vhost": "orders",
        "resourceName": "orders-q",
        "resourceKind": "queue",
        "routingKey": None,
        "isConfirmedLocal": True,
    }
    serialized = json.dumps(body)
    assert "binding-secret" not in serialized
    assert "super-secret" not in serialized
    assert "secret.internal" not in serialized
    assert "amqp091" not in serialized


def test_topology_endpoint_enforces_source_ownership_and_viewer_role(tmp_path: Path) -> None:
    snapshot = _sdk_loaded_snapshot(tmp_path)

    with TestClient(app) as client:
        replace_static_analyses({snapshot.analysis_id: snapshot})
        missing_session = client.get(
            "/api/topology",
            params={"source": "session", "analysis_id": snapshot.analysis_id},
        )
        unknown = client.get(
            "/api/topology",
            params={"source": "static", "analysis_id": "unknown"},
        )

        async def principal_without_role() -> Principal:
            return Principal(
                principal_id="no-role",
                roles=frozenset(),
                auth_mode=AuthMode.OIDC,
            )

        app.dependency_overrides[get_principal] = principal_without_role
        forbidden = client.get(
            "/api/topology",
            params={"source": "static", "analysis_id": snapshot.analysis_id},
        )

    assert missing_session.status_code == 400
    assert unknown.status_code == 404
    assert forbidden.status_code == 403


def test_topology_rejects_unknown_query_fields_for_a_real_sdk_snapshot(tmp_path: Path) -> None:
    snapshot = _sdk_loaded_snapshot(tmp_path)
    query = {"source": "static", "analysis_id": snapshot.analysis_id}
    with TestClient(app) as client:
        replace_static_analyses({snapshot.analysis_id: snapshot})
        valid = client.get("/api/topology", params=query)
        malformed = client.get("/api/topology", params={**query, "unexpected": "private-value"})
        repeated = client.get("/api/topology", params=query)

    assert valid.status_code == repeated.status_code == 200
    assert valid.json() == repeated.json()
    assert malformed.status_code == 422
    assert malformed.json()["error"]["issues"] == [
        {"field": "query", "code": "extra_forbidden", "message": "Unknown field."},
    ]


def test_topology_export_downloads_the_complete_safe_sdk_graph(tmp_path: Path) -> None:
    snapshot = _sdk_loaded_snapshot(tmp_path)
    query = {"source": "static", "analysis_id": snapshot.analysis_id}
    export_query = {**query, "snapshot_revision": snapshot.snapshot_revision}
    with TestClient(app) as client:
        replace_static_analyses({snapshot.analysis_id: snapshot})
        graph = client.get("/api/topology", params=query).json()
        download = client.get("/api/topology/export", params=export_query)
        repeated = client.get("/api/topology/export", params=export_query)
        missing_revision = client.get("/api/topology/export", params=query)

    assert download.status_code == 200
    assert download.headers["Content-Type"] == "application/json"
    assert download.headers["Content-Disposition"] == 'attachment; filename="orders-fixture.topology.json"'
    assert download.headers["Cache-Control"] == "no-store"
    assert repeated.content == download.content
    assert missing_revision.status_code == 422
    body = download.json()
    assert body == {
        "schemaVersion": 1,
        "source": "static",
        "analysisId": snapshot.analysis_id,
        "label": snapshot.label,
        "snapshotRevision": snapshot.snapshot_revision,
        "loadedAt": snapshot.loaded_at.isoformat().replace("+00:00", "Z"),
        "captureCompleteness": "definitions_only",
        "cluster": graph["cluster"],
        "vhosts": ["orders"],
        "nodes": [
            {"id": node["id"], "type": node["type"], "data": node["data"]}
            for node in graph["nodes"]
        ],
        "edges": graph["edges"],
    }
    for secret in ("binding-secret", "super-secret", "secret.internal", "amqp091"):
        assert secret not in download.text


def test_topology_export_enforces_viewer_ownership_and_revision(tmp_path: Path) -> None:
    snapshot = replace(_sdk_loaded_snapshot(tmp_path), source=AnalysisSource.SESSION)
    owner = OwnerContext(principal_id="export-viewer", session_id=str(uuid4()))
    query = {
        "source": "session",
        "analysis_id": snapshot.analysis_id,
        "snapshot_revision": snapshot.snapshot_revision,
    }

    async def viewer() -> Principal:
        return Principal(owner.principal_id, frozenset({AppRole.VIEWER}), AuthMode.NONE)

    async def no_role() -> Principal:
        return Principal(owner.principal_id, frozenset(), AuthMode.NONE)

    with TestClient(app) as client:
        publish_session_analysis(owner, snapshot)
        app.dependency_overrides[get_principal] = viewer
        headers = {"X-Session-Token": owner.session_id}
        download = client.get("/api/topology/export", params=query, headers=headers)
        conflict = client.get(
            "/api/topology/export",
            params={**query, "snapshot_revision": "sha256:" + "b" * 64},
            headers=headers,
        )
        other_owner = client.get(
            "/api/topology/export", params=query, headers={"X-Session-Token": str(uuid4())}
        )
        missing_session = client.get("/api/topology/export", params=query)
        app.dependency_overrides[get_principal] = no_role
        forbidden = client.get("/api/topology/export", params=query, headers=headers)

    assert download.status_code == 200
    assert download.json()["source"] == "session"
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "snapshot_revision_mismatch"
    assert other_owner.status_code == 404
    assert missing_session.status_code == 400
    assert forbidden.status_code == 403
    for error in (conflict, other_owner, missing_session, forbidden):
        assert "Content-Disposition" not in error.headers
