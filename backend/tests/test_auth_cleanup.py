import asyncio
from datetime import UTC, datetime, timedelta
from threading import Event as ThreadEvent
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from rabbitmq_management_sdk.topology import ClusterTopology

from auth.activity import OwnerActivityRegistry, OwnerSessionClosingError
from auth.cleanup import cleanup_owner_session
from auth.sessions import AuthSessionRepository, ProviderCredentialState
from core.security import AppRole, AuthMode, Principal
from core.session import OwnerContext
from core.state import (
    AnalysisSnapshot,
    AnalysisSource,
    CaptureCompleteness,
    delete_owner_session_analyses,
    list_session_analyses,
    publish_session_analysis,
)
from jobs.config import JobSettings
from jobs.results import ResultRepository
from jobs.scheduler import JobScheduler

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from pathlib import Path

    from pytest import MonkeyPatch


def _run_bounded(operation: Awaitable[None]) -> None:
    async def bounded() -> None:
        async with asyncio.timeout(5):
            await operation

    asyncio.run(bounded())


async def _assert_owner_closed(registry: OwnerActivityRegistry, owner: OwnerContext) -> None:
    assert registry.is_closing(owner)
    with pytest.raises(OwnerSessionClosingError):
        async with registry.request(owner):
            pytest.fail("closing owner admitted new work")


def _snapshot(analysis_id: str) -> AnalysisSnapshot:
    return AnalysisSnapshot(
        topology=ClusterTopology(
            exchanges=frozenset(),
            queues=frozenset(),
            shovels=frozenset(),
            edges=frozenset(),
        ),
        analysis_id=analysis_id,
        source=AnalysisSource.SESSION,
        label=analysis_id,
        capture_completeness=CaptureCompleteness.DEFINITIONS_ONLY,
        snapshot_revision="sha256:" + "a" * 64,
        loaded_at=datetime.now(UTC),
        cluster_name=None,
        cluster_tags={},
        in_cluster_amqp_hosts=(),
        vhost_count=0,
    )


def _scheduler(tmp_path: Path) -> JobScheduler:
    settings = JobSettings(
        worker_count=1,
        queue_capacity=0,
        heartbeat_ttl=timedelta(seconds=120),
        result_ttl=timedelta(seconds=120),
        results_dir=tmp_path,
        results_max_bytes=1_000_000,
    )
    return JobScheduler(settings, ResultRepository(tmp_path, 1_000_000))


def test_activity_cleanup_cancels_request_and_closes_admission() -> None:
    async def run() -> None:
        owner = OwnerContext("principal", str(uuid4()))
        registry = OwnerActivityRegistry()
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        allow_cleanup = asyncio.Event()

        async def request() -> None:
            try:
                async with registry.request(owner):
                    entered.set()
                    await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        task = asyncio.create_task(request())
        await entered.wait()

        async def cleanup() -> None:
            assert cancelled.is_set()
            with pytest.raises(OwnerSessionClosingError):
                async with registry.request(owner):
                    pytest.fail("closing owner admitted a new request")
            await allow_cleanup.wait()

        cleanup_task = asyncio.create_task(registry.cleanup(owner, cleanup))
        await cancelled.wait()
        allow_cleanup.set()
        await cleanup_task
        assert task.cancelled()
        assert not registry.is_closing(owner)
        assert registry._cleanup_gates == {}

    asyncio.run(run())


def test_cancelled_logout_finishes_request_draining_and_cleanup_before_reopening() -> None:
    async def run() -> None:
        owner = OwnerContext("principal", str(uuid4()))
        registry = OwnerActivityRegistry()
        entered = asyncio.Event()
        draining = asyncio.Event()
        release_request = asyncio.Event()
        cleaning = asyncio.Event()
        release_cleanup = asyncio.Event()
        finished = False

        async def request() -> None:
            async with registry.request(owner):
                entered.set()
                try:
                    await asyncio.Future()
                finally:
                    draining.set()
                    await release_request.wait()

        async def cleanup() -> None:
            nonlocal finished
            cleaning.set()
            await release_cleanup.wait()
            finished = True

        worker = asyncio.create_task(request())
        await entered.wait()
        logout = asyncio.create_task(registry.cleanup(owner, cleanup))
        try:
            await draining.wait()
            logout.cancel()
            await asyncio.sleep(0)

            assert not worker.done(), "logout cancellation interrupted request cleanup"
            await _assert_owner_closed(registry, owner)

            release_request.set()
            await cleaning.wait()
            logout.cancel()
            await asyncio.sleep(0)
            assert not logout.done()
            await _assert_owner_closed(registry, owner)
        finally:
            release_request.set()
            release_cleanup.set()
            await asyncio.gather(logout, worker, return_exceptions=True)

        assert logout.cancelled()
        assert finished
        assert not registry.is_closing(owner)
        assert registry._cleanup_gates == {}
        async with registry.request(owner):
            pass

    _run_bounded(run())


def test_cancelled_logout_waits_for_scheduler_thread_snapshots_and_oidc_invalidation(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def run() -> None:
        now = datetime.now(UTC)
        sessions = AuthSessionRepository()
        session = sessions.establish_session(
            principal=Principal("principal", frozenset({AppRole.ANALYST}), AuthMode.OIDC),
            issuer="https://issuer.example.test",
            subject="verified-subject",
            nonce="verified-nonce",
            provider_credentials=ProviderCredentialState(
                access_token="provider-access",
                refresh_token="provider-refresh",
                access_token_expires_at=now + timedelta(minutes=5),
                refresh_token_expires_at=None,
            ),
        )
        owner = session.owner_context
        publish_session_analysis(owner, _snapshot("owned"))
        registry = OwnerActivityRegistry()
        scheduler = _scheduler(tmp_path)
        original_cleanup = scheduler.cleanup_owner
        started = asyncio.Event()
        release_thread = ThreadEvent()
        thread_finished = ThreadEvent()
        loop = asyncio.get_running_loop()

        def blocked_cleanup(owner_context: OwnerContext) -> int:
            loop.call_soon_threadsafe(started.set)
            assert release_thread.wait(5), "test did not release scheduler cleanup"
            result = original_cleanup(owner_context)
            thread_finished.set()
            return result

        monkeypatch.setattr(scheduler, "cleanup_owner", blocked_cleanup)

        async def invalidate() -> None:
            assert registry.is_closing(owner)
            assert thread_finished.is_set()
            assert list_session_analyses(owner) == ()
            sessions.delete_session(session.lookup_token)

        logout = asyncio.create_task(
            cleanup_owner_session(
                owner,
                activity_registry=registry,
                scheduler=scheduler,
                finalizer=invalidate,
            )
        )
        try:
            await started.wait()
            logout.cancel()
            await asyncio.sleep(0)
            logout.cancel()
            await asyncio.sleep(0)

            assert not logout.done()
            assert list_session_analyses(owner)
            assert sessions.get_session(session.lookup_token) is not None
            await _assert_owner_closed(registry, owner)
        finally:
            release_thread.set()
            await asyncio.gather(logout, return_exceptions=True)

        assert logout.cancelled()
        assert thread_finished.is_set()
        assert list_session_analyses(owner) == ()
        assert sessions.get_session(session.lookup_token) is None
        assert not registry.is_closing(owner)
        assert registry._cleanup_gates == {}

    _run_bounded(run())


def test_concurrent_cleanup_keeps_owner_closed_when_a_queued_caller_is_cancelled() -> None:
    async def run() -> None:
        owner = OwnerContext("principal", str(uuid4()))
        registry = OwnerActivityRegistry()
        first_entered = asyncio.Event()
        second_entered = asyncio.Event()
        release_first = asyncio.Event()
        release_second = asyncio.Event()

        async def first_cleanup() -> None:
            first_entered.set()
            await release_first.wait()

        async def second_cleanup() -> None:
            second_entered.set()
            await release_second.wait()

        first = asyncio.create_task(registry.cleanup(owner, first_cleanup))
        await first_entered.wait()
        second = asyncio.create_task(registry.cleanup(owner, second_cleanup))
        try:
            await asyncio.sleep(0)
            second.cancel()
            await asyncio.sleep(0)
            assert not second_entered.is_set()
            release_first.set()
            await second_entered.wait()
            await first

            assert not second.done()
            await _assert_owner_closed(registry, owner)
        finally:
            release_first.set()
            release_second.set()
            await asyncio.gather(first, second, return_exceptions=True)

        assert second.cancelled()
        assert not registry.is_closing(owner)
        assert registry._cleanup_gates == {}

    _run_bounded(run())


def test_cleanup_failure_is_propagated_and_restores_owner_admission() -> None:
    async def run() -> None:
        owner = OwnerContext("principal", str(uuid4()))
        registry = OwnerActivityRegistry()

        async def fail() -> None:
            raise RuntimeError("cleanup failed")

        with pytest.raises(RuntimeError, match="cleanup failed"):
            await registry.cleanup(owner, fail)

        assert not registry.is_closing(owner)
        assert registry._cleanup_gates == {}
        async with registry.request(owner):
            pass

    asyncio.run(run())


def test_owner_cleanup_removes_only_exact_owner_snapshots(tmp_path: Path) -> None:
    async def run() -> None:
        owner = OwnerContext("principal", str(uuid4()))
        other = OwnerContext("principal", str(uuid4()))
        publish_session_analysis(owner, _snapshot("owned"))
        publish_session_analysis(other, _snapshot("other"))
        scheduler = _scheduler(tmp_path)
        registry = OwnerActivityRegistry()
        finalized = False

        async def finalize() -> None:
            nonlocal finalized
            assert registry.is_closing(owner)
            assert list_session_analyses(owner) == ()
            finalized = True

        await cleanup_owner_session(
            owner,
            activity_registry=registry,
            scheduler=scheduler,
            finalizer=finalize,
        )

        assert finalized
        assert list_session_analyses(owner) == ()
        assert [entry.snapshot.analysis_id for entry in list_session_analyses(other)] == ["other"]
        assert delete_owner_session_analyses(other) == 1

    asyncio.run(run())
