import {
  Background,
  BackgroundVariant,
  Controls,
  ControlButton,
  MarkerType,
  Position,
  ReactFlow,
  ReactFlowProvider,
  getViewportForBounds,
  useReactFlow,
  useStore,
  type NodeChange,
  type XYPosition,
} from "@xyflow/react";
import LayoutWorker from "./topology-layout.worker?worker";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { GraphSelection } from "../components/DetailsPanel";
import type { TopologyNodeKind } from "../types/topology";
import {
  ExchangeNodeRenderer,
  QueueNodeRenderer,
  ShovelNodeRenderer,
  TopologyEdgeRenderer,
  VhostGroupRenderer,
  type ResourceFlowNode,
  type TopologyFlowEdge,
  type TopologyFlowNode,
  type VhostGroupNode,
} from "./flow-elements";
import {
  createLayoutRequest,
  RESOURCE_HEIGHT,
  RESOURCE_WIDTH,
  shouldReuseElkRoutes,
  type DrawingLayout,
  type LayoutReply,
  vhostGroupId,
} from "./layout";
import { visibleTopologyKey, type VisibleTopology } from "./presentation";
import { drawingBounds } from "./edge-routing";
import {
  DETAIL_ZOOM, onscreenElements, prepareLargeDrawing, updateDraggedGeometry, type DrawingGeometry,
} from "./large-drawing";
import { LargeGraphOverview } from "./LargeGraphOverview";

interface TopologyCanvasProps {
  topology: VisibleTopology;
  searchMatchIds: ReadonlySet<string>;
  searchFocus?: { id: string } | null;
  selection: GraphSelection;
  onSelectionChange: (selection: GraphSelection) => void;
  onExitCanvas?: () => void;
  resetGeneration: number;
  componentIndexByNode?: ReadonlyMap<string, number>;
  cycleStepByNode?: ReadonlyMap<string, number>;
  cycleStepByEdge?: ReadonlyMap<string, number>;
}

interface AppliedLayout {
  key: string;
  generation: number;
  layout: DrawingLayout;
  dragPositions: ReadonlyMap<string, XYPosition>;
  draggedDrawing: DrawingGeometry;
}

const nodeTypes = {
  exchange: ExchangeNodeRenderer,
  queue: QueueNodeRenderer,
  shovel: ShovelNodeRenderer,
  vhostGroup: VhostGroupRenderer,
};

const edgeTypes = { topologyEdge: TopologyEdgeRenderer };
const EMPTY_COMPONENT_MEMBERSHIP: ReadonlyMap<string, number> = new Map();
const EMPTY_CYCLE_STEPS: ReadonlyMap<string, number> = new Map();
const EMPTY_LAYOUT: DrawingLayout = {
  items: [],
  routing: new Map(),
  bounds: { x: 0, y: 0, width: 0, height: 0 },
};
const EMPTY_DRAG_POSITIONS: ReadonlyMap<string, XYPosition> = new Map();
const EMPTY_DRAGGED_DRAWING: DrawingGeometry = { nodes: new Map(), edges: new Map() };
const INITIAL_TRANSFORM = [0, 0, 1] as const;
const RESOURCE_HANDLES: NonNullable<ResourceFlowNode["handles"]> = [
  { id: null, type: "source", position: Position.Right, x: RESOURCE_WIDTH - 4.5,
    y: RESOURCE_HEIGHT / 2 - 4.5, width: 9, height: 9 },
  { id: null, type: "target", position: Position.Left, x: -4.5,
    y: RESOURCE_HEIGHT / 2 - 4.5, width: 9, height: 9 },
];

function nodeAriaLabel(kind: TopologyNodeKind, name: string, vhost: string, missing: boolean) {
  const displayName = name || "unnamed default exchange";
  return `${missing ? "Missing " : ""}${kind} ${displayName}, vhost ${vhost}`;
}

function CanvasInner({
  topology,
  searchMatchIds,
  searchFocus = null,
  selection,
  onSelectionChange,
  onExitCanvas,
  resetGeneration,
  componentIndexByNode = EMPTY_COMPONENT_MEMBERSHIP,
  cycleStepByNode = EMPTY_CYCLE_STEPS,
  cycleStepByEdge = EMPTY_CYCLE_STEPS,
}: TopologyCanvasProps) {
  const reactFlow = useReactFlow<TopologyFlowNode, TopologyFlowEdge>();
  const canvasRef = useRef<HTMLDivElement>(null);
  const latestGenerationRef = useRef(0);
  const lastResetRef = useRef(resetGeneration);
  const layoutCacheRef = useRef(new Map<string, DrawingLayout>());
  const [appliedLayout, setAppliedLayout] = useState<AppliedLayout | null>(null);
  const [layoutState, setLayoutState] = useState<"idle" | "running" | "ready" | "error">("idle");
  const [layoutError, setLayoutError] = useState<string | null>(null);
  const [completedGeneration, setCompletedGeneration] = useState(0);
  const [canvasSize, setCanvasSize] = useState({ width: 0, height: 0 });
  const topologyKey = useMemo(() => visibleTopologyKey(topology), [topology]);
  const initialTopologyKey = useRef(topologyKey);
  const currentLayout = appliedLayout?.key === topologyKey ? appliedLayout : null;
  const { items: layoutItems, routing: edgeRouting, bounds } = currentLayout?.layout ?? EMPTY_LAYOUT;
  const dragPositions = currentLayout?.dragPositions ?? EMPTY_DRAG_POSITIONS;
  const draggedDrawing = currentLayout?.draggedDrawing ?? EMPTY_DRAGGED_DRAWING;
  const fitGeneration = appliedLayout?.generation ?? 0;
  const large = shouldReuseElkRoutes(topology.nodes.length, topology.edges.length);
  const transform = useStore((state) => large ? state.transform : INITIAL_TRANSFORM);
  const showOverview = large && transform[2] < DETAIL_ZOOM;
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const observer = new ResizeObserver(() => {
      setCanvasSize({ width: canvas.clientWidth, height: canvas.clientHeight });
    });
    observer.observe(canvas);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const generation = latestGenerationRef.current + 1;
    latestGenerationRef.current = generation;
    // This effect synchronizes the external ELK worker/cache and React Flow.
    // Its new generation must become pending before worker or cached output
    // is applied; this is not state derived solely from another React state.
    // oxlint-disable-next-line react/set-state-in-effect
    setCompletedGeneration(0);
    const startMark = `rabbitmq-layout-${generation}-start`;
    performance.clearMarks(startMark);
    performance.mark(startMark);
    const forceLayout = resetGeneration !== lastResetRef.current;
    lastResetRef.current = resetGeneration;

    const applyLayout = (layout: DrawingLayout) => {
      setAppliedLayout({
        key: topologyKey, generation, layout,
        dragPositions: EMPTY_DRAG_POSITIONS, draggedDrawing: EMPTY_DRAGGED_DRAWING,
      });
      setLayoutState("ready");
      setLayoutError(null);
    };
    if (topology.nodes.length === 0) {
      applyLayout(EMPTY_LAYOUT);
      return;
    }
    const cached = forceLayout ? undefined : layoutCacheRef.current.get(topologyKey);
    if (cached) {
      applyLayout(cached);
      return;
    }

    setLayoutState("running");
    setLayoutError(null);
    const failLayout = (message: string) => {
      setLayoutState("error");
      setLayoutError(message);
    };
    let worker: Worker | undefined;
    let active = true;
    const stopWorker = () => {
      if (!active) return;
      active = false;
      worker?.terminate();
    };
    try {
      worker = new LayoutWorker();
      worker.onmessage = ({ data: reply }: MessageEvent<LayoutReply>) => {
        if (!active || reply.generation !== latestGenerationRef.current) return;
        stopWorker();
        if ("error" in reply) {
          failLayout(reply.error);
          return;
        }
        const cache = layoutCacheRef.current;
        cache.delete(topologyKey);
        cache.set(topologyKey, reply.layout);
        // Retain the initial drawing for clearing focus, plus recent scopes.
        if (cache.size > 3) {
          const oldestScope = [...cache.keys()].find((key) => key !== initialTopologyKey.current);
          if (oldestScope !== undefined) cache.delete(oldestScope);
        }
        applyLayout(reply.layout);
      };
      const workerFailed = () => {
        if (!active || generation !== latestGenerationRef.current) return;
        stopWorker();
        failLayout("The layout worker is unavailable.");
      };
      worker.onerror = workerFailed;
      worker.onmessageerror = workerFailed;
      worker.postMessage(createLayoutRequest(generation, topology));
    } catch {
      stopWorker();
      failLayout("The layout worker is unavailable.");
    }
    return stopWorker;
  }, [resetGeneration, topology, topologyKey]);

  const itemById = useMemo(
    () => new Map(layoutItems.map((item) => [item.id, item])),
    [layoutItems],
  );
  const vhosts = useMemo(
    () => [...new Set(topology.nodes.map((node) => node.data.vhost))],
    [topology.nodes],
  );

  const flowNodes = useMemo<TopologyFlowNode[]>(() => {
    const groups: VhostGroupNode[] = vhosts.flatMap((vhost) => {
      const id = vhostGroupId(vhost);
      const item = itemById.get(id);
      if (!item) {
        return [];
      }
      return [
        {
          id,
          type: "vhostGroup",
          position: { x: item.x, y: item.y },
          data: { vhost },
          style: { width: item.width, height: item.height },
          measured: { width: item.width, height: item.height },
          className: "vhost-group",
          draggable: false,
          selectable: false,
          focusable: false,
          zIndex: -1,
        },
      ];
    });
    const resources: ResourceFlowNode[] = topology.nodes.flatMap((wireNode) => {
      const item = itemById.get(wireNode.id);
      if (!item) {
        return [];
      }
      return [
        {
          id: wireNode.id,
          type: wireNode.type,
          parentId: item.parentId ?? undefined,
          extent: "parent",
          position: { x: item.x, y: item.y },
          // These fixed CSS/ELK dimensions survive controlled-node rebuilds.
          // Omitting measured resets React Flow's handle bounds on style/focus
          // changes and can leave unchanged-size nodes awaiting an observer.
          measured: { width: item.width, height: item.height },
          handles: large ? RESOURCE_HANDLES : undefined,
          data: {
            wireNode,
            searchMatch: searchMatchIds.has(wireNode.id),
            componentIndex: componentIndexByNode.get(wireNode.id) ?? null,
            cycleStep: cycleStepByNode.get(wireNode.id) ?? null,
            onSelect: (id) => onSelectionChange({ kind: "node", id }),
          },
          selected: selection?.kind === "node" && selection.id === wireNode.id,
          ariaLabel: nodeAriaLabel(
            wireNode.type,
            wireNode.data.name,
            wireNode.data.vhost,
            wireNode.data.isMissing,
          ),
          draggable: true,
          selectable: true,
          focusable: false,
          zIndex: 1,
        },
      ];
    });
    return [...groups, ...resources];
  }, [
    componentIndexByNode,
    cycleStepByNode,
    itemById,
    large,
    onSelectionChange,
    searchMatchIds,
    selection,
    topology.nodes,
    vhosts,
  ]);

  const nodeDescriptions = useMemo(
    () => new Map(
      topology.nodes.map((node) => [
        node.id,
        `${node.data.kind} ${node.data.name || "(default exchange)"} in vhost ${node.data.vhost}`,
      ]),
    ),
    [topology.nodes],
  );
  const flowEdges = useMemo<TopologyFlowEdge[]>(
    () =>
      topology.edges.flatMap((wireEdge) => {
        const routing = edgeRouting.get(wireEdge.id);
        if (!routing) return [];
        const sourceComponent = componentIndexByNode.get(wireEdge.source);
        const targetComponent = componentIndexByNode.get(wireEdge.target);
        const kind = wireEdge.data.kind.replaceAll("_", " ");
        const source = nodeDescriptions.get(wireEdge.source) ?? wireEdge.source;
        const target = nodeDescriptions.get(wireEdge.target) ?? wireEdge.target;
        const routingKey = wireEdge.data.routingKey === null
          ? ""
          : `, routing key ${wireEdge.data.routingKey || "(empty)"}`;
        const inspectionLabel = `Inspect ${kind} relationship ${wireEdge.id} from ${source} to ${target}${routingKey}`;
        return [{
          id: wireEdge.id,
          source: wireEdge.source,
          target: wireEdge.target,
          type: "topologyEdge",
          data: {
            wireEdge,
            componentIndex:
              sourceComponent !== undefined && sourceComponent === targetComponent
                ? sourceComponent
                : null,
            cycleStep: cycleStepByEdge.get(wireEdge.id) ?? null,
            // Animate the typed path in our renderer to preserve its dash pattern.
            cycleAnimated: cycleStepByEdge.has(wireEdge.id) && !reducedMotion,
            routing,
            inspectionLabel,
            onSelect: (id) => onSelectionChange({ kind: "edge", id }),
          },
          selected: selection?.kind === "edge" && selection.id === wireEdge.id,
          markerEnd: {
            type: MarkerType.ArrowClosed,
            width: 18,
            height: 18,
            markerUnits: "userSpaceOnUse",
            color: "context-stroke",
          },
          ariaLabel: inspectionLabel,
          focusable: false,
          selectable: true,
          zIndex: 2,
        } satisfies TopologyFlowEdge];
      }),
    [
      componentIndexByNode,
      cycleStepByEdge,
      edgeRouting,
      nodeDescriptions,
      onSelectionChange,
      reducedMotion,
      selection,
      topology.edges,
    ],
  );

  // Allow the complete drawing to fit even when its overview is smaller than
  // the ordinary zoom-out limit. Normal zoom-in remains available.
  const minZoom = useMemo(() => Math.min(0.08, getViewportForBounds(
    bounds,
    canvasSize.width || 1, canvasSize.height || 1, 0, 1.25, 0.16,
  ).zoom), [bounds, canvasSize]);
  const largeDrawing = useMemo(() => large && layoutItems.length > 0
    ? prepareLargeDrawing(topology, layoutItems, edgeRouting)
    : null, [edgeRouting, large, layoutItems, topology]);
  const displayed = useMemo(() => {
    if (showOverview) return { nodes: [], edges: [] };
    const visible = largeDrawing
      ? onscreenElements(largeDrawing, draggedDrawing, flowNodes, flowEdges, transform, canvasSize)
      : { nodes: flowNodes, edges: flowEdges };
    if (dragPositions.size === 0) return visible;
    return {
      nodes: visible.nodes.map((node) => {
        const position = dragPositions.get(node.id);
        return position ? { ...node, position } : node;
      }),
      edges: visible.edges,
    };
  }, [canvasSize, dragPositions, draggedDrawing, flowEdges, flowNodes, largeDrawing, showOverview, transform]);

  const fitCurrentDrawing = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas?.clientWidth || !canvas.clientHeight) return Promise.resolve(false);
    const drawing = dragPositions.size === 0 ? bounds : drawingBounds(
      layoutItems.map((item) => ({ ...item, ...dragPositions.get(item.id) })),
      topology.edges, edgeRouting,
    );
    const viewport = getViewportForBounds(
      drawing, canvas.clientWidth, canvas.clientHeight, 0, 1.25, 0.16,
    );
    return reactFlow.setViewport(viewport, { duration: 0 });
  }, [bounds, dragPositions, edgeRouting, layoutItems, reactFlow, topology.edges]);

  useEffect(() => {
    if (
      fitGeneration === 0 ||
      completedGeneration === fitGeneration ||
      fitGeneration !== latestGenerationRef.current ||
      layoutState !== "ready" ||
      flowNodes.length === 0
    ) {
      return;
    }
    const generation = fitGeneration;
    let active = true;
    let frame: number;
    const frameLayout = () => {
      if (!active || generation !== latestGenerationRef.current) return;
      // React Flow can still have the preceding compound dimensions/absolute
      // positions for a frame after a smaller focused layout is applied.
      const measured = large || layoutItems.every((item) => {
        const node = reactFlow.getInternalNode(item.id);
        const parent = item.parentId ? itemById.get(item.parentId) : undefined;
        const position = dragPositions.get(item.id) ?? item;
        return node &&
          Math.abs((node.measured.width ?? 0) - item.width) < 1 &&
          Math.abs((node.measured.height ?? 0) - item.height) < 1 &&
          Math.abs(node.internals.positionAbsolute.x - position.x - (parent?.x ?? 0)) < 1 &&
          Math.abs(node.internals.positionAbsolute.y - position.y - (parent?.y ?? 0)) < 1;
      });
      const canvas = canvasRef.current;
      if (!measured || !canvas?.clientWidth || !canvas.clientHeight) {
        frame = requestAnimationFrame(frameLayout);
        return;
      }
      void fitCurrentDrawing().then(() => {
        if (!active || generation !== latestGenerationRef.current) return;
        const startMark = `rabbitmq-layout-${generation}-start`;
        const endMark = `rabbitmq-layout-${generation}-end`;
        const measureName = `rabbitmq-layout-${generation}`;
        performance.clearMarks(endMark);
        performance.clearMeasures(measureName);
        performance.mark(endMark);
        const duration = performance.measure(measureName, startMark, endMark).duration;
        setCompletedGeneration(generation);
        window.dispatchEvent(
          new CustomEvent("rabbitmq:layout-complete", {
            detail: {
              generation,
              duration,
              nodeCount: topology.nodes.length,
              edgeCount: topology.edges.length,
            },
          }),
        );
      });
    };
    frame = requestAnimationFrame(frameLayout);
    return () => {
      active = false;
      cancelAnimationFrame(frame);
    };
  }, [
    completedGeneration, dragPositions, fitCurrentDrawing, fitGeneration, flowNodes.length,
    itemById, large, layoutItems, layoutState, reactFlow, topology,
  ]);

  // A drag updates the bounds used by the next search without recentering.
  const draggedNodesRef = useRef(draggedDrawing.nodes);
  useEffect(() => {
    draggedNodesRef.current = draggedDrawing.nodes;
  }, [draggedDrawing.nodes]);

  useEffect(() => {
    if (
      searchMatchIds.size === 0 ||
      layoutState !== "ready" ||
      completedGeneration === 0 ||
      completedGeneration !== latestGenerationRef.current
    ) {
      return;
    }
    // Read moved positions when search recenters so dragging alone preserves
    // the current viewport.
    // A restored compound layout can briefly retain the preceding vhost
    // dimensions. Wait for measurement so fitView uses the final positions.
    const generation = completedGeneration;
    let frame: number;
    const centerWhenMeasured = () => {
      if (generation !== latestGenerationRef.current) return;
      const canvas = canvasRef.current;
      const ids = searchFocus ? [searchFocus.id] : [...searchMatchIds];
      const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      if (largeDrawing && canvas) {
        const items = ids.flatMap((id) => {
          const item = draggedNodesRef.current.get(id) ?? largeDrawing.nodes.get(id);
          return item ? [{ ...item, parentId: null }] : [];
        });
        if (items.length === 0) return;
        const viewport = getViewportForBounds(
          drawingBounds(items, [], edgeRouting), canvas.clientWidth, canvas.clientHeight,
          0, 1.5, 0.45,
        );
        void reactFlow.setViewport(viewport, { duration: reducedMotion ? 0 : 250 });
        return;
      }
      const measured = layoutItems.every((item) => {
        const node = reactFlow.getInternalNode(item.id);
        if (!node?.measured.width || !node.measured.height) return false;
        return (
          !item.isGroup ||
          (Math.abs(node.measured.width - item.width) < 1 &&
            Math.abs(node.measured.height - item.height) < 1)
        );
      });
      if (!measured) {
        frame = requestAnimationFrame(centerWhenMeasured);
        return;
      }
      void reactFlow.fitView({
        nodes: ids.map((id) => ({ id })),
        padding: 0.45,
        duration: reducedMotion ? 0 : 250,
        maxZoom: 1.5,
      });
    };
    frame = requestAnimationFrame(centerWhenMeasured);
    return () => cancelAnimationFrame(frame);
  }, [completedGeneration, edgeRouting, largeDrawing, layoutItems, layoutState, reactFlow, searchFocus, searchMatchIds]);

  const handleNodeChanges = useCallback((changes: NodeChange<TopologyFlowNode>[]) => {
    const positionChanges = new Map<string, XYPosition>();
    for (const change of changes) {
      if (change.type !== "position" || !change.position) continue;
      const item = itemById.get(change.id);
      if (item && !item.isGroup) positionChanges.set(change.id, change.position);
    }
    if (positionChanges.size === 0) return;
    setAppliedLayout((current) => {
      if (current?.generation !== fitGeneration || current.key !== topologyKey) return current;
      const changed = new Map<string, XYPosition>();
      const positions = new Map(current.dragPositions);
      for (const [id, position] of positionChanges) {
        const item = itemById.get(id)!;
        const previous = positions.get(id) ?? item;
        if (previous.x === position.x && previous.y === position.y) continue;
        changed.set(id, position);
        if (item.x === position.x && item.y === position.y) positions.delete(id);
        else positions.set(id, position);
      }
      if (changed.size === 0) return current;
      return {
        ...current,
        dragPositions: positions,
        draggedDrawing: largeDrawing
          ? updateDraggedGeometry(largeDrawing, current.draggedDrawing, changed, edgeRouting)
          : current.draggedDrawing,
      };
    });
  }, [edgeRouting, fitGeneration, itemById, largeDrawing, topologyKey]);

  return (
    <div
      ref={canvasRef}
      className="topology-canvas"
      aria-label="RabbitMQ topology graph"
      data-layout-state={layoutState}
      data-layout-generation={fitGeneration}
      data-layout-completed-generation={completedGeneration}
      data-layout-node-count={topology.nodes.length}
      data-layout-edge-count={topology.edges.length}
      onKeyDown={(event) => {
        if (event.key === "Escape") {
          event.preventDefault();
          event.stopPropagation();
          onExitCanvas?.();
        }
      }}
    >
      <ReactFlow<TopologyFlowNode, TopologyFlowEdge>
        nodes={displayed.nodes}
        edges={displayed.edges}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        onNodesChange={handleNodeChanges}
        onNodeClick={(_, node) => {
          if (node.type !== "vhostGroup") {
            onSelectionChange({ kind: "node", id: node.id });
          }
        }}
        onEdgeClick={(_, edge) => onSelectionChange({ kind: "edge", id: edge.id })}
        onPaneClick={() => onSelectionChange(null)}
        nodesConnectable={false}
        edgesReconnectable={false}
        deleteKeyCode={null}
        minZoom={minZoom}
        maxZoom={2}
        proOptions={{ hideAttribution: true }}
        colorMode="light"
      >
        <Background variant={BackgroundVariant.Dots} gap={24} size={1} color="#c6d1d8" />
        <Controls position="bottom-right" showInteractive={false} showFitView={false}>
          <ControlButton
            aria-label="Fit view"
            title="Fit graph and relationships"
            onClick={() => void fitCurrentDrawing()}
          >
            <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true">
              <path d="M1 6V1h5M10 1h5v5M15 10v5h-5M6 15H1v-5" fill="none" stroke="currentColor" strokeWidth="1.5" />
            </svg>
          </ControlButton>
        </Controls>
      </ReactFlow>
      {showOverview && largeDrawing && (
        <LargeGraphOverview drawing={largeDrawing} draggedDrawing={draggedDrawing} topology={topology} transform={transform}
          size={canvasSize} searchMatchIds={searchMatchIds} componentIndexByNode={componentIndexByNode}
          cycleStepByNode={cycleStepByNode} cycleStepByEdge={cycleStepByEdge} />
      )}
      <p className="layout-status" role="status" aria-live="polite">
        {layoutState === "running" ? "Laying out visible topology…" : null}
        {layoutState === "error" ? layoutError : null}
        {layoutState === "ready" && showOverview ? "Zoom in or search to inspect resources and relationships." : null}
      </p>
    </div>
  );
}

export function TopologyCanvas(props: TopologyCanvasProps) {
  return (
    <ReactFlowProvider>
      <CanvasInner {...props} />
    </ReactFlowProvider>
  );
}
