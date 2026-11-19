"""Viewer-authorized topology graph endpoint."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from analyses.validation import validate_analysis_id
from core.concurrency import blocking_route
from core.security import require_viewer
from core.session import OwnerContext, get_optional_owner_context
from core.state import AnalysisSource, resolve_analysis
from topology.schemas import (  # noqa: TC001 - FastAPI resolves at runtime.
    TopologyQuery,
    TopologyResponse,
)
from topology.sdk_bridge import serialize_topology

router = APIRouter(tags=["topology"], dependencies=[Depends(require_viewer)])


@router.get("/topology")
@blocking_route
def get_topology(
    query: Annotated[TopologyQuery, Query()],
    owner_context: Annotated[OwnerContext | None, Depends(get_optional_owner_context)],
) -> TopologyResponse:
    """Return the complete safe graph for one accessible snapshot."""
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
    return serialize_topology(snapshot)
