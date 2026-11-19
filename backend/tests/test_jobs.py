import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Event
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from rabbitmq_management_sdk.topology import ClusterTopology

from core.session import OwnerContext
from core.state import AnalysisSource
from jobs.config import JobSettings, load_job_settings
from jobs.models import (
    AnalysisRun,
    IdempotencyConflictError,
    JobQueueFullError,
    JobStart,
    OperationKind,
    OwnerActiveRunError,
    ResultEnvelopeContext,
    RunStatus,
    encode_result_envelope,
)
from jobs.results import ResultRepository
from jobs.scheduler import ComputeSlotPool, JobScheduler

if TYPE_CHECKING:
    from multiprocessing.connection import Connection
    from os import stat_result

    from jobs.models import WorkerFunction
    from jobs.results import ResultWriteSpec


def _empty_topology() -> ClusterTopology:
    return ClusterTopology(
        exchanges=frozenset(),
        queues=frozenset(),
        shovels=frozenset(),
        edges=frozenset(),
        cluster_id="test-cluster",
        cluster_name="Test cluster",
    )


def _quick_worker(
    _topology: ClusterTopology,
    context: ResultEnvelopeContext,
) -> bytes:
    return encode_result_envelope(context, {"ok": True})


def _slow_worker(
    _topology: ClusterTopology,
    context: ResultEnvelopeContext,
) -> bytes:
    time.sleep(10)
    return encode_result_envelope(context, {"ok": True})


def _memory_error_worker(
    _topology: ClusterTopology,
    _context: ResultEnvelopeContext,
) -> bytes:
    raise MemoryError


def _exit_with_partial_result(
    worker: WorkerFunction,
    topology: ClusterTopology,
    context: ResultEnvelopeContext,
    write_spec: ResultWriteSpec,
    connection: Connection,
) -> None:
    try:
        payload = worker(topology, context)
        connection.send(("reserve", len(payload)))
        assert connection.recv() == ("reservation", True)
        write_spec.temporary_path.write_bytes(payload)
        # Simulate a child exiting after writing, before the atomic rename.
    finally:
        connection.close()


def _settings(
    root: Path,
    *,
    queue_capacity: int = 1,
    heartbeat_seconds: float = 5,
    result_seconds: float = 5,
    result_bytes: int = 10_000,
) -> JobSettings:
    return JobSettings(
        worker_count=1,
        queue_capacity=queue_capacity,
        heartbeat_ttl=timedelta(seconds=heartbeat_seconds),
        result_ttl=timedelta(seconds=result_seconds),
        results_dir=root,
        results_max_bytes=result_bytes,
    )


def _start(owner: OwnerContext) -> JobStart:
    return JobStart(
        owner_context=owner,
        operation=OperationKind.CYCLIC_SCC,
        source=AnalysisSource.STATIC,
        analysis_id="static-analysis",
        snapshot_revision="sha256:" + "a" * 64,
        topology=_empty_topology(),
        parameters={},
        worker=_quick_worker,
    )


def _admit(
    scheduler: JobScheduler,
    owner: OwnerContext,
    *,
    key: str | None = None,
    payload: bytes = b"payload-a",
) -> str:
    return scheduler.admit(
        start=_start(owner),
        canonical_payload=payload,
        idempotency_key=key or str(uuid4()),
    ).run.run_id


def _wait_for_terminal(
    scheduler: JobScheduler,
    run_id: str,
    owner: OwnerContext,
) -> AnalysisRun:
    deadline = time.monotonic() + 5
    run = scheduler.get_owned(run_id, owner)
    while run is not None and not run.status.is_terminal and time.monotonic() < deadline:
        time.sleep(0.02)
        run = scheduler.get_owned(run_id, owner)
    assert run is not None
    assert run.status.is_terminal
    return run


def _publish_result(repository: ResultRepository, run_id: str, payload: bytes) -> ResultWriteSpec:
    spec = repository.prepare(run_id)
    assert repository.reserve(run_id, len(payload))
    spec.temporary_path.write_bytes(payload)
    spec.temporary_path.replace(spec.committed_path)
    repository.commit(run_id)
    return spec


def _fail_unlink_once(monkeypatch: pytest.MonkeyPatch, target: Path) -> list[Path]:
    original_unlink = Path.unlink
    failures: list[Path] = []

    def unlink(path: Path, *, missing_ok: bool = False) -> None:
        if path == target and not failures:
            failures.append(path)
            raise OSError("transient unlink failure")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    return failures


def _fail_stat_once(monkeypatch: pytest.MonkeyPatch, target: Path) -> list[Path]:
    original_stat = Path.stat
    failures: list[Path] = []

    def stat(path: Path, *, follow_symlinks: bool = True) -> stat_result:
        if path == target and not failures:
            failures.append(path)
            raise OSError("transient stat failure")
        return original_stat(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", stat)
    return failures


def _assert_safe_cleanup_logs(
    caplog: pytest.LogCaptureFixture,
    root: Path,
    messages: list[str],
) -> None:
    records = [record for record in caplog.records if record.name == "jobs.scheduler"]
    assert [record.getMessage() for record in records] == messages
    assert all(record.exc_info is None and record.stack_info is None for record in records)
    assert all(value not in caplog.text for value in ("guest:secret", "broker.internal", str(root)))


def test_job_configuration_defaults_and_bounds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = (
        "RABBITMQ_ANALYSIS_WORKERS",
        "RABBITMQ_ANALYSIS_QUEUE_CAPACITY",
        "RABBITMQ_ANALYSIS_HEARTBEAT_TTL_SECONDS",
        "RABBITMQ_ANALYSIS_RESULT_TTL_SECONDS",
        "RABBITMQ_ANALYSIS_RESULTS_DIR",
        "RABBITMQ_ANALYSIS_RESULTS_MAX_BYTES",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)

    defaults = load_job_settings()

    assert defaults.worker_count > 0
    assert defaults.queue_capacity == defaults.worker_count
    assert defaults.heartbeat_ttl == timedelta(seconds=120)
    assert defaults.result_ttl == timedelta(hours=4)
    assert defaults.results_max_bytes == 1_073_741_824

    monkeypatch.setenv("RABBITMQ_ANALYSIS_WORKERS", "2")
    monkeypatch.setenv("RABBITMQ_ANALYSIS_QUEUE_CAPACITY", "0")
    monkeypatch.setenv("RABBITMQ_ANALYSIS_RESULTS_DIR", str(tmp_path))
    configured = load_job_settings()
    assert configured.worker_count == 2
    assert configured.queue_capacity == 0
    assert configured.results_dir == tmp_path

    monkeypatch.setenv("RABBITMQ_ANALYSIS_RESULT_TTL_SECONDS", "14401")
    with pytest.raises(ValueError, match="at most 14400"):
        load_job_settings()


def test_admission_is_owner_scoped_bounded_and_exactly_idempotent(tmp_path: Path) -> None:
    slots = ComputeSlotPool(1)
    occupied = slots.try_acquire()
    assert occupied is not None
    repository = ResultRepository(tmp_path, 10_000)
    scheduler = JobScheduler(_settings(tmp_path), repository, compute_slots=slots)
    owner = OwnerContext("principal", str(uuid4()))
    key = str(uuid4())

    first = scheduler.admit(
        start=_start(owner),
        canonical_payload=b"payload-a",
        idempotency_key=key,
    )
    replay = scheduler.admit(
        start=_start(owner),
        canonical_payload=b"payload-a",
        idempotency_key=key,
    )

    assert first.run.status is RunStatus.QUEUED
    assert replay.replayed is True
    assert replay.run.run_id == first.run.run_id
    assert scheduler.run_count() == 1
    with pytest.raises(IdempotencyConflictError):
        scheduler.admit(
            start=_start(owner),
            canonical_payload=b"payload-b",
            idempotency_key=key,
        )
    with pytest.raises(OwnerActiveRunError):
        _admit(scheduler, owner)
    with pytest.raises(JobQueueFullError):
        _admit(scheduler, OwnerContext("other", str(uuid4())))
    assert scheduler.run_count() == 1

    cancelled = scheduler.cancel_owned(first.run.run_id, owner)
    assert cancelled is not None
    assert cancelled.status is RunStatus.CANCELLED
    occupied.release()
    scheduler.shutdown()


def test_concurrent_queue_admission_never_overbooks_capacity(tmp_path: Path) -> None:
    slots = ComputeSlotPool(1)
    occupied = slots.try_acquire()
    assert occupied is not None
    scheduler = JobScheduler(
        _settings(tmp_path, queue_capacity=3),
        ResultRepository(tmp_path, 10_000),
        compute_slots=slots,
    )
    owners = [OwnerContext(f"principal-{index}", str(uuid4())) for index in range(12)]

    def attempt(owner: OwnerContext) -> bool:
        try:
            _admit(scheduler, owner)
        except JobQueueFullError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=len(owners)) as executor:
        accepted = list(executor.map(attempt, owners))

    assert sum(accepted) == 3
    assert scheduler.run_count() == 3
    scheduler.shutdown()
    occupied.release()


def test_releasing_immediate_upload_capacity_promotes_one_waiting_job(tmp_path: Path) -> None:
    scheduler = JobScheduler(
        _settings(tmp_path, queue_capacity=1),
        ResultRepository(tmp_path, 10_000),
    )
    occupied = scheduler.try_acquire_immediate()
    assert occupied is not None
    owner = OwnerContext("principal", str(uuid4()))

    run_id = _admit(scheduler, owner)

    queued = scheduler.get_owned(run_id, owner)
    assert queued is not None
    assert queued.status is RunStatus.QUEUED
    scheduler.release_immediate(occupied)
    terminal = _wait_for_terminal(scheduler, run_id, owner)
    assert terminal.status is RunStatus.SUCCEEDED
    assert scheduler.compute_slots.leased == 0
    scheduler.shutdown()


def test_result_repository_reserves_atomically_and_preserves_published_results(
    tmp_path: Path,
) -> None:
    repository = ResultRepository(tmp_path, 10)
    first = repository.prepare("first")
    assert repository.reserve("first", 6) is True
    first.temporary_path.write_bytes(b"result")
    first.temporary_path.replace(first.committed_path)
    repository.commit("first")

    repository.prepare("second")
    assert repository.reserve("second", 5) is False
    repository.abort("second")

    assert repository.read("first") == b"result"
    assert repository.accounted_bytes == 6
    assert first.committed_path.exists()


@pytest.mark.parametrize("file_state", ["temporary", "renamed", "both"])
@pytest.mark.parametrize("cleanup_method", ["abort", "delete"])
def test_result_cleanup_retains_paths_and_reservation_until_unlinks_succeed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    file_state: str,
    cleanup_method: str,
) -> None:
    repository = ResultRepository(tmp_path, 20)
    spec = repository.prepare("partial")
    reserved_size = 10 if file_state == "both" else 5
    assert repository.reserve("partial", reserved_size)
    spec.temporary_path.write_bytes(b"bytes")
    if file_state == "renamed":
        spec.temporary_path.replace(spec.committed_path)
    elif file_state == "both":
        spec.committed_path.write_bytes(b"bytes")
    target = spec.temporary_path if file_state == "temporary" else spec.committed_path
    failures = _fail_unlink_once(monkeypatch, target)
    cleanup = getattr(repository, cleanup_method)

    with pytest.raises(OSError, match="transient unlink failure"):
        cleanup("partial")

    assert failures == [target]
    assert set(tmp_path.iterdir()) == {target}
    assert target.read_bytes() == b"bytes"
    assert repository._specs == {"partial": spec}
    assert repository._reservations == {"partial": reserved_size}
    assert repository._committed == {}
    assert repository.accounted_bytes == reserved_size
    with pytest.raises(ValueError, match="already exist"):
        repository.prepare("partial")

    cleanup("partial")
    cleanup("partial")

    assert set(tmp_path.iterdir()) == set()
    assert repository._specs == {}
    assert repository._reservations == {}
    assert repository._committed == {}
    assert repository.accounted_bytes == 0


def test_committed_delete_failure_keeps_quota_during_concurrent_other_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = ResultRepository(tmp_path, 14)
    published = _publish_result(repository, "published", b"keep")
    removed = _publish_result(repository, "removed", b"bytes")
    failures = _fail_unlink_once(monkeypatch, removed.committed_path)

    with pytest.raises(OSError, match="transient unlink failure"):
        repository.delete("removed")

    assert failures == [removed.committed_path]
    assert set(tmp_path.iterdir()) == {published.committed_path, removed.committed_path}
    assert repository.read("removed") == b"bytes"
    assert repository._specs == {}
    assert repository._reservations == {}
    assert repository._committed == {"published": 4, "removed": 5}
    assert repository.accounted_bytes == 9

    other = repository.prepare("other")
    assert repository.reserve("other", 5)
    other.temporary_path.write_bytes(b"other")
    repository.prepare("over-quota")
    assert not repository.reserve("over-quota", 1)
    repository.abort("over-quota")
    assert repository.accounted_bytes == 14
    barrier = Barrier(2)

    def retry_delete() -> None:
        barrier.wait(timeout=5)
        repository.delete("removed")

    def publish_other() -> None:
        barrier.wait(timeout=5)
        other.temporary_path.replace(other.committed_path)
        repository.commit("other")

    with ThreadPoolExecutor(max_workers=2) as executor:
        deletion = executor.submit(retry_delete)
        publication = executor.submit(publish_other)
        deletion.result(timeout=5)
        publication.result(timeout=5)
    repository.abort("removed")

    assert set(tmp_path.iterdir()) == {published.committed_path, other.committed_path}
    assert repository.read("published") == b"keep"
    assert repository.read("other") == b"other"
    assert repository._specs == {}
    assert repository._reservations == {}
    assert repository._committed == {"published": 4, "other": 5}
    assert repository.accounted_bytes == 9
    repository.delete("published")
    repository.delete("other")
    assert set(tmp_path.iterdir()) == set()
    assert repository.accounted_bytes == 0


@pytest.mark.parametrize("retry_commit", [False, True])
def test_commit_stat_failure_keeps_reservation_for_retry_or_abort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    retry_commit: bool,
) -> None:
    repository = ResultRepository(tmp_path, 10)
    spec = repository.prepare("result")
    assert repository.reserve("result", 5)
    spec.temporary_path.write_bytes(b"bytes")
    spec.temporary_path.replace(spec.committed_path)
    failures = _fail_stat_once(monkeypatch, spec.committed_path)

    with pytest.raises(OSError):
        repository.commit("result")

    assert failures == [spec.committed_path]
    assert set(tmp_path.iterdir()) == {spec.committed_path}
    assert spec.committed_path.read_bytes() == b"bytes"
    assert repository._specs == {"result": spec}
    assert repository._reservations == {"result": 5}
    assert repository._committed == {}
    assert repository.accounted_bytes == 5
    if retry_commit:
        repository.commit("result")
        assert repository.read("result") == b"bytes"
        assert repository._specs == {}
        assert repository._reservations == {}
        assert repository._committed == {"result": 5}
        assert repository.accounted_bytes == 5
        with pytest.raises(KeyError):
            repository.commit("result")
    repository.abort("result")
    repository.abort("result")
    assert set(tmp_path.iterdir()) == set()
    assert repository._specs == {}
    assert repository._reservations == {}
    assert repository._committed == {}
    assert repository.accounted_bytes == 0


@pytest.mark.parametrize("published", [False, True])
def test_commit_validation_failure_can_retry_cleanup_without_losing_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    published: bool,
) -> None:
    repository = ResultRepository(tmp_path, 10)
    spec = repository.prepare("invalid")
    assert repository.reserve("invalid", 5)
    spec.temporary_path.write_bytes(b"bad")
    target = spec.temporary_path
    if published:
        spec.temporary_path.replace(spec.committed_path)
        target = spec.committed_path
    failures = _fail_unlink_once(monkeypatch, target)

    with pytest.raises(OSError):
        repository.commit("invalid")

    assert set(tmp_path.iterdir()) == {target}
    assert target.read_bytes() == b"bad"
    assert repository._specs == {"invalid": spec}
    assert repository._reservations == {"invalid": 5}
    assert repository._committed == {}
    assert repository.accounted_bytes == 5
    with pytest.raises(OSError, match="transient unlink failure"):
        repository.abort("invalid")
    assert failures == [target]
    assert set(tmp_path.iterdir()) == {target}
    assert target.read_bytes() == b"bad"
    assert repository._specs == {"invalid": spec}
    assert repository._reservations == {"invalid": 5}
    assert repository._committed == {}
    assert repository.accounted_bytes == 5

    repository.abort("invalid")
    repository.abort("invalid")

    assert set(tmp_path.iterdir()) == set()
    assert repository._specs == {}
    assert repository._reservations == {}
    assert repository._committed == {}
    assert repository.accounted_bytes == 0


def test_result_repository_reconciles_known_commits_and_removes_orphans(tmp_path: Path) -> None:
    (tmp_path / "known.json").write_bytes(b"known")
    (tmp_path / "orphan.json").write_bytes(b"orphan")
    (tmp_path / ".partial.tmp").write_bytes(b"partial")
    repository = ResultRepository(tmp_path, 100)

    repository.reconcile(frozenset({"known"}))

    assert repository.accounted_bytes == 5
    assert repository.read("known") == b"known"
    assert not (tmp_path / "orphan.json").exists()
    assert not (tmp_path / ".partial.tmp").exists()


def test_process_restart_drops_run_cache_idempotency_and_startup_orphans(tmp_path: Path) -> None:
    owner = OwnerContext("principal", str(uuid4()))
    idempotency_key = str(uuid4())
    original = JobScheduler(_settings(tmp_path), ResultRepository(tmp_path, 10_000))
    run_id = _admit(original, owner, key=idempotency_key)
    run = _wait_for_terminal(original, run_id, owner)
    result_path = tmp_path / f"{run_id}.json"
    assert run.status is RunStatus.SUCCEEDED
    assert result_path.exists()
    original.shutdown()

    # A new web process has no run/idempotency metadata. Its startup
    # reconciliation therefore treats every pre-restart result as an orphan.
    restarted_repository = ResultRepository(tmp_path, 10_000)
    restarted_repository.reconcile()
    restarted = JobScheduler(_settings(tmp_path), restarted_repository)

    assert restarted.get_owned(run_id, owner) is None
    assert restarted.run_count() == 0
    assert restarted._idempotency == {}
    assert restarted_repository.accounted_bytes == 0
    assert not result_path.exists()
    restarted.shutdown()


def test_spawned_worker_publishes_without_retaining_operation_object(tmp_path: Path) -> None:
    scheduler = JobScheduler(_settings(tmp_path), ResultRepository(tmp_path, 10_000))
    owner = OwnerContext("principal", str(uuid4()))
    run_id = _admit(scheduler, owner)

    run = _wait_for_terminal(scheduler, run_id, owner)

    assert run.status is RunStatus.SUCCEEDED
    envelope = json.loads(scheduler.result_repository.read(run_id))
    assert envelope["operation"] == "cyclic_scc"
    assert envelope["result"] == {"ok": True}
    assert scheduler.active_process_count() == 0
    scheduler.invalidate_analysis(
        owner,
        AnalysisSource.STATIC,
        "static-analysis",
        keep_snapshot_revision="sha256:" + "b" * 64,
    )
    assert scheduler.get_owned(run_id, owner) is None
    assert scheduler.result_repository.accounted_bytes == 0
    scheduler.shutdown()


def test_owner_cleanup_removes_runs_results_and_idempotency_for_exact_owner(
    tmp_path: Path,
) -> None:
    scheduler = JobScheduler(_settings(tmp_path), ResultRepository(tmp_path, 10_000))
    owner = OwnerContext("principal", str(uuid4()))
    other = OwnerContext("principal", str(uuid4()))
    owner_key = str(uuid4())
    other_key = str(uuid4())
    owner_run_id = _admit(scheduler, owner, key=owner_key)
    _wait_for_terminal(scheduler, owner_run_id, owner)
    other_run_id = _admit(scheduler, other, key=other_key)
    _wait_for_terminal(scheduler, other_run_id, other)

    assert scheduler.cleanup_owner(owner) == 1

    assert scheduler.get_owned(owner_run_id, owner) is None
    with pytest.raises(FileNotFoundError):
        scheduler.result_repository.read(owner_run_id)
    assert scheduler.get_owned(other_run_id, other) is not None
    assert scheduler.result_repository.read(other_run_id)

    # The exact idempotency key is reusable after its owner-scoped record was
    # purged, proving cleanup did not leave a hidden replay reference.
    replacement = _admit(scheduler, owner, key=owner_key)
    assert replacement != owner_run_id
    scheduler.cleanup_owner(owner)
    scheduler.cleanup_owner(other)
    scheduler.shutdown()


def test_quota_pressure_fails_only_new_run_with_safe_storage_code(tmp_path: Path) -> None:
    scheduler = JobScheduler(
        _settings(tmp_path, result_bytes=10),
        ResultRepository(tmp_path, 10),
    )
    owner = OwnerContext("principal", str(uuid4()))
    run_id = _admit(scheduler, owner)

    run = _wait_for_terminal(scheduler, run_id, owner)

    assert run.status is RunStatus.FAILED
    assert run.error is not None
    assert run.error.code == "result_storage_full"
    assert scheduler.result_repository.accounted_bytes == 0
    scheduler.shutdown()


def test_worker_memory_failure_is_safe_and_never_publishes_partial_output(tmp_path: Path) -> None:
    scheduler = JobScheduler(_settings(tmp_path), ResultRepository(tmp_path, 10_000))
    owner = OwnerContext("principal", str(uuid4()))
    start = _start(owner)
    memory_start = JobStart(
        owner_context=start.owner_context,
        operation=start.operation,
        source=start.source,
        analysis_id=start.analysis_id,
        snapshot_revision=start.snapshot_revision,
        topology=start.topology,
        parameters=start.parameters,
        worker=_memory_error_worker,
    )
    run_id = scheduler.admit(
        start=memory_start,
        canonical_payload=b"memory",
        idempotency_key=str(uuid4()),
    ).run.run_id

    run = _wait_for_terminal(scheduler, run_id, owner)

    assert run.status is RunStatus.FAILED
    assert run.error is not None
    assert run.error.code == "worker_out_of_memory"
    assert scheduler.result_repository.accounted_bytes == 0
    scheduler.shutdown()


def test_worker_exit_after_partial_write_releases_only_its_own_result_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = ResultRepository(tmp_path, 10_000)
    scheduler = JobScheduler(_settings(tmp_path), repository)
    owner = OwnerContext("principal", str(uuid4()))
    try:
        published_id = _admit(scheduler, owner)
        assert _wait_for_terminal(scheduler, published_id, owner).status is RunStatus.SUCCEEDED
        published_bytes = repository.read(published_id)
        monkeypatch.setattr("jobs.scheduler.run_operation_child", _exit_with_partial_result)
        run_id = _admit(scheduler, owner)

        failed = _wait_for_terminal(scheduler, run_id, owner)

        assert failed.status is RunStatus.FAILED
        assert failed.error is not None
        assert failed.error.code == "worker_ipc_failure"
        assert set(tmp_path.iterdir()) == {tmp_path / f"{published_id}.json"}
        assert repository.read(published_id) == published_bytes
        assert repository._specs == {}
        assert repository._reservations == {}
        assert repository._committed == {published_id: len(published_bytes)}
        assert repository.accounted_bytes == len(published_bytes)
        assert scheduler.active_process_count() == 0
        assert scheduler.compute_slots.leased == 0
        repository.abort(run_id)
        assert repository.accounted_bytes == len(published_bytes)
    finally:
        scheduler.shutdown()


def test_worker_publication_stat_failure_cleans_output_and_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = ResultRepository(tmp_path, 10_000)
    scheduler = JobScheduler(_settings(tmp_path), repository)
    owner = OwnerContext("principal", str(uuid4()))
    try:
        lease = scheduler.try_acquire_immediate()
        assert lease is not None
        run_id = _admit(scheduler, owner)
        target = tmp_path / f"{run_id}.json"
        failures = _fail_stat_once(monkeypatch, target)
        scheduler.release_immediate(lease)

        failed = _wait_for_terminal(scheduler, run_id, owner)

        assert failures == [target]
        assert failed.status is RunStatus.FAILED
        assert failed.error is not None
        assert failed.error.code == "result_storage_failure"
        assert set(tmp_path.iterdir()) == set()
        assert repository._specs == {}
        assert repository._reservations == {}
        assert repository._committed == {}
        assert repository.accounted_bytes == 0
        assert scheduler.active_process_count() == 0
        assert scheduler.compute_slots.leased == 0
        repository.abort(run_id)
        assert repository.accounted_bytes == 0
    finally:
        scheduler.shutdown()


def test_running_cancellation_reaps_process_and_slot_within_one_second(tmp_path: Path) -> None:
    scheduler = JobScheduler(_settings(tmp_path), ResultRepository(tmp_path, 10_000))
    owner = OwnerContext("principal", str(uuid4()))
    start = _start(owner)
    start = JobStart(
        owner_context=start.owner_context,
        operation=start.operation,
        source=start.source,
        analysis_id=start.analysis_id,
        snapshot_revision=start.snapshot_revision,
        topology=start.topology,
        parameters=start.parameters,
        worker=_slow_worker,
    )
    run_id = scheduler.admit(
        start=start,
        canonical_payload=b"slow",
        idempotency_key=str(uuid4()),
    ).run.run_id
    deadline = time.monotonic() + 2
    while scheduler.active_process_count() == 0 and time.monotonic() < deadline:
        time.sleep(0.01)

    started = time.monotonic()
    cancelled = scheduler.cancel_owned(run_id, owner)
    elapsed = time.monotonic() - started

    assert cancelled is not None
    assert cancelled.status is RunStatus.CANCELLED
    assert elapsed < 1
    assert scheduler.active_process_count() == 0
    assert scheduler.compute_slots.leased == 0
    assert scheduler.result_repository.accounted_bytes == 0
    scheduler.shutdown()


def test_heartbeat_reaper_cancels_abandoned_queued_work(tmp_path: Path) -> None:
    slots = ComputeSlotPool(1)
    occupied = slots.try_acquire()
    assert occupied is not None
    scheduler = JobScheduler(
        _settings(tmp_path, heartbeat_seconds=0.1),
        ResultRepository(tmp_path, 10_000),
        compute_slots=slots,
    )
    scheduler.start()
    owner = OwnerContext("principal", str(uuid4()))
    run_id = _admit(scheduler, owner)

    time.sleep(0.25)
    run = scheduler.get_owned(run_id, owner)

    assert run is not None
    assert run.status is RunStatus.CANCELLED
    scheduler.shutdown()
    occupied.release()


def test_background_reaper_retries_transient_result_deletion_and_stops_on_shutdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    repository = ResultRepository(tmp_path, 10_000)
    scheduler = JobScheduler(_settings(tmp_path, heartbeat_seconds=0.2), repository)
    owner = OwnerContext("principal", str(uuid4()))
    retry_entered = Event()
    allow_retry = Event()
    deleted = Event()
    attempts: list[str] = []
    original_delete = repository.delete
    try:
        run_id = _admit(scheduler, owner)
        run = _wait_for_terminal(scheduler, run_id, owner)
        assert run.status is RunStatus.SUCCEEDED
        target = tmp_path / f"{run_id}.json"
        payload = target.read_bytes()

        def delete(candidate: str) -> None:
            assert candidate == run_id
            attempts.append(candidate)
            if len(attempts) == 1:
                raise OSError(f"private capture guest:secret@broker.internal {target}")
            retry_entered.set()
            assert allow_retry.wait(timeout=5)
            original_delete(candidate)
            deleted.set()

        monkeypatch.setattr(repository, "delete", delete)
        with scheduler._lock:
            scheduler._runs[run_id] = replace(
                run,
                result_expires_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        scheduler.start()
        reaper = scheduler._reaper_thread

        # Only the scheduled thread may drive cleanup. Hold its second attempt
        # so the retained file and charge can be inspected before recovery.
        assert retry_entered.wait(timeout=5)
        assert reaper is not None and reaper.is_alive()
        assert target.read_bytes() == payload
        assert repository.accounted_bytes == len(payload)
        assert repository._committed == {run_id: len(payload)}
        allow_retry.set()
        assert deleted.wait(timeout=5)
        with scheduler._lock:
            assert run_id not in scheduler._runs
            assert scheduler._idempotency == {}
            assert scheduler._pending_result_cleanup == set()
        assert attempts == [run_id, run_id]
        assert set(tmp_path.iterdir()) == set()
        assert repository.accounted_bytes == 0
        assert repository._committed == {}
        assert repository._reservations == {}
        _assert_safe_cleanup_logs(
            caplog,
            tmp_path,
            ["Background cleanup failed; phase=result_cleanup type=OSError."],
        )
    finally:
        allow_retry.set()
        scheduler.shutdown()
    assert reaper is not None and not reaper.is_alive()


def test_persistent_result_failure_preserves_reaper_progress(  # noqa: PLR0915 - full lifecycle
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = ResultRepository(tmp_path, 10_000)
    session_reaped = Event()

    def reap_sessions() -> int:
        session_reaped.set()
        return 0

    scheduler = JobScheduler(
        _settings(tmp_path, heartbeat_seconds=0.2),
        repository,
        session_expiry_reaper=reap_sessions,
    )
    owner = OwnerContext("principal", str(uuid4()))
    retried = Event()
    cancelled = Event()
    attempts: list[str] = []
    original_delete = repository.delete
    original_cancel = scheduler._cancel_run
    reaper = None
    try:
        blocked_id = _admit(scheduler, owner)
        blocked = _wait_for_terminal(scheduler, blocked_id, owner)
        other_id = _admit(scheduler, owner)
        other = _wait_for_terminal(scheduler, other_id, owner)
        blocked_path = tmp_path / f"{blocked_id}.json"
        blocked_bytes = blocked_path.read_bytes()
        abandoned_id = scheduler.admit(
            start=replace(_start(owner), worker=_slow_worker),
            canonical_payload=b"abandoned",
            idempotency_key=str(uuid4()),
        ).run.run_id

        def delete(candidate: str) -> None:
            if candidate == blocked_id:
                attempts.append(candidate)
                if len(attempts) >= 2:
                    retried.set()
                raise OSError("persistent unlink failure")
            original_delete(candidate)

        def cancel(candidate: str) -> AnalysisRun | None:
            result = original_cancel(candidate)
            if candidate == abandoned_id:
                cancelled.set()
            return result

        monkeypatch.setattr(repository, "delete", delete)
        monkeypatch.setattr(scheduler, "_cancel_run", cancel)
        with scheduler._lock:
            past = datetime.now(UTC) - timedelta(seconds=1)
            scheduler._runs[blocked_id] = replace(blocked, result_expires_at=past)
            scheduler._runs[other_id] = replace(other, result_expires_at=past)
            scheduler._runs[abandoned_id] = replace(
                scheduler._runs[abandoned_id],
                heartbeat_expires_at=past,
            )
        scheduler.start()
        reaper = scheduler._reaper_thread

        assert cancelled.wait(timeout=5)
        assert session_reaped.wait(timeout=5)
        assert retried.wait(timeout=5)
        assert reaper is not None and reaper.is_alive()
        with scheduler._lock:
            assert scheduler._runs[abandoned_id].status is RunStatus.CANCELLED
            assert other_id not in scheduler._runs
            assert blocked_id in scheduler._runs
        assert scheduler.active_process_count() == 0
        assert scheduler.compute_slots.leased == 0
        assert set(tmp_path.iterdir()) == {blocked_path}
        assert blocked_path.read_bytes() == blocked_bytes
        assert repository.accounted_bytes == len(blocked_bytes)

    finally:
        scheduler.shutdown()
    assert reaper is not None and not reaper.is_alive()
    assert scheduler.active_process_count() == 0
    assert scheduler.compute_slots.leased == 0
    assert blocked_path.read_bytes() == blocked_bytes
    assert repository.accounted_bytes == len(blocked_bytes)


def test_reaper_isolates_heartbeat_and_session_phase_failures_and_logs_only_types(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    slots = ComputeSlotPool(1)
    occupied = slots.try_acquire()
    assert occupied is not None
    session_attempts: list[bool] = []
    secret = f"private-capture guest:secret@broker.internal {tmp_path}"

    def reap_sessions() -> int:
        session_attempts.append(True)
        if len(session_attempts) == 1:
            raise RuntimeError(secret)
        return 0

    scheduler = JobScheduler(
        _settings(tmp_path, queue_capacity=2),
        ResultRepository(tmp_path, 10_000),
        compute_slots=slots,
        session_expiry_reaper=reap_sessions,
    )
    first_owner = OwnerContext("first", str(uuid4()))
    other_owner = OwnerContext("other", str(uuid4()))
    try:
        first_id = _admit(scheduler, first_owner)
        other_id = _admit(scheduler, other_owner)
        original_cancel = scheduler._cancel_run
        attempts: list[str] = []

        def cancel(run_id: str) -> AnalysisRun | None:
            attempts.append(run_id)
            if run_id == first_id and attempts.count(first_id) == 1:
                raise OSError(secret)
            return original_cancel(run_id)

        monkeypatch.setattr(scheduler, "_cancel_run", cancel)
        with scheduler._lock:
            for run_id in (first_id, other_id):
                scheduler._runs[run_id] = replace(
                    scheduler._runs[run_id],
                    heartbeat_expires_at=datetime.now(UTC) - timedelta(seconds=1),
                )

        scheduler.reap_expired()

        assert attempts == [first_id, other_id]
        assert session_attempts == [True]
        assert scheduler._runs[first_id].status is RunStatus.QUEUED
        assert scheduler._runs[other_id].status is RunStatus.CANCELLED
        scheduler.reap_expired()
        assert scheduler._runs[first_id].status is RunStatus.CANCELLED
        assert session_attempts == [True, True]
        _assert_safe_cleanup_logs(
            caplog,
            tmp_path,
            [
                "Background cleanup failed; phase=heartbeat type=OSError.",
                "Background cleanup failed; phase=session_expiry type=RuntimeError.",
            ],
        )
    finally:
        occupied.release()
        scheduler.shutdown()


def test_reaper_loop_survives_unexpected_iteration_failure_with_safe_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    scheduler = JobScheduler(
        _settings(tmp_path, heartbeat_seconds=0.2),
        ResultRepository(tmp_path, 10_000),
    )
    recovered = Event()
    attempts: list[bool] = []

    def reap() -> None:
        attempts.append(True)
        if len(attempts) == 1:
            raise ValueError(f"private guest:secret@broker.internal {tmp_path}")
        recovered.set()

    monkeypatch.setattr(scheduler, "reap_expired", reap)
    scheduler.start()
    reaper = scheduler._reaper_thread
    try:
        assert recovered.wait(timeout=5)
        assert reaper is not None and reaper.is_alive()
        _assert_safe_cleanup_logs(
            caplog,
            tmp_path,
            ["Background cleanup failed; phase=reaper_iteration type=ValueError."],
        )
    finally:
        scheduler.shutdown()
    assert reaper is not None and not reaper.is_alive()


@pytest.mark.parametrize("cleanup_method", ["owner", "analysis"])
def test_explicit_cleanup_still_propagates_deletion_errors_and_can_be_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    cleanup_method: str,
) -> None:
    repository = ResultRepository(tmp_path, 10_000)
    scheduler = JobScheduler(_settings(tmp_path), repository)
    owner = OwnerContext("principal", str(uuid4()))
    try:
        run_id = _admit(scheduler, owner)
        _wait_for_terminal(scheduler, run_id, owner)
        target = tmp_path / f"{run_id}.json"
        payload = target.read_bytes()
        _fail_unlink_once(monkeypatch, target)

        def cleanup() -> None:
            if cleanup_method == "owner":
                scheduler.cleanup_owner(owner)
            else:
                scheduler.invalidate_analysis(owner, AnalysisSource.STATIC, "static-analysis")

        with pytest.raises(OSError, match="transient unlink failure"):
            cleanup()

        assert target.read_bytes() == payload
        assert repository.accounted_bytes == len(payload)
        assert scheduler._pending_result_cleanup == {run_id}
        assert run_id in scheduler._runs
        assert not [record for record in caplog.records if record.name == "jobs.scheduler"]

        cleanup()

        assert scheduler._runs == {}
        assert scheduler._pending_result_cleanup == set()
        assert scheduler._idempotency == {}
        assert set(tmp_path.iterdir()) == set()
        assert repository.accounted_bytes == 0
    finally:
        scheduler.shutdown()


def test_shutdown_rejects_promotion_and_reaps_running_child(tmp_path: Path) -> None:
    scheduler = JobScheduler(_settings(tmp_path), ResultRepository(tmp_path, 10_000))
    owner = OwnerContext("principal", str(uuid4()))
    start = _start(owner)
    slow_start = JobStart(
        owner_context=start.owner_context,
        operation=start.operation,
        source=start.source,
        analysis_id=start.analysis_id,
        snapshot_revision=start.snapshot_revision,
        topology=start.topology,
        parameters=start.parameters,
        worker=_slow_worker,
    )
    scheduler.admit(
        start=slow_start,
        canonical_payload=b"shutdown",
        idempotency_key=str(uuid4()),
    )

    scheduler.shutdown()

    assert scheduler.active_process_count() == 0
    assert scheduler.compute_slots.leased == 0
