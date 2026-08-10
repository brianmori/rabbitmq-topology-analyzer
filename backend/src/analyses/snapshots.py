"""Construction of immutable application snapshots and revision identities."""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from core.state import AnalysisSnapshot, AnalysisSource, CaptureCompleteness

if TYPE_CHECKING:
    from collections.abc import Mapping

    from rabbitmq_management_sdk.topology import ClusterTopology

_SNAPSHOT_CONTRACT_VERSION = "rabbitmq-topology-visualizer-snapshot-v1"


@dataclass(frozen=True, slots=True)
class SnapshotMetadata:
    """Application metadata supplied alongside one normalized SDK topology."""

    analysis_id: str
    source: AnalysisSource
    label: str
    capture_completeness: CaptureCompleteness
    snapshot_revision: str
    cluster_tags: Mapping[str, str]
    in_cluster_amqp_hosts: tuple[str, ...]
    vhost_count: int
    description: str | None = None
    owner_contact: str | None = None


def compute_snapshot_revision(
    *,
    definitions_bytes: bytes,
    queues_bytes: bytes | None,
    exchanges_bytes: bytes | None,
    in_cluster_amqp_hosts: tuple[str, ...],
) -> str:
    """Hash every effective topology input with explicit role boundaries."""
    digest = hashlib.sha256()
    inputs = (
        ("contract", _SNAPSHOT_CONTRACT_VERSION.encode()),
        ("definitions", definitions_bytes),
        ("queues", queues_bytes),
        ("exchanges", exchanges_bytes),
        (
            "in_cluster_amqp_hosts",
            json.dumps(in_cluster_amqp_hosts, ensure_ascii=False, separators=(",", ":")).encode(),
        ),
    )
    for role, value in inputs:
        encoded_role = role.encode()
        digest.update(len(encoded_role).to_bytes(4, "big"))
        digest.update(encoded_role)
        if value is None:
            digest.update(b"\x00")
            continue
        digest.update(b"\x01")
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)
    return f"sha256:{digest.hexdigest()}"


def create_analysis_snapshot(
    topology: ClusterTopology,
    metadata: SnapshotMetadata,
) -> AnalysisSnapshot:
    """Create one timestamped application-owned snapshot record."""
    return AnalysisSnapshot(
        topology=topology,
        analysis_id=metadata.analysis_id,
        source=metadata.source,
        label=metadata.label,
        capture_completeness=metadata.capture_completeness,
        snapshot_revision=metadata.snapshot_revision,
        loaded_at=datetime.now(UTC),
        cluster_name=topology.cluster_name,
        cluster_tags=metadata.cluster_tags,
        in_cluster_amqp_hosts=metadata.in_cluster_amqp_hosts,
        vhost_count=metadata.vhost_count,
        description=metadata.description,
        owner_contact=metadata.owner_contact,
    )
