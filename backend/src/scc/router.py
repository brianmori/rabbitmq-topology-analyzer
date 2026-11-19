"""Viewer-authorized cyclic-SCC start, status, and cancellation routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from pydantic import ValidationError

from analyses.validation import validate_analysis_id
from core.concurrency import blocking_route
from core.query import NoQuery, SnapshotRevisionQuery  # noqa: TC001 - FastAPI resolves at runtime.
from core.security import require_viewer
from core.session import OwnerContext, require_owner_context
from core.state import AnalysisActiveError, AnalysisSource, pin_analysis_run, resolve_analysis
from jobs.models import (
    IdempotencyConflictError,
    JobQueueFullError,
    JobStart,
    OperationKind,
    OwnerActiveRunError,
    SchedulerShuttingDownError,
    canonical_start_payload,
    parse_uuid4,
)
from jobs.runtime import get_job_scheduler
from scc.schemas import SccRunResponse, SccStartRequest, scc_run_response
from scc.sdk_bridge import execute_cyclic_scc

router = APIRouter(tags=["cyclic components"], dependencies=[Depends(require_viewer)])


def _validated_run_id(run_id: str) -> str:
    try:
        return parse_uuid4(run_id, field_name="runId")
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc


def _validate_current_snapshot(
    *,
    source: AnalysisSource,
    analysis_id: str,
    snapshot_revision: str,
    owner_context: OwnerContext,
) -> None:
    snapshot = resolve_analysis(source, analysis_id, owner_context)
    if snapshot is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Analysis not found.")
    if snapshot.snapshot_revision != snapshot_revision:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="snapshot_revision_mismatch",
        )


def _response_for_run(run_id: str, owner_context: OwnerContext) -> SccRunResponse:
    scheduler = get_job_scheduler()
    run = scheduler.get_owned(run_id, owner_context)
    if run is None or run.operation is not OperationKind.CYCLIC_SCC:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found.")
    try:
        return scc_run_response(run, scheduler.result_repository)
    except (FileNotFoundError, ValidationError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The completed SCC result is unavailable.",
        ) from exc


@router.post("/scc-runs", status_code=status.HTTP_202_ACCEPTED)
@blocking_route
def start_scc_run(
    request: SccStartRequest,
    response: Response,
    _query: NoQuery,
    owner_context: Annotated[OwnerContext, Depends(require_owner_context)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> SccRunResponse:
    """Atomically replay or admit one SDK cyclic-component operation."""
    try:
        analysis_id = validate_analysis_id(request.analysis_id)
        canonical_idempotency_key = parse_uuid4(
            idempotency_key,
            field_name="Idempotency-Key",
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    snapshot = resolve_analysis(request.source, analysis_id, owner_context)
    if snapshot is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Analysis not found.")
    if snapshot.snapshot_revision != request.snapshot_revision:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="snapshot_revision_mismatch",
        )

    scheduler = get_job_scheduler()
    try:
        admission = scheduler.admit(
            start=JobStart(
                owner_context=owner_context,
                operation=OperationKind.CYCLIC_SCC,
                source=request.source,
                analysis_id=analysis_id,
                snapshot_revision=request.snapshot_revision,
                topology=snapshot.topology,
                parameters={},
                worker=execute_cyclic_scc,
            ),
            canonical_payload=canonical_start_payload(
                source=request.source,
                analysis_id=analysis_id,
                snapshot_revision=request.snapshot_revision,
                parameters={},
            ),
            idempotency_key=canonical_idempotency_key,
            on_admit=lambda: pin_analysis_run(
                owner_context,
                request.source,
                analysis_id,
                request.snapshot_revision,
            ),
        )
    except IdempotencyConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="idempotency_key_reused",
        ) from exc
    except OwnerActiveRunError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="owner_has_active_run",
        ) from exc
    except AnalysisActiveError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="analysis_has_active_work",
        ) from exc
    except (JobQueueFullError, SchedulerShuttingDownError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="analysis_capacity_unavailable",
            headers={"Retry-After": "1"},
        ) from exc
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Analysis not found.",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="snapshot_revision_mismatch",
        ) from exc

    if admission.replayed and admission.run.status.is_terminal:
        response.status_code = status.HTTP_200_OK
    return scc_run_response(admission.run, scheduler.result_repository)


@router.get("/scc-runs/{run_id}")
@blocking_route
def get_scc_run(
    run_id: str,
    query: Annotated[SnapshotRevisionQuery, Query()],
    owner_context: Annotated[OwnerContext, Depends(require_owner_context)],
) -> SccRunResponse:
    """Poll one owned SCC run and renew its heartbeat or terminal TTL."""
    validated_run_id = _validated_run_id(run_id)
    response = _response_for_run(validated_run_id, owner_context)
    if response.snapshot_revision != query.snapshot_revision:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="snapshot_revision_mismatch",
        )
    _validate_current_snapshot(
        source=response.source,
        analysis_id=response.analysis_id,
        snapshot_revision=query.snapshot_revision,
        owner_context=owner_context,
    )
    return response


@router.delete("/scc-runs/{run_id}")
@blocking_route
def cancel_scc_run(
    run_id: str,
    query: Annotated[SnapshotRevisionQuery, Query()],
    owner_context: Annotated[OwnerContext, Depends(require_owner_context)],
) -> SccRunResponse:
    """Cancel queued/running SCC work and return after process release."""
    validated_run_id = _validated_run_id(run_id)
    scheduler = get_job_scheduler()
    current = scheduler.get_owned(validated_run_id, owner_context)
    if current is None or current.operation is not OperationKind.CYCLIC_SCC:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found.")
    if current.snapshot_revision != query.snapshot_revision:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="snapshot_revision_mismatch",
        )
    _validate_current_snapshot(
        source=current.source,
        analysis_id=current.analysis_id,
        snapshot_revision=query.snapshot_revision,
        owner_context=owner_context,
    )
    run = scheduler.cancel_owned(validated_run_id, owner_context)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found.")
    return scc_run_response(run, scheduler.result_repository)
