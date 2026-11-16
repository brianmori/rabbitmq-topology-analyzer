import time
from datetime import UTC, datetime, timedelta
from threading import Event, Thread
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from rabbitmq_management_sdk.topology import ClusterTopology

from analyses.snapshots import SnapshotMetadata, create_analysis_snapshot
from core import state
from core.session import OwnerContext
from core.state import (
    AnalysisSource,
    CaptureCompleteness,
    ExpiredSessionAnalysis,
    configure_analysis_store,
    pin_analysis_run,
    publish_session_analysis,
    reap_expired_session_analyses,
    refresh_analysis_run_access,
    set_session_expiry_handler,
    unpin_analysis_run,
)
from jobs.config import JobSettings
from jobs.models import (
    AnalysisRun,
    JobStart,
    OperationKind,
    ResultEnvelopeContext,
    RunStatus,
    encode_result_envelope,
)
from jobs.results import ResultRepository
from jobs.scheduler import JobScheduler
from main import app

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def isolated_session_store(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("RABBITMQ_SESSION_ANALYSIS_LIMIT", raising=False)
    monkeypatch.delenv("RABBITMQ_SESSION_ANALYSIS_GLOBAL_LIMIT", raising=False)
    monkeypatch.delenv("RABBITMQ_SESSION_TOPOLOGY_TTL_SECONDS", raising=False)
    set_session_expiry_handler(None)
    state._session_store.clear()
    configure_analysis_store()
    yield
    set_session_expiry_handler(None)
    state._session_store.clear()
    configure_analysis_store()


def _empty_topology() -> ClusterTopology:
    return ClusterTopology(
        exchanges=frozenset(),
        queues=frozenset(),
        shovels=frozenset(),
        edges=frozenset(),
        cluster_id="session-expiry-cluster",
        cluster_name="Session expiry cluster",
    )


def _snapshot(
    analysis_id: str = "expiring-analysis",
    revision: str = "sha256:" + "e" * 64,
) -> state.AnalysisSnapshot:
    return create_analysis_snapshot(
        _empty_topology(),
        SnapshotMetadata(
            analysis_id=analysis_id,
            source=AnalysisSource.SESSION,
            label="Expiring analysis",
            capture_completeness=CaptureCompleteness.DEFINITIONS_ONLY,
            snapshot_revision=revision,
            cluster_tags={},
            in_cluster_amqp_hosts=(),
            vhost_count=0,
        ),
    )


def _quick_worker(
    _topology: ClusterTopology,
    context: ResultEnvelopeContext,
) -> bytes:
    return encode_result_envelope(context, {"ok": True})


def _settings(results_dir: Path) -> JobSettings:
    return JobSettings(
        worker_count=1,
        queue_capacity=1,
        heartbeat_ttl=timedelta(seconds=5),
        result_ttl=timedelta(hours=1),
        results_dir=results_dir,
        results_max_bytes=10_000,
    )


def _terminal_run(run: AnalysisRun) -> None:
    unpin_analysis_run(
        run.owner_context,
        run.source,
        run.analysis_id,
        run.snapshot_revision,
    )


def _refresh_run_access(run: AnalysisRun) -> bool:
    return refresh_analysis_run_access(
        run.owner_context,
        run.source,
        run.analysis_id,
        run.snapshot_revision,
    )


def _scheduler(results_dir: Path) -> JobScheduler:
    scheduler = JobScheduler(
        _settings(results_dir),
        ResultRepository(results_dir, 10_000),
        terminal_callback=_terminal_run,
        analysis_access_callback=_refresh_run_access,
        session_expiry_reaper=reap_expired_session_analyses,
    )
    set_session_expiry_handler(scheduler.invalidate_expired_analysis)
    return scheduler


def _start(
    owner: OwnerContext,
    snapshot: state.AnalysisSnapshot,
    operation: OperationKind,
) -> JobStart:
    parameters: dict[str, str | int | bool | None] = {}
    if operation is OperationKind.STRUCTURAL_CYCLES:
        parameters["maxCycles"] = 0
    return JobStart(
        owner_context=owner,
        operation=operation,
        source=snapshot.source,
        analysis_id=snapshot.analysis_id,
        snapshot_revision=snapshot.snapshot_revision,
        topology=snapshot.topology,
        parameters=parameters,
        worker=_quick_worker,
    )


def _admit(
    scheduler: JobScheduler,
    owner: OwnerContext,
    snapshot: state.AnalysisSnapshot,
    operation: OperationKind,
    *,
    idempotency_key: str,
) -> AnalysisRun:
    return scheduler.admit(
        start=_start(owner, snapshot, operation),
        canonical_payload=b"same-request",
        idempotency_key=idempotency_key,
        on_admit=lambda: pin_analysis_run(
            owner,
            snapshot.source,
            snapshot.analysis_id,
            snapshot.snapshot_revision,
        ),
    ).run


def _wait_for_terminal(
    scheduler: JobScheduler,
    owner: OwnerContext,
    run_id: str,
) -> AnalysisRun:
    deadline = time.monotonic() + 5
    run = scheduler.get_owned(run_id, owner)
    while run is not None and not run.status.is_terminal and time.monotonic() < deadline:
        time.sleep(0.02)
        run = scheduler.get_owned(run_id, owner)
    assert run is not None
    assert run.status is RunStatus.SUCCEEDED
    return run


def _expire_topology(owner: OwnerContext, analysis_id: str) -> None:
    state._session_store[(owner, analysis_id)].last_accessed_at = datetime.now(UTC) - timedelta(
        seconds=2,
    )


@pytest.mark.parametrize(
    "operation",
    [OperationKind.CYCLIC_SCC, OperationKind.STRUCTURAL_CYCLES],
)
def test_periodic_topology_expiry_removes_terminal_run_file_quota_and_idempotency(
    operation: OperationKind,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RABBITMQ_SESSION_TOPOLOGY_TTL_SECONDS", "1")
    configure_analysis_store()
    owner = OwnerContext("analyst", str(uuid4()))
    snapshot = _snapshot()
    publish_session_analysis(owner, snapshot)
    scheduler = _scheduler(tmp_path)
    idempotency_key = str(uuid4())
    try:
        original = _admit(
            scheduler,
            owner,
            snapshot,
            operation,
            idempotency_key=idempotency_key,
        )
        _wait_for_terminal(scheduler, owner, original.run_id)
        result_path = tmp_path / f"{original.run_id}.json"
        assert result_path.is_file()
        assert scheduler.result_repository.accounted_bytes > 0

        _expire_topology(owner, snapshot.analysis_id)
        scheduler.reap_expired()

        assert state.list_session_analyses(owner) == ()
        assert scheduler.get_owned(original.run_id, owner) is None
        assert not result_path.exists()
        assert scheduler.result_repository.accounted_bytes == 0

        publish_session_analysis(owner, snapshot)
        restarted = _admit(
            scheduler,
            owner,
            snapshot,
            operation,
            idempotency_key=idempotency_key,
        )
        assert restarted.run_id != original.run_id
    finally:
        set_session_expiry_handler(None)
        scheduler.shutdown()


def test_expired_topology_is_invalidated_before_terminal_result_ttl_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RABBITMQ_SESSION_TOPOLOGY_TTL_SECONDS", "1")
    configure_analysis_store()
    owner = OwnerContext("analyst", str(uuid4()))
    snapshot = _snapshot()
    publish_session_analysis(owner, snapshot)
    scheduler = _scheduler(tmp_path)
    try:
        admitted = _admit(
            scheduler,
            owner,
            snapshot,
            OperationKind.STRUCTURAL_CYCLES,
            idempotency_key=str(uuid4()),
        )
        _wait_for_terminal(scheduler, owner, admitted.run_id)
        result_path = tmp_path / f"{admitted.run_id}.json"
        assert result_path.is_file()
        _expire_topology(owner, snapshot.analysis_id)

        refreshes: list[str] = []
        original_refresh = scheduler._refresh_terminal_locked

        def tracked_refresh(run: AnalysisRun, now: datetime) -> AnalysisRun:
            refreshes.append(run.run_id)
            return original_refresh(run, now)

        monkeypatch.setattr(scheduler, "_refresh_terminal_locked", tracked_refresh)

        assert scheduler.get_owned(admitted.run_id, owner) is None
        assert refreshes == []
        assert state.list_session_analyses(owner) == ()
        assert not result_path.exists()
        assert scheduler.result_repository.accounted_bytes == 0
    finally:
        set_session_expiry_handler(None)
        scheduler.shutdown()


def test_run_access_uses_retained_revision_before_router_checks_request_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RABBITMQ_ANALYSIS_RESULTS_DIR", str(tmp_path))
    owner_token = str(uuid4())
    owner = OwnerContext("anonymous", owner_token)
    snapshot = _snapshot()

    with TestClient(app) as client:
        publish_session_analysis(owner, snapshot)
        started = client.post(
            "/api/scc-runs",
            json={
                "source": "session",
                "analysisId": snapshot.analysis_id,
                "snapshotRevision": snapshot.snapshot_revision,
            },
            headers={
                "X-Session-Token": owner_token,
                "Idempotency-Key": str(uuid4()),
            },
        )
        assert started.status_code == 202
        run_id = started.json()["runId"]
        deadline = time.monotonic() + 5
        current = started
        while current.json()["status"] not in {"succeeded", "failed", "cancelled"}:
            assert time.monotonic() < deadline
            time.sleep(0.02)
            current = client.get(
                f"/api/scc-runs/{run_id}",
                params={"snapshot_revision": snapshot.snapshot_revision},
                headers={"X-Session-Token": owner_token},
            )
            assert current.status_code == 200

        wrong_request_revision = client.get(
            f"/api/scc-runs/{run_id}",
            params={"snapshot_revision": "sha256:" + "f" * 64},
            headers={"X-Session-Token": owner_token},
        )

    assert wrong_request_revision.status_code == 409


def test_delayed_expiry_event_does_not_remove_same_revision_republish_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RABBITMQ_SESSION_TOPOLOGY_TTL_SECONDS", "1")
    configure_analysis_store()
    owner = OwnerContext("analyst", str(uuid4()))
    snapshot = _snapshot()
    publish_session_analysis(owner, snapshot)
    scheduler = _scheduler(tmp_path)
    captured: list[ExpiredSessionAnalysis] = []
    try:
        old_run = _admit(
            scheduler,
            owner,
            snapshot,
            OperationKind.CYCLIC_SCC,
            idempotency_key=str(uuid4()),
        )
        _wait_for_terminal(scheduler, owner, old_run.run_id)
        set_session_expiry_handler(captured.append)
        _expire_topology(owner, snapshot.analysis_id)
        assert reap_expired_session_analyses() == 1
        assert len(captured) == 1

        publish_session_analysis(owner, snapshot)
        new_run = _admit(
            scheduler,
            owner,
            snapshot,
            OperationKind.STRUCTURAL_CYCLES,
            idempotency_key=str(uuid4()),
        )
        _wait_for_terminal(scheduler, owner, new_run.run_id)

        scheduler.invalidate_expired_analysis(captured[0])

        assert scheduler.get_owned(old_run.run_id, owner) is None
        assert scheduler.get_owned(new_run.run_id, owner) is not None
    finally:
        set_session_expiry_handler(None)
        scheduler.shutdown()


def test_expiry_callback_runs_outside_store_lock_and_preserves_domain_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RABBITMQ_SESSION_TOPOLOGY_TTL_SECONDS", "1")
    configure_analysis_store()
    owner = OwnerContext("analyst", str(uuid4()))
    snapshot = _snapshot()
    publish_session_analysis(owner, snapshot)
    _expire_topology(owner, snapshot.analysis_id)
    callback_ran = Event()

    def failing_handler(_expired: ExpiredSessionAnalysis) -> None:
        acquired = Event()

        def acquire_store_lock() -> None:
            with state._store_lock:
                acquired.set()

        thread = Thread(target=acquire_store_lock)
        thread.start()
        assert acquired.wait(timeout=1)
        thread.join(timeout=1)
        callback_ran.set()
        raise RuntimeError("cleanup failed")

    set_session_expiry_handler(failing_handler)

    with pytest.raises(KeyError, match="missing-analysis"):
        pin_analysis_run(
            owner,
            AnalysisSource.SESSION,
            "missing-analysis",
            snapshot.snapshot_revision,
        )

    assert callback_ran.is_set()


def test_topology_expiry_retries_results_and_preserves_other_cleanup(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RABBITMQ_SESSION_TOPOLOGY_TTL_SECONDS", "1")
    configure_analysis_store()
    owner = OwnerContext("analyst", str(uuid4()))
    blocked_snapshot = _snapshot("blocked")
    other_snapshot = _snapshot("other")
    publish_session_analysis(owner, blocked_snapshot)
    publish_session_analysis(owner, other_snapshot)
    scheduler = _scheduler(tmp_path)
    repository = scheduler.result_repository
    retried = Event()
    recover = Event()
    blocked_deleted = Event()
    other_deleted = Event()
    attempts: list[str] = []
    original_delete = repository.delete
    reaper = None
    try:
        blocked = _admit(
            scheduler,
            owner,
            blocked_snapshot,
            OperationKind.CYCLIC_SCC,
            idempotency_key=str(uuid4()),
        )
        _wait_for_terminal(scheduler, owner, blocked.run_id)
        other = _admit(
            scheduler,
            owner,
            other_snapshot,
            OperationKind.CYCLIC_SCC,
            idempotency_key=str(uuid4()),
        )
        _wait_for_terminal(scheduler, owner, other.run_id)
        blocked_path = tmp_path / f"{blocked.run_id}.json"
        blocked_bytes = blocked_path.read_bytes()

        def delete(run_id: str) -> None:
            if run_id == blocked.run_id and not recover.is_set():
                attempts.append(run_id)
                if len(attempts) >= 2:
                    retried.set()
                raise OSError("persistent result deletion failure")
            original_delete(run_id)
            if run_id == blocked.run_id:
                blocked_deleted.set()
            elif run_id == other.run_id:
                other_deleted.set()

        monkeypatch.setattr(repository, "delete", delete)
        _expire_topology(owner, blocked_snapshot.analysis_id)
        _expire_topology(owner, other_snapshot.analysis_id)
        scheduler.start()
        reaper = scheduler._reaper_thread

        assert other_deleted.wait(timeout=5)
        assert retried.wait(timeout=5)
        assert reaper is not None and reaper.is_alive()
        with state._store_lock:
            assert state._session_store == {}
        with scheduler._lock:
            assert set(scheduler._runs) == {blocked.run_id}
            assert scheduler._pending_result_cleanup == {blocked.run_id}
            # Its one-hour result TTL has not elapsed: retry must remember the
            # earlier topology invalidation after the snapshot is gone.
            expires_at = scheduler._runs[blocked.run_id].result_expires_at
            assert expires_at is not None and expires_at > datetime.now(UTC)
        assert set(tmp_path.iterdir()) == {blocked_path}
        assert blocked_path.read_bytes() == blocked_bytes
        assert repository.accounted_bytes == len(blocked_bytes)

        recover.set()
        assert blocked_deleted.wait(timeout=5)
        with scheduler._lock:
            assert scheduler._runs == {}
            assert scheduler._pending_result_cleanup == set()
            assert scheduler._idempotency == {}
        assert set(tmp_path.iterdir()) == set()
        assert repository.accounted_bytes == 0
        assert repository._committed == {}
        assert repository._reservations == {}
    finally:
        recover.set()
        scheduler.shutdown()
    assert reaper is not None and not reaper.is_alive()
