import { describe, expect, it } from "vitest";
import { topologyFixture } from "../test/api-fixtures";
import {
  ACTIVE_SCC_RUN_STORAGE_KEY,
  isActiveRunStatus,
  readSccRecovery,
  readStoredSccRecovery,
} from "./scc";

describe("SCC recovery state", () => {
  it("keeps only active statuses and exact-snapshot recovery records", () => {
    expect(isActiveRunStatus("queued")).toBe(true);
    expect(isActiveRunStatus("running")).toBe(true);
    expect(isActiveRunStatus("cancelling")).toBe(true);
    expect(isActiveRunStatus("succeeded")).toBe(false);

    sessionStorage.setItem(
      ACTIVE_SCC_RUN_STORAGE_KEY,
      JSON.stringify({
        operation: "cyclic_scc",
        source: topologyFixture.source,
        analysisId: topologyFixture.analysisId,
        snapshotRevision: `sha256:${"f".repeat(64)}`,
        idempotencyKey: "20000000-0000-4000-8000-000000000002",
        runId: null,
      }),
    );

    expect(readSccRecovery(topologyFixture)).toBeNull();
    expect(sessionStorage.getItem(ACTIVE_SCC_RUN_STORAGE_KEY)).toBeNull();
  });

  it("reads a valid target before topology selection without discarding it", () => {
    const recovery = {
      operation: "cyclic_scc",
      source: topologyFixture.source,
      analysisId: topologyFixture.analysisId,
      snapshotRevision: topologyFixture.snapshotRevision,
      idempotencyKey: "20000000-0000-4000-8000-000000000002",
      runId: null,
    } as const;
    sessionStorage.setItem(ACTIVE_SCC_RUN_STORAGE_KEY, JSON.stringify(recovery));

    expect(readStoredSccRecovery()).toEqual(recovery);
    expect(sessionStorage.getItem(ACTIVE_SCC_RUN_STORAGE_KEY)).not.toBeNull();
  });
});
