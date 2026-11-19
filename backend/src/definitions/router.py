"""Session capture upload endpoint."""

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse

from analyses.schemas import UploadResponse, analysis_summary
from analyses.snapshots import SnapshotMetadata, create_analysis_snapshot
from analyses.validation import (
    normalize_in_cluster_amqp_hosts,
    normalize_label,
    validate_analysis_id,
)
from core.concurrency import run_blocking
from core.query import NoQuery  # noqa: TC001 - FastAPI resolves at runtime.
from core.security import require_analyst
from core.session import OwnerContext, require_owner_context
from core.state import (
    AnalysisActiveError,
    AnalysisSource,
    CaptureCompleteness,
    SessionAnalysisLimitError,
    SessionStoreCapacityError,
    SessionUploadReservation,
    publish_session_analysis,
    release_session_upload,
    reserve_session_upload,
)
from definitions.config import get_capture_upload_settings
from definitions.multipart import CaptureUploadRoute
from definitions.processing import (
    CaptureCapacityUnavailableError,
    CaptureProcessingError,
    CaptureRequestDisconnectedError,
    CaptureValidationError,
    ProcessedCapture,
    process_staged_capture,
)
from definitions.staging import CaptureUploadTooLargeError, StagedCapture, stage_capture_files
from jobs.runtime import get_job_scheduler

if TYPE_CHECKING:
    from core.state import AnalysisSnapshot, SessionPublication

router = APIRouter(
    tags=["definitions"],
    dependencies=[Depends(require_analyst)],
    route_class=CaptureUploadRoute,
)


def _validation_error(message: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=message)


def _reserve_upload(owner_context: OwnerContext, analysis_id: str) -> SessionUploadReservation:
    """Map atomic upload-reservation failures to the existing safe route errors."""
    try:
        return reserve_session_upload(owner_context, analysis_id)
    except SessionAnalysisLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="session_analysis_limit_reached",
        ) from exc
    except SessionStoreCapacityError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="session_store_capacity_reached",
            headers={"Retry-After": "1"},
        ) from exc
    except AnalysisActiveError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="analysis_has_active_work",
        ) from exc


async def _process_capture(
    staged: StagedCapture,
    *,
    normalized_hosts: tuple[str, ...],
    normalized_label: str,
    request: Request,
) -> ProcessedCapture:
    """Map immediate parser outcomes to the existing safe route errors."""
    try:
        return await process_staged_capture(
            staged,
            in_cluster_amqp_hosts=normalized_hosts,
            cluster_label=normalized_label,
            scheduler=get_job_scheduler(),
            is_disconnected=request.is_disconnected,
        )
    except CaptureCapacityUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="analysis_capacity_unavailable",
            headers={"Retry-After": "1"},
        ) from exc
    except CaptureValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded files are not a valid RabbitMQ capture.",
        ) from exc
    except CaptureRequestDisconnectedError as exc:
        raise HTTPException(status_code=499, detail="upload_request_cancelled") from exc
    except CaptureProcessingError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The uploaded capture could not be processed.",
        ) from exc


def _publish_capture(
    owner_context: OwnerContext,
    snapshot: AnalysisSnapshot,
    reservation: SessionUploadReservation,
) -> SessionPublication:
    """Commit publication and result invalidation as one blocking transaction."""
    try:
        publication = publish_session_analysis(owner_context, snapshot, reservation=reservation)
    except AnalysisActiveError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="analysis_has_active_work",
        ) from exc
    if publication.revision_changed:
        get_job_scheduler().invalidate_analysis(
            owner_context,
            AnalysisSource.SESSION,
            snapshot.analysis_id,
            keep_snapshot_revision=publication.entry.snapshot.snapshot_revision,
        )
    return publication


@router.post(
    "/definitions/upload",
    status_code=status.HTTP_201_CREATED,
    response_model=UploadResponse,
)
async def upload_definitions(
    analysis_id: Annotated[str, Form(...)],
    definitions_file: Annotated[UploadFile, File(...)],
    owner_context: Annotated[OwnerContext, Depends(require_owner_context)],
    request: Request,
    _query: NoQuery,
    queues_file: Annotated[UploadFile | None, File()] = None,
    exchanges_file: Annotated[UploadFile | None, File()] = None,
    in_cluster_amqp_host: Annotated[list[str] | None, Form()] = None,
    label: Annotated[str | None, Form()] = None,
) -> JSONResponse:
    """Validate and atomically publish a caller-owned session capture."""
    try:
        validated_analysis_id = validate_analysis_id(analysis_id)
        normalized_label = normalize_label(label if label is not None else analysis_id)
        normalized_hosts = normalize_in_cluster_amqp_hosts(in_cluster_amqp_host or ())
    except ValueError as exc:
        raise _validation_error(str(exc)) from exc
    if (queues_file is None) != (exchanges_file is None):
        raise _validation_error("queues_file and exchanges_file must be supplied together")

    reservation: SessionUploadReservation | None = None

    def reserve() -> None:
        # Retain the claim even if cancellation arrives while the thread waits
        # for expiry cleanup; the outer finally must still release it.
        nonlocal reservation
        reservation = _reserve_upload(owner_context, validated_analysis_id)

    try:
        await run_blocking(reserve)
        assert reservation is not None
        with TemporaryDirectory(prefix="rabbitmq-topology-") as temporary_directory:
            temporary_root = Path(temporary_directory)
            try:
                staged = await stage_capture_files(
                    temporary_root,
                    definitions_file=definitions_file,
                    queues_file=queues_file,
                    exchanges_file=exchanges_file,
                    max_upload_bytes=get_capture_upload_settings().max_upload_bytes,
                )
            except CaptureUploadTooLargeError as exc:
                raise HTTPException(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    detail="capture_upload_too_large",
                ) from exc
            processed = await _process_capture(
                staged,
                normalized_hosts=normalized_hosts,
                normalized_label=normalized_label,
                request=request,
            )

        loaded_definitions = processed.loaded_definitions
        snapshot = create_analysis_snapshot(
            loaded_definitions.topology,
            SnapshotMetadata(
                analysis_id=validated_analysis_id,
                source=AnalysisSource.SESSION,
                label=normalized_label,
                capture_completeness=(
                    CaptureCompleteness.COMPLETE
                    if staged.queues_path is not None
                    else CaptureCompleteness.DEFINITIONS_ONLY
                ),
                snapshot_revision=processed.snapshot_revision,
                cluster_tags=loaded_definitions.cluster_tags,
                in_cluster_amqp_hosts=normalized_hosts,
                vhost_count=loaded_definitions.vhost_count,
            ),
        )
        publication = await run_blocking(_publish_capture, owner_context, snapshot, reservation)

        upload_response = UploadResponse(
            analysis=analysis_summary(publication.entry),
            replaced=publication.replaced,
        )
        return JSONResponse(
            status_code=(status.HTTP_200_OK if publication.replaced else status.HTTP_201_CREATED),
            content=upload_response.model_dump(mode="json", by_alias=True),
        )
    finally:
        if reservation is not None:
            release_session_upload(reservation)
