"""Adapt public SDK topology values to explicit safe graph DTOs."""

from typing import TYPE_CHECKING

from rabbitmq_management_sdk.topology import ExchangeNode as SdkExchangeNode
from rabbitmq_management_sdk.topology import (
    NodeId,
    NodeKind,
    ResourceEndpoint,
    edge_sort_key,
    node_sort_key,
)
from rabbitmq_management_sdk.topology import QueueNode as SdkQueueNode
from rabbitmq_management_sdk.topology import ShovelNode as SdkShovelNode
from rabbitmq_management_sdk.topology import TopologyNode as SdkTopologyNode

from analyses.schemas import cluster_summary
from topology.schemas import (
    ExchangeNodeData,
    GraphPosition,
    QueueNodeData,
    SafeShovelEndpoint,
    ShovelEndpointResourceKind,
    ShovelNodeData,
    TopologyEdge,
    TopologyEdgeData,
    TopologyEdgeKind,
    TopologyNode,
    TopologyNodeData,
    TopologyNodeKind,
    TopologyResponse,
)
from topology.wire_ids import TopologyWireIds, node_wire_id

if TYPE_CHECKING:
    from core.state import AnalysisSnapshot


def _safe_endpoint(endpoint: ResourceEndpoint) -> SafeShovelEndpoint:
    resource_kind = (
        ShovelEndpointResourceKind(endpoint.resource_kind.value)
        if endpoint.resource_kind is not None
        else None
    )
    return SafeShovelEndpoint(
        vhost=endpoint.vhost,
        resource_name=endpoint.resource_name,
        resource_kind=resource_kind,
        routing_key=endpoint.routing_key,
        is_confirmed_local=endpoint.is_confirmed_local,
    )


def _captured_node_data(node: SdkTopologyNode) -> TopologyNodeData:
    common = {
        "name": node.id.name,
        "vhost": node.id.vhost,
        "cluster_id": node.id.cluster_id,
        "is_missing": False,
    }
    if isinstance(node, SdkExchangeNode):
        return ExchangeNodeData(
            **common,
            exchange_type=node.exchange_type,
            durable=node.durable,
            internal=node.internal,
            is_default=node.is_default,
        )
    if isinstance(node, SdkQueueNode):
        return QueueNodeData(
            **common,
            queue_type=node.queue_type,
            durable=node.durable,
        )
    if isinstance(node, SdkShovelNode):
        return ShovelNodeData(
            **common,
            source=_safe_endpoint(node.source),
            destination=_safe_endpoint(node.destination),
            is_cross_vhost=node.is_cross_vhost,
            has_unconfirmed_endpoint=node.has_unconfirmed_endpoint,
        )
    raise AssertionError(f"Unsupported SDK topology node: {node!r}")


def _missing_node_data(node_id: NodeId) -> TopologyNodeData:
    common = {
        "name": node_id.name,
        "vhost": node_id.vhost,
        "cluster_id": node_id.cluster_id,
        "is_missing": True,
    }
    if node_id.kind is NodeKind.EXCHANGE:
        return ExchangeNodeData(
            **common,
            exchange_type=None,
            durable=None,
            internal=None,
            is_default=None,
        )
    if node_id.kind is NodeKind.QUEUE:
        return QueueNodeData(**common, queue_type=None, durable=None)
    return ShovelNodeData(
        **common,
        source=None,
        destination=None,
        is_cross_vhost=None,
        has_unconfirmed_endpoint=None,
    )


def serialize_topology(snapshot: AnalysisSnapshot) -> TopologyResponse:
    """Serialize one complete SDK topology without raw or sensitive fields."""
    sdk_topology = snapshot.topology
    known_node_ids = sdk_topology.all_node_ids
    referenced_node_ids = {
        node_id for edge in sdk_topology.edges for node_id in (edge.source, edge.target)
    }
    all_node_ids = sorted(known_node_ids | referenced_node_ids, key=node_sort_key)
    wire_ids = TopologyWireIds.from_topology(sdk_topology)

    nodes: list[TopologyNode] = []
    for node_id in all_node_ids:
        is_missing = node_id not in known_node_ids
        data = (
            _missing_node_data(node_id)
            if is_missing
            else _captured_node_data(sdk_topology.nodes_by_id[node_id])
        )
        nodes.append(
            TopologyNode(
                id=node_wire_id(node_id, is_missing=is_missing),
                type=TopologyNodeKind(node_id.kind.value),
                position=GraphPosition(x=0, y=0),
                data=data,
            ),
        )

    edges = tuple(
        TopologyEdge(
            id=wire_ids.edge_id(edge),
            source=node_wire_id(edge.source, is_missing=edge.source not in known_node_ids),
            target=node_wire_id(edge.target, is_missing=edge.target not in known_node_ids),
            type=TopologyEdgeKind(edge.kind.value),
            data=TopologyEdgeData(
                kind=TopologyEdgeKind(edge.kind.value),
                routing_key=edge.routing_key,
            ),
        )
        for edge in sorted(sdk_topology.edges, key=edge_sort_key)
    )
    return TopologyResponse(
        source=snapshot.source,
        analysis_id=snapshot.analysis_id,
        snapshot_revision=snapshot.snapshot_revision,
        cluster=cluster_summary(snapshot),
        nodes=tuple(nodes),
        edges=edges,
    )
