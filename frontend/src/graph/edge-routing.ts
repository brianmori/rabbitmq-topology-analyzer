import type { TopologyEdge } from "../types/topology";
import { RESOURCE_HEIGHT, RESOURCE_WIDTH, type LayoutItem } from "./layout";
import { compareUnicodeCodePoints } from "./presentation";
import {
  paddedObstacles, pathHitsObstacle, pathHugsBorder, pathsShareLane, routeAroundObstacles, simplifyPoints,
  type RoutingPath,
} from "./obstacle-routing";

export interface EdgeRouting {
  selfLoop: boolean;
  separated: boolean;
  side: -1 | 1;
  lane: number;
  points?: ReadonlyArray<Point>;
}

export interface Point {
  x: number;
  y: number;
}

export interface EdgeGeometry {
  path: string;
  label: Point;
  points: ReadonlyArray<Point>;
}

const ENDPOINT_CLEARANCE = 48;
const NODE_CLEARANCE = 28;
const LANE_SPACING = 36;
const CORNER_RADIUS = 12;
// The renderer's handles are 9 px wide; React Flow attaches at their outside edge.
const HANDLE_OUTSET = 4.5;
const INSPECTOR_PADDING = 14;

/** Assign drawing lanes to wire IDs without changing graph membership or order. */
export function createEdgeRouting(
  edges: ReadonlyArray<TopologyEdge>,
  items: ReadonlyArray<LayoutItem> = [],
): ReadonlyMap<string, EdgeRouting> {
  const pairs = new Map<string, TopologyEdge[]>();
  for (const edge of edges) {
    const endpoints = [edge.source, edge.target].sort(compareUnicodeCodePoints);
    const key = JSON.stringify(endpoints);
    const siblings = pairs.get(key) ?? [];
    siblings.push(edge);
    pairs.set(key, siblings);
  }
  const routing = new Map<string, EdgeRouting>();
  for (const siblings of pairs.values()) {
    const lanes = { [-1]: 0, [1]: 0 };
    for (const edge of siblings.sort((left, right) => compareUnicodeCodePoints(left.id, right.id))) {
      const selfLoop = edge.source === edge.target;
      // Reciprocal directions occupy opposite sides of the endpoint pair.
      const side = compareUnicodeCodePoints(edge.source, edge.target) <= 0 ? -1 : 1;
      routing.set(edge.id, {
        selfLoop,
        separated: selfLoop || siblings.length > 1,
        side,
        lane: lanes[side]++,
      });
    }
  }
  // Pair-local lanes cannot see unrelated return paths sharing the same row.
  // Reserve space using the completed compound layout, then retain these lane
  // assignments while live handles move during a drag.
  const positioned = absoluteItems(items);
  const borders = [...positioned.values()].filter((item) => item.isGroup).map((item) => ({
    id: item.id, left: item.x, right: item.x + item.width, top: item.y, bottom: item.y + item.height,
  }));
  const obstacles = paddedObstacles([...positioned.values()].map((item) => item.isGroup
    ? { id: `heading:${item.id}`, left: item.x + 12, right: item.x + item.width - 12,
      top: item.y + 8, bottom: item.y + 34 }
    : { id: item.id, left: item.x, right: item.x + item.width, top: item.y, bottom: item.y + item.height }));
  const reserved: RoutingPath[] = [];
  const span = (edge: TopologyEdge) => {
    const source = positioned.get(edge.source);
    const target = positioned.get(edge.target);
    return source && target ? Math.abs(source.x + source.width - target.x) + Math.abs(source.y - target.y) : 0;
  };
  // Give the constrained local connections their channels before long return
  // paths, which have more room to detour around the compound groups.
  const ordered = [...edges].sort((left, right) => span(left) - span(right) || compareUnicodeCodePoints(left.id, right.id));
  for (const edge of ordered) {
    const source = positioned.get(edge.source);
    const target = positioned.get(edge.target);
    if (!source || !target) continue;
    const start = { x: source.x + source.width + HANDLE_OUTSET, y: source.y + source.height / 2 };
    const end = { x: target.x - HANDLE_OUTSET, y: target.y + target.height / 2 };
    let route = routing.get(edge.id)!;
    let geometry = routedEdgeGeometry(start, end, route);
    const forward = [start, { x: (start.x + end.x) / 2, y: start.y },
      { x: (start.x + end.x) / 2, y: end.y }, end];
    let attempts = 0;
    const blocked = () => {
      const points = geometry?.points ?? forward;
      const candidate = { sourceId: edge.source, targetId: edge.target, points };
      return pathHitsObstacle(points, obstacles, edge.source, edge.target) || pathHugsBorder(points, borders) ||
        reserved.some((other) => pathsShareLane(candidate, other));
    };
    while (geometry && blocked() && attempts < 8) {
      route = { ...route, lane: route.lane + 1 };
      geometry = routedEdgeGeometry(start, end, route);
      attempts += 1;
    }
    if (blocked()) {
      const points = routeAroundObstacles(start, end, edge.source, edge.target, obstacles,
        geometry?.points ?? forward, reserved, borders);
      route = { ...route, points };
      geometry = geometryFromPoints(points);
    }
    routing.set(edge.id, route);
    reserved.push({ sourceId: edge.source, targetId: edge.target, points: geometry?.points ?? simplifyPoints(forward) });
  }
  return routing;
}

function absoluteItems(items: ReadonlyArray<LayoutItem>): ReadonlyMap<string, LayoutItem> {
  const byId = new Map(items.map((item) => [item.id, item]));
  return new Map(items.map((item) => {
    const parent = item.parentId ? byId.get(item.parentId) : undefined;
    return [item.id, { ...item, x: item.x + (parent?.x ?? 0), y: item.y + (parent?.y ?? 0) }];
  }));
}

function toward(point: Point, destination: Point, distance: number): Point {
  const length = Math.hypot(destination.x - point.x, destination.y - point.y);
  if (length === 0) return point;
  return {
    x: point.x + (destination.x - point.x) * distance / length,
    y: point.y + (destination.y - point.y) * distance / length,
  };
}

function roundedPath(points: ReadonlyArray<Point>): string {
  const first = points[0]!;
  let path = `M${first.x} ${first.y}`;
  for (let index = 1; index < points.length - 1; index += 1) {
    const previous = points[index - 1]!;
    const corner = points[index]!;
    const next = points[index + 1]!;
    const radius = Math.min(
      CORNER_RADIUS,
      Math.hypot(previous.x - corner.x, previous.y - corner.y) / 2,
      Math.hypot(next.x - corner.x, next.y - corner.y) / 2,
    );
    const entry = toward(corner, previous, radius);
    const exit = toward(corner, next, radius);
    path += `L${entry.x} ${entry.y}Q${corner.x} ${corner.y} ${exit.x} ${exit.y}`;
  }
  const last = points.at(-1)!;
  return `${path}L${last.x} ${last.y}`;
}

function geometryFromPoints(points: ReadonlyArray<Point>): EdgeGeometry {
  let label = points[0]!;
  let longest = -1;
  let horizontalInterior = false;
  for (let index = 1; index < points.length; index += 1) {
    const start = points[index - 1]!;
    const end = points[index]!;
    const length = Math.hypot(end.x - start.x, end.y - start.y);
    const interior = index > 1 && index < points.length - 1;
    if (points.length > 2 && !interior) continue;
    const horizontal = start.y === end.y;
    if ((horizontal && !horizontalInterior) || (horizontal === horizontalInterior && length > longest)) {
      longest = length;
      horizontalInterior = horizontal;
      label = { x: (start.x + end.x) / 2, y: (start.y + end.y) / 2 };
    }
  }
  return { points, path: roundedPath(points), label };
}

/** Route from the live Right/Left handles; null retains the ordinary smooth-step path. */
export function routedEdgeGeometry(
  source: Point,
  target: Point,
  routing: EdgeRouting,
): EdgeGeometry | null {
  if (routing.points) {
    const points = routing.points;
    const originalSource = points[0]!;
    const originalTarget = points.at(-1)!;
    if (source.x === originalSource.x && source.y === originalSource.y &&
      target.x === originalTarget.x && target.y === originalTarget.y) return geometryFromPoints(points);
    // Extend the existing approach channels to moved handles. Moving an entire
    // vertical leg avoids inserting tiny side steps beside the cached interior.
    const moved = points.map((point) => ({ ...point }));
    moved[0] = source;
    moved[moved.length - 1] = target;
    if (moved.length === 2 && source.y === target.y && target.x > source.x) return geometryFromPoints(moved);
    if (moved.length <= 4 && target.x > source.x) {
      const run = Math.min(CORNER_RADIUS, (target.x - source.x) / 4);
      const channel = Math.max(source.x + run,
        Math.min(points[1]!.x, target.x - run));
      return geometryFromPoints(simplifyPoints([source, { x: channel, y: source.y },
        { x: channel, y: target.y }, target]));
    }
    if (moved.length >= 6) {
      const gap = target.x - source.x;
      const run = gap > 0 ? Math.min(CORNER_RADIUS, gap / 4) : CORNER_RADIUS;
      let firstX = Math.max(points[1]!.x, source.x + run);
      let lastX = Math.min(points.at(-2)!.x, target.x - run);
      if (gap > 0 && lastX - firstX < run * 2) {
        firstX = source.x + gap / 4;
        lastX = target.x - gap / 4;
      }
      moved[1] = { x: firstX, y: source.y };
      moved[2]!.x = firstX;
      moved[moved.length - 3]!.x = lastX;
      moved[moved.length - 2] = { x: lastX, y: target.y };
      return geometryFromPoints(simplifyPoints(moved));
    }
    return routedEdgeGeometry(source, target, { ...routing, points: undefined, separated: true });
  }
  if (!routing.separated && target.x > source.x) return null;

  const clearance = ENDPOINT_CLEARANCE + routing.lane * LANE_SPACING;
  let sourceOutside = source.x + clearance;
  let targetOutside = target.x - clearance;
  if (!routing.selfLoop && Math.abs(source.y - target.y) >= RESOURCE_HEIGHT) {
    // With vertically separated nodes, go around both horizontal extents so
    // dragging one above the other cannot pull a return leg through its body.
    sourceOutside = Math.max(source.x, target.x + RESOURCE_WIDTH) + clearance;
    targetOutside = Math.min(target.x, source.x - RESOURCE_WIDTH) - clearance;
  } else if (target.x > source.x) {
    const clearance = Math.min(ENDPOINT_CLEARANCE, (target.x - source.x) / 4);
    sourceOutside = source.x + clearance;
    targetOutside = target.x - clearance;
  }
  const distance = RESOURCE_HEIGHT / 2 + NODE_CLEARANCE + routing.lane * LANE_SPACING;
  const laneY = routing.side < 0
    ? Math.min(source.y, target.y) - distance
    : Math.max(source.y, target.y) + distance;
  const points = [
    source,
    { x: sourceOutside, y: source.y },
    { x: sourceOutside, y: laneY },
    { x: targetOutside, y: laneY },
    { x: targetOutside, y: target.y },
    target,
  ];
  return {
    path: roundedPath(points),
    label: { x: (sourceOutside + targetOutside) / 2, y: laneY },
    points,
  };
}

/** Include routed paths and inspector hit areas when framing a completed layout. */
export function drawingBounds(
  items: ReadonlyArray<LayoutItem>,
  edges: ReadonlyArray<TopologyEdge>,
  routing: ReadonlyMap<string, EdgeRouting>,
): { x: number; y: number; width: number; height: number } {
  const byId = absoluteItems(items);
  let left = Infinity;
  let top = Infinity;
  let right = -Infinity;
  let bottom = -Infinity;
  const include = (point: Point, padding = 0) => {
    left = Math.min(left, point.x - padding);
    top = Math.min(top, point.y - padding);
    right = Math.max(right, point.x + padding);
    bottom = Math.max(bottom, point.y + padding);
  };
  for (const item of byId.values()) {
    include(item);
    include({ x: item.x + item.width, y: item.y + item.height });
  }
  for (const edge of edges) {
    const source = byId.get(edge.source);
    const target = byId.get(edge.target);
    if (!source || !target) continue;
    const geometry = routedEdgeGeometry(
      { x: source.x + source.width + HANDLE_OUTSET, y: source.y + source.height / 2 },
      { x: target.x - HANDLE_OUTSET, y: target.y + target.height / 2 },
      routing.get(edge.id)!,
    );
    for (const point of geometry?.points ?? []) include(point, INSPECTOR_PADDING);
  }
  return items.length === 0
    ? { x: 0, y: 0, width: 0, height: 0 }
    : { x: left, y: top, width: right - left, height: bottom - top };
}
