import type { ElkExtendedEdge, ElkNode } from "elkjs/lib/elk-api.js";
import { compareUnicodeCodePoints, type VisibleTopology } from "./presentation";

export interface LayoutResourceInput {
  id: string;
  vhost: string;
  width: number;
  height: number;
}

export interface LayoutEdgeInput {
  id: string;
  source: string;
  target: string;
}

export interface LayoutRequest {
  generation: number;
  resources: ReadonlyArray<LayoutResourceInput>;
  edges: ReadonlyArray<LayoutEdgeInput>;
}

export interface LayoutItem {
  id: string;
  parentId: string | null;
  x: number;
  y: number;
  width: number;
  height: number;
  isGroup: boolean;
}

export const RESOURCE_WIDTH = 220;
export const RESOURCE_HEIGHT = 82;

export function vhostGroupId(vhost: string): string {
  return `vhost:${encodeURIComponent(vhost)}`;
}

export function createLayoutRequest(generation: number, topology: VisibleTopology): LayoutRequest {
  return {
    generation,
    resources: topology.nodes.map((node) => ({
      id: node.id,
      vhost: node.data.vhost,
      width: RESOURCE_WIDTH,
      height: RESOURCE_HEIGHT,
    })),
    edges: topology.edges.map((edge) => ({
      id: edge.id,
      source: edge.source,
      target: edge.target,
    })),
  };
}

export function createElkGraph(request: LayoutRequest): ElkNode {
  const resourcesByVhost = new Map<string, LayoutRequest["resources"][number][]>();
  for (const resource of request.resources) {
    const resources = resourcesByVhost.get(resource.vhost) ?? [];
    resources.push(resource);
    resourcesByVhost.set(resource.vhost, resources);
  }

  const children: ElkNode[] = [...resourcesByVhost.entries()]
    .sort(([left], [right]) => compareUnicodeCodePoints(left, right))
    .map(([vhost, resources]) => ({
      id: vhostGroupId(vhost),
      layoutOptions: {
        "elk.algorithm": "layered",
        "elk.direction": "RIGHT",
        "elk.padding": "[top=54,left=24,bottom=24,right=24]",
        "elk.spacing.nodeNode": "34",
        "elk.layered.spacing.nodeNodeBetweenLayers": "56",
      },
      children: resources.map((resource) => ({
        id: resource.id,
        width: resource.width,
        height: resource.height,
      })),
    }));

  const edges: ElkExtendedEdge[] = request.edges.map((edge) => ({
    id: edge.id,
    sources: [edge.source],
    targets: [edge.target],
  }));
  return {
    id: "topology-root",
    layoutOptions: {
      "elk.algorithm": "layered",
      "elk.direction": "RIGHT",
      "elk.hierarchyHandling": "INCLUDE_CHILDREN",
      "elk.padding": "[top=32,left=32,bottom=32,right=32]",
      "elk.spacing.nodeNode": "72",
      "elk.layered.spacing.nodeNodeBetweenLayers": "96",
      "elk.layered.nodePlacement.strategy": "NETWORK_SIMPLEX",
    },
    children,
    edges,
  };
}

export function itemsFromElkGraph(graph: ElkNode): ReadonlyArray<LayoutItem> {
  const items: LayoutItem[] = [];
  for (const group of graph.children ?? []) {
    items.push({
      id: group.id,
      parentId: null,
      x: group.x ?? 0,
      y: group.y ?? 0,
      width: group.width ?? 0,
      height: group.height ?? 0,
      isGroup: true,
    });
    for (const resource of group.children ?? []) {
      items.push({
        id: resource.id,
        parentId: group.id,
        x: resource.x ?? 0,
        y: resource.y ?? 0,
        width: resource.width ?? 0,
        height: resource.height ?? 0,
        isGroup: false,
      });
    }
  }
  return items;
}
