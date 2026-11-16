"""Owner-scoped request coordination used by deterministic logout cleanup."""

import asyncio
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from threading import RLock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from core.session import OwnerContext


class OwnerSessionClosingError(Exception):
    """Raised when new work targets an owner whose logout is in progress."""


@dataclass(slots=True)
class _CleanupGate:
    """Reference-counted per-owner serialization gate."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


class OwnerActivityRegistry:
    """Track authenticated request tasks so logout can close publication races."""

    def __init__(self) -> None:
        """Create an empty process-local activity registry."""
        self._lock = RLock()
        self._tasks: dict[OwnerContext, set[asyncio.Task[object]]] = defaultdict(set)
        self._closing: set[OwnerContext] = set()
        self._cleanup_gates: dict[OwnerContext, _CleanupGate] = {}

    @asynccontextmanager
    async def request(self, owner_context: OwnerContext) -> AsyncIterator[None]:
        """Register the current request unless cleanup already closed admission."""
        current = asyncio.current_task()
        if current is None:
            raise RuntimeError("authenticated request has no asyncio task")
        with self._lock:
            if owner_context in self._closing:
                raise OwnerSessionClosingError
            self._tasks[owner_context].add(current)
        try:
            yield
        finally:
            with self._lock:
                owner_tasks = self._tasks.get(owner_context)
                if owner_tasks is not None:
                    owner_tasks.discard(current)
                    if not owner_tasks:
                        self._tasks.pop(owner_context, None)

    async def cleanup(
        self,
        owner_context: OwnerContext,
        cleanup: Callable[[], Awaitable[None]],
    ) -> None:
        """Finish accepted cleanup before propagating caller cancellation.

        The entire request-drain, scheduler, snapshot, and credential cleanup
        stays in one shielded task. In particular, cancelling a waiter must not
        reopen admission while a scheduler thread still removes resources.
        Concurrent cleanups serialize and keep admission closed until every
        accepted cleanup has finished.
        """
        with self._lock:
            gate = self._cleanup_gates.setdefault(owner_context, _CleanupGate())
            gate.users += 1
            self._closing.add(owner_context)
        operation = asyncio.create_task(self._cleanup_owner(owner_context, cleanup, gate))
        cancellation_requested = False
        try:
            while not operation.done():
                try:
                    await asyncio.shield(operation)
                except asyncio.CancelledError:
                    # A further cancel() can interrupt this wait again. Keep
                    # waiting for the same protected operation every time.
                    cancellation_requested = True
            operation.result()
            if cancellation_requested:
                raise asyncio.CancelledError
        finally:
            with self._lock:
                gate.users -= 1
                if gate.users == 0 and self._cleanup_gates.get(owner_context) is gate:
                    self._cleanup_gates.pop(owner_context)
                    self._closing.discard(owner_context)
                if not self._tasks.get(owner_context):
                    self._tasks.pop(owner_context, None)

    async def _cleanup_owner(
        self,
        owner_context: OwnerContext,
        cleanup: Callable[[], Awaitable[None]],
        gate: _CleanupGate,
    ) -> None:
        """Serialize owner cleanup independently of its HTTP request task."""
        async with gate.lock:
            with self._lock:
                tasks = tuple(self._tasks.get(owner_context, ()))
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await cleanup()

    def is_closing(self, owner_context: OwnerContext) -> bool:
        """Return whether cleanup currently rejects new work for this owner."""
        with self._lock:
            return owner_context in self._closing


owner_activity_registry = OwnerActivityRegistry()
