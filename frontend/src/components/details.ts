import type {
  SafeShovelEndpoint,
  TopologyEdge,
  TopologyNode,
  TopologyNodeKind,
} from "../types/topology";

export interface DetailRow {
  label: string;
  value: string;
}

function displayText(value: string | null): string {
  if (value === null) {
    return "Not available";
  }
  return value === "" ? "(empty)" : value;
}

function displayBoolean(value: boolean | null): string {
  return value === null ? "Not available" : value ? "Yes" : "No";
}

function displayResourceName(value: string | null, kind: TopologyNodeKind | null): string {
  if (kind === "exchange" && value === "") {
    return "(unnamed default exchange)";
  }
  return displayText(value);
}

function endpointRows(prefix: string, endpoint: SafeShovelEndpoint | null): DetailRow[] {
  if (!endpoint) {
    return [{ label: `${prefix} endpoint`, value: "Not available" }];
  }
  return [
    { label: `${prefix} resource kind`, value: displayText(endpoint.resourceKind) },
    {
      label: `${prefix} resource name`,
      value: displayResourceName(endpoint.resourceName, endpoint.resourceKind),
    },
    { label: `${prefix} vhost`, value: displayText(endpoint.vhost) },
    { label: `${prefix} routing key`, value: displayText(endpoint.routingKey) },
    { label: `${prefix} confirmed local`, value: displayBoolean(endpoint.isConfirmedLocal) },
  ];
}

export function safeNodeDetailRows(node: TopologyNode): ReadonlyArray<DetailRow> {
  const common: DetailRow[] = [
    { label: "Wire identity", value: node.id },
    { label: "Resource kind", value: node.data.kind },
    { label: "Name", value: displayResourceName(node.data.name, node.data.kind) },
    { label: "Vhost", value: displayText(node.data.vhost) },
    { label: "Cluster ID", value: displayText(node.data.clusterId) },
    { label: "Captured resource", value: node.data.isMissing ? "No — MISSING" : "Yes" },
  ];

  if (node.data.kind === "exchange") {
    return [
      ...common,
      { label: "Exchange type", value: displayText(node.data.exchangeType) },
      { label: "Durable", value: displayBoolean(node.data.durable) },
      { label: "Internal", value: displayBoolean(node.data.internal) },
      { label: "Default exchange", value: displayBoolean(node.data.isDefault) },
    ];
  }
  if (node.data.kind === "queue") {
    return [
      ...common,
      { label: "Queue type", value: displayText(node.data.queueType) },
      { label: "Durable", value: displayBoolean(node.data.durable) },
    ];
  }
  return [
    ...common,
    ...endpointRows("Source", node.data.source),
    ...endpointRows("Destination", node.data.destination),
    { label: "Cross-vhost", value: displayBoolean(node.data.isCrossVhost) },
    {
      label: "Unconfirmed endpoint",
      value: displayBoolean(node.data.hasUnconfirmedEndpoint),
    },
  ];
}

function relationshipEndpointRows(prefix: string, node: TopologyNode | undefined): DetailRow[] {
  return [
    { label: `${prefix} resource kind`, value: node?.data.kind ?? "Not available" },
    {
      label: `${prefix} resource name`,
      value: node ? displayResourceName(node.data.name, node.data.kind) : "Not available",
    },
    { label: `${prefix} vhost`, value: node ? displayText(node.data.vhost) : "Not available" },
    {
      label: `${prefix} captured resource`,
      value: node ? (node.data.isMissing ? "No — MISSING" : "Yes") : "Not available",
    },
  ];
}

function relationshipEndpointDescription(node: TopologyNode | undefined): string {
  return node
    ? `${node.data.isMissing ? "MISSING " : ""}${node.data.kind} ${displayResourceName(node.data.name, node.data.kind)} in vhost ${displayText(node.data.vhost)}`
    : "Resource not available";
}

export function safeEdgeSummary(edge: TopologyEdge, nodes: ReadonlyArray<TopologyNode>): string {
  return `${relationshipEndpointDescription(nodes.find((node) => node.id === edge.source))} → ${relationshipEndpointDescription(nodes.find((node) => node.id === edge.target))}`;
}

export function safeEdgeDetailRows(
  edge: TopologyEdge,
  nodes: ReadonlyArray<TopologyNode>,
): ReadonlyArray<DetailRow> {
  return [
    ...relationshipEndpointRows(
      "Source",
      nodes.find((node) => node.id === edge.source),
    ),
    ...relationshipEndpointRows(
      "Target",
      nodes.find((node) => node.id === edge.target),
    ),
    { label: "Relationship kind", value: edge.data.kind.replaceAll("_", " ") },
    { label: "Routing key", value: displayText(edge.data.routingKey) },
    { label: "Wire identity", value: edge.id },
    { label: "Source wire identity", value: edge.source },
    { label: "Target wire identity", value: edge.target },
  ];
}
