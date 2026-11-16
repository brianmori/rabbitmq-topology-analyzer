import { describe, expect, it } from "vitest";
import type { TopologyEdge, TopologyEdgeKind } from "../types/topology";
import { createEdgeRouting, drawingBounds, routedEdgeGeometry, type Point } from "./edge-routing";
import type { LayoutItem } from "./layout";

function edge(id: string, source: string, target: string, kind: TopologyEdgeKind = "binding"): TopologyEdge {
  return { id, source, target, type: kind, data: { kind, routingKey: null } };
}

const edges = [
  edge("first-binding", "a", "b"),
  edge("second-binding", "a", "b"),
  edge("alternate", "a", "b", "alternate_exchange"),
  edge("reciprocal", "b", "a", "dead_letter"),
  edge("self-binding", "a", "a"),
  edge("self-alternate", "a", "a", "alternate_exchange"),
];

function sampledSegments(points: ReadonlyArray<Point>): Point[] {
  return points.slice(1).flatMap((end, index) => {
    const start = points[index]!;
    return Array.from({ length: 21 }, (_, step) => ({
      x: start.x + (end.x - start.x) * step / 20,
      y: start.y + (end.y - start.y) * step / 20,
    }));
  });
}

function inside(point: Point, left: number, top: number): boolean {
  return point.x > left && point.x < left + 220 && point.y > top && point.y < top + 82;
}

function expectNoSharedRuns(drawings: ReadonlyArray<{ id: string; points: ReadonlyArray<Point> }>) {
  const segments = drawings.flatMap((drawing) => drawing.points.slice(1).map((end, index) =>
    ({ id: drawing.id, start: drawing.points[index]!, end })));
  for (const [index, segment] of segments.entries()) {
    const horizontal = segment.start.y === segment.end.y;
    const along = horizontal ? "x" : "y";
    const across = horizontal ? "y" : "x";
    for (const other of segments.slice(index + 1)) {
      if (segment.id === other.id || horizontal !== (other.start.y === other.end.y)) continue;
      const overlap = Math.min(Math.max(segment.start[along], segment.end[along]), Math.max(other.start[along], other.end[along])) -
        Math.max(Math.min(segment.start[along], segment.end[along]), Math.min(other.start[along], other.end[along]));
      expect(Math.abs(segment.start[across] - other.start[across]) < 0.5 && overlap > 4,
        `${segment.id} shares a run with ${other.id}`).toBe(false);
    }
  }
}

describe("relationship drawing lanes", () => {
  it("assigns deterministic lanes across parallel kinds, reciprocal directions and loops without mutating wire data", () => {
    const original = JSON.stringify(edges);
    const routes = createEdgeRouting(edges);
    expect(routes.size).toBe(edges.length);
    for (const [id, route] of createEdgeRouting([...edges].reverse())) {
      expect(routes.get(id)).toEqual(route);
    }
    expect(routes.get("first-binding")!.side).not.toBe(routes.get("reciprocal")!.side);
    expect(new Set(edges.slice(0, 3).map((item) => routes.get(item.id)!.lane)).size).toBe(3);
    expect(JSON.stringify(edges)).toBe(original);
  });

  it("draws nonidentical paths and spaced inspector centers for each parallel relationship", () => {
    const routes = createEdgeRouting(edges);
    const geometry = edges.slice(0, 3).map((item) => routedEdgeGeometry(
      { x: 220, y: 41 }, { x: 268, y: 41 }, routes.get(item.id)!,
    )!);
    expect(new Set(geometry.map((item) => item.path)).size).toBe(3);
    for (const [index, item] of geometry.entries()) {
      expect(item.points[0]).toEqual({ x: 220, y: 41 });
      expect(item.points.at(-1)).toEqual({ x: 268, y: 41 });
      expect(item.path).not.toMatch(/NaN|Infinity/);
      for (const other of geometry.slice(index + 1)) {
        expect(Math.hypot(item.label.x - other.label.x, item.label.y - other.label.y))
          .toBeGreaterThan(22);
      }
    }
  });

  it("returns around a self-loop's body and gives multiple loops distinct inspectors", () => {
    const routes = createEdgeRouting(edges);
    const loops = edges.slice(4).map((item) => routedEdgeGeometry(
      { x: 220, y: 41 }, { x: 0, y: 41 }, routes.get(item.id)!,
    )!);
    expect(loops[0]!.path).not.toBe(loops[1]!.path);
    for (const loop of loops) {
      expect(loop.label.y).toBeLessThan(0);
      expect(sampledSegments(loop.points).some((point) => inside(point, 0, 0))).toBe(false);
      expect(loop.points[0]).toEqual({ x: 220, y: 41 });
      expect(loop.points.at(-1)).toEqual({ x: 0, y: 41 });
    }
  });

  it("keeps a backward relationship outside both endpoint bodies even with no parallel sibling", () => {
    const route = createEdgeRouting([edge("backward", "b", "a")]).get("backward")!;
    const geometry = routedEdgeGeometry({ x: 550, y: 41 }, { x: 0, y: 41 }, route)!;
    expect(sampledSegments(geometry.points).some((point) =>
      inside(point, 0, 0) || inside(point, 330, 0))).toBe(false);
    expect(geometry.label.y).toBeGreaterThan(82);
  });

  it("separates return paths from different endpoint pairs across compound groups", () => {
    const returns = [edge("long-return", "z-top", "a-lower"), edge("short-return", "z-lower", "a-upper")];
    const resource = (id: string, parentId: string, x: number, y: number): LayoutItem =>
      ({ id, parentId, x, y, width: 220, height: 82, isGroup: false });
    const items: LayoutItem[] = [
      { id: "control", parentId: null, x: 32, y: 160, width: 268, height: 300, isGroup: true },
      { id: "region-b", parentId: null, x: 400, y: 0, width: 1600, height: 160, isGroup: true },
      { id: "region-a", parentId: null, x: 400, y: 276, width: 1300, height: 160, isGroup: true },
      resource("a-upper", "control", 24, 54),
      resource("a-lower", "control", 24, 170),
      resource("z-top", "region-b", 1300, 54),
      resource("z-lower", "region-a", 1000, 54),
    ];
    const original = JSON.stringify(items);
    const routes = createEdgeRouting(returns, items);
    const long = routedEdgeGeometry({ x: 1924.5, y: 95 }, { x: 51.5, y: 371 }, routes.get("long-return")!)!;
    const short = routedEdgeGeometry({ x: 1624.5, y: 371 }, { x: 51.5, y: 255 }, routes.get("short-return")!)!;
    expect(Math.abs(long.label.y - short.label.y)).toBeGreaterThanOrEqual(36);
    // Separate the vertical return legs as well as the bottom horizontal runs.
    expect(Math.abs(long.points[4]!.x - short.points[4]!.x)).toBeGreaterThanOrEqual(36);
    for (const geometry of [long, short]) {
      const last = geometry.points.at(-1)!;
      const bend = geometry.points.at(-2)!;
      expect(last.x - bend.x).toBeGreaterThanOrEqual(48);
      expect(bend.y).toBe(last.y);
    }
    const reversed = createEdgeRouting([...returns].reverse(), [...items].reverse());
    for (const edge of returns) expect(reversed.get(edge.id)).toEqual(routes.get(edge.id));
    expect(JSON.stringify(items)).toBe(original);
    const bounds = drawingBounds(items, returns, routes);
    expect(bounds.y + bounds.height).toBeGreaterThan(Math.max(long.label.y, short.label.y));
    // Disjoint drawings reuse space instead of taking one global lane per edge.
    const distant = items.map((item) => item.id === "region-a" || item.id === "a-upper"
      ? { ...item, y: item.y + 1000 }
      : item);
    expect(createEdgeRouting(returns, distant).get("short-return")!.lane)
      .toBe(createEdgeRouting([returns[1]!], distant).get("short-return")!.lane);
  });

  it("recomputes routes from moved handles without reassigning lanes or crossing vertically separated endpoints", () => {
    const route = createEdgeRouting(edges).get("first-binding")!;
    const original = routedEdgeGeometry({ x: 220, y: 41 }, { x: 330, y: 41 }, route)!;
    const moved = routedEdgeGeometry({ x: 300, y: 200 }, { x: 250, y: 60 }, route)!;
    expect(moved.path).not.toBe(original.path);
    expect(moved.points[0]).toEqual({ x: 300, y: 200 });
    expect(moved.points.at(-1)).toEqual({ x: 250, y: 60 });
    expect(sampledSegments(moved.points).some((point) =>
      inside(point, 80, 159) || inside(point, 250, 19))).toBe(false);
  });

  it("avoids every resource body and the vhost title in a multi-row collection of loops", () => {
    const positions: Array<[string, number, number]> = [
      ["alternate.a", 24, 54], ["alternate.b", 300, 54], ["binding.self", 24, 180],
      ["mixed.b", 24, 296], ["mixed.a", 300, 296],
      ["dead-ae.fallback", 24, 412], ["dead-ae.q", 300, 428], ["dead-ae.entry", 576, 412],
      ["dead.b.q", 24, 528], ["dead.a", 300, 544], ["dead.a.q", 576, 544], ["dead.b", 852, 528],
      ["binding.a", 24, 672], ["binding.b", 300, 656], ["binding.c", 576, 720],
      ["dead.self.q", 24, 788], ["dead.self", 300, 788],
    ];
    const items: LayoutItem[] = [
      { id: "loops", parentId: null, x: 0, y: 0, width: 1100, height: 920, isGroup: true },
      ...positions.map(([id, x, y]) => ({ id, x, y, parentId: "loops", width: 220, height: 82, isGroup: false })),
    ];
    const cycles = [
      ["alternate.a", "alternate.b"], ["binding.self"], ["mixed.a", "mixed.b"],
      ["dead-ae.fallback", "dead-ae.q", "dead-ae.entry"],
      ["dead.a", "dead.a.q", "dead.b", "dead.b.q"], ["binding.a", "binding.b", "binding.c"],
      ["dead.self", "dead.self.q"],
    ];
    const relationships = cycles.flatMap((cycle, index) => cycle.map((source, step) =>
      edge(`cycle-${index}-${step}`, source, cycle[(step + 1) % cycle.length]!)));
    const routes = createEdgeRouting(relationships, items);
    const reversed = createEdgeRouting([...relationships].reverse(), [...items].reverse());
    const byId = new Map(items.map((item) => [item.id, item]));
    const drawings: Array<{ id: string; points: ReadonlyArray<Point> }> = [];
    for (const relationship of relationships) {
      expect(reversed.get(relationship.id)).toEqual(routes.get(relationship.id));
      const source = byId.get(relationship.source)!;
      const target = byId.get(relationship.target)!;
      const geometry = routedEdgeGeometry({ x: source.x + 224.5, y: source.y + 41 },
        { x: target.x - 4.5, y: target.y + 41 }, routes.get(relationship.id)!);
      const start = { x: source.x + 224.5, y: source.y + 41 };
      const end = { x: target.x - 4.5, y: target.y + 41 };
      const middle = (start.x + end.x) / 2;
      const points = geometry?.points ?? [start, { x: middle, y: start.y }, { x: middle, y: end.y }, end];
      drawings.push({ id: relationship.id, points });
      const samples = sampledSegments(points);
      for (const [id, x, y] of positions) {
        expect(samples.some((point) => inside(point, x, y)), `${relationship.id} crosses ${id}`).toBe(false);
      }
      expect(samples.some((point) => point.x > 12 && point.x < 1088 && point.y > 8 && point.y < 34),
        `${relationship.id} crosses the heading`).toBe(false);
    }
    expectNoSharedRuns(drawings);
    for (const id of ["cycle-0-1", "cycle-2-0", "cycle-6-0"]) {
      expect(drawings.find((drawing) => drawing.id === id)!.points.length,
        `${id} adds an unnecessary step beside its resource`).toBeLessThanOrEqual(6);
    }
  });

  it("retains approach channels without tiny steps when live handles move by a fraction", () => {
    const points = [{ x: 220, y: 41 }, { x: 268, y: 41 }, { x: 268, y: -28 },
      { x: -48, y: -28 }, { x: -48, y: 41 }, { x: 0, y: 41 }];
    const geometry = routedEdgeGeometry({ x: 220.5, y: 41.5 }, { x: 0.5, y: 41.5 }, {
      selfLoop: true, separated: true, side: -1, lane: 0, points,
    })!;
    expect(geometry.points).toHaveLength(6);
    expect(geometry.points[1]).toEqual({ x: 268, y: 41.5 });
    expect(geometry.points.at(-2)).toEqual({ x: -48, y: 41.5 });
  });

  it("keeps a dragged short forward connection inside its endpoints' visible bounds", () => {
    const geometry = routedEdgeGeometry({ x: 28, y: 15 }, { x: 50, y: 0 }, {
      selfLoop: false, separated: true, side: -1, lane: 8,
      points: [{ x: 0, y: 0 }, { x: 50, y: 0 }],
    })!;
    expect(geometry.points.length).toBeLessThanOrEqual(4);
    for (const point of geometry.points) {
      expect(point.x).toBeGreaterThanOrEqual(28);
      expect(point.x).toBeLessThanOrEqual(50);
      expect(point.y).toBeGreaterThanOrEqual(0);
      expect(point.y).toBeLessThanOrEqual(15);
    }
  });

  it("keeps the two legs of a dragged forward detour apart and its inspector on that detour", () => {
    const geometry = routedEdgeGeometry({ x: 273.5, y: 110 }, { x: 295.5, y: 95 }, {
      selfLoop: false, separated: true, side: -1, lane: 0,
      points: [{ x: 248.5, y: 95 }, { x: 260, y: 95 }, { x: 260, y: 59 },
        { x: 283.75, y: 59 }, { x: 283.75, y: 95 }, { x: 295.5, y: 95 }],
    })!;
    expect(geometry.points[3]!.x - geometry.points[2]!.x).toBeGreaterThanOrEqual(11);
    expect(geometry.label.y).toBe(59);
  });

  it("routes an ordinary forward relationship around an intervening resource", () => {
    const items = [
      { id: "a", parentId: null, x: 0, y: 100, width: 220, height: 82, isGroup: false },
      { id: "blocker", parentId: null, x: 300, y: 100, width: 220, height: 82, isGroup: false },
      { id: "z", parentId: null, x: 600, y: 100, width: 220, height: 82, isGroup: false },
    ];
    const relationship = edge("forward", "a", "z");
    const route = createEdgeRouting([relationship], items).get("forward")!;
    const geometry = routedEdgeGeometry({ x: 224.5, y: 141 }, { x: 595.5, y: 141 }, route)!;
    expect(geometry).not.toBeNull();
    expect(sampledSegments(geometry.points).some((point) => inside(point, 300, 100))).toBe(false);
    const moved = routedEdgeGeometry({ x: 234.5, y: 146 }, { x: 605.5, y: 146 }, route)!;
    expect(moved.points[0]).toEqual({ x: 234.5, y: 146 });
    expect(moved.points.at(-1)).toEqual({ x: 605.5, y: 146 });
    for (let index = 1; index < moved.points.length; index += 1) {
      const previous = moved.points[index - 1]!;
      expect(moved.points[index]!.x === previous.x || moved.points[index]!.y === previous.y).toBe(true);
    }
  });

  it("separates forward routes in a shared cross-vhost corridor and clears every container border", () => {
    const group = (id: string, x: number, y: number, width: number, height: number): LayoutItem =>
      ({ id, parentId: null, x, y, width, height, isGroup: true });
    const resource = (id: string, parentId: string, x: number, y: number): LayoutItem =>
      ({ id, parentId, x, y, width: 220, height: 82, isGroup: false });
    const items = [
      group("a", 0, 0, 540, 160), group("b", 0, 232, 540, 160),
      group("control", 648, 0, 268, 392), group("c", 1020, 116, 540, 160),
      resource("a.in", "a", 24, 54), resource("a.q", "a", 300, 54),
      resource("b.in", "b", 24, 54), resource("b.q", "b", 300, 54),
      resource("c.in", "c", 24, 54), resource("c.q", "c", 300, 54),
      resource("ab", "control", 24, 286), resource("bc", "control", 24, 170), resource("ca", "control", 24, 54),
    ];
    const cycle = ["ab", "b.in", "b.q", "bc", "c.in", "c.q", "ca", "a.in", "a.q"];
    const edges = cycle.map((source, index) => edge(`three-${index}`, source, cycle[(index + 1) % cycle.length]!));
    const routes = createEdgeRouting(edges, items);
    const byId = new Map(items.map((item) => [item.id, item]));
    const handles = (id: string, source: boolean): Point => {
      const item = byId.get(id)!;
      const parent = byId.get(item.parentId!)!;
      return { x: item.x + parent.x + (source ? 224.5 : -4.5), y: item.y + parent.y + 41 };
    };
    const drawings = edges.map((edge) => {
      const source = handles(edge.source, true);
      const target = handles(edge.target, false);
      const geometry = routedEdgeGeometry(source, target, routes.get(edge.id)!);
      const middle = (source.x + target.x) / 2;
      return { id: edge.id, points: geometry?.points ?? [source, { x: middle, y: source.y }, { x: middle, y: target.y }, target] };
    });
    const segments = drawings.flatMap((drawing) => drawing.points.slice(1).map((end, index) =>
      ({ id: drawing.id, start: drawing.points[index]!, end })));
    for (const [index, segment] of segments.entries()) {
      const horizontal = segment.start.y === segment.end.y;
      const along = horizontal ? "x" : "y";
      const across = horizontal ? "y" : "x";
      for (const other of segments.slice(index + 1)) {
        if (segment.id === other.id || horizontal !== (other.start.y === other.end.y)) continue;
        const overlap = Math.min(Math.max(segment.start[along], segment.end[along]), Math.max(other.start[along], other.end[along])) -
          Math.max(Math.min(segment.start[along], segment.end[along]), Math.min(other.start[along], other.end[along]));
        expect(Math.abs(segment.start[across] - other.start[across]) < 0.5 && overlap > 4,
          `${segment.id} shares a run with ${other.id}`).toBe(false);
      }
      for (const group of items.filter((item) => item.isGroup)) {
        const low = horizontal ? group.x : group.y;
        const high = low + (horizontal ? group.width : group.height);
        if (Math.max(segment.start[along], segment.end[along]) <= low || Math.min(segment.start[along], segment.end[along]) >= high) continue;
        const first = horizontal ? group.y : group.x;
        const second = first + (horizontal ? group.height : group.width);
        expect(Math.min(Math.abs(segment.start[across] - first), Math.abs(segment.start[across] - second)),
          `${segment.id} hugs ${group.id}`).toBeGreaterThanOrEqual(24);
      }
    }
  });

  it("keeps the ordinary forward smooth-step path and only routes actual retained IDs", () => {
    const routes = createEdgeRouting([edges[1]!]);
    expect([...routes.keys()]).toEqual(["second-binding"]);
    expect(routedEdgeGeometry({ x: 220, y: 41 }, { x: 330, y: 41 }, routes.get("second-binding")!))
      .toBeNull();
  });

  it("keeps narrow parallel routes distinct after removing endpoint backtracking", () => {
    const relationships = edges.slice(0, 3);
    const items: LayoutItem[] = [
      { id: "group", parentId: null, x: 0, y: 0, width: 568, height: 160, isGroup: true },
      { id: "a", parentId: "group", x: 24, y: 54, width: 220, height: 82, isGroup: false },
      { id: "b", parentId: "group", x: 300, y: 54, width: 220, height: 82, isGroup: false },
    ];
    const routes = createEdgeRouting(relationships, items);
    const paths = relationships.map((edge) => routedEdgeGeometry({ x: 248.5, y: 95 }, { x: 295.5, y: 95 }, routes.get(edge.id)!)!);
    expect(new Set(paths.map((geometry) => geometry.path)).size).toBe(relationships.length);
    expect(new Set(paths.map((geometry) => JSON.stringify(geometry.label))).size).toBe(relationships.length);
  });

  it("frames the compound resources and outside inspector lanes without adding layout nodes", () => {
    const items = [
      { id: "vhost", parentId: null, x: 32, y: 32, width: 560, height: 164, isGroup: true },
      { id: "a", parentId: "vhost", x: 24, y: 54, width: 220, height: 82, isGroup: false },
      { id: "b", parentId: "vhost", x: 300, y: 54, width: 220, height: 82, isGroup: false },
    ];
    const original = JSON.stringify(items);
    const bounds = drawingBounds(items, edges, createEdgeRouting(edges));
    expect(bounds.x).toBeLessThanOrEqual(32);
    expect(bounds.y).toBeLessThan(32);
    expect(bounds.x + bounds.width).toBeGreaterThanOrEqual(592);
    expect(bounds.y + bounds.height).toBeGreaterThan(196);
    expect(JSON.stringify(items)).toBe(original);
    expect(drawingBounds([], [], new Map())).toEqual({ x: 0, y: 0, width: 0, height: 0 });
  });
});
