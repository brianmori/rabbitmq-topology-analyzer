"""Real-Uvicorn routes for the unexpected-error logging regression."""

from typing import TYPE_CHECKING

from fastapi.responses import StreamingResponse

from main import app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@app.get("/api/_safe_error_probe/before")
async def before_headers() -> None:
    try:
        raise ValueError("SYNTHETIC_CAUSE_SECRET_DO_NOT_LOG")
    except ValueError as cause:
        raise RuntimeError("SYNTHETIC_EXCEPTION_SECRET_DO_NOT_LOG") from cause


@app.get("/api/_safe_error_probe/after")
async def after_headers() -> StreamingResponse:
    async def chunks() -> AsyncIterator[bytes]:
        yield b"first chunk"
        raise RuntimeError("SYNTHETIC_STREAM_SECRET_DO_NOT_LOG")

    return StreamingResponse(chunks())
