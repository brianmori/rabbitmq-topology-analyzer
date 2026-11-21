import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  ApiError,
  cancelCycleRun,
  cancelSccRun,
  fetchCycleDetail,
  fetchCyclePage,
  fetchCycleRun,
  fetchSccRun,
  startCycleRun,
  startSccRun,
} from "../api/topology";
import { browserUuid } from "../browserUuid";
import { TopologyCanvas } from "../graph/TopologyCanvas";
import {
  ALL_NODE_KINDS,
  componentIndexByNode,
  cycleSteps,
  deriveCycleVisibleTopology,
  deriveSccVisibleTopology,
  deriveVisibleTopology,
  findResourceMatches,
  topologyVhosts,
} from "../graph/presentation";
import {
  type ActiveCycleRunRecovery,
  clearCycleRecovery,
  readCycleRecovery,
  readStoredCycleRecovery,
  writeCycleRecovery,
} from "../operations/cycles";
import {
  clearSccRecovery,
  isActiveRunStatus,
  readSccRecovery,
  writeSccRecovery,
} from "../operations/scc";
import {
  isSnapshotRevisionMismatch,
  operationRecoveryInvalid,
  retryOperationRequest,
} from "../operations/retry";
import type {
  AccessResponse,
  CycleDetail,
  CyclePage,
  CycleRunResponse,
  CycleSort,
  SccRunResponse,
  TopologyNodeKind,
  TopologyResponse,
} from "../types/topology";
import { DetailsPanel, type GraphSelection } from "./DetailsPanel";
import { CycleResults } from "./CycleResults";
import { ExplorerSidebar, GraphLegend } from "./ExplorerSidebar";
import { SccResults } from "./SccResults";

const RUN_POLL_INTERVAL_MS = 600;
const DRAWER_TABS = ["Inspection", "Components", "Cycles"] as const;
type DrawerTab = (typeof DRAWER_TABS)[number];

function nonNegativeInteger(value: string): number | null {
  if (!/^\d+$/.test(value)) return null;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) ? parsed : null;
}

function optionalPositiveInteger(value: string): number | undefined | null {
  if (value === "") return undefined;
  const parsed = nonNegativeInteger(value);
  return parsed !== null && parsed >= 1 ? parsed : null;
}

function sccStatusText(
  starting: boolean,
  recovering: boolean,
  activeRun: SccRunResponse | null,
  error: string | null,
  completedRun: SccRunResponse | null,
): string {
  if (error) return error;
  if (starting) return "Starting cyclic-component analysis…";
  if (recovering) return "Recovering active cyclic-component analysis…";
  if (activeRun) {
    return `Cyclic-component analysis ${activeRun.status}. Keep this tab open while it runs.`;
  }
  const components = completedRun?.result?.components;
  return components
    ? `${components.length} cyclic component${components.length === 1 ? "" : "s"} found.`
    : "";
}

function cycleRunStatusText(
  starting: boolean,
  recovering: boolean,
  activeRun: CycleRunResponse | null,
  error: string | null,
  completedRun: CycleRunResponse | null,
): string {
  if (error) return error;
  if (starting) return "Starting structural-cycle analysis…";
  if (recovering) return "Recovering active structural-cycle analysis…";
  if (activeRun) {
    return `Structural-cycle analysis ${activeRun.status}. Keep this tab open while it runs.`;
  }
  const result = completedRun?.result;
  if (!result) return "";
  const count = `${result.cycleCount} structural cycle${result.cycleCount === 1 ? "" : "s"} found.`;
  if (result.crossVhostCount !== undefined) {
    const scope = `${result.crossVhostCount} cycle${result.crossVhostCount === 1 ? " spans" : "s span"} multiple vhosts.`;
    return `${count} ${scope}${result.truncated ? ` Single-vhost results were limited to ${result.maxCycles}.` : ""}`;
  }
  return `${count}${result.truncated ? ` Results were truncated at maxCycles=${result.maxCycles}.` : ""}`;
}

function cyclePageStatusText(
  loading: boolean,
  error: string | null,
  page: CyclePage | null,
): string {
  if (loading) return "Loading cycle summaries…";
  if (error) return error;
  return page ? `${page.total} cycle${page.total === 1 ? "" : "s"} match the current filters.` : "";
}

function cycleDetailStatusText(loading: boolean, error: string | null): string {
  if (loading) return "Loading exact cycle path…";
  return error ?? "";
}

interface TopologyExplorerProps {
  access: AccessResponse;
  topology: TopologyResponse;
  resetGeneration: number;
  onActiveRunChange?: (active: boolean) => void;
  onSnapshotUnavailable?: (reason: "missing" | "changed") => void;
  isWorkspaceCurrent?: () => boolean;
}

interface CyclePageResult {
  requestKey: string;
  page: CyclePage | null;
  error: string | null;
}

export function TopologyExplorer({
  access,
  topology,
  resetGeneration,
  onActiveRunChange,
  onSnapshotUnavailable,
  isWorkspaceCurrent,
}: TopologyExplorerProps) {
  const vhosts = useMemo(() => topologyVhosts(topology), [topology]);
  const [selectedVhosts, setSelectedVhosts] = useState<ReadonlySet<string>>(() => new Set(vhosts));
  const [visibleKinds, setVisibleKinds] = useState<ReadonlySet<TopologyNodeKind>>(
    () => new Set(ALL_NODE_KINDS),
  );
  const [hideDefaultExchanges, setHideDefaultExchanges] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [searchFocus, setSearchFocus] = useState<{ id: string } | null>(null);
  const [selection, setSelection] = useState<GraphSelection>(null);
  const [drawerOpen, setDrawerOpen] = useState(true);
  const [drawerTab, setDrawerTab] = useState<DrawerTab>(() =>
    readSccRecovery(topology)
      ? "Components"
      : readCycleRecovery(topology)
        ? "Cycles"
        : "Inspection",
  );
  const [controlsOpen, setControlsOpen] = useState(
    () => !window.matchMedia("(max-width: 780px)").matches,
  );
  const graphEntryRef = useRef<HTMLButtonElement>(null);
  const controlsDisclosureRef = useRef<HTMLDetailsElement>(null);
  const drawerRef = useRef<HTMLElement>(null);
  const focusDrawerRef = useRef(false);
  const focusSelectedDrawerTab = useCallback(() => {
    const tab = drawerRef.current?.querySelector<HTMLButtonElement>('[aria-selected="true"]');
    tab?.focus();
    tab?.scrollIntoView?.({ block: "start" });
  }, []);

  function openDrawer(tab: DrawerTab, focus = true) {
    focusDrawerRef.current = focus;
    setDrawerTab(tab);
    setDrawerOpen(true);
    // Opening an already selected tab still needs to move focus on request.
    if (focus && drawerOpen && drawerTab === tab) {
      focusSelectedDrawerTab();
      focusDrawerRef.current = false;
    }
  }

  useLayoutEffect(() => {
    if (drawerOpen && focusDrawerRef.current) {
      focusSelectedDrawerTab();
      focusDrawerRef.current = false;
    }
  }, [drawerOpen, drawerTab, focusSelectedDrawerTab]);

  useEffect(() => {
    const narrow = window.matchMedia("(max-width: 780px)");
    const change = () => setControlsOpen(!narrow.matches);
    narrow.addEventListener("change", change);
    return () => narrow.removeEventListener("change", change);
  }, []);

  const inspect = useCallback((next: GraphSelection) => {
    setSelection(next);
    if (next) {
      setDrawerTab("Inspection");
      setDrawerOpen(true);
    }
  }, []);

  function returnToGraph() {
    if (window.matchMedia("(max-width: 780px)").matches) {
      // Close the native disclosure before positioning the page, including
      // when the browser still has an opening toggle event queued.
      const disclosure = controlsDisclosureRef.current;
      if (disclosure?.open) disclosure.open = false;
      setControlsOpen(false);
    }
    const entry = graphEntryRef.current;
    entry?.focus({ preventScroll: true });
    entry?.parentElement?.scrollIntoView?.({ block: "start", inline: "nearest" });
  }
  const [activeSccRun, setActiveSccRun] = useState<SccRunResponse | null>(null);
  const [completedSccRun, setCompletedSccRun] = useState<SccRunResponse | null>(null);
  const [sccError, setSccError] = useState<string | null>(null);
  const [selectedComponentIndexes, setSelectedComponentIndexes] = useState<ReadonlySet<number>>(
    new Set(),
  );
  const [pendingSccRecovery, setPendingSccRecovery] = useState(() => readSccRecovery(topology));
  const [pendingCycleRecovery, setPendingCycleRecovery] = useState(() => {
    if (pendingSccRecovery) {
      clearCycleRecovery();
      return null;
    }
    return readCycleRecovery(topology);
  });
  const [startingScc, setStartingScc] = useState(false);
  const [recoveringScc, setRecoveringScc] = useState(pendingSccRecovery !== null);
  const [cycleMaxCycles, setCycleMaxCycles] = useState(() =>
    String(pendingCycleRecovery?.maxCycles ?? 20),
  );
  const [activeCycleRun, setActiveCycleRun] = useState<CycleRunResponse | null>(null);
  const [completedCycleRun, setCompletedCycleRun] = useState<CycleRunResponse | null>(null);
  const [cycleError, setCycleError] = useState<string | null>(null);
  const [cyclePageResult, setCyclePageResult] = useState<CyclePageResult | null>(null);
  const [cycleOffset, setCycleOffset] = useState(0);
  const [cycleSort, setCycleSort] = useState<CycleSort>("sdk");
  const [cycleMinLength, setCycleMinLength] = useState("");
  const [cycleMaxLength, setCycleMaxLength] = useState("");
  const [selectedCycleDetail, setSelectedCycleDetail] = useState<CycleDetail | null>(null);
  const [cycleDetailLoading, setCycleDetailLoading] = useState(false);
  const [cycleDetailError, setCycleDetailError] = useState<string | null>(null);
  const [startingCycle, setStartingCycle] = useState(false);
  const [recoveringCycle, setRecoveringCycle] = useState(pendingCycleRecovery !== null);
  const [sccRequestPaused, setSccRequestPaused] = useState(false);
  const [cycleRequestPaused, setCycleRequestPaused] = useState(false);
  const [cancellationPending, setCancellationPending] = useState<string | null>(null);
  const activeIdempotencyKey = useRef<string | null>(null);
  const activeCycleOptions = useRef<Pick<ActiveCycleRunRecovery, "maxCycles" | "includeAllCrossVhost">>({
    maxCycles: 20,
  });
  const cancellingRunId = useRef<string | null>(null);
  const cycleDetailGeneration = useRef(0);
  const currentCycleResultId = useRef<string | null>(null);
  const unavailableCycleRunId = useRef<string | null>(null);
  const requestLifetime = useRef(0);
  const operationRequests = useRef(new Set<AbortController>());
  // Layout cleanup runs synchronously at teardown, before any late promise
  // can write recovery state. StrictMode's setup/cleanup/setup gets a new epoch.
  useLayoutEffect(() => {
    requestLifetime.current += 1;
    const requests = operationRequests.current;
    return () => {
      requestLifetime.current += 1;
      cycleDetailGeneration.current += 1;
      for (const controller of requests) controller.abort();
      requests.clear();
    };
  }, [topology.source, topology.analysisId, topology.snapshotRevision]);
  const isRequestCurrent = useCallback(
    (lifetime: number) => lifetime === requestLifetime.current && (isWorkspaceCurrent?.() ?? true),
    [isWorkspaceCurrent],
  );
  const requestOperation = useCallback(
    <T,>(
      request: () => Promise<T>,
      lifetime: number,
      onRetry: () => void,
      current: () => boolean = () => true,
      controller: AbortController = new AbortController(),
    ) => {
      operationRequests.current.add(controller);
      return retryOperationRequest(
        request,
        controller.signal,
        () => isRequestCurrent(lifetime) && current(),
        onRetry,
      ).finally(() => operationRequests.current.delete(controller));
    },
    [isRequestCurrent],
  );
  const computeRunActive =
    pendingSccRecovery !== null ||
    pendingCycleRecovery !== null ||
    startingScc ||
    recoveringScc ||
    activeSccRun !== null ||
    startingCycle ||
    recoveringCycle ||
    activeCycleRun !== null;
  const parsedMaxCycles = nonNegativeInteger(cycleMaxCycles);
  const cycleMaxCyclesError =
    parsedMaxCycles === null ? "Maximum cycles must be a non-negative integer." : null;
  const parsedMinLength = optionalPositiveInteger(cycleMinLength);
  const parsedMaxLength = optionalPositiveInteger(cycleMaxLength);
  const cycleLengthError =
    parsedMinLength === null || parsedMaxLength === null
      ? "Cycle lengths must be positive integers."
      : parsedMinLength !== undefined &&
          parsedMaxLength !== undefined &&
          parsedMaxLength < parsedMinLength
        ? "Maximum length must be at least minimum length."
        : null;
  // A response belongs to one exact query. Changing filters immediately derives
  // a loading/validation state instead of resetting several states in an effect.
  const cyclePageRequestKey = JSON.stringify([
    completedCycleRun?.runId,
    topology.snapshotRevision,
    cycleOffset,
    cycleSort,
    parsedMinLength,
    parsedMaxLength,
  ]);
  const currentPageResult =
    cyclePageResult?.requestKey === cyclePageRequestKey ? cyclePageResult : null;
  const retainedPage =
    cyclePageResult?.page?.runId === completedCycleRun?.runId
      ? (cyclePageResult?.page ?? null)
      : null;
  const cyclePage = cycleLengthError
    ? null
    : currentPageResult
      ? currentPageResult.page
      : retainedPage;
  const cyclePageLoading =
    Boolean(completedCycleRun?.result) && !cycleLengthError && !currentPageResult;
  const cyclePageError = completedCycleRun?.result
    ? (cycleLengthError ?? currentPageResult?.error ?? null)
    : null;

  const requestErrorMessage = useCallback(
    (error: unknown, fallback: string): string => {
      if (!(error instanceof ApiError)) return fallback;
      if (isSnapshotRevisionMismatch(error)) {
        onSnapshotUnavailable?.("changed");
        return "The selected snapshot changed. Refreshing the current analysis…";
      }
      if (error.status === 404) {
        onSnapshotUnavailable?.("missing");
        return "The analysis or retained result is no longer available.";
      }
      return error.message || fallback;
    },
    [onSnapshotUnavailable],
  );

  const settleUnavailableCycleResult = useCallback(
    (run: CycleRunResponse, error: ApiError) => {
      if (currentCycleResultId.current !== run.runId) return;
      // Invalidate pending page/detail callbacks before React commits the reset.
      currentCycleResultId.current = null;
      unavailableCycleRunId.current = run.runId;
      cycleDetailGeneration.current += 1;
      setCompletedCycleRun(null);
      setCyclePageResult(null);
      setCycleOffset(0);
      setSelectedCycleDetail(null);
      setCycleDetailLoading(false);
      setCycleDetailError(null);
      setSelection(null);
      if (readStoredCycleRecovery()?.runId === run.runId) clearCycleRecovery(run);
      setCycleError(requestErrorMessage(error, "Cycle results are no longer available."));
    },
    [requestErrorMessage],
  );

  const explorationTopology = useMemo(
    () => deriveVisibleTopology(topology, selectedVhosts, visibleKinds, hideDefaultExchanges),
    [hideDefaultExchanges, selectedVhosts, topology, visibleKinds],
  );
  const sccComponents = useMemo(() => completedSccRun?.result?.components ?? [], [completedSccRun]);
  const sccFocusActive = selectedComponentIndexes.size > 0;
  const cycleFocusActive = selectedCycleDetail !== null;
  const visibleTopology = useMemo(
    () =>
      selectedCycleDetail
        ? deriveCycleVisibleTopology(topology, selectedCycleDetail)
        : sccFocusActive
          ? deriveSccVisibleTopology(topology, sccComponents, selectedComponentIndexes)
          : explorationTopology,
    [
      explorationTopology,
      sccComponents,
      sccFocusActive,
      selectedComponentIndexes,
      selectedCycleDetail,
      topology,
    ],
  );
  const selectionStillVisible =
    selection === null ||
    (selection.kind === "node"
      ? visibleTopology.nodes.some((node) => node.id === selection.id)
      : visibleTopology.edges.some((edge) => edge.id === selection.id));
  if (!selectionStillVisible) {
    // Adjust this component's state before committing children, so hidden
    // details cannot flash or reappear when a visibility filter is cleared.
    setSelection(null);
  }
  const visibleComponentByNode = useMemo(
    () =>
      cycleFocusActive
        ? new Map<string, number>()
        : componentIndexByNode(sccComponents, selectedComponentIndexes),
    [cycleFocusActive, sccComponents, selectedComponentIndexes],
  );
  const visibleCycleSteps = useMemo(() => cycleSteps(selectedCycleDetail), [selectedCycleDetail]);
  const explorationNodeIds = useMemo(
    () => new Set(explorationTopology.nodes.map((node) => node.id)),
    [explorationTopology.nodes],
  );
  const revealedNodes = visibleTopology.nodes.filter((node) => !explorationNodeIds.has(node.id));
  const overriddenFilters = [
    revealedNodes.some((node) => !selectedVhosts.has(node.data.vhost)) ? "vhost scope" : null,
    revealedNodes.some((node) => !visibleKinds.has(node.data.kind)) ? "resource visibility" : null,
    revealedNodes.some(
      (node) => hideDefaultExchanges && node.data.kind === "exchange" && node.data.isDefault,
    )
      ? "Hide default exchanges"
      : null,
  ].filter(Boolean);
  const graphMode = selectedCycleDetail
    ? `Cycle ${selectedCycleDetail.cycleIndex + 1} focus`
    : sccFocusActive
      ? `Component focus: ${[...selectedComponentIndexes]
          .sort((a, b) => a - b)
          .map((index) => `Cyclic component ${index + 1}`)
          .join(", ")}`
      : "Exploration view";
  const searchMatchIds = useMemo(
    () => findResourceMatches(visibleTopology.nodes, searchQuery),
    [searchQuery, visibleTopology.nodes],
  );
  const searchMatches = useMemo(
    () => visibleTopology.nodes.filter((node) => searchMatchIds.has(node.id)),
    [searchMatchIds, visibleTopology.nodes],
  );
  if (searchFocus && !searchMatchIds.has(searchFocus.id)) setSearchFocus(null);
  const selectedNode =
    selection?.kind === "node"
      ? topology.nodes.find((node) => node.id === selection.id)
      : undefined;
  const inspectionLabel = selectedNode
    ? `${selectedNode.data.kind} ${selectedNode.data.name || "(default exchange)"}, vhost ${selectedNode.data.vhost}`
    : selection?.kind === "edge"
      ? `relationship ${selection.id}`
      : null;
  const currentSccStatusText = sccStatusText(
    startingScc,
    recoveringScc,
    activeSccRun,
    sccError,
    completedSccRun,
  );
  const currentCycleRunStatusText = cycleRunStatusText(
    startingCycle,
    recoveringCycle,
    activeCycleRun,
    cycleError,
    completedCycleRun,
  );
  const currentCyclePageStatusText = cyclePageStatusText(
    cyclePageLoading,
    cyclePageError,
    cyclePage,
  );
  const currentCycleDetailStatusText = cycleDetailStatusText(cycleDetailLoading, cycleDetailError);

  const applySccRun = useCallback(
    (run: SccRunResponse) => {
      if (
        run.source !== topology.source ||
        run.analysisId !== topology.analysisId ||
        run.snapshotRevision !== topology.snapshotRevision
      ) {
        return;
      }
      setPendingSccRecovery(null);
      setRecoveringScc(false);
      setSccRequestPaused(false);
      setSccError(null);
      if (isActiveRunStatus(run.status)) {
        setStartingScc(false);
        setActiveSccRun(
          cancellingRunId.current === run.runId ? { ...run, status: "cancelling" } : run,
        );
        const idempotencyKey = activeIdempotencyKey.current;
        if (idempotencyKey) {
          writeSccRecovery({
            operation: "cyclic_scc",
            source: topology.source,
            analysisId: topology.analysisId,
            snapshotRevision: topology.snapshotRevision,
            idempotencyKey,
            runId: run.runId,
          });
        }
        return;
      }
      setActiveSccRun(null);
      setStartingScc(false);
      cancellingRunId.current = null;
      setCancellationPending(null);
      clearSccRecovery(run);
      activeIdempotencyKey.current = null;
      if (run.status === "succeeded") {
        setSelectedComponentIndexes(new Set());
        setCompletedSccRun(run);
        setSccError(null);
      } else if (run.status === "cancelled") {
        setSccError("Cyclic-component analysis cancelled.");
      } else {
        setSccError(run.error?.message ?? "Cyclic-component analysis failed.");
      }
    },
    [topology],
  );

  const applyCycleRun = useCallback(
    (run: CycleRunResponse) => {
      if (
        run.runId === unavailableCycleRunId.current ||
        run.source !== topology.source ||
        run.analysisId !== topology.analysisId ||
        run.snapshotRevision !== topology.snapshotRevision
      ) {
        return;
      }
      setPendingCycleRecovery(null);
      setRecoveringCycle(false);
      setCycleRequestPaused(false);
      setCycleError(null);
      if (isActiveRunStatus(run.status)) {
        setStartingCycle(false);
        setActiveCycleRun(
          cancellingRunId.current === run.runId ? { ...run, status: "cancelling" } : run,
        );
        const idempotencyKey = activeIdempotencyKey.current;
        if (idempotencyKey) {
          writeCycleRecovery({
            operation: "structural_cycles",
            source: topology.source,
            analysisId: topology.analysisId,
            snapshotRevision: topology.snapshotRevision,
            ...activeCycleOptions.current,
            idempotencyKey,
            runId: run.runId,
          });
        }
        return;
      }
      setActiveCycleRun(null);
      setStartingCycle(false);
      cancellingRunId.current = null;
      setCancellationPending(null);
      clearCycleRecovery(run);
      activeIdempotencyKey.current = null;
      if (run.status === "succeeded") {
        currentCycleResultId.current = run.runId;
        cycleDetailGeneration.current += 1;
        setCompletedCycleRun(run);
        setCycleOffset(0);
        if (run.result?.crossVhostCount !== undefined) {
          setCycleSort("sdk");
          setCycleMinLength("");
          setCycleMaxLength("");
        }
        setCyclePageResult(null);
        setSelectedCycleDetail(null);
        setCycleDetailLoading(false);
        setCycleDetailError(null);
        setCycleError(null);
      } else if (run.status === "cancelled") {
        setCycleError("Structural-cycle analysis cancelled.");
      } else {
        setCycleError(run.error?.message ?? "Structural-cycle analysis failed.");
      }
    },
    [topology],
  );

  useEffect(() => {
    onActiveRunChange?.(computeRunActive);
    return () => onActiveRunChange?.(false);
  }, [computeRunActive, onActiveRunChange]);

  useEffect(() => {
    const recovery = pendingSccRecovery;
    if (!recovery || sccRequestPaused) {
      return;
    }
    let current = true;
    const controller = new AbortController();
    const lifetime = requestLifetime.current;
    activeIdempotencyKey.current = recovery.idempotencyKey;
    const recover = () =>
      recovery.runId
        ? fetchSccRun(recovery.runId, topology.snapshotRevision)
        : startSccRun(
            topology.source,
            topology.analysisId,
            topology.snapshotRevision,
            recovery.idempotencyKey,
          );
    void requestOperation(
      recover,
      lifetime,
      () => setSccError("Cyclic-component recovery is temporarily unavailable. Retrying…"),
      () => current,
      controller,
    )
      .then((run) => {
        if (current && isRequestCurrent(lifetime)) {
          setRecoveringScc(false);
          setPendingSccRecovery(null);
          applySccRun(run);
        }
      })
      .catch((error: unknown) => {
        if (current && isRequestCurrent(lifetime)) {
          setRecoveringScc(false);
          setStartingScc(false);
          if (operationRecoveryInvalid(error, recovery.runId === null)) {
            cancellingRunId.current = null;
            setCancellationPending(null);
            clearSccRecovery();
            setPendingSccRecovery(null);
            activeIdempotencyKey.current = null;
          } else {
            setSccRequestPaused(true);
          }
          setSccError(
            requestErrorMessage(
              error,
              "The active cyclic-component analysis could not be recovered.",
            ),
          );
        }
      });
    return () => {
      current = false;
      controller.abort();
    };
  }, [
    applySccRun,
    pendingSccRecovery,
    sccRequestPaused,
    requestOperation,
    isRequestCurrent,
    requestErrorMessage,
    topology,
  ]);

  useEffect(() => {
    if (!activeSccRun || sccRequestPaused || !isActiveRunStatus(activeSccRun.status)) {
      return;
    }
    let current = true;
    const controller = new AbortController();
    const lifetime = requestLifetime.current;
    const timeout = window.setTimeout(() => {
      void requestOperation(
        () => fetchSccRun(activeSccRun.runId, topology.snapshotRevision),
        lifetime,
        () => setSccError("Cyclic-component status is temporarily unavailable. Retrying…"),
        () => current,
        controller,
      )
        .then((run) => {
          if (current && isRequestCurrent(lifetime)) {
            applySccRun(run);
          }
        })
        .catch((error: unknown) => {
          if (current && isRequestCurrent(lifetime)) {
            if (operationRecoveryInvalid(error)) {
              clearSccRecovery(activeSccRun);
              activeIdempotencyKey.current = null;
              setActiveSccRun(null);
              cancellingRunId.current = null;
              setCancellationPending(null);
            } else {
              setSccRequestPaused(true);
            }
            setSccError(
              requestErrorMessage(error, "Cyclic-component polling stopped unexpectedly."),
            );
          }
        });
    }, RUN_POLL_INTERVAL_MS);
    return () => {
      current = false;
      controller.abort();
      window.clearTimeout(timeout);
    };
  }, [
    activeSccRun,
    sccRequestPaused,
    applySccRun,
    requestOperation,
    isRequestCurrent,
    requestErrorMessage,
    topology.snapshotRevision,
  ]);

  useEffect(() => {
    const recovery = pendingCycleRecovery;
    if (!recovery || cycleRequestPaused) return;
    let current = true;
    const controller = new AbortController();
    const lifetime = requestLifetime.current;
    activeIdempotencyKey.current = recovery.idempotencyKey;
    activeCycleOptions.current = {
      maxCycles: recovery.maxCycles,
      includeAllCrossVhost: recovery.includeAllCrossVhost,
    };
    const recover = () =>
      recovery.runId
        ? fetchCycleRun(recovery.runId, topology.snapshotRevision)
        : startCycleRun(
            topology.source,
            topology.analysisId,
            topology.snapshotRevision,
            recovery.maxCycles,
            recovery.idempotencyKey,
            recovery.includeAllCrossVhost,
          );
    void requestOperation(
      recover,
      lifetime,
      () => setCycleError("Structural-cycle recovery is temporarily unavailable. Retrying…"),
      () => current,
      controller,
    )
      .then((run) => {
        if (current && isRequestCurrent(lifetime)) {
          setRecoveringCycle(false);
          setPendingCycleRecovery(null);
          applyCycleRun(run);
        }
      })
      .catch((error: unknown) => {
        if (current && isRequestCurrent(lifetime)) {
          setRecoveringCycle(false);
          setStartingCycle(false);
          if (operationRecoveryInvalid(error, recovery.runId === null)) {
            cancellingRunId.current = null;
            setCancellationPending(null);
            clearCycleRecovery();
            setPendingCycleRecovery(null);
            activeIdempotencyKey.current = null;
          } else {
            setCycleRequestPaused(true);
          }
          setCycleError(
            requestErrorMessage(
              error,
              "The active structural-cycle analysis could not be recovered.",
            ),
          );
        }
      });
    return () => {
      current = false;
      controller.abort();
    };
  }, [
    applyCycleRun,
    pendingCycleRecovery,
    cycleRequestPaused,
    requestOperation,
    isRequestCurrent,
    requestErrorMessage,
    topology,
  ]);

  useEffect(() => {
    if (!activeCycleRun || cycleRequestPaused || !isActiveRunStatus(activeCycleRun.status)) return;
    let current = true;
    const controller = new AbortController();
    const lifetime = requestLifetime.current;
    const timeout = window.setTimeout(() => {
      void requestOperation(
        () => fetchCycleRun(activeCycleRun.runId, topology.snapshotRevision),
        lifetime,
        () => setCycleError("Structural-cycle status is temporarily unavailable. Retrying…"),
        () => current,
        controller,
      )
        .then((run) => {
          if (current && isRequestCurrent(lifetime)) applyCycleRun(run);
        })
        .catch((error: unknown) => {
          if (current && isRequestCurrent(lifetime)) {
            if (operationRecoveryInvalid(error)) {
              clearCycleRecovery(activeCycleRun);
              activeIdempotencyKey.current = null;
              setActiveCycleRun(null);
              cancellingRunId.current = null;
              setCancellationPending(null);
              unavailableCycleRunId.current = activeCycleRun.runId;
            } else {
              setCycleRequestPaused(true);
            }
            setCycleError(
              requestErrorMessage(error, "Structural-cycle polling stopped unexpectedly."),
            );
          }
        });
    }, RUN_POLL_INTERVAL_MS);
    return () => {
      current = false;
      controller.abort();
      window.clearTimeout(timeout);
    };
  }, [
    activeCycleRun,
    cycleRequestPaused,
    applyCycleRun,
    requestOperation,
    isRequestCurrent,
    requestErrorMessage,
    topology.snapshotRevision,
  ]);

  const runScc = useCallback(() => {
    const lifetime = requestLifetime.current;
    if (computeRunActive || !isRequestCurrent(lifetime)) {
      return;
    }
    const idempotencyKey = browserUuid();
    activeIdempotencyKey.current = idempotencyKey;
    setStartingScc(true);
    setDrawerTab("Components");
    setSccError(null);
    const recovery = {
      operation: "cyclic_scc",
      source: topology.source,
      analysisId: topology.analysisId,
      snapshotRevision: topology.snapshotRevision,
      idempotencyKey,
      runId: null,
    } as const;
    writeSccRecovery(recovery);
    void requestOperation(
      () =>
        startSccRun(
          topology.source,
          topology.analysisId,
          topology.snapshotRevision,
          idempotencyKey,
        ).then((run) => {
          if (isRequestCurrent(lifetime)) applySccRun(run);
          return run;
        }),
      lifetime,
      () => setSccError("Analysis admission is uncertain. Retrying the same request…"),
    ).catch((error: unknown) => {
      if (!isRequestCurrent(lifetime)) return;
      setStartingScc(false);
      if (operationRecoveryInvalid(error, true)) {
        clearSccRecovery();
        activeIdempotencyKey.current = null;
      } else {
        setPendingSccRecovery(recovery);
        setSccRequestPaused(true);
      }
      setSccError(requestErrorMessage(error, "Cyclic-component analysis could not be started."));
    });
  }, [
    applySccRun,
    computeRunActive,
    requestOperation,
    isRequestCurrent,
    requestErrorMessage,
    topology,
  ]);

  const stopScc = useCallback(() => {
    const lifetime = requestLifetime.current;
    const runId = activeSccRun?.runId ?? pendingSccRecovery?.runId;
    if (!runId || !isRequestCurrent(lifetime)) {
      return;
    }
    cancellingRunId.current = runId;
    setCancellationPending(runId);
    if (activeSccRun) setActiveSccRun({ ...activeSccRun, status: "cancelling" });
    void requestOperation(
      () => cancelSccRun(runId, topology.snapshotRevision),
      lifetime,
      () => setSccError("Cancellation is temporarily unavailable. Retrying…"),
      () => cancellingRunId.current === runId,
    )
      .then((run) => {
        if (isRequestCurrent(lifetime) && cancellingRunId.current === runId) {
          applySccRun(run);
        }
      })
      .catch((error: unknown) => {
        if (!isRequestCurrent(lifetime) || cancellingRunId.current !== runId) return;
        cancellingRunId.current = null;
        setCancellationPending(null);
        if (operationRecoveryInvalid(error)) {
          setActiveSccRun(null);
          setPendingSccRecovery(null);
          setRecoveringScc(false);
          clearSccRecovery();
          activeIdempotencyKey.current = null;
        } else {
          setActiveSccRun(activeSccRun);
        }
        setSccError(
          requestErrorMessage(error, "Cyclic-component analysis could not be cancelled."),
        );
      });
  }, [
    activeSccRun,
    pendingSccRecovery,
    applySccRun,
    requestOperation,
    isRequestCurrent,
    requestErrorMessage,
    topology.snapshotRevision,
  ]);

  const runCycle = useCallback(() => {
    const lifetime = requestLifetime.current;
    if (computeRunActive || parsedMaxCycles === null || !isRequestCurrent(lifetime)) return;
    const idempotencyKey = browserUuid();
    activeIdempotencyKey.current = idempotencyKey;
    activeCycleOptions.current = { maxCycles: parsedMaxCycles, includeAllCrossVhost: true };
    setStartingCycle(true);
    setDrawerTab("Cycles");
    setCycleError(null);
    const recovery = {
      operation: "structural_cycles",
      source: topology.source,
      analysisId: topology.analysisId,
      snapshotRevision: topology.snapshotRevision,
      maxCycles: parsedMaxCycles,
      includeAllCrossVhost: true,
      idempotencyKey,
      runId: null,
    } as const;
    writeCycleRecovery(recovery);
    void requestOperation(
      () =>
        startCycleRun(
          topology.source,
          topology.analysisId,
          topology.snapshotRevision,
          parsedMaxCycles,
          idempotencyKey,
          recovery.includeAllCrossVhost,
        ).then((run) => {
          if (isRequestCurrent(lifetime)) applyCycleRun(run);
          return run;
        }),
      lifetime,
      () => setCycleError("Analysis admission is uncertain. Retrying the same request…"),
    ).catch((error: unknown) => {
      if (!isRequestCurrent(lifetime)) return;
      setStartingCycle(false);
      if (operationRecoveryInvalid(error, true)) {
        clearCycleRecovery();
        activeIdempotencyKey.current = null;
      } else {
        setPendingCycleRecovery(recovery);
        setCycleRequestPaused(true);
      }
      setCycleError(requestErrorMessage(error, "Structural-cycle analysis could not be started."));
    });
  }, [
    applyCycleRun,
    computeRunActive,
    requestOperation,
    isRequestCurrent,
    parsedMaxCycles,
    requestErrorMessage,
    topology,
  ]);

  const stopCycle = useCallback(() => {
    const lifetime = requestLifetime.current;
    const runId = activeCycleRun?.runId ?? pendingCycleRecovery?.runId;
    if (!runId || !isRequestCurrent(lifetime)) return;
    cancellingRunId.current = runId;
    setCancellationPending(runId);
    if (activeCycleRun) setActiveCycleRun({ ...activeCycleRun, status: "cancelling" });
    void requestOperation(
      () => cancelCycleRun(runId, topology.snapshotRevision),
      lifetime,
      () => setCycleError("Cancellation is temporarily unavailable. Retrying…"),
      () => cancellingRunId.current === runId,
    )
      .then((run) => {
        if (isRequestCurrent(lifetime) && cancellingRunId.current === runId) {
          applyCycleRun(run);
        }
      })
      .catch((error: unknown) => {
        if (!isRequestCurrent(lifetime) || cancellingRunId.current !== runId) return;
        cancellingRunId.current = null;
        setCancellationPending(null);
        if (operationRecoveryInvalid(error)) {
          unavailableCycleRunId.current = runId;
          setActiveCycleRun(null);
          setPendingCycleRecovery(null);
          setRecoveringCycle(false);
          clearCycleRecovery();
          activeIdempotencyKey.current = null;
        } else {
          setActiveCycleRun(activeCycleRun);
        }
        setCycleError(
          requestErrorMessage(error, "Structural-cycle analysis could not be cancelled."),
        );
      });
  }, [
    activeCycleRun,
    pendingCycleRecovery,
    applyCycleRun,
    requestOperation,
    isRequestCurrent,
    requestErrorMessage,
    topology.snapshotRevision,
  ]);

  useEffect(() => {
    const run = completedCycleRun;
    if (!run?.result || cycleLengthError) return;
    let current = true;
    const lifetime = requestLifetime.current;
    void fetchCyclePage(run.runId, topology.snapshotRevision, {
      offset: cycleOffset,
      limit: 50,
      sort: cycleSort,
      minLength: parsedMinLength ?? undefined,
      maxLength: parsedMaxLength ?? undefined,
    })
      .then((page) => {
        if (
          current &&
          isRequestCurrent(lifetime) &&
          currentCycleResultId.current === run.runId &&
          unavailableCycleRunId.current !== run.runId &&
          page.runId === run.runId &&
          page.snapshotRevision === topology.snapshotRevision
        ) {
          setCyclePageResult({ requestKey: cyclePageRequestKey, page, error: null });
        }
      })
      .catch((error: unknown) => {
        if (
          current &&
          isRequestCurrent(lifetime) &&
          currentCycleResultId.current === run.runId &&
          unavailableCycleRunId.current !== run.runId
        ) {
          if (error instanceof ApiError && error.status === 404) {
            settleUnavailableCycleResult(run, error);
            return;
          }
          setCyclePageResult({
            requestKey: cyclePageRequestKey,
            page: null,
            error: requestErrorMessage(error, "Cycle summaries could not be loaded."),
          });
        }
      });
    return () => {
      current = false;
    };
  }, [
    completedCycleRun,
    cyclePageRequestKey,
    cycleLengthError,
    cycleMaxLength,
    cycleMinLength,
    cycleOffset,
    cycleSort,
    isRequestCurrent,
    parsedMaxLength,
    parsedMinLength,
    requestErrorMessage,
    settleUnavailableCycleResult,
    topology.snapshotRevision,
  ]);

  const selectCycle = useCallback(
    (cycleIndex: number) => {
      const run = completedCycleRun;
      const lifetime = requestLifetime.current;
      if (!run?.result || !isRequestCurrent(lifetime)) return;
      const generation = cycleDetailGeneration.current + 1;
      cycleDetailGeneration.current = generation;
      setCycleDetailLoading(true);
      setCycleDetailError(null);
      void fetchCycleDetail(run.runId, cycleIndex, topology.snapshotRevision)
        .then((detail) => {
          if (!isRequestCurrent(lifetime) || generation !== cycleDetailGeneration.current) return;
          if (
            detail.runId !== run.runId ||
            detail.snapshotRevision !== topology.snapshotRevision ||
            detail.cycleIndex !== cycleIndex
          ) {
            setCycleDetailLoading(false);
            setCycleDetailError("The cycle detail response did not match the selected result.");
            return;
          }
          setSelectedCycleDetail(detail);
          setCycleDetailLoading(false);
        })
        .catch((error: unknown) => {
          if (isRequestCurrent(lifetime) && generation === cycleDetailGeneration.current) {
            if (error instanceof ApiError && error.status === 404) {
              settleUnavailableCycleResult(run, error);
              return;
            }
            setCycleDetailLoading(false);
            setCycleDetailError(
              requestErrorMessage(error, "The exact cycle path could not be loaded."),
            );
          }
        });
    },
    [
      completedCycleRun,
      isRequestCurrent,
      requestErrorMessage,
      settleUnavailableCycleResult,
      topology.snapshotRevision,
    ],
  );

  function changeVhost(vhost: string, visible: boolean) {
    setSelectedVhosts((current) => {
      const next = new Set(current);
      if (visible) {
        next.add(vhost);
      } else {
        next.delete(vhost);
      }
      return next;
    });
  }

  function changeKind(kind: TopologyNodeKind, visible: boolean) {
    setVisibleKinds((current) => {
      const next = new Set(current);
      if (visible) {
        next.add(kind);
      } else {
        next.delete(kind);
      }
      return next;
    });
  }

  function clearCycleFocus() {
    cycleDetailGeneration.current += 1;
    setSelectedCycleDetail(null);
    setCycleDetailLoading(false);
    setCycleDetailError(null);
  }

  function clearCycleFocusAndShowGraph() {
    clearCycleFocus();
    returnToGraph();
  }

  function clearComponentFocusAndShowGraph() {
    setSelectedComponentIndexes(new Set());
    returnToGraph();
  }

  const emptyReason =
    !sccFocusActive && !cycleFocusActive && selectedVhosts.size === 0
      ? "No vhosts selected. Select at least one vhost to render resources."
      : visibleTopology.nodes.length === 0
        ? "No resources match the current visibility controls."
        : null;

  return (
    <>
      <div className="workbench-main">
        <details
          ref={controlsDisclosureRef}
          className="controls-disclosure"
          open={controlsOpen}
          onToggle={(event) => setControlsOpen(event.currentTarget.open)}
        >
          <summary>Filters, search &amp; operations</summary>
          <ExplorerSidebar
            access={access}
            vhosts={vhosts}
            selectedVhosts={selectedVhosts}
            visibleKinds={visibleKinds}
            hideDefaultExchanges={hideDefaultExchanges}
            searchQuery={searchQuery}
            searchMatchCount={searchMatchIds.size}
            searchMatches={searchMatches}
            searchFocusId={searchFocus?.id ?? null}
            onSearchMatchSelect={(id) => {
              // Selecting the same match again requests another viewport move.
              setSearchFocus({ id });
              inspect({ kind: "node", id });
            }}
            sccStatusText={currentSccStatusText}
            cycleStatusText={currentCycleRunStatusText}
            onShowComponents={() => openDrawer("Components")}
            onShowCycles={() => openDrawer("Cycles")}
            onReturnToGraph={returnToGraph}
            sccRunStatus={
              cancellationPending &&
              cancellationPending === (activeSccRun?.runId ?? pendingSccRecovery?.runId)
                ? "cancelling"
                : startingScc || recoveringScc || pendingSccRecovery
                  ? "queued"
                  : (activeSccRun?.status ?? completedSccRun?.status ?? null)
            }
            sccCanCancel={Boolean(activeSccRun?.runId ?? pendingSccRecovery?.runId)}
            sccCanRetry={sccRequestPaused && !cancellationPending}
            onRetryScc={() => {
              setSccError(null);
              setSccRequestPaused(false);
              if (pendingSccRecovery) setRecoveringScc(true);
            }}
            cycleMaxCycles={cycleMaxCycles}
            cycleMaxCyclesError={cycleMaxCyclesError}
            cycleMaxCyclesUnbounded={parsedMaxCycles === 0}
            cycleRunStatus={
              cancellationPending &&
              cancellationPending === (activeCycleRun?.runId ?? pendingCycleRecovery?.runId)
                ? "cancelling"
                : startingCycle || recoveringCycle || pendingCycleRecovery
                  ? "queued"
                  : (activeCycleRun?.status ?? completedCycleRun?.status ?? null)
            }
            cycleCanCancel={Boolean(activeCycleRun?.runId ?? pendingCycleRecovery?.runId)}
            cycleCanRetry={cycleRequestPaused && !cancellationPending}
            onRetryCycle={() => {
              setCycleError(null);
              setCycleRequestPaused(false);
              if (pendingCycleRecovery) setRecoveringCycle(true);
            }}
            computeRunActive={computeRunActive}
            onVhostChange={changeVhost}
            onAllVhostsChange={(visible) => setSelectedVhosts(new Set(visible ? vhosts : []))}
            onKindChange={changeKind}
            onHideDefaultExchangesChange={setHideDefaultExchanges}
            onSearchQueryChange={(query) => {
              setSearchFocus(null);
              setSearchQuery(query);
            }}
            onStartScc={runScc}
            onCancelScc={stopScc}
            onCycleMaxCyclesChange={setCycleMaxCycles}
            onStartCycle={runCycle}
            onCancelCycle={stopCycle}
          />
        </details>
        <main className="canvas-region">
          <nav className="graph-toolbar" aria-label="Graph inspection and results">
            <button ref={graphEntryRef} type="button" onClick={() => openDrawer("Inspection")}>
              {inspectionLabel ? `Details: ${inspectionLabel}` : "Open inspection"}
            </button>
            <button type="button" onClick={() => openDrawer("Components")}>
              Components{completedSccRun?.result ? ` (${sccComponents.length})` : ""}
            </button>
            <button type="button" onClick={() => openDrawer("Cycles")}>
              Cycles{completedCycleRun?.result ? ` (${completedCycleRun.result.cycleCount})` : ""}
            </button>
            <span className="graph-keyboard-hint">From drawing: Esc to details controls</span>
          </nav>
          <section className="graph-mode" aria-label="Graph presentation">
            <strong>{graphMode}</strong>
            {revealedNodes.length > 0 && (
              <p>
                Selected result overrides {overriddenFilters.join(", ")}: {revealedNodes.length}{" "}
                resource{revealedNodes.length === 1 ? "" : "s"} hidden by exploration settings
                {revealedNodes.length === 1 ? " is" : " are"} shown to keep the complete result.
              </p>
            )}
            {cycleFocusActive ? (
              <button
                type="button"
                className="text-button"
                onClick={clearCycleFocusAndShowGraph}
              >
                Clear cycle filter
              </button>
            ) : sccFocusActive ? (
              <button
                type="button"
                className="text-button"
                onClick={clearComponentFocusAndShowGraph}
              >
                Clear component filter
              </button>
            ) : null}
          </section>
          <GraphLegend
            focus={cycleFocusActive ? "cycle" : sccFocusActive ? "components" : "explore"}
          />
          {emptyReason ? (
            <div className="topology-canvas canvas-empty" role="status">
              <div>
                <span aria-hidden="true">⌁</span>
                <h2>Nothing to display</h2>
                <p>{emptyReason}</p>
              </div>
            </div>
          ) : (
            <TopologyCanvas
              topology={visibleTopology}
              searchMatchIds={searchMatchIds}
              searchFocus={searchFocus}
              selection={selection}
              onSelectionChange={inspect}
              onExitCanvas={returnToGraph}
              resetGeneration={resetGeneration}
              componentIndexByNode={visibleComponentByNode}
              cycleStepByNode={visibleCycleSteps.nodes}
              cycleStepByEdge={visibleCycleSteps.edges}
            />
          )}
          <section
            ref={drawerRef}
            className={`results-drawer${drawerOpen ? " results-drawer--open" : ""}`}
          >
            <button
              type="button"
              className="drawer-toggle"
              aria-expanded={drawerOpen}
              aria-controls="results-drawer-content"
              onClick={() => setDrawerOpen((open) => !open)}
            >
              <span>Results &amp; inspection</span>
              <span aria-hidden="true">{drawerOpen ? "⌄" : "⌃"}</span>
            </button>
            {drawerOpen && (
              <div id="results-drawer-content" className="drawer-content">
                <div
                  role="tablist"
                  aria-label="Results and inspection tabs"
                  onKeyDown={(event) => {
                    const index = DRAWER_TABS.indexOf(drawerTab);
                    const next =
                      event.key === "ArrowRight"
                        ? (index + 1) % 3
                        : event.key === "ArrowLeft"
                          ? (index + 2) % 3
                          : event.key === "Home"
                            ? 0
                            : event.key === "End"
                              ? 2
                              : null;
                    if (next === null) return;
                    event.preventDefault();
                    openDrawer(DRAWER_TABS[next]);
                  }}
                >
                  {DRAWER_TABS.map((tab) => (
                    <button
                      key={tab}
                      type="button"
                      role="tab"
                      id={`tab-${tab}`}
                      aria-selected={drawerTab === tab}
                      aria-controls={`panel-${tab}`}
                      tabIndex={drawerTab === tab ? 0 : -1}
                      onClick={() => setDrawerTab(tab)}
                    >
                      {tab}
                    </button>
                  ))}
                </div>
                <button type="button" className="text-button" onClick={returnToGraph}>
                  Return to graph
                </button>
                <div
                  role="tabpanel"
                  id="panel-Inspection"
                  aria-labelledby="tab-Inspection"
                  hidden={drawerTab !== "Inspection"}
                >
                  <DetailsPanel
                    selection={selection}
                    nodes={topology.nodes}
                    edges={topology.edges}
                  />
                </div>
                <div
                  role="tabpanel"
                  id="panel-Components"
                  aria-labelledby="tab-Components"
                  hidden={drawerTab !== "Components"}
                >
                  <SccResults
                    completedRun={completedSccRun}
                    statusText={currentSccStatusText}
                    selectedComponentIndexes={selectedComponentIndexes}
                    nodes={topology.nodes}
                    onComponentChange={(componentIndex, selected) => {
                      setSelectedComponentIndexes((current) => {
                        const next = new Set(current);
                        if (selected) {
                          next.add(componentIndex);
                        } else {
                          next.delete(componentIndex);
                        }
                        return next;
                      });
                    }}
                    onClearFocus={clearComponentFocusAndShowGraph}
                  />
                </div>
                <div
                  role="tabpanel"
                  id="panel-Cycles"
                  aria-labelledby="tab-Cycles"
                  hidden={drawerTab !== "Cycles"}
                >
                  <CycleResults
                    completedRun={completedCycleRun}
                    runStatusText={currentCycleRunStatusText}
                    page={cyclePage}
                    pageLoading={cyclePageLoading}
                    pageStatusText={currentCyclePageStatusText}
                    sort={cycleSort}
                    minLength={cycleMinLength}
                    maxLength={cycleMaxLength}
                    selectedDetail={selectedCycleDetail}
                    detailStatusText={currentCycleDetailStatusText}
                    nodes={topology.nodes}
                    edges={topology.edges}
                    onSortChange={(sort) => {
                      setCycleOffset(0);
                      setCycleSort(sort);
                    }}
                    onMinLengthChange={(value) => {
                      setCycleOffset(0);
                      setCycleMinLength(value);
                    }}
                    onMaxLengthChange={(value) => {
                      setCycleOffset(0);
                      setCycleMaxLength(value);
                    }}
                    onPageOffsetChange={setCycleOffset}
                    onSelectCycle={selectCycle}
                    onShowGraph={returnToGraph}
                    onClearFocus={clearCycleFocusAndShowGraph}
                  />
                </div>
              </div>
            )}
          </section>
        </main>
      </div>

      <p
        className="visually-hidden"
        role="status"
        aria-live="polite"
        aria-atomic="true"
        aria-label="Cyclic-component operation status"
      >
        {currentSccStatusText}
      </p>
      <p
        className="visually-hidden"
        role="status"
        aria-live="polite"
        aria-atomic="true"
        aria-label="Structural-cycle operation status"
      >
        {currentCycleRunStatusText}
      </p>
      <p
        className="visually-hidden"
        role="status"
        aria-live="polite"
        aria-atomic="true"
        aria-label="Cycle summary status"
      >
        {currentCyclePageStatusText}
      </p>
      <p
        className="visually-hidden"
        role="status"
        aria-live="polite"
        aria-atomic="true"
        aria-label="Cycle detail status"
      >
        {currentCycleDetailStatusText}
      </p>
    </>
  );
}
