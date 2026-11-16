"""Fail-fast validation for explicitly selected production deployments."""

import os
from enum import StrEnum
from pathlib import Path


class DeploymentMode(StrEnum):
    """Runtime profile used only to strengthen startup validation."""

    DEVELOPMENT = "development"
    PRODUCTION = "production"


def load_deployment_mode() -> DeploymentMode:
    """Return the explicit deployment mode, defaulting only local work to development."""
    raw_mode = os.environ.get("RABBITMQ_DEPLOYMENT_MODE", DeploymentMode.DEVELOPMENT.value)
    try:
        return DeploymentMode(raw_mode)
    except ValueError as exc:
        choices = ", ".join(mode.value for mode in DeploymentMode)
        raise ValueError(f"RABBITMQ_DEPLOYMENT_MODE must be one of: {choices}") from exc


def _required_positive_integer(name: str) -> int:
    raw_value = os.environ.get(name)
    if raw_value is None:
        raise ValueError(f"{name} must be explicitly configured in production")
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def validate_production_environment() -> DeploymentMode:
    """Reject a production process that could silently use unsafe local defaults."""
    mode = load_deployment_mode()
    if mode is DeploymentMode.DEVELOPMENT:
        return mode

    _required_positive_integer("RABBITMQ_ANALYSIS_WORKERS")
    _required_positive_integer("RABBITMQ_ANALYSIS_RESULTS_MAX_BYTES")
    raw_results_dir = os.environ.get("RABBITMQ_ANALYSIS_RESULTS_DIR")
    if raw_results_dir is None:
        raise ValueError(
            "RABBITMQ_ANALYSIS_RESULTS_DIR must be explicitly configured in production"
        )
    results_dir = Path(raw_results_dir)
    if not results_dir.is_absolute():
        raise ValueError("RABBITMQ_ANALYSIS_RESULTS_DIR must be absolute in production")
    return mode
