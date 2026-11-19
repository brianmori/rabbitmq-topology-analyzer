"""Versioned reversible node IDs and snapshot-scoped edge IDs."""

import base64
import binascii
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

from rabbitmq_management_sdk import TopologyValidationError
from rabbitmq_management_sdk.topology import (
    ClusterTopology,
    NodeId,
    NodeKind,
    TopologyEdge,
    edge_sort_key,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

_CAPTURED_NODE_PREFIX = "n1."
_MISSING_NODE_PREFIX = "m1."
_EDGE_PREFIX = "e1."
_NODE_IDENTITY_FIELD_COUNT = 4


@dataclass(frozen=True, slots=True)
class DecodedNodeWireId:
    """Decoded SDK identity plus its presentation-only missing flag."""

    node_id: NodeId
    is_missing: bool


def node_wire_id(node_id: NodeId, *, is_missing: bool = False) -> str:
    """Encode one SDK node identity as canonical unpadded base64url JSON."""
    identity = [node_id.cluster_id, node_id.vhost, node_id.name, node_id.kind.value]
    encoded = json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()
    payload = base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")
    prefix = _MISSING_NODE_PREFIX if is_missing else _CAPTURED_NODE_PREFIX
    return prefix + payload


def decode_node_wire_id(value: str) -> DecodedNodeWireId:
    """Decode and validate one canonical captured or missing node wire ID."""
    if value.startswith(_CAPTURED_NODE_PREFIX):
        is_missing = False
        payload = value.removeprefix(_CAPTURED_NODE_PREFIX)
    elif value.startswith(_MISSING_NODE_PREFIX):
        is_missing = True
        payload = value.removeprefix(_MISSING_NODE_PREFIX)
    else:
        raise ValueError("node wire ID has an unsupported prefix")
    if not payload or "=" in payload:
        raise ValueError("node wire ID payload must be non-empty and unpadded")

    padding = "=" * (-len(payload) % 4)
    try:
        raw = base64.b64decode(
            (payload + padding).encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
        identity: object = json.loads(raw.decode("utf-8"))
    except (UnicodeEncodeError, UnicodeDecodeError, binascii.Error, json.JSONDecodeError) as exc:
        raise ValueError("node wire ID payload is not valid base64url JSON") from exc

    if not isinstance(identity, list) or len(identity) != _NODE_IDENTITY_FIELD_COUNT:
        raise ValueError("node wire ID must contain four identity fields")
    cluster_id, vhost, name, kind_value = identity
    if cluster_id is not None and not isinstance(cluster_id, str):
        raise ValueError("node wire ID cluster identity must be a string or null")
    if not isinstance(vhost, str) or not isinstance(name, str) or not isinstance(kind_value, str):
        raise ValueError("node wire ID resource identity fields must be strings")
    try:
        node_id = NodeId(
            cluster_id=cluster_id,
            vhost=vhost,
            name=name,
            kind=NodeKind(kind_value),
        )
    except (ValueError, TopologyValidationError) as exc:
        raise ValueError("node wire ID contains an invalid SDK identity") from exc

    decoded = DecodedNodeWireId(node_id=node_id, is_missing=is_missing)
    if node_wire_id(node_id, is_missing=is_missing) != value:
        raise ValueError("node wire ID is not canonically encoded")
    return decoded


@dataclass(frozen=True, slots=True)
class TopologyWireIds:
    """One topology's stable mapping reused by topology and result adapters."""

    edge_ids: Mapping[TopologyEdge, str]

    @classmethod
    def from_topology(cls, topology: ClusterTopology) -> TopologyWireIds:
        """Assign stable IDs using the SDK's complete canonical edge order."""
        edge_ids = {
            edge: f"{_EDGE_PREFIX}{index}"
            for index, edge in enumerate(sorted(topology.edges, key=edge_sort_key))
        }
        return cls(edge_ids=MappingProxyType(edge_ids))

    def edge_id(self, edge: TopologyEdge) -> str:
        """Return the stable ID assigned to one edge in this topology."""
        return self.edge_ids[edge]
