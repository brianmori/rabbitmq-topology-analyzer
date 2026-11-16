"""Safe structural-cycle run, paging, and exact-detail schemas."""

from datetime import datetime  # noqa: TC003 - Pydantic resolves this at runtime.
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.schemas import CamelModel
from core.state import AnalysisSource  # noqa: TC001 - Pydantic resolves this at runtime.
from jobs.models import (  # noqa: TC001 - Pydantic resolves these enums at runtime.
    OperationKind,
    RunStatus,
)

if TYPE_CHECKING:
    from typing import Self

_REVISION_PATTERN = r"^sha256:[0-9a-f]{64}$"


class CycleSort(StrEnum):
    """Supported deterministic cycle-summary ordering modes."""

    SDK = "sdk"
    LENGTH_ASC = "length_asc"
    LENGTH_DESC = "length_desc"


class CyclePageQuery(BaseModel):
    """Bounded summary filters using the API's snake-case query names."""

    model_config = ConfigDict(extra="forbid")

    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=100)
    sort: CycleSort = CycleSort.SDK
    vhost: list[str] | None = None
    min_length: int | None = Field(default=None, ge=1)
    max_length: int | None = Field(default=None, ge=1)


class CyclePageRequestQuery(CyclePageQuery):
    """Exact snapshot plus the bounded cycle-page query contract."""

    snapshot_revision: str = Field(pattern=_REVISION_PATTERN)


class CycleStartRequest(CamelModel):
    """Exact snapshot and SDK enumeration ceiling for one cycle run."""

    model_config = ConfigDict(extra="forbid")

    source: AnalysisSource
    analysis_id: str
    snapshot_revision: str = Field(pattern=_REVISION_PATTERN)
    max_cycles: int = Field(default=20, ge=0, strict=True)


class RunErrorResponse(CamelModel):
    """Safe terminal failure exposed with one run."""

    code: str
    message: str


class CycleRunResult(CamelModel):
    """Small completed-result metadata returned from the run resource."""

    cycle_count: int = Field(ge=0)
    truncated: bool
    max_cycles: int = Field(ge=0)


class CycleRunResponse(CamelModel):
    """Caller-owned structural-cycle run resource."""

    run_id: str
    operation: Literal[OperationKind.STRUCTURAL_CYCLES]
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
    result: CycleRunResult | None


class StoredCycle(CamelModel):
    """One exact SDK cycle retained under its original deterministic index."""

    cycle_index: int = Field(ge=0)
    length: int = Field(ge=1)
    vhosts: tuple[str, ...]
    node_ids: tuple[str, ...]
    edge_ids: tuple[str, ...]

    @model_validator(mode="after")
    def validate_path_lengths(self) -> Self:
        """Require one stored node and representative edge per cycle hop."""
        if len(self.node_ids) != self.length or len(self.edge_ids) != self.length:
            raise ValueError("stored cycle path lengths do not match")
        return self


class StoredCycleSummary(CamelModel):
    """Lightweight indexed record used for filtering and summary pages."""

    cycle_index: int = Field(ge=0)
    length: int = Field(ge=1)
    vhosts: tuple[str, ...]
    preview_node_ids: tuple[str, ...]

    @model_validator(mode="after")
    def validate_preview_length(self) -> Self:
        """Retain no more than the contract's first three ordered nodes."""
        if len(self.preview_node_ids) > min(3, self.length):
            raise ValueError("stored cycle summary preview is too long")
        return self


class StoredCycleMetadataEnvelope(CamelModel):
    """Small versioned metadata frame stored ahead of cycle indexes."""

    schema_version: Literal[1]
    operation: Literal[OperationKind.STRUCTURAL_CYCLES]
    source: AnalysisSource
    analysis_id: str
    snapshot_revision: str
    parameters: dict[str, str | int | bool | None]
    result: CycleRunResult


class CycleSummary(CamelModel):
    """Bounded list representation of one original SDK cycle."""

    cycle_index: int
    length: int
    vhosts: tuple[str, ...]
    preview_node_ids: tuple[str, ...]


class CyclePage(CamelModel):
    """Filtered, deterministically sorted page from one completed run."""

    run_id: str
    snapshot_revision: str
    offset: int
    limit: int
    total: int
    sort: CycleSort
    truncated: bool
    items: tuple[CycleSummary, ...]


class CycleDetail(CamelModel):
    """Complete ordered path for one original SDK cycle index."""

    run_id: str
    snapshot_revision: str
    cycle_index: int
    length: int
    vhosts: tuple[str, ...]
    node_ids: tuple[str, ...]
    edge_ids: tuple[str, ...]
