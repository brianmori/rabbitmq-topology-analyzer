"""Session capture upload endpoint."""

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status

from analyses.schemas import UploadResponse, analysis_summary
from analyses.snapshots import SnapshotMetadata, compute_snapshot_revision, create_analysis_snapshot
from analyses.validation import (
    normalize_in_cluster_amqp_hosts,
    normalize_label,
    validate_analysis_id,
)
from core.security import require_analyst
from core.session import OwnerContext, require_owner_context
from core.state import (
    AnalysisSource,
    CaptureCompleteness,
    SessionAnalysisLimitError,
    SessionStoreCapacityError,
    publish_session_analysis,
)
from definitions.sdk_bridge import DefinitionsLoadError, load_definitions

router = APIRouter(tags=["definitions"], dependencies=[Depends(require_analyst)])


def _validation_error(message: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=message)


@router.post("/definitions/upload", status_code=status.HTTP_201_CREATED)
async def upload_definitions(
    analysis_id: Annotated[str, Form(...)],
    definitions_file: Annotated[UploadFile, File(...)],
    owner_context: Annotated[OwnerContext, Depends(require_owner_context)],
    response: Response,
    queues_file: Annotated[UploadFile | None, File()] = None,
    exchanges_file: Annotated[UploadFile | None, File()] = None,
    in_cluster_amqp_host: Annotated[list[str] | None, Form()] = None,
    label: Annotated[str | None, Form()] = None,
) -> UploadResponse:
    """Validate and atomically publish a caller-owned session capture."""
    try:
        validated_analysis_id = validate_analysis_id(analysis_id)
        normalized_label = normalize_label(label if label is not None else analysis_id)
        normalized_hosts = normalize_in_cluster_amqp_hosts(in_cluster_amqp_host or ())
    except ValueError as exc:
        raise _validation_error(str(exc)) from exc
    if (queues_file is None) != (exchanges_file is None):
        raise _validation_error("queues_file and exchanges_file must be supplied together")

    definitions_bytes = await definitions_file.read()
    queues_bytes = await queues_file.read() if queues_file is not None else None
    exchanges_bytes = await exchanges_file.read() if exchanges_file is not None else None

    with TemporaryDirectory(prefix="rabbitmq-topology-") as temporary_directory:
        temporary_root = Path(temporary_directory)
        definitions_path = temporary_root / "definitions.json"
        definitions_path.write_bytes(definitions_bytes)
        queues_path = temporary_root / "queues.json" if queues_bytes is not None else None
        exchanges_path = temporary_root / "exchanges.json" if exchanges_bytes is not None else None
        if queues_path is not None and queues_bytes is not None:
            queues_path.write_bytes(queues_bytes)
        if exchanges_path is not None and exchanges_bytes is not None:
            exchanges_path.write_bytes(exchanges_bytes)
        try:
            loaded_definitions = load_definitions(
                definitions_path,
                queues_path=queues_path,
                exchanges_path=exchanges_path,
                in_cluster_amqp_hosts=normalized_hosts,
                cluster_label=normalized_label,
            )
        except DefinitionsLoadError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Uploaded files are not a valid RabbitMQ capture.",
            ) from exc

    snapshot = create_analysis_snapshot(
        loaded_definitions.topology,
        SnapshotMetadata(
            analysis_id=validated_analysis_id,
            source=AnalysisSource.SESSION,
            label=normalized_label,
            capture_completeness=(
                CaptureCompleteness.COMPLETE
                if queues_bytes is not None
                else CaptureCompleteness.DEFINITIONS_ONLY
            ),
            snapshot_revision=compute_snapshot_revision(
                definitions_bytes=definitions_bytes,
                queues_bytes=queues_bytes,
                exchanges_bytes=exchanges_bytes,
                in_cluster_amqp_hosts=normalized_hosts,
            ),
            cluster_tags=loaded_definitions.cluster_tags,
            in_cluster_amqp_hosts=normalized_hosts,
            vhost_count=loaded_definitions.vhost_count,
        ),
    )
    try:
        publication = publish_session_analysis(owner_context, snapshot)
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

    if publication.replaced:
        response.status_code = status.HTTP_200_OK
    return UploadResponse(
        analysis=analysis_summary(publication.entry),
        replaced=publication.replaced,
    )
