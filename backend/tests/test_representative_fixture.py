import asyncio
import math
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from benchmarks import benchmark_backend
from benchmarks.representative_capture import (
    CROSS_VHOST_SHOVEL_EDGES,
    EXPECTED_EDGE_COUNT,
    EXPECTED_NODE_COUNT,
    PARALLEL_EDGE_EXTRAS,
    VHOST_COUNT,
    load_and_validate,
    write_representative_definitions,
)

from auth.runtime import get_auth_runtime, shutdown_auth_runtime


def test_representative_fixture_is_constructed_and_validated_by_the_sdk(tmp_path: Path) -> None:
    definitions_path = write_representative_definitions(tmp_path / "definitions.json")

    auditor, summary = load_and_validate(definitions_path)

    assert summary.vhost_count == VHOST_COUNT
    assert summary.node_count == EXPECTED_NODE_COUNT == len(auditor.topology.nodes)
    assert summary.edge_count == EXPECTED_EDGE_COUNT == len(auditor.topology.edges)
    assert summary.parallel_edge_extras == PARALLEL_EDGE_EXTRAS
    assert summary.cross_vhost_shovel_edges == CROSS_VHOST_SHOVEL_EDGES


def test_backend_benchmark_configures_and_cleans_its_own_auth_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(shutdown_auth_runtime())
    monkeypatch.setenv("RABBITMQ_AUTH_MODE", "oidc")
    monkeypatch.setenv("RABBITMQ_OIDC_ISSUER", "not-a-provider")
    monkeypatch.setattr(benchmark_backend, "WARM_UP_RUNS", 0)
    monkeypatch.setattr(benchmark_backend, "RECORDED_RUNS", 1)

    report = benchmark_backend.run_backend_benchmark(tmp_path)

    assert {phase.phase for phase in report.phases} == {
        "topology_api",
        "job_admission",
        "post_transfer_upload_processing",
    }
    assert all(len(phase.samples) == 1 for phase in report.phases)
    assert all(math.isfinite(phase.median) and phase.median >= 0 for phase in report.phases)
    with pytest.raises(RuntimeError, match="authentication runtime is not configured"):
        get_auth_runtime()
