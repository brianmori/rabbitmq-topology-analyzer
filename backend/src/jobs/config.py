"""Validated process-worker and completed-result configuration."""

import os
import tempfile
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

_DEFAULT_HEARTBEAT_TTL_SECONDS = 120
_DEFAULT_RESULT_TTL_SECONDS = 14_400
_MAX_RESULT_TTL_SECONDS = 14_400
_DEFAULT_RESULTS_MAX_BYTES = 1_073_741_824


@dataclass(frozen=True, slots=True)
class JobSettings:
    """Effective bounded-job and result-repository settings."""

    worker_count: int
    queue_capacity: int
    heartbeat_ttl: timedelta
    result_ttl: timedelta
    results_dir: Path
    results_max_bytes: int


def _environment_integer(name: str, default: int, *, allow_zero: bool = False) -> int:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{name} must be a {qualifier} integer") from exc
    if value < 0 or (value == 0 and not allow_zero):
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{name} must be a {qualifier} integer")
    return value


def load_job_settings() -> JobSettings:
    """Load all Milestone 2 settings and fail fast on invalid values."""
    default_workers = os.process_cpu_count() or 1
    worker_count = _environment_integer("RABBITMQ_ANALYSIS_WORKERS", default_workers)
    queue_capacity = _environment_integer(
        "RABBITMQ_ANALYSIS_QUEUE_CAPACITY",
        worker_count,
        allow_zero=True,
    )
    heartbeat_seconds = _environment_integer(
        "RABBITMQ_ANALYSIS_HEARTBEAT_TTL_SECONDS",
        _DEFAULT_HEARTBEAT_TTL_SECONDS,
    )
    result_seconds = _environment_integer(
        "RABBITMQ_ANALYSIS_RESULT_TTL_SECONDS",
        _DEFAULT_RESULT_TTL_SECONDS,
    )
    if result_seconds > _MAX_RESULT_TTL_SECONDS:
        raise ValueError(
            f"RABBITMQ_ANALYSIS_RESULT_TTL_SECONDS must be at most {_MAX_RESULT_TTL_SECONDS}",
        )
    results_max_bytes = _environment_integer(
        "RABBITMQ_ANALYSIS_RESULTS_MAX_BYTES",
        _DEFAULT_RESULTS_MAX_BYTES,
    )
    configured_dir = os.environ.get("RABBITMQ_ANALYSIS_RESULTS_DIR")
    results_dir = (
        Path(configured_dir).expanduser().resolve()
        if configured_dir
        else Path(tempfile.gettempdir()) / "rabbitmq-topology-analyzer" / "results"
    )
    return JobSettings(
        worker_count=worker_count,
        queue_capacity=queue_capacity,
        heartbeat_ttl=timedelta(seconds=heartbeat_seconds),
        result_ttl=timedelta(seconds=result_seconds),
        results_dir=results_dir,
        results_max_bytes=results_max_bytes,
    )
