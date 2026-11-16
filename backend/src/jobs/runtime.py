"""Application-lifespan ownership of the shared scheduler and result store."""

import logging
from dataclasses import dataclass
from threading import RLock
from typing import TYPE_CHECKING

from core.state import (
    reap_expired_session_analyses,
    refresh_analysis_run_access,
    set_session_expiry_handler,
    unpin_analysis_run,
)
from jobs.config import JobSettings, load_job_settings
from jobs.results import ResultRepository
from jobs.scheduler import ComputeSlotPool, JobScheduler

if TYPE_CHECKING:
    from jobs.models import AnalysisRun


@dataclass(slots=True)
class _RuntimeState:
    scheduler: JobScheduler | None = None


_runtime_lock = RLock()
_runtime = _RuntimeState()
_LOGGER = logging.getLogger(__name__)


def _terminal_run(run: AnalysisRun) -> None:
    unpin_analysis_run(
        run.owner_context,
        run.source,
        run.analysis_id,
        run.snapshot_revision,
    )


def _refresh_run_access(run: AnalysisRun) -> bool:
    return refresh_analysis_run_access(
        run.owner_context,
        run.source,
        run.analysis_id,
        run.snapshot_revision,
    )


def configure_job_runtime(settings: JobSettings | None = None) -> JobScheduler:
    """Create and start one process-local scheduler for this app lifespan."""
    with _runtime_lock:
        if _runtime.scheduler is not None:
            set_session_expiry_handler(None)
            _runtime.scheduler.shutdown()
        effective_settings = settings or load_job_settings()
        repository = ResultRepository(
            effective_settings.results_dir,
            effective_settings.results_max_bytes,
        )
        # No run metadata survives startup, so every pre-existing result is an
        # orphan even if its atomic file was complete.
        repository.reconcile()
        _LOGGER.info(
            "Analysis results use process-local storage with a %d-byte quota.",
            effective_settings.results_max_bytes,
        )
        scheduler = JobScheduler(
            effective_settings,
            repository,
            compute_slots=ComputeSlotPool(effective_settings.worker_count),
            terminal_callback=_terminal_run,
            analysis_access_callback=_refresh_run_access,
            session_expiry_reaper=reap_expired_session_analyses,
        )
        set_session_expiry_handler(scheduler.invalidate_expired_analysis)
        scheduler.start()
        _runtime.scheduler = scheduler
        return scheduler


def get_job_scheduler() -> JobScheduler:
    """Return the lifespan-configured scheduler."""
    with _runtime_lock:
        if _runtime.scheduler is None:
            raise RuntimeError("analysis job runtime is not configured")
        return _runtime.scheduler


def shutdown_job_runtime() -> None:
    """Gracefully stop admission and reap every compute child."""
    with _runtime_lock:
        scheduler = _runtime.scheduler
        _runtime.scheduler = None
        set_session_expiry_handler(None)
    if scheduler is not None:
        scheduler.shutdown()
