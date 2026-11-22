"""Prepare and validate application metadata for a release tag."""

import argparse
import json
import re
import shutil
import subprocess
import sys
import tomllib
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory

_NUMBER = r"(?:0|[1-9][0-9]*)"
_TAG = re.compile(rf"v({_NUMBER}\.{_NUMBER}\.{_NUMBER})(?:-(alpha|beta|rc)\.({_NUMBER}))?")
_PYTHON_PRERELEASE = {"alpha": "a", "beta": "b", "rc": "rc"}
_MAX_TAG_LENGTH = 128
_BACKEND_PACKAGE = "rabbitmq-topology-analyzer-backend"
_CHART = "charts/rabbitmq-topology-analyzer/Chart.yaml"


def _release_versions(tag: str) -> tuple[str, str]:
    match = _TAG.fullmatch(tag)
    release = tag.removeprefix("v")
    if match is None or len(release) > _MAX_TAG_LENGTH:
        raise ValueError("Expected vMAJOR.MINOR.PATCH or vMAJOR.MINOR.PATCH-(alpha|beta|rc).N")
    base, phase, number = match.groups()
    package = base if phase is None else f"{base}{_PYTHON_PRERELEASE[phase]}{number}"
    return release, package


def validate_release(
    tag: str,
    backend: str,
    frontend: str,
    installed: str,
    openapi: str,
    chart_app: str,
) -> str:
    """Require a Docker-safe version tag agreeing with all application metadata."""
    release, package = _release_versions(tag)
    metadata = (
        ("backend/pyproject.toml", backend, package),
        ("frontend/package.json", frontend, release),
        ("installed backend", installed, package),
        ("OpenAPI", openapi, package),
        ("chart appVersion", chart_app, release),
    )
    mismatches = [
        f"{name} is {actual!r}, expected {expected!r}"
        for name, actual, expected in metadata
        if actual != expected
    ]
    if mismatches:
        details = ". ".join(mismatches)
        raise ValueError(f"Release tag {tag} must agree with application metadata. {details}")
    return release


def prepare_release(root: Path, tag: str) -> None:
    """Set the application versions while retaining the locked dependencies."""
    release, package = _release_versions(tag)
    lock_entry = rf'\[\[package\]\]\nname = "{_BACKEND_PACKAGE}"\nversion = "'
    fields = (
        ("backend/pyproject.toml", r'^(version = ")[^"\n]*(?="$)', package),
        ("backend/uv.lock", rf'^({lock_entry})[^"\n]*(?="$)', package),
        ("frontend/package.json", r'^(  "version": ")[^"\n]*(?=",$)', release),
        (_CHART, r'^(appVersion: ")[^"\n]*(?="$)', release),
        ("Dockerfile", r"^(ARG VERSION=)\S*$", package),
    )
    with TemporaryDirectory(dir=root, prefix=".release-prepare-") as staging_dir:
        staging = Path(staging_dir)
        updates = []
        for filename, pattern, target in fields:
            path = root / filename
            original = path.read_text(encoding="utf-8")
            updated, matches = re.subn(pattern, rf"\g<1>{target}", original, flags=re.MULTILINE)
            if matches != 1:
                raise ValueError(f"Expected one application version field in {filename}")
            candidate = staging / filename
            candidate.parent.mkdir(parents=True, exist_ok=True)
            candidate.write_text(updated, encoding="utf-8")
            if updated != original:
                shutil.copymode(path, candidate)
                backup = candidate.with_suffix(candidate.suffix + ".original")
                shutil.copy2(path, backup)
                updates.append((path, candidate, backup))

        # Only the local project entry changes. Check that uv still accepts
        # the lock before replacing any source files.
        subprocess.run(
            ["uv", "lock", "--check", "--offline", "--python", sys.executable],
            cwd=staging / "backend",
            check=True,
            timeout=30,
        )
        applied = []
        try:
            for path, candidate, backup in updates:
                candidate.replace(path)
                applied.append((path, backup))
        except OSError, KeyboardInterrupt:
            for path, backup in reversed(applied):
                backup.replace(path)
            raise


def _check_release(root: Path, tag: str) -> None:
    # Preparing source metadata only needs the standard library.
    import yaml  # noqa: PLC0415

    from main import app  # noqa: PLC0415

    backend = tomllib.loads((root / "backend/pyproject.toml").read_text())["project"]["version"]
    frontend = json.loads((root / "frontend/package.json").read_text())["version"]
    chart = yaml.safe_load((root / _CHART).read_text())
    release = validate_release(
        tag,
        backend,
        frontend,
        version(_BACKEND_PACKAGE),
        app.openapi()["info"]["version"],
        chart["appVersion"],
    )
    print(f"version={release}")
    print(f"package_version={backend}")
    print(f"stable={str('-' not in release).lower()}")


def main() -> None:
    """Prepare local metadata or print validated outputs for the workflow."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true", help="Update local application versions")
    parser.add_argument("tag")
    arguments = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    try:
        if arguments.prepare:
            prepare_release(root, arguments.tag)
        else:
            _check_release(root, arguments.tag)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
