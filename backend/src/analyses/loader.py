"""Fail-fast loader for the configured static analysis catalogue."""

import os
from pathlib import Path

from pydantic import ValidationError

from analyses.manifest import StaticAnalysisManifest, StaticAnalysisMetadata
from analyses.sdk_bridge import StaticAnalysisLoadError, load_static_capture
from analyses.snapshots import (
    SnapshotMetadata,
    compute_snapshot_revision,
    create_analysis_snapshot,
)
from core.state import (
    AnalysisSnapshot,
    AnalysisSource,
    CaptureCompleteness,
    replace_static_analyses,
)


def _application_root() -> Path:
    module_path = Path(__file__).resolve()
    if module_path.parents[1].name == "src":
        return module_path.parents[2]
    return module_path.parents[1]


_DEFAULT_STATIC_DEFINITIONS_ROOT = _application_root() / "static" / "definitions"


class StaticCatalogueConfigurationError(RuntimeError):
    """Raised when static catalogue configuration cannot be loaded safely."""


def configured_static_definitions_root() -> Path:
    """Resolve the environment override or application-root default."""
    configured = os.environ.get("RABBITMQ_FULL_EXPORT_DEFINITIONS_DIR")
    if configured is not None:
        return Path(configured).expanduser()
    return _DEFAULT_STATIC_DEFINITIONS_ROOT


def _read_bytes(path: Path, description: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise StaticCatalogueConfigurationError(f"Could not read {description}: {path}") from exc


def _resolve_referenced_file(root: Path, relative_path: str, field: str) -> Path:
    requested = Path(relative_path)
    if requested.is_absolute() or ".." in requested.parts:
        raise StaticCatalogueConfigurationError(f"{field} must be a confined relative path")
    try:
        resolved = (root / requested).resolve(strict=True)
    except OSError as exc:
        raise StaticCatalogueConfigurationError(
            f"{field} does not reference a readable file",
        ) from exc
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise StaticCatalogueConfigurationError(
            f"{field} must resolve to a file inside the definitions root",
        )
    return resolved


def _load_metadata(root: Path, metadata_path: str | None) -> StaticAnalysisMetadata | None:
    if metadata_path is None:
        return None
    path = _resolve_referenced_file(root, metadata_path, "metadataPath")
    try:
        return StaticAnalysisMetadata.model_validate_json(_read_bytes(path, "analysis metadata"))
    except ValidationError as exc:
        raise StaticCatalogueConfigurationError(
            f"Invalid static analysis metadata: {path}",
        ) from exc


def _load_manifest(root: Path) -> StaticAnalysisManifest:
    manifest_path = root / "manifest.json"
    try:
        manifest_bytes = _read_bytes(manifest_path, "static manifest")
        return StaticAnalysisManifest.model_validate_json(manifest_bytes)
    except ValidationError as exc:
        raise StaticCatalogueConfigurationError(
            f"Invalid static analysis manifest: {manifest_path}",
        ) from exc


def load_static_analyses() -> None:
    """Load every configured static capture, then publish the catalogue atomically."""
    try:
        root = configured_static_definitions_root().resolve(strict=True)
    except OSError as exc:
        raise StaticCatalogueConfigurationError(
            "The static definitions root does not exist",
        ) from exc
    if not root.is_dir():
        raise StaticCatalogueConfigurationError("The static definitions root must be a directory")

    manifest = _load_manifest(root)
    snapshots: dict[str, AnalysisSnapshot] = {}
    for analysis in manifest.analyses:
        capture = analysis.clusters[0]
        definitions_path = _resolve_referenced_file(
            root,
            capture.definitions_path,
            f"{analysis.analysis_id}.definitionsPath",
        )
        queues_path = (
            _resolve_referenced_file(
                root,
                capture.queues_path,
                f"{analysis.analysis_id}.queuesPath",
            )
            if capture.queues_path is not None
            else None
        )
        exchanges_path = (
            _resolve_referenced_file(
                root,
                capture.exchanges_path,
                f"{analysis.analysis_id}.exchangesPath",
            )
            if capture.exchanges_path is not None
            else None
        )
        definitions_bytes = _read_bytes(definitions_path, "definitions capture")
        queues_bytes = (
            _read_bytes(queues_path, "queue observations") if queues_path is not None else None
        )
        exchanges_bytes = (
            _read_bytes(exchanges_path, "exchange observations")
            if exchanges_path is not None
            else None
        )
        try:
            loaded = load_static_capture(
                definitions_path,
                queues_path=queues_path,
                exchanges_path=exchanges_path,
                in_cluster_amqp_hosts=capture.in_cluster_amqp_hosts,
                cluster_label=analysis.label,
            )
        except StaticAnalysisLoadError as exc:
            raise StaticCatalogueConfigurationError(
                f"SDK validation failed for static analysis {analysis.analysis_id!r}",
            ) from exc
        metadata = _load_metadata(root, analysis.metadata_path)
        completeness = (
            CaptureCompleteness.COMPLETE
            if queues_path is not None
            else CaptureCompleteness.DEFINITIONS_ONLY
        )
        snapshots[analysis.analysis_id] = create_analysis_snapshot(
            loaded.topology,
            SnapshotMetadata(
                analysis_id=analysis.analysis_id,
                source=AnalysisSource.STATIC,
                label=analysis.label,
                capture_completeness=completeness,
                snapshot_revision=compute_snapshot_revision(
                    definitions_bytes=definitions_bytes,
                    queues_bytes=queues_bytes,
                    exchanges_bytes=exchanges_bytes,
                    in_cluster_amqp_hosts=capture.in_cluster_amqp_hosts,
                ),
                cluster_tags=loaded.cluster_tags,
                in_cluster_amqp_hosts=capture.in_cluster_amqp_hosts,
                vhost_count=loaded.vhost_count,
                description=metadata.description if metadata is not None else None,
                owner_contact=metadata.owner_contact if metadata is not None else None,
            ),
        )

    replace_static_analyses(snapshots)
