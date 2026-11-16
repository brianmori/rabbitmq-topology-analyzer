"""Process-local analysis snapshot stores and lifecycle state."""

import logging
import os
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from threading import RLock
from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

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
    revision_changed: bool = False


@dataclass(frozen=True, slots=True, eq=False)
class SessionUploadReservation:
    """Exclusive same-analysis mutation and optional catalogue-slot claim."""

    owner_context: OwnerContext
    analysis_id: str
    reserves_slot: bool


@dataclass(frozen=True, slots=True, eq=False)
class SessionDeletionReservation:
    """Exclusive claim held while associated run resources are removed."""

    owner_context: OwnerContext
    analysis_id: str
    snapshot_revision: str


@dataclass(frozen=True, slots=True)
class ExpiredSessionAnalysis:
    """One removed session snapshot whose retained runs must be invalidated."""

    owner_context: OwnerContext
    source: AnalysisSource
    analysis_id: str
    snapshot_revision: str
    removed_at: datetime


@dataclass(slots=True)
class _SessionEntry:
    snapshot: AnalysisSnapshot
    last_accessed_at: datetime
    active_job_pins: int = 0


@dataclass(slots=True)
class _AnalysisStoreConfiguration:
    session_analysis_limit: int = _DEFAULT_SESSION_ANALYSIS_LIMIT
    session_analysis_global_limit: int = _DEFAULT_SESSION_ANALYSIS_GLOBAL_LIMIT
    session_topology_ttl: timedelta = timedelta(seconds=_DEFAULT_SESSION_TOPOLOGY_TTL_SECONDS)


@dataclass(slots=True)
class _SessionExpiryListener:
    handler: Callable[[ExpiredSessionAnalysis], None] | None = None


class SessionAnalysisLimitError(Exception):
    """Raised when one owner reaches the retained-analysis limit."""


class SessionStoreCapacityError(Exception):
    """Raised when the process-wide retained-analysis limit is full."""


class AnalysisActiveError(Exception):
    """Raised when publication or deletion targets an analysis with active work."""


_static_store: dict[str, AnalysisSnapshot] = {}
_session_store: dict[tuple[OwnerContext, str], _SessionEntry] = {}
_active_uploads: dict[tuple[OwnerContext, str], SessionUploadReservation] = {}
_active_deletions: dict[tuple[OwnerContext, str], SessionDeletionReservation] = {}
_store_lock = RLock()
_configuration = _AnalysisStoreConfiguration()
_session_expiry_listener = _SessionExpiryListener()
_LOGGER = logging.getLogger(__name__)


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


def set_session_expiry_handler(
    handler: Callable[[ExpiredSessionAnalysis], None] | None,
) -> None:
    """Register the process-local run cleanup invoked after topology expiry."""
    with _store_lock:
        _session_expiry_listener.handler = handler


def _remove_expired_locked(now: datetime) -> tuple[ExpiredSessionAnalysis, ...]:
    expired_keys = [
        key
        for key, entry in _session_store.items()
        if entry.active_job_pins == 0
        and key not in _active_uploads
        and key not in _active_deletions
        and _expiry_for(entry) <= now
    ]
    expired: list[ExpiredSessionAnalysis] = []
    for key in expired_keys:
        owner_context, analysis_id = key
        entry = _session_store.pop(key)
        expired.append(
            ExpiredSessionAnalysis(
                owner_context=owner_context,
                source=AnalysisSource.SESSION,
                analysis_id=analysis_id,
                snapshot_revision=entry.snapshot.snapshot_revision,
                removed_at=now,
            ),
        )
    return tuple(expired)


def _notify_expired(expired: tuple[ExpiredSessionAnalysis, ...]) -> None:
    if not expired:
        return
    with _store_lock:
        handler = _session_expiry_listener.handler
    if handler is None:
        return
    for analysis in expired:
        handler(analysis)


def _notify_expired_without_masking(expired: tuple[ExpiredSessionAnalysis, ...]) -> None:
    """Best-effort cleanup while preserving an already-raised domain error."""
    try:
        _notify_expired(expired)
    except Exception as exc:
        # Cleanup callbacks can fail on an SDK result or filesystem operation.
        # Log only the type so capture data and deployment paths stay private.
        _LOGGER.error(
            "Run cleanup failed while another analysis-store error was active; type=%s.",
            type(exc).__name__,
        )


def reap_expired_session_analyses() -> int:
    """Remove every inactive expired session snapshot and its retained runs."""
    with _store_lock:
        expired = _remove_expired_locked(datetime.now(UTC))
    _notify_expired(expired)
    return len(expired)


def reserve_session_upload(
    owner_context: OwnerContext,
    analysis_id: str,
) -> SessionUploadReservation:
    """Reserve one same-analysis upload and any required catalogue slot."""
    now = datetime.now(UTC)
    key = (owner_context, analysis_id)
    expired: tuple[ExpiredSessionAnalysis, ...] = ()
    try:
        with _store_lock:
            expired = _remove_expired_locked(now)
            if key in _active_uploads or key in _active_deletions:
                raise AnalysisActiveError
            existing = _session_store.get(key)
            if existing is not None and existing.active_job_pins > 0:
                raise AnalysisActiveError
            reserves_slot = existing is None
            if reserves_slot:
                reserved_for_owner = sum(
                    1
                    for reservation in _active_uploads.values()
                    if reservation.reserves_slot and reservation.owner_context == owner_context
                )
                retained_for_owner = sum(
                    1 for owner, _analysis_id in _session_store if owner == owner_context
                )
                deleting_for_owner = sum(
                    1
                    for deletion in _active_deletions.values()
                    if deletion.owner_context == owner_context
                )
                if (
                    retained_for_owner + reserved_for_owner + deleting_for_owner
                    >= _configuration.session_analysis_limit
                ):
                    raise SessionAnalysisLimitError
                globally_reserved = sum(
                    1 for reservation in _active_uploads.values() if reservation.reserves_slot
                )
                if (
                    len(_session_store) + globally_reserved + len(_active_deletions)
                    >= _configuration.session_analysis_global_limit
                ):
                    raise SessionStoreCapacityError
            reservation = SessionUploadReservation(
                owner_context=owner_context,
                analysis_id=analysis_id,
                reserves_slot=reserves_slot,
            )
            _active_uploads[key] = reservation
    except Exception:
        _notify_expired_without_masking(expired)
        raise
    try:
        _notify_expired(expired)
    except Exception:
        # The caller cannot release a reservation it never received. Undo this
        # admission claim before surfacing an unrelated expiry-cleanup failure.
        with _store_lock:
            if _active_uploads.get(key) is reservation:
                _active_uploads.pop(key)
        raise
    return reservation


def release_session_upload(reservation: SessionUploadReservation) -> None:
    """Idempotently release one upload conflict and capacity reservation."""
    key = (reservation.owner_context, reservation.analysis_id)
    with _store_lock:
        if _active_uploads.get(key) is reservation:
            _active_uploads.pop(key)


def _publication_replaced_locked(
    key: tuple[OwnerContext, str],
    owner_context: OwnerContext,
    reservation: SessionUploadReservation | None,
) -> bool:
    """Validate a publication claim and return whether it replaces an entry."""
    active_reservation = _active_uploads.get(key)
    if key in _active_deletions:
        raise AnalysisActiveError
    if reservation is not None:
        if (
            reservation.owner_context != owner_context
            or reservation.analysis_id != key[1]
            or active_reservation is not reservation
        ):
            raise AnalysisActiveError
    elif active_reservation is not None:
        raise AnalysisActiveError
    replaced = key in _session_store
    if replaced and _session_store[key].active_job_pins > 0:
        raise AnalysisActiveError
    if reservation is not None:
        if reservation.reserves_slot == replaced:
            raise AnalysisActiveError
    elif not replaced:
        reserved_for_owner = sum(
            1
            for active in _active_uploads.values()
            if active.reserves_slot and active.owner_context == owner_context
        )
        retained_for_owner = sum(
            1 for owner, _analysis_id in _session_store if owner == owner_context
        )
        deleting_for_owner = sum(
            1 for deletion in _active_deletions.values() if deletion.owner_context == owner_context
        )
        if (
            retained_for_owner + reserved_for_owner + deleting_for_owner
            >= _configuration.session_analysis_limit
        ):
            raise SessionAnalysisLimitError
        globally_reserved = sum(1 for active in _active_uploads.values() if active.reserves_slot)
        if (
            len(_session_store) + globally_reserved + len(_active_deletions)
            >= _configuration.session_analysis_global_limit
        ):
            raise SessionStoreCapacityError
    return replaced


def publish_session_analysis(
    owner_context: OwnerContext,
    snapshot: AnalysisSnapshot,
    *,
    reservation: SessionUploadReservation | None = None,
) -> SessionPublication:
    """Atomically create or replace one caller-owned session analysis."""
    if snapshot.source is not AnalysisSource.SESSION:
        raise ValueError("session publication requires a session snapshot")

    now = datetime.now(UTC)
    key = (owner_context, snapshot.analysis_id)
    expired: tuple[ExpiredSessionAnalysis, ...] = ()
    prior_entry: _SessionEntry | None = None
    entry: _SessionEntry | None = None
    try:
        with _store_lock:
            expired = _remove_expired_locked(now)
            replaced = _publication_replaced_locked(key, owner_context, reservation)
            retained_snapshot = snapshot
            revision_changed = False
            if replaced:
                prior_entry = _session_store[key]
                prior_snapshot = prior_entry.snapshot
                revision_changed = prior_snapshot.snapshot_revision != snapshot.snapshot_revision
                if prior_snapshot.snapshot_revision == snapshot.snapshot_revision:
                    # The topology identity and its retained operation results stay
                    # valid, while non-revision presentation metadata from this
                    # accepted refresh (notably the optional label) still applies.
                    retained_snapshot = replace(snapshot, loaded_at=prior_snapshot.loaded_at)
            entry = _SessionEntry(snapshot=retained_snapshot, last_accessed_at=now)
            _session_store[key] = entry
            publication = SessionPublication(
                entry=AnalysisCatalogueEntry(
                    snapshot=retained_snapshot,
                    expires_at=_expiry_for(entry),
                ),
                replaced=replaced,
                revision_changed=revision_changed,
            )
    except Exception:
        _notify_expired_without_masking(expired)
        raise
    try:
        _notify_expired(expired)
    except Exception:
        # Do not report a failed publication after exposing its replacement.
        # An upload reservation keeps this key exclusive while cleanup runs.
        with _store_lock:
            if entry is not None and _session_store.get(key) is entry:
                if prior_entry is None:
                    _session_store.pop(key)
                else:
                    _session_store[key] = prior_entry
        raise
    return publication


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
        expired = _remove_expired_locked(now)
        matching_entries = (
            (analysis_id, entry)
            for (owner, analysis_id), entry in _session_store.items()
            if owner == owner_context
        )
        entries = tuple(
            AnalysisCatalogueEntry(snapshot=entry.snapshot, expires_at=_expiry_for(entry))
            for _analysis_id, entry in sorted(matching_entries)
        )
    _notify_expired(expired)
    return entries


def reserve_session_analysis_deletion(
    owner_context: OwnerContext,
    analysis_id: str,
) -> SessionDeletionReservation | None:
    """Remove and exclusively reserve one owned snapshot for run cleanup."""
    now = datetime.now(UTC)
    expired: tuple[ExpiredSessionAnalysis, ...] = ()
    removed_entry: _SessionEntry | None = None
    try:
        with _store_lock:
            expired = _remove_expired_locked(now)
            key = (owner_context, analysis_id)
            if key in _active_uploads or key in _active_deletions:
                raise AnalysisActiveError
            entry = _session_store.get(key)
            if entry is not None and entry.active_job_pins > 0:
                raise AnalysisActiveError
            if entry is None:
                reservation = None
            else:
                removed_entry = _session_store.pop(key)
                reservation = SessionDeletionReservation(
                    owner_context=owner_context,
                    analysis_id=analysis_id,
                    snapshot_revision=entry.snapshot.snapshot_revision,
                )
                _active_deletions[key] = reservation
    except Exception:
        _notify_expired_without_masking(expired)
        raise
    try:
        _notify_expired(expired)
    except Exception:
        # Keep deletion atomic when cleanup of a different expired analysis
        # fails: release the unreturned claim and restore its exact entry.
        if reservation is not None and removed_entry is not None:
            with _store_lock:
                if _active_deletions.get(key) is reservation:
                    _active_deletions.pop(key)
                    _session_store[key] = removed_entry
        raise
    return reservation


def release_session_analysis_deletion(reservation: SessionDeletionReservation) -> None:
    """Release one deletion exclusion after terminal resources are removed."""
    key = (reservation.owner_context, reservation.analysis_id)
    with _store_lock:
        if _active_deletions.get(key) is reservation:
            _active_deletions.pop(key)


def delete_session_analysis(owner_context: OwnerContext, analysis_id: str) -> bool:
    """Delete one caller-owned session analysis if it exists and is unexpired."""
    reservation = reserve_session_analysis_deletion(owner_context, analysis_id)
    if reservation is None:
        return False
    release_session_analysis_deletion(reservation)
    return True


def delete_owner_session_analyses(owner_context: OwnerContext) -> int:
    """Remove every inactive session snapshot belonging to one exact owner.

    Authenticated logout first closes owner admission and cancels active upload
    and job work. Treating any remaining reservation or pin as an error keeps a
    lifecycle regression from silently publishing state after cleanup.
    """
    with _store_lock:
        has_upload = any(
            reservation.owner_context == owner_context for reservation in _active_uploads.values()
        )
        has_deletion = any(
            reservation.owner_context == owner_context for reservation in _active_deletions.values()
        )
        owned_entries = [
            (key, entry) for key, entry in _session_store.items() if key[0] == owner_context
        ]
        if (
            has_upload
            or has_deletion
            or any(entry.active_job_pins > 0 for _, entry in owned_entries)
        ):
            raise AnalysisActiveError
        for key, _entry in owned_entries:
            _session_store.pop(key)
        return len(owned_entries)


def pin_analysis_run(
    owner_context: OwnerContext,
    source: AnalysisSource,
    analysis_id: str,
    snapshot_revision: str,
) -> None:
    """Pin one exact session snapshot while an admitted run remains active."""
    if source is AnalysisSource.STATIC:
        return
    now = datetime.now(UTC)
    expired: tuple[ExpiredSessionAnalysis, ...] = ()
    pinned_entry: _SessionEntry | None = None
    previous_access: datetime | None = None
    try:
        with _store_lock:
            expired = _remove_expired_locked(now)
            key = (owner_context, analysis_id)
            if key in _active_uploads or key in _active_deletions:
                raise AnalysisActiveError
            entry = _session_store.get(key)
            if entry is None:
                raise KeyError(analysis_id)
            if entry.snapshot.snapshot_revision != snapshot_revision:
                raise ValueError("snapshot revision changed before run admission")
            pinned_entry = entry
            previous_access = entry.last_accessed_at
            entry.active_job_pins += 1
            entry.last_accessed_at = now
    except Exception:
        _notify_expired_without_masking(expired)
        raise
    try:
        _notify_expired(expired)
    except Exception:
        # Scheduler admission does not create a run when its pin callback
        # raises, so undo the pin the caller can no longer release.
        with _store_lock:
            current = _session_store.get((owner_context, analysis_id))
            if current is pinned_entry and current.active_job_pins > 0:
                current.active_job_pins -= 1
                if current.last_accessed_at == now and previous_access is not None:
                    current.last_accessed_at = previous_access
        raise


def unpin_analysis_run(
    owner_context: OwnerContext,
    source: AnalysisSource,
    analysis_id: str,
    snapshot_revision: str,
) -> None:
    """Release one active-run pin without touching a replacement snapshot."""
    if source is AnalysisSource.STATIC:
        return
    with _store_lock:
        entry = _session_store.get((owner_context, analysis_id))
        if entry is None or entry.snapshot.snapshot_revision != snapshot_revision:
            return
        if entry.active_job_pins <= 0:
            raise RuntimeError("session analysis run pin underflow")
        entry.active_job_pins -= 1


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
        expired = _remove_expired_locked(now)
        entry = _session_store.get((owner_context, analysis_id))
        if entry is not None:
            entry.last_accessed_at = now
            snapshot = entry.snapshot
        else:
            snapshot = None
    _notify_expired(expired)
    return snapshot


def refresh_analysis_run_access(
    owner_context: OwnerContext,
    source: AnalysisSource,
    analysis_id: str,
    snapshot_revision: str,
) -> bool:
    """Validate an exact run snapshot before refreshing either associated TTL."""
    now = datetime.now(UTC)
    with _store_lock:
        if source is AnalysisSource.STATIC:
            snapshot = _static_store.get(analysis_id)
            return snapshot is not None and snapshot.snapshot_revision == snapshot_revision
        expired = _remove_expired_locked(now)
        entry = _session_store.get((owner_context, analysis_id))
        if entry is not None and entry.snapshot.snapshot_revision == snapshot_revision:
            entry.last_accessed_at = now
            accessible = True
        else:
            accessible = False
    _notify_expired(expired)
    return accessible
