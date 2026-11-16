/** Shared wire shapes mirroring the backend's approved Pydantic DTOs. */

export type AnalysisSource = "static" | "session";
export type CaptureCompleteness = "complete" | "definitions_only";

export interface ClusterSummary {
  clusterId: string | null;
  clusterName: string | null;
  clusterTags: Readonly<Record<string, string>>;
  vhostCount: number;
  nodeCount: number;
  edgeCount: number;
}

export interface AnalysisSummary {
  analysisId: string;
  label: string;
  source: AnalysisSource;
  snapshotRevision: string;
  loadedAt: string;
  captureCompleteness: CaptureCompleteness;
  cluster: ClusterSummary;
  description: string | null;
  ownerContact: string | null;
  expiresAt: string | null;
}

export interface AnalysisCatalogueResponse {
  static: ReadonlyArray<AnalysisSummary>;
  session: ReadonlyArray<AnalysisSummary>;
}

export interface UploadResponse {
  analysis: AnalysisSummary;
  replaced: boolean;
}

export type AuthMode = "none" | "oidc" | "basic";
export type AppRole = "viewer" | "analyst";

export interface AuthSessionResponse {
  authMode: AuthMode;
  authenticated: boolean;
  loginPath: "/api/auth/oidc/login" | null;
  logoutPath: "/api/auth/logout" | null;
  csrfToken: string | null;
}

export type AuthSessionMetadata = Omit<AuthSessionResponse, "csrfToken">;

export interface AccessResponse {
  authMode: AuthMode;
  roles: ReadonlyArray<AppRole>;
  capabilities: {
    viewTopology: boolean;
    runGraphOperations: boolean;
    manageUploads: boolean;
  };
}

export type TopologyNodeKind = "exchange" | "queue" | "shovel";
export type TopologyEdgeKind = "binding" | "dead_letter" | "alternate_exchange" | "shovel";

export interface GraphPosition {
  x: number;
  y: number;
}

export interface CommonNodeData {
  kind: TopologyNodeKind;
  name: string;
  vhost: string;
  clusterId: string | null;
  isMissing: boolean;
}

export interface ExchangeNodeData extends CommonNodeData {
  kind: "exchange";
  exchangeType: string | null;
  durable: boolean | null;
  internal: boolean | null;
  isDefault: boolean | null;
}

export interface QueueNodeData extends CommonNodeData {
  kind: "queue";
  queueType: string | null;
  durable: boolean | null;
}

export interface SafeShovelEndpoint {
  vhost: string | null;
  resourceName: string | null;
  resourceKind: "exchange" | "queue" | null;
  routingKey: string | null;
  isConfirmedLocal: boolean;
}

export interface ShovelNodeData extends CommonNodeData {
  kind: "shovel";
  source: SafeShovelEndpoint | null;
  destination: SafeShovelEndpoint | null;
  isCrossVhost: boolean | null;
  hasUnconfirmedEndpoint: boolean | null;
}

export type TopologyNodeData = ExchangeNodeData | QueueNodeData | ShovelNodeData;

export interface TopologyNode {
  id: string;
  type: TopologyNodeKind;
  position: GraphPosition;
  data: TopologyNodeData;
}

export interface TopologyEdge {
  id: string;
  source: string;
  target: string;
  type: TopologyEdgeKind;
  data: {
    kind: TopologyEdgeKind;
    routingKey: string | null;
  };
}

export interface TopologyResponse {
  source: AnalysisSource;
  analysisId: string;
  snapshotRevision: string;
  cluster: ClusterSummary;
  nodes: ReadonlyArray<TopologyNode>;
  edges: ReadonlyArray<TopologyEdge>;
}

export type RunStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "cancelling"
  | "cancelled";

export interface RunError {
  code: string;
  message: string;
}

export interface SccComponent {
  componentIndex: number;
  isCyclic: true;
  nodeCount: number;
  vhosts: ReadonlyArray<string>;
  nodeIds: ReadonlyArray<string>;
}

export interface SccRunResponse {
  runId: string;
  operation: "cyclic_scc";
  status: RunStatus;
  source: AnalysisSource;
  analysisId: string;
  snapshotRevision: string;
  createdAt: string;
  startedAt: string | null;
  finishedAt: string | null;
  heartbeatExpiresAt: string | null;
  resultExpiresAt: string | null;
  error: RunError | null;
  result: { components: ReadonlyArray<SccComponent> } | null;
}

export interface CycleRunResponse {
  runId: string;
  operation: "structural_cycles";
  status: RunStatus;
  source: AnalysisSource;
  analysisId: string;
  snapshotRevision: string;
  createdAt: string;
  startedAt: string | null;
  finishedAt: string | null;
  heartbeatExpiresAt: string | null;
  resultExpiresAt: string | null;
  error: RunError | null;
  result: {
    cycleCount: number;
    truncated: boolean;
    maxCycles: number;
  } | null;
}

export type CycleSort = "sdk" | "length_asc" | "length_desc";

export interface CycleSummary {
  cycleIndex: number;
  length: number;
  vhosts: ReadonlyArray<string>;
  previewNodeIds: ReadonlyArray<string>;
}

export interface CyclePage {
  runId: string;
  snapshotRevision: string;
  offset: number;
  limit: number;
  total: number;
  sort: CycleSort;
  truncated: boolean;
  items: ReadonlyArray<CycleSummary>;
}

export interface CycleDetail {
  runId: string;
  snapshotRevision: string;
  cycleIndex: number;
  length: number;
  vhosts: ReadonlyArray<string>;
  nodeIds: ReadonlyArray<string>;
  edgeIds: ReadonlyArray<string>;
}

export interface CyclePageOptions {
  offset?: number;
  limit?: number;
  sort?: CycleSort;
  vhosts?: ReadonlyArray<string>;
  minLength?: number;
  maxLength?: number;
}
