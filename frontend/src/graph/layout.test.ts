import { describe, expect, it } from "vitest";
import { topologyFixture } from "../test/api-fixtures";
import { createElkGraph, createLayoutRequest, vhostGroupId } from "./layout";
import { deriveVisibleTopology, topologyVhosts, ALL_NODE_KINDS } from "./presentation";

describe("compound ELK input", () => {
  it("uses exactly one vhost parent level and preserves cross-vhost endpoints", () => {
    const visible = deriveVisibleTopology(
      topologyFixture,
      new Set(topologyVhosts(topologyFixture)),
      new Set(ALL_NODE_KINDS),
      false,
    );
    const graph = createElkGraph(createLayoutRequest(7, visible));

    expect(graph.children?.map((group) => group.id)).toEqual([
      vhostGroupId("/audit"),
      vhostGroupId("/orders"),
    ]);
    expect(graph.children?.every((group) => group.children?.every((node) => !node.children))).toBe(
      true,
    );
    expect(graph.edges).toHaveLength(topologyFixture.edges.length);
    expect(graph.edges).toContainEqual(
      expect.objectContaining({
        id: "e1.3",
        sources: ["n1.cross-vhost-shovel"],
        targets: ["n1.audit-queue"],
      }),
    );
  });
});
