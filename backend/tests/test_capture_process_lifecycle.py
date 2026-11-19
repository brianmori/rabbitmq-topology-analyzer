import asyncio
import time
from contextlib import suppress
from datetime import timedelta
from threading import Event, get_ident
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from rabbitmq_management_sdk.topology import ClusterTopology

from core.session import OwnerContext
from core.state import AnalysisSource
from definitions.processing import (
    CaptureProcessingError,
    CaptureRequestDisconnectedError,
    CaptureValidationError,
    _CaptureProcessRequest,
    process_staged_capture,
)
from definitions.staging import StagedCapture
from jobs.config import JobSettings
from jobs.models import (
    JobStart,
    OperationKind,
    ResultEnvelopeContext,
    RunStatus,
    encode_result_envelope,
)
from jobs.results import ResultRepository
from jobs.scheduler import ComputeSlotLease, JobScheduler

if TYPE_CHECKING:
    from pathlib import Path


_SLOW_UNPICKLE_SECONDS = 0.5
_UNPICKLE_STARTED = Event()
_UNPICKLE_STARTED_AT: float | None = None


def _slow_validation_message() -> tuple[str]:
    global _UNPICKLE_STARTED_AT  # noqa: PLW0603 - process-transfer test probe
    _UNPICKLE_STARTED_AT = time.perf_counter()
    _UNPICKLE_STARTED.set()
    time.sleep(_SLOW_UNPICKLE_SECONDS)
    return ("validation",)


class _SlowPayload:
    def __reduce__(self) -> tuple[object, tuple[()]]:
        return (_slow_validation_message, ())


def _send_slow_payload(
    _request: _CaptureProcessRequest,
    connection: object,
) -> None:
    connection.send(_SlowPayload())
    connection.close()


def _quick_job_worker(
    _topology: ClusterTopology,
    context: ResultEnvelopeContext,
) -> bytes:
    return encode_result_envelope(context, {"ok": True})


def _blocking_child(request: _CaptureProcessRequest, connection: object) -> None:
    (request.definitions_path.parent / "child-started").touch()
    try:
        while True:
            time.sleep(0.1)
    finally:
        connection.close()


class _ClosingConnection:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _StartFailingProcess:
    pid: int | None = None

    def __init__(self) -> None:
        self.start_calls = 0

    def start(self) -> None:
        self.start_calls += 1
        raise RuntimeError("spawn failed")

    def is_alive(self) -> bool:
        return False


class _StartFailingContext:
    def __init__(self) -> None:
        self.parent_connection = _ClosingConnection()
        self.child_connection = _ClosingConnection()
        self.process = _StartFailingProcess()

    def Pipe(self, *, duplex: bool) -> tuple[_ClosingConnection, _ClosingConnection]:
        assert duplex is False
        return self.parent_connection, self.child_connection

    def Process(self, **_kwargs: object) -> _StartFailingProcess:
        return self.process


class _NeverStartedProcess:
    pid: int | None = None

    def __init__(self) -> None:
        self.start_called = False

    def start(self) -> None:
        self.start_called = True


def _scheduler(root: Path) -> JobScheduler:
    settings = JobSettings(
        worker_count=1,
        queue_capacity=1,
        heartbeat_ttl=timedelta(seconds=10),
        result_ttl=timedelta(seconds=10),
        results_dir=root,
        results_max_bytes=10_000,
    )
    return JobScheduler(settings, ResultRepository(root, settings.results_max_bytes))


def _staged_capture(root: Path) -> StagedCapture:
    root.mkdir()
    definitions_path = root / "definitions.json"
    definitions_path.write_text("{}")
    return StagedCapture(
        definitions_path=definitions_path,
        queues_path=None,
        exchanges_path=None,
        total_bytes=definitions_path.stat().st_size,
    )


async def _connected() -> bool:
    return False


def test_failed_process_start_releases_immediate_capacity(tmp_path: Path) -> None:
    scheduler = _scheduler(tmp_path / "results")
    staged = _staged_capture(tmp_path / "staging")
    context = _StartFailingContext()

    async def run() -> None:
        with pytest.raises(CaptureProcessingError):
            await process_staged_capture(
                staged,
                in_cluster_amqp_hosts=(),
                cluster_label="Test",
                scheduler=scheduler,
                is_disconnected=_connected,
                process_context=context,
            )

    asyncio.run(run())

    assert context.process.start_calls == 1
    assert context.parent_connection.closed is True
    assert context.child_connection.closed is True
    assert scheduler.compute_slots.leased == 0
    scheduler.shutdown()


def test_result_receive_and_topology_unpickle_remain_off_event_loop(tmp_path: Path) -> None:
    global _UNPICKLE_STARTED_AT  # noqa: PLW0603 - process-transfer test probe
    _UNPICKLE_STARTED.clear()
    _UNPICKLE_STARTED_AT = None
    scheduler = _scheduler(tmp_path / "results")
    staged = _staged_capture(tmp_path / "staging")

    async def run() -> None:
        task = asyncio.create_task(
            process_staged_capture(
                staged,
                in_cluster_amqp_hosts=(),
                cluster_label="Test",
                scheduler=scheduler,
                is_disconnected=_connected,
                child_target=_send_slow_payload,
            ),
        )
        try:
            assert await asyncio.to_thread(_UNPICKLE_STARTED.wait, 5)
            assert _UNPICKLE_STARTED_AT is not None
            assert time.perf_counter() - _UNPICKLE_STARTED_AT < 0.35
            with pytest.raises(CaptureValidationError):
                await task
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    asyncio.run(run())

    assert scheduler.compute_slots.leased == 0
    scheduler.shutdown()


def test_disconnect_during_result_decode_wins_and_releases_capacity(tmp_path: Path) -> None:
    _UNPICKLE_STARTED.clear()
    scheduler = _scheduler(tmp_path / "results")
    staged = _staged_capture(tmp_path / "staging")

    async def disconnected() -> bool:
        return _UNPICKLE_STARTED.is_set()

    async def run() -> None:
        with pytest.raises(CaptureRequestDisconnectedError):
            await process_staged_capture(
                staged,
                in_cluster_amqp_hosts=(),
                cluster_label="Test",
                scheduler=scheduler,
                is_disconnected=disconnected,
                child_target=_send_slow_payload,
            )

    asyncio.run(run())

    assert scheduler.compute_slots.leased == 0
    assert scheduler.active_process_count() == 0
    scheduler.shutdown()


def test_queued_job_promotion_runs_off_the_request_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = _scheduler(tmp_path / "results")
    staging_root = tmp_path / "staging"
    staged = _staged_capture(staging_root)
    release_threads: list[int] = []
    original_release = scheduler.release_immediate

    def observed_release(lease: ComputeSlotLease) -> None:
        release_threads.append(get_ident())
        original_release(lease)

    monkeypatch.setattr(scheduler, "release_immediate", observed_release)

    async def run() -> tuple[str, OwnerContext, int]:
        disconnected = Event()

        async def is_disconnected() -> bool:
            return disconnected.is_set()

        parser = asyncio.create_task(
            process_staged_capture(
                staged,
                in_cluster_amqp_hosts=(),
                cluster_label="Test",
                scheduler=scheduler,
                is_disconnected=is_disconnected,
                child_target=_blocking_child,
            ),
        )
        for _attempt in range(500):
            if (staging_root / "child-started").exists():
                break
            await asyncio.sleep(0.01)
        assert (staging_root / "child-started").exists()

        owner = OwnerContext("principal", str(uuid4()))
        topology = ClusterTopology(
            exchanges=frozenset(),
            queues=frozenset(),
            shovels=frozenset(),
            edges=frozenset(),
            cluster_id="test-cluster",
            cluster_name="Test cluster",
        )
        admitted = scheduler.admit(
            start=JobStart(
                owner_context=owner,
                operation=OperationKind.CYCLIC_SCC,
                source=AnalysisSource.STATIC,
                analysis_id="static-analysis",
                snapshot_revision="sha256:" + "a" * 64,
                topology=topology,
                parameters={},
                worker=_quick_job_worker,
            ),
            canonical_payload=b"queued",
            idempotency_key=str(uuid4()),
        ).run
        assert admitted.status is RunStatus.QUEUED
        request_thread = get_ident()
        disconnected.set()
        with pytest.raises(CaptureRequestDisconnectedError):
            await parser
        return admitted.run_id, owner, request_thread

    run_id, owner, request_thread = asyncio.run(run())

    assert len(release_threads) == 1
    assert release_threads[0] != request_thread
    deadline = time.monotonic() + 5
    run = scheduler.get_owned(run_id, owner)
    while run is not None and not run.status.is_terminal and time.monotonic() < deadline:
        time.sleep(0.02)
        run = scheduler.get_owned(run_id, owner)
    assert run is not None
    assert run.status is RunStatus.SUCCEEDED
    scheduler.shutdown()


def test_shutdown_terminates_registered_immediate_parser(tmp_path: Path) -> None:
    scheduler = _scheduler(tmp_path / "results")
    staging_root = tmp_path / "staging"
    staged = _staged_capture(staging_root)

    async def run() -> None:
        task = asyncio.create_task(
            process_staged_capture(
                staged,
                in_cluster_amqp_hosts=(),
                cluster_label="Test",
                scheduler=scheduler,
                is_disconnected=_connected,
                child_target=_blocking_child,
            ),
        )
        try:
            for _attempt in range(500):
                if (staging_root / "child-started").exists():
                    break
                await asyncio.sleep(0.01)
            assert (staging_root / "child-started").exists()
            assert scheduler.active_process_count() == 1

            await asyncio.to_thread(scheduler.shutdown)

            assert scheduler.active_process_count() == 0
            assert scheduler.compute_slots.leased == 0
            with pytest.raises(CaptureProcessingError):
                await asyncio.wait_for(task, timeout=2)
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            scheduler.shutdown()

    asyncio.run(run())


def test_shutdown_winning_start_race_prevents_immediate_process_start(tmp_path: Path) -> None:
    scheduler = _scheduler(tmp_path / "results")
    lease = scheduler.try_acquire_immediate()
    assert lease is not None
    process = _NeverStartedProcess()

    scheduler.shutdown()

    assert scheduler.start_immediate_process(lease, process) is False
    assert process.start_called is False
    assert scheduler.compute_slots.leased == 0
