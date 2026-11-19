"""FastAPI application entrypoint.

Wires together the domain routers (analyses, definitions, topology,
cycles, audit) and owns the app-level lifespan, which
is responsible for loading static analyses into ``core.state`` once at
startup.

This file should stay thin: it mounts routers and configures cross-cutting
concerns (CORS, lifespan). Business logic belongs in each domain package.
"""

from contextlib import asynccontextmanager
from importlib.metadata import version
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from access.router import router as access_router
from analyses.loader import load_static_analyses
from analyses.router import router as analyses_router
from auth.router import router as auth_router
from auth.runtime import (
    RuntimeAuthMiddleware,
    configure_auth_runtime,
    shutdown_auth_runtime,
)
from core.deployment import validate_production_environment
from core.errors import register_error_handlers
from core.frontend import mount_configured_frontend
from core.query import NoQuery  # noqa: TC001 - FastAPI resolves at runtime.
from core.state import configure_analysis_store
from cycles.router import router as cycles_router
from definitions.config import configure_capture_uploads
from definitions.router import router as definitions_router
from jobs.runtime import configure_job_runtime, shutdown_job_runtime
from scc.router import router as scc_router
from topology.router import router as topology_router

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Configure lifecycle state and load static analyses once at startup.

    Args:
        app: The FastAPI application instance (unused, required by the
            lifespan protocol).

    Yields:
        None. Control returns to FastAPI to serve requests; nothing runs
        after the yield in this iteration.
    """
    validate_production_environment()
    await configure_auth_runtime()
    try:
        configure_analysis_store()
        configure_capture_uploads()
        configure_job_runtime()
        load_static_analyses()
        yield
    finally:
        shutdown_job_runtime()
        await shutdown_auth_runtime()


app = FastAPI(
    title="RabbitMQ Topology Analyzer",
    version=version("rabbitmq-topology-analyzer-backend"),
    lifespan=lifespan,
)
# Dev convenience only. The Vite dev server proxies /api to this backend
# (see frontend/vite.config.ts), so CORS headers aren't exercised during
# local development. This middleware exists for the case where the built
# frontend is later served from a different origin than the backend.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RuntimeAuthMiddleware)
# Register last so this request boundary also contains authentication and CORS
# failures before Starlette's server-error layer can re-raise them to Uvicorn.
register_error_handlers(app)

app.include_router(auth_router, prefix="/api")
app.include_router(analyses_router, prefix="/api")
app.include_router(definitions_router, prefix="/api")
app.include_router(access_router, prefix="/api")
app.include_router(topology_router, prefix="/api")
app.include_router(scc_router, prefix="/api")
app.include_router(cycles_router, prefix="/api")


@app.get("/api/health")
async def health_check(_query: NoQuery) -> dict[str, str]:
    """Report that the backend is up.

    Returns:
        A small status payload the frontend polls once to confirm the
        backend is reachable.
    """
    return {"status": "ok"}


@app.get("/api/health/live")
async def liveness_check(_query: NoQuery) -> dict[str, str]:
    """Report that the sole ASGI process can serve requests."""
    return {"status": "live"}


@app.get("/api/health/ready")
async def readiness_check(_query: NoQuery) -> dict[str, str]:
    """Report readiness after fail-fast lifespan initialization completed."""
    return {"status": "ready"}


mount_configured_frontend(app)
