import { memo, useMemo } from "react";
import type { DrawingGeometry, DrawingSize, ViewTransform } from "./large-drawing";
import type { LayoutItem } from "./layout";
import type { VisibleTopology } from "./presentation";

function rectanglePath({ x, y, width, height }: LayoutItem) {
  return `M${x} ${y}h${width}v${height}h${-width}Z`;
}

export const LargeGraphOverview = memo(function LargeGraphOverview({
  drawing, draggedDrawing, topology, transform, size, searchMatchIds,
  componentIndexByNode, cycleStepByNode, cycleStepByEdge,
}: {
  drawing: DrawingGeometry;
  draggedDrawing: DrawingGeometry;
  topology: VisibleTopology;
  transform: ViewTransform;
  size: DrawingSize;
  searchMatchIds: ReadonlySet<string>;
  componentIndexByNode: ReadonlyMap<string, number>;
  cycleStepByNode: ReadonlyMap<string, number>;
  cycleStepByEdge: ReadonlyMap<string, number>;
}) {
  const paths = useMemo(() => {
    const edgePaths = new Map<string, string>();
    for (const edge of topology.edges) {
      const points = draggedDrawing.edges.get(edge.id) ?? drawing.edges.get(edge.id);
      if (points) {
        const component = componentIndexByNode.get(edge.source);
        const tone = component !== undefined && component === componentIndexByNode.get(edge.target)
          ? ` topology-edge--scc scc-tone-${component % 6}` : "";
        const cycle = cycleStepByEdge.has(edge.id) ? " topology-edge--cycle" : "";
        const classes = `topology-edge topology-edge--${edge.data.kind}${tone}${cycle}`;
        edgePaths.set(classes, (edgePaths.get(classes) ?? "") +
          `M${points.map(({ x, y }) => `${x} ${y}`).join("L")}`);
      }
    }
    const nodePaths = new Map<string, string>();
    for (const node of topology.nodes) {
      const box = draggedDrawing.nodes.get(node.id) ?? drawing.nodes.get(node.id);
      if (box) {
        const kind = node.data.isMissing ? "missing" : node.type;
        const component = componentIndexByNode.get(node.id);
        const tone = component === undefined ? "" : ` large-graph-overview__scc scc-tone-${component % 6}`;
        const cycle = cycleStepByNode.has(node.id) ? " large-graph-overview__cycle" : "";
        const classes = `large-graph-overview__${kind}${tone}${cycle}`;
        nodePaths.set(classes, (nodePaths.get(classes) ?? "") + rectanglePath(box));
      }
    }
    return { edgePaths, nodePaths };
  }, [componentIndexByNode, cycleStepByEdge, cycleStepByNode, draggedDrawing, drawing, topology]);
  const highlights = useMemo(() => [...searchMatchIds].map((id) => {
    const box = draggedDrawing.nodes.get(id) ?? drawing.nodes.get(id);
    return box ? rectanglePath(box) : "";
  }).join(""), [draggedDrawing.nodes, drawing.nodes, searchMatchIds]);
  const groups = useMemo(() => {
    const names = new Map(topology.nodes.map((node) => [node.id, node.data.vhost]));
    const vhosts = new Map<string, string>();
    for (const [id, item] of drawing.nodes) {
      const name = names.get(id);
      if (item.parentId && name !== undefined) vhosts.set(item.parentId, name);
    }
    return [...vhosts].map(([id, name]) => ({ box: drawing.nodes.get(id)!, name }));
  }, [drawing, topology.nodes]);
  const [x, y, zoom] = transform;
  return (
    <svg className="large-graph-overview" width={size.width} height={size.height} aria-hidden="true">
      <g transform={`translate(${x} ${y}) scale(${zoom})`}>
        {groups.map(({ box }) => (
          <rect key={box.id} x={box.x} y={box.y} width={box.width} height={box.height}
            className="large-graph-overview__vhost" vectorEffect="non-scaling-stroke" />
        ))}
        {[...paths.edgePaths].map(([classes, path]) => (
          <path key={classes} d={path} className={classes}
            vectorEffect="non-scaling-stroke" />
        ))}
        {[...paths.nodePaths].map(([classes, path]) => (
          <path key={classes} d={path} className={classes} />
        ))}
        {highlights && <path className="large-graph-overview__match" d={highlights}
          vectorEffect="non-scaling-stroke" />}
      </g>
      {groups.filter(({ box }) => box.width * zoom > 40 && box.height * zoom > 18).map(({ box, name }) => (
        <text key={box.id} x={x + box.x * zoom + 4} y={y + box.y * zoom + 13}>{name}</text>
      ))}
    </svg>
  );
});
