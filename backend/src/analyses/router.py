"""Static and caller-session analysis catalogue endpoint."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status

from analyses.schemas import AnalysisCatalogueResponse, analysis_summary
from analyses.validation import validate_analysis_id
from core.security import require_analyst, require_viewer
from core.session import (
    OwnerContext,
    get_optional_owner_context,
    require_owner_context,
)
from core.state import delete_session_analysis, list_session_analyses, list_static_analyses

router = APIRouter(tags=["analyses"], dependencies=[Depends(require_viewer)])


@router.get("/analyses")
async def get_analyses(
    owner_context: Annotated[OwnerContext | None, Depends(get_optional_owner_context)],
) -> AnalysisCatalogueResponse:
    """List static analyses and only the current owner's session analyses."""
    session_entries = list_session_analyses(owner_context) if owner_context is not None else ()
    return AnalysisCatalogueResponse(
        static=tuple(analysis_summary(entry) for entry in list_static_analyses()),
        session=tuple(analysis_summary(entry) for entry in session_entries),
    )


@router.delete(
    "/analyses/{analysis_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_analyst)],
)
async def delete_analysis(
    analysis_id: str,
    owner_context: Annotated[OwnerContext, Depends(require_owner_context)],
) -> Response:
    """Delete one inactive session analysis owned by the current caller."""
    try:
        validated_analysis_id = validate_analysis_id(analysis_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    if not delete_session_analysis(owner_context, validated_analysis_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Analysis not found.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
