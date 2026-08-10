"""Approved wire schemas for the static and session analysis catalogue."""

from datetime import datetime  # noqa: TC003 - Pydantic resolves this field at runtime.

from core.schemas import CamelModel
from core.state import (  # noqa: TC001 - Pydantic resolves these fields at runtime.
    AnalysisCatalogueEntry,
    AnalysisSource,
    CaptureCompleteness,
)


class ClusterSummary(CamelModel):
    """Safe SDK-backed cluster statistics for one v1 analysis."""

    cluster_id: str | None
    cluster_name: str | None
    cluster_tags: dict[str, str]
    vhost_count: int
    node_count: int
    edge_count: int


class AnalysisSummary(CamelModel):
    """One selectable static or caller-owned session analysis."""

    analysis_id: str
    label: str
    source: AnalysisSource
    snapshot_revision: str
    loaded_at: datetime
    capture_completeness: CaptureCompleteness
    cluster: ClusterSummary
    description: str | None
    owner_contact: str | None
    expires_at: datetime | None


class AnalysisCatalogueResponse(CamelModel):
    """Response body for ``GET /api/analyses``."""

    static: tuple[AnalysisSummary, ...]
    session: tuple[AnalysisSummary, ...]


class UploadResponse(CamelModel):
    """Response body for a new or replacement session capture."""

    analysis: AnalysisSummary
    replaced: bool


def analysis_summary(entry: AnalysisCatalogueEntry) -> AnalysisSummary:
    """Adapt one internal catalogue entry to its safe wire representation."""
    snapshot = entry.snapshot
    return AnalysisSummary(
        analysis_id=snapshot.analysis_id,
        label=snapshot.label,
        source=snapshot.source,
        snapshot_revision=snapshot.snapshot_revision,
        loaded_at=snapshot.loaded_at,
        capture_completeness=snapshot.capture_completeness,
        cluster=ClusterSummary(
            cluster_id=snapshot.topology.cluster_id,
            cluster_name=snapshot.cluster_name,
            cluster_tags=dict(snapshot.cluster_tags),
            vhost_count=snapshot.vhost_count,
            node_count=len(snapshot.topology.nodes),
            edge_count=len(snapshot.topology.edges),
        ),
        description=snapshot.description,
        owner_contact=snapshot.owner_contact,
        expires_at=entry.expires_at,
    )
