"""FastAPI application entrypoint.

Wires together the domain routers (analyses, definitions, topology,
cycles, audit) and owns the app-level lifespan, which
is responsible for loading static analyses into ``core.state`` once at
startup.

This file should stay thin: it mounts routers and configures cross-cutting
concerns (CORS, lifespan). Business logic belongs in each domain package.
"""

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from access.router import router as access_router
from analyses.loader import load_static_analyses
from analyses.router import router as analyses_router
from core.state import configure_analysis_store
from definitions.router import router as definitions_router

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
    configure_analysis_store()
    load_static_analyses()
    yield


app = FastAPI(title="RabbitMQ Topology Viewer", lifespan=lifespan)

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

app.include_router(analyses_router, prefix="/api")
app.include_router(definitions_router, prefix="/api")
app.include_router(access_router, prefix="/api")


@app.get("/api/health")
async def health_check() -> dict[str, str]:
    """Report that the backend is up.

    Returns:
        A small status payload the frontend polls once to confirm the
        backend is reachable.
    """
    return {"status": "ok"}
