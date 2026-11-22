"""Viewer-authorized topology graph and download endpoints."""

from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from analyses.validation import validate_analysis_id
from core.concurrency import blocking_route
from core.security import require_viewer
from core.session import OwnerContext, get_optional_owner_context
from core.state import AnalysisSource, resolve_analysis
from topology.schemas import (  # noqa: TC001 - FastAPI resolves at runtime.
    TopologyExport,
    TopologyExportQuery,
    TopologyQuery,
    TopologyResponse,
)
from topology.sdk_bridge import serialize_topology, serialize_topology_export

if TYPE_CHECKING:
    from core.state import AnalysisSnapshot

router = APIRouter(tags=["topology"], dependencies=[Depends(require_viewer)])


def _resolve_topology(query: TopologyQuery, owner_context: OwnerContext | None) -> AnalysisSnapshot:
    try:
        validated_analysis_id = validate_analysis_id(query.analysis_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    if query.source is AnalysisSource.SESSION and owner_context is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Session-Token is required for a session analysis.",
        )

    snapshot = resolve_analysis(query.source, validated_analysis_id, owner_context)
    if snapshot is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Analysis not found.")
    return snapshot


@router.get("/topology")
@blocking_route
def get_topology(
    query: Annotated[TopologyQuery, Query()],
    owner_context: Annotated[OwnerContext | None, Depends(get_optional_owner_context)],
) -> TopologyResponse:
    """Return the complete safe graph for one accessible snapshot."""
    return serialize_topology(_resolve_topology(query, owner_context))


@router.get("/topology/export")
@blocking_route
def export_topology(
    query: Annotated[TopologyExportQuery, Query()],
    owner_context: Annotated[OwnerContext | None, Depends(get_optional_owner_context)],
    response: Response,
) -> TopologyExport:
    """Download the complete safe graph for the requested capture revision."""
    snapshot = _resolve_topology(query, owner_context)
    if snapshot.snapshot_revision != query.snapshot_revision:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="snapshot_revision_mismatch",
        )
    filename = f"{snapshot.analysis_id}.topology.json"
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.headers["Cache-Control"] = "no-store"
    return serialize_topology_export(snapshot)
