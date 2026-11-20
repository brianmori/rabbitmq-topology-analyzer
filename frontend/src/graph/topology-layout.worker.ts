import ElkConstructor from "elkjs/lib/elk.bundled.js";
import { createEdgeRouting, drawingBounds } from "./edge-routing";
import {
  createElkGraph, itemsFromElkGraph, pathsFromElkGraph, shouldReuseElkRoutes,
  type LayoutReply, type LayoutRequest,
} from "./layout";

// The bundled ELK runs in this dedicated worker alongside the routing pass.
const elk = new ElkConstructor();

self.onmessage = async ({ data: request }: MessageEvent<LayoutRequest>) => {
  let reply: LayoutReply;
  try {
    const graph = await elk.layout(createElkGraph(request));
    const items = itemsFromElkGraph(graph);
    const routing = createEdgeRouting(request.edges, items,
      shouldReuseElkRoutes(request.resources.length, request.edges.length) ? pathsFromElkGraph(graph) : undefined);
    reply = { generation: request.generation, layout: {
      items, routing, bounds: drawingBounds(items, request.edges, routing),
    } };
  } catch {
    reply = { generation: request.generation, error: "Could not lay out the visible topology." };
  }
  self.postMessage(reply);
};
