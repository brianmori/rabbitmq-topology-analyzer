"""Public SDK cycle analysis and exact indexed result serialization."""

from dataclasses import replace
from typing import TYPE_CHECKING

from rabbitmq_management_sdk import ClusterAuditor

from cycles.result_index import encode_cycle_result
from cycles.schemas import StoredCycle
from topology.wire_ids import TopologyWireIds, node_wire_id

if TYPE_CHECKING:
    from rabbitmq_management_sdk.topology import ClusterTopology, Cycle

    from jobs.models import ResultEnvelopeContext


def _spans_vhosts(cycle: Cycle) -> bool:
    return any(edge.source.vhost != edge.target.vhost for edge in cycle.edges)


def _complete_cross_vhost_cycles(auditor: ClusterAuditor) -> tuple[Cycle, ...]:
    """Use SDK analysis on the cyclic components able to cross vhost boundaries."""
    eligible = frozenset(
        node
        for component in auditor.cyclic_components()
        if len({node.vhost for node in component.nodes}) > 1
        for node in component.nodes
    )
    if not eligible:
        return ()
    topology = auditor.topology
    scoped = replace(
        topology,
        exchanges=frozenset(node for node in topology.exchanges if node.id in eligible),
        queues=frozenset(node for node in topology.queues if node.id in eligible),
        shovels=frozenset(node for node in topology.shovels if node.id in eligible),
        edges=frozenset(
            edge for edge in topology.edges if edge.source in eligible and edge.target in eligible
        ),
    )
    # Every cross-vhost hop is a shovel edge, so only SDK loop candidates can qualify.
    candidates = ClusterAuditor.from_topology(scoped).message_loop_candidates()
    return tuple(cycle for cycle in candidates.cycles if _spans_vhosts(cycle))


def _cycles_with_all_cross_vhost(
    auditor: ClusterAuditor, max_cycles: int
) -> tuple[tuple[Cycle, ...], bool, int]:
    """Return complete cross-vhost cycles followed by the requested local cycles."""
    if max_cycles == 0:
        complete = auditor.structural_cycles().cycles
        cross_vhost = tuple(cycle for cycle in complete if _spans_vhosts(cycle))
        single_vhost = tuple(cycle for cycle in complete if not _spans_vhosts(cycle))
        return cross_vhost + single_vhost, False, len(cross_vhost)

    cross_vhost = _complete_cross_vhost_cycles(auditor)
    if cross_vhost:
        topology = auditor.topology
        scoped = replace(
            topology,
            edges=frozenset(
                edge for edge in topology.edges if edge.source.vhost == edge.target.vhost
            ),
        )
        auditor = ClusterAuditor.from_topology(scoped)
    bounded = auditor.structural_cycles(max_cycles=max_cycles)
    return cross_vhost + bounded.cycles, bounded.truncated, len(cross_vhost)


def execute_structural_cycles(
    topology: ClusterTopology,
    context: ResultEnvelopeContext,
) -> bytes:
    """Run the public SDK cycle facade and serialize its original result index."""
    max_cycles = context.parameters.get("maxCycles")
    if isinstance(max_cycles, bool) or not isinstance(max_cycles, int) or max_cycles < 0:
        raise ValueError("structural cycle context requires a non-negative maxCycles")

    auditor = ClusterAuditor.from_topology(topology)
    cross_vhost_count = None
    if context.parameters.get("includeAllCrossVhost") is True:
        cycles, truncated, cross_vhost_count = _cycles_with_all_cross_vhost(auditor, max_cycles)
    else:
        result = auditor.structural_cycles(max_cycles=max_cycles)
        cycles, truncated = result.cycles, result.truncated
    wire_ids = TopologyWireIds.from_topology(topology)
    indexed_cycles: list[StoredCycle] = []
    for cycle_index, cycle in enumerate(cycles):
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
        truncated=truncated,
        max_cycles=max_cycles,
        cross_vhost_count=cross_vhost_count,
    )
