"""The adapter between this domain and rabbitmq_management_sdk."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from rabbitmq_management_sdk import ClusterAuditor, TopologyError

if TYPE_CHECKING:
    from pathlib import Path

    from rabbitmq_management_sdk.topology import ClusterTopology


class DefinitionsLoadError(Exception):
    """Raised when the SDK cannot construct a topology from an export."""


@dataclass(frozen=True, slots=True)
class LoadedDefinitions:
    """Safe SDK output retained by one uploaded application snapshot."""

    topology: ClusterTopology
    vhost_count: int
    cluster_tags: dict[str, str]


def load_definitions(
    definitions_path: Path,
    *,
    queues_path: Path | None = None,
    exchanges_path: Path | None = None,
    in_cluster_amqp_hosts: tuple[str, ...] = (),
    cluster_label: str | None = None,
) -> LoadedDefinitions:
    """Load an uploaded capture through the SDK's public facade.

    Args:
        definitions_path: Local path to the RabbitMQ definitions export.
        queues_path: Optional complete queue observation capture.
        exchanges_path: Optional complete exchange observation capture.
        in_cluster_amqp_hosts: Normalized local shovel-host evidence.
        cluster_label: Optional application presentation label.

    Returns:
        The SDK's immutable topology graph and the export's vhost count.
    """
    try:
        auditor = ClusterAuditor.from_files(
            definitions_path=definitions_path,
            queues_path=queues_path,
            exchanges_path=exchanges_path,
            in_cluster_amqp_hosts=in_cluster_amqp_hosts,
            cluster_label=cluster_label,
        )
    except TopologyError as exc:
        raise DefinitionsLoadError("Could not load the RabbitMQ definitions export.") from exc

    return LoadedDefinitions(
        topology=auditor.topology,
        vhost_count=len(auditor.definitions.vhosts),
        cluster_tags=dict(auditor.definitions.cluster_tags or {}),
    )
