import {
  Background,
  BackgroundVariant,
  Controls,
  ControlButton,
  MarkerType,
  ReactFlow,
  ReactFlowProvider,
  getViewportForBounds,
  useReactFlow,
  type NodeChange,
  type XYPosition,
} from "@xyflow/react";
import ElkConstructor from "elkjs/lib/elk-api.js";
import type { ELK as ElkApi } from "elkjs/lib/elk-api.js";
import ElkWorker from "elkjs/lib/elk-worker.min.js?worker";
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
  createElkGraph,
  createLayoutRequest,
  itemsFromElkGraph,
  type LayoutItem,
  vhostGroupId,
} from "./layout";
import { visibleTopologyKey, type VisibleTopology } from "./presentation";
import { createEdgeRouting, drawingBounds } from "./edge-routing";

interface TopologyCanvasProps {
  topology: VisibleTopology;
  searchMatchIds: ReadonlySet<string>;
  searchFocusId?: string | null;
  selection: GraphSelection;
  onSelectionChange: (selection: GraphSelection) => void;
  onExitCanvas?: () => void;
  resetGeneration: number;
  componentIndexByNode?: ReadonlyMap<string, number>;
  cycleStepByNode?: ReadonlyMap<string, number>;
  cycleStepByEdge?: ReadonlyMap<string, number>;
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

function nodeAriaLabel(kind: TopologyNodeKind, name: string, vhost: string, missing: boolean) {
  const displayName = name || "unnamed default exchange";
  return `${missing ? "Missing " : ""}${kind} ${displayName}, vhost ${vhost}`;
}

function CanvasInner({
  topology,
  searchMatchIds,
  searchFocusId = null,
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
  const elkRef = useRef<ElkApi | null>(null);
  const latestGenerationRef = useRef(0);
  const pendingKeyRef = useRef("");
  const lastResetRef = useRef(resetGeneration);
  const layoutCacheRef = useRef(new Map<string, ReadonlyArray<LayoutItem>>());
  const [layoutItems, setLayoutItems] = useState<ReadonlyArray<LayoutItem>>([]);
  const [positions, setPositions] = useState<ReadonlyMap<string, XYPosition>>(new Map());
  const [layoutState, setLayoutState] = useState<"idle" | "running" | "ready" | "error">("idle");
  const [layoutError, setLayoutError] = useState<string | null>(null);
  const [fitGeneration, setFitGeneration] = useState(0);
  const [completedGeneration, setCompletedGeneration] = useState(0);
  const [canvasSize, setCanvasSize] = useState({ width: 0, height: 0 });
  const topologyKey = useMemo(() => visibleTopologyKey(topology), [topology]);
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

  const applyLayout = useCallback((items: ReadonlyArray<LayoutItem>, generation: number) => {
    setLayoutItems(items);
    setPositions(new Map(items.map((item) => [item.id, { x: item.x, y: item.y }])));
    setLayoutState("ready");
    setLayoutError(null);
    setFitGeneration(generation);
  }, []);

  useEffect(() => {
    const elk = new ElkConstructor({
      workerFactory: () => new ElkWorker(),
    });
    elkRef.current = elk;
    return () => {
      latestGenerationRef.current += 1;
      elk.terminateWorker();
      elkRef.current = null;
    };
  }, []);

  useEffect(() => {
    const generation = latestGenerationRef.current + 1;
    latestGenerationRef.current = generation;
    pendingKeyRef.current = topologyKey;
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

    if (topology.nodes.length === 0) {
      applyLayout([], generation);
      return;
    }
    const cached = forceLayout ? undefined : layoutCacheRef.current.get(topologyKey);
    if (cached) {
      applyLayout(cached, generation);
      return;
    }

    setLayoutState("running");
    setLayoutError(null);
    const elk = elkRef.current;
    if (!elk) {
      setLayoutState("error");
      setLayoutError("The ELK layout worker is unavailable.");
      return;
    }
    const request = createLayoutRequest(generation, topology);
    void elk
      .layout(createElkGraph(request))
      .then((graph) => {
        if (generation !== latestGenerationRef.current) {
          return;
        }
        const items = itemsFromElkGraph(graph);
        layoutCacheRef.current.set(pendingKeyRef.current, items);
        applyLayout(items, generation);
      })
      .catch(() => {
        if (generation === latestGenerationRef.current) {
          setLayoutState("error");
          setLayoutError("ELK could not lay out the visible topology.");
        }
      });
  }, [applyLayout, resetGeneration, topology, topologyKey]);

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
          position: positions.get(id) ?? { x: item.x, y: item.y },
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
          position: positions.get(wireNode.id) ?? { x: item.x, y: item.y },
          // These fixed CSS/ELK dimensions survive controlled-node rebuilds.
          // Omitting measured resets React Flow's handle bounds on style/focus
          // changes and can leave unchanged-size nodes awaiting an observer.
          measured: { width: item.width, height: item.height },
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
    onSelectionChange,
    positions,
    searchMatchIds,
    selection,
    topology.nodes,
    vhosts,
  ]);

  const edgeRouting = useMemo(
    () => createEdgeRouting(topology.edges, layoutItems),
    [layoutItems, topology.edges],
  );
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
      topology.edges.map((wireEdge) => {
        const sourceComponent = componentIndexByNode.get(wireEdge.source);
        const targetComponent = componentIndexByNode.get(wireEdge.target);
        const kind = wireEdge.data.kind.replaceAll("_", " ");
        const source = nodeDescriptions.get(wireEdge.source) ?? wireEdge.source;
        const target = nodeDescriptions.get(wireEdge.target) ?? wireEdge.target;
        const routingKey = wireEdge.data.routingKey === null
          ? ""
          : `, routing key ${wireEdge.data.routingKey || "(empty)"}`;
        const inspectionLabel = `Inspect ${kind} relationship ${wireEdge.id} from ${source} to ${target}${routingKey}`;
        return {
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
            routing: edgeRouting.get(wireEdge.id)!,
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
        } satisfies TopologyFlowEdge;
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

  const currentItems = useMemo(() => layoutItems.map((item) => ({
    ...item,
    ...(positions.get(item.id) ?? {}),
  })), [layoutItems, positions]);
  // Allow the complete drawing to fit even when its overview is smaller than
  // the ordinary zoom-out limit. Normal zoom-in remains available.
  const minZoom = useMemo(() => Math.min(0.08, getViewportForBounds(
    drawingBounds(currentItems, topology.edges, edgeRouting),
    canvasSize.width || 1, canvasSize.height || 1, 0, 1.25, 0.16,
  ).zoom), [canvasSize, currentItems, edgeRouting, topology.edges]);

  const frameDrawing = useCallback((items: ReadonlyArray<LayoutItem>) => {
    const canvas = canvasRef.current;
    if (!canvas?.clientWidth || !canvas.clientHeight) return Promise.resolve(false);
    const bounds = drawingBounds(items, topology.edges, edgeRouting);
    const viewport = getViewportForBounds(
      bounds, canvas.clientWidth, canvas.clientHeight, minZoom, 1.25, 0.16,
    );
    return reactFlow.setViewport(viewport, { duration: 0 });
  }, [edgeRouting, minZoom, reactFlow, topology.edges]);

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
    let frame: number;
    const frameLayout = () => {
      if (generation !== latestGenerationRef.current) return;
      // React Flow can still have the preceding compound dimensions/absolute
      // positions for a frame after a smaller focused layout is applied.
      const measured = layoutItems.every((item) => {
        const node = reactFlow.getInternalNode(item.id);
        const parent = item.parentId ? itemById.get(item.parentId) : undefined;
        return node &&
          Math.abs((node.measured.width ?? 0) - item.width) < 1 &&
          Math.abs((node.measured.height ?? 0) - item.height) < 1 &&
          Math.abs(node.internals.positionAbsolute.x - item.x - (parent?.x ?? 0)) < 1 &&
          Math.abs(node.internals.positionAbsolute.y - item.y - (parent?.y ?? 0)) < 1;
      });
      const canvas = canvasRef.current;
      if (!measured || !canvas?.clientWidth || !canvas.clientHeight) {
        frame = requestAnimationFrame(frameLayout);
        return;
      }
      void frameDrawing(layoutItems).then(() => {
        if (generation !== latestGenerationRef.current) return;
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
    return () => cancelAnimationFrame(frame);
  }, [completedGeneration, fitGeneration, flowNodes.length, frameDrawing, itemById, layoutItems, layoutState, reactFlow, topology]);

  useEffect(() => {
    if (
      searchMatchIds.size === 0 ||
      layoutState !== "ready" ||
      completedGeneration === 0 ||
      completedGeneration !== latestGenerationRef.current
    ) {
      return;
    }
    // Match changes and completed layouts request centering. Selection and drag
    // only rebuild presentation nodes, so they must not trigger this effect.
    // A restored compound layout can briefly retain the preceding vhost
    // dimensions. Wait for measurement so fitView uses the final positions.
    const generation = completedGeneration;
    let frame: number;
    const centerWhenMeasured = () => {
      if (generation !== latestGenerationRef.current) return;
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
      const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      void reactFlow.fitView({
        nodes: (searchFocusId ? [searchFocusId] : [...searchMatchIds]).map((id) => ({ id })),
        padding: 0.45,
        duration: reducedMotion ? 0 : 250,
        maxZoom: 1.5,
      });
    };
    frame = requestAnimationFrame(centerWhenMeasured);
    return () => cancelAnimationFrame(frame);
  }, [completedGeneration, layoutItems, layoutState, reactFlow, searchFocusId, searchMatchIds]);

  const handleNodeChanges = useCallback((changes: NodeChange<TopologyFlowNode>[]) => {
    const positionChanges = changes.filter(
      (change) => change.type === "position" && change.position !== undefined,
    );
    if (positionChanges.length === 0) {
      return;
    }
    setPositions((current) => {
      const next = new Map(current);
      for (const change of positionChanges) {
        if (change.type === "position" && change.position) {
          next.set(change.id, change.position);
        }
      }
      return next;
    });
  }, []);

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
        nodes={flowNodes}
        edges={flowEdges}
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
            onClick={() => void frameDrawing(currentItems)}
          >
            <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true">
              <path d="M1 6V1h5M10 1h5v5M15 10v5h-5M6 15H1v-5" fill="none" stroke="currentColor" strokeWidth="1.5" />
            </svg>
          </ControlButton>
        </Controls>
      </ReactFlow>
      <p className="layout-status" role="status" aria-live="polite">
        {layoutState === "running" ? "Laying out visible topology…" : null}
        {layoutState === "error" ? layoutError : null}
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
