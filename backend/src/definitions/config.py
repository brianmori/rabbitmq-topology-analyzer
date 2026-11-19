"""Validated configuration for bounded capture uploads."""

import os
from dataclasses import dataclass
from threading import RLock

_DEFAULT_CAPTURE_MAX_UPLOAD_BYTES = 52_428_800


@dataclass(frozen=True, slots=True)
class CaptureUploadSettings:
    """Effective aggregate upload bound for capture JSON files."""

    max_upload_bytes: int


@dataclass(slots=True)
class _CaptureUploadState:
    settings: CaptureUploadSettings


_settings_lock = RLock()
_state = _CaptureUploadState(
    settings=CaptureUploadSettings(max_upload_bytes=_DEFAULT_CAPTURE_MAX_UPLOAD_BYTES),
)


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


def configure_capture_uploads() -> CaptureUploadSettings:
    """Load the aggregate capture limit and fail fast on invalid values."""
    configured = CaptureUploadSettings(
        max_upload_bytes=_positive_environment_integer(
            "RABBITMQ_CAPTURE_MAX_UPLOAD_BYTES",
            _DEFAULT_CAPTURE_MAX_UPLOAD_BYTES,
        ),
    )
    with _settings_lock:
        _state.settings = configured
    return configured


def get_capture_upload_settings() -> CaptureUploadSettings:
    """Return the process-lifespan upload settings."""
    with _settings_lock:
        return _state.settings
