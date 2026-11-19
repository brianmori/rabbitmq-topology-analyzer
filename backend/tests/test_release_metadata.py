import json
import tomllib
from importlib.metadata import version
from pathlib import Path

import pytest
import yaml

from tools.release import validate_release

from main import app

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_application_package_versions_match_the_release_candidate() -> None:
    backend = tomllib.loads((REPOSITORY_ROOT / "backend/pyproject.toml").read_text())
    frontend = json.loads((REPOSITORY_ROOT / "frontend/package.json").read_text())
    chart = yaml.safe_load(
        (REPOSITORY_ROOT / "charts/rabbitmq-topology-analyzer/Chart.yaml").read_text()
    )

    assert (
        validate_release(
            f"v{frontend['version']}",
            backend["project"]["version"],
            frontend["version"],
            version("rabbitmq-topology-analyzer-backend"),
            app.openapi()["info"]["version"],
            chart["appVersion"],
        )
        == frontend["version"]
    )
    assert "rabbitmq-management-sdk==0.5.0" in backend["project"]["dependencies"]


def test_openapi_uses_installed_application_metadata_not_fastapi_default() -> None:
    assert app.openapi()["info"]["title"] == "RabbitMQ Topology Analyzer"
    assert app.version == version("rabbitmq-topology-analyzer-backend")
    assert app.openapi()["info"]["version"] == app.version


@pytest.mark.parametrize(
    "suffix, python_suffix", [("", ""), ("-alpha.0", "a0"), ("-beta.2", "b2"), ("-rc.1", "rc1")]
)
def test_release_tags_match_python_and_frontend_version_spellings(
    suffix: str, python_suffix: str
) -> None:
    python_version = f"2.3.4{python_suffix}"
    release = f"2.3.4{suffix}"
    assert (
        validate_release(
            f"v{release}", python_version, release, python_version, python_version, release
        )
        == release
    )


@pytest.mark.parametrize(
    "tag",
    [
        "1.0.0",
        "v1.0",
        "v01.0.0",
        "v1.0.0-rc.01",
        "v1.0.0-preview.1",
        "v1.0.0+build.1",
        "v1.0.0\n",
        "v1.0.0;echo injected",
    ],
)
def test_release_rejects_malformed_or_unsupported_tags(tag: str) -> None:
    with pytest.raises(ValueError, match="Expected vMAJOR"):
        validate_release(tag, "1.0.0", "1.0.0", "1.0.0", "1.0.0", "1.0.0")


@pytest.mark.parametrize("index", range(5))
def test_release_rejects_each_metadata_mismatch(index: int) -> None:
    metadata = ["1.0.0"] * 5
    metadata[index] = "1.0.1"
    with pytest.raises(ValueError, match="must agree"):
        validate_release("v1.0.0", *metadata)
