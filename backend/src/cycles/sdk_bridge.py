"""Public SDK structural-cycle call and exact indexed result serialization."""

from typing import TYPE_CHECKING

from rabbitmq_management_sdk import ClusterAuditor

from cycles.result_index import encode_cycle_result
from cycles.schemas import StoredCycle
from topology.wire_ids import TopologyWireIds, node_wire_id

if TYPE_CHECKING:
    from rabbitmq_management_sdk.topology import ClusterTopology

    from jobs.models import ResultEnvelopeContext


def execute_structural_cycles(
    topology: ClusterTopology,
    context: ResultEnvelopeContext,
) -> bytes:
    """Run the public SDK cycle facade and serialize its original result index."""
    max_cycles = context.parameters.get("maxCycles")
    if isinstance(max_cycles, bool) or not isinstance(max_cycles, int) or max_cycles < 0:
        raise ValueError("structural cycle context requires a non-negative maxCycles")

    search_result = ClusterAuditor.from_topology(topology).structural_cycles(
        max_cycles=max_cycles,
    )
    wire_ids = TopologyWireIds.from_topology(topology)
    indexed_cycles: list[StoredCycle] = []
    for cycle_index, cycle in enumerate(search_result.cycles):
        nodes = cycle.nodes
        indexed_cycles.append(
            StoredCycle(
                cycle_index=cycle_index,
                length=len(cycle),
                vhosts=tuple(sorted({node.vhost for node in nodes})),
                node_ids=tuple(node_wire_id(node) for node in nodes),
                edge_ids=tuple(wire_ids.edge_id(edge) for edge in cycle.edges),
            ),
        )

    return encode_cycle_result(
        context,
        indexed_cycles,
        truncated=search_result.truncated,
        max_cycles=max_cycles,
    )
