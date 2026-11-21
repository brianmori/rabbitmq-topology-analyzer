import json
import tomllib
from importlib.metadata import version
from pathlib import Path

import pytest
import yaml
from tools.release import prepare_release, validate_release

from main import app

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CHART_METADATA = "charts/rabbitmq-topology-analyzer/Chart.yaml"
RELEASE_FILES = (
    "backend/pyproject.toml",
    "backend/uv.lock",
    "frontend/package.json",
    CHART_METADATA,
    "Dockerfile",
)


@pytest.fixture
def release_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uv-cache"))
    for name in RELEASE_FILES:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((REPOSITORY_ROOT / name).read_bytes())
    return tmp_path


def test_application_package_versions_match_the_release_candidate() -> None:
    backend = tomllib.loads((REPOSITORY_ROOT / "backend/pyproject.toml").read_text())
    frontend = json.loads((REPOSITORY_ROOT / "frontend/package.json").read_text())
    chart = yaml.safe_load((REPOSITORY_ROOT / CHART_METADATA).read_text())

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
    with pytest.raises(ValueError, match=r"is '1\.0\.1', expected '1\.0\.0'"):
        validate_release("v1.0.0", *metadata)


@pytest.mark.parametrize(("release", "package"), [("2.3.4-beta.1", "2.3.4b1"), ("2.3.4", "2.3.4")])
def test_prepare_release_updates_only_application_versions(
    release_checkout: Path, release: str, package: str
) -> None:
    lock = release_checkout / "backend/uv.lock"
    expected_lock = tomllib.loads(lock.read_text())
    [project] = [
        entry for entry in expected_lock["package"] if entry["source"] == {"editable": "."}
    ]
    project["version"] = package
    chart = release_checkout / CHART_METADATA
    chart_version = yaml.safe_load(chart.read_text())["version"]

    prepare_release(release_checkout, f"v{release}")

    assert tomllib.loads(lock.read_text()) == expected_lock
    backend = tomllib.loads((release_checkout / "backend/pyproject.toml").read_text())
    assert backend["project"]["version"] == package
    frontend = json.loads((release_checkout / "frontend/package.json").read_text())
    assert frontend["version"] == release
    chart_data = yaml.safe_load(chart.read_text())
    assert chart_data["appVersion"] == release
    assert chart_data["version"] == chart_version
    assert f"ARG VERSION={package}" in (release_checkout / "Dockerfile").read_text().splitlines()

    prepared = {name: (release_checkout / name).read_bytes() for name in RELEASE_FILES}
    prepare_release(release_checkout, f"v{release}")
    assert {name: (release_checkout / name).read_bytes() for name in RELEASE_FILES} == prepared


def test_prepare_release_leaves_files_intact_when_a_version_field_is_missing(
    release_checkout: Path,
) -> None:
    (release_checkout / "Dockerfile").write_text("FROM scratch\n")
    original = {name: (release_checkout / name).read_bytes() for name in RELEASE_FILES}

    with pytest.raises(ValueError, match="Expected one application version field in Dockerfile"):
        prepare_release(release_checkout, "v2.3.4-beta.1")

    assert {name: (release_checkout / name).read_bytes() for name in RELEASE_FILES} == original
