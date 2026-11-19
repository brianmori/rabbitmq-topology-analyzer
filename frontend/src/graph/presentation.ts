import type {
  CycleDetail,
  SccComponent,
  TopologyEdge,
  TopologyNode,
  TopologyNodeKind,
  TopologyResponse,
} from "../types/topology";

export interface VisibleTopology {
  nodes: ReadonlyArray<TopologyNode>;
  edges: ReadonlyArray<TopologyEdge>;
}

/** Focuses the cached graph on exactly one SDK-provided cycle path. */
export function deriveCycleVisibleTopology(
  topology: TopologyResponse,
  detail: CycleDetail,
): VisibleTopology {
  const nodeIds = new Set(detail.nodeIds);
  const edgeIds = new Set(detail.edgeIds);
  return {
    nodes: topology.nodes.filter((node) => nodeIds.has(node.id)),
    edges: topology.edges.filter((edge) => edgeIds.has(edge.id)),
  };
}

/** Maps exact cycle members to their ordered path positions. */
export function cycleSteps(detail: CycleDetail | null): {
  nodes: ReadonlyMap<string, number>;
  edges: ReadonlyMap<string, number>;
} {
  if (!detail) return { nodes: new Map(), edges: new Map() };
  return {
    nodes: new Map(detail.nodeIds.map((nodeId, index) => [nodeId, index])),
    edges: new Map(detail.edgeIds.map((edgeId, index) => [edgeId, index])),
  };
}

/** Focuses the cached full graph on complete selected SDK component memberships. */
export function deriveSccVisibleTopology(
  topology: TopologyResponse,
  components: ReadonlyArray<SccComponent>,
  selectedComponentIndexes: ReadonlySet<number>,
): VisibleTopology {
  const nodeIds = new Set(
    components
      .filter((component) => selectedComponentIndexes.has(component.componentIndex))
      .flatMap((component) => component.nodeIds),
  );
  const nodes = topology.nodes.filter((node) => nodeIds.has(node.id));
  const edges = topology.edges.filter(
    (edge) => nodeIds.has(edge.source) && nodeIds.has(edge.target),
  );
  return { nodes, edges };
}

/** Maps visible component membership to its snapshot-scoped result index. */
export function componentIndexByNode(
  components: ReadonlyArray<SccComponent>,
  selectedComponentIndexes: ReadonlySet<number>,
): ReadonlyMap<string, number> {
  const membership = new Map<string, number>();
  for (const component of components) {
    if (!selectedComponentIndexes.has(component.componentIndex)) {
      continue;
    }
    for (const nodeId of component.nodeIds) {
      membership.set(nodeId, component.componentIndex);
    }
  }
  return membership;
}

export const ALL_NODE_KINDS: ReadonlyArray<TopologyNodeKind> = ["exchange", "queue", "shovel"];

/** Matches the backend contract's Unicode code-point ordering. */
export function compareUnicodeCodePoints(left: string, right: string): number {
  const leftPoints = [...left].map((character) => character.codePointAt(0)!);
  const rightPoints = [...right].map((character) => character.codePointAt(0)!);
  const sharedLength = Math.min(leftPoints.length, rightPoints.length);
  for (let index = 0; index < sharedLength; index += 1) {
    if (leftPoints[index] !== rightPoints[index]) {
      return leftPoints[index]! - rightPoints[index]!;
    }
  }
  return leftPoints.length - rightPoints.length;
}

/** Returns the topology's vhosts in the wire contract's Unicode order. */
export function topologyVhosts(topology: TopologyResponse): ReadonlyArray<string> {
  return [...new Set(topology.nodes.map((node) => node.data.vhost))].sort(compareUnicodeCodePoints);
}

/**
 * Applies presentation-only visibility controls to immutable wire data.
 * Incident edges disappear with either hidden endpoint.
 */
export function deriveVisibleTopology(
  topology: TopologyResponse,
  selectedVhosts: ReadonlySet<string>,
  visibleKinds: ReadonlySet<TopologyNodeKind>,
  hideDefaultExchanges: boolean,
): VisibleTopology {
  const nodes = topology.nodes.filter((node) => {
    if (!selectedVhosts.has(node.data.vhost) || !visibleKinds.has(node.type)) {
      return false;
    }
    return !(hideDefaultExchanges && node.data.kind === "exchange" && node.data.isDefault === true);
  });
  const nodeIds = new Set(nodes.map((node) => node.id));
  const edges = topology.edges.filter(
    (edge) => nodeIds.has(edge.source) && nodeIds.has(edge.target),
  );
  return { nodes, edges };
}

/** Search styles visible nodes only; it never changes graph membership. */
export function findResourceMatches(
  nodes: ReadonlyArray<TopologyNode>,
  query: string,
): ReadonlySet<string> {
  const normalizedQuery = query.trim().toLocaleLowerCase();
  if (!normalizedQuery) {
    return new Set();
  }
  return new Set(
    nodes
      .filter((node) => node.data.name.toLocaleLowerCase().includes(normalizedQuery))
      .map((node) => node.id),
  );
}

/** Stable membership key used to cache layout without involving search state. */
export function visibleTopologyKey(topology: VisibleTopology): string {
  return `${topology.nodes.map((node) => node.id).join("\u001f")}\u001e${topology.edges
    .map((edge) => edge.id)
    .join("\u001f")}`;
}
