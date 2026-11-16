"""Seekable single-file index for one exact structural-cycle SDK result."""

import io
import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cycles.schemas import (
    CycleDetail,
    CyclePage,
    CyclePageQuery,
    CycleRunResponse,
    CycleRunResult,
    CycleSort,
    CycleSummary,
    RunErrorResponse,
    StoredCycle,
    StoredCycleMetadataEnvelope,
    StoredCycleSummary,
)
from jobs.models import AnalysisRun, OperationKind, ResultEnvelopeContext, RunStatus

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from typing import BinaryIO

    from jobs.results import ResultRepository

_MAGIC = b"RTVCYC1\x00"
_PREFIX = struct.Struct(">8sQQ")
_OFFSET_ENTRY = struct.Struct(">QQ")
_ORDER_ENTRY = struct.Struct(">Q")
_MAX_METADATA_BYTES = 65_536


@dataclass(frozen=True, slots=True)
class _Layout:
    """Validated absolute section positions within one committed result file."""

    cycle_count: int
    file_size: int
    summary_index_offset: int
    detail_index_offset: int
    length_asc_offset: int
    length_desc_offset: int
    data_offset: int


def _record_bytes(record: StoredCycle | StoredCycleSummary) -> bytes:
    return record.model_dump_json(by_alias=True).encode()


def _validate_encode_context(
    context: ResultEnvelopeContext,
    cycles: Sequence[StoredCycle],
    max_cycles: int,
) -> None:
    if context.operation is not OperationKind.STRUCTURAL_CYCLES:
        raise ValueError("cycle result context has the wrong operation")
    if dict(context.parameters) != {"maxCycles": max_cycles}:
        raise ValueError("cycle result parameters do not match maxCycles")
    if any(cycle.cycle_index != index for index, cycle in enumerate(cycles)):
        raise ValueError("stored cycles are not in original SDK index order")


def encode_cycle_result(
    context: ResultEnvelopeContext,
    cycles: Sequence[StoredCycle],
    *,
    truncated: bool,
    max_cycles: int,
) -> bytes:
    """Encode metadata, fixed-width indexes, and JSON records into one file."""
    _validate_encode_context(context, cycles, max_cycles)

    metadata = (
        StoredCycleMetadataEnvelope(
            schema_version=1,
            operation=OperationKind.STRUCTURAL_CYCLES,
            source=context.source,
            analysis_id=context.analysis_id,
            snapshot_revision=context.snapshot_revision,
            parameters=dict(context.parameters),
            result=CycleRunResult(
                cycle_count=len(cycles),
                truncated=truncated,
                max_cycles=max_cycles,
            ),
        )
        .model_dump_json(by_alias=True)
        .encode()
    )
    if not metadata or len(metadata) > _MAX_METADATA_BYTES:
        raise ValueError("cycle result metadata is too large")

    summary_records = tuple(
        _record_bytes(
            StoredCycleSummary(
                cycle_index=cycle.cycle_index,
                length=cycle.length,
                vhosts=cycle.vhosts,
                preview_node_ids=cycle.node_ids[:3],
            ),
        )
        for cycle in cycles
    )
    detail_records = tuple(_record_bytes(cycle) for cycle in cycles)
    cycle_count = len(cycles)
    data_offset = (
        _PREFIX.size
        + len(metadata)
        + cycle_count * _OFFSET_ENTRY.size * 2
        + cycle_count * _ORDER_ENTRY.size * 2
    )

    summary_offsets: list[tuple[int, int]] = []
    cursor = data_offset
    for record in summary_records:
        summary_offsets.append((cursor, len(record)))
        cursor += len(record)
    detail_offsets: list[tuple[int, int]] = []
    for record in detail_records:
        detail_offsets.append((cursor, len(record)))
        cursor += len(record)

    length_asc = sorted(
        range(cycle_count),
        key=lambda index: (cycles[index].length, index),
    )
    length_desc = sorted(
        range(cycle_count),
        key=lambda index: (-cycles[index].length, index),
    )

    output = io.BytesIO()
    output.write(_PREFIX.pack(_MAGIC, len(metadata), cycle_count))
    output.write(metadata)
    for offset, length in summary_offsets:
        output.write(_OFFSET_ENTRY.pack(offset, length))
    for offset, length in detail_offsets:
        output.write(_OFFSET_ENTRY.pack(offset, length))
    for cycle_index in length_asc:
        output.write(_ORDER_ENTRY.pack(cycle_index))
    for cycle_index in length_desc:
        output.write(_ORDER_ENTRY.pack(cycle_index))
    if output.tell() != data_offset:
        raise AssertionError("cycle result index layout drifted")
    for record in summary_records:
        output.write(record)
    for record in detail_records:
        output.write(record)
    return output.getvalue()


def _read_exact(result_file: BinaryIO, size: int) -> bytes:
    data = result_file.read(size)
    if len(data) != size:
        raise ValueError("committed cycle result is truncated")
    return data


def _layout(metadata_length: int, cycle_count: int, file_size: int) -> _Layout:
    if metadata_length <= 0 or metadata_length > _MAX_METADATA_BYTES:
        raise ValueError("committed cycle metadata length is invalid")
    summary_index_offset = _PREFIX.size + metadata_length
    detail_index_offset = summary_index_offset + cycle_count * _OFFSET_ENTRY.size
    length_asc_offset = detail_index_offset + cycle_count * _OFFSET_ENTRY.size
    length_desc_offset = length_asc_offset + cycle_count * _ORDER_ENTRY.size
    data_offset = length_desc_offset + cycle_count * _ORDER_ENTRY.size
    if data_offset > file_size:
        raise ValueError("committed cycle indexes exceed the result file")
    return _Layout(
        cycle_count=cycle_count,
        file_size=file_size,
        summary_index_offset=summary_index_offset,
        detail_index_offset=detail_index_offset,
        length_asc_offset=length_asc_offset,
        length_desc_offset=length_desc_offset,
        data_offset=data_offset,
    )


def _metadata(result_file: BinaryIO) -> tuple[_Layout, StoredCycleMetadataEnvelope]:
    result_file.seek(0, io.SEEK_END)
    file_size = result_file.tell()
    result_file.seek(0)
    magic, metadata_length, cycle_count = _PREFIX.unpack(_read_exact(result_file, _PREFIX.size))
    if magic != _MAGIC:
        raise ValueError("committed cycle result has an unsupported format")
    layout = _layout(metadata_length, cycle_count, file_size)
    envelope = StoredCycleMetadataEnvelope.model_validate_json(
        _read_exact(result_file, metadata_length),
    )
    if envelope.result.cycle_count != cycle_count:
        raise ValueError("committed cycle count does not match its indexes")
    return layout, envelope


def _validate_run_metadata(
    run: AnalysisRun,
    envelope: StoredCycleMetadataEnvelope,
) -> None:
    if (
        envelope.source is not run.source
        or envelope.analysis_id != run.analysis_id
        or envelope.snapshot_revision != run.snapshot_revision
        or envelope.parameters != {"maxCycles": envelope.result.max_cycles}
    ):
        raise ValueError("committed cycle result metadata does not match its run")


def read_cycle_metadata(
    run: AnalysisRun,
    repository: ResultRepository,
) -> StoredCycleMetadataEnvelope:
    """Read only the small metadata frame for one exact completed run."""
    with repository.open_reader(run.run_id) as result_file:
        _layout_value, envelope = _metadata(result_file)
    _validate_run_metadata(run, envelope)
    return envelope


def _offset_entry(
    result_file: BinaryIO,
    layout: _Layout,
    *,
    index_offset: int,
    cycle_index: int,
) -> tuple[int, int]:
    if cycle_index < 0 or cycle_index >= layout.cycle_count:
        raise IndexError(cycle_index)
    result_file.seek(index_offset + cycle_index * _OFFSET_ENTRY.size)
    offset, length = _OFFSET_ENTRY.unpack(_read_exact(result_file, _OFFSET_ENTRY.size))
    if length <= 0 or offset < layout.data_offset or offset + length > layout.file_size:
        raise ValueError("committed cycle record offset is invalid")
    return offset, length


def _summary(
    result_file: BinaryIO,
    layout: _Layout,
    cycle_index: int,
) -> StoredCycleSummary:
    offset, length = _offset_entry(
        result_file,
        layout,
        index_offset=layout.summary_index_offset,
        cycle_index=cycle_index,
    )
    result_file.seek(offset)
    summary = StoredCycleSummary.model_validate_json(_read_exact(result_file, length))
    if summary.cycle_index != cycle_index:
        raise ValueError("committed cycle summary has the wrong original index")
    return summary


def _ordered_cycle_indexes(
    result_file: BinaryIO,
    layout: _Layout,
    sort: CycleSort,
    *,
    start: int = 0,
    stop: int | None = None,
) -> Iterator[int]:
    positions = range(start, layout.cycle_count if stop is None else min(stop, layout.cycle_count))
    if sort is CycleSort.SDK:
        yield from positions
        return
    order_offset = (
        layout.length_asc_offset if sort is CycleSort.LENGTH_ASC else layout.length_desc_offset
    )
    for position in positions:
        result_file.seek(order_offset + position * _ORDER_ENTRY.size)
        (cycle_index,) = _ORDER_ENTRY.unpack(_read_exact(result_file, _ORDER_ENTRY.size))
        if cycle_index >= layout.cycle_count:
            raise ValueError("committed cycle ordering contains an invalid index")
        yield cycle_index


def _page_summary(summary: StoredCycleSummary) -> CycleSummary:
    return CycleSummary(
        cycle_index=summary.cycle_index,
        length=summary.length,
        vhosts=summary.vhosts,
        preview_node_ids=summary.preview_node_ids,
    )


def read_cycle_page(
    run: AnalysisRun,
    repository: ResultRepository,
    query: CyclePageQuery,
) -> CyclePage:
    """Seek unfiltered pages directly; scan filtered totals with bounded memory."""
    selected_vhosts = frozenset(query.vhost or ())
    items: list[CycleSummary] = []
    total = 0
    with repository.open_reader(run.run_id) as result_file:
        layout, envelope = _metadata(result_file)
        _validate_run_metadata(run, envelope)
        has_filters = (
            bool(selected_vhosts) or query.min_length is not None or query.max_length is not None
        )
        if not has_filters:
            total = layout.cycle_count
            for cycle_index in _ordered_cycle_indexes(
                result_file,
                layout,
                query.sort,
                start=query.offset,
                stop=query.offset + query.limit,
            ):
                items.append(_page_summary(_summary(result_file, layout, cycle_index)))
        else:
            for cycle_index in _ordered_cycle_indexes(result_file, layout, query.sort):
                summary = _summary(result_file, layout, cycle_index)
                if query.min_length is not None and summary.length < query.min_length:
                    continue
                if query.max_length is not None and summary.length > query.max_length:
                    continue
                if selected_vhosts and selected_vhosts.isdisjoint(summary.vhosts):
                    continue
                if total >= query.offset and len(items) < query.limit:
                    items.append(_page_summary(summary))
                total += 1
    return CyclePage(
        run_id=run.run_id,
        snapshot_revision=run.snapshot_revision,
        offset=query.offset,
        limit=query.limit,
        total=total,
        sort=query.sort,
        truncated=envelope.result.truncated,
        items=tuple(items),
    )


def read_cycle_detail(
    run: AnalysisRun,
    repository: ResultRepository,
    cycle_index: int,
) -> CycleDetail:
    """Seek directly to one complete cycle by its original SDK index."""
    with repository.open_reader(run.run_id) as result_file:
        layout, envelope = _metadata(result_file)
        _validate_run_metadata(run, envelope)
        offset, length = _offset_entry(
            result_file,
            layout,
            index_offset=layout.detail_index_offset,
            cycle_index=cycle_index,
        )
        result_file.seek(offset)
        cycle = StoredCycle.model_validate_json(_read_exact(result_file, length))
    if cycle.cycle_index != cycle_index:
        raise ValueError("committed cycle detail has the wrong original index")
    return CycleDetail(
        run_id=run.run_id,
        snapshot_revision=run.snapshot_revision,
        cycle_index=cycle.cycle_index,
        length=cycle.length,
        vhosts=cycle.vhosts,
        node_ids=cycle.node_ids,
        edge_ids=cycle.edge_ids,
    )


def cycle_run_response(run: AnalysisRun, repository: ResultRepository) -> CycleRunResponse:
    """Adapt run metadata while reading only the small cycle metadata frame."""
    result: CycleRunResult | None = None
    if run.status is RunStatus.SUCCEEDED:
        result = read_cycle_metadata(run, repository).result
    error = (
        RunErrorResponse(code=run.error.code, message=run.error.message)
        if run.error is not None
        else None
    )
    return CycleRunResponse(
        run_id=run.run_id,
        operation=OperationKind.STRUCTURAL_CYCLES,
        status=run.status,
        source=run.source,
        analysis_id=run.analysis_id,
        snapshot_revision=run.snapshot_revision,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        heartbeat_expires_at=run.heartbeat_expires_at,
        result_expires_at=run.result_expires_at,
        error=error,
        result=result,
    )
