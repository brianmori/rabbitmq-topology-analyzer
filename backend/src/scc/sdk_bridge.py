"""Public SDK cyclic-component call and safe result serialization."""

from typing import TYPE_CHECKING

from rabbitmq_management_sdk import ClusterAuditor
from rabbitmq_management_sdk.topology import node_sort_key

from jobs.models import ResultEnvelopeContext, encode_result_envelope
from topology.wire_ids import node_wire_id

if TYPE_CHECKING:
    from rabbitmq_management_sdk.topology import ClusterTopology


def execute_cyclic_scc(
    topology: ClusterTopology,
    context: ResultEnvelopeContext,
) -> bytes:
    """Run the SDK facade and return one versioned safe SCC envelope."""
    components = ClusterAuditor.from_topology(topology).cyclic_components()
    serialized_components: list[dict[str, object]] = []
    for component_index, component in enumerate(components):
        ordered_nodes = tuple(sorted(component.nodes, key=node_sort_key))
        serialized_components.append(
            {
                "componentIndex": component_index,
                "isCyclic": True,
                "nodeCount": len(ordered_nodes),
                "vhosts": sorted({node.vhost for node in ordered_nodes}),
                "nodeIds": [node_wire_id(node) for node in ordered_nodes],
            },
        )
    return encode_result_envelope(
        context,
        {"components": serialized_components},
    )
