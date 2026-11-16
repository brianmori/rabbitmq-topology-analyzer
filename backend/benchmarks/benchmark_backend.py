"""Measure the canonical backend performance phases against approved budgets."""

import argparse
import asyncio
import json
import os
import platform
import statistics
import time
from dataclasses import asdict, dataclass
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING
from uuid import uuid4

from fastapi.testclient import TestClient

from analyses.snapshots import (
    SnapshotMetadata,
    compute_snapshot_revision_from_files,
    create_analysis_snapshot,
)
from auth.config import AuthSettings
from auth.runtime import configure_auth_runtime, shutdown_auth_runtime
from benchmarks.representative_capture import (
    VHOST_COUNT,
    load_and_validate,
    write_representative_definitions,
)
from core.deployment import DeploymentMode
from core.security import AuthMode
from core.state import (
    AnalysisSource,
    CaptureCompleteness,
    configure_analysis_store,
    replace_static_analyses,
)
from definitions.processing import process_staged_capture
from definitions.staging import StagedCapture
from jobs.config import JobSettings
from jobs.runtime import configure_job_runtime, shutdown_job_runtime
from main import app

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

WARM_UP_RUNS = 1
RECORDED_RUNS = 5
TOPOLOGY_API_BUDGET_MS = 2_000.0
JOB_ADMISSION_BUDGET_MS = 500.0
UPLOAD_PROCESSING_BUDGET_MS = 10_000.0
_ANALYSIS_ID = "representative-v1"


@dataclass(frozen=True, slots=True)
class PhaseMeasurement:
    """One median-of-five benchmark result and its approved budget."""

    phase: str
    unit: str
    samples: tuple[float, ...]
    median: float
    budget: float
    passed: bool


@dataclass(frozen=True, slots=True)
class BackendBenchmarkReport:
    """Machine-readable backend benchmark environment and phase results."""

    environment: dict[str, object]
    fixture: dict[str, object]
    warm_up_runs: int
    recorded_runs: int
    phases: tuple[PhaseMeasurement, ...]


def _measure(operation: Callable[[], None]) -> float:
    started_at = time.perf_counter()
    operation()
    return (time.perf_counter() - started_at) * 1_000


def _record_phase(
    phase: str,
    budget: float,
    operation: Callable[[], None],
) -> PhaseMeasurement:
    for _ in range(WARM_UP_RUNS):
        operation()
    samples = tuple(_measure(operation) for _ in range(RECORDED_RUNS))
    median = statistics.median(samples)
    return PhaseMeasurement(
        phase=phase,
        unit="milliseconds",
        samples=tuple(round(sample, 3) for sample in samples),
        median=round(median, 3),
        budget=budget,
        passed=median <= budget,
    )


def _memory_bytes() -> int | None:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except OSError, ValueError:
        return None


async def _never_disconnected() -> bool:
    return False


def _wait_for_terminal(client: TestClient, run_id: str, revision: str, token: str) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        response = client.get(
            f"/api/scc-runs/{run_id}",
            params={"snapshot_revision": revision},
            headers={"X-Session-Token": token},
        )
        response.raise_for_status()
        if response.json()["status"] in {"succeeded", "failed", "cancelled"}:
            return
        time.sleep(0.01)
    raise TimeoutError("representative SCC benchmark run did not finish")


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Write the JSON report to this path.")
    parser.add_argument(
        "--no-enforce",
        action="store_true",
        help="Report budget failures without returning a failing exit status.",
    )
    return parser.parse_args(argv)


def run_backend_benchmark(root: Path) -> BackendBenchmarkReport:
    """Run one warm-up and five measured executions for every backend phase."""
    definitions_path = write_representative_definitions(root / "definitions.json")
    auditor, fixture_summary = load_and_validate(definitions_path)
    revision = compute_snapshot_revision_from_files(
        definitions_path=definitions_path,
        queues_path=None,
        exchanges_path=None,
        in_cluster_amqp_hosts=(),
    )
    snapshot = create_analysis_snapshot(
        auditor.topology,
        SnapshotMetadata(
            analysis_id=_ANALYSIS_ID,
            source=AnalysisSource.STATIC,
            label="Representative v1 fixture",
            capture_completeness=CaptureCompleteness.DEFINITIONS_ONLY,
            snapshot_revision=revision,
            cluster_tags=dict(auditor.definitions.cluster_tags or {}),
            in_cluster_amqp_hosts=(),
            vhost_count=VHOST_COUNT,
        ),
    )

    configure_analysis_store()
    replace_static_analyses({_ANALYSIS_ID: snapshot})
    scheduler = configure_job_runtime(
        JobSettings(
            worker_count=1,
            queue_capacity=1,
            heartbeat_ttl=timedelta(seconds=120),
            result_ttl=timedelta(seconds=120),
            results_dir=root / "results",
            results_max_bytes=64 * 1024 * 1024,
        ),
    )
    token = str(uuid4())
    # This harness deliberately bypasses the normal lifespan to install its
    # representative snapshot. Initialize the real pre-body boundary too,
    # using synthetic none mode rather than an operator's provider secrets.
    asyncio.run(
        configure_auth_runtime(
            AuthSettings(
                mode=AuthMode.NONE,
                deployment_mode=DeploymentMode.DEVELOPMENT,
                public_origin=None,
                oidc=None,
                basic=None,
            ),
        ),
    )
    client = TestClient(app)
    try:
        topology_phase = _record_phase(
            "topology_api",
            TOPOLOGY_API_BUDGET_MS,
            lambda: client.get(
                "/api/topology",
                params={"source": "static", "analysis_id": _ANALYSIS_ID},
                headers={"X-Session-Token": token},
            ).raise_for_status(),
        )

        # Admission must exclude operation completion. Record the POST only,
        # then wait outside the timed interval before the next sample.
        admission_samples: list[float] = []
        for index in range(WARM_UP_RUNS + RECORDED_RUNS):
            run_id: str | None = None

            def admit() -> None:
                nonlocal run_id
                response = client.post(
                    "/api/scc-runs",
                    headers={
                        "Content-Type": "application/json",
                        "Idempotency-Key": str(uuid4()),
                        "X-Session-Token": token,
                    },
                    json={
                        "source": "static",
                        "analysisId": _ANALYSIS_ID,
                        "snapshotRevision": revision,
                    },
                )
                response.raise_for_status()
                run_id = response.json()["runId"]

            elapsed = _measure(admit)
            if run_id is None:
                raise AssertionError("admission response omitted its run ID")
            _wait_for_terminal(client, run_id, revision, token)
            if index >= WARM_UP_RUNS:
                admission_samples.append(elapsed)
        admission_median = statistics.median(admission_samples)
        admission_phase = PhaseMeasurement(
            phase="job_admission",
            unit="milliseconds",
            samples=tuple(round(sample, 3) for sample in admission_samples),
            median=round(admission_median, 3),
            budget=JOB_ADMISSION_BUDGET_MS,
            passed=admission_median <= JOB_ADMISSION_BUDGET_MS,
        )

        staged = StagedCapture(
            definitions_path=definitions_path,
            queues_path=None,
            exchanges_path=None,
            total_bytes=definitions_path.stat().st_size,
        )

        def process_upload() -> None:
            asyncio.run(
                process_staged_capture(
                    staged,
                    in_cluster_amqp_hosts=(),
                    cluster_label="Representative v1 fixture",
                    scheduler=scheduler,
                    is_disconnected=_never_disconnected,
                ),
            )

        upload_phase = _record_phase(
            "post_transfer_upload_processing",
            UPLOAD_PROCESSING_BUDGET_MS,
            process_upload,
        )
    finally:
        client.close()
        shutdown_job_runtime()
        asyncio.run(shutdown_auth_runtime())
        replace_static_analyses({})

    return BackendBenchmarkReport(
        environment={
            "platform": platform.platform(),
            "python": platform.python_version(),
            "logicalCpuCount": os.process_cpu_count(),
            "physicalMemoryBytes": _memory_bytes(),
            "analysisWorkers": 1,
        },
        fixture=asdict(fixture_summary),
        warm_up_runs=WARM_UP_RUNS,
        recorded_runs=RECORDED_RUNS,
        phases=(topology_phase, admission_phase, upload_phase),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Execute the benchmark, print phase measurements, and enforce budgets."""
    args = _parse_args(argv)
    with TemporaryDirectory(prefix="rabbitmq-backend-benchmark-") as temporary_directory:
        report = run_backend_benchmark(Path(temporary_directory))
    payload = {
        "environment": report.environment,
        "fixture": report.fixture,
        "warmUpRuns": report.warm_up_runs,
        "recordedRuns": report.recorded_runs,
        "phases": [asdict(phase) for phase in report.phases],
    }
    rendered = json.dumps(payload, indent=2, sort_keys=True)
    print(rendered)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    if not args.no_enforce and any(not phase.passed for phase in report.phases):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
