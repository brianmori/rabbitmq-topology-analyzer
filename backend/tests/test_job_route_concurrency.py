import asyncio
import signal
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from functools import partial
from threading import Event
from typing import TYPE_CHECKING
from uuid import uuid4

import httpx2
import pytest
from fastapi import FastAPI
from rabbitmq_management_sdk.topology import ClusterTopology

import auth.router as auth_routes
import cycles.router as cycle_routes
import definitions.router as upload_routes
import scc.router as scc_routes
from analyses.router import router as analyses_router
from auth.activity import OwnerActivityRegistry
from auth.cleanup import cleanup_owner_session
from auth.middleware import PreBodyAuthMiddleware
from core import state
from core.errors import register_error_handlers
from core.security import AuthMode, configure_authentication_dependencies
from core.session import OwnerContext
from core.state import AnalysisSnapshot, AnalysisSource, CaptureCompleteness
from definitions.config import configure_capture_uploads
from definitions.router import router as definitions_router
from jobs.config import JobSettings
from jobs.runtime import configure_job_runtime, shutdown_job_runtime
from test_auth_middleware import _BASIC_CREDENTIAL, _basic_runtime
from test_capture_uploads import _streaming_multipart, _valid_definitions
from topology.router import router as topology_router

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
    from multiprocessing.process import BaseProcess
    from pathlib import Path

    from fastapi import Request

    from definitions.processing import ProcessedCapture
    from definitions.staging import StagedCapture
    from jobs.models import AdmissionResult, JobStart, ResultEnvelopeContext
    from jobs.scheduler import JobScheduler


@dataclass
class Service:
    app: FastAPI
    scheduler: JobScheduler
    owner: OwnerContext
    snapshot: AnalysisSnapshot


@pytest.fixture
def service(tmp_path: Path) -> Iterator[Service]:
    configure_authentication_dependencies(AuthMode.NONE)
    state.configure_analysis_store()
    configure_capture_uploads()
    state._session_store.clear()
    snapshot = AnalysisSnapshot(
        topology=ClusterTopology(
            exchanges=frozenset(),
            queues=frozenset(),
            shovels=frozenset(),
            edges=frozenset(),
        ),
        analysis_id="thread-fixture",
        source=AnalysisSource.SESSION,
        label="Thread fixture",
        capture_completeness=CaptureCompleteness.DEFINITIONS_ONLY,
        snapshot_revision="sha256:" + "a" * 64,
        loaded_at=datetime.now(UTC),
        cluster_name=None,
        cluster_tags={},
        in_cluster_amqp_hosts=(),
        vhost_count=0,
    )
    owner = OwnerContext("anonymous", str(uuid4()))
    state.publish_session_analysis(owner, snapshot)
    scheduler = configure_job_runtime(
        JobSettings(
            worker_count=1,
            queue_capacity=1,
            heartbeat_ttl=timedelta(seconds=120),
            result_ttl=timedelta(seconds=120),
            results_dir=tmp_path / "results",
            results_max_bytes=1_000_000,
        )
    )
    application = FastAPI()
    register_error_handlers(application)
    for router in (
        scc_routes.router,
        cycle_routes.router,
        analyses_router,
        topology_router,
        definitions_router,
    ):
        application.include_router(router, prefix="/api")

    @application.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    try:
        yield Service(application, scheduler, owner, snapshot)
    finally:
        shutdown_job_runtime()
        state._session_store.clear()


class ThreadGate:
    def __init__(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.entered = asyncio.Event()
        self.release = Event()
        self.progressed = False

    def wait(self) -> None:
        self.loop.call_soon_threadsafe(self.entered.set)
        self.progressed = self.release.wait(3)

    def observe_join(self, process: BaseProcess, monkeypatch: pytest.MonkeyPatch) -> None:
        original_join = process.join

        def join(timeout: float | None = None) -> None:
            if timeout is not None:
                self.wait()
            original_join(timeout)

        monkeypatch.setattr(process, "join", join)


def _headers(service: Service) -> dict[str, str]:
    return {"X-Session-Token": service.owner.session_id, "Idempotency-Key": str(uuid4())}


def _payload(service: Service) -> dict[str, str]:
    return {
        "source": "session",
        "analysisId": service.snapshot.analysis_id,
        "snapshotRevision": service.snapshot.snapshot_revision,
    }


def _assert_owner_clean(service: Service) -> None:
    assert service.scheduler.run_count() == 0
    assert service.scheduler.compute_slots.leased == 0
    assert service.scheduler.active_process_count() == 0
    assert not service.scheduler._idempotency
    assert state.list_session_analyses(service.owner) == ()
    assert service.scheduler.result_repository.accounted_bytes == 0


async def _health_while_blocked(
    client: httpx2.AsyncClient,
    operation: Awaitable[httpx2.Response],
    gate: ThreadGate,
) -> httpx2.Response:
    task = asyncio.create_task(operation)
    try:
        await asyncio.wait_for(gate.entered.wait(), 5)
        response = await client.get("/api/health")
        assert response.status_code == 200
        gate.release.set()
        result = await task
        assert gate.progressed, "scheduler work blocked the event loop until its barrier timed out"
        return result
    finally:
        gate.release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("operation", ["scc", "cycle"])
def test_admission_leaves_health_responsive(
    service: Service,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    async def run() -> None:
        gate = ThreadGate()
        repository = service.scheduler.result_repository
        original_prepare = repository.prepare

        def prepare(run_id: str) -> object:
            gate.wait()
            return original_prepare(run_id)

        monkeypatch.setattr(repository, "prepare", prepare)
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=service.app),
            base_url="http://testserver",
        ) as client:
            response = await _health_while_blocked(
                client,
                client.post(
                    f"/api/{operation}-runs", json=_payload(service), headers=_headers(service)
                ),
                gate,
            )
            assert response.status_code == 202

    asyncio.run(run())


async def _completed_run(client: httpx2.AsyncClient, service: Service, operation: str) -> str:
    started = await client.post(
        f"/api/{operation}-runs",
        json=_payload(service),
        headers=_headers(service),
    )
    assert started.status_code == 202
    run_id = started.json()["runId"]
    await _wait_succeeded(client, service, operation, run_id)
    return run_id


async def _wait_succeeded(
    client: httpx2.AsyncClient,
    service: Service,
    operation: str,
    run_id: str,
) -> None:
    async with asyncio.timeout(5):
        while True:
            response = await client.get(
                f"/api/{operation}-runs/{run_id}",
                params={"snapshot_revision": service.snapshot.snapshot_revision},
                headers=_headers(service),
            )
            assert response.status_code == 200
            if response.json()["status"] == "succeeded":
                return
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("operation", ["scc", "cycle"])
def test_run_lookup_keeps_health_responsive(
    service: Service,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    async def run() -> None:
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=service.app),
            base_url="http://testserver",
        ) as client:
            run_id = await _completed_run(client, service, operation)
            gate = ThreadGate()
            original_get = service.scheduler.get_owned

            def get_owned(*args: object) -> object:
                gate.wait()
                return original_get(*args)

            monkeypatch.setattr(service.scheduler, "get_owned", get_owned)
            response = await _health_while_blocked(
                client,
                client.get(
                    f"/api/{operation}-runs/{run_id}",
                    params={"snapshot_revision": service.snapshot.snapshot_revision},
                    headers=_headers(service),
                ),
                gate,
            )
            assert response.status_code == 200

    asyncio.run(run())


def test_completed_scc_result_read_keeps_health_responsive(
    service: Service,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=service.app),
            base_url="http://testserver",
        ) as client:
            run_id = await _completed_run(client, service, "scc")
            gate = ThreadGate()
            repository = service.scheduler.result_repository
            original_read = repository.read

            def read(result_id: str) -> bytes:
                gate.wait()
                return original_read(result_id)

            monkeypatch.setattr(repository, "read", read)
            response = await _health_while_blocked(
                client,
                client.get(
                    f"/api/scc-runs/{run_id}",
                    params={"snapshot_revision": service.snapshot.snapshot_revision},
                    headers=_headers(service),
                ),
                gate,
            )
            assert response.status_code == 200
            assert response.json()["result"] == {"components": []}

    asyncio.run(run())


@pytest.mark.parametrize("path", ["analyses", "topology"])
def test_snapshot_expiry_callbacks_keep_health_responsive(service: Service, path: str) -> None:
    expired_owner = OwnerContext("anonymous", str(uuid4()))
    state.publish_session_analysis(expired_owner, service.snapshot)
    state._session_store[
        (expired_owner, service.snapshot.analysis_id)
    ].last_accessed_at -= timedelta(days=1)

    async def run() -> None:
        gate = ThreadGate()

        def expire(expired: state.ExpiredSessionAnalysis) -> None:
            gate.wait()
            service.scheduler.invalidate_expired_analysis(expired)

        state.set_session_expiry_handler(expire)
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=service.app),
            base_url="http://testserver",
        ) as client:
            response = await _health_while_blocked(
                client,
                client.get(
                    f"/api/{path}",
                    params=(
                        {"source": "session", "analysis_id": service.snapshot.analysis_id}
                        if path == "topology"
                        else None
                    ),
                    headers=_headers(service),
                ),
                gate,
            )
            assert response.status_code == 200

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["scc", "cycle"])
def test_basic_job_admission_uses_the_tracked_dependency_owner(
    service: Service,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    runtime = _basic_runtime()
    registry = OwnerActivityRegistry()
    wrapped = PreBodyAuthMiddleware(
        service.app,
        mode=AuthMode.BASIC,
        authenticator=runtime.authenticate,
        activity_registry=registry,
    )

    async def run() -> None:
        token = str(uuid4())
        authentication = await runtime.authenticate(
            _BASIC_CREDENTIAL,
            {"type": "http", "headers": [(b"x-session-token", token.encode())]},
        )
        assert authentication is not None and authentication.owner_context is not None
        owner = authentication.owner_context
        state.publish_session_analysis(owner, service.snapshot)
        original_admit = service.scheduler.admit
        task: asyncio.Task[httpx2.Response] | None = None

        def admit(
            *,
            start: JobStart,
            canonical_payload: bytes,
            idempotency_key: str,
            on_admit: Callable[[], None] | None = None,
        ) -> AdmissionResult:
            assert start.owner_context == owner
            assert task in registry._tasks[owner]
            return original_admit(
                start=start,
                canonical_payload=canonical_payload,
                idempotency_key=idempotency_key,
                on_admit=on_admit,
            )

        monkeypatch.setattr(service.scheduler, "admit", admit)
        try:
            async with httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=wrapped),
                base_url="https://testserver",
            ) as client:
                task = asyncio.create_task(
                    client.post(
                        f"/api/{operation}-runs",
                        json=_payload(service),
                        headers={
                            "Authorization": _BASIC_CREDENTIAL,
                            "X-Session-Token": token.upper(),
                            "Idempotency-Key": str(uuid4()),
                        },
                    )
                )
                response = await task
                assert response.status_code == 202
                assert service.scheduler._runs[response.json()["runId"]].owner_context == owner
                assert registry._tasks == {}
        finally:
            assert runtime.basic_authenticator is not None
            await runtime.basic_authenticator.close()

    asyncio.run(run())


@pytest.mark.parametrize("phase", ["receipt", "processing"])
def test_basic_logout_drains_upload_and_preserves_other_owner(  # noqa: PLR0915 - full race
    service: Service,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    runtime = _basic_runtime()
    registry = OwnerActivityRegistry()
    service.app.include_router(auth_routes.router, prefix="/api")
    service.app.dependency_overrides[auth_routes.get_auth_runtime] = lambda: runtime
    wrapped = PreBodyAuthMiddleware(
        service.app,
        mode=AuthMode.BASIC,
        authenticator=runtime.authenticate,
        activity_registry=registry,
    )

    async def cleanup(owner: OwnerContext) -> None:
        await cleanup_owner_session(owner, activity_registry=registry, scheduler=service.scheduler)

    monkeypatch.setattr(auth_routes, "cleanup_owner_session", cleanup)

    async def run() -> None:  # noqa: PLR0915 - coordinated request lifecycle
        token = str(uuid4())
        other_token = str(uuid4())
        authentication = await runtime.authenticate(
            _BASIC_CREDENTIAL,
            {"type": "http", "headers": [(b"x-session-token", token.encode())]},
        )
        assert authentication is not None and authentication.owner_context is not None
        owner = authentication.owner_context
        other = OwnerContext(owner.principal_id, other_token)
        state.publish_session_analysis(owner, service.snapshot)
        state.publish_session_analysis(other, service.snapshot)
        entered = asyncio.Event()
        other_entered = asyncio.Event()
        release = asyncio.Event()
        other_release = asyncio.Event()
        original_process = upload_routes._process_capture

        async def process(
            staged: StagedCapture,
            *,
            normalized_hosts: tuple[str, ...],
            normalized_label: str,
            request: Request,
        ) -> ProcessedCapture:
            if request.state.owner_context == owner and phase == "processing":
                assert (owner, "retained") in state._active_uploads
                entered.set()
                await release.wait()
            return await original_process(
                staged,
                normalized_hosts=normalized_hosts,
                normalized_label=normalized_label,
                request=request,
            )

        monkeypatch.setattr(upload_routes, "_process_capture", process)
        boundary = "logout-upload-boundary"
        body = _streaming_multipart(boundary, _valid_definitions())

        async def stream(*, other_owner: bool) -> AsyncIterator[bytes]:
            if other_owner:
                other_entered.set()
                await other_release.wait()
            elif phase == "receipt":
                entered.set()
                await release.wait()
            yield body

        def headers(session_id: str) -> dict[str, str]:
            return {
                "Authorization": _BASIC_CREDENTIAL,
                "X-Session-Token": session_id,
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            }

        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=wrapped),
            base_url="https://testserver",
        ) as client:
            upload = asyncio.create_task(
                client.post(
                    "/api/definitions/upload",
                    headers=headers(token.upper()),
                    content=stream(other_owner=False),
                )
            )
            other_upload = asyncio.create_task(
                client.post(
                    "/api/definitions/upload",
                    headers=headers(other_token),
                    content=stream(other_owner=True),
                )
            )
            try:
                async with asyncio.timeout(5):
                    await entered.wait()
                    await other_entered.wait()
                    assert upload in registry._tasks[owner]
                    assert other_upload in registry._tasks[other]
                    logged_out = await client.post("/api/auth/logout", headers=headers(token))
                    assert logged_out.status_code == 204
                    assert upload.cancelled()
                    assert not other_upload.done()
                    assert (owner, "retained") not in state._active_uploads
                    assert state.list_session_analyses(owner) == ()
                    assert [entry.snapshot for entry in state.list_session_analyses(other)] == [
                        service.snapshot,
                    ]
                    release.set()
                    other_release.set()
                    accepted = await other_upload
                    assert accepted.status_code == 201
                    assert state.list_session_analyses(owner) == ()
                    assert {
                        entry.snapshot.analysis_id for entry in state.list_session_analyses(other)
                    } == {
                        service.snapshot.analysis_id,
                        "retained",
                    }
                    assert registry._tasks == {}
                    assert not registry.is_closing(owner)
            finally:
                release.set()
                other_release.set()
                for task in (upload, other_upload):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(upload, other_upload, return_exceptions=True)
                assert runtime.basic_authenticator is not None
                await runtime.basic_authenticator.close()

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["scc", "cycle"])
@pytest.mark.parametrize("logout", [False, True], ids=["disconnect-and-retry", "logout"])
def test_cancelled_admission_drains_before_retry_or_owner_cleanup(
    service: Service,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    logout: bool,
) -> None:
    async def run() -> None:
        registry = OwnerActivityRegistry()
        gate = ThreadGate()
        repository = service.scheduler.result_repository
        original_prepare = repository.prepare

        def prepare(run_id: str) -> object:
            gate.wait()
            return original_prepare(run_id)

        monkeypatch.setattr(repository, "prepare", prepare)
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=service.app),
            base_url="http://testserver",
        ) as client:
            headers = _headers(service)

            async def request() -> httpx2.Response:
                async with registry.request(service.owner):
                    return await client.post(
                        f"/api/{operation}-runs",
                        json=_payload(service),
                        headers=headers,
                    )

            task = asyncio.create_task(request())
            cleanup = None
            try:
                await asyncio.wait_for(gate.entered.wait(), 5)
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done()
                assert registry._tasks.get(service.owner)
                if logout:
                    cleanup = asyncio.create_task(
                        cleanup_owner_session(
                            service.owner,
                            activity_registry=registry,
                            scheduler=service.scheduler,
                        )
                    )
                    await asyncio.sleep(0)
                    assert registry.is_closing(service.owner)
                    assert not cleanup.done()
                assert (await client.get("/api/health")).status_code == 200
            finally:
                gate.release.set()
                await asyncio.gather(task, return_exceptions=True)
                if cleanup is not None:
                    await cleanup

            assert task.cancelled()
            assert not registry._tasks.get(service.owner)
            if logout:
                _assert_owner_clean(service)
            else:
                assert service.scheduler.run_count() == 1
                run_id = next(iter(service.scheduler._runs))
                replay = await client.post(
                    f"/api/{operation}-runs",
                    json=_payload(service),
                    headers=headers,
                )
                assert replay.status_code in {200, 202}
                assert replay.json()["runId"] == run_id
                assert service.scheduler.run_count() == 1

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["scc", "cycle"])
def test_cancel_logout_race_retains_capacity_until_exit_and_promotes_other_owner(
    service: Service,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: str,
) -> None:
    marker = tmp_path / "ready"
    restore_worker = _install_stubborn_worker(operation, marker, monkeypatch)
    other = replace(service, owner=OwnerContext("anonymous", str(uuid4())))
    state.publish_session_analysis(other.owner, service.snapshot)

    async def run() -> None:
        registry = OwnerActivityRegistry()
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=service.app),
            base_url="http://testserver",
        ) as client:
            started = await client.post(
                f"/api/{operation}-runs",
                json=_payload(service),
                headers=_headers(service),
            )
            run_id = started.json()["runId"]
            await _wait_for_marker(marker)
            restore_worker()
            queued = await client.post(
                f"/api/{operation}-runs",
                json=_payload(other),
                headers=_headers(other),
            )
            assert queued.status_code == 202
            assert queued.json()["status"] == "queued"
            queued_id = queued.json()["runId"]
            gate = ThreadGate()
            handle = service.scheduler._handles[run_id]
            gate.observe_join(handle.process, monkeypatch)

            async def cancel() -> httpx2.Response:
                async with registry.request(service.owner):
                    return await client.delete(
                        f"/api/{operation}-runs/{run_id}",
                        params={"snapshot_revision": service.snapshot.snapshot_revision},
                        headers=_headers(service),
                    )

            task = asyncio.create_task(cancel())
            cleanup = None
            try:
                await asyncio.wait_for(gate.entered.wait(), 5)
                task.cancel()
                await asyncio.sleep(0)
                cleanup = asyncio.create_task(
                    cleanup_owner_session(
                        service.owner,
                        activity_registry=registry,
                        scheduler=service.scheduler,
                    )
                )
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done() and not cleanup.done()
                assert service.scheduler.compute_slots.leased == 1
                assert service.scheduler._runs[queued_id].status.value == "queued"
                assert registry.is_closing(service.owner)
                assert (await client.get("/api/health")).status_code == 200
            finally:
                gate.release.set()
                await asyncio.gather(task, return_exceptions=True)
                if cleanup is not None:
                    await cleanup
            assert task.cancelled()
            assert not handle.process.is_alive()
            await _wait_succeeded(client, other, operation, queued_id)
            assert service.scheduler.run_count() == 1
            assert service.scheduler.compute_slots.leased == 0
            assert service.scheduler.active_process_count() == 0
            assert state.list_session_analyses(service.owner) == ()
            assert len(state.list_session_analyses(other.owner)) == 1

    asyncio.run(run())


@pytest.mark.parametrize("stage", ["reservation", "capacity"])
def test_cancelled_upload_acquisition_releases_every_claim(
    service: Service,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    async def run() -> None:
        registry = OwnerActivityRegistry()
        gate = ThreadGate()
        if stage == "reservation":
            original_reserve = upload_routes._reserve_upload

            def reserve(*args: object) -> object:
                gate.wait()
                return original_reserve(*args)

            monkeypatch.setattr(upload_routes, "_reserve_upload", reserve)
        else:
            original_acquire = service.scheduler.try_acquire_immediate

            def acquire() -> object:
                gate.wait()
                return original_acquire()

            monkeypatch.setattr(service.scheduler, "try_acquire_immediate", acquire)

        def unexpected_child(*_args: object) -> None:
            pytest.fail("Cancelled acquisition must not start an upload parser child")

        monkeypatch.setattr(service.scheduler, "start_immediate_process", unexpected_child)
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=service.app),
            base_url="http://testserver",
        ) as client:

            async def request() -> httpx2.Response:
                async with registry.request(service.owner):
                    return await client.post(
                        "/api/definitions/upload",
                        data={"analysis_id": "cancelled-upload"},
                        files={
                            "definitions_file": (
                                "capture.json",
                                b'{"vhosts":[]}',
                                "application/json",
                            )
                        },
                        headers=_headers(service),
                    )

            task = asyncio.create_task(request())
            try:
                await asyncio.wait_for(gate.entered.wait(), 5)
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done()
                assert registry._tasks.get(service.owner)
                assert (await client.get("/api/health")).status_code == 200
            finally:
                gate.release.set()
                await asyncio.gather(task, return_exceptions=True)
            assert task.cancelled()
            assert not state._active_uploads
            assert service.scheduler.compute_slots.leased == 0
            assert service.scheduler.active_process_count() == 0
            assert (
                state.resolve_analysis(
                    AnalysisSource.SESSION,
                    service.snapshot.analysis_id,
                    service.owner,
                )
                is service.snapshot
            )

    asyncio.run(run())


def _ignore_sigterm(
    marker: Path,
    _topology: ClusterTopology,
    _context: ResultEnvelopeContext,
) -> bytes:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    marker.touch()
    while True:
        time.sleep(0.1)


def _install_stubborn_worker(
    operation: str,
    marker: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[], None]:
    module = scc_routes if operation == "scc" else cycle_routes
    worker_name = "execute_cyclic_scc" if operation == "scc" else "execute_structural_cycles"
    original_worker = getattr(module, worker_name)
    monkeypatch.setattr(module, worker_name, partial(_ignore_sigterm, marker))

    def restore() -> None:
        monkeypatch.setattr(module, worker_name, original_worker)

    return restore


async def _wait_for_marker(marker: Path) -> None:
    async with asyncio.timeout(5):
        while not marker.exists():
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("operation", ["scc", "cycle"])
def test_hard_cancellation_leaves_health_responsive_and_reaps_child(
    service: Service,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: str,
) -> None:
    marker = tmp_path / "ready"
    _install_stubborn_worker(operation, marker, monkeypatch)

    async def run() -> None:
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=service.app),
            base_url="http://testserver",
        ) as client:
            started = await client.post(
                f"/api/{operation}-runs",
                json=_payload(service),
                headers=_headers(service),
            )
            assert started.status_code == 202
            run_id = started.json()["runId"]
            await _wait_for_marker(marker)
            handle = service.scheduler._handles[run_id]
            gate = ThreadGate()
            gate.observe_join(handle.process, monkeypatch)
            cancellation_started = time.monotonic()
            response = await _health_while_blocked(
                client,
                client.delete(
                    f"/api/{operation}-runs/{run_id}",
                    params={"snapshot_revision": service.snapshot.snapshot_revision},
                    headers=_headers(service),
                ),
                gate,
            )
            assert response.status_code == 200
            assert time.monotonic() - cancellation_started < 1.0
            assert response.json()["status"] == "cancelled"
            assert not handle.process.is_alive()
            assert handle.process.exitcode == -signal.SIGKILL
            assert service.scheduler.compute_slots.leased == 0
            assert service.scheduler.active_process_count() == 0
            assert (
                state._session_store[(service.owner, service.snapshot.analysis_id)].active_job_pins
                == 0
            )

    asyncio.run(run())
