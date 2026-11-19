import type {
  AnalysisSource,
  RunStatus,
  SccRunResponse,
  TopologyResponse,
} from "../types/topology";

export const ACTIVE_SCC_RUN_STORAGE_KEY = "rabbitmq-topology-active-scc-run";

export interface ActiveSccRunRecovery {
  operation: "cyclic_scc";
  source: AnalysisSource;
  analysisId: string;
  snapshotRevision: string;
  idempotencyKey: string;
  runId: string | null;
}

export function isActiveRunStatus(status: RunStatus): boolean {
  return status === "queued" || status === "running" || status === "cancelling";
}

/** Reads a structurally valid recovery record before its catalogue snapshot is selected. */
export function readStoredSccRecovery(): ActiveSccRunRecovery | null {
  const encoded = sessionStorage.getItem(ACTIVE_SCC_RUN_STORAGE_KEY);
  if (!encoded) {
    return null;
  }
  try {
    const value = JSON.parse(encoded) as Partial<ActiveSccRunRecovery>;
    if (
      value.operation !== "cyclic_scc" ||
      (value.source !== "static" && value.source !== "session") ||
      typeof value.analysisId !== "string" ||
      typeof value.snapshotRevision !== "string" ||
      typeof value.idempotencyKey !== "string" ||
      (value.runId !== null && typeof value.runId !== "string")
    ) {
      sessionStorage.removeItem(ACTIVE_SCC_RUN_STORAGE_KEY);
      return null;
    }
    return value as ActiveSccRunRecovery;
  } catch {
    sessionStorage.removeItem(ACTIVE_SCC_RUN_STORAGE_KEY);
    return null;
  }
}

/** Reads only a valid recovery record for the currently loaded immutable snapshot. */
export function readSccRecovery(topology: TopologyResponse): ActiveSccRunRecovery | null {
  const value = readStoredSccRecovery();
  if (!value) {
    return null;
  }
  if (
    value.source !== topology.source ||
    value.analysisId !== topology.analysisId ||
    value.snapshotRevision !== topology.snapshotRevision
  ) {
    sessionStorage.removeItem(ACTIVE_SCC_RUN_STORAGE_KEY);
    return null;
  }
  return value;
}

export function writeSccRecovery(recovery: ActiveSccRunRecovery): void {
  sessionStorage.setItem(ACTIVE_SCC_RUN_STORAGE_KEY, JSON.stringify(recovery));
}

export function clearSccRecovery(run?: SccRunResponse): void {
  if (!run) {
    sessionStorage.removeItem(ACTIVE_SCC_RUN_STORAGE_KEY);
    return;
  }
  const encoded = sessionStorage.getItem(ACTIVE_SCC_RUN_STORAGE_KEY);
  if (!encoded) {
    return;
  }
  try {
    const value = JSON.parse(encoded) as Partial<ActiveSccRunRecovery>;
    if (value.runId === null || value.runId === run.runId) {
      sessionStorage.removeItem(ACTIVE_SCC_RUN_STORAGE_KEY);
    }
  } catch {
    sessionStorage.removeItem(ACTIVE_SCC_RUN_STORAGE_KEY);
  }
}
