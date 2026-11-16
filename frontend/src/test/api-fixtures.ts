import { vi } from "vitest";
import type {
  AccessResponse,
  AnalysisCatalogueResponse,
  AuthSessionResponse,
  CycleDetail,
  CyclePage,
  CycleRunResponse,
  SccRunResponse,
  TopologyResponse,
} from "../types/topology";

export interface ApiFixture {
  body: unknown;
  status?: number;
  headers?: Readonly<Record<string, string>>;
}

export const noneAuthSessionFixture = {
  authMode: "none",
  authenticated: true,
  loginPath: null,
  logoutPath: null,
  csrfToken: null,
} as const satisfies AuthSessionResponse;

export const oidcAuthSessionFixture = {
  authMode: "oidc",
  authenticated: true,
  loginPath: null,
  logoutPath: "/api/auth/logout",
  csrfToken: "test-oidc-csrf-token",
} as const satisfies AuthSessionResponse;

export const basicAuthSessionFixture = {
  authMode: "basic",
  authenticated: true,
  loginPath: null,
  logoutPath: "/api/auth/logout",
  csrfToken: null,
} as const satisfies AuthSessionResponse;

export const accessFixture = {
  authMode: "none",
  roles: ["viewer", "analyst"],
  capabilities: {
    viewTopology: true,
    runGraphOperations: true,
    manageUploads: true,
  },
} as const satisfies AccessResponse;

export const catalogueFixture = {
  static: [
    {
      analysisId: "orders-production",
      label: "Orders production",
      source: "static",
      snapshotRevision: `sha256:${"a".repeat(64)}`,
      loadedAt: "2026-08-10T08:00:00Z",
      captureCompleteness: "complete",
      cluster: {
        clusterId: "cluster-1",
        clusterName: "rabbit@orders",
        clusterTags: {},
        vhostCount: 2,
        nodeCount: 5,
        edgeCount: 5,
      },
      description: null,
      ownerContact: null,
      expiresAt: null,
    },
  ],
  session: [],
} as const satisfies AnalysisCatalogueResponse;

export const topologyFixture = {
  source: "static",
  analysisId: "orders-production",
  snapshotRevision: `sha256:${"a".repeat(64)}`,
  cluster: catalogueFixture.static[0].cluster,
  nodes: [
    {
      id: "n1.orders-exchange",
      type: "exchange",
      position: { x: 0, y: 0 },
      data: {
        kind: "exchange",
        name: "orders.events",
        vhost: "/orders",
        clusterId: "cluster-1",
        isMissing: false,
        exchangeType: "topic",
        durable: true,
        internal: false,
        isDefault: false,
      },
    },
    {
      id: "n1.default-exchange",
      type: "exchange",
      position: { x: 0, y: 0 },
      data: {
        kind: "exchange",
        name: "",
        vhost: "/orders",
        clusterId: "cluster-1",
        isMissing: false,
        exchangeType: "direct",
        durable: true,
        internal: false,
        isDefault: true,
      },
    },
    {
      id: "n1.orders-queue",
      type: "queue",
      position: { x: 0, y: 0 },
      data: {
        kind: "queue",
        name: "Orders.Process",
        vhost: "/orders",
        clusterId: "cluster-1",
        isMissing: false,
        queueType: "quorum",
        durable: true,
      },
    },
    {
      id: "n1.audit-queue",
      type: "queue",
      position: { x: 0, y: 0 },
      data: {
        kind: "queue",
        name: "audit",
        vhost: "/audit",
        clusterId: "cluster-1",
        isMissing: false,
        queueType: "classic",
        durable: true,
      },
    },
    {
      id: "n1.cross-vhost-shovel",
      type: "shovel",
      position: { x: 0, y: 0 },
      data: {
        kind: "shovel",
        name: "orders-to-audit",
        vhost: "/orders",
        clusterId: "cluster-1",
        isMissing: false,
        source: {
          vhost: "/orders",
          resourceName: "Orders.Process",
          resourceKind: "queue",
          routingKey: null,
          isConfirmedLocal: true,
        },
        destination: {
          vhost: "/audit",
          resourceName: "audit",
          resourceKind: "queue",
          routingKey: null,
          isConfirmedLocal: true,
        },
        isCrossVhost: true,
        hasUnconfirmedEndpoint: false,
      },
    },
    {
      id: "m1.missing-dlx",
      type: "exchange",
      position: { x: 0, y: 0 },
      data: {
        kind: "exchange",
        name: "missing.dlx",
        vhost: "/orders",
        clusterId: "cluster-1",
        isMissing: true,
        exchangeType: null,
        durable: null,
        internal: null,
        isDefault: null,
      },
    },
  ],
  edges: [
    {
      id: "e1.0",
      source: "n1.orders-exchange",
      target: "n1.orders-queue",
      type: "binding",
      data: { kind: "binding", routingKey: "orders.*" },
    },
    {
      id: "e1.1",
      source: "n1.orders-queue",
      target: "m1.missing-dlx",
      type: "dead_letter",
      data: { kind: "dead_letter", routingKey: null },
    },
    {
      id: "e1.2",
      source: "n1.orders-queue",
      target: "n1.cross-vhost-shovel",
      type: "shovel",
      data: { kind: "shovel", routingKey: null },
    },
    {
      id: "e1.3",
      source: "n1.cross-vhost-shovel",
      target: "n1.audit-queue",
      type: "shovel",
      data: { kind: "shovel", routingKey: null },
    },
    {
      id: "e1.4",
      source: "n1.audit-queue",
      target: "n1.orders-queue",
      type: "dead_letter",
      data: { kind: "dead_letter", routingKey: null },
    },
  ],
} as const satisfies TopologyResponse;

const sccRunBase = {
  runId: "10000000-0000-4000-8000-000000000001",
  operation: "cyclic_scc",
  source: "static",
  analysisId: topologyFixture.analysisId,
  snapshotRevision: topologyFixture.snapshotRevision,
  createdAt: "2026-08-11T08:00:00Z",
  startedAt: "2026-08-11T08:00:00Z",
  finishedAt: null,
  heartbeatExpiresAt: "2026-08-11T08:02:00Z",
  resultExpiresAt: null,
  error: null,
} as const;

export const runningSccRunFixture = {
  ...sccRunBase,
  status: "running",
  result: null,
} as const satisfies SccRunResponse;

export const succeededSccRunFixture = {
  ...sccRunBase,
  status: "succeeded",
  finishedAt: "2026-08-11T08:00:02Z",
  heartbeatExpiresAt: null,
  resultExpiresAt: "2026-08-11T12:00:02Z",
  result: {
    components: [
      {
        componentIndex: 0,
        isCyclic: true,
        nodeCount: 2,
        vhosts: ["/orders"],
        nodeIds: ["n1.orders-exchange", "n1.orders-queue"],
      },
      {
        componentIndex: 1,
        isCyclic: true,
        nodeCount: 2,
        vhosts: ["/orders", "/audit"],
        nodeIds: ["n1.cross-vhost-shovel", "n1.audit-queue"],
      },
    ],
  },
} as const satisfies SccRunResponse;

const cycleRunBase = {
  runId: "30000000-0000-4000-8000-000000000003",
  operation: "structural_cycles",
  source: "static",
  analysisId: topologyFixture.analysisId,
  snapshotRevision: topologyFixture.snapshotRevision,
  createdAt: "2026-08-11T09:00:00Z",
  startedAt: "2026-08-11T09:00:00Z",
  finishedAt: null,
  heartbeatExpiresAt: "2026-08-11T09:02:00Z",
  resultExpiresAt: null,
  error: null,
} as const;

export const runningCycleRunFixture = {
  ...cycleRunBase,
  status: "running",
  result: null,
} as const satisfies CycleRunResponse;

export const succeededCycleRunFixture = {
  ...cycleRunBase,
  status: "succeeded",
  finishedAt: "2026-08-11T09:00:03Z",
  heartbeatExpiresAt: null,
  resultExpiresAt: "2026-08-11T13:00:03Z",
  result: { cycleCount: 2, truncated: true, maxCycles: 2 },
} as const satisfies CycleRunResponse;

export const cyclePageFixture = {
  runId: succeededCycleRunFixture.runId,
  snapshotRevision: topologyFixture.snapshotRevision,
  offset: 0,
  limit: 50,
  total: 2,
  sort: "sdk",
  truncated: true,
  items: [
    {
      cycleIndex: 0,
      length: 2,
      vhosts: ["/orders"],
      previewNodeIds: ["n1.orders-exchange", "n1.orders-queue"],
    },
    {
      cycleIndex: 1,
      length: 3,
      vhosts: ["/orders", "/audit"],
      previewNodeIds: ["n1.orders-queue", "n1.cross-vhost-shovel", "n1.audit-queue"],
    },
  ],
} as const satisfies CyclePage;

export const cycleDetailFixture = {
  runId: succeededCycleRunFixture.runId,
  snapshotRevision: topologyFixture.snapshotRevision,
  cycleIndex: 1,
  length: 3,
  vhosts: ["/orders", "/audit"],
  nodeIds: ["n1.orders-queue", "n1.cross-vhost-shovel", "n1.audit-queue"],
  edgeIds: ["e1.2", "e1.3", "e1.4"],
} as const satisfies CycleDetail;

/** Replaces fetch with deterministic JSON routes and rejects unexpected calls. */
export function installApiFixtures(routes: Readonly<Record<string, ApiFixture>>) {
  const fetchMock = vi.fn<typeof fetch>(async (input) => {
    const url = typeof input === "string" || input instanceof URL ? input.toString() : input.url;
    const fixture =
      routes[url] ?? (url === "/api/auth/session" ? { body: noneAuthSessionFixture } : undefined);

    if (!fixture) {
      throw new Error(`Unexpected API request: ${url}`);
    }

    const responseStatus = fixture.status ?? 200;
    return new Response(responseStatus === 204 ? null : JSON.stringify(fixture.body), {
      status: responseStatus,
      headers: { "Content-Type": "application/json", ...fixture.headers },
    });
  });

  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}
