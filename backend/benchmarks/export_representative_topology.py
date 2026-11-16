"""Export the real-SDK representative topology as a safe browser fixture."""

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

from analyses.snapshots import (
    SnapshotMetadata,
    compute_snapshot_revision_from_files,
    create_analysis_snapshot,
)
from benchmarks.representative_capture import (
    VHOST_COUNT,
    load_and_validate,
    write_representative_definitions,
)
from core.state import AnalysisSource, CaptureCompleteness
from topology.sdk_bridge import serialize_topology

if TYPE_CHECKING:
    from collections.abc import Sequence


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="Safe topology JSON output path.")
    return parser.parse_args(argv)


def export_representative_topology(output_path: Path) -> Path:
    """Generate through the SDK, serialize through the app adapter, and write JSON."""
    with TemporaryDirectory(prefix="rabbitmq-topology-export-") as temporary_directory:
        definitions_path = write_representative_definitions(
            Path(temporary_directory) / "definitions.json",
        )
        auditor, _ = load_and_validate(definitions_path)
        revision = compute_snapshot_revision_from_files(
            definitions_path=definitions_path,
            queues_path=None,
            exchanges_path=None,
            in_cluster_amqp_hosts=(),
        )
        snapshot = create_analysis_snapshot(
            auditor.topology,
            SnapshotMetadata(
                analysis_id="representative-v1",
                source=AnalysisSource.STATIC,
                label="Representative v1 fixture",
                capture_completeness=CaptureCompleteness.DEFINITIONS_ONLY,
                snapshot_revision=revision,
                cluster_tags=dict(auditor.definitions.cluster_tags or {}),
                in_cluster_amqp_hosts=(),
                vhost_count=VHOST_COUNT,
            ),
        )
        payload = serialize_topology(snapshot).model_dump(mode="json", by_alias=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    return output_path


def main(argv: Sequence[str] | None = None) -> int:
    """Write one safe representative topology fixture."""
    args = _parse_args(argv)
    output_path = export_representative_topology(args.output.resolve())
    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
