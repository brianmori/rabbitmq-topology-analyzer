import { describe, expect, it } from "vitest";
import { cycleDetailFixture, succeededSccRunFixture, topologyFixture } from "../test/api-fixtures";
import type { TopologyResponse } from "../types/topology";
import {
  ALL_NODE_KINDS,
  componentIndexByNode,
  cycleSteps,
  deriveCycleVisibleTopology,
  deriveSccVisibleTopology,
  deriveVisibleTopology,
  findResourceMatches,
  topologyVhosts,
} from "./presentation";

const allVhosts = new Set(topologyVhosts(topologyFixture));
const allKinds = new Set(ALL_NODE_KINDS);

describe("topology presentation derivation", () => {
  it("starts with all vhosts, kinds, and default exchanges visible", () => {
    const visible = deriveVisibleTopology(topologyFixture, allVhosts, allKinds, false);

    expect(visible.nodes).toHaveLength(topologyFixture.nodes.length);
    expect(visible.edges).toHaveLength(topologyFixture.edges.length);
    expect(
      visible.nodes.some((node) => node.data.kind === "exchange" && node.data.isDefault === true),
    ).toBe(true);
  });

  it("uses only isDefault to hide the SDK-classified default exchange", () => {
    const visible = deriveVisibleTopology(topologyFixture, allVhosts, allKinds, true);

    expect(visible.nodes.map((node) => node.id)).not.toContain("n1.default-exchange");
    expect(visible.nodes.map((node) => node.id)).toContain("n1.orders-exchange");
  });

  it("removes hidden kinds, their missing placeholders, and incident edges", () => {
    const withoutExchanges = deriveVisibleTopology(
      topologyFixture,
      allVhosts,
      new Set(["queue", "shovel"]),
      false,
    );

    expect(withoutExchanges.nodes.every((node) => node.type !== "exchange")).toBe(true);
    expect(withoutExchanges.nodes.map((node) => node.id)).not.toContain("m1.missing-dlx");
    expect(withoutExchanges.edges.map((edge) => edge.id)).toEqual(["e1.2", "e1.3", "e1.4"]);
  });

  it("treats selecting no vhosts as an empty graph", () => {
    const visible = deriveVisibleTopology(topologyFixture, new Set(), allKinds, false);

    expect(visible.nodes).toEqual([]);
    expect(visible.edges).toEqual([]);
  });

  it("searches visible names case-insensitively without changing membership", () => {
    const visible = deriveVisibleTopology(topologyFixture, allVhosts, allKinds, false);
    const matches = findResourceMatches(visible.nodes, "ORDERS.PRO");

    expect([...matches]).toEqual(["n1.orders-queue"]);
    expect(visible.nodes).toHaveLength(topologyFixture.nodes.length);
    expect(visible.edges).toHaveLength(topologyFixture.edges.length);
  });

  it("focuses complete selected SCCs with every original connecting edge", () => {
    const components = succeededSccRunFixture.result.components;
    const selected = new Set([0, 1]);

    const focused = deriveSccVisibleTopology(topologyFixture, components, selected);
    const membership = componentIndexByNode(components, selected);

    expect(focused.nodes.map((node) => node.id)).toEqual([
      "n1.orders-exchange",
      "n1.orders-queue",
      "n1.audit-queue",
      "n1.cross-vhost-shovel",
    ]);
    expect(focused.edges.map((edge) => edge.id)).toEqual(["e1.0", "e1.2", "e1.3", "e1.4"]);
    expect(membership.get("n1.orders-exchange")).toBe(0);
    expect(membership.get("n1.audit-queue")).toBe(1);
    expect(topologyFixture.nodes).toHaveLength(6);
    expect(topologyFixture.edges).toHaveLength(5);
  });

  it("focuses exactly the SDK cycle nodes and representative parallel-safe edges", () => {
    const parallelEdge = { ...topologyFixture.edges[2], id: "e1.parallel-shovel" } as const;
    const topologyWithParallelEdge = {
      ...topologyFixture,
      edges: [...topologyFixture.edges, parallelEdge],
    } as const satisfies TopologyResponse;
    const focused = deriveCycleVisibleTopology(topologyWithParallelEdge, cycleDetailFixture);
    const steps = cycleSteps(cycleDetailFixture);

    expect(focused.nodes.map((node) => node.id)).toEqual([
      "n1.orders-queue",
      "n1.audit-queue",
      "n1.cross-vhost-shovel",
    ]);
    expect(focused.edges.map((edge) => edge.id)).toEqual(["e1.2", "e1.3", "e1.4"]);
    expect(focused.edges.map((edge) => edge.id)).not.toContain(parallelEdge.id);
    expect(steps.nodes.get("n1.orders-queue")).toBe(0);
    expect(steps.nodes.get("n1.audit-queue")).toBe(2);
    expect(steps.edges.get("e1.2")).toBe(0);
    expect(steps.edges.get("e1.4")).toBe(2);
    expect(topologyFixture.nodes).toHaveLength(6);
    expect(topologyWithParallelEdge.edges).toHaveLength(6);
  });
});
