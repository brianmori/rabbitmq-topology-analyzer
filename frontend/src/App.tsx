import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  AuthenticationSupersededError,
  deleteAnalysis,
  fetchAccess,
  fetchAnalyses,
  fetchAuthSession,
  fetchTopology,
  logout,
  subscribeAuthenticationFailure,
  uploadCapture,
  type CaptureUploadInput,
} from "./api/topology";
import { CaptureManager } from "./components/CaptureManager";
import { TopologyExplorer } from "./components/TopologyExplorer";
import { TopologyExportButton } from "./components/TopologyExportButton";
import {
  ACTIVE_SCC_RUN_STORAGE_KEY,
  clearSccRecovery,
  readStoredSccRecovery,
} from "./operations/scc";
import {
  ACTIVE_CYCLE_RUN_STORAGE_KEY,
  clearCycleRecovery,
  readStoredCycleRecovery,
} from "./operations/cycles";
import type {
  AccessResponse,
  AnalysisCatalogueResponse,
  AnalysisSummary,
  AuthMode,
  AuthSessionMetadata,
  TopologyResponse,
  UploadResponse,
} from "./types/topology";
import "./App.css";

type TopologyLoadState =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "ready"; topology: TopologyResponse }
  | { kind: "error"; message: string };

function safeApiMessage(error: unknown, fallback: string): string {
  return error instanceof ApiError && error.message ? error.message : fallback;
}

function analysisKey(analysis: AnalysisSummary): string {
  return `${analysis.source}:${analysis.analysisId}`;
}

function allAnalyses(catalogue: AnalysisCatalogueResponse): ReadonlyArray<AnalysisSummary> {
  return [...catalogue.static, ...catalogue.session];
}

function compareAnalysisId(left: AnalysisSummary, right: AnalysisSummary): number {
  if (left.analysisId < right.analysisId) return -1;
  if (left.analysisId > right.analysisId) return 1;
  return 0;
}

function upsertSessionAnalysis(
  catalogue: AnalysisCatalogueResponse,
  analysis: AnalysisSummary,
): AnalysisCatalogueResponse {
  return {
    static: catalogue.static,
    session: [
      ...catalogue.session.filter((item) => item.analysisId !== analysis.analysisId),
      analysis,
    ].sort(compareAnalysisId),
  };
}

function removeExactAnalysis(
  catalogue: AnalysisCatalogueResponse,
  removed: AnalysisSummary,
): AnalysisCatalogueResponse {
  const keep = (analysis: AnalysisSummary) =>
    analysisKey(analysis) !== analysisKey(removed) ||
    analysis.snapshotRevision !== removed.snapshotRevision;
  return {
    static: catalogue.static.filter(keep),
    session: catalogue.session.filter(keep),
  };
}

interface StartupSelection {
  analysis: AnalysisSummary | null;
  recovering: boolean;
}

function matchingRecoveryAnalysis(
  catalogue: AnalysisCatalogueResponse,
  recovery: Pick<AnalysisSummary, "source" | "analysisId" | "snapshotRevision">,
): AnalysisSummary | null {
  return (
    allAnalyses(catalogue).find(
      (analysis) =>
        analysis.source === recovery.source &&
        analysis.analysisId === recovery.analysisId &&
        analysis.snapshotRevision === recovery.snapshotRevision,
    ) ?? null
  );
}

function selectStartupAnalysis(catalogue: AnalysisCatalogueResponse): StartupSelection {
  const sccRecovery = readStoredSccRecovery();
  if (sccRecovery) {
    const analysis = matchingRecoveryAnalysis(catalogue, sccRecovery);
    if (analysis) {
      clearCycleRecovery();
      return { analysis, recovering: true };
    }
    clearSccRecovery();
  }

  const cycleRecovery = readStoredCycleRecovery();
  if (cycleRecovery) {
    const analysis = matchingRecoveryAnalysis(catalogue, cycleRecovery);
    if (analysis) {
      return { analysis, recovering: true };
    }
    clearCycleRecovery();
  }

  return { analysis: allAnalyses(catalogue)[0] ?? null, recovering: false };
}

interface AuthenticationShellProps {
  session: AuthSessionMetadata;
  loggedOutMode: AuthMode | null;
  retryPending: boolean;
  onRetry: () => void;
}

function AuthenticationShell({
  session,
  loggedOutMode,
  retryPending,
  onRetry,
}: AuthenticationShellProps) {
  const oidcLoggedOut = session.authMode === "oidc" && loggedOutMode === "oidc";
  const basicLoggedOut = session.authMode === "basic" && loggedOutMode === "basic";

  return (
    <main className="startup-state auth-shell">
      <span className="brand-mark" aria-hidden="true">
        RMQ
      </span>
      <h1>{oidcLoggedOut || basicLoggedOut ? "Signed out" : "Authentication required"}</h1>
      {session.authMode === "oidc" ? (
        <>
          <p>
            {oidcLoggedOut
              ? "Your application session has ended. Your identity provider may keep a separate sign-in session."
              : "Sign in through the configured identity provider to open the topology workspace."}
          </p>
          {session.loginPath && (
            <a className="primary-button auth-shell__action" href={session.loginPath}>
              Sign in
            </a>
          )}
        </>
      ) : session.authMode === "basic" ? (
        <>
          <p>
            {basicLoggedOut
              ? "The local application session and this tab's ownership identifier were cleared."
              : "Your browser will use its native HTTP Basic credential prompt. This application does not collect passwords."}
          </p>
          {basicLoggedOut && (
            <p>
              Browsers may retain and resend HTTP Basic credentials. Signing out here does not
              revoke your password.
            </p>
          )}
          <button
            type="button"
            className="primary-button auth-shell__action"
            disabled={retryPending}
            onClick={onRetry}
          >
            {retryPending ? "Checking…" : "Authenticate"}
          </button>
        </>
      ) : (
        <>
          <p>The anonymous application session is unavailable. Try the startup request again.</p>
          <button
            type="button"
            className="primary-button auth-shell__action"
            disabled={retryPending}
            onClick={onRetry}
          >
            {retryPending ? "Checking…" : "Retry"}
          </button>
        </>
      )}
    </main>
  );
}

function App() {
  const [authSession, setAuthSession] = useState<AuthSessionMetadata | null>(null);
  const [access, setAccess] = useState<AccessResponse | null>(null);
  const [catalogue, setCatalogue] = useState<AnalysisCatalogueResponse | null>(null);
  const [selectedAnalysis, setSelectedAnalysis] = useState<AnalysisSummary | null>(null);
  const [topologyState, setTopologyState] = useState<TopologyLoadState>({ kind: "idle" });
  const [startupError, setStartupError] = useState<string | null>(null);
  const [authRetryGeneration, setAuthRetryGeneration] = useState(0);
  const [authRetryPending, setAuthRetryPending] = useState(true);
  const [loggedOutMode, setLoggedOutMode] = useState<AuthMode | null>(null);
  const [logoutPending, setLogoutPending] = useState(false);
  const [logoutError, setLogoutError] = useState<string | null>(null);
  const [resetGeneration, setResetGeneration] = useState(0);
  const [captureManagerOpen, setCaptureManagerOpen] = useState(false);
  const [operationActive, setOperationActive] = useState(
    () =>
      sessionStorage.getItem(ACTIVE_SCC_RUN_STORAGE_KEY) !== null ||
      sessionStorage.getItem(ACTIVE_CYCLE_RUN_STORAGE_KEY) !== null,
  );
  const topologyRequestGeneration = useRef(0);
  const authenticationGeneration = useRef(0);
  const workspaceGeneration = useRef(0);
  const unavailableRefresh = useRef<{
    authentication: number;
    workspace: number;
    topology: number;
  } | null>(null);
  const workspace = workspaceGeneration.current;
  const topologyGeneration = topologyRequestGeneration.current;
  const isWorkspaceCurrent = useCallback(
    () =>
      workspace === workspaceGeneration.current &&
      topologyGeneration === topologyRequestGeneration.current,
    [workspace, topologyGeneration],
  );

  const clearSnapshotOperationState = useCallback(() => {
    workspaceGeneration.current += 1;
    sessionStorage.removeItem(ACTIVE_SCC_RUN_STORAGE_KEY);
    sessionStorage.removeItem(ACTIVE_CYCLE_RUN_STORAGE_KEY);
    setOperationActive(false);
  }, []);

  const clearProtectedWorkspace = useCallback(() => {
    authenticationGeneration.current += 1;
    topologyRequestGeneration.current += 1;
    clearSnapshotOperationState();
    clearSccRecovery();
    clearCycleRecovery();
    setAccess(null);
    setCatalogue(null);
    setSelectedAnalysis(null);
    setTopologyState({ kind: "idle" });
    setResetGeneration(0);
    setCaptureManagerOpen(false);
  }, [clearSnapshotOperationState]);

  const loadAnalysis = useCallback(async (requested: AnalysisSummary): Promise<boolean> => {
    let analysis = requested;
    const missingSnapshots: AnalysisSummary[] = [];

    while (true) {
      const generation = topologyRequestGeneration.current + 1;
      topologyRequestGeneration.current = generation;
      setSelectedAnalysis(analysis);
      setTopologyState({ kind: "loading" });
      setResetGeneration(0);
      try {
        const topology = await fetchTopology(analysis.source, analysis.analysisId);
        if (generation !== topologyRequestGeneration.current) {
          return false;
        }
        if (
          topology.source !== analysis.source ||
          topology.analysisId !== analysis.analysisId ||
          topology.snapshotRevision !== analysis.snapshotRevision
        ) {
          clearSnapshotOperationState();
          setTopologyState({
            kind: "error",
            message:
              "The selected snapshot changed while it was loading. Retry to refresh the catalogue.",
          });
          return false;
        }
        setTopologyState({ kind: "ready", topology });
        return true;
      } catch (error: unknown) {
        if (generation !== topologyRequestGeneration.current) {
          return false;
        }
        if (error instanceof ApiError && error.status === 404) {
          clearSccRecovery();
          clearCycleRecovery();
          setOperationActive(false);
          missingSnapshots.push(analysis);
          try {
            const refreshed = missingSnapshots.reduce(
              (current, missing) => removeExactAnalysis(current, missing),
              await fetchAnalyses(),
            );
            if (generation !== topologyRequestGeneration.current) {
              return false;
            }
            setCatalogue(refreshed);
            const refreshedAnalyses = allAnalyses(refreshed);
            const fallback =
              refreshedAnalyses.find(
                (candidate) => analysisKey(candidate) === analysisKey(analysis),
              ) ?? refreshedAnalyses[0];
            if (!fallback) {
              setSelectedAnalysis(null);
              setTopologyState({ kind: "idle" });
              setResetGeneration(0);
              return false;
            }
            analysis = fallback;
            continue;
          } catch (refreshError: unknown) {
            if (generation === topologyRequestGeneration.current) {
              setCatalogue((current) =>
                current
                  ? missingSnapshots.reduce(
                      (next, missing) => removeExactAnalysis(next, missing),
                      current,
                    )
                  : current,
              );
              setSelectedAnalysis(null);
              setTopologyState({
                kind: "error",
                message: safeApiMessage(refreshError, "The catalogue could not be refreshed."),
              });
            }
            return false;
          }
        }
        setTopologyState({
          kind: "error",
          message: safeApiMessage(error, "The selected topology could not be loaded."),
        });
        return false;
      }
    }
  }, [clearSnapshotOperationState]);

  useEffect(
    () =>
      subscribeAuthenticationFailure((session) => {
        clearProtectedWorkspace();
        setAuthRetryPending(false);
        setLogoutPending(false);
        setLogoutError(null);
        setAuthSession(session);
      }),
    [clearProtectedWorkspace],
  );

  useEffect(() => {
    let active = true;
    const authentication = authenticationGeneration.current;
    void fetchAuthSession()
      .then(async (nextSession) => {
        if (!active || authentication !== authenticationGeneration.current) {
          return;
        }
        setAuthSession(nextSession);
        setLoggedOutMode(null);
        if (nextSession.authMode === "oidc" && !nextSession.authenticated) {
          clearProtectedWorkspace();
          setAuthRetryPending(false);
          return;
        }

        let establishedSession = nextSession;
        let challengedAccess: AccessResponse | null = null;
        if (nextSession.authMode === "basic" && !nextSession.authenticated) {
          // The first protected fetch deliberately triggers the browser's
          // native Basic challenge. Re-bootstrap afterward so React learns
          // only whether that browser-managed exchange succeeded.
          challengedAccess = await fetchAccess();
          if (!active || authentication !== authenticationGeneration.current) return;
          establishedSession = await fetchAuthSession();
          if (!active || authentication !== authenticationGeneration.current) return;
          setAuthSession(establishedSession);
          if (!establishedSession.authenticated) {
            clearProtectedWorkspace();
            setAuthRetryPending(false);
            return;
          }
        }

        const nextAccess = challengedAccess ?? (await fetchAccess());
        if (!active || authentication !== authenticationGeneration.current) return;
        if (nextAccess.authMode !== establishedSession.authMode) {
          throw new ApiError(
            500,
            "auth_mode_mismatch",
            "Authentication configuration changed during startup.",
            null,
          );
        }

        setAccess(nextAccess);
        if (!nextAccess.capabilities.viewTopology) {
          clearSccRecovery();
          clearCycleRecovery();
          setOperationActive(false);
          setCatalogue({ static: [], session: [] });
          setAuthRetryPending(false);
          return;
        }
        const nextCatalogue = await fetchAnalyses();
        if (!active || authentication !== authenticationGeneration.current) return;
        setCatalogue(nextCatalogue);
        const startupSelection = selectStartupAnalysis(nextCatalogue);
        setOperationActive(startupSelection.recovering);
        if (startupSelection.analysis) {
          void loadAnalysis(startupSelection.analysis).then((loaded) => {
            if (active && !loaded && startupSelection.recovering) {
              clearSccRecovery();
              clearCycleRecovery();
              setOperationActive(false);
            }
          });
        }
        setAuthRetryPending(false);
      })
      .catch((error: unknown) => {
        if (active && authentication === authenticationGeneration.current) {
          setAuthRetryPending(false);
          if (
            !(error instanceof AuthenticationSupersededError) &&
            (!(error instanceof ApiError) || error.status !== 401)
          ) {
            setStartupError(safeApiMessage(error, "Application startup failed."));
          }
        }
      });
    return () => {
      active = false;
      topologyRequestGeneration.current += 1;
    };
  }, [authRetryGeneration, clearProtectedWorkspace, loadAnalysis]);

  const handleAuthRetry = useCallback(() => {
    setAuthRetryPending(true);
    setStartupError(null);
    setLogoutError(null);
    setAuthRetryGeneration((generation) => generation + 1);
  }, []);

  const handleLogout = useCallback(async (): Promise<void> => {
    const mode = authSession?.authMode;
    if (!mode || mode === "none") return;
    const authentication = authenticationGeneration.current;
    setLogoutPending(true);
    setLogoutError(null);
    try {
      const loggedOutSession = await logout();
      if (authentication !== authenticationGeneration.current) return;
      setLogoutPending(false);
      clearProtectedWorkspace();
      setLoggedOutMode(mode);
      setAuthSession(loggedOutSession);
    } catch (error: unknown) {
      if (
        authentication === authenticationGeneration.current &&
        !(error instanceof AuthenticationSupersededError)
      ) {
        setLogoutError(safeApiMessage(error, "The application session could not be ended."));
      }
    } finally {
      if (authentication === authenticationGeneration.current) setLogoutPending(false);
    }
  }, [authSession?.authMode, clearProtectedWorkspace]);

  const reconcileCatalogue = useCallback(
    async (
      nextCatalogue: AnalysisCatalogueResponse,
      removedAnalysis?: AnalysisSummary,
    ): Promise<void> => {
      setCatalogue(nextCatalogue);
      const nextAnalyses = allAnalyses(nextCatalogue);
      const retainedSelection = selectedAnalysis
        ? nextAnalyses.find((analysis) => analysisKey(analysis) === analysisKey(selectedAnalysis))
        : undefined;

      if (
        retainedSelection &&
        retainedSelection.snapshotRevision === selectedAnalysis?.snapshotRevision
      ) {
        setSelectedAnalysis(retainedSelection);
        return;
      }

      topologyRequestGeneration.current += 1;
      if (
        removedAnalysis &&
        selectedAnalysis?.source === removedAnalysis.source &&
        selectedAnalysis.analysisId === removedAnalysis.analysisId
      ) {
        setTopologyState({ kind: "idle" });
      }
      clearSnapshotOperationState();
      const nextAnalysis = retainedSelection ?? nextAnalyses[0];
      if (nextAnalysis) {
        await loadAnalysis(nextAnalysis);
      } else {
        topologyRequestGeneration.current += 1;
        setSelectedAnalysis(null);
        setTopologyState({ kind: "idle" });
        setResetGeneration(0);
      }
    },
    [clearSnapshotOperationState, loadAnalysis, selectedAnalysis],
  );

  const handleTopologyRetry = useCallback(async (): Promise<void> => {
    if (!selectedAnalysis) return;
    const authentication = authenticationGeneration.current;
    const workspace = workspaceGeneration.current;
    const generation = ++topologyRequestGeneration.current;
    const isCurrent = () =>
      authentication === authenticationGeneration.current &&
      workspace === workspaceGeneration.current &&
      generation === topologyRequestGeneration.current;
    setTopologyState({ kind: "loading" });
    try {
      const refreshed = await fetchAnalyses();
      if (!isCurrent()) return;
      setCatalogue(refreshed);
      const analyses = allAnalyses(refreshed);
      const latest =
        analyses.find((analysis) => analysisKey(analysis) === analysisKey(selectedAnalysis)) ??
        analyses[0];
      if (
        !latest ||
        analysisKey(latest) !== analysisKey(selectedAnalysis) ||
        latest.snapshotRevision !== selectedAnalysis.snapshotRevision
      ) {
        clearSnapshotOperationState();
      }
      if (latest) {
        await loadAnalysis(latest);
      } else {
        setSelectedAnalysis(null);
        setTopologyState({ kind: "idle" });
        setResetGeneration(0);
      }
    } catch (error: unknown) {
      if (!isCurrent()) return;
      setTopologyState({
        kind: "error",
        message: safeApiMessage(error, "The catalogue could not be refreshed."),
      });
    }
  }, [clearSnapshotOperationState, loadAnalysis, selectedAnalysis]);

  const handleUpload = useCallback(
    async (input: CaptureUploadInput): Promise<UploadResponse> => {
      const authentication = authenticationGeneration.current;
      const result = await uploadCapture(input);
      if (authentication !== authenticationGeneration.current) return result;
      setCatalogue((current) =>
        current ? upsertSessionAnalysis(current, result.analysis) : current,
      );

      const currentTopology = topologyState.kind === "ready" ? topologyState.topology : null;
      const identicalVisibleSnapshot =
        currentTopology?.source === "session" &&
        currentTopology.analysisId === result.analysis.analysisId &&
        currentTopology.snapshotRevision === result.analysis.snapshotRevision;
      setSelectedAnalysis(result.analysis);
      if (identicalVisibleSnapshot) {
        return result;
      }

      const changedVisibleSnapshot =
        selectedAnalysis?.source === "session" &&
        selectedAnalysis.analysisId === result.analysis.analysisId &&
        selectedAnalysis.snapshotRevision !== result.analysis.snapshotRevision;
      if (changedVisibleSnapshot) {
        clearSnapshotOperationState();
      }
      await loadAnalysis(result.analysis);
      return result;
    },
    [clearSnapshotOperationState, loadAnalysis, selectedAnalysis, topologyState],
  );

  const handleSnapshotUnavailable = useCallback((reason: "missing" | "changed") => {
    if (!isWorkspaceCurrent()) return;
    if (reason === "changed") {
      // Invalidate callbacks synchronously, including concurrent operation
      // failures, before the loading state unmounts the obsolete explorer.
      clearSnapshotOperationState();
      void handleTopologyRetry();
      return;
    }
    const missing = selectedAnalysis;
    if (!missing) return;
    const refresh = {
      authentication: authenticationGeneration.current,
      workspace: workspaceGeneration.current,
      topology: topologyRequestGeneration.current,
    };
    const pending = unavailableRefresh.current;
    if (
      pending?.authentication === refresh.authentication &&
      pending.workspace === refresh.workspace &&
      pending.topology === refresh.topology
    ) {
      return;
    }
    unavailableRefresh.current = refresh;
    const isCurrent = () =>
      refresh.authentication === authenticationGeneration.current &&
      refresh.workspace === workspaceGeneration.current &&
      refresh.topology === topologyRequestGeneration.current;
    void fetchAnalyses()
      .then((nextCatalogue) => {
        if (!isCurrent()) return;
        return reconcileCatalogue(nextCatalogue, missing);
      })
      .catch(() => {
        // An unavailable run does not establish that its base snapshot vanished.
        // Keep the cached graph if catalogue reconciliation itself is unavailable.
      })
      .finally(() => {
        if (unavailableRefresh.current === refresh) unavailableRefresh.current = null;
      });
  }, [
    clearSnapshotOperationState,
    handleTopologyRetry,
    isWorkspaceCurrent,
    reconcileCatalogue,
    selectedAnalysis,
  ]);

  const handleDelete = useCallback(
    async (analysis: AnalysisSummary): Promise<void> => {
      const authentication = authenticationGeneration.current;
      try {
        await deleteAnalysis(analysis.analysisId);
        if (authentication !== authenticationGeneration.current) return;
      } catch (error: unknown) {
        if (authentication !== authenticationGeneration.current) return;
        if (!(error instanceof ApiError) || error.status !== 404) {
          throw error;
        }
        try {
          const nextCatalogue = await fetchAnalyses();
          if (authentication !== authenticationGeneration.current) return;
          await reconcileCatalogue(nextCatalogue, analysis);
        } catch (refreshError: unknown) {
          if (authentication !== authenticationGeneration.current) return;
          if (catalogue) {
            await reconcileCatalogue(
              {
                static: catalogue.static,
                session: catalogue.session.filter(
                  (item) => item.analysisId !== analysis.analysisId,
                ),
              },
              analysis,
            );
          }
          throw refreshError;
        }
        throw error;
      }

      if (!catalogue) return;
      await reconcileCatalogue(
        {
          static: catalogue.static,
          session: catalogue.session.filter((item) => item.analysisId !== analysis.analysisId),
        },
        analysis,
      );
    },
    [catalogue, reconcileCatalogue],
  );

  if (startupError) {
    return (
      <main className="startup-state" role="alert">
        <span className="brand-mark" aria-hidden="true">
          RMQ
        </span>
        <h1>RabbitMQ Topology Analyzer</h1>
        <p>Could not load the application catalogue.</p>
        <p>{startupError}</p>
      </main>
    );
  }

  if (authSession && !authSession.authenticated) {
    return (
      <AuthenticationShell
        session={authSession}
        loggedOutMode={loggedOutMode}
        retryPending={authRetryPending}
        onRetry={handleAuthRetry}
      />
    );
  }

  if (!authSession || !access || !catalogue) {
    return (
      <main className="startup-state" aria-busy="true">
        <span className="brand-mark" aria-hidden="true">
          RMQ
        </span>
        <h1>RabbitMQ Topology Analyzer</h1>
        <p role="status">Loading analysis catalogue…</p>
      </main>
    );
  }

  const analyses = allAnalyses(catalogue);
  const selectedKey = selectedAnalysis ? analysisKey(selectedAnalysis) : "";
  const topologyKey = topologyState.kind === "ready"
    ? `${topologyState.topology.source}:${topologyState.topology.analysisId}:${topologyState.topology.snapshotRevision}`
    : undefined;

  return (
    <>
      <div
        className="app-shell"
        inert={captureManagerOpen}
        aria-hidden={captureManagerOpen ? "true" : undefined}
      >
        <header className="top-bar">
          <div className="brand-lockup">
            <span className="brand-mark" aria-hidden="true">
              RMQ
            </span>
            <span>
              <strong>Topology Analyzer</strong>
              <small>Captured configuration analysis</small>
            </span>
          </div>

          <div className="analysis-picker">
            <div className="analysis-picker__heading">
              <label htmlFor="analysis-select">Capture</label>
              {access.capabilities.viewTopology && topologyState.kind === "ready" && (
                <TopologyExportButton
                  key={topologyKey}
                  snapshot={topologyState.topology}
                  isWorkspaceCurrent={isWorkspaceCurrent}
                  onRefreshCapture={() => void handleTopologyRetry()}
                />
              )}
            </div>
            <select
              id="analysis-select"
              aria-describedby={selectedAnalysis ? "snapshot-loaded-at" : undefined}
              value={selectedKey}
              disabled={
                !access.capabilities.viewTopology || analyses.length === 0 || operationActive
              }
              onChange={(event) => {
                const analysis = analyses.find(
                  (candidate) => analysisKey(candidate) === event.target.value,
                );
                if (analysis) {
                  void loadAnalysis(analysis);
                }
              }}
            >
              {analyses.length === 0 && <option value="">No captures</option>}
              {catalogue.static.length > 0 && (
                <optgroup label="Static captures">
                  {catalogue.static.map((analysis) => (
                    <option key={analysisKey(analysis)} value={analysisKey(analysis)}>
                      {analysis.label}
                    </option>
                  ))}
                </optgroup>
              )}
              {catalogue.session.length > 0 && (
                <optgroup label="Session captures">
                  {catalogue.session.map((analysis) => (
                    <option key={analysisKey(analysis)} value={analysisKey(analysis)}>
                      {analysis.label}
                    </option>
                  ))}
                </optgroup>
              )}
            </select>
            {selectedAnalysis && (
              <div
                className="snapshot-provenance"
                role="group"
                aria-label="Selected snapshot"
                aria-live="polite"
              >
                <span id="snapshot-loaded-at">
                  Loaded in app:{" "}
                  <time dateTime={selectedAnalysis.loadedAt}>
                    {new Date(selectedAnalysis.loadedAt).toISOString().slice(0, 19).replace("T", " ")} UTC
                  </time>
                </span>
                <details>
                  <summary>
                    Revision: <code>{selectedAnalysis.snapshotRevision.slice(7, 19)}…</code>
                  </summary>
                  <span>Full revision:</span>
                  <code className="snapshot-provenance__full-revision">
                    {selectedAnalysis.snapshotRevision}
                  </code>
                </details>
              </div>
            )}
            <div className="snapshot-status" aria-live="polite">
              {selectedAnalysis ? (
                <>
                  <span className={`source-badge source-badge--${selectedAnalysis.source}`}>
                    {selectedAnalysis.source}
                  </span>
                  <span>
                    {selectedAnalysis.captureCompleteness === "complete"
                      ? "Complete capture"
                      : "Definitions only"}
                  </span>
                  <span>
                    {selectedAnalysis.cluster.vhostCount} vhosts · {selectedAnalysis.cluster.nodeCount} nodes · {selectedAnalysis.cluster.edgeCount}{" "}
                    edges
                  </span>
                </>
              ) : (
                <span>No snapshot selected</span>
              )}
            </div>
          </div>

          <div className="top-bar__actions">
            {access.capabilities.manageUploads && (
              <button
                type="button"
                className="secondary-button"
                disabled={operationActive}
                onClick={() => setCaptureManagerOpen(true)}
              >
                Upload capture
              </button>
            )}
            <button
              type="button"
              className="primary-button"
              disabled={topologyState.kind !== "ready"}
              onClick={() => setResetGeneration((generation) => generation + 1)}
            >
              Reset layout
            </button>
            {authSession.logoutPath && (
              <button
                type="button"
                className="secondary-button"
                disabled={logoutPending}
                onClick={() => void handleLogout()}
              >
                {logoutPending ? "Signing out…" : "Sign out"}
              </button>
            )}
          </div>
        </header>

        {logoutError && (
          <div className="auth-error" role="alert">
            {logoutError}
          </div>
        )}

        <div className="snapshot-notice">
          This graph is an offline configuration snapshot. Edges show configured possible paths, not
          live message traffic. Configured paths do not guarantee message delivery.
        </div>

        {!access.capabilities.viewTopology ? (
          <main className="workspace-state" role="alert">
            <h1>Topology access unavailable</h1>
            <p>Your current access does not include topology viewing.</p>
          </main>
        ) : analyses.length === 0 ? (
          <main className="workspace-state">
            <h1>No captures available</h1>
            <p>
              {access.capabilities.manageUploads
                ? "Upload a session capture or add a static capture manifest to make a topology available."
                : "Add a static capture manifest to make a topology available."}
            </p>
          </main>
        ) : topologyState.kind === "loading" || topologyState.kind === "idle" ? (
          <main className="workspace-state" aria-busy="true">
            <div className="loading-orbit" aria-hidden="true" />
            <h1>Loading topology</h1>
            <p role="status">Resolving the selected immutable snapshot…</p>
          </main>
        ) : topologyState.kind === "error" ? (
          <main className="workspace-state" role="alert">
            <h1>Topology unavailable</h1>
            <p>{topologyState.message}</p>
            {selectedAnalysis && (
              <button
                type="button"
                className="primary-button"
                onClick={() => void handleTopologyRetry()}
              >
                Retry
              </button>
            )}
          </main>
        ) : (
          <TopologyExplorer
            key={topologyKey}
            access={access}
            topology={topologyState.topology}
            resetGeneration={resetGeneration}
            onActiveRunChange={setOperationActive}
            onSnapshotUnavailable={handleSnapshotUnavailable}
            isWorkspaceCurrent={isWorkspaceCurrent}
          />
        )}
      </div>
      {access.capabilities.manageUploads && (
        <CaptureManager
          open={captureManagerOpen}
          selectedAnalysis={selectedAnalysis}
          sessionAnalyses={catalogue.session}
          operationActive={operationActive}
          onClose={() => setCaptureManagerOpen(false)}
          onUpload={handleUpload}
          onDelete={handleDelete}
        />
      )}
    </>
  );
}

export default App;
