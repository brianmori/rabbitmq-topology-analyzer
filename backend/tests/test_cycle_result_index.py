from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from core.session import OwnerContext
from core.state import AnalysisSource
from cycles import result_index
from cycles.result_index import (
    cycle_run_response,
    encode_cycle_result,
    read_cycle_detail,
    read_cycle_page,
)
from cycles.schemas import CyclePage, CyclePageQuery, CycleSort, CycleSummary, StoredCycle
from jobs.models import AnalysisRun, OperationKind, ResultEnvelopeContext, RunStatus
from jobs.results import ResultRepository

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path
    from typing import BinaryIO


@dataclass
class _ReaderOperations:
    read_sizes: list[int] = field(default_factory=list)
    seeks: list[tuple[int, int]] = field(default_factory=list)
    ordered_indexes: list[int] = field(default_factory=list)


class _TrackedReader:
    def __init__(self, result_file: BinaryIO, operations: _ReaderOperations) -> None:
        self._result_file = result_file
        self._operations = operations

    def read(self, size: int = -1) -> bytes:
        self._operations.read_sizes.append(size)
        return self._result_file.read(size)

    def seek(self, offset: int, whence: int = 0) -> int:
        self._operations.seeks.append((offset, whence))
        return self._result_file.seek(offset, whence)

    def tell(self) -> int:
        return self._result_file.tell()


def _track_reader(
    repository: ResultRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> _ReaderOperations:
    operations = _ReaderOperations()
    original_open_reader = repository.open_reader
    original_indexes = result_index._ordered_cycle_indexes

    @contextmanager
    def tracked_reader(requested_run_id: str) -> Iterator[_TrackedReader]:
        with original_open_reader(requested_run_id) as result_file:
            yield _TrackedReader(result_file, operations)

    def tracked_indexes(
        result_file: BinaryIO,
        layout: result_index._Layout,
        sort: CycleSort,
        **bounds: int,
    ) -> Iterator[int]:
        for cycle_index in original_indexes(result_file, layout, sort, **bounds):
            operations.ordered_indexes.append(cycle_index)
            yield cycle_index

    monkeypatch.setattr(repository, "open_reader", tracked_reader)
    monkeypatch.setattr(result_index, "_ordered_cycle_indexes", tracked_indexes)
    return operations


def _cycle(
    cycle_index: int,
    length: int,
    *vhosts: str,
) -> StoredCycle:
    return StoredCycle(
        cycle_index=cycle_index,
        length=length,
        vhosts=tuple(vhosts),
        node_ids=tuple(f"n1.node-{cycle_index}-{position}" for position in range(length)),
        edge_ids=tuple(f"e1.{cycle_index}-{position}" for position in range(length)),
    )


def _committed_index(
    tmp_path: Path,
    cycles: Sequence[StoredCycle] | None = None,
) -> tuple[AnalysisRun, ResultRepository]:
    context = ResultEnvelopeContext(
        operation=OperationKind.STRUCTURAL_CYCLES,
        source=AnalysisSource.STATIC,
        analysis_id="indexed-cycles",
        snapshot_revision="sha256:" + "a" * 64,
        parameters={"maxCycles": 0},
    )
    if cycles is None:
        cycles = (
            _cycle(0, 2, "/a"),
            _cycle(1, 2, "/b"),
            _cycle(2, 3, "/a", "/c"),
            _cycle(3, 2, "/d"),
        )
    payload = encode_cycle_result(context, cycles, truncated=False, max_cycles=0)
    run_id = str(uuid4())
    repository = ResultRepository(tmp_path / "results", len(payload) + 1)
    write_spec = repository.prepare(run_id)
    assert repository.reserve(run_id, len(payload)) is True
    write_spec.committed_path.write_bytes(payload)
    repository.commit(run_id)
    now = datetime.now(UTC)
    run = AnalysisRun(
        run_id=run_id,
        owner_context=OwnerContext("test-principal", str(uuid4())),
        operation=OperationKind.STRUCTURAL_CYCLES,
        status=RunStatus.SUCCEEDED,
        source=context.source,
        analysis_id=context.analysis_id,
        snapshot_revision=context.snapshot_revision,
        created_at=now,
        started_at=now,
        finished_at=now,
        heartbeat_expires_at=None,
        result_expires_at=now,
        error=None,
    )
    return run, repository


def test_indexed_reads_never_use_the_whole_result_reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, repository = _committed_index(tmp_path)

    def unexpected_whole_read(_run_id: str) -> bytes:
        pytest.fail("cycle status, page, and detail must not read the whole result file")

    monkeypatch.setattr(repository, "read", unexpected_whole_read)

    response = cycle_run_response(run, repository)
    page = read_cycle_page(
        run,
        repository,
        CyclePageQuery(offset=1, limit=2, sort=CycleSort.LENGTH_ASC),
    )
    detail = read_cycle_detail(run, repository, 3)

    assert response.result is not None
    assert response.result.cycle_count == 4
    assert page.total == 4
    assert [item.cycle_index for item in page.items] == [1, 3]
    assert detail.cycle_index == 3
    assert detail.node_ids == ("n1.node-3-0", "n1.node-3-1")


def test_large_unfiltered_page_and_detail_never_read_the_complete_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cycle_count = 5_000
    run, repository = _committed_index(
        tmp_path,
        tuple(_cycle(index, 2, "/large") for index in range(cycle_count)),
    )
    operations = _track_reader(repository, monkeypatch)

    response = cycle_run_response(run, repository)
    page = read_cycle_page(
        run,
        repository,
        CyclePageQuery(offset=cycle_count - 1, limit=1),
    )
    detail = read_cycle_detail(run, repository, cycle_count - 1)

    assert response.result is not None
    assert response.result.cycle_count == cycle_count
    assert [item.cycle_index for item in page.items] == [cycle_count - 1]
    assert detail.cycle_index == cycle_count - 1
    assert -1 not in operations.read_sizes
    file_size = (repository.root / f"{run.run_id}.json").stat().st_size
    assert sum(operations.read_sizes) < file_size // 10


@pytest.mark.parametrize("sort", list(CycleSort))
@pytest.mark.parametrize(
    ("offset", "limit"),
    [(0, 1), (9_999, 1), (5_000, 100), (9_998, 100), (10_000, 1), (10_001, 1), (10**100, 1)],
)
def test_unfiltered_pages_only_traverse_and_read_the_requested_interval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sort: CycleSort,
    offset: int,
    limit: int,
) -> None:
    # Synthetic stored records isolate index access from SDK enumeration cost.
    cycles = tuple(_cycle(index, 1 + index % 5, "/large") for index in range(10_000))
    run, repository = _committed_index(tmp_path, cycles)
    expected_order = list(range(len(cycles)))
    if sort is CycleSort.LENGTH_ASC:
        expected_order.sort(key=lambda index: (cycles[index].length, index))
    elif sort is CycleSort.LENGTH_DESC:
        expected_order.sort(key=lambda index: (-cycles[index].length, index))
    expected_indexes = expected_order[offset : offset + limit]
    operations = _track_reader(repository, monkeypatch)

    page = read_cycle_page(run, repository, CyclePageQuery(offset=offset, limit=limit, sort=sort))

    assert page == CyclePage(
        run_id=run.run_id,
        snapshot_revision=run.snapshot_revision,
        offset=offset,
        limit=limit,
        total=len(cycles),
        sort=sort,
        truncated=False,
        items=tuple(
            CycleSummary(
                cycle_index=index,
                length=cycles[index].length,
                vhosts=cycles[index].vhosts,
                preview_node_ids=cycles[index].node_ids[:3],
            )
            for index in expected_indexes
        ),
    )
    # SDK order has no file-backed order entries, so count yielded indexes too.
    assert operations.ordered_indexes == expected_indexes
    per_item_operations = 2 if sort is CycleSort.SDK else 3
    assert len(operations.read_sizes) <= 2 + per_item_operations * len(expected_indexes)
    assert len(operations.seeks) <= 2 + per_item_operations * len(expected_indexes)
    assert -1 not in operations.read_sizes
    for index in expected_indexes:
        detail = read_cycle_detail(run, repository, index)
        assert detail.cycle_index == index
        assert detail.node_ids == cycles[index].node_ids
        assert detail.edge_ids == cycles[index].edge_ids


@pytest.mark.parametrize("sort", list(CycleSort))
@pytest.mark.parametrize("offset", [0, 1, 4])
@pytest.mark.parametrize(
    ("filters", "matching_indexes"),
    [
        ({"vhost": ["/b", "/c"]}, [1, 2]),
        ({"min_length": 3}, [2]),
        ({"max_length": 2}, [0, 1, 3]),
        ({"vhost": ["/b", "/c"], "min_length": 2, "max_length": 2}, [1]),
        ({"vhost": ["/missing"]}, []),
    ],
)
def test_filtered_pages_scan_all_summaries_to_count_matches_before_paging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sort: CycleSort,
    offset: int,
    filters: dict[str, object],
    matching_indexes: list[int],
) -> None:
    run, repository = _committed_index(tmp_path)
    operations = _track_reader(repository, monkeypatch)
    expected_orders = {
        CycleSort.SDK: [0, 1, 2, 3],
        CycleSort.LENGTH_ASC: [0, 1, 3, 2],
        CycleSort.LENGTH_DESC: [2, 0, 1, 3],
    }
    expected_indexes = [index for index in expected_orders[sort] if index in matching_indexes]
    query = CyclePageQuery.model_validate({"sort": sort, "offset": offset, "limit": 1, **filters})

    page = read_cycle_page(run, repository, query)

    assert page.total == len(matching_indexes)
    assert page.offset == offset
    assert page.limit == 1
    assert page.sort is sort
    assert [item.cycle_index for item in page.items] == expected_indexes[offset : offset + 1]
    assert operations.ordered_indexes == expected_orders[sort]
    per_item_operations = 2 if sort is CycleSort.SDK else 3
    assert len(operations.read_sizes) == 2 + per_item_operations * 4
    assert len(operations.seeks) == 2 + per_item_operations * 4


@pytest.mark.parametrize("sort", list(CycleSort))
def test_empty_unfiltered_index_only_reads_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sort: CycleSort,
) -> None:
    run, repository = _committed_index(tmp_path, ())
    operations = _track_reader(repository, monkeypatch)

    page = read_cycle_page(run, repository, CyclePageQuery(sort=sort))

    assert page.total == 0
    assert page.items == ()
    assert operations.ordered_indexes == []
    assert len(operations.read_sizes) == len(operations.seeks) == 2


@pytest.mark.parametrize("sort", [CycleSort.LENGTH_ASC, CycleSort.LENGTH_DESC])
def test_unfiltered_page_validates_accessed_order_entries(
    tmp_path: Path,
    sort: CycleSort,
) -> None:
    run, repository = _committed_index(tmp_path)
    result_path = repository.root / f"{run.run_id}.json"
    with result_path.open("r+b") as result_file:
        layout, _envelope = result_index._metadata(result_file)
        order_offset = (
            layout.length_asc_offset if sort is CycleSort.LENGTH_ASC else layout.length_desc_offset
        )
        result_file.seek(order_offset)
        result_file.write(result_index._ORDER_ENTRY.pack(layout.cycle_count))

    # A deep page does not access the damaged first entry.
    page = read_cycle_page(run, repository, CyclePageQuery(offset=3, limit=1, sort=sort))
    assert [item.cycle_index for item in page.items] == [2 if sort is CycleSort.LENGTH_ASC else 3]
    with pytest.raises(ValueError, match="ordering contains an invalid index"):
        read_cycle_page(run, repository, CyclePageQuery(limit=1, sort=sort))


@pytest.mark.parametrize("sort", list(CycleSort))
def test_out_of_range_page_still_validates_run_metadata(tmp_path: Path, sort: CycleSort) -> None:
    run, repository = _committed_index(tmp_path)
    stale_run = replace(run, snapshot_revision="sha256:" + "b" * 64)

    with pytest.raises(ValueError, match="metadata does not match its run"):
        read_cycle_page(stale_run, repository, CyclePageQuery(offset=4, sort=sort))


def test_indexed_page_uses_or_vhosts_and_deterministic_length_orders(tmp_path: Path) -> None:
    run, repository = _committed_index(tmp_path)

    selected_vhosts = read_cycle_page(
        run,
        repository,
        CyclePageQuery(vhost=["/b", "/c"]),
    )
    descending = read_cycle_page(
        run,
        repository,
        CyclePageQuery(sort=CycleSort.LENGTH_DESC),
    )

    assert selected_vhosts.total == 2
    assert [item.cycle_index for item in selected_vhosts.items] == [1, 2]
    assert [item.cycle_index for item in descending.items] == [2, 0, 1, 3]


def test_indexed_detail_rejects_out_of_range_original_index(tmp_path: Path) -> None:
    run, repository = _committed_index(tmp_path)

    with pytest.raises(IndexError):
        read_cycle_detail(run, repository, 4)


def test_indexed_metadata_rejects_a_truncated_committed_file(tmp_path: Path) -> None:
    run, repository = _committed_index(tmp_path)
    result_path = repository.root / f"{run.run_id}.json"
    result_path.write_bytes(result_path.read_bytes()[:16])

    with pytest.raises(ValueError, match="truncated"):
        cycle_run_response(run, repository)
