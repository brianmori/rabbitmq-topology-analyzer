import {
  BaseEdge,
  EdgeLabelRenderer,
  Handle,
  Position,
  getSmoothStepPath,
  type Edge,
  type EdgeProps,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import type { TopologyEdge, TopologyNode, TopologyNodeKind } from "../types/topology";
import { routedEdgeGeometry, type EdgeRouting } from "./edge-routing";

export type ResourceFlowData = Record<string, unknown> & {
  wireNode: TopologyNode;
  searchMatch: boolean;
  componentIndex: number | null;
  cycleStep: number | null;
  onSelect: (id: string) => void;
};

export type VhostGroupData = Record<string, unknown> & {
  vhost: string;
};

export type ResourceFlowNode = Node<ResourceFlowData, TopologyNodeKind>;
export type VhostGroupNode = Node<VhostGroupData, "vhostGroup">;
export type TopologyFlowNode = ResourceFlowNode | VhostGroupNode;

export type TopologyFlowEdgeData = Record<string, unknown> & {
  wireEdge: TopologyEdge;
  componentIndex: number | null;
  cycleStep: number | null;
  cycleAnimated: boolean;
  routing: EdgeRouting;
  inspectionLabel: string;
  onSelect: (id: string) => void;
};
export type TopologyFlowEdge = Edge<TopologyFlowEdgeData, "topologyEdge">;

const NODE_SYMBOLS: Readonly<Record<TopologyNodeKind, string>> = {
  exchange: "◇",
  queue: "▤",
  shovel: "⇢",
};

function ResourceNodeRenderer({ data, selected }: NodeProps<ResourceFlowNode>) {
  const node = data.wireNode;
  const displayName = node.data.name || "(default exchange)";
  return (
    <div className="resource-node-shell">
      <Handle type="target" position={Position.Left} className="resource-handle" />
      <button
        type="button"
        aria-label={`Inspect ${node.data.isMissing ? "missing " : ""}${node.data.kind} ${displayName}, vhost ${node.data.vhost}`}
        onClick={() => data.onSelect(node.id)}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            event.stopPropagation();
            data.onSelect(node.id);
          }
        }}
        className={[
          "resource-node",
          `resource-node--${node.data.kind}`,
          node.data.isMissing ? "resource-node--missing" : "",
          data.searchMatch ? "resource-node--search-match" : "",
          data.componentIndex !== null ? `scc-tone-${data.componentIndex % 6}` : "",
          data.componentIndex !== null ? "resource-node--scc" : "",
          data.cycleStep !== null ? "resource-node--cycle" : "",
          selected ? "resource-node--selected" : "",
        ]
          .filter(Boolean)
          .join(" ")}
      >
        <span className="resource-node__symbol" aria-hidden="true">
          {node.data.isMissing ? "⚠" : NODE_SYMBOLS[node.data.kind]}
        </span>
        <span className="resource-node__copy">
          <strong>{displayName}</strong>
          <span>{node.data.isMissing ? `MISSING ${node.data.kind}` : node.data.kind}</span>
        </span>
        {data.cycleStep !== null ? (
          <span className="resource-node__cycle-step">P{data.cycleStep + 1}</span>
        ) : null}
      </button>
      <Handle type="source" position={Position.Right} className="resource-handle" />
    </div>
  );
}

export const ExchangeNodeRenderer = ResourceNodeRenderer;
export const QueueNodeRenderer = ResourceNodeRenderer;
export const ShovelNodeRenderer = ResourceNodeRenderer;

export function VhostGroupRenderer({ data }: NodeProps<VhostGroupNode>) {
  return (
    <div className="vhost-group__label">
      <span>Vhost</span>
      <strong>{data.vhost}</strong>
    </div>
  );
}

export function TopologyEdgeRenderer({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  markerEnd,
  selected,
  data,
}: EdgeProps<TopologyFlowEdge>) {
  const routed = data
    ? routedEdgeGeometry(
        { x: sourceX, y: sourceY },
        { x: targetX, y: targetY },
        data.routing,
      )
    : null;
  const [path, labelX, labelY] = routed
    ? ([routed.path, routed.label.x, routed.label.y] as const)
    : getSmoothStepPath({
        sourceX,
        sourceY,
        targetX,
        targetY,
        sourcePosition,
        targetPosition,
        borderRadius: 12,
      });
  const kind = data?.wireEdge.data.kind ?? "binding";
  const componentClass =
    data?.componentIndex !== null && data?.componentIndex !== undefined
      ? ` topology-edge--scc scc-tone-${data.componentIndex % 6}`
      : "";
  const cycleClass =
    data?.cycleStep !== null && data?.cycleStep !== undefined
      ? ` topology-edge--cycle${data.cycleAnimated ? " topology-edge--cycle-animated" : ""}`
      : "";
  return (
    <>
      <BaseEdge
        id={id}
        path={path}
        markerEnd={markerEnd}
        className={`topology-edge topology-edge--${kind}${componentClass}${cycleClass}${selected ? " topology-edge--selected" : ""}`}
        interactionWidth={24}
      />
      <EdgeLabelRenderer>
        <button
          type="button"
          className={`edge-inspect-button nodrag nopan${selected ? " edge-inspect-button--selected" : ""}`}
          style={{ transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)` }}
          data-edge-id={id}
          aria-label={data?.inspectionLabel}
          title={data?.inspectionLabel}
          onClick={() => data?.onSelect(id)}
          onKeyDown={(event) => {
            if (event.key === "Enter" || event.key === " ") {
              event.preventDefault();
              event.stopPropagation();
              data?.onSelect(id);
            }
          }}
        >
          {data?.cycleStep !== null && data?.cycleStep !== undefined
            ? `P${data.cycleStep + 1}`
            : "↗"}
        </button>
      </EdgeLabelRenderer>
    </>
  );
}
