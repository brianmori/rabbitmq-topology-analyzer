"""Cancellation-safe thread boundaries for blocking request transactions."""

import asyncio
from functools import wraps
from typing import TYPE_CHECKING

from anyio import CancelScope

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from typing import Any


async def complete_on_cancel[T](operation: Coroutine[Any, Any, T]) -> T:
    """Drain an accepted operation before propagating request cancellation.

    The request remains registered with owner activity until its thread exits.
    Logout can then drain it before removing runs, pins, and snapshots, without
    a late admission or cancellation thread racing that cleanup.
    """
    pending = asyncio.create_task(operation)
    cancellation_requested = False
    with CancelScope(shield=True):
        while not pending.done():
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError:
                cancellation_requested = True
        result = pending.result()
    if cancellation_requested:
        raise asyncio.CancelledError
    return result


async def run_blocking[**P, T](operation: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Run blocking work off-loop and keep its request alive until it finishes."""
    return await complete_on_cancel(asyncio.to_thread(operation, *args, **kwargs))


def blocking_route[**P, T](operation: Callable[P, T]) -> Callable[P, Coroutine[Any, Any, T]]:
    """Run a synchronous route transaction off-loop and drain it on cancellation."""

    @wraps(operation)
    async def handler(*args: P.args, **kwargs: P.kwargs) -> T:
        return await run_blocking(operation, *args, **kwargs)

    return handler
