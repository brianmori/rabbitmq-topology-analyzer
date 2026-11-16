import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { topologyFixture } from "../test/api-fixtures";
import type { TopologyNode } from "../types/topology";
import { DetailsPanel } from "./DetailsPanel";

function detailValue(label: string) {
  return screen.getByText(label, { selector: "dt" }).nextElementSibling?.textContent;
}

describe("DetailsPanel", () => {
  it("renders only the explicit safe node whitelist", () => {
    const shovel = topologyFixture.nodes.find((node) => node.type === "shovel")!;
    const nodeWithExcludedData = {
      ...shovel,
      data: {
        ...shovel.data,
        endpointAuthority: "secret.internal:5672",
        uri: "amqp://user:password@secret.internal",
        arguments: { password: "raw-secret" },
        source: {
          ...shovel.data.source,
          routingKey: "",
          uri: "amqp://user:password@nested-secret.internal",
          arguments: { password: "nested-raw-secret" },
        },
      },
    } as unknown as TopologyNode;

    render(
      <DetailsPanel
        selection={{ kind: "node", id: nodeWithExcludedData.id }}
        nodes={[nodeWithExcludedData]}
        edges={[]}
      />,
    );

    expect(screen.getByText("orders-to-audit")).toBeVisible();
    expect(screen.getByText("Source confirmed local")).toBeVisible();
    expect(screen.getByText("Destination resource name")).toBeVisible();
    expect(screen.queryByText(/secret\.internal/)).not.toBeInTheDocument();
    expect(screen.queryByText(/raw-secret/)).not.toBeInTheDocument();
    expect(screen.queryByText(/amqp:\/\//)).not.toBeInTheDocument();
    expect(screen.queryByText("endpointAuthority")).not.toBeInTheDocument();
    expect(detailValue("Source routing key")).toBe("(empty)");
    expect(screen.getAllByRole("term").map((term) => term.textContent)).toEqual([
      "Wire identity",
      "Resource kind",
      "Name",
      "Vhost",
      "Cluster ID",
      "Captured resource",
      "Source resource kind",
      "Source resource name",
      "Source vhost",
      "Source routing key",
      "Source confirmed local",
      "Destination resource kind",
      "Destination resource name",
      "Destination vhost",
      "Destination routing key",
      "Destination confirmed local",
      "Cross-vhost",
      "Unconfirmed endpoint",
    ]);
  });

  it.each([
    [null, "Not available"],
    ["", "(empty)"],
    ["orders.*", "orders.*"],
  ])("renders an edge routing key %j without changing its meaning", (routingKey, expected) => {
    const edge = {
      ...topologyFixture.edges[0],
      data: { ...topologyFixture.edges[0].data, routingKey },
      rawArguments: "binding-secret",
    };

    render(<DetailsPanel selection={{ kind: "edge", id: edge.id }} nodes={[]} edges={[edge]} />);

    expect(screen.getByText("Source wire identity")).toBeVisible();
    expect(detailValue("Routing key")).toBe(expected);
    expect(screen.queryByText("binding-secret")).not.toBeInTheDocument();
    expect(screen.getAllByRole("term").map((term) => term.textContent)).toEqual([
      "Source resource kind",
      "Source resource name",
      "Source vhost",
      "Source captured resource",
      "Target resource kind",
      "Target resource name",
      "Target vhost",
      "Target captured resource",
      "Relationship kind",
      "Routing key",
      "Wire identity",
      "Source wire identity",
      "Target wire identity",
    ]);
  });

  it("leads with safe endpoint names and vhosts while preserving exact edge identities and MISSING status", () => {
    const source = topologyFixture.nodes.find((node) => node.id === "n1.orders-queue")!;
    const target = topologyFixture.nodes.find((node) => node.data.isMissing)!;
    const edge = {
      ...topologyFixture.edges[0],
      source: source.id,
      target: target.id,
      data: { kind: "dead_letter" as const, routingKey: "returned" },
      rawArguments: "binding-secret",
    };
    const excludedSource = {
      ...source,
      data: {
        ...source.data,
        uri: "amqp://user:password@secret.internal",
        arguments: { password: "raw-secret" },
      },
    } as unknown as TopologyNode;
    render(
      <DetailsPanel
        selection={{ kind: "edge", id: edge.id }}
        nodes={[excludedSource, target]}
        edges={[edge]}
      />,
    );
    expect(
      screen.getByText(/queue Orders.Process in vhost \/orders → MISSING exchange/),
    ).toBeVisible();
    expect(detailValue("Source resource name")).toBe("Orders.Process");
    expect(detailValue("Source vhost")).toBe("/orders");
    expect(detailValue("Target resource name")).toBe(target.data.name);
    expect(detailValue("Target captured resource")).toBe("No — MISSING");
    expect(detailValue("Relationship kind")).toBe("dead letter");
    expect(detailValue("Wire identity")).toBe(edge.id);
    expect(detailValue("Source wire identity")).toBe(source.id);
    expect(detailValue("Target wire identity")).toBe(target.id);
    expect(screen.queryByText("binding-secret")).not.toBeInTheDocument();
    expect(screen.queryByText(/secret.internal|amqp:\/\/|raw-secret/)).not.toBeInTheDocument();
  });

  it.each([
    [null, "Not available", "Not available"],
    ["", "(empty)", "(unnamed default exchange)"],
    ["orders.*", "orders.*", "orders.*"],
  ])("formats shovel strings %j by field meaning", (value, expectedText, expectedExchange) => {
    const shovel = topologyFixture.nodes.find((node) => node.type === "shovel")!;
    const node: TopologyNode = {
      ...shovel,
      data: {
        ...shovel.data,
        source: {
          resourceKind: "exchange",
          resourceName: value,
          routingKey: value,
          vhost: value,
          isConfirmedLocal: true,
        },
        destination: {
          resourceKind: "queue",
          resourceName: value,
          routingKey: value,
          vhost: value,
          isConfirmedLocal: false,
        },
      },
    };

    render(<DetailsPanel selection={{ kind: "node", id: node.id }} nodes={[node]} edges={[]} />);

    expect(detailValue("Source resource name")).toBe(expectedExchange);
    expect(detailValue("Destination resource name")).toBe(expectedText);
    for (const prefix of ["Source", "Destination"]) {
      expect(detailValue(`${prefix} routing key`)).toBe(expectedText);
      expect(detailValue(`${prefix} vhost`)).toBe(expectedText);
    }
    expect(detailValue("Source confirmed local")).toBe("Yes");
    expect(detailValue("Destination confirmed local")).toBe("No");
  });

  it.each([
    ["exchange", "(unnamed default exchange)"],
    ["queue", "(empty)"],
    ["shovel", "(empty)"],
  ] as const)("formats an empty %s name as resource identity", (kind, expected) => {
    const original = topologyFixture.nodes.find(
      (node) => node.type === kind && (kind !== "exchange" || node.data.name === ""),
    )!;
    const node: TopologyNode = { ...original, data: { ...original.data, name: "" } };

    render(<DetailsPanel selection={{ kind: "node", id: node.id }} nodes={[node]} edges={[]} />);

    expect(detailValue("Name")).toBe(expected);
  });

  it.each([
    [null, "Not available"],
    ["", "(empty)"],
    ["cluster-1", "cluster-1"],
  ])("preserves null, empty, and populated metadata %j", (value, expected) => {
    const exchange = topologyFixture.nodes.find((node) => node.type === "exchange")!;
    const node: TopologyNode = {
      ...exchange,
      data: { ...exchange.data, clusterId: value, exchangeType: value },
    };

    render(<DetailsPanel selection={{ kind: "node", id: node.id }} nodes={[node]} edges={[]} />);

    expect(detailValue("Cluster ID")).toBe(expected);
    expect(detailValue("Exchange type")).toBe(expected);
  });

  it.each([
    [null, "Not available"],
    [false, "No"],
    [true, "Yes"],
  ] as const)("preserves nullable boolean %j", (value, expected) => {
    const exchange = topologyFixture.nodes.find((node) => node.type === "exchange")!;
    const node: TopologyNode = {
      ...exchange,
      data: { ...exchange.data, durable: value, internal: value, isDefault: value },
    };

    render(<DetailsPanel selection={{ kind: "node", id: node.id }} nodes={[node]} edges={[]} />);

    for (const label of ["Durable", "Internal", "Default exchange"]) {
      expect(detailValue(label)).toBe(expected);
    }
  });

  it("keeps absent endpoints and unresolved empty names distinct", () => {
    const shovel = topologyFixture.nodes.find((node) => node.type === "shovel")!;
    const node: TopologyNode = {
      ...shovel,
      data: {
        ...shovel.data,
        source: null,
        destination: { ...shovel.data.destination, resourceKind: null, resourceName: "" },
      },
    };

    render(<DetailsPanel selection={{ kind: "node", id: node.id }} nodes={[node]} edges={[]} />);

    expect(detailValue("Source endpoint")).toBe("Not available");
    expect(detailValue("Destination resource kind")).toBe("Not available");
    expect(detailValue("Destination resource name")).toBe("(empty)");
  });
});
