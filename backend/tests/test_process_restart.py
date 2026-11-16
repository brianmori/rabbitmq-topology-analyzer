import json
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import httpx2
import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping


_STARTUP_TIMEOUT_SECONDS = 15
_SHUTDOWN_TIMEOUT_SECONDS = 10
_RUN_TIMEOUT_SECONDS = 10
_TERMINAL_RUN_STATUSES = {"succeeded", "failed", "cancelled"}


def _cyclic_definitions() -> bytes:
    return json.dumps(
        {
            "users": [],
            "vhosts": [{"name": "/"}],
            "permissions": [],
            "topic_permissions": [],
            "global_parameters": [
                {"name": "internal_cluster_id", "value": "cluster-restart-smoke"},
                {"name": "cluster_name", "value": "Restart smoke broker"},
            ],
            "parameters": [],
            "policies": [],
            "queues": [
                {
                    "name": "loop-q",
                    "vhost": "/",
                    "durable": True,
                    "auto_delete": False,
                    "arguments": {
                        "x-queue-type": "classic",
                        "x-dead-letter-exchange": "loop-x",
                    },
                },
            ],
            "exchanges": [
                {
                    "name": "loop-x",
                    "vhost": "/",
                    "type": "direct",
                    "durable": True,
                    "auto_delete": False,
                    "internal": False,
                    "arguments": {},
                },
            ],
            "bindings": [
                {
                    "source": "loop-x",
                    "vhost": "/",
                    "destination": "loop-q",
                    "destination_type": "queue",
                    "routing_key": "loop",
                    "arguments": {},
                },
            ],
        },
        separators=(",", ":"),
    ).encode()


def _process_environment(
    *,
    backend_root: Path,
    definitions_root: Path,
    results_root: Path,
) -> dict[str, str]:
    environment = dict(os.environ)
    existing_pythonpath = environment.get("PYTHONPATH")
    python_paths = [str(backend_root / "src"), str(backend_root)]
    if existing_pythonpath:
        python_paths.append(existing_pythonpath)
    environment.update(
        {
            "PYTHONPATH": os.pathsep.join(python_paths),
            "RABBITMQ_DEPLOYMENT_MODE": "production",
            "RABBITMQ_AUTH_MODE": "none",
            "RABBITMQ_FULL_EXPORT_DEFINITIONS_DIR": str(definitions_root),
            "RABBITMQ_ANALYSIS_WORKERS": "1",
            "RABBITMQ_ANALYSIS_QUEUE_CAPACITY": "1",
            "RABBITMQ_ANALYSIS_RESULTS_DIR": str(results_root),
            "RABBITMQ_ANALYSIS_RESULTS_MAX_BYTES": "1000000",
        },
    )
    environment.pop("RABBITMQ_FRONTEND_DIST_DIR", None)
    return environment


def _process_log(log_path: Path) -> str:
    try:
        return log_path.read_text(encoding="utf-8")
    except OSError:
        return "<process log unavailable>"


def _wait_until_ready(process: subprocess.Popen[bytes], base_url: str, log_path: Path) -> None:
    deadline = time.monotonic() + _STARTUP_TIMEOUT_SECONDS
    with httpx2.Client(base_url=base_url, timeout=0.5) as client:
        while time.monotonic() < deadline:
            return_code = process.poll()
            if return_code is not None:
                pytest.fail(
                    f"Uvicorn exited with status {return_code} before readiness:\n"
                    f"{_process_log(log_path)}",
                )
            try:
                response = client.get("/api/health/ready")
            except httpx2.HTTPError:
                time.sleep(0.05)
                continue
            if response.status_code == 200 and response.json() == {"status": "ready"}:
                return
            time.sleep(0.05)
    pytest.fail(f"Uvicorn did not become ready:\n{_process_log(log_path)}")


def _stop_process(process: subprocess.Popen[bytes], log_path: Path) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=_SHUTDOWN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=_SHUTDOWN_TIMEOUT_SECONDS)
        pytest.fail(f"Uvicorn did not stop gracefully:\n{_process_log(log_path)}")


@contextmanager
def _running_application(
    *,
    backend_root: Path,
    environment: Mapping[str, str],
    log_path: Path,
) -> Iterator[str]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    with log_path.open("wb") as process_log:
        process = subprocess.Popen(
            (
                sys.executable,
                "-m",
                "uvicorn",
                "main:app",
                "--fd",
                str(listener.fileno()),
                "--log-level",
                "info",
            ),
            cwd=backend_root,
            env=environment,
            pass_fds=(listener.fileno(),),
            stdout=process_log,
            stderr=subprocess.STDOUT,
        )
        listener.close()
        base_url = f"http://127.0.0.1:{port}"
        try:
            _wait_until_ready(process, base_url, log_path)
            yield base_url
        finally:
            _stop_process(process, log_path)


def _wait_for_scc_result(
    client: httpx2.Client,
    *,
    run_id: str,
    snapshot_revision: str,
    owner_token: str,
) -> dict[str, object]:
    deadline = time.monotonic() + _RUN_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        response = client.get(
            f"/api/scc-runs/{run_id}",
            params={"snapshot_revision": snapshot_revision},
            headers={"X-Session-Token": owner_token},
        )
        assert response.status_code == 200
        body = response.json()
        if body["status"] in _TERMINAL_RUN_STATUSES:
            return body
        time.sleep(0.05)
    pytest.fail("SCC run did not reach a terminal state")


def test_process_restart_clears_session_state_and_result_orphans(tmp_path: Path) -> None:
    backend_root = Path(__file__).resolve().parents[1]
    definitions_root = tmp_path / "definitions"
    results_root = tmp_path / "results"
    definitions_root.mkdir()
    results_root.mkdir()
    (definitions_root / "manifest.json").write_text(
        '{"schemaVersion":1,"analyses":[]}',
        encoding="utf-8",
    )
    (results_root / ".startup-orphan.tmp").write_bytes(b"partial")
    environment = _process_environment(
        backend_root=backend_root,
        definitions_root=definitions_root,
        results_root=results_root,
    )
    owner_token = str(uuid4())
    analysis_id = "restart-smoke"

    with _running_application(
        backend_root=backend_root,
        environment=environment,
        log_path=tmp_path / "first-uvicorn.log",
    ) as base_url:
        assert list(results_root.iterdir()) == []
        with httpx2.Client(base_url=base_url, timeout=10) as client:
            upload = client.post(
                "/api/definitions/upload",
                data={"analysis_id": analysis_id, "label": "Restart smoke"},
                files={
                    "definitions_file": (
                        "definitions.json",
                        _cyclic_definitions(),
                        "application/json",
                    ),
                },
                headers={"X-Session-Token": owner_token},
            )
            assert upload.status_code == 201
            snapshot_revision = upload.json()["analysis"]["snapshotRevision"]

            catalogue = client.get(
                "/api/analyses",
                headers={"X-Session-Token": owner_token},
            )
            assert catalogue.status_code == 200
            assert [entry["analysisId"] for entry in catalogue.json()["session"]] == [
                analysis_id,
            ]

            started = client.post(
                "/api/scc-runs",
                json={
                    "source": "session",
                    "analysisId": analysis_id,
                    "snapshotRevision": snapshot_revision,
                },
                headers={
                    "X-Session-Token": owner_token,
                    "Idempotency-Key": str(uuid4()),
                },
            )
            assert started.status_code == 202
            run_id = started.json()["runId"]
            terminal = _wait_for_scc_result(
                client,
                run_id=run_id,
                snapshot_revision=snapshot_revision,
                owner_token=owner_token,
            )
            assert terminal["status"] == "succeeded"
            assert terminal["result"] is not None
            committed_result = results_root / f"{run_id}.json"
            assert committed_result.is_file()

    assert committed_result.is_file()

    with _running_application(
        backend_root=backend_root,
        environment=environment,
        log_path=tmp_path / "second-uvicorn.log",
    ) as base_url:
        assert list(results_root.iterdir()) == []
        with httpx2.Client(base_url=base_url, timeout=5) as client:
            catalogue = client.get(
                "/api/analyses",
                headers={"X-Session-Token": owner_token},
            )
            assert catalogue.status_code == 200
            assert catalogue.json()["session"] == []

            topology = client.get(
                "/api/topology",
                params={"source": "session", "analysis_id": analysis_id},
                headers={"X-Session-Token": owner_token},
            )
            assert topology.status_code == 404

            run = client.get(
                f"/api/scc-runs/{run_id}",
                params={"snapshot_revision": snapshot_revision},
                headers={"X-Session-Token": owner_token},
            )
            assert run.status_code == 404
