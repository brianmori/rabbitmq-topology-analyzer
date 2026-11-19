"""Safe SCC start, run-state, and persisted-result schemas."""

from datetime import datetime  # noqa: TC003 - Pydantic resolves this at runtime.
from typing import TYPE_CHECKING, Literal

from pydantic import ConfigDict, Field

from core.schemas import CamelModel
from core.state import AnalysisSource  # noqa: TC001 - Pydantic resolves this at runtime.
from jobs.models import (
    AnalysisRun,
    OperationKind,
    RunStatus,
)

if TYPE_CHECKING:
    from jobs.results import ResultRepository

_REVISION_PATTERN = r"^sha256:[0-9a-f]{64}$"


class SccStartRequest(CamelModel):
    """Exact immutable snapshot selected for cyclic-SCC analysis."""

    model_config = ConfigDict(extra="forbid")

    source: AnalysisSource
    analysis_id: str
    snapshot_revision: str = Field(pattern=_REVISION_PATTERN)


class RunErrorResponse(CamelModel):
    """Safe terminal failure exposed with one run."""

    code: str
    message: str


class SccComponent(CamelModel):
    """One deterministic SDK cyclic component membership."""

    component_index: int
    is_cyclic: Literal[True]
    node_count: int
    vhosts: tuple[str, ...]
    node_ids: tuple[str, ...]


class SccResult(CamelModel):
    """Complete bounded SCC result returned inline with a successful run."""

    components: tuple[SccComponent, ...]


class SccRunResponse(CamelModel):
    """Caller-owned cyclic-SCC run resource."""

    run_id: str
    operation: Literal[OperationKind.CYCLIC_SCC]
    status: RunStatus
    source: AnalysisSource
    analysis_id: str
    snapshot_revision: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    heartbeat_expires_at: datetime | None
    result_expires_at: datetime | None
    error: RunErrorResponse | None
    result: SccResult | None


class StoredSccEnvelope(CamelModel):
    """Versioned internal envelope validated before serving a result."""

    schema_version: Literal[1]
    operation: Literal[OperationKind.CYCLIC_SCC]
    source: AnalysisSource
    analysis_id: str
    snapshot_revision: str
    parameters: dict[str, str | int | bool | None]
    result: SccResult


def scc_run_response(run: AnalysisRun, repository: ResultRepository) -> SccRunResponse:
    """Adapt run metadata and validate its committed SCC envelope."""
    result: SccResult | None = None
    if run.status is RunStatus.SUCCEEDED:
        envelope = StoredSccEnvelope.model_validate_json(repository.read(run.run_id))
        if (
            envelope.source is not run.source
            or envelope.analysis_id != run.analysis_id
            or envelope.snapshot_revision != run.snapshot_revision
            or envelope.parameters
        ):
            raise ValueError("committed SCC result metadata does not match its run")
        result = envelope.result
    error = (
        RunErrorResponse(code=run.error.code, message=run.error.message)
        if run.error is not None
        else None
    )
    return SccRunResponse(
        run_id=run.run_id,
        operation=OperationKind.CYCLIC_SCC,
        status=run.status,
        source=run.source,
        analysis_id=run.analysis_id,
        snapshot_revision=run.snapshot_revision,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        heartbeat_expires_at=run.heartbeat_expires_at,
        result_expires_at=run.result_expires_at,
        error=error,
        result=result,
    )
