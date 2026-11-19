import { describe, expect, it } from "vitest";
import { paddedObstacles, routeAroundObstacles, simplifyPoints } from "./obstacle-routing";

describe("simple relationship paths", () => {
  it("removes a target approach that overshoots and crosses its own final stem", () => {
    expect(simplifyPoints([
      { x: 200, y: 0 }, { x: 240, y: 0 }, { x: 240, y: 160 },
      { x: -48, y: 160 }, { x: -48, y: 80 }, { x: -52, y: 80 },
      { x: -52, y: 96 }, { x: 0, y: 96 },
    ])).toEqual([
      { x: 200, y: 0 }, { x: 240, y: 0 }, { x: 240, y: 160 },
      { x: -48, y: 160 }, { x: -48, y: 96 }, { x: 0, y: 96 },
    ]);
  });

  it("erases a detour crossing the source stem and cancels collinear reversals", () => {
    expect(simplifyPoints([
      { x: 0, y: 0 }, { x: 48, y: 0 }, { x: 48, y: -40 },
      { x: 12, y: -40 }, { x: 12, y: 20 }, { x: 100, y: 20 },
    ])).toEqual([{ x: 0, y: 0 }, { x: 12, y: 0 }, { x: 12, y: 20 }, { x: 100, y: 20 }]);
    expect(simplifyPoints([
      { x: 0, y: 0 }, { x: 40, y: 0 }, { x: 20, y: 0 }, { x: 20, y: 20 },
    ])).toEqual([{ x: 0, y: 0 }, { x: 20, y: 0 }, { x: 20, y: 20 }]);
  });

  it("preserves a valid self-loop between the resource's distinct right and left handles", () => {
    const loop = [{ x: 220, y: 41 }, { x: 268, y: 41 }, { x: 268, y: -28 },
      { x: -48, y: -28 }, { x: -48, y: 41 }, { x: 0, y: 41 }];
    expect(simplifyPoints(loop)).toEqual(loop);
  });

  it("keeps short forward connections from doubling back between their endpoint stems", () => {
    const source = { x: 224.5, y: 141 };
    const target = { x: 271.5, y: 157 };
    const obstacles = paddedObstacles([
      { id: "a", left: 0, right: 220, top: 100, bottom: 182 },
      { id: "b", left: 276, right: 496, top: 116, bottom: 198 },
    ]);
    const points = routeAroundObstacles(source, target, "a", "b", obstacles,
      [source, { x: 248, y: 141 }, { x: 248, y: 157 }, target], [], []);
    expect(points[0]).toEqual(source);
    expect(points.at(-1)).toEqual(target);
    for (let index = 1; index < points.length; index += 1) {
      expect(points[index]!.x).toBeGreaterThanOrEqual(points[index - 1]!.x);
    }
  });

  it("avoids a nearby reserved run in the neighboring bucket across zero", () => {
    const source = { x: 0, y: -0.5 };
    const target = { x: 240, y: -0.5 };
    const points = routeAroundObstacles(source, target, "a", "b", [], [source, target], [{
      sourceId: "c",
      targetId: "d",
      points: [{ x: 48, y: 0.5 }, { x: 192, y: 0.5 }],
    }], []);

    expect(points).toEqual([
      source, { x: 48, y: -0.5 }, { x: 48, y: -23.5 },
      { x: 192, y: -23.5 }, { x: 192, y: -0.5 }, target,
    ]);
  });

  it("uses a single return channel instead of stairs when the channel is clear", () => {
    const source = { x: 524.5, y: 95 };
    const target = { x: 19.5, y: 95 };
    const obstacles = paddedObstacles([
      { id: "a", left: 24, right: 244, top: 54, bottom: 136 },
      { id: "b", left: 300, right: 520, top: 54, bottom: 136 },
      { id: "heading", left: 12, right: 1088, top: 8, bottom: 34 },
    ]);
    const points = routeAroundObstacles(source, target, "b", "a", obstacles,
      [source, { x: 572.5, y: 95 }, { x: 572.5, y: 164 }, { x: -28.5, y: 164 }, { x: -28.5, y: 95 }, target],
      [], [{ id: "group", left: 0, right: 1100, top: 0, bottom: 400 }]);
    expect(points).toHaveLength(6);
    expect(points[1]!.x).toBe(points[2]!.x);
    expect(points[2]!.y).toBe(points[3]!.y);
    expect(points[3]!.x).toBe(points[4]!.x);
  });
});
