"""Safe React Flow-shaped topology response schemas."""

from enum import StrEnum
from typing import Literal

from analyses.schemas import ClusterSummary  # noqa: TC001 - Pydantic resolves at runtime.
from core.query import StrictQuery
from core.schemas import CamelModel
from core.state import AnalysisSource  # noqa: TC001 - Pydantic resolves this field at runtime.


class TopologyQuery(StrictQuery):
    """Select exactly one accessible topology using the documented query names."""

    source: AnalysisSource
    analysis_id: str


class TopologyNodeKind(StrEnum):
    """Closed node vocabulary exposed to React Flow."""

    EXCHANGE = "exchange"
    QUEUE = "queue"
    SHOVEL = "shovel"


class TopologyEdgeKind(StrEnum):
    """Closed edge vocabulary exposed to React Flow."""

    BINDING = "binding"
    DEAD_LETTER = "dead_letter"
    ALTERNATE_EXCHANGE = "alternate_exchange"
    SHOVEL = "shovel"


class ShovelEndpointResourceKind(StrEnum):
    """Resource kinds permitted on a safe shovel endpoint."""

    EXCHANGE = "exchange"
    QUEUE = "queue"


class GraphPosition(CamelModel):
    """Backend placeholder position replaced by browser-side ELK."""

    x: float
    y: float


class CommonNodeData(CamelModel):
    """Safe identity and presentation state shared by every node kind."""

    kind: TopologyNodeKind
    name: str
    vhost: str
    cluster_id: str | None
    is_missing: bool


class ExchangeNodeData(CommonNodeData):
    """Whitelisted exchange attributes."""

    kind: Literal[TopologyNodeKind.EXCHANGE] = TopologyNodeKind.EXCHANGE
    exchange_type: str | None
    durable: bool | None
    internal: bool | None
    is_default: bool | None


class QueueNodeData(CommonNodeData):
    """Whitelisted queue attributes."""

    kind: Literal[TopologyNodeKind.QUEUE] = TopologyNodeKind.QUEUE
    queue_type: str | None
    durable: bool | None


class SafeShovelEndpoint(CamelModel):
    """Credential-free resource metadata for one shovel endpoint."""

    vhost: str | None
    resource_name: str | None
    resource_kind: ShovelEndpointResourceKind | None
    routing_key: str | None
    is_confirmed_local: bool


class ShovelNodeData(CommonNodeData):
    """Whitelisted shovel attributes without connection authorities."""

    kind: Literal[TopologyNodeKind.SHOVEL] = TopologyNodeKind.SHOVEL
    source: SafeShovelEndpoint | None
    destination: SafeShovelEndpoint | None
    is_cross_vhost: bool | None
    has_unconfirmed_endpoint: bool | None


type TopologyNodeData = ExchangeNodeData | QueueNodeData | ShovelNodeData


class TopologyNode(CamelModel):
    """One captured or presentation-only missing React Flow node."""

    id: str
    type: TopologyNodeKind
    position: GraphPosition
    data: TopologyNodeData


class TopologyEdgeData(CamelModel):
    """Safe routing metadata for one graph edge."""

    kind: TopologyEdgeKind
    routing_key: str | None


class TopologyEdge(CamelModel):
    """One directed React Flow edge."""

    id: str
    source: str
    target: str
    type: TopologyEdgeKind
    data: TopologyEdgeData


class TopologyResponse(CamelModel):
    """Complete safe graph for one immutable analysis snapshot."""

    source: AnalysisSource
    analysis_id: str
    snapshot_revision: str
    cluster: ClusterSummary
    nodes: tuple[TopologyNode, ...]
    edges: tuple[TopologyEdge, ...]
