import type { SccRunResponse, TopologyNode } from "../types/topology";

interface SccResultsProps {
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
          <p className="muted">SDK strongly connected components that contain a directed cycle.</p>
        </div>
        {selectedComponentIndexes.size > 0 && (
          <button type="button" className="text-button" onClick={onClearFocus}>
            Clear component focus
          </button>
        )}
      </div>

      <p className="operation-status">{statusText}</p>

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
                <summary>Show complete membership</summary>
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
