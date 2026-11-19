"""Atomic quota-backed repository for opaque completed-operation results."""

from contextlib import contextmanager
from dataclasses import dataclass
from stat import S_ISREG
from threading import RLock
from typing import TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from typing import BinaryIO


@dataclass(frozen=True, slots=True)
class ResultWriteSpec:
    """Parent-generated confined paths passed to one result-writing child."""

    run_id: str
    temporary_path: Path
    committed_path: Path


class ResultRepository:
    """Own result paths, exact byte reservations, and atomic publication."""

    def __init__(self, root: Path, max_bytes: int) -> None:
        """Create a repository rooted at one process-local directory."""
        if max_bytes <= 0:
            raise ValueError("result repository quota must be positive")
        self._root = root
        self._max_bytes = max_bytes
        self._lock = RLock()
        self._accounted_bytes = 0
        self._reservations: dict[str, int] = {}
        self._committed: dict[str, int] = {}
        self._specs: dict[str, ResultWriteSpec] = {}
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        """Return the configured process-local result directory."""
        return self._root

    @property
    def accounted_bytes(self) -> int:
        """Return temporary reservations plus committed result bytes."""
        with self._lock:
            return self._accounted_bytes

    def reconcile(self, known_run_ids: frozenset[str] = frozenset()) -> None:
        """Remove temporary/orphan files and rebuild committed-byte accounting."""
        with self._lock:
            self._reservations.clear()
            self._committed.clear()
            self._specs.clear()
            self._accounted_bytes = 0
            for path in self._root.iterdir():
                if not path.is_file():
                    continue
                if path.suffix != ".json" or path.stem not in known_run_ids:
                    path.unlink(missing_ok=True)
                    continue
                size = path.stat().st_size
                self._committed[path.stem] = size
                self._accounted_bytes += size

    def prepare(self, run_id: str) -> ResultWriteSpec:
        """Allocate unique temporary and stable committed paths for one run."""
        with self._lock:
            if run_id in self._specs or run_id in self._committed:
                raise ValueError("result paths already exist for this run")
            spec = ResultWriteSpec(
                run_id=run_id,
                temporary_path=self._root / f".{run_id}.{uuid4()}.tmp",
                committed_path=self._root / f"{run_id}.json",
            )
            self._specs[run_id] = spec
            return spec

    def reserve(self, run_id: str, size: int) -> bool:
        """Atomically reserve the complete encoded size before a child writes."""
        if size <= 0:
            raise ValueError("result reservation must be positive")
        with self._lock:
            if run_id not in self._specs or run_id in self._reservations:
                raise ValueError("result run is not prepared for reservation")
            if self._accounted_bytes + size > self._max_bytes:
                return False
            self._reservations[run_id] = size
            self._accounted_bytes += size
            return True

    def commit(self, run_id: str) -> None:
        """Validate and transfer a reservation; leave failures retryable or abortable."""
        with self._lock:
            spec = self._specs[run_id]
            reserved_size = self._reservations[run_id]
            published = spec.committed_path.stat()
            if not S_ISREG(published.st_mode):
                raise OSError("worker did not publish its committed result")
            if published.st_size != reserved_size:
                raise OSError("published result size differs from its reservation")
            # No fallible filesystem work may follow the reservation transfer.
            self._committed[run_id] = reserved_size
            self._reservations.pop(run_id)
            self._specs.pop(run_id, None)

    def abort(self, run_id: str) -> None:
        """Remove all output before releasing retryable paths and byte accounting."""
        with self._lock:
            spec = self._specs.get(run_id)
            if spec is not None:
                spec.temporary_path.unlink(missing_ok=True)
                spec.committed_path.unlink(missing_ok=True)
            else:
                (self._root / f"{run_id}.json").unlink(missing_ok=True)
            # Retain both paths and the full charge if either unlink fails.
            self._specs.pop(run_id, None)
            reserved_size = self._reservations.pop(run_id, 0)
            committed_size = self._committed.pop(run_id, 0)
            self._accounted_bytes -= reserved_size + committed_size

    def read(self, run_id: str) -> bytes:
        """Read one already committed envelope."""
        with self._lock:
            if run_id not in self._committed:
                raise FileNotFoundError(run_id)
            return (self._root / f"{run_id}.json").read_bytes()

    @contextmanager
    def open_reader(self, run_id: str) -> Iterator[BinaryIO]:
        """Open one stable committed file without holding the ledger lock during I/O."""
        with self._lock:
            if run_id not in self._committed:
                raise FileNotFoundError(run_id)
            result_file = (self._root / f"{run_id}.json").open("rb")
        try:
            yield result_file
        finally:
            result_file.close()

    def delete(self, run_id: str) -> None:
        """Delete one result during run expiry or invalidation."""
        self.abort(run_id)
