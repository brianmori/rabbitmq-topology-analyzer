"""Optional production serving of the compiled single-page frontend."""

import os
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi.staticfiles import StaticFiles

if TYPE_CHECKING:
    from fastapi import FastAPI


def mount_configured_frontend(app: FastAPI) -> None:
    """Mount a configured production build after API routes, or do nothing locally."""
    configured = os.environ.get("RABBITMQ_FRONTEND_DIST_DIR")
    if configured is None:
        return
    try:
        frontend_root = Path(configured).resolve(strict=True)
    except OSError as exc:
        raise ValueError("RABBITMQ_FRONTEND_DIST_DIR must reference a readable directory") from exc
    if not frontend_root.is_dir() or not (frontend_root / "index.html").is_file():
        raise ValueError("RABBITMQ_FRONTEND_DIST_DIR must contain index.html")
    app.mount("/", StaticFiles(directory=frontend_root, html=True), name="frontend")
