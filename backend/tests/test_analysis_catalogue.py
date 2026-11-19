import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from rabbitmq_management_sdk.topology import ClusterTopology

import analyses.snapshots as snapshots_module
from analyses.loader import StaticCatalogueConfigurationError, load_static_analyses
from analyses.schemas import analysis_summary
from analyses.snapshots import SnapshotMetadata, compute_snapshot_revision, create_analysis_snapshot
from capture_envelopes import (
    CAPTURE_PRIVATE_MARKER,
    INVALID_CAPTURE_ENVELOPES,
    VALID_CAPTURE_ENVELOPES,
)
from core import state
from core.session import OwnerContext
from core.state import (
    AnalysisActiveError,
    AnalysisSource,
    CaptureCompleteness,
    SessionAnalysisLimitError,
    SessionStoreCapacityError,
    configure_analysis_store,
    list_session_analyses,
    list_static_analyses,
    pin_analysis_run,
    publish_session_analysis,
    replace_static_analyses,
    resolve_analysis,
    unpin_analysis_run,
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


def _snapshot(
    analysis_id: str,
    revision: str,
    source: AnalysisSource = AnalysisSource.SESSION,
) -> state.AnalysisSnapshot:
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
            source=source,
            label=analysis_id,
            capture_completeness=CaptureCompleteness.DEFINITIONS_ONLY,
            snapshot_revision=revision,
            cluster_tags={},
            in_cluster_amqp_hosts=(),
            vhost_count=0,
        ),
    )


def _session_snapshot(analysis_id: str, revision: str) -> state.AnalysisSnapshot:
    return _snapshot(analysis_id, revision)


def test_snapshot_revision_covers_every_topology_affecting_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def revision(
        *,
        definitions: bytes = b"definitions-a",
        queues: bytes | None = b"queues-a",
        exchanges: bytes | None = b"exchanges-a",
        hosts: tuple[str, ...] = ("rabbit-a",),
    ) -> str:
        return compute_snapshot_revision(
            definitions_bytes=definitions,
            queues_bytes=queues,
            exchanges_bytes=exchanges,
            in_cluster_amqp_hosts=hosts,
        )

    original = revision()

    assert original.startswith("sha256:")
    assert len(original) == 71
    assert revision(definitions=b"definitions-b") != original
    assert revision(queues=b"queues-b") != original
    assert revision(exchanges=b"exchanges-b") != original
    assert revision(hosts=("rabbit-b",)) != original

    monkeypatch.setattr(
        snapshots_module,
        "_SNAPSHOT_CONTRACT_VERSION",
        "rabbitmq-topology-analyzer-snapshot-v2",
    )
    assert revision() != original


def test_static_and_session_sources_resolve_to_the_same_snapshot_interface() -> None:
    owner = OwnerContext(principal_id="analyst", session_id=str(uuid4()))
    static_snapshot = _snapshot(
        "static-analysis",
        "sha256:" + "1" * 64,
        AnalysisSource.STATIC,
    )
    session_snapshot = _session_snapshot("session-analysis", "sha256:" + "2" * 64)
    replace_static_analyses({static_snapshot.analysis_id: static_snapshot})
    publish_session_analysis(owner, session_snapshot)

    resolved_static = resolve_analysis(AnalysisSource.STATIC, "static-analysis", None)
    resolved_session = resolve_analysis(AnalysisSource.SESSION, "session-analysis", owner)

    assert isinstance(resolved_static, state.AnalysisSnapshot)
    assert isinstance(resolved_session, state.AnalysisSnapshot)
    assert resolved_static is static_snapshot
    assert resolved_session is session_snapshot
    assert resolved_static.topology == resolved_session.topology


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


def test_missing_manifest_fails_startup_without_replacing_catalogue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RABBITMQ_FULL_EXPORT_DEFINITIONS_DIR", str(tmp_path))

    with pytest.raises(StaticCatalogueConfigurationError) as error:
        load_static_analyses()

    assert str(tmp_path) not in str(error.value)
    assert error.value.__cause__ is None
    assert list_static_analyses() == ()


def test_missing_capture_fails_startup_without_replacing_catalogue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_manifest(
        tmp_path,
        [_manifest_entry(clusters=[{"definitionsPath": "missing.json"}])],
    )
    monkeypatch.setenv("RABBITMQ_FULL_EXPORT_DEFINITIONS_DIR", str(tmp_path))

    with pytest.raises(StaticCatalogueConfigurationError):
        load_static_analyses()

    assert list_static_analyses() == ()


def test_sdk_rejected_capture_fails_startup_without_replacing_catalogue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_json(tmp_path / "invalid.json", {"vhosts": "not-a-list"})
    entry = _manifest_entry(clusters=[{"definitionsPath": "invalid.json"}])
    entry.pop("metadataPath")
    _write_manifest(tmp_path, [entry])
    monkeypatch.setenv("RABBITMQ_FULL_EXPORT_DEFINITIONS_DIR", str(tmp_path))

    with pytest.raises(
        StaticCatalogueConfigurationError,
        match="SDK validation failed",
    ) as error:
        load_static_analyses()

    assert "not-a-list" not in str(error.value)
    assert str(tmp_path) not in str(error.value)
    assert error.value.__cause__ is None
    assert list_static_analyses() == ()


@pytest.mark.parametrize("definitions", INVALID_CAPTURE_ENVELOPES)
def test_sdk_envelope_rejection_prevents_readiness_and_partial_static_publication(
    definitions: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    previous = _snapshot("previous", "sha256:" + "a" * 64, AnalysisSource.STATIC)
    replace_static_analyses({previous.analysis_id: previous})
    _write_json(tmp_path / "valid.json", {"vhosts": []})
    _write_json(tmp_path / "invalid.json", definitions)
    _write_manifest(
        tmp_path,
        [
            {
                "analysisId": analysis_id,
                "label": analysis_id,
                "clusters": [{"definitionsPath": path}],
            }
            for analysis_id, path in (
                ("valid-first", "valid.json"),
                ("invalid-second", "invalid.json"),
            )
        ],
    )
    monkeypatch.setenv("RABBITMQ_FULL_EXPORT_DEFINITIONS_DIR", str(tmp_path))
    with (
        pytest.raises(StaticCatalogueConfigurationError, match="SDK validation failed") as error,
        TestClient(app),
    ):
        pytest.fail("An invalid static capture must fail before HTTP readiness.")

    assert tuple(entry.snapshot for entry in list_static_analyses()) == (previous,)
    assert resolve_analysis(AnalysisSource.STATIC, "previous", None) is previous
    assert str(error.value) == "SDK validation failed for static analysis 'invalid-second'"
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__
    output = capfd.readouterr()
    for text in (str(error.value), output.out, output.err):
        assert CAPTURE_PRIVATE_MARKER not in text
        assert "TopologyDefinitionsError" not in text
        assert "invalid schema" not in text
        assert str(tmp_path) not in text


@pytest.mark.parametrize(("definitions", "vhost_count"), VALID_CAPTURE_ENVELOPES)
def test_empty_and_older_static_cluster_exports_become_ready(
    definitions: dict[str, object],
    vhost_count: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_json(tmp_path / "compatible.json", definitions)
    _write_manifest(
        tmp_path,
        [
            {
                "analysisId": "compatible",
                "label": "Compatible",
                "clusters": [{"definitionsPath": "compatible.json"}],
            }
        ],
    )
    monkeypatch.setenv("RABBITMQ_FULL_EXPORT_DEFINITIONS_DIR", str(tmp_path))
    with TestClient(app) as client:
        assert client.get("/api/health/ready").status_code == 200
        summary = client.get("/api/analyses").json()["static"][0]
        assert summary["analysisId"] == "compatible"
        assert summary["cluster"]["vhostCount"] == vhost_count
        assert (
            client.get(
                "/api/topology",
                params={"source": "static", "analysis_id": "compatible"},
            ).status_code
            == 200
        )


def test_static_catalogue_is_ordered_by_analysis_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_json(tmp_path / "shared.json", _definitions())
    zeta = _manifest_entry(
        analysisId="zeta",
        label="Zeta",
        clusters=[{"definitionsPath": "shared.json"}],
    )
    alpha = _manifest_entry(
        analysisId="alpha",
        label="Alpha",
        clusters=[{"definitionsPath": "shared.json"}],
    )
    zeta.pop("metadataPath")
    alpha.pop("metadataPath")
    _write_manifest(tmp_path, [zeta, alpha])
    monkeypatch.setenv("RABBITMQ_FULL_EXPORT_DEFINITIONS_DIR", str(tmp_path))

    load_static_analyses()

    assert [entry.snapshot.analysis_id for entry in list_static_analyses()] == [
        "alpha",
        "zeta",
    ]


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
    assert replacement.entry.snapshot is not first.entry.snapshot
    assert replacement.entry.snapshot.loaded_at == first.entry.snapshot.loaded_at
    assert replacement.entry.snapshot.snapshot_revision == first.entry.snapshot.snapshot_revision


def test_default_five_capture_journey_rejects_sixth_then_delete_frees_slot() -> None:
    owner = OwnerContext(principal_id="analyst", session_id=str(uuid4()))
    revisions = [f"sha256:{value * 64}" for value in "1234567"]
    for index in range(5):
        publish_session_analysis(owner, _session_snapshot(f"capture-{index}", revisions[index]))

    with pytest.raises(SessionAnalysisLimitError):
        publish_session_analysis(owner, _session_snapshot("capture-5", revisions[5]))

    at_capacity_replacement = publish_session_analysis(
        owner,
        _session_snapshot("capture-0", revisions[6]),
    )
    assert at_capacity_replacement.replaced is True
    assert len(list_session_analyses(owner)) == 5

    assert state.delete_session_analysis(owner, "capture-1") is True
    subsequent = publish_session_analysis(
        owner,
        _session_snapshot("capture-5", revisions[5]),
    )

    assert subsequent.replaced is False
    assert [entry.snapshot.analysis_id for entry in list_session_analyses(owner)] == [
        "capture-0",
        "capture-2",
        "capture-3",
        "capture-4",
        "capture-5",
    ]


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


def test_static_unknown_expired_and_other_owner_deletions_are_indistinguishable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requester_token = str(uuid4())
    requester = OwnerContext(principal_id="anonymous", session_id=requester_token)
    another_owner = OwnerContext(principal_id="anonymous", session_id=str(uuid4()))

    with TestClient(app) as client:
        replace_static_analyses(
            {"static-only": _snapshot("static-only", "sha256:" + "1" * 64, AnalysisSource.STATIC)},
        )
        publish_session_analysis(
            another_owner,
            _session_snapshot("other-owner", "sha256:" + "2" * 64),
        )
        monkeypatch.setenv("RABBITMQ_SESSION_TOPOLOGY_TTL_SECONDS", "1")
        configure_analysis_store()
        publish_session_analysis(
            requester,
            _session_snapshot("expired", "sha256:" + "3" * 64),
        )
        state._session_store[(requester, "expired")].last_accessed_at -= timedelta(seconds=2)

        responses = [
            client.delete(
                f"/api/analyses/{analysis_id}",
                headers={"X-Session-Token": requester_token},
            )
            for analysis_id in ("static-only", "unknown", "expired", "other-owner")
        ]

    assert {response.status_code for response in responses} == {404}
    assert {response.json()["error"]["code"] for response in responses} == {"not_found"}
    assert len({response.text for response in responses}) == 1


def test_active_job_blocks_upload_replacement_and_delete_routes() -> None:
    owner_token = str(uuid4())
    owner = OwnerContext(principal_id="anonymous", session_id=owner_token)
    snapshot = _session_snapshot("active", "sha256:" + "4" * 64)
    publish_session_analysis(owner, snapshot)
    pin_analysis_run(owner, AnalysisSource.SESSION, "active", snapshot.snapshot_revision)
    try:
        with TestClient(app) as client:
            replacement = client.post(
                "/api/definitions/upload",
                data={"analysis_id": "active"},
                files={
                    "definitions_file": ("definitions.json", b"{}", "application/json"),
                },
                headers={"X-Session-Token": owner_token},
            )
            deletion = client.delete(
                "/api/analyses/active",
                headers={"X-Session-Token": owner_token},
            )
    finally:
        unpin_analysis_run(owner, AnalysisSource.SESSION, "active", snapshot.snapshot_revision)

    assert replacement.status_code == 409
    assert replacement.json()["error"]["code"] == "analysis_has_active_work"
    assert deletion.status_code == 409
    assert deletion.json()["error"]["code"] == "analysis_has_active_work"


def test_active_run_pin_blocks_replacement_deletion_and_topology_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RABBITMQ_SESSION_TOPOLOGY_TTL_SECONDS", "1")
    configure_analysis_store()
    owner = OwnerContext(principal_id="analyst", session_id=str(uuid4()))
    revision = "sha256:" + "9" * 64
    publish_session_analysis(owner, _session_snapshot("active", revision))
    pin_analysis_run(owner, AnalysisSource.SESSION, "active", revision)
    state._session_store[(owner, "active")].last_accessed_at = datetime.now(UTC) - timedelta(
        seconds=2,
    )

    assert len(list_session_analyses(owner)) == 1
    with pytest.raises(AnalysisActiveError):
        publish_session_analysis(owner, _session_snapshot("active", "sha256:" + "8" * 64))
    with pytest.raises(AnalysisActiveError):
        state.delete_session_analysis(owner, "active")

    unpin_analysis_run(owner, AnalysisSource.SESSION, "active", revision)
    assert list_session_analyses(owner) == ()


@pytest.mark.parametrize("value", ["0", "-1", "not-a-number"])
def test_invalid_session_analysis_limit_fails_configuration(
    value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RABBITMQ_SESSION_ANALYSIS_LIMIT", value)

    with pytest.raises(ValueError, match="RABBITMQ_SESSION_ANALYSIS_LIMIT"):
        configure_analysis_store()
