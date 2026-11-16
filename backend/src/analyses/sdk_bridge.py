"""Public SDK bridge for manifest-backed static capture loading."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from rabbitmq_management_sdk import ClusterAuditor, TopologyError

if TYPE_CHECKING:
    from pathlib import Path

    from rabbitmq_management_sdk.topology import ClusterTopology


class StaticAnalysisLoadError(Exception):
    """Raised when the SDK rejects a configured static capture."""


@dataclass(frozen=True, slots=True)
class LoadedStaticAnalysis:
    """Safe SDK output retained by the application snapshot."""

    topology: ClusterTopology
    vhost_count: int
    cluster_tags: dict[str, str]


def load_static_capture(
    definitions_path: Path,
    *,
    queues_path: Path | None,
    exchanges_path: Path | None,
    in_cluster_amqp_hosts: tuple[str, ...],
    cluster_label: str,
) -> LoadedStaticAnalysis:
    """Build one topology exclusively through the SDK's public facade."""
    try:
        auditor = ClusterAuditor.from_files(
            definitions_path=definitions_path,
            queues_path=queues_path,
            exchanges_path=exchanges_path,
            in_cluster_amqp_hosts=in_cluster_amqp_hosts,
            cluster_label=cluster_label,
        )
    except TopologyError as exc:
        raise StaticAnalysisLoadError("The SDK rejected a configured static capture.") from exc

    return LoadedStaticAnalysis(
        topology=auditor.topology,
        vhost_count=len(auditor.definitions.vhosts),
        cluster_tags=dict(auditor.definitions.cluster_tags or {}),
    )
