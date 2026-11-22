import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from rabbitmq_management_sdk import ClusterAuditor

from core.session import OwnerContext
from core.state import AnalysisSource
from cycles.result_index import read_cycle_detail, read_cycle_metadata
from cycles.sdk_bridge import execute_structural_cycles
from jobs.models import AnalysisRun, OperationKind, ResultEnvelopeContext, RunStatus
from jobs.results import ResultRepository
from topology.wire_ids import TopologyWireIds, node_wire_id

if TYPE_CHECKING:
    from pathlib import Path


def _cycle_definitions() -> dict[str, object]:
    return {
        "users": [],
        "vhosts": [{"name": "/"}],
        "permissions": [],
        "topic_permissions": [],
        "global_parameters": [
            {"name": "internal_cluster_id", "value": "cluster-cycles"},
        ],
        "parameters": [],
        "policies": [],
        "queues": [
            {
                "name": name,
                "vhost": "/",
                "durable": True,
                "auto_delete": False,
                "arguments": {
                    "x-queue-type": "classic",
                    "x-dead-letter-exchange": "loop-x",
                },
            }
            for name in ("loop-q-1", "loop-q-2")
        ],
        "exchanges": [
            {
                "name": "loop-x",
                "vhost": "/",
                "type": "direct",
                "durable": True,
                "auto_delete": False,
                "internal": False,
                "arguments": {},
            },
        ],
        "bindings": [
            {
                "source": "loop-x",
                "vhost": "/",
                "destination": "loop-q-1",
                "destination_type": "queue",
                "routing_key": routing_key,
                "arguments": arguments,
            }
            for routing_key, arguments in (
                ("a", {"parallel": 1}),
                ("b", {"parallel": 2, "password": "must-not-leak"}),
            )
        ]
        + [
            {
                "source": "loop-x",
                "vhost": "/",
                "destination": "loop-q-2",
                "destination_type": "queue",
                "routing_key": "c",
                "arguments": {},
            },
        ],
    }


def _cross_vhost_shovel_definitions() -> dict[str, object]:
    return {
        "users": [],
        "vhosts": [{"name": "src"}, {"name": "dst"}],
        "permissions": [],
        "topic_permissions": [],
        "global_parameters": [
            {"name": "internal_cluster_id", "value": "cluster-cross-vhost-cycles"},
        ],
        "parameters": [
            {
                "vhost": "src",
                "component": "shovel",
                "name": "src-to-dst",
                "value": {
                    "src-protocol": "amqp091",
                    "src-uri": "amqp:///src",
                    "src-queue": "q-src",
                    "dest-protocol": "amqp091",
                    "dest-uri": "amqp:///dst",
                    "dest-queue": "q-dst",
                },
            },
            {
                "vhost": "dst",
                "component": "shovel",
                "name": "dst-to-src",
                "value": {
                    "src-protocol": "amqp091",
                    "src-uri": "amqp:///dst",
                    "src-queue": "q-dst",
                    "dest-protocol": "amqp091",
                    "dest-uri": "amqp:///src",
                    "dest-queue": "q-src",
                },
            },
        ],
        "policies": [],
        "queues": [
            {
                "name": "q-src",
                "vhost": "src",
                "durable": True,
                "auto_delete": False,
                "arguments": {"x-queue-type": "classic"},
            },
            {
                "name": "q-dst",
                "vhost": "dst",
                "durable": True,
                "auto_delete": False,
                "arguments": {"x-queue-type": "classic"},
            },
        ],
        "exchanges": [],
        "bindings": [],
    }


def _auditor(tmp_path: Path) -> ClusterAuditor:
    definitions_path = tmp_path / "definitions.json"
    definitions_path.write_text(json.dumps(_cycle_definitions()), encoding="utf-8")
    return ClusterAuditor.from_files(definitions_path=definitions_path)


def _context(max_cycles: int, *, all_cross_vhost: bool = False) -> ResultEnvelopeContext:
    parameters: dict[str, str | int | bool | None] = {"maxCycles": max_cycles}
    if all_cross_vhost:
        parameters["includeAllCrossVhost"] = True
    return ResultEnvelopeContext(
        operation=OperationKind.STRUCTURAL_CYCLES,
        source=AnalysisSource.STATIC,
        analysis_id="cycle-fixture",
        snapshot_revision="sha256:" + "c" * 64,
        parameters=parameters,
    )


def _commit_payload(
    tmp_path: Path,
    payload: bytes,
    context: ResultEnvelopeContext,
) -> tuple[AnalysisRun, ResultRepository]:
    run_id = str(uuid4())
    repository = ResultRepository(tmp_path / run_id, len(payload) + 1)
    write_spec = repository.prepare(run_id)
    assert repository.reserve(run_id, len(payload)) is True
    write_spec.committed_path.write_bytes(payload)
    repository.commit(run_id)
    now = datetime.now(UTC)
    return (
        AnalysisRun(
            run_id=run_id,
            owner_context=OwnerContext("test-principal", str(uuid4())),
            operation=OperationKind.STRUCTURAL_CYCLES,
            status=RunStatus.SUCCEEDED,
            source=context.source,
            analysis_id=context.analysis_id,
            snapshot_revision=context.snapshot_revision,
            created_at=now,
            started_at=now,
            finished_at=now,
            heartbeat_expires_at=None,
            result_expires_at=now,
            error=None,
        ),
        repository,
    )


def test_cycle_bridge_preserves_sdk_order_original_indexes_and_exact_edges(
    tmp_path: Path,
) -> None:
    auditor = _auditor(tmp_path)
    expected = auditor.structural_cycles(max_cycles=0)
    wire_ids = TopologyWireIds.from_topology(auditor.topology)
    context = _context(0)

    run, repository = _commit_payload(
        tmp_path,
        execute_structural_cycles(auditor.topology, context),
        context,
    )
    metadata = read_cycle_metadata(run, repository)
    details = tuple(
        read_cycle_detail(run, repository, cycle_index)
        for cycle_index in range(metadata.result.cycle_count)
    )

    assert metadata.operation == OperationKind.STRUCTURAL_CYCLES
    assert metadata.parameters == {"maxCycles": 0}
    assert metadata.result.cycle_count == len(expected.cycles) == 2
    assert metadata.result.truncated is False
    assert [detail.cycle_index for detail in details] == [0, 1]
    assert [detail.node_ids for detail in details] == [
        tuple(node_wire_id(node) for node in cycle.nodes) for cycle in expected.cycles
    ]
    assert [detail.edge_ids for detail in details] == [
        tuple(wire_ids.edge_id(edge) for edge in cycle.edges) for cycle in expected.cycles
    ]
    retained_output = metadata.model_dump_json() + "".join(
        detail.model_dump_json() for detail in details
    )
    assert "must-not-leak" not in retained_output


def test_cycle_bridge_passes_bounded_and_unbounded_limits_to_sdk(tmp_path: Path) -> None:
    auditor = _auditor(tmp_path)

    bounded_context = _context(1)
    bounded_run, bounded_repository = _commit_payload(
        tmp_path,
        execute_structural_cycles(auditor.topology, bounded_context),
        bounded_context,
    )
    unbounded_context = _context(0)
    unbounded_run, unbounded_repository = _commit_payload(
        tmp_path,
        execute_structural_cycles(auditor.topology, unbounded_context),
        unbounded_context,
    )
    bounded = read_cycle_metadata(bounded_run, bounded_repository).result
    unbounded = read_cycle_metadata(unbounded_run, unbounded_repository).result

    assert bounded.cycle_count == 1
    assert bounded.max_cycles == 1
    assert bounded.truncated is True
    assert unbounded.cycle_count == 2
    assert unbounded.max_cycles == 0
    assert unbounded.truncated is False


def test_cycle_bridge_preserves_real_sdk_cross_vhost_shovel_cycle(tmp_path: Path) -> None:
    definitions_path = tmp_path / "cross-vhost-definitions.json"
    definitions_path.write_text(
        json.dumps(_cross_vhost_shovel_definitions()),
        encoding="utf-8",
    )
    auditor = ClusterAuditor.from_files(definitions_path=definitions_path)
    expected = auditor.structural_cycles(max_cycles=0)
    wire_ids = TopologyWireIds.from_topology(auditor.topology)
    context = _context(0)

    run, repository = _commit_payload(
        tmp_path,
        execute_structural_cycles(auditor.topology, context),
        context,
    )
    metadata = read_cycle_metadata(run, repository)
    detail = read_cycle_detail(run, repository, 0)

    assert len(expected.cycles) == metadata.result.cycle_count == 1
    assert metadata.result.truncated is False
    assert detail.vhosts == ("dst", "src")
    assert detail.node_ids == tuple(node_wire_id(node) for node in expected.cycles[0].nodes)
    assert detail.edge_ids == tuple(wire_ids.edge_id(edge) for edge in expected.cycles[0].edges)
    assert {node.vhost for node in expected.cycles[0].nodes} == {"dst", "src"}


@pytest.mark.parametrize("max_cycles", [0, 1])
def test_complete_cross_vhost_cycles_are_included_before_the_local_budget(
    tmp_path: Path, max_cycles: int
) -> None:
    definitions = _cross_vhost_shovel_definitions()
    parameters = definitions["parameters"]
    assert isinstance(parameters, list)
    parameters.append({**parameters[1], "name": "second-return"})
    single_vhost = _cycle_definitions()
    for section in ("vhosts", "queues", "exchanges", "bindings"):
        existing = definitions[section]
        additional = single_vhost[section]
        assert isinstance(existing, list) and isinstance(additional, list)
        definitions[section] = [*existing, *additional]
    definitions_path = tmp_path / "mixed-cycles.json"
    definitions_path.write_text(json.dumps(definitions), encoding="utf-8")
    auditor = ClusterAuditor.from_files(definitions_path)
    assert len(auditor.structural_cycles(max_cycles=1).cycles[0]) == 2

    context = _context(max_cycles, all_cross_vhost=True)
    run, repository = _commit_payload(
        tmp_path,
        execute_structural_cycles(auditor.topology, context),
        context,
    )
    metadata = read_cycle_metadata(run, repository).result
    assert metadata.cross_vhost_count == 2
    assert metadata.cycle_count == 2 + (max_cycles or 2)
    assert metadata.truncated is bool(max_cycles)
    details = [read_cycle_detail(run, repository, index) for index in range(metadata.cycle_count)]
    assert [detail.vhosts for detail in details] == [("dst", "src")] * 2 + [("/",)] * (
        max_cycles or 2
    )
    wire_ids = TopologyWireIds.from_topology(auditor.topology)
    expected_edges = {
        tuple(wire_ids.edge_id(edge) for edge in cycle.edges)
        for cycle in auditor.structural_cycles().cycles
        if len({node.vhost for node in cycle.nodes}) > 1
    }
    assert {detail.edge_ids for detail in details[:2]} == expected_edges
