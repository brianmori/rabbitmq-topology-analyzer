import type { TopologyEdge, TopologyNode } from "../types/topology";
import { safeEdgeDetailRows, safeEdgeSummary, safeNodeDetailRows } from "./details";

export type GraphSelection = { kind: "node"; id: string } | { kind: "edge"; id: string } | null;

interface DetailsPanelProps {
  selection: GraphSelection;
  nodes: ReadonlyArray<TopologyNode>;
  edges: ReadonlyArray<TopologyEdge>;
}

export function DetailsPanel({ selection, nodes, edges }: DetailsPanelProps) {
  const selectedNode =
    selection?.kind === "node" ? nodes.find((node) => node.id === selection.id) : undefined;
  const selectedEdge =
    selection?.kind === "edge" ? edges.find((edge) => edge.id === selection.id) : undefined;
  const heading = selectedNode ? "Resource details" : selectedEdge ? "Relationship details" : "Details";
  const rows = selectedNode
    ? safeNodeDetailRows(selectedNode)
    : selectedEdge
      ? safeEdgeDetailRows(selectedEdge, nodes)
      : [];

  return (
    <section className="details-panel" aria-labelledby="details-heading">
      <h3 id="details-heading">{heading}</h3>
      {selectedEdge && (
        <p className="relationship-summary">{safeEdgeSummary(selectedEdge, nodes)}</p>
      )}
      {rows.length === 0 ? (
        <p className="muted">Select a resource or relationship to inspect it.</p>
      ) : (
        <dl className="details-list">
          {rows.map((row) => (
            <div key={row.label}>
              <dt>{row.label}</dt>
              <dd>{row.value}</dd>
            </div>
          ))}
        </dl>
      )}
    </section>
  );
}
