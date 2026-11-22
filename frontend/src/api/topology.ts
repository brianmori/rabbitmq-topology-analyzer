/**
 * Every fetch() call in this app lives here, nowhere else -- components
 * import from this module rather than calling fetch() directly.
 *
 * All requests go through the relative `/api/...` path. During local dev,
 * Vite's proxy (see vite.config.ts) forwards these to the FastAPI backend
 * at http://localhost:8000, so this code never needs to know the backend's
 * real address and never triggers a CORS preflight.
 */

import { browserUuid } from "../browserUuid";
import type {
  AccessResponse,
  AnalysisCatalogueResponse,
  AnalysisSource,
  AuthMode,
  AuthSessionMetadata,
  AuthSessionResponse,
  CycleDetail,
  CyclePage,
  CyclePageOptions,
  CycleRunResponse,
  SccRunResponse,
  TopologyResponse,
  UploadResponse,
} from "../types/topology";

const SESSION_STORAGE_KEY = "rabbitmq-topology-session-token";
const UNSAFE_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

let authSession: AuthSessionResponse | null = null;
// Accepted authentication changes invalidate older product responses. A
// separate sequence also makes overlapping bootstraps latest-request-only.
let authenticationGeneration = 0;
let bootstrapGeneration = 0;
const authenticationFailureListeners = new Set<(session: AuthSessionMetadata | null) => void>();

interface RequestPolicy {
  ownerScoped?: boolean;
  notifyAuthenticationFailure?: boolean;
}

export interface CaptureUploadInput {
  analysisId: string;
  label?: string;
  definitionsFile: File;
  queuesFile?: File;
  exchangesFile?: File;
  inClusterAmqpHosts: ReadonlyArray<string>;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly retryAfter: string | null;
  readonly issues: ReadonlyArray<{ field: string; code: string; message: string }>;

  constructor(
    status: number,
    code: string,
    message: string,
    retryAfter: string | null,
    issues: ReadonlyArray<{ field: string; code: string; message: string }> = [],
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.retryAfter = retryAfter;
    this.issues = issues;
  }
}

/** Local cancellation signal, not an HTTP or provider authentication failure. */
export class AuthenticationSupersededError extends Error {
  constructor() {
    super("Authentication request was superseded.");
    this.name = "AuthenticationSupersededError";
  }
}

function assertAuthenticationGeneration(generation: number): void {
  if (generation !== authenticationGeneration) throw new AuthenticationSupersededError();
}

function commitAuthenticationSession(session: AuthSessionResponse): void {
  authenticationGeneration += 1;
  authSession = session;
}

async function apiError(response: Response, fallback: string): Promise<ApiError> {
  let code = fallback;
  let message = fallback;
  try {
    const payload = (await response.json()) as {
      error?: { code?: unknown; message?: unknown; issues?: unknown };
    };
    if (typeof payload.error?.code === "string") code = payload.error.code;
    if (typeof payload.error?.message === "string") message = payload.error.message;
    const issues = Array.isArray(payload.error?.issues)
      ? payload.error.issues.filter(
          (issue): issue is { field: string; code: string; message: string } =>
            typeof issue === "object" &&
            issue !== null &&
            typeof (issue as Record<string, unknown>).field === "string" &&
            typeof (issue as Record<string, unknown>).code === "string" &&
            typeof (issue as Record<string, unknown>).message === "string",
        )
      : [];
    return new ApiError(
      response.status,
      code,
      message,
      response.headers.get("Retry-After"),
      issues,
    );
  } catch {
    // Keep the route-specific safe fallback when the response has no JSON body.
  }
  return new ApiError(response.status, code, message, response.headers.get("Retry-After"));
}

function safeSessionMetadata(session: AuthSessionResponse): AuthSessionMetadata {
  const { authMode, authenticated, loginPath, logoutPath } = session;
  return { authMode, authenticated, loginPath, logoutPath };
}

function authenticationMode(): AuthMode {
  return authSession?.authMode ?? "none";
}

function withoutAuthentication(session: AuthSessionResponse): AuthSessionResponse {
  return {
    ...session,
    authenticated: false,
    loginPath: session.authMode === "oidc" ? "/api/auth/oidc/login" : null,
    logoutPath: null,
    csrfToken: null,
  };
}

function parseAuthSession(payload: unknown): AuthSessionResponse {
  if (typeof payload !== "object" || payload === null) {
    throw new ApiError(
      502,
      "invalid_auth_session",
      "Invalid authentication session response.",
      null,
    );
  }
  const value = payload as Record<string, unknown>;
  const expectedFields = new Set([
    "authMode",
    "authenticated",
    "loginPath",
    "logoutPath",
    "csrfToken",
  ]);
  const authMode = value.authMode;
  const authenticated = value.authenticated;
  const loginPath = value.loginPath;
  const logoutPath = value.logoutPath;
  const csrfToken = value.csrfToken;

  if (
    Object.keys(value).some((field) => !expectedFields.has(field)) ||
    Object.keys(value).length !== expectedFields.size ||
    (authMode !== "none" && authMode !== "oidc" && authMode !== "basic") ||
    typeof authenticated !== "boolean" ||
    (loginPath !== null && loginPath !== "/api/auth/oidc/login") ||
    (logoutPath !== null && logoutPath !== "/api/auth/logout") ||
    (csrfToken !== null && typeof csrfToken !== "string")
  ) {
    throw new ApiError(
      502,
      "invalid_auth_session",
      "Invalid authentication session response.",
      null,
    );
  }
  if (
    (authMode === "none" &&
      (!authenticated || loginPath !== null || logoutPath !== null || csrfToken !== null)) ||
    (authMode === "oidc" &&
      ((authenticated ? loginPath !== null : loginPath !== "/api/auth/oidc/login") ||
        (authenticated
          ? logoutPath !== "/api/auth/logout" || !csrfToken
          : logoutPath !== null || csrfToken !== null))) ||
    (authMode === "basic" &&
      (loginPath !== null ||
        csrfToken !== null ||
        (authenticated ? logoutPath !== "/api/auth/logout" : logoutPath !== null)))
  ) {
    throw new ApiError(
      502,
      "invalid_auth_session",
      "Invalid authentication session response.",
      null,
    );
  }

  return { authMode, authenticated, loginPath, logoutPath, csrfToken };
}

function notifyAuthenticationFailure(): void {
  authenticationGeneration += 1;
  if (authSession) {
    authSession = withoutAuthentication(authSession);
  }
  const metadata = authSession ? safeSessionMetadata(authSession) : null;
  for (const listener of authenticationFailureListeners) listener(metadata);
}

/** Registers the application-shell reaction to an expired or rejected credential. */
export function subscribeAuthenticationFailure(
  listener: (session: AuthSessionMetadata | null) => void,
): () => void {
  authenticationFailureListeners.add(listener);
  return () => authenticationFailureListeners.delete(listener);
}

/** Clears module-owned authentication metadata, primarily between app lifecycles and tests. */
export function clearAuthenticationState(): void {
  authenticationGeneration += 1;
  bootstrapGeneration += 1;
  authSession = null;
  authenticationFailureListeners.clear();
}

/** Removes the logical browser-session owner after successful Basic cleanup. */
export function clearBrowserSessionToken(): void {
  sessionStorage.removeItem(SESSION_STORAGE_KEY);
}

async function apiFetch(
  input: string,
  init: RequestInit = {},
  policy: RequestPolicy = {},
): Promise<Response> {
  const headers = new Headers(init.headers);
  const method = (init.method ?? "GET").toUpperCase();
  const mode = authenticationMode();
  const authentication = authenticationGeneration;

  if (policy.ownerScoped && mode !== "oidc") {
    headers.set("X-Session-Token", getSessionToken());
  }
  if (mode === "oidc" && UNSAFE_METHODS.has(method) && authSession?.csrfToken) {
    headers.set("X-CSRF-Token", authSession.csrfToken);
  }

  const response = await fetch(input, {
    ...init,
    credentials: "same-origin",
    headers,
  });
  if (
    response.status === 401 &&
    policy.notifyAuthenticationFailure !== false &&
    authentication === authenticationGeneration
  ) {
    notifyAuthenticationFailure();
  }
  return response;
}

/**
 * Reads anonymous-safe authentication bootstrap data before any protected API.
 *
 * The CSRF value remains private to this module; React receives only the mode,
 * authentication state, and same-origin navigation paths.
 */
export async function fetchAuthSession(): Promise<AuthSessionMetadata> {
  const bootstrap = ++bootstrapGeneration;
  const authentication = authenticationGeneration;
  const assertCurrent = () => {
    assertAuthenticationGeneration(authentication);
    if (bootstrap !== bootstrapGeneration) throw new AuthenticationSupersededError();
  };
  let response = await apiFetch("/api/auth/session", {}, { notifyAuthenticationFailure: false });
  assertCurrent();
  if (!response.ok) {
    const basicChallenge = response.headers
      .get("WWW-Authenticate")
      ?.toLowerCase()
      .startsWith("basic ");
    if (response.status === 401 && basicChallenge) {
      authSession = {
        authMode: "basic",
        authenticated: false,
        loginPath: null,
        logoutPath: null,
        csrfToken: null,
      };
      notifyAuthenticationFailure();
      throw await apiError(response, "auth_session_unavailable");
    }
    if (response.status === 401) {
      // An expired or invalid OIDC cookie is deleted by the 401 response.
      // Re-bootstrap once and trust only the validated anonymous-safe payload
      // returned after the browser has applied that cookie deletion.
      response = await apiFetch("/api/auth/session", {}, { notifyAuthenticationFailure: false });
      assertCurrent();
      if (response.status === 401) {
        throw new ApiError(
          502,
          "auth_session_unavailable",
          "Authentication session bootstrap remained unauthorized after credential cleanup.",
          null,
        );
      }
    }
    if (!response.ok) {
      const error = await apiError(response, "auth_session_unavailable");
      assertCurrent();
      throw error;
    }
  }
  const payload: unknown = await response.json();
  assertCurrent();
  const session = parseAuthSession(payload);
  commitAuthenticationSession(session);
  return safeSessionMetadata(session);
}

/**
 * Returns backend-computed capabilities for the current principal.
 *
 * Components consume these booleans instead of interpreting OIDC claims or
 * duplicating the backend's analyst-to-viewer role hierarchy.
 */
export async function fetchAccess(): Promise<AccessResponse> {
  const response = await apiFetch("/api/access");
  if (!response.ok) {
    throw await apiError(response, "access_unavailable");
  }
  return response.json() as Promise<AccessResponse>;
}

/**
 * Reads the session token from sessionStorage, generating and storing one
 * if this is the first call this browser session has made.
 *
 * A session token identifies this browser tab's ephemeral uploads to the
 * backend. It's machine-generated and never shown to the user -- the
 * human-readable name they choose is the separate `analysisId`.
 */
function getSessionToken(): string {
  let token = sessionStorage.getItem(SESSION_STORAGE_KEY);
  if (!token) {
    token = browserUuid();
    sessionStorage.setItem(SESSION_STORAGE_KEY, token);
  }
  return token;
}

/**
 * Fetches the list of static and session analyses available to this
 * browser session.
 */
export async function fetchAnalyses(): Promise<AnalysisCatalogueResponse> {
  const response = await apiFetch("/api/analyses", {}, { ownerScoped: true });
  if (!response.ok) {
    throw await apiError(response, "catalogue_unavailable");
  }
  return response.json() as Promise<AnalysisCatalogueResponse>;
}

/** Fetches one complete immutable topology snapshot. */
export async function fetchTopology(
  source: AnalysisSource,
  analysisId: string,
): Promise<TopologyResponse> {
  const query = new URLSearchParams({ source, analysis_id: analysisId });
  const response = await apiFetch(`/api/topology?${query}`, {}, { ownerScoped: true });
  if (!response.ok) {
    throw await apiError(response, "topology_unavailable");
  }
  return response.json() as Promise<TopologyResponse>;
}

/** Fetches a versioned topology file for the exact graph the user is viewing. */
export async function fetchTopologyExport(
  snapshot: Pick<TopologyResponse, "source" | "analysisId" | "snapshotRevision">,
  signal: AbortSignal,
): Promise<{ blob: Blob; filename: string }> {
  const authentication = authenticationGeneration;
  const query = new URLSearchParams({
    source: snapshot.source,
    analysis_id: snapshot.analysisId,
    snapshot_revision: snapshot.snapshotRevision,
  });
  const response = await apiFetch(`/api/topology/export?${query}`, { signal }, { ownerScoped: true });
  if (!response.ok) {
    throw await apiError(response, "topology_export_unavailable");
  }
  const disposition = response.headers.get("Content-Disposition");
  const filename = disposition?.match(/^attachment; filename="([a-z0-9._-]+\.topology\.json)"$/)?.[1];
  if (!filename || response.headers.get("Content-Type")?.split(";")[0] !== "application/json") {
    throw new ApiError(502, "invalid_topology_export", "Invalid topology export response.", null);
  }
  const blob = await response.blob();
  assertAuthenticationGeneration(authentication);
  return { blob, filename };
}

/** Starts or idempotently replays one cyclic-component analysis run. */
export async function startSccRun(
  source: AnalysisSource,
  analysisId: string,
  snapshotRevision: string,
  idempotencyKey: string,
): Promise<SccRunResponse> {
  const response = await apiFetch(
    "/api/scc-runs",
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": idempotencyKey,
      },
      body: JSON.stringify({ source, analysisId, snapshotRevision }),
    },
    { ownerScoped: true },
  );
  if (!response.ok) {
    throw await apiError(response, "scc_start_failed");
  }
  return response.json() as Promise<SccRunResponse>;
}

/** Polls one owned SCC run and renews its active or terminal lease. */
export async function fetchSccRun(
  runId: string,
  snapshotRevision: string,
): Promise<SccRunResponse> {
  const query = new URLSearchParams({ snapshot_revision: snapshotRevision });
  const response = await apiFetch(
    `/api/scc-runs/${encodeURIComponent(runId)}?${query}`,
    {},
    { ownerScoped: true },
  );
  if (!response.ok) {
    throw await apiError(response, "scc_run_unavailable");
  }
  return response.json() as Promise<SccRunResponse>;
}

/** Cancels queued/running SCC work and resolves after the child exits. */
export async function cancelSccRun(
  runId: string,
  snapshotRevision: string,
): Promise<SccRunResponse> {
  const query = new URLSearchParams({ snapshot_revision: snapshotRevision });
  const response = await apiFetch(
    `/api/scc-runs/${encodeURIComponent(runId)}?${query}`,
    { method: "DELETE" },
    { ownerScoped: true },
  );
  if (!response.ok) {
    throw await apiError(response, "scc_cancel_failed");
  }
  return response.json() as Promise<SccRunResponse>;
}

/** Starts or idempotently replays one structural-cycle analysis run. */
export async function startCycleRun(
  source: AnalysisSource,
  analysisId: string,
  snapshotRevision: string,
  maxCycles: number,
  idempotencyKey: string,
  includeAllCrossVhost?: boolean,
): Promise<CycleRunResponse> {
  const response = await apiFetch(
    "/api/cycle-runs",
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": idempotencyKey,
      },
      body: JSON.stringify({ source, analysisId, snapshotRevision, maxCycles, includeAllCrossVhost }),
    },
    { ownerScoped: true },
  );
  if (!response.ok) {
    throw await apiError(response, "cycle_start_failed");
  }
  return response.json() as Promise<CycleRunResponse>;
}

/** Polls one owned cycle run and renews its active or terminal lease. */
export async function fetchCycleRun(
  runId: string,
  snapshotRevision: string,
): Promise<CycleRunResponse> {
  const query = new URLSearchParams({ snapshot_revision: snapshotRevision });
  const response = await apiFetch(
    `/api/cycle-runs/${encodeURIComponent(runId)}?${query}`,
    {},
    { ownerScoped: true },
  );
  if (!response.ok) {
    throw await apiError(response, "cycle_run_unavailable");
  }
  return response.json() as Promise<CycleRunResponse>;
}

/** Cancels queued/running cycle work and resolves after the child exits. */
export async function cancelCycleRun(
  runId: string,
  snapshotRevision: string,
): Promise<CycleRunResponse> {
  const query = new URLSearchParams({ snapshot_revision: snapshotRevision });
  const response = await apiFetch(
    `/api/cycle-runs/${encodeURIComponent(runId)}?${query}`,
    { method: "DELETE" },
    { ownerScoped: true },
  );
  if (!response.ok) {
    throw await apiError(response, "cycle_cancel_failed");
  }
  return response.json() as Promise<CycleRunResponse>;
}

/** Reads one bounded summary page without starting or recomputing SDK work. */
export async function fetchCyclePage(
  runId: string,
  snapshotRevision: string,
  options: CyclePageOptions = {},
): Promise<CyclePage> {
  const query = new URLSearchParams({ snapshot_revision: snapshotRevision });
  if (options.offset !== undefined) query.set("offset", String(options.offset));
  if (options.limit !== undefined) query.set("limit", String(options.limit));
  if (options.sort !== undefined) query.set("sort", options.sort);
  if (options.minLength !== undefined) query.set("min_length", String(options.minLength));
  if (options.maxLength !== undefined) query.set("max_length", String(options.maxLength));
  for (const vhost of options.vhosts ?? []) query.append("vhost", vhost);
  const response = await apiFetch(
    `/api/cycle-runs/${encodeURIComponent(runId)}/cycles?${query}`,
    {},
    { ownerScoped: true },
  );
  if (!response.ok) {
    throw await apiError(response, "cycle_page_unavailable");
  }
  return response.json() as Promise<CyclePage>;
}

/** Loads one exact ordered cycle path by its original SDK result index. */
export async function fetchCycleDetail(
  runId: string,
  cycleIndex: number,
  snapshotRevision: string,
): Promise<CycleDetail> {
  const query = new URLSearchParams({ snapshot_revision: snapshotRevision });
  const response = await apiFetch(
    `/api/cycle-runs/${encodeURIComponent(runId)}/cycles/${cycleIndex}?${query}`,
    {},
    { ownerScoped: true },
  );
  if (!response.ok) {
    throw await apiError(response, "cycle_detail_unavailable");
  }
  return response.json() as Promise<CycleDetail>;
}

/**
 * Uploads a definitions export under a user-chosen analysis ID.
 *
 * Re-uploading the same analysisId within this session replaces the
 * previous upload -- the backend treats this as ordinary versioning, not
 * an error.
 */
export async function uploadCapture(input: CaptureUploadInput): Promise<UploadResponse> {
  const formData = new FormData();
  formData.append("analysis_id", input.analysisId);
  if (input.label?.trim()) formData.append("label", input.label);
  formData.append("definitions_file", input.definitionsFile);
  if (input.queuesFile) formData.append("queues_file", input.queuesFile);
  if (input.exchangesFile) formData.append("exchanges_file", input.exchangesFile);
  for (const host of input.inClusterAmqpHosts) {
    formData.append("in_cluster_amqp_host", host);
  }

  const response = await apiFetch(
    "/api/definitions/upload",
    { method: "POST", body: formData },
    { ownerScoped: true },
  );
  if (!response.ok) {
    throw await apiError(response, "capture_upload_failed");
  }
  return response.json() as Promise<UploadResponse>;
}

export async function uploadDefinitions(analysisId: string, file: File): Promise<UploadResponse> {
  return uploadCapture({
    analysisId,
    definitionsFile: file,
    inClusterAmqpHosts: [],
  });
}

/** Deletes one caller-owned session analysis. */
export async function deleteAnalysis(analysisId: string): Promise<void> {
  const encodedAnalysisId = encodeURIComponent(analysisId);
  const response = await apiFetch(
    `/api/analyses/${encodedAnalysisId}`,
    { method: "DELETE" },
    { ownerScoped: true },
  );
  if (!response.ok) {
    throw await apiError(response, "capture_delete_failed");
  }
}

/** Pings the backend health check. Used once at startup to confirm the
 * FastAPI backend is reachable through the Vite proxy. */
export async function fetchHealth(): Promise<{ status: string }> {
  const response = await apiFetch("/api/health", {}, { notifyAuthenticationFailure: false });
  if (!response.ok) {
    throw await apiError(response, "health_unavailable");
  }
  return response.json() as Promise<{ status: string }>;
}

/** Cleans the current authenticated application session. */
export async function logout(): Promise<AuthSessionMetadata> {
  const session = authSession;
  if (!session?.logoutPath) {
    throw new ApiError(400, "logout_unavailable", "Logout is not available.", null);
  }
  const authentication = authenticationGeneration;
  // A bootstrap begun before logout cannot later restore the ended session.
  bootstrapGeneration += 1;
  const response = await apiFetch(
    session.logoutPath,
    { method: "POST" },
    { ownerScoped: session.authMode === "basic", notifyAuthenticationFailure: false },
  );
  assertAuthenticationGeneration(authentication);
  if (!response.ok) {
    if (response.status === 401) notifyAuthenticationFailure();
    const failureAuthentication = authenticationGeneration;
    const error = await apiError(response, "logout_failed");
    assertAuthenticationGeneration(failureAuthentication);
    throw error;
  }
  if (session.authMode === "basic") clearBrowserSessionToken();
  const loggedOutSession = withoutAuthentication(session);
  commitAuthenticationSession(loggedOutSession);
  return safeSessionMetadata(loggedOutSession);
}
