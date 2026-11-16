"""Bounded owner-scoped scheduler for isolated analysis workers."""

import logging
import signal
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from multiprocessing import get_context
from threading import Event, Lock, RLock, Thread, current_thread
from typing import TYPE_CHECKING
from uuid import uuid4

from jobs.models import (
    AdmissionResult,
    AnalysisRun,
    IdempotencyConflictError,
    JobQueueFullError,
    JobStart,
    OperationKind,
    OwnerActiveRunError,
    ResultEnvelopeContext,
    RunError,
    RunStatus,
    SchedulerShuttingDownError,
)
from jobs.processes import terminate_process
from jobs.worker import run_operation_child

if TYPE_CHECKING:
    from collections.abc import Callable
    from multiprocessing.connection import Connection
    from multiprocessing.context import SpawnContext
    from multiprocessing.process import BaseProcess

    from rabbitmq_management_sdk.topology import ClusterTopology

    from core.session import OwnerContext
    from core.state import AnalysisSource, ExpiredSessionAnalysis
    from jobs.config import JobSettings
    from jobs.models import WorkerFunction
    from jobs.results import ResultRepository, ResultWriteSpec

_RESERVE_MESSAGE_LENGTH = 2
_FAILURE_MESSAGE_LENGTH = 3
_LOGGER = logging.getLogger(__name__)


def _log_cleanup_failure(phase: str, error: Exception) -> None:
    """Report a background failure without exception text, paths, or traceback."""
    _LOGGER.error(
        "Background cleanup failed; phase=%s type=%s.",
        phase,
        type(error).__name__,
    )


class ComputeSlotLease:
    """Idempotently releasable claim on shared process compute capacity."""

    def __init__(self, pool: ComputeSlotPool) -> None:
        """Create one live lease belonging to ``pool``."""
        self._pool = pool
        self._released = False
        self._lock = Lock()

    def release(self) -> None:
        """Return this claim to the pool exactly once."""
        with self._lock:
            if self._released:
                return
            self._released = True
        self._pool._release()


class ComputeSlotPool:
    """Shared immediate-admission capacity for jobs and future upload parsers."""

    def __init__(self, capacity: int) -> None:
        """Create a pool with a fixed positive capacity."""
        if capacity <= 0:
            raise ValueError("compute-slot capacity must be positive")
        self._capacity = capacity
        self._leased = 0
        self._lock = Lock()

    def try_acquire(self) -> ComputeSlotLease | None:
        """Return an immediate lease, or ``None`` when every slot is busy."""
        with self._lock:
            if self._leased >= self._capacity:
                return None
            self._leased += 1
        return ComputeSlotLease(self)

    def _release(self) -> None:
        with self._lock:
            if self._leased <= 0:
                raise RuntimeError("compute-slot pool was released without a lease")
            self._leased -= 1

    @property
    def leased(self) -> int:
        """Return the current number of occupied slots."""
        with self._lock:
            return self._leased


@dataclass(frozen=True, slots=True)
class _ExecutionRequest:
    topology: ClusterTopology
    worker: WorkerFunction
    context: ResultEnvelopeContext


@dataclass(frozen=True, slots=True)
class _WorkerHandle:
    process: BaseProcess
    connection: Connection
    lease: ComputeSlotLease
    write_spec: ResultWriteSpec


@dataclass(frozen=True, slots=True)
class _ImmediateProcessHandle:
    """One registered synchronous parser child and its termination lock."""

    process: BaseProcess
    termination_lock: Lock


class JobScheduler:
    """Own the closed run state machine, process handles, and waiting queue."""

    def __init__(
        self,
        settings: JobSettings,
        result_repository: ResultRepository,
        *,
        compute_slots: ComputeSlotPool | None = None,
        process_context: SpawnContext | None = None,
        terminal_callback: Callable[[AnalysisRun], None] | None = None,
        analysis_access_callback: Callable[[AnalysisRun], bool] | None = None,
        session_expiry_reaper: Callable[[], int] | None = None,
    ) -> None:
        """Create an empty scheduler around one result repository."""
        self._settings = settings
        self._results = result_repository
        self._compute_slots = compute_slots or ComputeSlotPool(settings.worker_count)
        self._process_context = process_context or get_context("spawn")
        self._terminal_callback = terminal_callback
        self._analysis_access_callback = analysis_access_callback
        self._session_expiry_reaper = session_expiry_reaper
        self._lock = RLock()
        self._runs: dict[str, AnalysisRun] = {}
        self._executions: dict[str, _ExecutionRequest] = {}
        self._handles: dict[str, _WorkerHandle] = {}
        self._waiting: deque[str] = deque()
        self._active_by_owner: dict[OwnerContext, str] = {}
        self._idempotency: dict[tuple[OwnerContext, OperationKind, str], tuple[str, bytes]] = {}
        self._idempotency_by_run: dict[str, tuple[OwnerContext, OperationKind, str]] = {}
        self._cancel_locks: dict[str, Lock] = {}
        self._pending_result_cleanup: set[str] = set()
        self._immediate_leases: set[ComputeSlotLease] = set()
        self._immediate_processes: dict[ComputeSlotLease, _ImmediateProcessHandle] = {}
        self._accepting = True
        self._reaper_stop = Event()
        self._reaper_thread: Thread | None = None

    @property
    def result_repository(self) -> ResultRepository:
        """Expose stable result reads to operation response adapters."""
        return self._results

    @property
    def compute_slots(self) -> ComputeSlotPool:
        """Expose immediate shared capacity to later upload parser work."""
        return self._compute_slots

    def try_acquire_immediate(self) -> ComputeSlotLease | None:
        """Claim unqueued shared capacity for one synchronous upload parser."""
        with self._lock:
            if not self._accepting:
                return None
            lease = self._compute_slots.try_acquire()
            if lease is not None:
                self._immediate_leases.add(lease)
            return lease

    def start_immediate_process(
        self,
        lease: ComputeSlotLease,
        process: BaseProcess,
    ) -> bool:
        """Atomically start and register one immediate parser child.

        Returning ``False`` means shutdown won the race after the lease was
        acquired; the caller must not start the process and must release the
        lease through :meth:`release_immediate`.
        """
        with self._lock:
            if not self._accepting or lease not in self._immediate_leases:
                return False
            process.start()
            self._immediate_processes[lease] = _ImmediateProcessHandle(
                process=process,
                termination_lock=Lock(),
            )
            return True

    def terminate_immediate_process(
        self,
        lease: ComputeSlotLease,
        process: BaseProcess,
    ) -> None:
        """Terminate one parser child without racing scheduler shutdown."""
        with self._lock:
            handle = self._immediate_processes.get(lease)
        if handle is not None and handle.process is process:
            with handle.termination_lock:
                terminate_process(process)
            return
        # Covers a failed start as well as a process already reaped by
        # shutdown. ``terminate_process`` is idempotent for both states.
        terminate_process(process)

    def release_immediate(self, lease: ComputeSlotLease) -> None:
        """Release upload-parser capacity and promote one waiting job."""
        with self._lock:
            if lease not in self._immediate_leases:
                return
            handle = self._immediate_processes.get(lease)
            if handle is not None and handle.process.is_alive():
                raise RuntimeError("cannot release capacity for a live immediate process")
            self._immediate_processes.pop(lease, None)
            self._immediate_leases.remove(lease)
            lease.release()
            if self._accepting:
                self._promote_locked()

    def start(self) -> None:
        """Start periodic heartbeat and terminal-result cleanup."""
        with self._lock:
            if self._reaper_thread is not None:
                return
            self._reaper_stop.clear()
            interval = min(1.0, max(0.05, self._settings.heartbeat_ttl.total_seconds() / 4))
            self._reaper_thread = Thread(
                target=self._reaper_loop,
                args=(interval,),
                name="analysis-job-reaper",
                daemon=True,
            )
            self._reaper_thread.start()

    def admit(
        self,
        *,
        start: JobStart,
        canonical_payload: bytes,
        idempotency_key: str,
        on_admit: Callable[[], None] | None = None,
    ) -> AdmissionResult:
        """Atomically replay or admit one run within owner/global bounds."""
        now = datetime.now(UTC)
        scope = (start.owner_context, start.operation, idempotency_key)
        with self._lock:
            self._expire_terminal_locked(now)
            existing = self._idempotency.get(scope)
            if existing is not None:
                run_id, existing_payload = existing
                if existing_payload != canonical_payload:
                    raise IdempotencyConflictError
                run = self._runs[run_id]
                if run.status.is_terminal:
                    run = self._refresh_terminal_locked(run, now)
                return AdmissionResult(run=run, replayed=True)
            if not self._accepting:
                raise SchedulerShuttingDownError
            if start.owner_context in self._active_by_owner:
                raise OwnerActiveRunError

            lease = self._compute_slots.try_acquire()
            if lease is None and len(self._waiting) >= self._settings.queue_capacity:
                raise JobQueueFullError
            try:
                if on_admit is not None:
                    on_admit()
            except Exception:
                if lease is not None:
                    lease.release()
                raise

            admitted_at = datetime.now(UTC)
            run_id = str(uuid4())
            status = RunStatus.RUNNING if lease is not None else RunStatus.QUEUED
            run = AnalysisRun(
                run_id=run_id,
                owner_context=start.owner_context,
                operation=start.operation,
                status=status,
                source=start.source,
                analysis_id=start.analysis_id,
                snapshot_revision=start.snapshot_revision,
                created_at=admitted_at,
                started_at=None,
                finished_at=None,
                heartbeat_expires_at=admitted_at + self._settings.heartbeat_ttl,
                result_expires_at=None,
                error=None,
            )
            self._runs[run_id] = run
            self._executions[run_id] = _ExecutionRequest(
                topology=start.topology,
                worker=start.worker,
                context=ResultEnvelopeContext(
                    operation=start.operation,
                    source=start.source,
                    analysis_id=start.analysis_id,
                    snapshot_revision=start.snapshot_revision,
                    parameters=start.parameters,
                ),
            )
            self._active_by_owner[start.owner_context] = run_id
            self._idempotency[scope] = (run_id, canonical_payload)
            self._idempotency_by_run[run_id] = scope
            self._cancel_locks[run_id] = Lock()
            if lease is None:
                self._waiting.append(run_id)
            else:
                self._launch_locked(run_id, lease)
            return AdmissionResult(run=self._runs[run_id], replayed=False)

    def get_owned(self, run_id: str, owner_context: OwnerContext) -> AnalysisRun | None:
        """Read one owned run and renew its active lease or terminal TTL."""
        now = datetime.now(UTC)
        with self._lock:
            self._expire_terminal_locked(now)
            run = self._runs.get(run_id)
            if run is None or run.owner_context != owner_context:
                return None

        # Validate and touch the exact topology before renewing the run. This
        # callback may invalidate an expired run, so it must execute without
        # the scheduler lock and the run must be looked up again afterwards.
        if self._analysis_access_callback is not None and not self._analysis_access_callback(run):
            return None

        now = datetime.now(UTC)
        with self._lock:
            self._expire_terminal_locked(now)
            run = self._runs.get(run_id)
            if run is None or run.owner_context != owner_context:
                return None
            if run.status.is_terminal:
                return self._refresh_terminal_locked(run, now)
            run = replace(run, heartbeat_expires_at=now + self._settings.heartbeat_ttl)
            self._runs[run_id] = run
            return run

    def cancel_owned(self, run_id: str, owner_context: OwnerContext) -> AnalysisRun | None:
        """Cancel one owned active run and return only after its process exits."""
        with self._lock:
            run = self._runs.get(run_id)
            if run is None or run.owner_context != owner_context:
                return None
        return self._cancel_run(run_id)

    def invalidate_analysis(
        self,
        owner_context: OwnerContext,
        source: AnalysisSource,
        analysis_id: str,
        *,
        keep_snapshot_revision: str | None = None,
    ) -> None:
        """Delete terminal runs made stale by revision replacement or deletion."""
        with self._lock:
            stale_run_ids = [
                run_id
                for run_id, run in self._runs.items()
                if run.status.is_terminal
                and run.owner_context == owner_context
                and run.source == source
                and run.analysis_id == analysis_id
                and (
                    keep_snapshot_revision is None
                    or run.snapshot_revision != keep_snapshot_revision
                )
            ]
            for run_id in stale_run_ids:
                self._remove_terminal_locked(run_id)

    def invalidate_expired_analysis(self, expired: ExpiredSessionAnalysis) -> None:
        """Reap terminal resources for an expired snapshot, retaining failed cleanup."""
        with self._lock:
            stale_run_ids = [
                run_id
                for run_id, run in self._runs.items()
                if run.status.is_terminal
                and run.owner_context == expired.owner_context
                and run.source == expired.source
                and run.analysis_id == expired.analysis_id
                and run.snapshot_revision == expired.snapshot_revision
                and run.created_at < expired.removed_at
            ]
            for run_id in stale_run_ids:
                # Snapshot expiry is automatic, including when discovered by a
                # foreground read. One failed file must not stop later expiry
                # notifications after the store has already removed snapshots.
                self._remove_terminal_in_background_locked(run_id)

    def cleanup_owner(self, owner_context: OwnerContext) -> int:
        """Cancel active work and remove every retained resource for one owner."""
        with self._lock:
            owned_run_ids = [
                run_id for run_id, run in self._runs.items() if run.owner_context == owner_context
            ]
            owned_active = [
                run_id for run_id in owned_run_ids if not self._runs[run_id].status.is_terminal
            ]
        for run_id in owned_active:
            self._cancel_run(run_id)

        with self._lock:
            owned_terminal = [
                run_id
                for run_id, run in self._runs.items()
                if run.owner_context == owner_context and run.status.is_terminal
            ]
            for run_id in owned_terminal:
                self._remove_terminal_locked(run_id)
            return len(owned_run_ids)

    def _launch_locked(self, run_id: str, lease: ComputeSlotLease) -> None:
        request = self._executions[run_id]
        now = datetime.now(UTC)
        run = replace(
            self._runs[run_id],
            status=RunStatus.RUNNING,
            started_at=now,
            heartbeat_expires_at=now + self._settings.heartbeat_ttl,
        )
        self._runs[run_id] = run
        parent_connection: Connection | None = None
        child_connection: Connection | None = None
        try:
            write_spec = self._results.prepare(run_id)
            parent_connection, child_connection = self._process_context.Pipe(duplex=True)
            process = self._process_context.Process(
                target=run_operation_child,
                args=(
                    request.worker,
                    request.topology,
                    request.context,
                    write_spec,
                    child_connection,
                ),
                name=f"analysis-{run.operation.value}-{run_id}",
            )
            process.start()
        except Exception:
            if parent_connection is not None:
                parent_connection.close()
            if child_connection is not None:
                child_connection.close()
            self._results.abort(run_id)
            lease.release()
            self._finish_locked(
                run_id,
                RunStatus.FAILED,
                RunError("worker_start_failed", "The analysis worker could not start."),
                promote=False,
            )
            return
        child_connection.close()
        self._handles[run_id] = _WorkerHandle(
            process=process,
            connection=parent_connection,
            lease=lease,
            write_spec=write_spec,
        )
        Thread(
            target=self._monitor_worker,
            args=(run_id,),
            name=f"analysis-monitor-{run_id}",
            daemon=True,
        ).start()

    def _monitor_worker(self, run_id: str) -> None:
        with self._lock:
            handle = self._handles.get(run_id)
        if handle is None:
            return
        outcome: tuple[str, str | None, str | None] | None = None
        try:
            while outcome is None:
                message = handle.connection.recv()
                if not isinstance(message, tuple) or not message:
                    outcome = (
                        "failure",
                        "worker_ipc_failure",
                        "The analysis worker returned an invalid response.",
                    )
                    continue
                if message[0] == "reserve" and len(message) == _RESERVE_MESSAGE_LENGTH:
                    approved = self._reserve_for_worker(run_id, message[1])
                    handle.connection.send(("reservation", approved))
                elif message[0] == "published":
                    outcome = ("published", None, None)
                elif message[0] == "failure" and len(message) == _FAILURE_MESSAGE_LENGTH:
                    outcome = ("failure", str(message[1]), str(message[2]))
                else:
                    outcome = (
                        "failure",
                        "worker_ipc_failure",
                        "The analysis worker returned an invalid response.",
                    )
        except EOFError, OSError:
            outcome = None
        finally:
            handle.process.join()
            handle.connection.close()

        if outcome is None:
            if handle.process.exitcode == -signal.SIGKILL:
                outcome = (
                    "failure",
                    "worker_out_of_memory",
                    "The analysis worker was killed, possibly after exhausting memory.",
                )
            else:
                outcome = (
                    "failure",
                    "worker_ipc_failure",
                    "The analysis worker exited without a complete result.",
                )
        self._publish_worker_outcome(run_id, outcome)

    def _reserve_for_worker(self, run_id: str, raw_size: object) -> bool:
        if not isinstance(raw_size, int) or isinstance(raw_size, bool) or raw_size <= 0:
            return False
        with self._lock:
            run = self._runs.get(run_id)
            if run is None or run.status is not RunStatus.RUNNING:
                return False
            try:
                return self._results.reserve(run_id, raw_size)
            except ValueError:
                return False

    def _publish_worker_outcome(
        self,
        run_id: str,
        outcome: tuple[str, str | None, str | None],
    ) -> None:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                self._results.abort(run_id)
                return
            if run.status is RunStatus.CANCELLING:
                self._results.abort(run_id)
                return
            if run.status is not RunStatus.RUNNING:
                return
            if outcome[0] == "published":
                try:
                    self._results.commit(run_id)
                except KeyError, OSError, ValueError:
                    self._results.abort(run_id)
                    self._finish_locked(
                        run_id,
                        RunStatus.FAILED,
                        RunError(
                            "result_storage_failure",
                            "The completed result could not be published.",
                        ),
                    )
                    return
                self._finish_locked(run_id, RunStatus.SUCCEEDED, None)
                return
            self._results.abort(run_id)
            self._finish_locked(
                run_id,
                RunStatus.FAILED,
                RunError(
                    outcome[1] or "worker_failed",
                    outcome[2] or "The analysis worker failed.",
                ),
            )

    def _cancel_run(self, run_id: str) -> AnalysisRun | None:
        with self._lock:
            cancellation_lock = self._cancel_locks.get(run_id)
        if cancellation_lock is None:
            return None
        with cancellation_lock:
            with self._lock:
                run = self._runs.get(run_id)
                if run is None:
                    return None
                if run.status.is_terminal:
                    return self._refresh_terminal_locked(run, datetime.now(UTC))
                if run.status is RunStatus.QUEUED:
                    with suppress(ValueError):
                        self._waiting.remove(run_id)
                    self._results.abort(run_id)
                    self._finish_locked(run_id, RunStatus.CANCELLED, None)
                    return self._runs[run_id]
                run = replace(run, status=RunStatus.CANCELLING)
                self._runs[run_id] = run
                handle = self._handles.get(run_id)

            if handle is not None:
                terminate_process(handle.process)

            with self._lock:
                current = self._runs.get(run_id)
                if current is None:
                    return None
                if current.status is RunStatus.CANCELLING:
                    self._results.abort(run_id)
                    self._finish_locked(run_id, RunStatus.CANCELLED, None)
                return self._runs[run_id]

    def _finish_locked(
        self,
        run_id: str,
        status: RunStatus,
        error: RunError | None,
        *,
        promote: bool = True,
    ) -> None:
        if not status.is_terminal:
            raise ValueError("run completion requires a terminal state")
        current = self._runs[run_id]
        if current.status.is_terminal:
            return
        handle = self._handles.pop(run_id, None)
        if handle is not None:
            handle.lease.release()
        now = datetime.now(UTC)
        terminal = replace(
            current,
            status=status,
            finished_at=now,
            heartbeat_expires_at=None,
            result_expires_at=now + self._settings.result_ttl,
            error=error,
        )
        self._runs[run_id] = terminal
        self._executions.pop(run_id, None)
        if self._active_by_owner.get(current.owner_context) == run_id:
            self._active_by_owner.pop(current.owner_context, None)
        if self._terminal_callback is not None:
            self._terminal_callback(terminal)
        if promote and self._accepting:
            self._promote_locked()

    def _promote_locked(self) -> None:
        while self._waiting:
            lease = self._compute_slots.try_acquire()
            if lease is None:
                return
            run_id = self._waiting.popleft()
            run = self._runs.get(run_id)
            if run is None or run.status is not RunStatus.QUEUED:
                lease.release()
                continue
            self._launch_locked(run_id, lease)

    def _refresh_terminal_locked(self, run: AnalysisRun, now: datetime) -> AnalysisRun:
        refreshed = replace(run, result_expires_at=now + self._settings.result_ttl)
        self._runs[run.run_id] = refreshed
        return refreshed

    def _expire_terminal_locked(self, now: datetime, *, background: bool = False) -> None:
        expired = [
            run_id
            for run_id, run in self._runs.items()
            if run.status.is_terminal
            and (
                (background and run_id in self._pending_result_cleanup)
                or (run.result_expires_at is not None and run.result_expires_at <= now)
            )
        ]
        for run_id in expired:
            if background:
                self._remove_terminal_in_background_locked(run_id)
            else:
                self._remove_terminal_locked(run_id)

    def _remove_terminal_in_background_locked(self, run_id: str) -> None:
        try:
            self._remove_terminal_locked(run_id)
        except Exception as exc:
            _log_cleanup_failure("result_cleanup", exc)

    def _remove_terminal_locked(self, run_id: str) -> None:
        run = self._runs.get(run_id)
        if run is None or not run.status.is_terminal:
            return
        # The result may be invalidated before its own TTL. Remember that work
        # before I/O so a failed deletion remains eligible on the next tick.
        # Explicit cleanup callers still receive the original exception.
        self._pending_result_cleanup.add(run_id)
        self._results.delete(run_id)
        self._runs.pop(run_id, None)
        self._cancel_locks.pop(run_id, None)
        scope = self._idempotency_by_run.pop(run_id, None)
        if scope is not None:
            self._idempotency.pop(scope, None)
        self._pending_result_cleanup.discard(run_id)

    def reap_expired(self) -> None:
        """Reap each expired resource independently, retrying failures on later ticks."""
        now = datetime.now(UTC)
        with self._lock:
            self._expire_terminal_locked(now, background=True)
            abandoned = [
                run_id
                for run_id, run in self._runs.items()
                if not run.status.is_terminal
                and run.heartbeat_expires_at is not None
                and run.heartbeat_expires_at <= now
            ]
        for run_id in abandoned:
            try:
                self._cancel_run(run_id)
            except Exception as exc:
                _log_cleanup_failure("heartbeat", exc)
        if self._session_expiry_reaper is not None:
            try:
                self._session_expiry_reaper()
            except Exception as exc:
                _log_cleanup_failure("session_expiry", exc)

    def _reaper_loop(self, interval: float) -> None:
        while not self._reaper_stop.wait(interval):
            try:
                self.reap_expired()
            except Exception as exc:
                # Defensive boundary for failures outside the per-resource and
                # per-phase guards. Never let the only periodic driver exit.
                _log_cleanup_failure("reaper_iteration", exc)

    def shutdown(self) -> None:
        """Reject starts, cancel every active run, and reap all child processes."""
        with self._lock:
            if not self._accepting and self._reaper_thread is None:
                return
            self._accepting = False
            active_run_ids = [
                run_id for run_id, run in self._runs.items() if not run.status.is_terminal
            ]
            immediate_processes = tuple(self._immediate_processes.items())
            reaper = self._reaper_thread
            self._reaper_thread = None
            self._reaper_stop.set()
        for run_id in active_run_ids:
            self._cancel_run(run_id)
        for _lease, handle in immediate_processes:
            with handle.termination_lock:
                terminate_process(handle.process)
        with self._lock:
            # Shutdown also releases leases acquired immediately before it won
            # the start race; those entries have no process handle.
            for lease in tuple(self._immediate_leases):
                current_handle = self._immediate_processes.get(lease)
                if current_handle is not None and current_handle.process.is_alive():
                    continue
                self._immediate_processes.pop(lease, None)
                self._immediate_leases.remove(lease)
                lease.release()
        if reaper is not None and reaper is not current_thread():
            reaper.join(timeout=2)

    def run_count(self) -> int:
        """Return retained run resources for focused capacity tests."""
        with self._lock:
            return len(self._runs)

    def active_process_count(self) -> int:
        """Return child processes that have not yet been reaped."""
        with self._lock:
            job_processes = sum(1 for handle in self._handles.values() if handle.process.is_alive())
            immediate_processes = sum(
                1 for handle in self._immediate_processes.values() if handle.process.is_alive()
            )
            return job_processes + immediate_processes
