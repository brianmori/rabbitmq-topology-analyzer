import type {
  CycleDetail,
  CyclePage,
  CycleRunResponse,
  CycleSort,
  TopologyEdge,
  TopologyNode,
} from "../types/topology";

interface CycleResultsProps {
  canRun: boolean;
  completedRun: CycleRunResponse | null;
  runStatusText: string;
  page: CyclePage | null;
  pageLoading: boolean;
  pageStatusText: string;
  sort: CycleSort;
  minLength: string;
  maxLength: string;
  selectedDetail: CycleDetail | null;
  detailStatusText: string;
  nodes: ReadonlyArray<TopologyNode>;
  edges: ReadonlyArray<TopologyEdge>;
  onSortChange: (sort: CycleSort) => void;
  onMinLengthChange: (value: string) => void;
  onMaxLengthChange: (value: string) => void;
  onPageOffsetChange: (offset: number) => void;
  onSelectCycle: (cycleIndex: number) => void;
  onShowGraph: () => void;
  onClearFocus: () => void;
}

function nodeDescription(node: TopologyNode | undefined, nodeId: string): string {
  if (!node) return nodeId;
  return `${node.data.kind} ${node.data.name || "(default exchange)"} in ${node.data.vhost}`;
}

function nodePreview(node: TopologyNode | undefined, nodeId: string): string {
  return node ? node.data.name || "(default exchange)" : nodeId;
}

function edgeDescription(edge: TopologyEdge | undefined, edgeId: string): string {
  if (!edge) return `edge ${edgeId}`;
  const kind = edge.data.kind.replaceAll("_", " ");
  const routingKey =
    edge.data.routingKey === null ? "" : `, routing key ${edge.data.routingKey || "(empty)"}`;
  return `${kind} edge ${edge.id}${routingKey}`;
}

export function CycleResults({
  canRun,
  completedRun,
  runStatusText,
  page,
  pageLoading,
  pageStatusText,
  sort,
  minLength,
  maxLength,
  selectedDetail,
  detailStatusText,
  nodes,
  edges,
  onSortChange,
  onMinLengthChange,
  onMaxLengthChange,
  onPageOffsetChange,
  onSelectCycle,
  onShowGraph,
  onClearFocus,
}: CycleResultsProps) {
  const nodeById = new Map(nodes.map((node) => [node.id, node]));
  const edgeById = new Map(edges.map((edge) => [edge.id, edge]));
  const result = completedRun?.result;
  const currentEnd = page ? Math.min(page.offset + page.items.length, page.total) : 0;
  return (
    <section className="cycle-results" aria-labelledby="cycle-results-heading">
      <div className="result-heading-row">
        <div>
          <h3 id="cycle-results-heading">Structural cycles</h3>
          <p className="muted">
            A structural cycle is a configured route that returns to its starting resource. If an
            exchange routes to two queues and both queues have dead-letter routes back to it, each
            round trip is a separate cycle. Actual message flow depends on runtime conditions.
          </p>
        </div>
      </div>

      <p className="operation-status">{runStatusText}</p>

      {canRun && !completedRun && !runStatusText && (
        <p className="result-empty">Choose Run structural cycles in Operations to see results here.</p>
      )}
      {result && (
        <>
          {result.truncated && (
            <p className="result-warning">
              {result.crossVhostCount === undefined
                ? "The SDK stopped at the configured maximum, and more cycles may exist."
                : "More single-vhost cycles exist. The cross-vhost results are complete."}
            </p>
          )}
          <div className="cycle-filter-grid">
            <label>
              Sort summaries
              <select
                value={sort}
                onChange={(event) => onSortChange(event.target.value as CycleSort)}
              >
                <option value="sdk">
                  {result.crossVhostCount === undefined ? "SDK order" : "Cross-vhost first"}
                </option>
                <option value="length_asc">Length, shortest first</option>
                <option value="length_desc">Length, longest first</option>
              </select>
            </label>
            <label>
              Minimum length
              <input
                type="number"
                min="1"
                step="1"
                inputMode="numeric"
                value={minLength}
                onChange={(event) => onMinLengthChange(event.target.value)}
              />
            </label>
            <label>
              Maximum length
              <input
                type="number"
                min="1"
                step="1"
                inputMode="numeric"
                value={maxLength}
                onChange={(event) => onMaxLengthChange(event.target.value)}
              />
            </label>
          </div>
          <p className="operation-status">{pageStatusText}</p>

          {page && page.items.length === 0 && !pageLoading && (
            <p className="result-empty">No cycles match the current page filters.</p>
          )}
          {page && page.items.length > 0 && (
            <div className="cycle-summary-list">
              {page.items.map((cycle) => (
                <button
                  type="button"
                  key={cycle.cycleIndex}
                  className="cycle-summary-row"
                  aria-pressed={selectedDetail?.cycleIndex === cycle.cycleIndex}
                  onClick={() => onSelectCycle(cycle.cycleIndex)}
                >
                  <strong>Cycle {cycle.cycleIndex + 1}</strong>
                  <span>
                    {cycle.length} hops · Vhosts: {cycle.vhosts.join(", ")}
                  </span>
                  <small>
                    {cycle.previewNodeIds
                      .map((nodeId) => nodePreview(nodeById.get(nodeId), nodeId))
                      .join(" → ")}
                    {cycle.length > cycle.previewNodeIds.length ? " → …" : ""}
                  </small>
                </button>
              ))}
              <div className="cycle-pagination" aria-label="Cycle summary pages">
                <button
                  type="button"
                  disabled={page.offset === 0 || pageLoading}
                  onClick={() => onPageOffsetChange(Math.max(0, page.offset - page.limit))}
                >
                  Previous
                </button>
                <span>
                  {page.total === 0 ? 0 : page.offset + 1}–{currentEnd} of {page.total}
                </span>
                <button
                  type="button"
                  disabled={page.offset + page.limit >= page.total || pageLoading}
                  onClick={() => onPageOffsetChange(page.offset + page.limit)}
                >
                  Next
                </button>
              </div>
            </div>
          )}
        </>
      )}

      <p className="operation-status">{detailStatusText}</p>
      {selectedDetail && (
        <article
          className="cycle-detail"
          aria-label={`Cycle ${selectedDetail.cycleIndex + 1} path`}
        >
          <h4>Cycle {selectedDetail.cycleIndex + 1} ordered path</h4>
          <p>
            {selectedDetail.length} hops across {selectedDetail.vhosts.join(", ")}
          </p>
          <div className="cycle-detail-actions">
            <button type="button" className="text-button" onClick={onShowGraph}>
              Show cycle on graph
            </button>
            <button type="button" className="text-button" onClick={onClearFocus}>
              Clear cycle filter
            </button>
          </div>
          <ol>
            {selectedDetail.edgeIds.map((edgeId, index) => {
              const sourceId = selectedDetail.nodeIds[index] ?? edgeById.get(edgeId)?.source ?? "";
              const targetId =
                selectedDetail.nodeIds[(index + 1) % selectedDetail.nodeIds.length] ??
                edgeById.get(edgeId)?.target ??
                "";
              return (
                <li key={`${index}:${edgeId}`}>
                  <strong>Hop {index + 1}:</strong>{" "}
                  {nodeDescription(nodeById.get(sourceId), sourceId)} —{" "}
                  {edgeDescription(edgeById.get(edgeId), edgeId)} →{" "}
                  {nodeDescription(nodeById.get(targetId), targetId)}
                  {index === selectedDetail.edgeIds.length - 1 ? " (closes the cycle)" : ""}
                </li>
              );
            })}
          </ol>
        </article>
      )}
    </section>
  );
}
