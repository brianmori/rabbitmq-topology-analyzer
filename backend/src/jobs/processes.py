"""Shared hard-termination behavior for bounded compute children."""

from time import perf_counter
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from multiprocessing.process import BaseProcess

_CANCELLATION_DEADLINE_SECONDS = 1.0


def terminate_process(
    process: BaseProcess,
    *,
    deadline_seconds: float = _CANCELLATION_DEADLINE_SECONDS,
) -> None:
    """Terminate and reap one child within the shared hard deadline."""
    if process.pid is None:
        # Process construction can succeed before ``start()`` fails. There is
        # no OS child or multiprocessing handle to terminate or join in that
        # state.
        return
    started = perf_counter()
    if process.is_alive():
        process.terminate()
        process.join(timeout=min(0.7, deadline_seconds))
    if process.is_alive():
        process.kill()
        remaining = max(0.0, deadline_seconds - (perf_counter() - started))
        process.join(timeout=remaining)
    if process.is_alive():
        # A second kill is defensive; supported platforms reap a killed child
        # immediately and preserve the bounded-capacity guarantee.
        process.kill()
        process.join()
    else:
        process.join()
