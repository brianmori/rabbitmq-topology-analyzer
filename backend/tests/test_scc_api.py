import json
import logging
import time
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from rabbitmq_management_sdk import ClusterAuditor
from rabbitmq_management_sdk.topology import node_sort_key

from analyses.snapshots import SnapshotMetadata, create_analysis_snapshot
from core import state
from core.state import (
    AnalysisSource,
    CaptureCompleteness,
    configure_analysis_store,
    replace_static_analyses,
)
from main import app
from topology.wire_ids import node_wire_id

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from pytest import LogCaptureFixture


@pytest.fixture(autouse=True)
def isolated_application_state() -> Iterator[None]:
    app.dependency_overrides.clear()
    state._session_store.clear()
    replace_static_analyses({})
    configure_analysis_store()
    yield
    app.dependency_overrides.clear()
    state._session_store.clear()
    replace_static_analyses({})


def _cyclic_definitions() -> dict[str, object]:
    return {
        "users": [],
        "vhosts": [{"name": "/"}],
        "permissions": [],
        "topic_permissions": [],
        "global_parameters": [
            {"name": "internal_cluster_id", "value": "cluster-scc"},
            {"name": "cluster_name", "value": "SCC broker"},
        ],
        "parameters": [],
        "policies": [],
        "queues": [
            {
                "name": "loop-q",
                "vhost": "/",
                "durable": True,
                "auto_delete": False,
                "arguments": {
                    "x-queue-type": "classic",
                    "x-dead-letter-exchange": "loop-x",
                },
            },
        ],
        "exchanges": [
            {
                "name": "loop-x",
                "vhost": "/",
                "type": "direct",
                "durable": True,
                "auto_delete": False,
                "internal": False,
                "arguments": {},
            },
        ],
        "bindings": [
            {
                "source": "loop-x",
                "vhost": "/",
                "destination": "loop-q",
                "destination_type": "queue",
                "routing_key": "loop",
                "arguments": {"password": "must-not-leak"},
            },
        ],
    }


def _snapshot(tmp_path: Path) -> tuple[state.AnalysisSnapshot, ClusterAuditor]:
    definitions_path = tmp_path / "definitions.json"
    definitions_path.write_text(json.dumps(_cyclic_definitions()), encoding="utf-8")
    auditor = ClusterAuditor.from_files(definitions_path=definitions_path)
    snapshot = create_analysis_snapshot(
        auditor.topology,
        SnapshotMetadata(
            analysis_id="scc-fixture",
            source=AnalysisSource.STATIC,
            label="SCC fixture",
            capture_completeness=CaptureCompleteness.DEFINITIONS_ONLY,
            snapshot_revision="sha256:" + "b" * 64,
            cluster_tags={},
            in_cluster_amqp_hosts=(),
            vhost_count=1,
        ),
    )
    return snapshot, auditor


def test_scc_api_runs_public_sdk_operation_and_replays_terminal_result(tmp_path: Path) -> None:
    snapshot, auditor = _snapshot(tmp_path)
    owner_token = str(uuid4())
    idempotency_key = str(uuid4())
    headers = {
        "X-Session-Token": owner_token,
        "Idempotency-Key": idempotency_key,
    }
    request = {
        "source": "static",
        "analysisId": snapshot.analysis_id,
        "snapshotRevision": snapshot.snapshot_revision,
    }

    with TestClient(app) as client:
        replace_static_analyses({snapshot.analysis_id: snapshot})
        started = client.post("/api/scc-runs", json=request, headers=headers)
        assert started.status_code == 202
        run_id = started.json()["runId"]

        deadline = time.monotonic() + 5
        response = started
        while response.json()["status"] not in {"succeeded", "failed", "cancelled"}:
            assert time.monotonic() < deadline
            time.sleep(0.02)
            response = client.get(
                f"/api/scc-runs/{run_id}",
                params={"snapshot_revision": snapshot.snapshot_revision},
                headers={"X-Session-Token": owner_token},
            )

        replay = client.post("/api/scc-runs", json=request, headers=headers)
        other_owner = client.get(
            f"/api/scc-runs/{run_id}",
            params={"snapshot_revision": snapshot.snapshot_revision},
            headers={"X-Session-Token": str(uuid4())},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "succeeded"
    assert body["operation"] == "cyclic_scc"
    assert body["snapshotRevision"] == snapshot.snapshot_revision
    expected_components = auditor.cyclic_components()
    assert len(body["result"]["components"]) == len(expected_components)
    assert [component["nodeIds"] for component in body["result"]["components"]] == [
        [node_wire_id(node) for node in sorted(component.nodes, key=node_sort_key)]
        for component in expected_components
    ]
    assert all(component["isCyclic"] is True for component in body["result"]["components"])
    assert "must-not-leak" not in json.dumps(body)
    assert replay.status_code == 200
    assert replay.json()["runId"] == body["runId"]
    assert replay.json()["result"] == body["result"]
    assert replay.json()["resultExpiresAt"] >= body["resultExpiresAt"]
    assert other_owner.status_code == 404


def test_scc_api_validates_revision_idempotency_and_owner_headers(tmp_path: Path) -> None:
    snapshot, _auditor = _snapshot(tmp_path)
    request = {
        "source": "static",
        "analysisId": snapshot.analysis_id,
        "snapshotRevision": snapshot.snapshot_revision,
    }

    with TestClient(app) as client:
        replace_static_analyses({snapshot.analysis_id: snapshot})
        missing_owner = client.post(
            "/api/scc-runs",
            json=request,
            headers={"Idempotency-Key": str(uuid4())},
        )
        bad_key = client.post(
            "/api/scc-runs",
            json=request,
            headers={"X-Session-Token": str(uuid4()), "Idempotency-Key": "not-a-uuid"},
        )
        stale = client.post(
            "/api/scc-runs",
            json={**request, "snapshotRevision": "sha256:" + "c" * 64},
            headers={
                "X-Session-Token": str(uuid4()),
                "Idempotency-Key": str(uuid4()),
            },
        )
        unknown_field = client.post(
            "/api/scc-runs",
            json={**request, "maxCycles": 20},
            headers={
                "X-Session-Token": str(uuid4()),
                "Idempotency-Key": str(uuid4()),
            },
        )

    assert missing_owner.status_code == 400
    assert bad_key.status_code == 422
    assert stale.status_code == 409
    assert unknown_field.status_code == 422


def test_scc_validation_does_not_reflect_unknown_json_keys(
    tmp_path: Path, caplog: LogCaptureFixture
) -> None:
    snapshot, _auditor = _snapshot(tmp_path)
    request = {
        "source": "static",
        "analysisId": snapshot.analysis_id,
        "snapshotRevision": snapshot.snapshot_revision,
    }
    headers = {
        "X-Session-Token": str(uuid4()),
        "Idempotency-Key": str(uuid4()),
    }
    unknown_keys = (
        ("amqp://review-user:review-secret@review.invalid/", "review-secret"),
        ("control\r\nleaked-control-secret\t\x00", "leaked-control-secret"),
        ("x" * 4096 + "long-key-secret", "long-key-secret"),
    )

    caplog.set_level(logging.DEBUG)
    with TestClient(app) as client:
        replace_static_analyses({snapshot.analysis_id: snapshot})
        for key, marker in unknown_keys:
            response = client.post("/api/scc-runs", json={**request, key: 1}, headers=headers)
            assert response.status_code == 422
            assert response.json()["error"]["issues"] == [
                {"field": "body", "code": "extra_forbidden", "message": "Unknown field."}
            ]
            assert key not in response.text
            assert marker not in response.text

        missing = client.post(
            "/api/scc-runs", json={"source": "static", "snapshotRevision": snapshot.snapshot_revision}, headers=headers
        )
        invalid = client.post(
            "/api/scc-runs", json={**request, "source": "invalid"}, headers=headers
        )

    assert missing.status_code == 422
    assert missing.json()["error"]["issues"] == [
        {"field": "analysisId", "code": "missing", "message": "This field is required."}
    ]
    assert invalid.status_code == 422
    assert invalid.json()["error"]["issues"][0]["field"] == "source"
    for _key, marker in unknown_keys:
        assert marker not in caplog.text
