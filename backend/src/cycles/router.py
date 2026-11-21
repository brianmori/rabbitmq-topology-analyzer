"""Viewer-authorized structural-cycle run, page, and detail routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from pydantic import ValidationError

from analyses.validation import validate_analysis_id
from core.concurrency import blocking_route
from core.query import NoQuery, SnapshotRevisionQuery  # noqa: TC001 - FastAPI resolves at runtime.
from core.security import require_viewer
from core.session import OwnerContext, require_owner_context
from core.state import AnalysisActiveError, AnalysisSource, pin_analysis_run, resolve_analysis
from cycles.result_index import cycle_run_response, read_cycle_detail, read_cycle_page
from cycles.schemas import (
    CycleDetail,  # noqa: TC001 - FastAPI resolves route annotations at runtime.
    CyclePage,  # noqa: TC001 - FastAPI resolves route annotations at runtime.
    CyclePageRequestQuery,  # noqa: TC001 - FastAPI resolves route annotations at runtime.
    CycleRunResponse,  # noqa: TC001 - FastAPI resolves route annotations at runtime.
    CycleStartRequest,  # noqa: TC001 - FastAPI resolves route annotations at runtime.
)
from cycles.sdk_bridge import execute_structural_cycles
from jobs.models import (
    AnalysisRun,
    IdempotencyConflictError,
    JobQueueFullError,
    JobStart,
    OperationKind,
    OwnerActiveRunError,
    RunStatus,
    SchedulerShuttingDownError,
    canonical_start_payload,
    parse_uuid4,
)
from jobs.runtime import get_job_scheduler

router = APIRouter(tags=["structural cycles"], dependencies=[Depends(require_viewer)])


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


def _owned_cycle_run(
    run_id: str,
    snapshot_revision: str,
    owner_context: OwnerContext,
) -> AnalysisRun:
    scheduler = get_job_scheduler()
    run = scheduler.get_owned(run_id, owner_context)
    if run is None or run.operation is not OperationKind.STRUCTURAL_CYCLES:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found.")
    if run.snapshot_revision != snapshot_revision:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="snapshot_revision_mismatch",
        )
    _validate_current_snapshot(
        source=run.source,
        analysis_id=run.analysis_id,
        snapshot_revision=snapshot_revision,
        owner_context=owner_context,
    )
    return run


def _response_for_run(run: AnalysisRun) -> CycleRunResponse:
    try:
        return cycle_run_response(run, get_job_scheduler().result_repository)
    except (OSError, ValidationError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The completed cycle result is unavailable.",
        ) from exc


def _require_succeeded(run: AnalysisRun) -> None:
    if run.status is not RunStatus.SUCCEEDED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="cycle_run_not_succeeded",
        )


@router.post("/cycle-runs", status_code=status.HTTP_202_ACCEPTED)
@blocking_route
def start_cycle_run(
    request: CycleStartRequest,
    response: Response,
    _query: NoQuery,
    owner_context: Annotated[OwnerContext, Depends(require_owner_context)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> CycleRunResponse:
    """Atomically replay or admit one SDK structural-cycle operation."""
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

    parameters: dict[str, str | int | bool | None] = {"maxCycles": request.max_cycles}
    if request.include_all_cross_vhost:
        parameters["includeAllCrossVhost"] = True
    scheduler = get_job_scheduler()
    try:
        admission = scheduler.admit(
            start=JobStart(
                owner_context=owner_context,
                operation=OperationKind.STRUCTURAL_CYCLES,
                source=request.source,
                analysis_id=analysis_id,
                snapshot_revision=request.snapshot_revision,
                topology=snapshot.topology,
                parameters=parameters,
                worker=execute_structural_cycles,
            ),
            canonical_payload=canonical_start_payload(
                source=request.source,
                analysis_id=analysis_id,
                snapshot_revision=request.snapshot_revision,
                parameters=parameters,
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
    return _response_for_run(admission.run)


@router.get("/cycle-runs/{run_id}")
@blocking_route
def get_cycle_run(
    run_id: str,
    query: Annotated[SnapshotRevisionQuery, Query()],
    owner_context: Annotated[OwnerContext, Depends(require_owner_context)],
) -> CycleRunResponse:
    """Poll one owned cycle run and renew its heartbeat or terminal TTL."""
    validated_run_id = _validated_run_id(run_id)
    run = _owned_cycle_run(validated_run_id, query.snapshot_revision, owner_context)
    return _response_for_run(run)


@router.delete("/cycle-runs/{run_id}")
@blocking_route
def cancel_cycle_run(
    run_id: str,
    query: Annotated[SnapshotRevisionQuery, Query()],
    owner_context: Annotated[OwnerContext, Depends(require_owner_context)],
) -> CycleRunResponse:
    """Cancel queued/running cycle work and return after process release."""
    validated_run_id = _validated_run_id(run_id)
    _owned_cycle_run(validated_run_id, query.snapshot_revision, owner_context)
    scheduler = get_job_scheduler()
    run = scheduler.cancel_owned(validated_run_id, owner_context)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found.")
    return _response_for_run(run)


@router.get("/cycle-runs/{run_id}/cycles")
@blocking_route
def get_cycle_page(
    run_id: str,
    query: Annotated[CyclePageRequestQuery, Query()],
    owner_context: Annotated[OwnerContext, Depends(require_owner_context)],
) -> CyclePage:
    """Filter, sort, and page summaries from one exact completed SDK run."""
    if (
        query.min_length is not None
        and query.max_length is not None
        and query.max_length < query.min_length
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="max_length must be at least min_length",
        )
    validated_run_id = _validated_run_id(run_id)
    run = _owned_cycle_run(validated_run_id, query.snapshot_revision, owner_context)
    _require_succeeded(run)
    try:
        return read_cycle_page(run, get_job_scheduler().result_repository, query)
    except (OSError, ValidationError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The completed cycle result is unavailable.",
        ) from exc


@router.get("/cycle-runs/{run_id}/cycles/{index}")
@blocking_route
def get_cycle_detail(
    run_id: str,
    index: int,
    query: Annotated[SnapshotRevisionQuery, Query()],
    owner_context: Annotated[OwnerContext, Depends(require_owner_context)],
) -> CycleDetail:
    """Return the exact ordered node and representative edge path by SDK index."""
    validated_run_id = _validated_run_id(run_id)
    run = _owned_cycle_run(validated_run_id, query.snapshot_revision, owner_context)
    _require_succeeded(run)
    try:
        return read_cycle_detail(run, get_job_scheduler().result_repository, index)
    except IndexError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cycle not found.",
        ) from exc
    except (OSError, ValidationError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The completed cycle result is unavailable.",
        ) from exc
