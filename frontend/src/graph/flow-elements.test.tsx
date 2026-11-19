import { fireEvent, render, screen } from "@testing-library/react";
import type { NodeProps } from "@xyflow/react";
import { describe, expect, it, vi } from "vitest";
import { topologyFixture } from "../test/api-fixtures";
import { ExchangeNodeRenderer, type ResourceFlowNode } from "./flow-elements";

// The browser suite exercises real handles; these tests isolate resource text
// and style classes without requiring a React Flow store or layout measurements.
vi.mock("@xyflow/react", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@xyflow/react")>()),
  Handle: () => null,
}));

function resourceProps(
  componentIndex: number | null,
  cycleStep: number | null = null,
): NodeProps<ResourceFlowNode> {
  return {
    id: topologyFixture.nodes[0].id,
    type: "exchange",
    data: {
      wireNode: topologyFixture.nodes[0],
      searchMatch: false,
      componentIndex,
      cycleStep,
      onSelect: vi.fn(),
    },
    width: 210,
    height: 64,
    selected: true,
    dragging: false,
    draggable: true,
    selectable: true,
    deletable: true,
    isConnectable: true,
    positionAbsoluteX: 0,
    positionAbsoluteY: 0,
    zIndex: 1,
  };
}

describe("ResourceNodeRenderer", () => {
  it.each([0, 1, 7])("keeps SCC %i colors and resource selection without an index label", (index) => {
    const props = resourceProps(index);
    const { container } = render(<ExchangeNodeRenderer {...props} />);
    const resource = screen.getByRole("button", { name: "Inspect exchange orders.events, vhost /orders" });

    expect(resource).toHaveClass(
      "resource-node--scc", `scc-tone-${index % 6}`, "resource-node--selected",
    );
    expect(screen.getByText("orders.events")).toBeVisible();
    expect(screen.queryByText(/^C\d+$/)).not.toBeInTheDocument();
    expect(container.querySelector(".resource-node__component")).toBeNull();
    expect(container.querySelector(".resource-node__cycle-step")).toBeNull();
    fireEvent.click(resource);
    fireEvent.keyDown(resource, { key: "Enter" });
    fireEvent.keyDown(resource, { key: " " });
    expect(props.data.onSelect).toHaveBeenCalledTimes(3);
    expect(props.data.onSelect).toHaveBeenLastCalledWith(props.id);
  });

  it("retains the cycle hop badge alongside SCC colors and selection", () => {
    render(<ExchangeNodeRenderer {...resourceProps(1, 2)} />);
    const resource = screen.getByRole("button", { name: "Inspect exchange orders.events, vhost /orders" });

    expect(resource).toHaveClass(
      "resource-node--scc", "scc-tone-1", "resource-node--cycle", "resource-node--selected",
    );
    expect(screen.getByText("P3")).toHaveClass("resource-node__cycle-step");
    expect(screen.queryByText(/^C\d+$/)).not.toBeInTheDocument();
  });

  it("keeps an ordinary topology resource free of SCC and cycle styling", () => {
    render(<ExchangeNodeRenderer {...resourceProps(null)} />);
    const resource = screen.getByRole("button", { name: "Inspect exchange orders.events, vhost /orders" });

    expect(resource).not.toHaveClass("resource-node--scc");
    expect(resource).not.toHaveClass("resource-node--cycle");
    expect(screen.queryByText(/^[CP]\d+$/)).not.toBeInTheDocument();
  });
});
