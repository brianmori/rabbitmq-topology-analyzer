import { describe, expect, it } from "vitest";
import { topologyFixture } from "../test/api-fixtures";
import { createEdgeRouting } from "./edge-routing";
import { prepareLargeDrawing, updateDraggedGeometry, type DrawingGeometry } from "./large-drawing";
import { RESOURCE_HEIGHT, RESOURCE_WIDTH, vhostGroupId, type LayoutItem } from "./layout";

describe("incremental drag geometry", () => {
  it("reuses unrelated paths, resolves moved endpoints across vhosts, and clears restored positions", () => {
    const topology = topologyFixture;
    const items: LayoutItem[] = [
      { id: vhostGroupId("/orders"), parentId: null, x: 100, y: 40, width: 1300, height: 500, isGroup: true },
      { id: vhostGroupId("/audit"), parentId: null, x: 1450, y: 250, width: 400, height: 300, isGroup: true },
      ...topology.nodes.map((node, index) => ({
        id: node.id,
        parentId: vhostGroupId(node.data.vhost),
        x: node.data.vhost === "/audit" ? 32 : 32 + index * 230,
        y: 60,
        width: RESOURCE_WIDTH,
        height: RESOURCE_HEIGHT,
        isGroup: false,
      })),
    ];
    const routing = createEdgeRouting(topology.edges);
    const base = prepareLargeDrawing(topology, items, routing);
    const empty: DrawingGeometry = { nodes: new Map(), edges: new Map() };

    const queue = "n1.orders-queue";
    const shovel = "n1.cross-vhost-shovel";
    const audit = "n1.audit-queue";
    const moved = new Map([[queue, { x: 505, y: 109 }], [shovel, { x: 921, y: 180 }]]);
    const first = updateDraggedGeometry(base, empty, moved, routing);
    expect([...first.nodes.keys()]).toEqual([queue, shovel]);
    expect([...first.edges.keys()].sort()).toEqual(["e1.0", "e1.1", "e1.2", "e1.3", "e1.4"]);

    const auditPosition = { x: 94, y: 120 };
    const second = updateDraggedGeometry(base, first, new Map([[audit, auditPosition]]), routing);
    expect(second.edges.get("e1.2")).toBe(first.edges.get("e1.2"));
    expect(second.edges.get("e1.3")).not.toBe(first.edges.get("e1.3"));
    expect(first.nodes.has(audit)).toBe(false);

    moved.set(audit, auditPosition);
    const rebuilt = prepareLargeDrawing(topology, items.map((item) => ({
      ...item, ...moved.get(item.id),
    })), routing);
    expect(new Map([...base.nodes, ...second.nodes])).toEqual(rebuilt.nodes);
    expect(new Map([...base.edges, ...second.edges])).toEqual(rebuilt.edges);

    const restored = new Map(items.filter((item) => moved.has(item.id)).map((item) => [item.id, item]));
    expect(updateDraggedGeometry(base, second, restored, routing)).toEqual(empty);
  });
});
