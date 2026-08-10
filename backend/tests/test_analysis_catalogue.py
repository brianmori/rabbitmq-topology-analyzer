import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from rabbitmq_management_sdk.topology import ClusterTopology

from analyses.loader import StaticCatalogueConfigurationError, load_static_analyses
from analyses.schemas import analysis_summary
from analyses.snapshots import SnapshotMetadata, create_analysis_snapshot
from core import state
from core.session import OwnerContext
from core.state import (
    AnalysisSource,
    CaptureCompleteness,
    SessionAnalysisLimitError,
    SessionStoreCapacityError,
    configure_analysis_store,
    list_session_analyses,
    list_static_analyses,
    publish_session_analysis,
    replace_static_analyses,
)
from main import app

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def isolated_analysis_store(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("RABBITMQ_SESSION_ANALYSIS_LIMIT", raising=False)
    monkeypatch.delenv("RABBITMQ_SESSION_ANALYSIS_GLOBAL_LIMIT", raising=False)
    monkeypatch.delenv("RABBITMQ_SESSION_TOPOLOGY_TTL_SECONDS", raising=False)
    state._session_store.clear()
    replace_static_analyses({})
    configure_analysis_store()
    yield
    state._session_store.clear()
    replace_static_analyses({})


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def _definitions() -> dict[str, object]:
    return {
        "vhosts": [{"name": "/"}],
        "global_parameters": [
            {"name": "internal_cluster_id", "value": "rabbitmq-cluster-id"},
            {"name": "cluster_name", "value": "Payments broker"},
            {"name": "cluster_tags", "value": {"environment": "production"}},
        ],
    }


def _manifest_entry(**overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "analysisId": "payments-prod",
        "label": "Payments production",
        "clusters": [{"definitionsPath": "payments.json"}],
        "metadataPath": "payments.meta.json",
    }
    entry.update(overrides)
    return entry


def _write_manifest(root: Path, analyses: list[dict[str, object]]) -> None:
    _write_json(root / "manifest.json", {"schemaVersion": 1, "analyses": analyses})


def _session_snapshot(analysis_id: str, revision: str) -> state.AnalysisSnapshot:
    topology = ClusterTopology(
        exchanges=frozenset(),
        queues=frozenset(),
        shovels=frozenset(),
        edges=frozenset(),
        cluster_id="cluster-id",
        cluster_name="Cluster name",
    )
    return create_analysis_snapshot(
        topology,
        SnapshotMetadata(
            analysis_id=analysis_id,
            source=AnalysisSource.SESSION,
            label=analysis_id,
            capture_completeness=CaptureCompleteness.DEFINITIONS_ONLY,
            snapshot_revision=revision,
            cluster_tags={},
            in_cluster_amqp_hosts=(),
            vhost_count=0,
        ),
    )


def test_static_manifest_loads_complete_approved_catalogue_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_json(tmp_path / "payments.json", _definitions())
    _write_json(tmp_path / "queues.json", [])
    _write_json(tmp_path / "exchanges.json", [])
    _write_json(
        tmp_path / "payments.meta.json",
        {
            "schemaVersion": 1,
            "description": "Captured by the payments team",
            "ownerContact": "payments@example.test",
        },
    )
    _write_manifest(
        tmp_path,
        [
            _manifest_entry(
                clusters=[
                    {
                        "definitionsPath": "payments.json",
                        "queuesPath": "queues.json",
                        "exchangesPath": "exchanges.json",
                        "inClusterAmqpHosts": ["RABBITMQ.EXAMPLE.TEST"],
                    },
                ],
            ),
        ],
    )
    monkeypatch.setenv("RABBITMQ_FULL_EXPORT_DEFINITIONS_DIR", str(tmp_path))

    load_static_analyses()

    entries = list_static_analyses()
    assert len(entries) == 1
    summary = analysis_summary(entries[0]).model_dump(mode="json", by_alias=True)
    assert summary["analysisId"] == "payments-prod"
    assert summary["label"] == "Payments production"
    assert summary["source"] == "static"
    assert summary["captureCompleteness"] == "complete"
    assert summary["snapshotRevision"].startswith("sha256:")
    assert len(summary["snapshotRevision"]) == 71
    assert summary["cluster"] == {
        "clusterId": "rabbitmq-cluster-id",
        "clusterName": "Payments broker",
        "clusterTags": {"environment": "production"},
        "vhostCount": 1,
        "nodeCount": 0,
        "edgeCount": 0,
    }
    assert summary["description"] == "Captured by the payments team"
    assert summary["ownerContact"] == "payments@example.test"
    assert summary["expiresAt"] is None


def test_empty_static_manifest_is_valid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_manifest(tmp_path, [])
    monkeypatch.setenv("RABBITMQ_FULL_EXPORT_DEFINITIONS_DIR", str(tmp_path))

    load_static_analyses()

    assert list_static_analyses() == ()


@pytest.mark.parametrize(
    "analyses",
    [
        [_manifest_entry(), _manifest_entry(label="Duplicate")],
        [
            _manifest_entry(
                clusters=[
                    {
                        "definitionsPath": "payments.json",
                        "queuesPath": "queues.json",
                    },
                ],
            ),
        ],
        [_manifest_entry(unexpectedField=True)],
        [_manifest_entry(clusters=[{"definitionsPath": "../outside.json"}])],
        [_manifest_entry(clusters=[{"definitionsPath": str(Path("/absolute.json"))}])],
        [
            _manifest_entry(
                clusters=[
                    {"definitionsPath": "payments.json"},
                    {"definitionsPath": "payments.json"},
                ],
            ),
        ],
    ],
    ids=[
        "duplicate-id",
        "incomplete-observation-pair",
        "unknown-field",
        "traversal",
        "absolute-path",
        "multiple-clusters",
    ],
)
def test_invalid_static_manifest_fails_without_replacing_catalogue(
    analyses: list[dict[str, object]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_json(tmp_path / "payments.json", _definitions())
    _write_json(tmp_path / "payments.meta.json", {"schemaVersion": 1})
    _write_manifest(tmp_path, analyses)
    monkeypatch.setenv("RABBITMQ_FULL_EXPORT_DEFINITIONS_DIR", str(tmp_path))

    with pytest.raises(StaticCatalogueConfigurationError):
        load_static_analyses()

    assert list_static_analyses() == ()


def test_invalid_metadata_sidecar_fails_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_json(tmp_path / "payments.json", _definitions())
    _write_json(
        tmp_path / "payments.meta.json",
        {"schemaVersion": 1, "unexpectedField": "rejected"},
    )
    _write_manifest(tmp_path, [_manifest_entry()])
    monkeypatch.setenv("RABBITMQ_FULL_EXPORT_DEFINITIONS_DIR", str(tmp_path))

    with pytest.raises(StaticCatalogueConfigurationError):
        load_static_analyses()


def test_static_manifest_rejects_symlink_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outside_path = tmp_path.parent / f"outside-{uuid4()}.json"
    _write_json(outside_path, _definitions())
    (tmp_path / "linked.json").symlink_to(outside_path)
    _write_json(tmp_path / "payments.meta.json", {"schemaVersion": 1})
    _write_manifest(
        tmp_path,
        [_manifest_entry(clusters=[{"definitionsPath": "linked.json"}])],
    )
    monkeypatch.setenv("RABBITMQ_FULL_EXPORT_DEFINITIONS_DIR", str(tmp_path))

    try:
        with pytest.raises(StaticCatalogueConfigurationError):
            load_static_analyses()
    finally:
        outside_path.unlink()


def test_session_catalogue_is_owner_scoped_and_listing_does_not_refresh_expiry() -> None:
    owner = OwnerContext(principal_id="analyst", session_id=str(uuid4()))
    another_owner = OwnerContext(principal_id="analyst", session_id=str(uuid4()))
    publication = publish_session_analysis(owner, _session_snapshot("mine", "sha256:" + "a" * 64))

    first_listing = list_session_analyses(owner)
    second_listing = list_session_analyses(owner)

    assert first_listing == second_listing == (publication.entry,)
    assert list_session_analyses(another_owner) == ()
    assert publication.entry.expires_at is not None
    assert publication.entry.expires_at > datetime.now(UTC)


def test_same_id_replacement_works_at_owner_capacity_and_identical_revision_is_retained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RABBITMQ_SESSION_ANALYSIS_LIMIT", "1")
    configure_analysis_store()
    owner = OwnerContext(principal_id="analyst", session_id=str(uuid4()))
    original = _session_snapshot("retained", "sha256:" + "b" * 64)
    first = publish_session_analysis(owner, original)

    with pytest.raises(SessionAnalysisLimitError):
        publish_session_analysis(owner, _session_snapshot("second", "sha256:" + "c" * 64))

    replacement = publish_session_analysis(
        owner,
        _session_snapshot("retained", original.snapshot_revision),
    )
    assert replacement.replaced is True
    assert replacement.entry.snapshot is first.entry.snapshot


def test_global_capacity_rejects_only_new_ids_and_deletion_frees_the_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RABBITMQ_SESSION_ANALYSIS_GLOBAL_LIMIT", "1")
    configure_analysis_store()
    first_owner = OwnerContext(principal_id="analyst", session_id=str(uuid4()))
    second_owner = OwnerContext(principal_id="analyst", session_id=str(uuid4()))
    publish_session_analysis(first_owner, _session_snapshot("first", "sha256:" + "d" * 64))

    with pytest.raises(SessionStoreCapacityError):
        publish_session_analysis(second_owner, _session_snapshot("second", "sha256:" + "e" * 64))

    assert state.delete_session_analysis(first_owner, "first") is True
    publication = publish_session_analysis(
        second_owner,
        _session_snapshot("second", "sha256:" + "e" * 64),
    )
    assert publication.replaced is False


def test_delete_endpoint_removes_only_the_callers_session_analysis() -> None:
    browser_session_id = str(uuid4())
    owner = OwnerContext(principal_id="anonymous", session_id=browser_session_id)
    publish_session_analysis(owner, _session_snapshot("temporary", "sha256:" + "f" * 64))

    with TestClient(app) as client:
        inaccessible = client.delete(
            "/api/analyses/temporary",
            headers={"X-Session-Token": str(uuid4())},
        )
        deleted = client.delete(
            "/api/analyses/temporary",
            headers={"X-Session-Token": browser_session_id},
        )

    assert inaccessible.status_code == 404
    assert deleted.status_code == 204
    assert list_session_analyses(owner) == ()


@pytest.mark.parametrize("value", ["0", "-1", "not-a-number"])
def test_invalid_session_analysis_limit_fails_configuration(
    value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RABBITMQ_SESSION_ANALYSIS_LIMIT", value)

    with pytest.raises(ValueError, match="RABBITMQ_SESSION_ANALYSIS_LIMIT"):
        configure_analysis_store()
