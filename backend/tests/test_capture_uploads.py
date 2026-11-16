import asyncio
import json
import time
from concurrent.futures import CancelledError as FutureCancelledError
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from multiprocessing import active_children
from pathlib import Path
from tempfile import SpooledTemporaryFile, TemporaryDirectory
from threading import Barrier
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from rabbitmq_management_sdk.topology import ClusterTopology
from starlette import formparsers

import definitions.router as definitions_router
from analyses.snapshots import (
    SnapshotMetadata,
    compute_snapshot_revision,
    compute_snapshot_revision_from_files,
    create_analysis_snapshot,
)
from capture_envelopes import (
    CAPTURE_PRIVATE_MARKER,
    INVALID_CAPTURE_ENVELOPES,
    VALID_CAPTURE_ENVELOPES,
)
from core import state
from core.session import OwnerContext
from core.state import (
    AnalysisActiveError,
    AnalysisSource,
    CaptureCompleteness,
    configure_analysis_store,
    publish_session_analysis,
    release_session_upload,
    replace_static_analyses,
    reserve_session_upload,
    resolve_analysis,
)
from definitions.config import configure_capture_uploads
from definitions.processing import (
    CaptureRequestDisconnectedError,
    ProcessedCapture,
    _CaptureProcessRequest,
    process_staged_capture,
)
from definitions.staging import StagedCapture
from jobs.config import JobSettings
from jobs.results import ResultRepository
from jobs.runtime import get_job_scheduler
from jobs.scheduler import JobScheduler
from main import app

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator
    from multiprocessing.connection import Connection

    from starlette.types import Message, Scope


@pytest.fixture(autouse=True)
def isolated_upload_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in (
        "RABBITMQ_CAPTURE_MAX_UPLOAD_BYTES",
        "RABBITMQ_SESSION_ANALYSIS_LIMIT",
        "RABBITMQ_SESSION_ANALYSIS_GLOBAL_LIMIT",
        "RABBITMQ_SESSION_TOPOLOGY_TTL_SECONDS",
        "RABBITMQ_ANALYSIS_WORKERS",
    ):
        monkeypatch.delenv(name, raising=False)
    state._session_store.clear()
    state._active_uploads.clear()
    state._active_deletions.clear()
    state.set_session_expiry_handler(None)
    replace_static_analyses({})
    configure_analysis_store()
    configure_capture_uploads()
    yield
    state._session_store.clear()
    state._active_uploads.clear()
    state._active_deletions.clear()
    state.set_session_expiry_handler(None)
    replace_static_analyses({})


def _snapshot_value(
    analysis_id: str,
    revision: str = "sha256:" + "a" * 64,
) -> state.AnalysisSnapshot:
    topology = ClusterTopology(
        exchanges=frozenset(),
        queues=frozenset(),
        shovels=frozenset(),
        edges=frozenset(),
        cluster_id="cluster-id",
        cluster_name="Existing cluster",
    )
    snapshot = create_analysis_snapshot(
        topology,
        SnapshotMetadata(
            analysis_id=analysis_id,
            source=AnalysisSource.SESSION,
            label="Existing capture",
            capture_completeness=CaptureCompleteness.DEFINITIONS_ONLY,
            snapshot_revision=revision,
            cluster_tags={},
            in_cluster_amqp_hosts=(),
            vhost_count=0,
        ),
    )
    return snapshot


def _snapshot(owner: OwnerContext, analysis_id: str) -> state.AnalysisSnapshot:
    snapshot = _snapshot_value(analysis_id)
    publish_session_analysis(owner, snapshot)
    return snapshot


def _record_staging_directories(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
) -> list[Path]:
    staged: list[Path] = []

    def temporary_directory(*, prefix: str) -> TemporaryDirectory[str]:
        directory = TemporaryDirectory(prefix=prefix, dir=root)
        staged.append(Path(directory.name))
        return directory

    monkeypatch.setattr(definitions_router, "TemporaryDirectory", temporary_directory)
    return staged


def _record_multipart_spools(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
) -> tuple[list[SpooledTemporaryFile[bytes]], list[int]]:
    spools: list[SpooledTemporaryFile[bytes]] = []
    writes: list[int] = []

    def temporary_file(*, max_size: int) -> SpooledTemporaryFile[bytes]:
        # Roll to real disk storage even with small regression payloads.
        spool = SpooledTemporaryFile(max_size=1, dir=root)  # noqa: SIM115 - parser owns closure
        original_write = spool.write

        def write(data: bytes) -> int:
            written = original_write(data)
            writes.append(written)
            return written

        monkeypatch.setattr(spool, "write", write)
        spools.append(spool)
        return spool

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", temporary_file)
    return spools, writes


def _streaming_multipart(boundary: str, file_bytes: bytes) -> bytes:
    return b"".join(
        (
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="analysis_id"\r\n\r\n',
            b"retained\r\n",
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="definitions_file"; '
            b'filename="definitions.json"\r\n',
            b"Content-Type: application/json\r\n\r\n",
            file_bytes,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ),
    )


async def _post_multipart_chunks(
    chunks: list[bytes],
    *,
    boundary: str,
    owner_token: str,
    content_length: str | None = None,
    interruption: str | None = None,
) -> tuple[int, dict[str, object], int]:
    messages: list[Message] = []
    reads = 0

    async def receive() -> Message:
        nonlocal reads
        if reads == len(chunks):
            if interruption is None:
                # The connection remains open while SDK processing completes.
                await asyncio.Event().wait()
            if interruption == "cancel":
                # Cancel the real request task while its next receive is waiting.
                task = asyncio.current_task()
                assert task is not None
                task.cancel()
                await asyncio.sleep(0)
            return {"type": "http.disconnect"}
        chunk = chunks[reads]
        reads += 1
        return {
            "type": "http.request",
            "body": chunk,
            "more_body": reads < len(chunks) or interruption is not None,
        }

    async def send(message: Message) -> None:
        messages.append(message)

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/definitions/upload",
        "raw_path": b"/api/definitions/upload",
        "query_string": b"",
        "headers": [
            (b"content-type", f"multipart/form-data; boundary={boundary}".encode()),
            (b"x-session-token", owner_token.encode()),
        ],
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 12345),
        "root_path": "",
    }
    if content_length is not None:
        scope["headers"].append((b"content-length", content_length.encode()))
    await app(scope, receive, send)
    start = next(message for message in messages if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )
    return start["status"], json.loads(body), reads


def _valid_definitions() -> bytes:
    return json.dumps(
        {
            "vhosts": [{"name": "/"}],
            "global_parameters": [
                {"name": "internal_cluster_id", "value": "uploaded-cluster"},
                {"name": "cluster_name", "value": "Uploaded cluster"},
            ],
        },
    ).encode()


def _job_settings(root: Path) -> JobSettings:
    return JobSettings(
        worker_count=1,
        queue_capacity=1,
        heartbeat_ttl=timedelta(seconds=10),
        result_ttl=timedelta(seconds=10),
        results_dir=root,
        results_max_bytes=10_000,
    )


def _blocking_capture_child(
    request: _CaptureProcessRequest,
    _connection: object,
) -> None:
    marker = request.definitions_path.parent / "child-started"
    marker.touch()
    while True:
        time.sleep(0.1)


def _unexpected_capture_child(
    _request: _CaptureProcessRequest,
    connection: Connection,
) -> None:
    connection.send(("unexpected",))
    connection.close()


def test_file_revision_hash_matches_existing_byte_contract(tmp_path: Path) -> None:
    definitions = b'{"vhosts":[]}'
    queues = b"[]"
    exchanges = b"[]"
    definitions_path = tmp_path / "definitions.json"
    queues_path = tmp_path / "queues.json"
    exchanges_path = tmp_path / "exchanges.json"
    definitions_path.write_bytes(definitions)
    queues_path.write_bytes(queues)
    exchanges_path.write_bytes(exchanges)

    assert compute_snapshot_revision_from_files(
        definitions_path=definitions_path,
        queues_path=queues_path,
        exchanges_path=exchanges_path,
        in_cluster_amqp_hosts=("rabbit-a", "rabbit-b"),
    ) == compute_snapshot_revision(
        definitions_bytes=definitions,
        queues_bytes=queues,
        exchanges_bytes=exchanges,
        in_cluster_amqp_hosts=("rabbit-a", "rabbit-b"),
    )


@pytest.mark.parametrize("content_length", [None, "1", "999999"])
def test_oversized_streaming_upload_stops_receipt_and_preserves_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content_length: str | None,
) -> None:
    monkeypatch.setenv("RABBITMQ_CAPTURE_MAX_UPLOAD_BYTES", "16")
    owner_token = str(uuid4())
    owner = OwnerContext(principal_id="anonymous", session_id=owner_token)
    previous = _snapshot(owner, "retained")
    staged = _record_staging_directories(monkeypatch, tmp_path)
    spools, writes = _record_multipart_spools(monkeypatch, tmp_path)
    boundary = "capture-boundary"
    multipart = _streaming_multipart(boundary, b"payload-marker")
    prefix, suffix = multipart.split(b"payload-marker")
    chunks = [prefix, *([b"x" * 1024] * 32), suffix]

    async def unexpected_parser(*_args: object, **_kwargs: object) -> ProcessedCapture:
        pytest.fail("An oversized receipt must not start an SDK child.")

    monkeypatch.setattr(definitions_router, "process_staged_capture", unexpected_parser)

    with TestClient(app):
        response_status, body, reads = asyncio.run(
            _post_multipart_chunks(
                chunks,
                boundary=boundary,
                owner_token=owner_token,
                content_length=content_length,
            ),
        )

    assert response_status == 413
    assert body == {
        "error": {
            "code": "capture_upload_too_large",
            "message": "The combined capture files exceed the configured limit.",
            "issues": [],
        },
    }
    assert reads == 2
    assert resolve_analysis(AnalysisSource.SESSION, "retained", owner) is previous
    assert not staged
    assert spools and all(spool.closed for spool in spools)
    assert sum(writes) <= 16
    assert not list(tmp_path.iterdir())
    assert not state._active_uploads
    retry = reserve_session_upload(owner, "retained")
    release_session_upload(retry)


def test_false_content_length_cannot_bypass_aggregate_upload_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RABBITMQ_CAPTURE_MAX_UPLOAD_BYTES", "8")
    owner_token = str(uuid4())
    owner = OwnerContext(principal_id="anonymous", session_id=owner_token)
    previous = _snapshot(owner, "retained")
    staged = _record_staging_directories(monkeypatch, tmp_path)

    with TestClient(app) as client:
        response = client.post(
            "/api/definitions/upload",
            data={"analysis_id": "retained"},
            files={
                "definitions_file": (
                    "definitions.json",
                    b"123456789",
                    "application/json",
                ),
            },
            headers={"X-Session-Token": owner_token, "Content-Length": "1"},
        )

    assert response.status_code == 413
    assert resolve_analysis(AnalysisSource.SESSION, "retained", owner) is previous
    assert not staged


def test_upload_limit_counts_the_aggregate_across_individually_bounded_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RABBITMQ_CAPTURE_MAX_UPLOAD_BYTES", "12")
    owner_token = str(uuid4())
    owner = OwnerContext(principal_id="anonymous", session_id=owner_token)
    previous = _snapshot(owner, "retained")
    staged = _record_staging_directories(monkeypatch, tmp_path)

    with TestClient(app) as client:
        response = client.post(
            "/api/definitions/upload",
            data={"analysis_id": "retained"},
            files={
                "definitions_file": ("definitions.json", b"123456", "application/json"),
                "queues_file": ("queues.json", b"123456", "application/json"),
                "exchanges_file": ("exchanges.json", b"1", "application/json"),
            },
            headers={"X-Session-Token": owner_token},
        )

    assert response.status_code == 413
    assert resolve_analysis(AnalysisSource.SESSION, "retained", owner) is previous
    assert not staged


@pytest.mark.parametrize("complete", [False, True], ids=["definitions-only", "three-files"])
@pytest.mark.parametrize("extra_byte", [False, True], ids=["at-limit", "limit-plus-one"])
def test_capture_byte_limit_excludes_multipart_framing_and_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    complete: bool,
    extra_byte: bool,
) -> None:
    definitions = _valid_definitions()
    limit = len(definitions) + (4 if complete else 0)
    monkeypatch.setenv("RABBITMQ_CAPTURE_MAX_UPLOAD_BYTES", str(limit))
    owner_token = str(uuid4())
    owner = OwnerContext(principal_id="anonymous", session_id=owner_token)
    previous = _snapshot(owner, "retained")
    spools, writes = _record_multipart_spools(monkeypatch, tmp_path)
    files = {
        "definitions_file": (
            "definitions.json",
            definitions + (b" " if extra_byte and not complete else b""),
            "application/json",
        ),
    }
    if complete:
        files.update(
            queues_file=("queues.json", b"[]", "application/json"),
            exchanges_file=(
                "exchanges.json",
                b"[] " if extra_byte else b"[]",
                "application/json",
            ),
        )

    with TestClient(app) as client:
        response = client.post(
            "/api/definitions/upload",
            data={"analysis_id": "retained", "label": "A label outside the file-byte budget"},
            files=files,
            headers={"X-Session-Token": owner_token},
        )

    assert response.status_code == (413 if extra_byte else 200)
    assert sum(writes) <= limit
    assert spools and all(spool.closed for spool in spools)
    assert not list(tmp_path.iterdir())
    assert not state._active_uploads
    current = resolve_analysis(AnalysisSource.SESSION, "retained", owner)
    if extra_byte:
        assert current is previous
    else:
        assert current is not None and current is not previous


@pytest.mark.parametrize(
    ("extra_field", "expected_field", "expected_code"),
    [
        ("definitions_file", "definitions_file", "duplicate_field"),
        ("ignored_file", "body", "extra_forbidden"),
    ],
)
def test_repeated_and_unknown_files_stop_before_extra_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extra_field: str,
    expected_field: str,
    expected_code: str,
) -> None:
    monkeypatch.setenv("RABBITMQ_CAPTURE_MAX_UPLOAD_BYTES", "8")
    owner_token = str(uuid4())
    owner = OwnerContext(principal_id="anonymous", session_id=owner_token)
    previous = _snapshot(owner, "retained")
    spools, writes = _record_multipart_spools(monkeypatch, tmp_path)
    boundary = "capture-boundary"
    prefix, suffix = _streaming_multipart(boundary, b"marker").split(b"marker")
    next_part = (
        f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="{extra_field}"; '
        'filename="extra.json"\r\n\r\n'
    ).encode()
    chunks = [prefix, b"123456", next_part, b"789", b"unread remainder", suffix]

    with TestClient(app):
        response_status, body, reads = asyncio.run(
            _post_multipart_chunks(chunks, boundary=boundary, owner_token=owner_token),
        )

    assert response_status == 422
    assert body["error"]["code"] == "validation_failed"
    assert body["error"]["issues"][0]["field"] == expected_field
    assert body["error"]["issues"][0]["code"] == expected_code
    assert reads == 3
    assert sum(writes) == 6
    assert len(spools) == 1 and all(spool.closed for spool in spools)
    assert not list(tmp_path.iterdir())
    assert resolve_analysis(AnalysisSource.SESSION, "retained", owner) is previous
    assert not state._active_uploads


@pytest.mark.parametrize(
    ("field", "value", "expected_field", "expected_code"),
    [
        ("amqp://review-user:review-secret@review.invalid/", (None, "ignored"), "body", "extra_forbidden"),
        ("unexpected_file", ("extra.json", b"ignored"), "body", "extra_forbidden"),
        ("analysis_id", (None, "review-last"), "analysis_id", "duplicate_field"),
        ("label", (None, "Other label"), "label", "duplicate_field"),
        ("definitions_file", ("other.json", b"{}"), "definitions_file", "duplicate_field"),
        ("queues_file", ("other-queues.json", b"[]"), "queues_file", "duplicate_field"),
        ("exchanges_file", ("other-exchanges.json", b"[]"), "exchanges_file", "duplicate_field"),
    ],
)
def test_upload_rejects_unknown_and_duplicate_multipart_fields_without_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: tuple[str | None, str | bytes],
    expected_field: str,
    expected_code: str,
) -> None:
    owner_token = str(uuid4())
    owner = OwnerContext(principal_id="anonymous", session_id=owner_token)
    previous = _snapshot(owner, "retained")
    spools, _writes = _record_multipart_spools(monkeypatch, tmp_path)
    staged = _record_staging_directories(monkeypatch, tmp_path)
    parts = [
        ("analysis_id", (None, "retained")),
        ("label", (None, "Existing label")),
        ("definitions_file", ("definitions.json", _valid_definitions(), "application/json")),
        ("queues_file", ("queues.json", b"[]", "application/json")),
        ("exchanges_file", ("exchanges.json", b"[]", "application/json")),
        (field, value),
    ]

    with TestClient(app) as client:
        response = client.post(
            "/api/definitions/upload",
            files=parts,
            headers={"X-Session-Token": owner_token},
        )

    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "validation_failed",
            "message": "Request validation failed.",
            "issues": [
                {
                    "field": expected_field,
                    "code": expected_code,
                    "message": (
                        "Unknown field."
                        if expected_code == "extra_forbidden"
                        else "This field may appear only once."
                    ),
                }
            ],
        }
    }
    if expected_code == "extra_forbidden":
        assert field not in response.text
    assert "review-secret" not in response.text
    assert resolve_analysis(AnalysisSource.SESSION, "retained", owner) is previous
    assert resolve_analysis(AnalysisSource.SESSION, "review-last", owner) is None
    entries = state.list_session_analyses(owner)
    assert len(entries) == 1 and entries[0].snapshot is previous
    assert not state._active_uploads
    assert not staged
    assert spools and all(spool.closed for spool in spools)
    assert not list(tmp_path.iterdir())
    reservation = reserve_session_upload(owner, "retained")
    release_session_upload(reservation)


@pytest.mark.parametrize("extra_byte", [False, True], ids=["at-limit", "limit-plus-one"])
def test_bytewise_multipart_chunks_preserve_the_exact_file_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extra_byte: bool,
) -> None:
    definitions = b'{"vhosts":[]}'
    monkeypatch.setenv("RABBITMQ_CAPTURE_MAX_UPLOAD_BYTES", str(len(definitions)))
    owner_token = str(uuid4())
    spools, writes = _record_multipart_spools(monkeypatch, tmp_path)
    boundary = "capture-boundary"
    multipart = _streaming_multipart(boundary, definitions + (b" " if extra_byte else b""))
    chunks = [bytes([byte]) for byte in multipart]

    with TestClient(app):
        response_status, _body, reads = asyncio.run(
            _post_multipart_chunks(chunks, boundary=boundary, owner_token=owner_token),
        )

    assert response_status == (413 if extra_byte else 201)
    assert reads < len(chunks) if extra_byte else reads == len(chunks)
    assert sum(writes) == len(definitions)
    assert spools and all(spool.closed for spool in spools)
    assert not list(tmp_path.iterdir())
    assert not state._active_uploads


@pytest.mark.parametrize("interruption", ["disconnect", "cancel"])
def test_interrupted_multipart_receipt_closes_spools_without_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interruption: str,
) -> None:
    owner_token = str(uuid4())
    owner = OwnerContext(principal_id="anonymous", session_id=owner_token)
    previous = _snapshot(owner, "retained")
    spools, writes = _record_multipart_spools(monkeypatch, tmp_path)
    staged = _record_staging_directories(monkeypatch, tmp_path)
    boundary = "capture-boundary"
    prefix, _suffix = _streaming_multipart(boundary, b"marker").split(b"marker")

    async def unexpected_parser(*_args: object, **_kwargs: object) -> ProcessedCapture:
        pytest.fail("An interrupted receipt must not start an SDK child.")

    monkeypatch.setattr(definitions_router, "process_staged_capture", unexpected_parser)
    with TestClient(app):
        request = _post_multipart_chunks(
            [prefix, b"123456"],
            boundary=boundary,
            owner_token=owner_token,
            interruption=interruption,
        )
        if interruption == "cancel":
            with pytest.raises(asyncio.CancelledError):
                asyncio.run(request)
        else:
            response_status, _body, reads = asyncio.run(request)
            assert response_status == 400
            assert reads == 2

    assert sum(writes) == 6
    assert spools and all(spool.closed for spool in spools)
    assert not staged
    assert not list(tmp_path.iterdir())
    assert resolve_analysis(AnalysisSource.SESSION, "retained", owner) is previous
    assert not state._active_uploads
    retry = reserve_session_upload(owner, "retained")
    release_session_upload(retry)


def test_incomplete_observation_pair_preserves_previous_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_token = str(uuid4())
    owner = OwnerContext(principal_id="anonymous", session_id=owner_token)
    previous = _snapshot(owner, "retained")

    with TestClient(app) as client:
        response = client.post(
            "/api/definitions/upload",
            data={"analysis_id": "retained"},
            files={
                "definitions_file": ("definitions.json", b"{}", "application/json"),
                "queues_file": ("queues.json", b"[]", "application/json"),
            },
            headers={"X-Session-Token": owner_token},
        )

    assert response.status_code == 422
    assert resolve_analysis(AnalysisSource.SESSION, "retained", owner) is previous


def test_malformed_capture_preserves_previous_snapshot_and_cleans_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_token = str(uuid4())
    owner = OwnerContext(principal_id="anonymous", session_id=owner_token)
    previous = _snapshot(owner, "retained")
    staged = _record_staging_directories(monkeypatch, tmp_path)

    with TestClient(app) as client:
        response = client.post(
            "/api/definitions/upload",
            data={"analysis_id": "retained"},
            files={
                "definitions_file": (
                    "definitions.json",
                    json.dumps({"vhosts": "not-a-list"}),
                    "application/json",
                ),
            },
            headers={"X-Session-Token": owner_token},
        )

    assert response.status_code == 400
    assert resolve_analysis(AnalysisSource.SESSION, "retained", owner) is previous
    assert staged and all(not path.exists() for path in staged)


@pytest.mark.parametrize("definitions", INVALID_CAPTURE_ENVELOPES)
def test_sdk_envelope_rejection_is_safe_and_publishes_no_upload(
    definitions: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    owner_token = str(uuid4())
    owner = OwnerContext(principal_id="anonymous", session_id=owner_token)
    staged = _record_staging_directories(monkeypatch, tmp_path)
    spools, _writes = _record_multipart_spools(monkeypatch, tmp_path)

    with TestClient(app) as client:
        response = client.post(
            "/api/definitions/upload",
            data={"analysis_id": "rejected"},
            files={
                "definitions_file": (
                    "definitions.json",
                    json.dumps(definitions),
                    "application/json",
                )
            },
            headers={"X-Session-Token": owner_token},
        )
        assert response.status_code == 400
        assert response.json() == {
            "error": {
                "code": "invalid_capture",
                "message": "The uploaded files are not a valid RabbitMQ capture.",
                "issues": [],
            }
        }
        assert resolve_analysis(AnalysisSource.SESSION, "rejected", owner) is None
        assert state.list_session_analyses(owner) == ()
        assert not state._active_uploads
        scheduler = get_job_scheduler()
        slot = scheduler.try_acquire_immediate()
        assert slot is not None
        scheduler.release_immediate(slot)

    output = capfd.readouterr()
    for text in (response.text, output.out, output.err):
        assert CAPTURE_PRIVATE_MARKER not in text
        assert "TopologyDefinitionsError" not in text
        assert "invalid schema" not in text
        assert str(tmp_path) not in text
    assert staged and all(not path.exists() for path in staged)
    assert spools and all(spool.closed for spool in spools)
    assert not list(tmp_path.iterdir())
    reservation = reserve_session_upload(owner, "rejected")
    release_session_upload(reservation)


@pytest.mark.parametrize(("definitions", "vhost_count"), VALID_CAPTURE_ENVELOPES)
def test_real_sdk_accepts_empty_and_older_cluster_uploads(
    definitions: dict[str, object],
    vhost_count: int,
) -> None:
    owner_token = str(uuid4())
    with TestClient(app) as client:
        response = client.post(
            "/api/definitions/upload",
            data={"analysis_id": "compatible"},
            files={
                "definitions_file": (
                    "definitions.json",
                    json.dumps(definitions),
                    "application/json",
                )
            },
            headers={"X-Session-Token": owner_token},
        )
        assert response.status_code == 201
        summary = response.json()["analysis"]
        assert summary["captureCompleteness"] == "definitions_only"
        assert summary["cluster"]["vhostCount"] == vhost_count
        topology = client.get(
            "/api/topology",
            params={"source": "session", "analysis_id": "compatible"},
            headers={"X-Session-Token": owner_token},
        )
        assert topology.status_code == 200
        assert topology.json()["snapshotRevision"] == summary["snapshotRevision"]
        if vhost_count == 0:
            assert topology.json()["nodes"] == []
            assert topology.json()["edges"] == []


@pytest.mark.parametrize("definitions", INVALID_CAPTURE_ENVELOPES)
def test_sdk_rejected_replacement_preserves_snapshot_result_and_idempotency(
    definitions: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_token = str(uuid4())
    owner = OwnerContext(principal_id="anonymous", session_id=owner_token)
    headers = {"X-Session-Token": owner_token}
    staged = _record_staging_directories(monkeypatch, tmp_path)
    spools, _writes = _record_multipart_spools(monkeypatch, tmp_path)
    with TestClient(app) as client:
        uploaded = client.post(
            "/api/definitions/upload",
            data={"analysis_id": "retained"},
            files={
                "definitions_file": ("definitions.json", _valid_definitions(), "application/json")
            },
            headers=headers,
        )
        assert uploaded.status_code == 201
        previous = resolve_analysis(AnalysisSource.SESSION, "retained", owner)
        assert previous is not None
        start_payload = {
            "source": "session",
            "analysisId": "retained",
            "snapshotRevision": previous.snapshot_revision,
        }
        start_headers = {**headers, "Idempotency-Key": str(uuid4())}
        started = client.post("/api/scc-runs", json=start_payload, headers=start_headers)
        assert started.status_code == 202
        run_id = started.json()["runId"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            run = client.get(
                f"/api/scc-runs/{run_id}",
                params={"snapshot_revision": previous.snapshot_revision},
                headers=headers,
            )
            assert run.status_code == 200
            if run.json()["status"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.01)
        assert run.json()["status"] == "succeeded"
        scheduler = get_job_scheduler()
        result = scheduler.result_repository.read(run_id)
        retained_bytes = scheduler.result_repository.accounted_bytes
        rejected = client.post(
            "/api/definitions/upload",
            data={"analysis_id": "retained"},
            files={
                "definitions_file": (
                    "definitions.json",
                    json.dumps(definitions),
                    "application/json",
                )
            },
            headers=headers,
        )
        assert rejected.status_code == 400
        assert rejected.json()["error"]["code"] == "invalid_capture"
        assert CAPTURE_PRIVATE_MARKER not in rejected.text
        assert resolve_analysis(AnalysisSource.SESSION, "retained", owner) is previous
        assert scheduler.result_repository.read(run_id) == result
        assert scheduler.result_repository.accounted_bytes == retained_bytes
        assert scheduler.run_count() == 1
        replay = client.post("/api/scc-runs", json=start_payload, headers=start_headers)
        assert replay.status_code == 200
        assert replay.json()["runId"] == run_id
        assert replay.json()["status"] == "succeeded"
        assert replay.json()["result"] == run.json()["result"]
        assert not state._active_uploads
        reservation = reserve_session_upload(owner, "retained")
        release_session_upload(reservation)
        slot = scheduler.try_acquire_immediate()
        assert slot is not None
        scheduler.release_immediate(slot)
        assert len(staged) == 2 and all(not path.exists() for path in staged)
        assert spools and all(spool.closed for spool in spools)
        assert not list(tmp_path.iterdir())


def test_unexpected_child_failure_preserves_previous_snapshot_and_cleans_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_token = str(uuid4())
    owner = OwnerContext(principal_id="anonymous", session_id=owner_token)
    previous = _snapshot(owner, "retained")
    staged = _record_staging_directories(monkeypatch, tmp_path)

    async def failed(
        staged_capture: StagedCapture,
        *,
        in_cluster_amqp_hosts: tuple[str, ...],
        cluster_label: str,
        scheduler: JobScheduler,
        is_disconnected: Callable[[], Awaitable[bool]],
    ) -> ProcessedCapture:
        return await process_staged_capture(
            staged_capture,
            in_cluster_amqp_hosts=in_cluster_amqp_hosts,
            cluster_label=cluster_label,
            scheduler=scheduler,
            is_disconnected=is_disconnected,
            child_target=_unexpected_capture_child,
        )

    monkeypatch.setattr(definitions_router, "process_staged_capture", failed)

    with TestClient(app) as client:
        response = client.post(
            "/api/definitions/upload",
            data={"analysis_id": "retained"},
            files={
                "definitions_file": (
                    "definitions.json",
                    _valid_definitions(),
                    "application/json",
                ),
            },
            headers={"X-Session-Token": owner_token},
        )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "capture_processing_failed"
    assert resolve_analysis(AnalysisSource.SESSION, "retained", owner) is previous
    assert staged and all(not path.exists() for path in staged)
    retry = reserve_session_upload(owner, "retained")
    release_session_upload(retry)


def test_complete_capture_is_processed_in_shared_child_and_published_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_token = str(uuid4())
    staged_directories = _record_staging_directories(monkeypatch, tmp_path)

    with TestClient(app) as client:
        response = client.post(
            "/api/definitions/upload",
            data={
                "analysis_id": "uploaded",
                "label": "Uploaded label",
                "in_cluster_amqp_host": ["RABBIT-A", "rabbit-b"],
            },
            files={
                "definitions_file": (
                    "definitions.json",
                    _valid_definitions(),
                    "application/json",
                ),
                "queues_file": ("queues.json", b"[]", "application/json"),
                "exchanges_file": ("exchanges.json", b"[]", "application/json"),
            },
            headers={"X-Session-Token": owner_token},
        )

    assert response.status_code == 201
    assert response.json()["analysis"]["captureCompleteness"] == "complete"
    owner = OwnerContext(principal_id="anonymous", session_id=owner_token)
    snapshot = resolve_analysis(AnalysisSource.SESSION, "uploaded", owner)
    assert snapshot is not None
    assert snapshot.in_cluster_amqp_hosts == ("rabbit-a", "rabbit-b")
    assert snapshot.topology.cluster_label == "Uploaded label"
    assert staged_directories and all(not path.exists() for path in staged_directories)


def test_busy_shared_capacity_rejects_without_queueing_and_preserves_previous_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RABBITMQ_ANALYSIS_WORKERS", "1")
    owner_token = str(uuid4())
    owner = OwnerContext(principal_id="anonymous", session_id=owner_token)
    previous = _snapshot(owner, "retained")
    staged = _record_staging_directories(monkeypatch, tmp_path)

    with TestClient(app) as client:
        scheduler = get_job_scheduler()
        occupied = scheduler.try_acquire_immediate()
        assert occupied is not None
        try:
            response = client.post(
                "/api/definitions/upload",
                data={"analysis_id": "retained"},
                files={
                    "definitions_file": (
                        "definitions.json",
                        _valid_definitions(),
                        "application/json",
                    ),
                },
                headers={"X-Session-Token": owner_token},
            )
        finally:
            scheduler.release_immediate(occupied)

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert response.json()["error"]["code"] == "analysis_capacity_unavailable"
    assert resolve_analysis(AnalysisSource.SESSION, "retained", owner) is previous
    assert staged and all(not path.exists() for path in staged)


def test_disconnected_processing_cleans_staging_and_preserves_previous_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_token = str(uuid4())
    owner = OwnerContext(principal_id="anonymous", session_id=owner_token)
    previous = _snapshot(owner, "retained")
    staged = _record_staging_directories(monkeypatch, tmp_path)

    async def disconnected(*_args: object, **_kwargs: object) -> ProcessedCapture:
        raise CaptureRequestDisconnectedError

    monkeypatch.setattr(definitions_router, "process_staged_capture", disconnected)

    with TestClient(app) as client:
        response = client.post(
            "/api/definitions/upload",
            data={"analysis_id": "retained"},
            files={
                "definitions_file": (
                    "definitions.json",
                    _valid_definitions(),
                    "application/json",
                ),
            },
            headers={"X-Session-Token": owner_token},
        )

    assert response.status_code == 499
    assert resolve_analysis(AnalysisSource.SESSION, "retained", owner) is previous
    assert staged and all(not path.exists() for path in staged)
    retry = reserve_session_upload(owner, "retained")
    release_session_upload(retry)


def test_cancelled_processing_cleans_staging_and_preserves_previous_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_token = str(uuid4())
    owner = OwnerContext(principal_id="anonymous", session_id=owner_token)
    previous = _snapshot(owner, "retained")
    staged = _record_staging_directories(monkeypatch, tmp_path)

    async def cancelled(*_args: object, **_kwargs: object) -> ProcessedCapture:
        raise asyncio.CancelledError

    monkeypatch.setattr(definitions_router, "process_staged_capture", cancelled)

    with TestClient(app) as client, pytest.raises(FutureCancelledError):
        client.post(
            "/api/definitions/upload",
            data={"analysis_id": "retained"},
            files={
                "definitions_file": (
                    "definitions.json",
                    _valid_definitions(),
                    "application/json",
                ),
            },
            headers={"X-Session-Token": owner_token},
        )

    assert resolve_analysis(AnalysisSource.SESSION, "retained", owner) is previous
    assert staged and all(not path.exists() for path in staged)
    retry = reserve_session_upload(owner, "retained")
    release_session_upload(retry)


def test_parser_child_is_terminated_on_disconnect_and_releases_capacity(tmp_path: Path) -> None:
    staging_root = tmp_path / "staging"
    staging_root.mkdir()
    definitions_path = staging_root / "definitions.json"
    definitions_path.write_bytes(_valid_definitions())
    staged = StagedCapture(definitions_path, None, None, definitions_path.stat().st_size)
    scheduler = JobScheduler(
        _job_settings(tmp_path / "results"),
        ResultRepository(tmp_path / "results", 10_000),
    )

    async def run() -> None:
        async def disconnected() -> bool:
            return (staging_root / "child-started").exists()

        with pytest.raises(CaptureRequestDisconnectedError):
            await process_staged_capture(
                staged,
                in_cluster_amqp_hosts=(),
                cluster_label="Test",
                scheduler=scheduler,
                is_disconnected=disconnected,
                child_target=_blocking_capture_child,
            )

    asyncio.run(run())
    assert scheduler.compute_slots.leased == 0
    assert all(child.name != "analysis-capture-upload" for child in active_children())
    scheduler.shutdown()


def test_parser_child_is_terminated_when_request_task_is_cancelled(tmp_path: Path) -> None:
    staging_root = tmp_path / "staging"
    staging_root.mkdir()
    definitions_path = staging_root / "definitions.json"
    definitions_path.write_bytes(_valid_definitions())
    staged = StagedCapture(definitions_path, None, None, definitions_path.stat().st_size)
    scheduler = JobScheduler(
        _job_settings(tmp_path / "results"),
        ResultRepository(tmp_path / "results", 10_000),
    )

    async def run() -> None:
        async def connected() -> bool:
            return False

        task = asyncio.create_task(
            process_staged_capture(
                staged,
                in_cluster_amqp_hosts=(),
                cluster_label="Test",
                scheduler=scheduler,
                is_disconnected=connected,
                child_target=_blocking_capture_child,
            ),
        )
        while not (staging_root / "child-started").exists():
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert scheduler.compute_slots.leased == 0
    assert all(child.name != "analysis-capture-upload" for child in active_children())
    scheduler.shutdown()


def test_same_analysis_upload_reservation_blocks_upload_job_pin_and_deletion() -> None:
    owner = OwnerContext(principal_id="analyst", session_id=str(uuid4()))
    previous = _snapshot(owner, "retained")
    reservation = reserve_session_upload(owner, "retained")
    try:
        with pytest.raises(AnalysisActiveError):
            reserve_session_upload(owner, "retained")
        with pytest.raises(AnalysisActiveError):
            state.pin_analysis_run(
                owner,
                AnalysisSource.SESSION,
                "retained",
                previous.snapshot_revision,
            )
        with pytest.raises(AnalysisActiveError):
            state.delete_session_analysis(owner, "retained")
    finally:
        release_session_upload(reservation)


def test_stale_upload_reservation_cannot_release_or_authorize_a_new_reservation() -> None:
    owner = OwnerContext(principal_id="analyst", session_id=str(uuid4()))
    stale = reserve_session_upload(owner, "reserved")
    release_session_upload(stale)
    current = reserve_session_upload(owner, "reserved")

    try:
        release_session_upload(stale)
        with pytest.raises(AnalysisActiveError):
            publish_session_analysis(
                owner,
                _snapshot_value("reserved"),
                reservation=stale,
            )
        with pytest.raises(AnalysisActiveError):
            reserve_session_upload(owner, "reserved")
    finally:
        release_session_upload(current)


def test_stale_deletion_reservation_cannot_release_a_new_reservation() -> None:
    owner = OwnerContext(principal_id="analyst", session_id=str(uuid4()))
    publish_session_analysis(owner, _snapshot_value("reserved"))
    stale = state.reserve_session_analysis_deletion(owner, "reserved")
    assert stale is not None
    state.release_session_analysis_deletion(stale)
    publish_session_analysis(owner, _snapshot_value("reserved"))
    current = state.reserve_session_analysis_deletion(owner, "reserved")
    assert current is not None

    try:
        state.release_session_analysis_deletion(stale)
        with pytest.raises(AnalysisActiveError):
            reserve_session_upload(owner, "reserved")
    finally:
        state.release_session_analysis_deletion(current)


def test_deletion_holds_owner_and_global_slot_until_resource_cleanup_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RABBITMQ_SESSION_ANALYSIS_LIMIT", "1")
    monkeypatch.setenv("RABBITMQ_SESSION_ANALYSIS_GLOBAL_LIMIT", "1")
    configure_analysis_store()
    owner = OwnerContext(principal_id="analyst", session_id=str(uuid4()))
    publish_session_analysis(owner, _snapshot_value("deleting"))
    deletion = state.reserve_session_analysis_deletion(owner, "deleting")
    assert deletion is not None

    with pytest.raises(state.SessionAnalysisLimitError):
        reserve_session_upload(owner, "replacement")
    with pytest.raises(state.SessionStoreCapacityError):
        reserve_session_upload(
            OwnerContext(principal_id="other", session_id=str(uuid4())),
            "replacement",
        )

    state.release_session_analysis_deletion(deletion)
    replacement = reserve_session_upload(owner, "replacement")
    release_session_upload(replacement)


def test_expiry_cleanup_failure_does_not_leak_an_unreturned_upload_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RABBITMQ_SESSION_TOPOLOGY_TTL_SECONDS", "1")
    configure_analysis_store()
    owner = OwnerContext(principal_id="analyst", session_id=str(uuid4()))
    publish_session_analysis(owner, _snapshot_value("expired"))
    state._session_store[(owner, "expired")].last_accessed_at -= timedelta(seconds=2)

    def fail_cleanup(_expired: state.ExpiredSessionAnalysis) -> None:
        raise RuntimeError("cleanup failed")

    state.set_session_expiry_handler(fail_cleanup)
    with pytest.raises(RuntimeError, match="cleanup failed"):
        reserve_session_upload(owner, "new-capture")

    assert (owner, "new-capture") not in state._active_uploads
    state.set_session_expiry_handler(None)
    retry = reserve_session_upload(owner, "new-capture")
    release_session_upload(retry)


def test_expiry_cleanup_failure_restores_an_unreturned_deletion_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RABBITMQ_SESSION_TOPOLOGY_TTL_SECONDS", "1")
    configure_analysis_store()
    owner = OwnerContext(principal_id="analyst", session_id=str(uuid4()))
    publish_session_analysis(owner, _snapshot_value("expired"))
    retained = _snapshot_value("retained")
    publish_session_analysis(owner, retained)
    state._session_store[(owner, "expired")].last_accessed_at -= timedelta(seconds=2)

    def fail_cleanup(_expired: state.ExpiredSessionAnalysis) -> None:
        raise RuntimeError("cleanup failed")

    state.set_session_expiry_handler(fail_cleanup)
    with pytest.raises(RuntimeError, match="cleanup failed"):
        state.reserve_session_analysis_deletion(owner, "retained")

    assert (owner, "retained") not in state._active_deletions
    assert state._session_store[(owner, "retained")].snapshot is retained
    state.set_session_expiry_handler(None)
    retry = state.reserve_session_analysis_deletion(owner, "retained")
    assert retry is not None
    state.release_session_analysis_deletion(retry)


def test_expiry_cleanup_failure_rolls_back_snapshot_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RABBITMQ_SESSION_TOPOLOGY_TTL_SECONDS", "1")
    configure_analysis_store()
    owner = OwnerContext(principal_id="analyst", session_id=str(uuid4()))
    retained = _snapshot_value("retained")
    publish_session_analysis(owner, retained)
    publish_session_analysis(owner, _snapshot_value("expired"))
    state._session_store[(owner, "expired")].last_accessed_at -= timedelta(seconds=2)

    def fail_cleanup(_expired: state.ExpiredSessionAnalysis) -> None:
        raise RuntimeError("cleanup failed")

    state.set_session_expiry_handler(fail_cleanup)
    with pytest.raises(RuntimeError, match="cleanup failed"):
        publish_session_analysis(
            owner,
            _snapshot_value("retained", "sha256:" + "b" * 64),
        )

    assert state._session_store[(owner, "retained")].snapshot is retained


def test_expiry_cleanup_failure_rolls_back_an_unadmitted_run_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RABBITMQ_SESSION_TOPOLOGY_TTL_SECONDS", "1")
    configure_analysis_store()
    owner = OwnerContext(principal_id="analyst", session_id=str(uuid4()))
    retained = _snapshot_value("retained")
    publish_session_analysis(owner, retained)
    publish_session_analysis(owner, _snapshot_value("expired"))
    state._session_store[(owner, "expired")].last_accessed_at -= timedelta(seconds=2)

    def fail_cleanup(_expired: state.ExpiredSessionAnalysis) -> None:
        raise RuntimeError("cleanup failed")

    state.set_session_expiry_handler(fail_cleanup)
    with pytest.raises(RuntimeError, match="cleanup failed"):
        state.pin_analysis_run(
            owner,
            AnalysisSource.SESSION,
            "retained",
            retained.snapshot_revision,
        )

    assert state._session_store[(owner, "retained")].active_job_pins == 0
    state.set_session_expiry_handler(None)
    retry = reserve_session_upload(owner, "retained")
    release_session_upload(retry)


def test_concurrent_per_owner_upload_reservations_cannot_overbook_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RABBITMQ_SESSION_ANALYSIS_LIMIT", "1")
    configure_analysis_store()
    owner = OwnerContext(principal_id="analyst", session_id=str(uuid4()))
    barrier = Barrier(2)

    def reserve(analysis_id: str) -> object:
        barrier.wait()
        try:
            return reserve_session_upload(owner, analysis_id)
        except Exception as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(reserve, ("first", "second")))

    reservations = [
        outcome for outcome in outcomes if isinstance(outcome, state.SessionUploadReservation)
    ]
    failures = [outcome for outcome in outcomes if isinstance(outcome, Exception)]
    assert len(reservations) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], state.SessionAnalysisLimitError)
    release_session_upload(reservations[0])


def test_concurrent_global_upload_reservations_cannot_overbook_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RABBITMQ_SESSION_ANALYSIS_GLOBAL_LIMIT", "1")
    configure_analysis_store()
    owners = (
        OwnerContext(principal_id="analyst", session_id=str(uuid4())),
        OwnerContext(principal_id="analyst", session_id=str(uuid4())),
    )
    barrier = Barrier(2)

    def reserve(owner: OwnerContext) -> object:
        barrier.wait()
        try:
            return reserve_session_upload(owner, "capture")
        except Exception as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(reserve, owners))

    reservations = [
        outcome for outcome in outcomes if isinstance(outcome, state.SessionUploadReservation)
    ]
    failures = [outcome for outcome in outcomes if isinstance(outcome, Exception)]
    assert len(reservations) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], state.SessionStoreCapacityError)
    release_session_upload(reservations[0])


def test_direct_publication_respects_an_active_upload_capacity_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RABBITMQ_SESSION_ANALYSIS_LIMIT", "1")
    configure_analysis_store()
    owner = OwnerContext(principal_id="analyst", session_id=str(uuid4()))
    reservation = reserve_session_upload(owner, "reserved")

    try:
        with pytest.raises(state.SessionAnalysisLimitError):
            publish_session_analysis(owner, _snapshot_value("other"))
    finally:
        release_session_upload(reservation)


def test_expiry_frees_owner_and_global_capacity_before_new_upload_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RABBITMQ_SESSION_ANALYSIS_LIMIT", "1")
    monkeypatch.setenv("RABBITMQ_SESSION_ANALYSIS_GLOBAL_LIMIT", "1")
    monkeypatch.setenv("RABBITMQ_SESSION_TOPOLOGY_TTL_SECONDS", "1")
    configure_analysis_store()
    owner = OwnerContext(principal_id="analyst", session_id=str(uuid4()))
    publish_session_analysis(owner, _snapshot_value("expired"))
    state._session_store[(owner, "expired")].last_accessed_at -= timedelta(seconds=2)

    reservation = reserve_session_upload(owner, "replacement")
    try:
        publication = publish_session_analysis(
            owner,
            _snapshot_value("replacement"),
            reservation=reservation,
        )
    finally:
        release_session_upload(reservation)

    assert publication.replaced is False
    assert resolve_analysis(AnalysisSource.SESSION, "expired", owner) is None
    assert resolve_analysis(AnalysisSource.SESSION, "replacement", owner) is not None


def test_identical_refresh_retains_terminal_result_and_changed_refresh_invalidates_it() -> None:
    owner_token = str(uuid4())
    owner = OwnerContext(principal_id="anonymous", session_id=owner_token)
    definitions = _valid_definitions()

    def upload(client: TestClient, content: bytes, *, label: str | None = None) -> object:
        data = {"analysis_id": "refreshable"}
        if label is not None:
            data["label"] = label
        return client.post(
            "/api/definitions/upload",
            data=data,
            files={
                "definitions_file": ("definitions.json", content, "application/json"),
            },
            headers={"X-Session-Token": owner_token},
        )

    with TestClient(app) as client:
        initial_response = upload(client, definitions)
        assert initial_response.status_code == 201
        initial = initial_response.json()["analysis"]
        revision = initial["snapshotRevision"]
        start = client.post(
            "/api/scc-runs",
            json={
                "source": "session",
                "analysisId": "refreshable",
                "snapshotRevision": revision,
            },
            headers={
                "X-Session-Token": owner_token,
                "Idempotency-Key": str(uuid4()),
            },
        )
        assert start.status_code == 202
        run_id = start.json()["runId"]
        for _attempt in range(100):
            run_response = client.get(
                f"/api/scc-runs/{run_id}",
                params={"snapshot_revision": revision},
                headers={"X-Session-Token": owner_token},
            )
            assert run_response.status_code == 200
            if run_response.json()["status"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.01)
        assert run_response.json()["status"] == "succeeded"
        scheduler = get_job_scheduler()
        retained_bytes = scheduler.result_repository.accounted_bytes
        assert retained_bytes > 0

        entry = state._session_store[(owner, "refreshable")]
        entry.last_accessed_at -= timedelta(hours=1)
        stale_access = entry.last_accessed_at
        identical = upload(client, definitions, label="Renamed capture")
        assert identical.status_code == 200
        assert identical.json()["analysis"]["snapshotRevision"] == revision
        assert identical.json()["analysis"]["loadedAt"] == initial["loadedAt"]
        assert identical.json()["analysis"]["label"] == "Renamed capture"
        identical_expiry = datetime.fromisoformat(identical.json()["analysis"]["expiresAt"])
        assert identical_expiry > datetime.fromisoformat(initial["expiresAt"])
        refreshed = state._session_store[(owner, "refreshable")]
        assert refreshed.last_accessed_at > stale_access
        assert refreshed.snapshot.label == "Renamed capture"
        assert refreshed.snapshot.topology.cluster_label == "Renamed capture"
        assert scheduler.get_owned(run_id, owner) is not None
        assert scheduler.result_repository.accounted_bytes == retained_bytes

        changed_definitions = definitions.replace(b"Uploaded cluster", b"Changed cluster")
        changed = upload(client, changed_definitions)
        assert changed.status_code == 200
        assert changed.json()["analysis"]["snapshotRevision"] != revision
        assert scheduler.get_owned(run_id, owner) is None
        assert scheduler.run_count() == 0
        assert scheduler.result_repository.accounted_bytes == 0


def test_delete_removes_terminal_run_file_quota_and_idempotency() -> None:
    owner_token = str(uuid4())

    with TestClient(app) as client:
        upload = client.post(
            "/api/definitions/upload",
            data={"analysis_id": "deletable"},
            files={
                "definitions_file": (
                    "definitions.json",
                    _valid_definitions(),
                    "application/json",
                ),
            },
            headers={"X-Session-Token": owner_token},
        )
        revision = upload.json()["analysis"]["snapshotRevision"]
        start = client.post(
            "/api/scc-runs",
            json={
                "source": "session",
                "analysisId": "deletable",
                "snapshotRevision": revision,
            },
            headers={
                "X-Session-Token": owner_token,
                "Idempotency-Key": str(uuid4()),
            },
        )
        run_id = start.json()["runId"]
        for _attempt in range(100):
            run = client.get(
                f"/api/scc-runs/{run_id}",
                params={"snapshot_revision": revision},
                headers={"X-Session-Token": owner_token},
            )
            if run.json()["status"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.01)
        assert run.json()["status"] == "succeeded"
        scheduler = get_job_scheduler()
        assert scheduler.result_repository.accounted_bytes > 0

        deleted = client.delete(
            "/api/analyses/deletable",
            headers={"X-Session-Token": owner_token},
        )

        assert deleted.status_code == 204
        assert scheduler.run_count() == 0
        assert scheduler.result_repository.accounted_bytes == 0
        assert scheduler._idempotency == {}


@pytest.mark.parametrize("value", ["0", "-1", "not-a-number"])
def test_invalid_capture_upload_limit_fails_configuration(
    value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RABBITMQ_CAPTURE_MAX_UPLOAD_BYTES", value)

    with pytest.raises(ValueError, match="RABBITMQ_CAPTURE_MAX_UPLOAD_BYTES"):
        configure_capture_uploads()
