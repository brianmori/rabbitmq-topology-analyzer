import type { Point } from "./edge-routing";

export interface RoutingObstacle {
  id: string;
  left: number;
  top: number;
  right: number;
  bottom: number;
}

export interface RoutingPath {
  sourceId: string;
  targetId: string;
  points: ReadonlyArray<Point>;
}

interface Segment {
  start: Point;
  end: Point;
  owners: ReadonlyArray<string>;
}

const segmentCache = new WeakMap<RoutingPath, ReadonlyArray<Segment>>();

const CLEARANCE = 16;
const ENDPOINT_RUN = 48;
const CHANNEL_SPACING = 36;
export const BORDER_CLEARANCE = 24;
const PATH_CLEARANCE = 24;

/** Split off only the short common handle stems, never an entire long run. */
function segmentsForPath(path: RoutingPath): ReadonlyArray<Segment> {
  const cached = segmentCache.get(path);
  if (cached) return cached;
  const gap = path.points.at(-1)!.x - path.points[0]!.x;
  const stemLength = gap > 0 && gap < ENDPOINT_RUN * 2 ? gap / 4 : ENDPOINT_RUN;
  const segments = path.points.slice(1).flatMap((end, index) => {
    const start = path.points[index]!;
    const length = Math.hypot(end.x - start.x, end.y - start.y);
    if (length === 0) return [];
    const first = index === 0;
    const last = index === path.points.length - 2;
    const cuts = [0, length];
    if (first && length > stemLength) cuts.push(stemLength);
    if (last && length > stemLength) cuts.push(length - stemLength);
    const positions = [...new Set(cuts)].sort((a, b) => a - b);
    const at = (distance: number) => ({ x: start.x + (end.x - start.x) * distance / length,
      y: start.y + (end.y - start.y) * distance / length });
    return positions.slice(1).map((distance, step) => {
      const from = positions[step]!;
      const owners: string[] = [];
      if (first && distance <= stemLength) owners.push(path.sourceId);
      if (last && from >= length - stemLength) owners.push(path.targetId);
      return { start: at(from), end: at(distance), owners };
    });
  });
  segmentCache.set(path, segments);
  return segments;
}

function segmentsTooClose(left: Segment, right: Segment, clearance = PATH_CLEARANCE): boolean {
  if (left.owners.some((owner) => right.owners.includes(owner))) return false;
  const horizontal = left.start.y === left.end.y;
  if (horizontal !== (right.start.y === right.end.y)) return false;
  const along = horizontal ? "x" : "y";
  const across = horizontal ? "y" : "x";
  return Math.abs(left.start[across] - right.start[across]) < clearance &&
    Math.max(Math.min(left.start[along], left.end[along]), Math.min(right.start[along], right.end[along])) <
    Math.min(Math.max(left.start[along], left.end[along]), Math.max(right.start[along], right.end[along]));
}

export function pathsShareLane(left: RoutingPath, right: RoutingPath): boolean {
  return segmentsForPath(left).some((segment) => segmentsForPath(right).some((other) => segmentsTooClose(segment, other)));
}

function indexNearbySegments(segments: ReadonlyArray<Segment>) {
  const horizontal = new Map<number, Segment[]>();
  const vertical = new Map<number, Segment[]>();
  for (const segment of segments) {
    const isHorizontal = segment.start.y === segment.end.y;
    const buckets = isHorizontal ? horizontal : vertical;
    const coordinate = isHorizontal ? segment.start.y : segment.start.x;
    const bucket = Math.floor(coordinate / PATH_CLEARANCE);
    // A segment can be within clearance of moves in its own or an adjacent
    // bucket. Insert into all three so each query visits candidates in the
    // original order and preserves the order of floating-point additions.
    for (const offset of [-1, 0, 1]) {
      const key = bucket + offset;
      const candidates = buckets.get(key);
      if (candidates) candidates.push(segment);
      else buckets.set(key, [segment]);
    }
  }
  return { horizontal, vertical };
}

function hugsBorder(start: Point, end: Point, box: RoutingObstacle): boolean {
  const horizontal = start.y === end.y;
  const along = horizontal ? "x" : "y";
  const across = horizontal ? "y" : "x";
  const low = horizontal ? box.left : box.top;
  const high = horizontal ? box.right : box.bottom;
  const near = horizontal ? [box.top, box.bottom] : [box.left, box.right];
  return Math.max(start[along], end[along]) > low - BORDER_CLEARANCE &&
    Math.min(start[along], end[along]) < high + BORDER_CLEARANCE &&
    near.some((coordinate) => Math.abs(start[across] - coordinate) < BORDER_CLEARANCE);
}

export function pathHugsBorder(points: ReadonlyArray<Point>, borders: ReadonlyArray<RoutingObstacle>): boolean {
  return points.slice(1).some((end, index) => borders.some((box) => hugsBorder(points[index]!, end, box)));
}

export function paddedObstacles(obstacles: ReadonlyArray<RoutingObstacle>): RoutingObstacle[] {
  return obstacles.map((obstacle) => ({
    id: obstacle.id,
    left: obstacle.left - CLEARANCE,
    top: obstacle.top - CLEARANCE,
    right: obstacle.right + CLEARANCE,
    bottom: obstacle.bottom + CLEARANCE,
  }));
}

function intersects(start: Point, end: Point, box: RoutingObstacle): boolean {
  return start.y === end.y
    ? start.y > box.top && start.y < box.bottom &&
      Math.max(start.x, end.x) > box.left && Math.min(start.x, end.x) < box.right
    : start.x > box.left && start.x < box.right &&
      Math.max(start.y, end.y) > box.top && Math.min(start.y, end.y) < box.bottom;
}

/** Endpoint stubs may leave/enter their own padded resource, but nothing else. */
export function pathHitsObstacle(
  points: ReadonlyArray<Point>,
  obstacles: ReadonlyArray<RoutingObstacle>,
  sourceId: string,
  targetId: string,
): boolean {
  return points.slice(1).some((end, index) => obstacles.some((box) =>
    !(index === 0 && box.id === sourceId) &&
    !(index === points.length - 2 && box.id === targetId) &&
    intersects(points[index]!, end, box),
  ));
}

export function simplifyPoints(points: ReadonlyArray<Point>): Point[] {
  const result: Point[] = [];
  const append = (point: Point) => {
    if (result.at(-1)?.x === point.x && result.at(-1)?.y === point.y) return;
    while (result.length >= 2) {
      const previous = result.at(-2)!;
      const last = result.at(-1)!;
      if (!((previous.x === last.x && last.x === point.x) ||
        (previous.y === last.y && last.y === point.y))) break;
      // Collinear reversals retrace a segment and can be removed as well.
      result.pop();
    }
    if (result.at(-1)?.x !== point.x || result.at(-1)?.y !== point.y) result.push(point);
  };
  for (const point of points) {
    append(point);
    // The searched interior may cross a fixed endpoint stem, or revisit a
    // location with a different arrival direction. Erase that geometric loop
    // by retaining the existing segments up to and after their intersection.
    let changed = true;
    while (changed && result.length >= 4) {
      changed = false;
      const start = result.at(-2)!;
      const end = result.at(-1)!;
      for (let index = 0; index < result.length - 3; index += 1) {
        const intersection = segmentIntersection(result[index]!, result[index + 1]!, start, end);
        if (!intersection) continue;
        result.splice(index + 1);
        append(intersection);
        append(end);
        changed = true;
        break;
      }
    }
  }
  return result;
}

function segmentIntersection(a: Point, b: Point, c: Point, d: Point): Point | null {
  const within = (value: number, from: number, to: number) => value >= Math.min(from, to) && value <= Math.max(from, to);
  const horizontal = a.y === b.y;
  if (horizontal !== (c.y === d.y)) {
    const point = horizontal ? { x: c.x, y: a.y } : { x: a.x, y: c.y };
    return within(point.x, a.x, b.x) && within(point.y, a.y, b.y) &&
      within(point.x, c.x, d.x) && within(point.y, c.y, d.y) ? point : null;
  }
  const along = horizontal ? "x" : "y";
  const across = horizontal ? "y" : "x";
  if (a[across] !== c[across]) return null;
  const low = Math.max(Math.min(a[along], b[along]), Math.min(c[along], d[along]));
  const high = Math.min(Math.max(a[along], b[along]), Math.max(c[along], d[along]));
  if (low > high) return null;
  const coordinate = c[along] <= d[along] ? low : high;
  return horizontal ? { x: coordinate, y: a.y } : { x: a.x, y: coordinate };
}

interface SearchEntry {
  state: number;
  cost: number;
  priority: number;
}

/** Small deterministic priority queue for the orthogonal visibility search. */
class SearchQueue {
  private entries: SearchEntry[] = [];

  push(entry: SearchEntry): void {
    let index = this.entries.length;
    this.entries.push(entry);
    while (index > 0) {
      const parent = (index - 1) >>> 1;
      if (this.entries[parent]!.priority <= entry.priority) break;
      this.entries[index] = this.entries[parent]!;
      index = parent;
    }
    this.entries[index] = entry;
  }

  pop(): SearchEntry | undefined {
    const first = this.entries[0];
    const last = this.entries.pop();
    if (!last || this.entries.length === 0) return first;
    let index = 0;
    while (index * 2 + 1 < this.entries.length) {
      let child = index * 2 + 1;
      if (child + 1 < this.entries.length &&
        this.entries[child + 1]!.priority < this.entries[child]!.priority) child += 1;
      if (last.priority <= this.entries[child]!.priority) break;
      this.entries[index] = this.entries[child]!;
      index = child;
    }
    this.entries[index] = last;
    return first;
  }
}

/**
 * Search only when a simple drawing lane hits a node or vhost heading. The
 * coordinate grid follows obstacle boundaries; no resource positions change.
 * Perpendicular crossings are allowed. Shared runs incur a large cost, while
 * border-hugging segments are excluded. Common handle stems may be shared.
 */
export function routeAroundObstacles(
  source: Point,
  target: Point,
  sourceId: string,
  targetId: string,
  obstacles: ReadonlyArray<RoutingObstacle>,
  preferred: ReadonlyArray<Point>,
  reserved: ReadonlyArray<RoutingPath>,
  borders: ReadonlyArray<RoutingObstacle>,
): ReadonlyArray<Point> {
  // A local connection should not search the coordinate grid of every vhost.
  // Widen the region only when its nearby channels cannot provide a route.
  for (const margin of [140, 420, Infinity]) {
    const region = { id: "routing-region", left: Math.min(source.x, target.x) - margin,
      right: Math.max(source.x, target.x) + margin, top: Math.min(source.y, target.y) - margin,
      bottom: Math.max(source.y, target.y) + margin };
    const overlaps = (box: RoutingObstacle) => box.left <= region.right && box.right >= region.left &&
      box.top <= region.bottom && box.bottom >= region.top;
    const paths = reserved.filter((path) => overlaps({ id: "path",
      left: Math.min(...path.points.map((point) => point.x)), right: Math.max(...path.points.map((point) => point.x)),
      top: Math.min(...path.points.map((point) => point.y)), bottom: Math.max(...path.points.map((point) => point.y)) }));
    const points = searchRegion(source, target, sourceId, targetId, obstacles.filter(overlaps), preferred,
      paths, borders.filter(overlaps), region);
    if (points) return points;
  }
  throw new Error(`No clear relationship path exists from ${sourceId} to ${targetId}.`);
}

function searchRegion(
  source: Point,
  target: Point,
  sourceId: string,
  targetId: string,
  obstacles: ReadonlyArray<RoutingObstacle>,
  preferred: ReadonlyArray<Point>,
  reserved: ReadonlyArray<RoutingPath>,
  borders: ReadonlyArray<RoutingObstacle>,
  region: RoutingObstacle,
): ReadonlyArray<Point> | null {
  const segments = reserved.flatMap((path) => segmentsForPath(path)).filter(({ start, end }) =>
    Math.max(start.x, end.x) >= region.left - PATH_CLEARANCE && Math.min(start.x, end.x) <= region.right + PATH_CLEARANCE &&
    Math.max(start.y, end.y) >= region.top - PATH_CLEARANCE && Math.min(start.y, end.y) <= region.bottom + PATH_CLEARANCE);
  const nearbySegments = indexNearbySegments(segments);
  const parallelSegments = new Set(reserved.filter((path) => path.sourceId === sourceId && path.targetId === targetId)
    .flatMap((path) => segmentsForPath(path)));
  const escape = (point: Point, direction: 1 | -1, owner: string): Point => {
    // Short forward gaps cannot fit two full stems without passing each other.
    const gap = target.x - source.x;
    let distance = gap > 0 && gap < ENDPOINT_RUN * 2 ? gap / 4 : ENDPOINT_RUN;
    for (const box of obstacles) {
      if (box.id === owner || point.y <= box.top || point.y >= box.bottom) continue;
      const gap = direction === 1 ? box.left - point.x : point.x - box.right;
      if (gap > 0) distance = Math.min(distance, gap);
    }
    return { x: point.x + direction * distance, y: point.y };
  };
  const start = escape(source, 1, sourceId);
  const goal = escape(target, -1, targetId);
  const coordinates = (axis: "x" | "y") => {
    const low = axis === "x" ? "left" : "top";
    const high = axis === "x" ? "right" : "bottom";
    const values = [start[axis], goal[axis], ...preferred.map((point) => point[axis]),
      ...obstacles.flatMap((box) => [box[low], box[high]]),
      ...borders.flatMap((box) => [box[low] - BORDER_CLEARANCE, box[low] + BORDER_CLEARANCE,
        box[high] - BORDER_CLEARANCE, box[high] + BORDER_CLEARANCE])];
    // Supply an alternative channel on either side, including outside groups.
    const lanes = preferred.flatMap((point) => [point[axis] - CHANNEL_SPACING, point[axis] + CHANNEL_SPACING]);
    const localLow = Math.min(source[axis], target[axis]) - ENDPOINT_RUN;
    const localHigh = Math.max(source[axis], target[axis]) + ENDPOINT_RUN;
    const occupied = segments.filter(({ start, end }) => axis === "x" ? start.x === end.x : start.y === end.y)
      .map(({ start }) => start[axis]);
    const alternatives = occupied.filter((value) => value >= localLow && value <= localHigh)
      .flatMap((value) => [value - PATH_CLEARANCE, value + PATH_CLEARANCE]);
    const outsideLow = Math.max(region[low], Math.min(...values, ...occupied) - ENDPOINT_RUN);
    const outsideHigh = Math.min(region[high], Math.max(...values, ...occupied) + ENDPOINT_RUN);
    return [...new Set([...values, ...lanes, ...alternatives, outsideLow, outsideHigh]
      .filter((value) => value >= region[low] && value <= region[high]))].sort((left, right) => left - right);
  };
  const xs = coordinates("x");
  const ys = coordinates("y");
  const width = xs.length;
  const pointAt = (cell: number): Point => ({ x: xs[cell % width]!, y: ys[Math.floor(cell / width)]! });
  const startCell = ys.indexOf(start.y) * width + xs.indexOf(start.x);
  const goalCell = ys.indexOf(goal.y) * width + xs.indexOf(goal.x);
  const startState = startCell * 2;
  const costs = new Map<number, number>([[startState, 0]]);
  const previous = new Map<number, number>();
  const movementCosts = new Map<number, number>();
  const queue = new SearchQueue();
  // The admissible distance preserves the turn cost: an early move toward the
  // target must not win by introducing extra stairs of equal total length.
  const heuristic = (point: Point) => Math.abs(point.x - goal.x) + Math.abs(point.y - goal.y);
  queue.push({ state: startState, cost: 0, priority: heuristic(start) });
  const segmentAt = (from: Point, to: Point): Segment => {
    const owners: string[] = [];
    for (const [handle, id] of [[source, sourceId], [target, targetId]] as const) {
      if (from.y === handle.y && to.y === handle.y &&
        Math.max(Math.abs(from.x - handle.x), Math.abs(to.x - handle.x)) <= ENDPOINT_RUN) owners.push(id);
    }
    return { start: from, end: to, owners };
  };
  const nearbyRunCost = (segment: Segment) => {
    const horizontal = segment.start.y === segment.end.y;
    const along = horizontal ? "x" : "y";
    const across = horizontal ? "y" : "x";
    const bucket = Math.floor(segment.start[across] / PATH_CLEARANCE);
    const candidates = (horizontal ? nearbySegments.horizontal : nearbySegments.vertical).get(bucket);
    return candidates?.reduce((cost, other) => {
      if (!segmentsTooClose(segment, other)) return cost;
      const overlap = Math.min(Math.max(segment.start[along], segment.end[along]), Math.max(other.start[along], other.end[along])) -
        Math.max(Math.min(segment.start[along], segment.end[along]), Math.min(other.start[along], other.end[along]));
      const proximity = 1 - Math.abs(segment.start[across] - other.start[across]) / PATH_CLEARANCE;
      return cost + overlap * proximity * (parallelSegments.has(other) ? 128 : 8);
    }, 0) ?? 0;
  };
  let entry: SearchEntry | undefined;
  while ((entry = queue.pop())) {
    if (entry.cost !== costs.get(entry.state)) continue;
    const cell = Math.floor(entry.state / 2);
    const point = pointAt(cell);
    if (cell === goalCell) {
      const path: Point[] = [target];
      let state: number | undefined = entry.state;
      while (state !== undefined) {
        path.push(pointAt(Math.floor(state / 2)));
        state = previous.get(state);
      }
      path.push(source);
      return simplifyPoints(path.reverse());
    }
    const x = cell % width;
    const y = Math.floor(cell / width);
    for (const [nextX, nextY, direction] of [[x + 1, y, 0], [x - 1, y, 0], [x, y + 1, 1], [x, y - 1, 1]]) {
      if (nextX! < 0 || nextX! >= width || nextY! < 0 || nextY! >= ys.length) continue;
      const nextCell = nextY! * width + nextX!;
      const nextPoint = pointAt(nextCell);
      if (cell === startCell && nextPoint.x < point.x) continue;
      if (nextCell === goalCell && nextPoint.x < point.x) continue;
      const movementKey = Math.min(cell, nextCell) * 2 + direction!;
      let movementCost = movementCosts.get(movementKey);
      if (movementCost === undefined) {
        if (obstacles.some((box) => intersects(point, nextPoint, box)) ||
          borders.some((box) => hugsBorder(point, nextPoint, box))) movementCost = Infinity;
        else movementCost = Math.abs(point.x - nextPoint.x) + Math.abs(point.y - nextPoint.y) +
          nearbyRunCost(segmentAt(point, nextPoint));
        movementCosts.set(movementKey, movementCost);
      }
      if (!Number.isFinite(movementCost)) continue;
      const nextState = nextCell * 2 + direction!;
      const cost = entry.cost + movementCost + (entry.state % 2 === direction ? 0 : 24);
      if (cost >= (costs.get(nextState) ?? Infinity)) continue;
      costs.set(nextState, cost);
      previous.set(nextState, entry.state);
      queue.push({ state: nextState, cost, priority: cost + heuristic(nextPoint) });
    }
  }
  return null;
}
