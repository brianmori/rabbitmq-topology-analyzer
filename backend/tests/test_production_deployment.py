from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.deployment import DeploymentMode, validate_production_environment
from core.frontend import mount_configured_frontend

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def isolated_deployment_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in (
        "RABBITMQ_DEPLOYMENT_MODE",
        "RABBITMQ_AUTH_MODE",
        "RABBITMQ_ANALYSIS_WORKERS",
        "RABBITMQ_ANALYSIS_RESULTS_MAX_BYTES",
        "RABBITMQ_ANALYSIS_RESULTS_DIR",
        "RABBITMQ_FRONTEND_DIST_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    yield


def test_local_development_keeps_validated_defaults_optional() -> None:
    assert validate_production_environment() is DeploymentMode.DEVELOPMENT


@pytest.mark.parametrize(
    ("missing_name", "message"),
    [
        ("RABBITMQ_ANALYSIS_WORKERS", "must be explicitly configured"),
        ("RABBITMQ_ANALYSIS_RESULTS_MAX_BYTES", "must be explicitly configured"),
        ("RABBITMQ_ANALYSIS_RESULTS_DIR", "must be explicitly configured"),
    ],
)
def test_production_rejects_missing_required_capacity(
    monkeypatch: pytest.MonkeyPatch,
    missing_name: str,
    message: str,
) -> None:
    configured = {
        "RABBITMQ_DEPLOYMENT_MODE": "production",
        "RABBITMQ_AUTH_MODE": "none",
        "RABBITMQ_ANALYSIS_WORKERS": "2",
        "RABBITMQ_ANALYSIS_RESULTS_MAX_BYTES": "3221225472",
        "RABBITMQ_ANALYSIS_RESULTS_DIR": "/var/lib/rabbitmq-topology/results",
    }
    for name, value in configured.items():
        if name != missing_name:
            monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=message):
        validate_production_environment()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("RABBITMQ_ANALYSIS_WORKERS", "0"),
        ("RABBITMQ_ANALYSIS_WORKERS", "two"),
        ("RABBITMQ_ANALYSIS_RESULTS_MAX_BYTES", "-1"),
        ("RABBITMQ_ANALYSIS_RESULTS_DIR", "relative/results"),
    ],
)
def test_production_rejects_invalid_worker_quota_or_directory(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    monkeypatch.setenv("RABBITMQ_DEPLOYMENT_MODE", "production")
    monkeypatch.setenv("RABBITMQ_AUTH_MODE", "none")
    monkeypatch.setenv("RABBITMQ_ANALYSIS_WORKERS", "2")
    monkeypatch.setenv("RABBITMQ_ANALYSIS_RESULTS_MAX_BYTES", "3221225472")
    monkeypatch.setenv(
        "RABBITMQ_ANALYSIS_RESULTS_DIR",
        "/var/lib/rabbitmq-topology/results",
    )
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError):
        validate_production_environment()


def test_production_accepts_explicit_positive_capacity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RABBITMQ_DEPLOYMENT_MODE", "production")
    monkeypatch.setenv("RABBITMQ_AUTH_MODE", "none")
    monkeypatch.setenv("RABBITMQ_ANALYSIS_WORKERS", "2")
    monkeypatch.setenv("RABBITMQ_ANALYSIS_RESULTS_MAX_BYTES", "3221225472")
    monkeypatch.setenv(
        "RABBITMQ_ANALYSIS_RESULTS_DIR",
        "/var/lib/rabbitmq-topology/results",
    )

    assert validate_production_environment() is DeploymentMode.PRODUCTION


def test_configured_frontend_requires_and_serves_a_production_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frontend_root = tmp_path / "frontend"
    frontend_root.mkdir()
    (frontend_root / "index.html").write_text("<h1>Production workbench</h1>", encoding="utf-8")
    monkeypatch.setenv("RABBITMQ_FRONTEND_DIST_DIR", str(frontend_root))
    test_app = FastAPI()

    mount_configured_frontend(test_app)

    response = TestClient(test_app).get("/")
    assert response.status_code == 200
    assert "Production workbench" in response.text
