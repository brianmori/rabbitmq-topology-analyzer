"""Static and caller-session analysis catalogue endpoint."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status

from analyses.schemas import AnalysisCatalogueResponse, analysis_summary
from analyses.validation import validate_analysis_id
from core.concurrency import blocking_route
from core.query import NoQuery  # noqa: TC001 - FastAPI resolves at runtime.
from core.security import require_analyst, require_viewer
from core.session import (
    OwnerContext,
    get_optional_owner_context,
    require_owner_context,
)
from core.state import (
    AnalysisActiveError,
    AnalysisSource,
    list_session_analyses,
    list_static_analyses,
    release_session_analysis_deletion,
    reserve_session_analysis_deletion,
)
from jobs.runtime import get_job_scheduler

router = APIRouter(tags=["analyses"], dependencies=[Depends(require_viewer)])


@router.get("/analyses")
@blocking_route
def get_analyses(
    owner_context: Annotated[OwnerContext | None, Depends(get_optional_owner_context)],
    _query: NoQuery,
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
@blocking_route
def delete_analysis(
    analysis_id: str,
    owner_context: Annotated[OwnerContext, Depends(require_owner_context)],
    _query: NoQuery,
) -> Response:
    """Delete one inactive session analysis owned by the current caller."""
    try:
        validated_analysis_id = validate_analysis_id(analysis_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    try:
        reservation = reserve_session_analysis_deletion(
            owner_context,
            validated_analysis_id,
        )
    except AnalysisActiveError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="analysis_has_active_work",
        ) from exc
    if reservation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Analysis not found.")
    try:
        get_job_scheduler().invalidate_analysis(
            owner_context,
            AnalysisSource.SESSION,
            validated_analysis_id,
        )
    finally:
        release_session_analysis_deletion(reservation)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
