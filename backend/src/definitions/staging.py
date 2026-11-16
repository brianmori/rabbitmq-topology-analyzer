"""Bounded staging of capture files after multipart receipt."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from anyio import open_file

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import UploadFile

_UPLOAD_CHUNK_BYTES = 1024 * 1024


class CaptureUploadTooLargeError(Exception):
    """Raised as soon as aggregate received or staged files exceed the limit."""


@dataclass(frozen=True, slots=True)
class StagedCapture:
    """Confined paths for one completely received multipart capture."""

    definitions_path: Path
    queues_path: Path | None
    exchanges_path: Path | None
    total_bytes: int


async def _stage_one(
    upload: UploadFile,
    destination: Path,
    *,
    current_total: int,
    max_upload_bytes: int,
) -> int:
    total = current_total
    async with await open_file(destination, "xb") as staged_file:
        while chunk := await upload.read(_UPLOAD_CHUNK_BYTES):
            total += len(chunk)
            if total > max_upload_bytes:
                raise CaptureUploadTooLargeError
            await staged_file.write(chunk)
    return total


async def stage_capture_files(
    staging_root: Path,
    *,
    definitions_file: UploadFile,
    queues_file: UploadFile | None,
    exchanges_file: UploadFile | None,
    max_upload_bytes: int,
) -> StagedCapture:
    """Stream multipart file values into fixed confined staging paths.

    The aggregate is counted from bytes actually read. Request headers and
    client-provided filenames never participate in limit enforcement or path
    selection.
    """
    uploads = tuple(
        upload for upload in (definitions_file, queues_file, exchanges_file) if upload is not None
    )
    definitions_path = staging_root / "definitions.json"
    queues_path = staging_root / "queues.json" if queues_file is not None else None
    exchanges_path = staging_root / "exchanges.json" if exchanges_file is not None else None
    total = 0
    try:
        total = await _stage_one(
            definitions_file,
            definitions_path,
            current_total=total,
            max_upload_bytes=max_upload_bytes,
        )
        if queues_file is not None and queues_path is not None:
            total = await _stage_one(
                queues_file,
                queues_path,
                current_total=total,
                max_upload_bytes=max_upload_bytes,
            )
        if exchanges_file is not None and exchanges_path is not None:
            total = await _stage_one(
                exchanges_file,
                exchanges_path,
                current_total=total,
                max_upload_bytes=max_upload_bytes,
            )
    finally:
        for upload in uploads:
            await upload.close()
    return StagedCapture(
        definitions_path=definitions_path,
        queues_path=queues_path,
        exchanges_path=exchanges_path,
        total_bytes=total,
    )
