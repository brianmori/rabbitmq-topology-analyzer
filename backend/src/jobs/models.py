"""Closed analysis-run state and scheduler-facing value objects."""

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from rabbitmq_management_sdk.topology import ClusterTopology

    from core.session import OwnerContext
    from core.state import AnalysisSource

_UUID_VERSION = 4


class OperationKind(StrEnum):
    """SDK operation executed by a background run."""

    CYCLIC_SCC = "cyclic_scc"
    STRUCTURAL_CYCLES = "structural_cycles"


class RunStatus(StrEnum):
    """Closed analysis-run status vocabulary."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        """Return whether no further state transition is possible."""
        return self in {self.SUCCEEDED, self.FAILED, self.CANCELLED}


@dataclass(frozen=True, slots=True)
class RunError:
    """Safe machine-readable terminal worker failure."""

    code: str
    message: str


@dataclass(frozen=True, slots=True)
class ResultEnvelopeContext:
    """Snapshot-scoped metadata persisted with an operation result."""

    operation: OperationKind
    source: AnalysisSource
    analysis_id: str
    snapshot_revision: str
    parameters: Mapping[str, str | int | bool | None]


@dataclass(frozen=True, slots=True)
class AnalysisRun:
    """Small process-owned metadata for one caller-owned operation."""

    run_id: str
    owner_context: OwnerContext
    operation: OperationKind
    status: RunStatus
    source: AnalysisSource
    analysis_id: str
    snapshot_revision: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    heartbeat_expires_at: datetime | None
    result_expires_at: datetime | None
    error: RunError | None


@dataclass(frozen=True, slots=True)
class AdmissionResult:
    """New or idempotently replayed run returned by admission."""

    run: AnalysisRun
    replayed: bool


@dataclass(frozen=True, slots=True)
class JobStart:
    """Complete immutable execution request supplied to admission."""

    owner_context: OwnerContext
    operation: OperationKind
    source: AnalysisSource
    analysis_id: str
    snapshot_revision: str
    topology: ClusterTopology
    parameters: dict[str, str | int | bool | None]
    worker: WorkerFunction


class WorkerFunction(Protocol):
    """Picklable domain bridge executed inside one spawned child."""

    def __call__(
        self,
        topology: ClusterTopology,
        context: ResultEnvelopeContext,
    ) -> bytes:
        """Return one complete versioned result representation as bytes."""
        ...


class JobAdmissionError(Exception):
    """Base class for safe admission failures."""


class OwnerActiveRunError(JobAdmissionError):
    """Raised when an owner already has queued or running work."""


class IdempotencyConflictError(JobAdmissionError):
    """Raised when one idempotency key is reused for a changed payload."""


class JobQueueFullError(JobAdmissionError):
    """Raised when neither a worker nor waiting position is available."""


class SchedulerShuttingDownError(JobAdmissionError):
    """Raised when new work arrives during graceful shutdown."""


def parse_uuid4(value: str, *, field_name: str) -> str:
    """Validate a canonical hyphenated UUIDv4 and return lowercase text."""
    try:
        parsed = UUID(value)
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a UUIDv4") from exc
    if parsed.version != _UUID_VERSION or str(parsed) != value.lower():
        raise ValueError(f"{field_name} must be a canonical UUIDv4")
    return str(parsed)


def canonical_start_payload(
    *,
    source: AnalysisSource,
    analysis_id: str,
    snapshot_revision: str,
    parameters: Mapping[str, str | int | bool | None],
) -> bytes:
    """Serialize the exact idempotency comparison payload deterministically."""
    return json.dumps(
        {
            "analysisId": analysis_id,
            "parameters": dict(parameters),
            "snapshotRevision": snapshot_revision,
            "source": source.value,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def encode_result_envelope(
    context: ResultEnvelopeContext,
    result: Mapping[str, object],
) -> bytes:
    """Build the common versioned operation-result envelope."""
    return json.dumps(
        {
            "schemaVersion": 1,
            "operation": context.operation.value,
            "source": context.source.value,
            "analysisId": context.analysis_id,
            "snapshotRevision": context.snapshot_revision,
            "parameters": dict(context.parameters),
            "result": dict(result),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
