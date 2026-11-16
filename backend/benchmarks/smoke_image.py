"""Smoke-test an installed image through its unmodified default command.

Run with just image-smoke. Only this driver, locked dependency metadata, and
synthetic capture fixtures are mounted. Full auth/lifecycle matrices stay in
the existing backend and browser suites.
"""

import argparse
import errno
import json
import os
import platform
import re
import shutil
import time
import tomllib
from importlib.metadata import distributions, version
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import httpx2
import rabbitmq_management_sdk

_VERIFICATION = Path("/verification")
_RESULTS = Path("/tmp/results")
_TIMEOUT_SECONDS = 15


def _verify_installation() -> None:
    project = tomllib.loads((_VERIFICATION / "pyproject.toml").read_text())
    lock = tomllib.loads((_VERIFICATION / "uv.lock").read_text())
    assert "rabbitmq-management-sdk==0.5.0" in project["project"]["dependencies"]
    assert version("rabbitmq-management-sdk") == "0.5.0"
    assert "site-packages" in str(rabbitmq_management_sdk.__file__)
    locked = {package["name"]: package["version"] for package in lock["package"]}
    locked[project["project"]["name"]] = project["project"]["version"]
    for distribution in distributions():
        name = re.sub(r"[-_.]+", "-", distribution.metadata["Name"]).lower()
        assert distribution.version == locked.get(name), f"Unexpected installed dependency: {name}"
        direct_url = distribution.read_text("direct_url.json")
        if direct_url:
            assert not json.loads(direct_url).get("dir_info", {}).get("editable", False)
    for binary in ("uv", "hatchling", "pytest", "node", "pnpm"):
        assert shutil.which(binary) is None
    assert (os.getuid(), os.getgid()) == (10001, 10001)
    for root in (Path("/app"), _VERIFICATION / "definitions"):
        try:
            (root / ".image-smoke-write").write_bytes(b"read-only probe")
        except OSError as error:
            assert error.errno == errno.EROFS, error
        else:
            raise AssertionError(f"Expected a read-only filesystem: {root}")
    command = Path("/proc/1/cmdline").read_bytes().split(b"\0")
    assert command[command.index(b"--workers") + 1] == b"1"
    with TemporaryDirectory() as temporary:
        probe = Path(temporary) / "writable"
        probe.write_bytes(b"temporary storage is required")
        assert probe.stat().st_uid == os.getuid()


def _wait_until_ready(client: httpx2.Client) -> None:
    deadline = time.monotonic() + _TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            response = client.get("/api/health/ready")
            if response.status_code == httpx2.codes.OK:
                assert response.json() == {"status": "ready"}
                return
        except httpx2.HTTPError:
            pass
        time.sleep(0.05)
    raise TimeoutError("The installed application did not become ready.")


def _completed_run(client: httpx2.Client, route: str, revision: str) -> httpx2.Response:
    deadline = time.monotonic() + _TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        response = client.get(route, params={"snapshot_revision": revision})
        response.raise_for_status()
        if response.json()["status"] in {"succeeded", "failed", "cancelled"}:
            assert response.json()["status"] == "succeeded", response.text
            return response
        time.sleep(0.05)
    raise TimeoutError("The installed SDK worker did not complete.")


def _exercise_http(client: httpx2.Client, *, static: bool) -> None:
    assert client.get("/api/health/live").json() == {"status": "live"}
    frontend = client.get("/")
    frontend.raise_for_status()
    assert re.search(r'(?:src|href)="(/assets/[^"]+)"', frontend.text)
    # Verify lazy chunks and the ELK worker as well as the initial HTML assets.
    for asset in Path("/app/frontend").rglob("*"):
        if asset.is_file():
            response = client.get(f"/{asset.relative_to('/app/frontend').as_posix()}")
            response.raise_for_status()
            assert response.content == asset.read_bytes(), f"Incorrect UI asset: {asset}"
    assert client.get("/openapi.json").json()["info"]["version"] == version(
        "rabbitmq-topology-analyzer-backend"
    )
    assert client.get("/api/auth/session").json()["authMode"] == "none"
    assert client.get("/api/access").json()["capabilities"]["manageUploads"] is True
    catalogue = client.get("/api/analyses").json()
    assert catalogue["session"] == []
    assert [entry["analysisId"] for entry in catalogue["static"]] == (
        ["e2e-static-cycle"] if static else []
    )
    with (_VERIFICATION / "definitions" / "cycle.definitions.json").open("rb") as capture:
        uploaded = client.post(
            "/api/definitions/upload",
            data={"analysis_id": "image-smoke", "label": "Installed image smoke"},
            files={"definitions_file": ("capture.json", capture, "application/json")},
        )
    assert uploaded.status_code == httpx2.codes.CREATED, uploaded.text
    revision = uploaded.json()["analysis"]["snapshotRevision"]
    topology = client.get(
        "/api/topology", params={"source": "session", "analysis_id": "image-smoke"}
    )
    topology.raise_for_status()
    assert topology.json()["nodes"] and topology.json()["edges"]
    if static:
        captured = client.get(
            "/api/topology", params={"source": "static", "analysis_id": "e2e-static-cycle"}
        )
        captured.raise_for_status()
        assert topology.json()["nodes"] == captured.json()["nodes"]
        assert topology.json()["edges"] == captured.json()["edges"]
    assert not list(Path("/tmp").glob("rabbitmq-topology-*")), "Upload staging was not removed."

    routes: list[str] = []
    for operation in ("scc", "cycle"):
        started = client.post(
            f"/api/{operation}-runs",
            json={"source": "session", "analysisId": "image-smoke", "snapshotRevision": revision},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert started.status_code == httpx2.codes.ACCEPTED, started.text
        run_id = started.json()["runId"]
        route = f"/api/{operation}-runs/{run_id}"
        completed = _completed_run(client, route, revision)
        if operation == "scc":
            assert completed.json()["result"]["components"]
        else:
            page = client.get(f"{route}/cycles", params={"snapshot_revision": revision, "limit": 1})
            page.raise_for_status()
            assert page.json()["total"] > 0
        result = _RESULTS / f"{run_id}.json"
        assert result.is_file() and result.stat().st_size > 0
        assert result.stat().st_uid == os.getuid()
        assert (
            client.get(
                route,
                params={"snapshot_revision": revision},
                headers={"X-Session-Token": str(uuid4())},
            ).status_code
            == httpx2.codes.NOT_FOUND
        )
        routes.append(route)
    assert len(list(_RESULTS.iterdir())) == len(routes)
    assert client.delete("/api/analyses/image-smoke").status_code == httpx2.codes.NO_CONTENT
    assert client.get("/api/analyses").json()["session"] == []
    for route in routes:
        assert (
            client.get(route, params={"snapshot_revision": revision}).status_code
            == httpx2.codes.NOT_FOUND
        )
    assert not list(_RESULTS.iterdir()), "Deleted analysis left result files."


def main() -> None:
    """Verify packaging and a short SDK journey for upload-only or static startup."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", choices=("upload", "static"))
    arguments = parser.parse_args()
    _verify_installation()
    with httpx2.Client(
        base_url="http://127.0.0.1:8000",
        headers={"X-Session-Token": str(uuid4())},
        timeout=10,
        trust_env=False,
    ) as client:
        _wait_until_ready(client)
        _exercise_http(client, static=arguments.source == "static")
    print(
        json.dumps(
            {
                "imageSmoke": "passed",
                "source": arguments.source,
                "architecture": platform.machine(),
                "sdkVersion": version("rabbitmq-management-sdk"),
            }
        )
    )


if __name__ == "__main__":
    main()
