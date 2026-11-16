"""Generate and validate the canonical Milestone 5 topology fixture."""

import argparse
import json
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING
from urllib.parse import quote

from rabbitmq_management_sdk import ClusterAuditor
from rabbitmq_management_sdk.topology import EdgeKind, NodeKind

if TYPE_CHECKING:
    from collections.abc import Sequence

    from rabbitmq_management_sdk.topology import ClusterTopology

VHOST_COUNT = 50
EXCHANGES_PER_VHOST = 6
QUEUES_PER_VHOST = 5
SHOVELS_PER_VHOST = 1
BINDINGS_PER_VHOST = 22
PARALLEL_EDGE_EXTRAS = 100
CROSS_VHOST_SHOVEL_EDGES = 25
EXPECTED_NODE_COUNT = 600
EXPECTED_EDGE_COUNT = 1_200


@dataclass(frozen=True, slots=True)
class RepresentativeFixtureSummary:
    """Validated SDK topology counts reported by the fixture generator."""

    vhost_count: int
    node_count: int
    edge_count: int
    parallel_edge_extras: int
    cross_vhost_shovel_edges: int
    node_kinds: dict[str, int]
    edge_kinds: dict[str, int]


def _vhost_name(index: int) -> str:
    return f"vhost-{index:02d}"


def _queue(vhost: str, index: int) -> dict[str, object]:
    return {
        "name": f"queue-{index:02d}",
        "vhost": vhost,
        "durable": True,
        "auto_delete": False,
        "arguments": {"x-queue-type": "classic"},
    }


def _exchange(vhost: str, index: int) -> dict[str, object]:
    return {
        "name": f"exchange-{index:02d}",
        "vhost": vhost,
        "type": "topic",
        "durable": True,
        "auto_delete": False,
        "internal": False,
        "arguments": {},
    }


def _binding(
    vhost: str,
    exchange_index: int,
    queue_index: int,
    routing_index: int,
    *,
    parallel_index: int | None = None,
) -> dict[str, object]:
    arguments: dict[str, object] = {"fixtureRoute": routing_index}
    if parallel_index is not None:
        arguments["parallelOrdinal"] = parallel_index
    return {
        "source": f"exchange-{exchange_index:02d}",
        "vhost": vhost,
        "destination": f"queue-{queue_index:02d}",
        "destination_type": "queue",
        "routing_key": f"route.{routing_index:02d}",
        "arguments": arguments,
    }


def _bindings(vhost: str) -> list[dict[str, object]]:
    # Three bindings share one endpoint pair. Relative to one unique pair,
    # that contributes two parallel-edge extras per vhost and exactly 100
    # extras across the 50-vhost fixture.
    bindings = [
        _binding(vhost, 0, 0, parallel_index, parallel_index=parallel_index)
        for parallel_index in range(3)
    ]
    unique_pairs = [
        (exchange_index, queue_index)
        for exchange_index in range(EXCHANGES_PER_VHOST)
        for queue_index in range(QUEUES_PER_VHOST)
        if (exchange_index, queue_index) != (0, 0)
    ]
    bindings.extend(
        _binding(vhost, exchange_index, queue_index, routing_index + 3)
        for routing_index, (exchange_index, queue_index) in enumerate(unique_pairs[:19])
    )
    if len(bindings) != BINDINGS_PER_VHOST:
        raise AssertionError("representative binding construction drifted")
    return bindings


def _shovel(vhost_index: int) -> dict[str, object]:
    source_vhost = _vhost_name(vhost_index)
    destination_index = vhost_index + 1 if vhost_index < CROSS_VHOST_SHOVEL_EDGES else vhost_index
    destination_vhost = _vhost_name(destination_index % VHOST_COUNT)
    return {
        "vhost": source_vhost,
        "component": "shovel",
        "name": "shovel-00",
        "value": {
            "src-protocol": "amqp091",
            "src-uri": f"amqp:///{quote(source_vhost, safe='')}",
            "src-queue": "queue-04",
            "dest-protocol": "amqp091",
            "dest-uri": f"amqp:///{quote(destination_vhost, safe='')}",
            "dest-queue": "queue-04",
        },
    }


def representative_definitions() -> dict[str, object]:
    """Return deterministic definitions input for the representative SDK graph."""
    vhosts = [_vhost_name(index) for index in range(VHOST_COUNT)]
    return {
        "users": [],
        "vhosts": [{"name": vhost} for vhost in vhosts],
        "permissions": [],
        "topic_permissions": [],
        "global_parameters": [
            {"name": "internal_cluster_id", "value": "representative-cluster"},
            {"name": "cluster_name", "value": "Milestone 5 representative topology"},
            {"name": "cluster_tags", "value": {"fixture": "representative-v1"}},
        ],
        "parameters": [_shovel(index) for index in range(VHOST_COUNT)],
        "policies": [],
        "queues": [
            _queue(vhost, queue_index)
            for vhost in vhosts
            for queue_index in range(QUEUES_PER_VHOST)
        ],
        "exchanges": [
            _exchange(vhost, exchange_index)
            for vhost in vhosts
            for exchange_index in range(EXCHANGES_PER_VHOST)
        ],
        "bindings": [binding for vhost in vhosts for binding in _bindings(vhost)],
    }


def write_representative_definitions(output_path: Path) -> Path:
    """Write the deterministic capture with stable compact JSON encoding."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(
            representative_definitions(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return output_path


def validate_representative_topology(topology: ClusterTopology) -> RepresentativeFixtureSummary:
    """Assert the loaded SDK graph matches every canonical fixture count."""
    node_kinds = Counter(node.id.kind.value for node in topology.nodes)
    edge_kinds = Counter(edge.kind.value for edge in topology.edges)
    vhosts = {node.id.vhost for node in topology.nodes}
    endpoint_counts = Counter(
        (edge.source, edge.target) for edge in topology.edges if edge.kind is EdgeKind.BINDING
    )
    parallel_edge_extras = sum(count - 1 for count in endpoint_counts.values())
    cross_vhost_shovel_edges = sum(
        edge.kind is EdgeKind.SHOVEL and edge.source.vhost != edge.target.vhost
        for edge in topology.edges
    )
    summary = RepresentativeFixtureSummary(
        vhost_count=len(vhosts),
        node_count=len(topology.nodes),
        edge_count=len(topology.edges),
        parallel_edge_extras=parallel_edge_extras,
        cross_vhost_shovel_edges=cross_vhost_shovel_edges,
        node_kinds=dict(sorted(node_kinds.items())),
        edge_kinds=dict(sorted(edge_kinds.items())),
    )
    expected_node_kinds = {
        NodeKind.EXCHANGE.value: VHOST_COUNT * EXCHANGES_PER_VHOST,
        NodeKind.QUEUE.value: VHOST_COUNT * QUEUES_PER_VHOST,
        NodeKind.SHOVEL.value: VHOST_COUNT * SHOVELS_PER_VHOST,
    }
    expected_edge_kinds = {
        EdgeKind.BINDING.value: VHOST_COUNT * BINDINGS_PER_VHOST,
        EdgeKind.SHOVEL.value: VHOST_COUNT * SHOVELS_PER_VHOST * 2,
    }
    expected = RepresentativeFixtureSummary(
        vhost_count=VHOST_COUNT,
        node_count=EXPECTED_NODE_COUNT,
        edge_count=EXPECTED_EDGE_COUNT,
        parallel_edge_extras=PARALLEL_EDGE_EXTRAS,
        cross_vhost_shovel_edges=CROSS_VHOST_SHOVEL_EDGES,
        node_kinds=expected_node_kinds,
        edge_kinds=expected_edge_kinds,
    )
    if summary != expected:
        raise ValueError(
            f"representative SDK topology mismatch: expected {expected}, got {summary}"
        )
    return summary


def load_and_validate(
    definitions_path: Path,
) -> tuple[ClusterAuditor, RepresentativeFixtureSummary]:
    """Load the capture only through the SDK facade and validate its topology."""
    auditor = ClusterAuditor.from_files(definitions_path=definitions_path)
    return auditor, validate_representative_topology(auditor.topology)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        help="Write the deterministic definitions JSON to this path before validating it.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Generate, SDK-load, validate, and report the canonical fixture."""
    args = _parse_args(argv)
    if args.output is not None:
        definitions_path = write_representative_definitions(args.output.resolve())
        _, summary = load_and_validate(definitions_path)
    else:
        with TemporaryDirectory(prefix="rabbitmq-representative-") as temporary_directory:
            definitions_path = write_representative_definitions(
                Path(temporary_directory) / "definitions.json",
            )
            _, summary = load_and_validate(definitions_path)
    print(json.dumps(asdict(summary), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
