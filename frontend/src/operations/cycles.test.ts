import { describe, expect, it } from "vitest";
import { succeededCycleRunFixture, topologyFixture } from "../test/api-fixtures";
import {
  ACTIVE_CYCLE_RUN_STORAGE_KEY,
  clearCycleRecovery,
  readCycleRecovery,
  readStoredCycleRecovery,
  writeCycleRecovery,
} from "./cycles";

describe("cycle run recovery", () => {
  it("retains exact SDK parameters and accepts only the current snapshot", () => {
    const recovery = {
      operation: "structural_cycles",
      source: topologyFixture.source,
      analysisId: topologyFixture.analysisId,
      snapshotRevision: topologyFixture.snapshotRevision,
      maxCycles: 0,
      idempotencyKey: "40000000-0000-4000-8000-000000000004",
      runId: succeededCycleRunFixture.runId,
    } as const;

    writeCycleRecovery(recovery);

    expect(readStoredCycleRecovery()).toEqual(recovery);
    expect(readCycleRecovery(topologyFixture)).toEqual(recovery);
    expect(
      readCycleRecovery({ ...topologyFixture, snapshotRevision: `sha256:${"f".repeat(64)}` }),
    ).toBeNull();
  });

  it("clears only the matching completed run", () => {
    writeCycleRecovery({
      operation: "structural_cycles",
      source: topologyFixture.source,
      analysisId: topologyFixture.analysisId,
      snapshotRevision: topologyFixture.snapshotRevision,
      maxCycles: 20,
      idempotencyKey: "40000000-0000-4000-8000-000000000004",
      runId: "50000000-0000-4000-8000-000000000005",
    });

    clearCycleRecovery(succeededCycleRunFixture);
    expect(sessionStorage.getItem(ACTIVE_CYCLE_RUN_STORAGE_KEY)).not.toBeNull();
    clearCycleRecovery();
    expect(sessionStorage.getItem(ACTIVE_CYCLE_RUN_STORAGE_KEY)).toBeNull();
  });
});
