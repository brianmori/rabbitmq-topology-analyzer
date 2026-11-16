import json
import time
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from rabbitmq_management_sdk import ClusterAuditor

from analyses.snapshots import SnapshotMetadata, create_analysis_snapshot
from core import state
from core.session import OwnerContext
from core.state import (
    AnalysisSource,
    CaptureCompleteness,
    configure_analysis_store,
    publish_session_analysis,
    replace_static_analyses,
)
from jobs.results import ResultRepository
from jobs.runtime import get_job_scheduler
from main import app
from topology.wire_ids import TopologyWireIds, node_wire_id

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


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


def _cycle_definitions() -> dict[str, object]:
    return {
        "users": [],
        "vhosts": [{"name": "/"}],
        "permissions": [],
        "topic_permissions": [],
        "global_parameters": [
            {"name": "internal_cluster_id", "value": "cluster-cycles"},
            {"name": "cluster_name", "value": "Cycle broker"},
        ],
        "parameters": [],
        "policies": [],
        "queues": [
            {
                "name": name,
                "vhost": "/",
                "durable": True,
                "auto_delete": False,
                "arguments": {
                    "x-queue-type": "classic",
                    "x-dead-letter-exchange": "loop-x",
                },
            }
            for name in ("direct-q", "long-q")
        ],
        "exchanges": [
            {
                "name": name,
                "vhost": "/",
                "type": "direct",
                "durable": True,
                "auto_delete": False,
                "internal": False,
                "arguments": {},
            }
            for name in ("loop-x", "middle-x")
        ],
        "bindings": [
            {
                "source": "loop-x",
                "vhost": "/",
                "destination": "direct-q",
                "destination_type": "queue",
                "routing_key": routing_key,
                "arguments": arguments,
            }
            for routing_key, arguments in (
                ("a", {"parallel": 1}),
                ("b", {"parallel": 2, "password": "must-not-leak"}),
            )
        ]
        + [
            {
                "source": "loop-x",
                "vhost": "/",
                "destination": "middle-x",
                "destination_type": "exchange",
                "routing_key": "long-1",
                "arguments": {},
            },
            {
                "source": "middle-x",
                "vhost": "/",
                "destination": "long-q",
                "destination_type": "queue",
                "routing_key": "long-2",
                "arguments": {},
            },
        ],
    }


def _snapshot(
    tmp_path: Path,
    source: AnalysisSource = AnalysisSource.STATIC,
) -> tuple[state.AnalysisSnapshot, ClusterAuditor]:
    definitions_path = tmp_path / "definitions.json"
    definitions_path.write_text(json.dumps(_cycle_definitions()), encoding="utf-8")
    auditor = ClusterAuditor.from_files(definitions_path=definitions_path)
    snapshot = create_analysis_snapshot(
        auditor.topology,
        SnapshotMetadata(
            analysis_id="cycle-fixture",
            source=source,
            label="Cycle fixture",
            capture_completeness=CaptureCompleteness.DEFINITIONS_ONLY,
            snapshot_revision="sha256:" + "d" * 64,
            cluster_tags={},
            in_cluster_amqp_hosts=(),
            vhost_count=1,
        ),
    )
    return snapshot, auditor


def _request(snapshot: state.AnalysisSnapshot, max_cycles: int = 0) -> dict[str, object]:
    return {
        "source": snapshot.source.value,
        "analysisId": snapshot.analysis_id,
        "snapshotRevision": snapshot.snapshot_revision,
        "maxCycles": max_cycles,
    }


def _wait_for_cycle_run(
    client: TestClient,
    run_id: str,
    snapshot_revision: str,
    owner_token: str,
) -> dict[str, object]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(
            f"/api/cycle-runs/{run_id}",
            params={"snapshot_revision": snapshot_revision},
            headers={"X-Session-Token": owner_token},
        )
        assert response.status_code == 200
        body = response.json()
        if body["status"] in {"succeeded", "failed", "cancelled"}:
            return body
        time.sleep(0.02)
    pytest.fail("cycle run did not reach a terminal state")


def _unexpected_whole_result_read(_repository: ResultRepository, _run_id: str) -> bytes:
    pytest.fail("cycle routes must use seekable indexed result reads")


def test_cycle_api_pages_and_resolves_exact_original_detail_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot, auditor = _snapshot(tmp_path)
    expected = auditor.structural_cycles(max_cycles=0)
    expected_wire_ids = TopologyWireIds.from_topology(snapshot.topology)
    owner_token = str(uuid4())
    idempotency_key = str(uuid4())
    headers = {
        "X-Session-Token": owner_token,
        "Idempotency-Key": idempotency_key,
    }

    monkeypatch.setattr(ResultRepository, "read", _unexpected_whole_result_read)

    with TestClient(app) as client:
        replace_static_analyses({snapshot.analysis_id: snapshot})
        started = client.post("/api/cycle-runs", json=_request(snapshot), headers=headers)
        assert started.status_code == 202
        run_id = started.json()["runId"]
        completed = _wait_for_cycle_run(client, run_id, snapshot.snapshot_revision, owner_token)

        default_page = client.get(
            f"/api/cycle-runs/{run_id}/cycles",
            params={"snapshot_revision": snapshot.snapshot_revision},
            headers={"X-Session-Token": owner_token},
        )
        sorted_page = client.get(
            f"/api/cycle-runs/{run_id}/cycles",
            params={
                "snapshot_revision": snapshot.snapshot_revision,
                "sort": "length_desc",
                "offset": 0,
                "limit": 1,
            },
            headers={"X-Session-Token": owner_token},
        )
        later_page = client.get(
            f"/api/cycle-runs/{run_id}/cycles",
            params={
                "snapshot_revision": snapshot.snapshot_revision,
                "offset": 1,
                "limit": 1,
            },
            headers={"X-Session-Token": owner_token},
        )
        long_page = client.get(
            f"/api/cycle-runs/{run_id}/cycles",
            params={
                "snapshot_revision": snapshot.snapshot_revision,
                "min_length": 3,
                "max_length": 3,
                "vhost": "/",
            },
            headers={"X-Session-Token": owner_token},
        )
        or_vhost_page = client.get(
            f"/api/cycle-runs/{run_id}/cycles",
            params=[
                ("snapshot_revision", snapshot.snapshot_revision),
                ("vhost", "/"),
                ("vhost", "/missing"),
            ],
            headers={"X-Session-Token": owner_token},
        )
        details = [
            client.get(
                f"/api/cycle-runs/{run_id}/cycles/{index}",
                params={"snapshot_revision": snapshot.snapshot_revision},
                headers={"X-Session-Token": owner_token},
            )
            for index in range(len(expected.cycles))
        ]
        replay = client.post("/api/cycle-runs", json=_request(snapshot), headers=headers)
        other_owner = client.get(
            f"/api/cycle-runs/{run_id}/cycles",
            params={"snapshot_revision": snapshot.snapshot_revision},
            headers={"X-Session-Token": str(uuid4())},
        )
        missing_cycle = client.get(
            f"/api/cycle-runs/{run_id}/cycles/99",
            params={"snapshot_revision": snapshot.snapshot_revision},
            headers={"X-Session-Token": owner_token},
        )

    assert completed["status"] == "succeeded"
    assert completed["result"] == {
        "cycleCount": len(expected.cycles),
        "truncated": False,
        "maxCycles": 0,
    }
    assert default_page.status_code == 200, default_page.text
    default_body = default_page.json()
    assert default_body["offset"] == 0
    assert default_body["limit"] == 50
    assert default_body["total"] == len(expected.cycles) == 2
    assert default_body["sort"] == "sdk"
    assert [item["cycleIndex"] for item in default_body["items"]] == [0, 1]
    assert all(len(item["previewNodeIds"]) <= 3 for item in default_body["items"])
    assert sorted_page.status_code == 200
    assert sorted_page.json()["items"][0]["length"] == 3
    assert sorted_page.json()["items"][0]["cycleIndex"] == next(
        index for index, cycle in enumerate(expected.cycles) if len(cycle) == 3
    )
    assert later_page.status_code == 200
    assert [item["cycleIndex"] for item in later_page.json()["items"]] == [1]
    assert long_page.status_code == 200
    assert long_page.json()["total"] == 1
    assert or_vhost_page.status_code == 200
    assert or_vhost_page.json()["total"] == len(expected.cycles)
    assert all(response.status_code == 200 for response in details)
    for index, (response, cycle) in enumerate(zip(details, expected.cycles, strict=True)):
        assert response.json() == {
            "runId": run_id,
            "snapshotRevision": snapshot.snapshot_revision,
            "cycleIndex": index,
            "length": len(cycle),
            "vhosts": ["/"],
            "nodeIds": [node_wire_id(node) for node in cycle.nodes],
            "edgeIds": [expected_wire_ids.edge_id(edge) for edge in cycle.edges],
        }
    all_output = [completed, default_body, *[response.json() for response in details]]
    assert "must-not-leak" not in json.dumps(all_output)
    assert replay.status_code == 200
    assert replay.json()["runId"] == run_id
    assert other_owner.status_code == 404
    assert missing_cycle.status_code == 404


def test_cycle_api_preserves_sdk_truncation_and_validates_all_bounds(tmp_path: Path) -> None:
    snapshot, _auditor = _snapshot(tmp_path)
    owner_token = str(uuid4())
    headers = {
        "X-Session-Token": owner_token,
        "Idempotency-Key": str(uuid4()),
    }

    with TestClient(app) as client:
        replace_static_analyses({snapshot.analysis_id: snapshot})
        started = client.post("/api/cycle-runs", json=_request(snapshot, 1), headers=headers)
        assert started.status_code == 202
        run_id = started.json()["runId"]
        completed = _wait_for_cycle_run(client, run_id, snapshot.snapshot_revision, owner_token)
        changed_limit_replay = client.post(
            "/api/cycle-runs",
            json=_request(snapshot, 0),
            headers=headers,
        )
        invalid_max_cycles = [
            client.post(
                "/api/cycle-runs",
                json={**_request(snapshot), "maxCycles": value},
                headers={
                    "X-Session-Token": str(uuid4()),
                    "Idempotency-Key": str(uuid4()),
                },
            )
            for value in (-1, True, 1.5, "1")
        ]
        bad_pages = [
            client.get(
                f"/api/cycle-runs/{run_id}/cycles",
                params={"snapshot_revision": snapshot.snapshot_revision, **params},
                headers={"X-Session-Token": owner_token},
            )
            for params in (
                {"offset": -1},
                {"limit": 0},
                {"limit": 101},
                {"min_length": 0},
                {"min_length": 3, "max_length": 2},
                {"sort": "unknown"},
            )
        ]
        stale_page = client.get(
            f"/api/cycle-runs/{run_id}/cycles",
            params={"snapshot_revision": "sha256:" + "e" * 64},
            headers={"X-Session-Token": owner_token},
        )
        unknown_query_page = client.get(
            f"/api/cycle-runs/{run_id}/cycles",
            params={
                "snapshot_revision": snapshot.snapshot_revision,
                "unknown_filter": "ignored-before-index-hardening",
            },
            headers={"X-Session-Token": owner_token},
        )

    assert completed["result"] == {
        "cycleCount": 1,
        "truncated": True,
        "maxCycles": 1,
    }
    assert changed_limit_replay.status_code == 409
    assert all(response.status_code == 422 for response in invalid_max_cycles)
    assert all(response.status_code == 422 for response in bad_pages)
    assert stale_page.status_code == 409, stale_page.text
    assert unknown_query_page.status_code == 422, unknown_query_page.text


def test_cycle_page_and_detail_refresh_result_and_session_topology_ttls(tmp_path: Path) -> None:
    snapshot, _auditor = _snapshot(tmp_path, AnalysisSource.SESSION)
    owner_token = str(uuid4())
    owner = OwnerContext("anonymous", owner_token)
    headers = {
        "X-Session-Token": owner_token,
        "Idempotency-Key": str(uuid4()),
    }

    with TestClient(app) as client:
        publish_session_analysis(owner, snapshot)
        started = client.post("/api/cycle-runs", json=_request(snapshot), headers=headers)
        assert started.status_code == 202
        run_id = started.json()["runId"]
        completed = _wait_for_cycle_run(client, run_id, snapshot.snapshot_revision, owner_token)
        assert completed["status"] == "succeeded"

        scheduler = get_job_scheduler()
        before_page_result_expiry = scheduler._runs[run_id].result_expires_at
        before_page_topology_access = state._session_store[
            (owner, snapshot.analysis_id)
        ].last_accessed_at
        assert before_page_result_expiry is not None
        time.sleep(0.01)

        page = client.get(
            f"/api/cycle-runs/{run_id}/cycles",
            params={"snapshot_revision": snapshot.snapshot_revision, "limit": 1},
            headers={"X-Session-Token": owner_token},
        )
        assert page.status_code == 200, page.text
        after_page_result_expiry = scheduler._runs[run_id].result_expires_at
        after_page_topology_access = state._session_store[
            (owner, snapshot.analysis_id)
        ].last_accessed_at
        assert after_page_result_expiry is not None
        assert after_page_result_expiry > before_page_result_expiry
        assert after_page_topology_access > before_page_topology_access
        time.sleep(0.01)

        detail = client.get(
            f"/api/cycle-runs/{run_id}/cycles/0",
            params={"snapshot_revision": snapshot.snapshot_revision},
            headers={"X-Session-Token": owner_token},
        )
        assert detail.status_code == 200, detail.text
        after_detail_result_expiry = scheduler._runs[run_id].result_expires_at
        after_detail_topology_access = state._session_store[
            (owner, snapshot.analysis_id)
        ].last_accessed_at
        assert after_detail_result_expiry is not None
        assert after_detail_result_expiry > after_page_result_expiry
        assert after_detail_topology_access > after_page_topology_access
