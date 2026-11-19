"""Validate application release metadata before registry credentials are available."""

import argparse
import json
import re
import tomllib
from importlib.metadata import version
from pathlib import Path

import yaml

from main import app

_NUMBER = r"(?:0|[1-9][0-9]*)"
_TAG = re.compile(rf"v({_NUMBER}\.{_NUMBER}\.{_NUMBER})(?:-(alpha|beta|rc)\.({_NUMBER}))?")
_PYTHON_PRERELEASE = {"alpha": "a", "beta": "b", "rc": "rc"}
_MAX_TAG_LENGTH = 128


def validate_release(
    tag: str,
    backend: str,
    frontend: str,
    installed: str,
    openapi: str,
    chart_app: str,
) -> str:
    """Require a Docker-safe version tag agreeing with all application metadata."""
    match = _TAG.fullmatch(tag)
    if match is None or len(tag.removeprefix("v")) > _MAX_TAG_LENGTH:
        raise ValueError("Expected vMAJOR.MINOR.PATCH or vMAJOR.MINOR.PATCH-(alpha|beta|rc).N")
    base, phase, number = match.groups()
    expected_python = base if phase is None else f"{base}{_PYTHON_PRERELEASE[phase]}{number}"
    release = tag.removeprefix("v")
    if (
        frontend != release
        or chart_app != release
        or any(value != expected_python for value in (backend, installed, openapi))
    ):
        raise ValueError(
            "Release tag must agree with backend, frontend, installed, OpenAPI, "
            "and chart appVersion"
        )
    return release


def main() -> None:
    """Print validated version outputs for the release workflow."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag")
    arguments = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    backend = tomllib.loads((root / "backend/pyproject.toml").read_text())["project"]["version"]
    frontend = json.loads((root / "frontend/package.json").read_text())["version"]
    chart = yaml.safe_load((root / "charts/rabbitmq-topology-analyzer/Chart.yaml").read_text())
    release = validate_release(
        arguments.tag,
        backend,
        frontend,
        version("rabbitmq-topology-analyzer-backend"),
        app.openapi()["info"]["version"],
        chart["appVersion"],
    )
    print(f"version={release}")
    print(f"package_version={backend}")
    print(f"stable={str('-' not in release).lower()}")


if __name__ == "__main__":
    main()
