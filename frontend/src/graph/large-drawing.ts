import { absoluteItems, edgeDrawingPoints, type EdgeRouting, type Point } from "./edge-routing";
import type { TopologyFlowEdge, TopologyFlowNode } from "./flow-elements";
import type { LayoutEdgeInput, LayoutItem } from "./layout";
import type { VisibleTopology } from "./presentation";

export type ViewTransform = readonly [x: number, y: number, zoom: number];
export type DrawingSize = { width: number; height: number };

export interface DrawingGeometry {
  nodes: ReadonlyMap<string, LayoutItem>;
  edges: ReadonlyMap<string, ReadonlyArray<Point>>;
}

interface LargeDrawing extends DrawingGeometry {
  edgesByNode: ReadonlyMap<string, ReadonlyArray<LayoutEdgeInput>>;
}

// At this zoom a 220px card is under 40px wide.
export const DETAIL_ZOOM = 0.18;
const OVERSCAN = 80;

export function prepareLargeDrawing(
  topology: VisibleTopology,
  items: ReadonlyArray<LayoutItem>,
  routing: ReadonlyMap<string, EdgeRouting>,
): LargeDrawing {
  const nodes = absoluteItems(items);
  const edges = new Map<string, ReadonlyArray<Point>>();
  const edgesByNode = new Map<string, LayoutEdgeInput[]>();
  const addIncidentEdge = (id: string, edge: LayoutEdgeInput) => {
    const incident = edgesByNode.get(id);
    if (incident) incident.push(edge);
    else edgesByNode.set(id, [edge]);
  };
  for (const edge of topology.edges) {
    const source = nodes.get(edge.source);
    const target = nodes.get(edge.target);
    const route = routing.get(edge.id);
    if (!source || !target || !route) continue;
    edges.set(edge.id, edgeDrawingPoints(source, target, route));
    addIncidentEdge(edge.source, edge);
    if (edge.target !== edge.source) addIncidentEdge(edge.target, edge);
  }
  return { nodes, edges, edgesByNode };
}

/** Update the sparse drawing overrides from one batch of parent-relative positions. */
export function updateDraggedGeometry(
  drawing: LargeDrawing,
  current: DrawingGeometry,
  positions: ReadonlyMap<string, Point>,
  routing: ReadonlyMap<string, EdgeRouting>,
): DrawingGeometry {
  const nodes = new Map(current.nodes);
  const edges = new Map(current.edges);
  const affectedEdges = new Set<LayoutEdgeInput>();
  for (const [id, position] of positions) {
    const node = drawing.nodes.get(id)!;
    const parent = node.parentId ? drawing.nodes.get(node.parentId) : undefined;
    const x = position.x + (parent?.x ?? 0);
    const y = position.y + (parent?.y ?? 0);
    if (x === node.x && y === node.y) nodes.delete(id);
    else nodes.set(id, { ...node, x, y });
    for (const edge of drawing.edgesByNode.get(id) ?? []) affectedEdges.add(edge);
  }
  // Both endpoints can move in one batch. Use their final positions and
  // calculate a single path per edge, including parallel edges and self-loops.
  for (const edge of affectedEdges) {
    const source = nodes.get(edge.source);
    const target = nodes.get(edge.target);
    if (!source && !target) {
      edges.delete(edge.id);
      continue;
    }
    edges.set(edge.id, edgeDrawingPoints(
      source ?? drawing.nodes.get(edge.source)!,
      target ?? drawing.nodes.get(edge.target)!,
      routing.get(edge.id)!,
    ));
  }
  return { nodes, edges };
}

export function onscreenElements(
  drawing: DrawingGeometry,
  draggedDrawing: DrawingGeometry,
  nodes: TopologyFlowNode[],
  edges: TopologyFlowEdge[],
  [x, y, zoom]: ViewTransform,
  { width, height }: DrawingSize,
) {
  const left = (-x - OVERSCAN) / zoom;
  const top = (-y - OVERSCAN) / zoom;
  const right = (width - x + OVERSCAN) / zoom;
  const bottom = (height - y + OVERSCAN) / zoom;
  const overlaps = (x1: number, y1: number, x2: number, y2: number) =>
    Math.min(x1, x2) <= right && Math.max(x1, x2) >= left &&
    Math.min(y1, y2) <= bottom && Math.max(y1, y2) >= top;
  const visibleEdges = edges.filter((edge) => {
    const points = draggedDrawing.edges.get(edge.id) ?? drawing.edges.get(edge.id);
    return points?.some((end, index) => {
      if (index === 0) return false;
      const start = points[index - 1]!;
      return overlaps(start.x, start.y, end.x, end.y);
    });
  });
  const endpointIds = new Set(visibleEdges.flatMap((edge) => [edge.source, edge.target]));
  for (const id of endpointIds) {
    const parentId = drawing.nodes.get(id)?.parentId;
    if (parentId) endpointIds.add(parentId);
  }
  const visibleNodes = nodes.flatMap((node) => {
    const item = draggedDrawing.nodes.get(node.id) ?? drawing.nodes.get(node.id)!;
    if (overlaps(item.x, item.y, item.x + item.width, item.y + item.height)) return [node];
    // Keep the endpoint geometry for a route crossing the viewport whose
    // resource card is outside it. Its vhost parent still supplies its offset.
    return endpointIds.has(node.id) ? [{ ...node, hidden: true }] : [];
  });
  return { nodes: visibleNodes, edges: visibleEdges };
}
