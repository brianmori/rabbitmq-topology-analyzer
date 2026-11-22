import type { SccRunResponse, TopologyNode } from "../types/topology";

interface SccResultsProps {
  canRun: boolean;
  completedRun: SccRunResponse | null;
  statusText: string;
  selectedComponentIndexes: ReadonlySet<number>;
  nodes: ReadonlyArray<TopologyNode>;
  onComponentChange: (componentIndex: number, selected: boolean) => void;
  onClearFocus: () => void;
}

function nodeDescription(node: TopologyNode | undefined, nodeId: string): string {
  if (!node) {
    return nodeId;
  }
  return `${node.data.kind} ${node.data.name || "(default exchange)"} in ${node.data.vhost}`;
}

function nodePreview(node: TopologyNode | undefined, nodeId: string): string {
  return node ? node.data.name || "(default exchange)" : nodeId;
}

export function SccResults({
  canRun,
  completedRun,
  statusText,
  selectedComponentIndexes,
  nodes,
  onComponentChange,
  onClearFocus,
}: SccResultsProps) {
  const nodeById = new Map(nodes.map((node) => [node.id, node]));
  const components = completedRun?.result?.components ?? [];
  return (
    <section className="scc-results" aria-labelledby="scc-results-heading">
      <div className="result-heading-row">
        <div>
          <h3 id="scc-results-heading">Cyclic components</h3>
          <p className="muted">
            A cyclic component groups resources that can all reach each other along configured
            routes. If an exchange routes to two queues and both queues have dead-letter routes
            back to it, all three resources belong to the same component. A resource that routes
            directly back to itself can also form a component on its own.
          </p>
        </div>
        {selectedComponentIndexes.size > 0 && (
          <button type="button" className="text-button" onClick={onClearFocus}>
            Clear component filter
          </button>
        )}
      </div>

      <p className="operation-status">{statusText}</p>

      {canRun && !completedRun && !statusText && (
        <p className="result-empty">Choose Run cyclic components in Operations to see results here.</p>
      )}
      {completedRun?.status === "succeeded" && components.length === 0 && (
        <p className="result-empty">No cyclic components were found in this snapshot.</p>
      )}
      {components.length > 0 && (
        <div className="scc-component-list">
          {components.map((component) => (
            <article
              className={`scc-component-row scc-tone-${component.componentIndex % 6}`}
              key={component.componentIndex}
            >
              <label>
                <input
                  type="checkbox"
                  checked={selectedComponentIndexes.has(component.componentIndex)}
                  onChange={(event) =>
                    onComponentChange(component.componentIndex, event.target.checked)
                  }
                />
                <span className="scc-component-badge" aria-hidden="true">
                  C{component.componentIndex + 1}
                </span>
                <span>
                  <strong>Cyclic component {component.componentIndex + 1}</strong>
                  <small>
                    {component.nodeCount} nodes · Vhosts: {component.vhosts.join(", ")}
                  </small>
                  <small>
                    Members:{" "}
                    {component.nodeIds
                      .slice(0, 3)
                      .map((nodeId) => nodePreview(nodeById.get(nodeId), nodeId))
                      .join(", ")}
                    {component.nodeIds.length > 3 ? "…" : ""}
                  </small>
                </span>
              </label>
              <details>
                <summary>Show all resources</summary>
                <ul>
                  {component.nodeIds.map((nodeId) => (
                    <li key={nodeId}>{nodeDescription(nodeById.get(nodeId), nodeId)}</li>
                  ))}
                </ul>
              </details>
            </article>
          ))}
        </div>
      )}
    </section>
  );
}
