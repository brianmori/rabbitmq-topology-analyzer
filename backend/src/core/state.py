"""Process-local analysis snapshot stores and lifecycle state."""

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from threading import RLock
from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from rabbitmq_management_sdk.topology import ClusterTopology

    from core.session import OwnerContext

_DEFAULT_SESSION_ANALYSIS_LIMIT = 5
_DEFAULT_SESSION_ANALYSIS_GLOBAL_LIMIT = 25
_DEFAULT_SESSION_TOPOLOGY_TTL_SECONDS = 28_800


class AnalysisSource(StrEnum):
    """Source namespace used to resolve an analysis."""

    STATIC = "static"
    SESSION = "session"


class CaptureCompleteness(StrEnum):
    """Capture evidence supplied when an analysis snapshot was built."""

    COMPLETE = "complete"
    DEFINITIONS_ONLY = "definitions_only"


@dataclass(frozen=True, slots=True)
class AnalysisSnapshot:
    """Immutable topology plus safe application-owned snapshot metadata."""

    topology: ClusterTopology
    analysis_id: str
    source: AnalysisSource
    label: str
    capture_completeness: CaptureCompleteness
    snapshot_revision: str
    loaded_at: datetime
    cluster_name: str | None
    cluster_tags: Mapping[str, str]
    in_cluster_amqp_hosts: tuple[str, ...]
    vhost_count: int
    description: str | None = None
    owner_contact: str | None = None

    def __post_init__(self) -> None:
        """Freeze copied metadata and require an aware UTC load timestamp."""
        if self.loaded_at.tzinfo is None or self.loaded_at.utcoffset() is None:
            raise ValueError("loaded_at must be timezone-aware")
        object.__setattr__(self, "loaded_at", self.loaded_at.astimezone(UTC))
        object.__setattr__(self, "cluster_tags", MappingProxyType(dict(self.cluster_tags)))


@dataclass(frozen=True, slots=True)
class AnalysisCatalogueEntry:
    """One catalogue snapshot and its current session expiry, when applicable."""

    snapshot: AnalysisSnapshot
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class SessionPublication:
    """Result of atomically publishing a caller-owned session snapshot."""

    entry: AnalysisCatalogueEntry
    replaced: bool


@dataclass(slots=True)
class _SessionEntry:
    snapshot: AnalysisSnapshot
    last_accessed_at: datetime


@dataclass(slots=True)
class _AnalysisStoreConfiguration:
    session_analysis_limit: int = _DEFAULT_SESSION_ANALYSIS_LIMIT
    session_analysis_global_limit: int = _DEFAULT_SESSION_ANALYSIS_GLOBAL_LIMIT
    session_topology_ttl: timedelta = timedelta(seconds=_DEFAULT_SESSION_TOPOLOGY_TTL_SECONDS)


class SessionAnalysisLimitError(Exception):
    """Raised when one owner reaches the retained-analysis limit."""


class SessionStoreCapacityError(Exception):
    """Raised when the process-wide retained-analysis limit is full."""


_static_store: dict[str, AnalysisSnapshot] = {}
_session_store: dict[tuple[OwnerContext, str], _SessionEntry] = {}
_store_lock = RLock()
_configuration = _AnalysisStoreConfiguration()


def _positive_environment_integer(name: str, default: int) -> int:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def configure_analysis_store() -> None:
    """Load and validate session catalogue capacity and TTL configuration."""
    _configuration.session_analysis_limit = _positive_environment_integer(
        "RABBITMQ_SESSION_ANALYSIS_LIMIT",
        _DEFAULT_SESSION_ANALYSIS_LIMIT,
    )
    _configuration.session_analysis_global_limit = _positive_environment_integer(
        "RABBITMQ_SESSION_ANALYSIS_GLOBAL_LIMIT",
        _DEFAULT_SESSION_ANALYSIS_GLOBAL_LIMIT,
    )
    ttl_seconds = _positive_environment_integer(
        "RABBITMQ_SESSION_TOPOLOGY_TTL_SECONDS",
        _DEFAULT_SESSION_TOPOLOGY_TTL_SECONDS,
    )
    _configuration.session_topology_ttl = timedelta(seconds=ttl_seconds)


def replace_static_analyses(snapshots: Mapping[str, AnalysisSnapshot]) -> None:
    """Atomically replace the startup-loaded static catalogue."""
    with _store_lock:
        _static_store.clear()
        _static_store.update(snapshots)


def _expiry_for(entry: _SessionEntry) -> datetime:
    return entry.last_accessed_at + _configuration.session_topology_ttl


def _remove_expired(now: datetime) -> None:
    expired_keys = [key for key, entry in _session_store.items() if _expiry_for(entry) <= now]
    for key in expired_keys:
        del _session_store[key]


def publish_session_analysis(
    owner_context: OwnerContext,
    snapshot: AnalysisSnapshot,
) -> SessionPublication:
    """Atomically create or replace one caller-owned session analysis."""
    if snapshot.source is not AnalysisSource.SESSION:
        raise ValueError("session publication requires a session snapshot")

    now = datetime.now(UTC)
    key = (owner_context, snapshot.analysis_id)
    with _store_lock:
        _remove_expired(now)
        replaced = key in _session_store
        if not replaced:
            owner_count = sum(1 for owner, _analysis_id in _session_store if owner == owner_context)
            if owner_count >= _configuration.session_analysis_limit:
                raise SessionAnalysisLimitError
            if len(_session_store) >= _configuration.session_analysis_global_limit:
                raise SessionStoreCapacityError
        retained_snapshot = snapshot
        if replaced:
            prior_snapshot = _session_store[key].snapshot
            if prior_snapshot.snapshot_revision == snapshot.snapshot_revision:
                retained_snapshot = prior_snapshot
        entry = _SessionEntry(snapshot=retained_snapshot, last_accessed_at=now)
        _session_store[key] = entry
        return SessionPublication(
            entry=AnalysisCatalogueEntry(snapshot=retained_snapshot, expires_at=_expiry_for(entry)),
            replaced=replaced,
        )


def list_static_analyses() -> tuple[AnalysisCatalogueEntry, ...]:
    """Return the immutable static catalogue in analysis-ID order."""
    with _store_lock:
        return tuple(
            AnalysisCatalogueEntry(snapshot=snapshot, expires_at=None)
            for _analysis_id, snapshot in sorted(_static_store.items())
        )


def list_session_analyses(owner_context: OwnerContext) -> tuple[AnalysisCatalogueEntry, ...]:
    """Return one owner's unexpired analyses without refreshing their TTL."""
    now = datetime.now(UTC)
    with _store_lock:
        _remove_expired(now)
        matching_entries = (
            (analysis_id, entry)
            for (owner, analysis_id), entry in _session_store.items()
            if owner == owner_context
        )
        return tuple(
            AnalysisCatalogueEntry(snapshot=entry.snapshot, expires_at=_expiry_for(entry))
            for _analysis_id, entry in sorted(matching_entries)
        )


def delete_session_analysis(owner_context: OwnerContext, analysis_id: str) -> bool:
    """Delete one caller-owned session analysis if it exists and is unexpired."""
    now = datetime.now(UTC)
    with _store_lock:
        _remove_expired(now)
        return _session_store.pop((owner_context, analysis_id), None) is not None


def resolve_analysis(
    source: AnalysisSource,
    analysis_id: str,
    owner_context: OwnerContext | None,
) -> AnalysisSnapshot | None:
    """Resolve one static or caller-owned session snapshot atomically."""
    now = datetime.now(UTC)
    with _store_lock:
        if source is AnalysisSource.STATIC:
            return _static_store.get(analysis_id)
        if owner_context is None:
            return None
        _remove_expired(now)
        entry = _session_store.get((owner_context, analysis_id))
        if entry is None:
            return None
        entry.last_accessed_at = now
        return entry.snapshot
