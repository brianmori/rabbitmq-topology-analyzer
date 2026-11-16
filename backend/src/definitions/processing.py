"""Immediate-capacity spawned processing for one staged capture."""

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from multiprocessing import get_context
from typing import TYPE_CHECKING, Protocol

from analyses.snapshots import compute_snapshot_revision_from_files
from core.concurrency import complete_on_cancel, run_blocking
from definitions.sdk_bridge import DefinitionsLoadError, LoadedDefinitions, load_definitions

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from multiprocessing.connection import Connection
    from multiprocessing.context import SpawnContext
    from multiprocessing.process import BaseProcess
    from pathlib import Path

    from definitions.staging import StagedCapture
    from jobs.scheduler import ComputeSlotLease, JobScheduler

_POLL_INTERVAL_SECONDS = 0.05
_SUCCESS_MESSAGE_LENGTH = 3


class CaptureCapacityUnavailableError(Exception):
    """Raised when no shared compute slot is immediately available."""


class CaptureValidationError(Exception):
    """Raised when the SDK rejects staged capture content."""


class CaptureProcessingError(Exception):
    """Raised when the parser child fails without a safe validation outcome."""


class CaptureRequestDisconnectedError(Exception):
    """Raised after a disconnected request's parser child has been reaped."""


@dataclass(frozen=True, slots=True)
class _CaptureProcessRequest:
    definitions_path: Path
    queues_path: Path | None
    exchanges_path: Path | None
    in_cluster_amqp_hosts: tuple[str, ...]
    cluster_label: str


@dataclass(frozen=True, slots=True)
class ProcessedCapture:
    """Safe child output used to construct one application snapshot."""

    loaded_definitions: LoadedDefinitions
    snapshot_revision: str


class CaptureChildTarget(Protocol):
    """Picklable spawned-child entrypoint used by processing tests."""

    def __call__(self, request: _CaptureProcessRequest, connection: Connection) -> None:
        """Process one request and send one terminal parent message."""


def process_capture_child(
    request: _CaptureProcessRequest,
    connection: Connection,
) -> None:
    """Load staged paths through the public SDK facade in a spawned child."""
    try:
        loaded = load_definitions(
            request.definitions_path,
            queues_path=request.queues_path,
            exchanges_path=request.exchanges_path,
            in_cluster_amqp_hosts=request.in_cluster_amqp_hosts,
            cluster_label=request.cluster_label,
        )
        revision = compute_snapshot_revision_from_files(
            definitions_path=request.definitions_path,
            queues_path=request.queues_path,
            exchanges_path=request.exchanges_path,
            in_cluster_amqp_hosts=request.in_cluster_amqp_hosts,
        )
        connection.send(("success", loaded, revision))
    except DefinitionsLoadError:
        connection.send(("validation",))
    except BaseException:
        connection.send(("failure",))
    finally:
        connection.close()


def _processed_message(message: object) -> ProcessedCapture:
    """Validate one terminal child message without exposing child failures."""
    if (
        isinstance(message, tuple)
        and len(message) == _SUCCESS_MESSAGE_LENGTH
        and message[0] == "success"
        and isinstance(message[1], LoadedDefinitions)
        and isinstance(message[2], str)
    ):
        return ProcessedCapture(
            loaded_definitions=message[1],
            snapshot_revision=message[2],
        )
    if message == ("validation",):
        raise CaptureValidationError
    raise CaptureProcessingError


async def _receive_until_complete_or_disconnected(
    receive_task: asyncio.Task[object],
    is_disconnected: Callable[[], Awaitable[bool]],
) -> object:
    """Keep observing request disconnects while IPC bytes are decoded."""
    while not receive_task.done():
        if await is_disconnected():
            raise CaptureRequestDisconnectedError
        await asyncio.wait((receive_task,), timeout=_POLL_INTERVAL_SECONDS)
    return receive_task.result()


async def _cleanup_capture_process(
    *,
    scheduler: JobScheduler,
    lease: ComputeSlotLease,
    process: BaseProcess | None,
    parent_connection: Connection | None,
    child_connection: Connection | None,
    receive_task: asyncio.Task[object] | None,
) -> None:
    """Reap one parser child, settle IPC, and release shared capacity."""
    try:
        if process is not None:
            await asyncio.to_thread(
                scheduler.terminate_immediate_process,
                lease,
                process,
            )
        if receive_task is not None:
            # Shielding keeps the connection receive alive when the request
            # task is cancelled. Reaping the child above guarantees that a
            # partial message reaches EOF; wait for that thread before the
            # parent closes the same connection underneath it.
            with suppress(BaseException):
                await asyncio.shield(receive_task)
    finally:
        if child_connection is not None:
            child_connection.close()
        if parent_connection is not None:
            parent_connection.close()
        # Releasing can promote and spawn one queued graph job. Keep that
        # process launch off the FastAPI request event loop too.
        await asyncio.to_thread(scheduler.release_immediate, lease)


async def process_staged_capture(
    staged: StagedCapture,
    *,
    in_cluster_amqp_hosts: tuple[str, ...],
    cluster_label: str,
    scheduler: JobScheduler,
    is_disconnected: Callable[[], Awaitable[bool]],
    process_context: SpawnContext | None = None,
    child_target: CaptureChildTarget = process_capture_child,
) -> ProcessedCapture:
    """Run one unqueued parser child and always release its shared slot."""
    lease: ComputeSlotLease | None = None

    def acquire() -> None:
        nonlocal lease
        lease = scheduler.try_acquire_immediate()

    context = process_context or get_context("spawn")
    parent_connection: Connection | None = None
    child_connection: Connection | None = None
    process = None
    receive_task: asyncio.Task[object] | None = None
    try:
        await run_blocking(acquire)
        if lease is None:
            raise CaptureCapacityUnavailableError
        parent_connection, child_connection = context.Pipe(duplex=False)
        request = _CaptureProcessRequest(
            definitions_path=staged.definitions_path,
            queues_path=staged.queues_path,
            exchanges_path=staged.exchanges_path,
            in_cluster_amqp_hosts=in_cluster_amqp_hosts,
            cluster_label=cluster_label,
        )
        process = context.Process(
            target=child_target,
            args=(request, child_connection),
            name="analysis-capture-upload",
        )
        try:
            started = await run_blocking(
                scheduler.start_immediate_process,
                lease,
                process,
            )
        except Exception as exc:
            raise CaptureProcessingError from exc
        if not started:
            raise CaptureCapacityUnavailableError
        child_connection.close()
        child_connection = None

        while True:
            if await is_disconnected():
                raise CaptureRequestDisconnectedError
            if await asyncio.to_thread(parent_connection.poll, _POLL_INTERVAL_SECONDS):
                receive_task = asyncio.create_task(asyncio.to_thread(parent_connection.recv))
                message = await _receive_until_complete_or_disconnected(
                    receive_task,
                    is_disconnected,
                )
                return _processed_message(message)
            if not process.is_alive():
                if parent_connection.poll():
                    continue
                raise CaptureProcessingError
    except OSError, EOFError:
        raise CaptureProcessingError from None
    finally:
        if lease is not None:
            await complete_on_cancel(
                _cleanup_capture_process(
                    scheduler=scheduler,
                    lease=lease,
                    process=process,
                    parent_connection=parent_connection,
                    child_connection=child_connection,
                    receive_task=receive_task,
                ),
            )
