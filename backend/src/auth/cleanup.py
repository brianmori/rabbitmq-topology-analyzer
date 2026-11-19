"""Application-session cleanup across request, job, and snapshot lifecycles."""

import asyncio
from typing import TYPE_CHECKING

from auth.activity import OwnerActivityRegistry, owner_activity_registry
from core.state import delete_owner_session_analyses
from jobs.runtime import get_job_scheduler

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from core.session import OwnerContext
    from jobs.scheduler import JobScheduler


async def cleanup_owner_session(
    owner_context: OwnerContext,
    *,
    activity_registry: OwnerActivityRegistry = owner_activity_registry,
    scheduler: JobScheduler | None = None,
    finalizer: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """Cancel and remove every resource while owner admission stays closed."""

    async def cleanup() -> None:
        effective_scheduler = scheduler or get_job_scheduler()
        await asyncio.to_thread(effective_scheduler.cleanup_owner, owner_context)
        delete_owner_session_analyses(owner_context)
        if finalizer is not None:
            await finalizer()

    await activity_registry.cleanup(owner_context, cleanup)
