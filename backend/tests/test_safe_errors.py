import asyncio
import json
import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import httpx2
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from core.errors import SafeExceptionMiddleware, register_error_handlers
from core.security import AuthMode, Principal, get_principal
from main import app as production_app

if TYPE_CHECKING:
    from pytest import LogCaptureFixture


def _error_app() -> FastAPI:
    test_app = FastAPI()
    register_error_handlers(test_app)
    return test_app


def test_production_error_boundary_is_outermost_user_middleware() -> None:
    assert production_app.user_middleware[0].cls is SafeExceptionMiddleware


def test_http_errors_use_the_safe_envelope_and_preserve_retry_after() -> None:
    test_app = _error_app()

    @test_app.get("/busy")
    async def busy() -> None:
        raise HTTPException(
            status_code=503,
            detail="analysis_capacity_unavailable",
            headers={"Retry-After": "2"},
        )

    response = TestClient(test_app).get("/busy")

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "2"
    assert response.json() == {
        "error": {
            "code": "analysis_capacity_unavailable",
            "message": "Analysis capacity is temporarily unavailable.",
            "issues": [],
        },
    }


def test_validation_errors_never_echo_submitted_values() -> None:
    test_app = _error_app()

    @test_app.get("/validated")
    async def validated(limit: int) -> dict[str, int]:
        return {"limit": limit}

    secret = "amqp://admin:super-secret@rabbit.internal/%2F"
    response = TestClient(test_app).get("/validated", params={"limit": secret})
    rendered = response.text

    assert response.status_code == 422
    assert response.json()["error"] == {
        "code": "validation_failed",
        "message": "Request validation failed.",
        "issues": [
            {
                "field": "limit",
                "code": "int_parsing",
                "message": "The value is invalid.",
            },
        ],
    }
    assert secret not in rendered
    assert "rabbit.internal" not in rendered


_REVISION_QUERY = [("snapshot_revision", "sha256:" + "a" * 64)]
_RUN_PATH = "00000000-0000-4000-8000-000000000001"
_PRODUCT_QUERY_CASES = [
    ("GET", "/api/access", [], 200),
    ("GET", "/api/analyses", [], 200),
    ("DELETE", "/api/analyses/missing", [], 404),
    ("GET", "/api/topology", [("source", "static"), ("analysis_id", "missing")], 404),
    ("POST", "/api/scc-runs", [], 404),
    ("GET", f"/api/scc-runs/{_RUN_PATH}", _REVISION_QUERY, 404),
    ("DELETE", f"/api/scc-runs/{_RUN_PATH}", _REVISION_QUERY, 404),
    ("POST", "/api/cycle-runs", [], 404),
    ("GET", f"/api/cycle-runs/{_RUN_PATH}", _REVISION_QUERY, 404),
    ("DELETE", f"/api/cycle-runs/{_RUN_PATH}", _REVISION_QUERY, 404),
    (
        "GET",
        f"/api/cycle-runs/{_RUN_PATH}/cycles",
        [*_REVISION_QUERY, ("vhost", "/"), ("vhost", "/other")],
        404,
    ),
    ("GET", f"/api/cycle-runs/{_RUN_PATH}/cycles/0", _REVISION_QUERY, 404),
    ("GET", "/api/auth/session", [], 200),
    ("GET", "/api/health", [], 200),
    ("GET", "/api/health/live", [], 200),
    ("GET", "/api/health/ready", [], 200),
]


@pytest.mark.parametrize(("method", "path", "query", "valid_status"), _PRODUCT_QUERY_CASES)
@pytest.mark.parametrize(
    "unknown_name",
    ["unexpected", "snapshotRevision", "amqp://query-user:query-key-secret@query.invalid/"],
)
def test_product_queries_reject_unknown_names_with_safe_errors(
    method: str,
    path: str,
    query: list[tuple[str, str]],
    valid_status: int,
    unknown_name: str,
    caplog: LogCaptureFixture,
) -> None:
    headers = {"X-Session-Token": str(uuid4()), "Idempotency-Key": str(uuid4())}
    body = (
        {"source": "static", "analysisId": "missing", "snapshotRevision": _REVISION_QUERY[0][1]}
        if method == "POST"
        else None
    )
    # Missing resources are valid controls for run queries; SDK-backed success
    # journeys in the topology/SCC/cycle suites cover the same accepted inputs.
    caplog.set_level(logging.WARNING)
    with TestClient(production_app) as client:
        control = client.request(method, path, params=query, headers=headers, json=body)
        response = client.request(
            method,
            path,
            params=[*query, (unknown_name, "amqp://user:query-value-secret@query.invalid/")],
            headers=headers,
            json=body,
        )

    assert control.status_code == valid_status, control.text
    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "validation_failed",
            "message": "Request validation failed.",
            "issues": [{"field": "query", "code": "extra_forbidden", "message": "Unknown field."}],
        },
    }
    for marker in (unknown_name, "query-key-secret", "query-value-secret", "query.invalid"):
        assert marker not in response.text
        assert marker not in caplog.text


@pytest.mark.parametrize("path", ["/api/scc-runs", "/api/cycle-runs"])
def test_unknown_queries_preserve_authentication_role_and_owner_precedence(path: str) -> None:
    body = {"source": "static", "analysisId": "missing", "snapshotRevision": _REVISION_QUERY[0][1]}
    headers = {"Idempotency-Key": str(uuid4())}

    async def no_role() -> Principal:
        return Principal(principal_id="no-role", roles=frozenset(), auth_mode=AuthMode.NONE)

    async def unauthenticated() -> Principal:
        raise HTTPException(status_code=401)

    with TestClient(production_app) as client:
        try:
            missing_owner = client.post(
                path,
                params={"unexpected": "x"},
                json=body,
                headers=headers,
            )
            invalid_owner = client.post(
                path,
                params={"unexpected": "x"},
                json=body,
                headers={**headers, "X-Session-Token": "invalid"},
            )
            production_app.dependency_overrides[get_principal] = no_role
            forbidden = client.post(path, params={"unexpected": "x"}, json=body, headers=headers)
            production_app.dependency_overrides[get_principal] = unauthenticated
            unauthorized = client.post(path, params={"unexpected": "x"}, json=body, headers=headers)
        finally:
            production_app.dependency_overrides.clear()

    assert missing_owner.status_code == 400
    assert invalid_owner.status_code == 400
    assert forbidden.status_code == 403
    assert unauthorized.status_code == 401


def test_upload_rejects_query_fields_before_processing_the_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unexpected_processing(*_args: object, **_kwargs: object) -> None:
        pytest.fail("an upload with unknown query fields must not reach SDK processing")

    monkeypatch.setattr("definitions.router._process_capture", unexpected_processing)
    with TestClient(production_app) as client:
        response = client.post(
            "/api/definitions/upload",
            params={"unexpected": "private-value"},
            data={"analysis_id": "query-fixture"},
            files={"definitions_file": ("definitions.json", b"{}", "application/json")},
            headers={"X-Session-Token": str(uuid4())},
        )

    assert response.status_code == 422
    assert response.json()["error"]["issues"] == [
        {"field": "query", "code": "extra_forbidden", "message": "Unknown field."},
    ]


def test_unexpected_errors_redact_exception_text_and_traceback(
    caplog: LogCaptureFixture,
) -> None:
    test_app = _error_app()
    secret = "guest:super-secret@rabbit.internal"

    @test_app.get("/unexpected")
    async def unexpected() -> None:
        raise RuntimeError(secret)

    caplog.set_level(logging.ERROR, logger="core.errors")
    response = TestClient(test_app, raise_server_exceptions=False).get("/unexpected")
    rendered_logs = caplog.text

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "internal_error",
            "message": "The request could not be completed.",
            "issues": [],
        },
    }
    assert secret not in response.text
    assert secret not in rendered_logs
    assert "RuntimeError" in rendered_logs


def _probe_request(port: int, path: str) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
        connection.settimeout(5)
        connection.sendall(
            f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n".encode(),
        )
        response = bytearray()
        while chunk := connection.recv(4096):
            response.extend(chunk)
    return bytes(response)


def _wait_for_probe(process: subprocess.Popen[bytes], port: int, log_path: Path) -> None:
    deadline = time.monotonic() + 15
    with httpx2.Client(base_url=f"http://127.0.0.1:{port}", timeout=0.5) as client:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail(f"Uvicorn exited before readiness: {log_path.read_text()}")
            try:
                ready = client.get("/api/health/ready")
            except httpx2.HTTPError:
                time.sleep(0.05)
                continue
            if ready.status_code == 200:
                return
            time.sleep(0.05)
    pytest.fail(f"Uvicorn did not become ready: {log_path.read_text()}")


def test_real_uvicorn_never_logs_unexpected_exception_content(tmp_path: Path) -> None:
    backend_root = Path(__file__).resolve().parents[1]
    definitions_root = tmp_path / "definitions"
    definitions_root.mkdir()
    (definitions_root / "manifest.json").write_text('{"schemaVersion":1,"analyses":[]}')
    log_path = tmp_path / "uvicorn.log"
    environment = dict(os.environ)
    environment.update(
        {
            "PYTHONPATH": os.pathsep.join((str(backend_root / "src"), str(backend_root / "tests"))),
            "RABBITMQ_AUTH_MODE": "none",
            "RABBITMQ_DEPLOYMENT_MODE": "production",
            "RABBITMQ_FULL_EXPORT_DEFINITIONS_DIR": str(definitions_root),
            "RABBITMQ_ANALYSIS_WORKERS": "1",
            "RABBITMQ_ANALYSIS_RESULTS_DIR": str(tmp_path / "results"),
            "RABBITMQ_ANALYSIS_RESULTS_MAX_BYTES": "1000000",
        },
    )
    environment.pop("RABBITMQ_FRONTEND_DIST_DIR", None)

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    with log_path.open("wb") as process_log:
        process = subprocess.Popen(
            (
                sys.executable,
                "-m",
                "uvicorn",
                "safe_errors_probe:app",
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
        try:
            _wait_for_probe(process, port, log_path)
            before = _probe_request(port, "/api/_safe_error_probe/before")
            after = _probe_request(port, "/api/_safe_error_probe/after")
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)

    logs = log_path.read_text()
    assert before.count(b"HTTP/1.1 ") == 1
    assert before.startswith(b"HTTP/1.1 500 Internal Server Error")
    assert json.loads(before.split(b"\r\n\r\n", 1)[1]) == {
        "error": {
            "code": "internal_error",
            "message": "The request could not be completed.",
            "issues": [],
        },
    }
    assert after.count(b"HTTP/1.1 ") == 1
    assert after.startswith(b"HTTP/1.1 200 OK")
    assert b"first chunk" in after
    for marker in (
        "SYNTHETIC_CAUSE_SECRET_DO_NOT_LOG",
        "SYNTHETIC_EXCEPTION_SECRET_DO_NOT_LOG",
        "SYNTHETIC_STREAM_SECRET_DO_NOT_LOG",
    ):
        assert marker not in logs
        assert marker.encode() not in before
        assert marker.encode() not in after
    assert logs.count("Unhandled application exception type=RuntimeError") == 2


def test_unexpected_error_boundary_propagates_cancellation_after_cleanup() -> None:
    cleaned_up = False
    messages: list[object] = []

    async def cancelled(_scope: object, _receive: object, _send: object) -> None:
        nonlocal cleaned_up
        try:
            raise asyncio.CancelledError
        finally:
            cleaned_up = True

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: object) -> None:
        messages.append(message)

    scope = {"type": "http", "method": "GET", "path": "/probe"}
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(SafeExceptionMiddleware(cancelled)(scope, receive, send))
    assert cleaned_up
    assert messages == []
