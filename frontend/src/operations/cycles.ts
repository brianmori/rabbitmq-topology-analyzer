import type { AnalysisSource, CycleRunResponse, TopologyResponse } from "../types/topology";

export const ACTIVE_CYCLE_RUN_STORAGE_KEY = "rabbitmq-topology-active-cycle-run";

export interface ActiveCycleRunRecovery {
  operation: "structural_cycles";
  source: AnalysisSource;
  analysisId: string;
  snapshotRevision: string;
  maxCycles: number;
  idempotencyKey: string;
  runId: string | null;
}

/** Reads a structurally valid recovery record before its catalogue snapshot is selected. */
export function readStoredCycleRecovery(): ActiveCycleRunRecovery | null {
  const encoded = sessionStorage.getItem(ACTIVE_CYCLE_RUN_STORAGE_KEY);
  if (!encoded) return null;
  try {
    const value = JSON.parse(encoded) as Partial<ActiveCycleRunRecovery>;
    if (
      value.operation !== "structural_cycles" ||
      (value.source !== "static" && value.source !== "session") ||
      typeof value.analysisId !== "string" ||
      typeof value.snapshotRevision !== "string" ||
      typeof value.maxCycles !== "number" ||
      !Number.isSafeInteger(value.maxCycles) ||
      value.maxCycles < 0 ||
      typeof value.idempotencyKey !== "string" ||
      (value.runId !== null && typeof value.runId !== "string")
    ) {
      sessionStorage.removeItem(ACTIVE_CYCLE_RUN_STORAGE_KEY);
      return null;
    }
    return value as ActiveCycleRunRecovery;
  } catch {
    sessionStorage.removeItem(ACTIVE_CYCLE_RUN_STORAGE_KEY);
    return null;
  }
}

/** Reads only a valid cycle recovery record for the loaded immutable snapshot. */
export function readCycleRecovery(topology: TopologyResponse): ActiveCycleRunRecovery | null {
  const value = readStoredCycleRecovery();
  if (!value) return null;
  if (
    value.source !== topology.source ||
    value.analysisId !== topology.analysisId ||
    value.snapshotRevision !== topology.snapshotRevision
  ) {
    sessionStorage.removeItem(ACTIVE_CYCLE_RUN_STORAGE_KEY);
    return null;
  }
  return value;
}

export function writeCycleRecovery(recovery: ActiveCycleRunRecovery): void {
  sessionStorage.setItem(ACTIVE_CYCLE_RUN_STORAGE_KEY, JSON.stringify(recovery));
}

export function clearCycleRecovery(run?: CycleRunResponse): void {
  if (!run) {
    sessionStorage.removeItem(ACTIVE_CYCLE_RUN_STORAGE_KEY);
    return;
  }
  const encoded = sessionStorage.getItem(ACTIVE_CYCLE_RUN_STORAGE_KEY);
  if (!encoded) return;
  try {
    const value = JSON.parse(encoded) as Partial<ActiveCycleRunRecovery>;
    if (value.runId === null || value.runId === run.runId) {
      sessionStorage.removeItem(ACTIVE_CYCLE_RUN_STORAGE_KEY);
    }
  } catch {
    sessionStorage.removeItem(ACTIVE_CYCLE_RUN_STORAGE_KEY);
  }
}
