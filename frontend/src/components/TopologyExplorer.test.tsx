import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import * as topologyApi from "../api/topology";
import type { GraphSelection } from "./DetailsPanel";
import type { VisibleTopology } from "../graph/presentation";
import {
  accessFixture,
  cycleDetailFixture,
  cyclePageFixture,
  runningCycleRunFixture,
  runningSccRunFixture,
  succeededCycleRunFixture,
  succeededSccRunFixture,
  topologyFixture,
} from "../test/api-fixtures";
import { ACTIVE_SCC_RUN_STORAGE_KEY } from "../operations/scc";
import { ACTIVE_CYCLE_RUN_STORAGE_KEY } from "../operations/cycles";
import type {
  CycleDetail,
  CyclePage,
  CycleRunResponse,
  SccRunResponse,
  TopologyResponse,
} from "../types/topology";
import { TopologyExplorer } from "./TopologyExplorer";

vi.mock("../graph/TopologyCanvas", () => ({
  TopologyCanvas: ({
    topology,
    searchMatchIds,
    onSelectionChange,
    cycleStepByNode,
    cycleStepByEdge,
  }: {
    topology: VisibleTopology;
    searchMatchIds: ReadonlySet<string>;
    cycleStepByNode?: ReadonlyMap<string, number>;
    cycleStepByEdge?: ReadonlyMap<string, number>;
    onSelectionChange: (selection: GraphSelection) => void;
  }) => (
    <div aria-label="Test topology canvas">
      <span>Node IDs: {topology.nodes.map((node) => node.id).join(",")}</span>
      <span>Edge IDs: {topology.edges.map((edge) => edge.id).join(",")}</span>
      <span>Search IDs: {[...searchMatchIds].join(",")}</span>
      {topology.nodes.some((node) => node.id === "n1.orders-queue") && (
        <button
          type="button"
          onClick={() => onSelectionChange({ kind: "node", id: "n1.orders-queue" })}
        >
          Inspect test queue
        </button>
      )}
      <span>
        Cycle node steps:{" "}
        {[...(cycleStepByNode ?? new Map())].map(([id, step]) => `${id}:P${step + 1}`).join(",")}
      </span>
      <span>
        Cycle edge steps:{" "}
        {[...(cycleStepByEdge ?? new Map())].map(([id, step]) => `${id}:P${step + 1}`).join(",")}
      </span>
    </div>
  ),
}));

function resultRegion(name: "Cyclic components" | "Structural cycles") {
  return within(screen.getByRole("region", { name }));
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((next, fail) => {
    resolve = next;
    reject = fail;
  });
  return { promise, resolve, reject };
}

describe("TopologyExplorer", () => {
  function mockRunningOperations() {
    const sccStart = vi.spyOn(topologyApi, "startSccRun").mockResolvedValue(runningSccRunFixture);
    const cycleStart = vi
      .spyOn(topologyApi, "startCycleRun")
      .mockResolvedValue(runningCycleRunFixture);
    const sccPoll = vi.spyOn(topologyApi, "fetchSccRun").mockResolvedValue(succeededSccRunFixture);
    const cyclePoll = vi
      .spyOn(topologyApi, "fetchCycleRun")
      .mockResolvedValue(succeededCycleRunFixture);
    vi.spyOn(topologyApi, "fetchCyclePage").mockResolvedValue(cyclePageFixture);
    vi.spyOn(topologyApi, "cancelSccRun").mockResolvedValue({
      ...runningSccRunFixture,
      status: "cancelled",
    });
    vi.spyOn(topologyApi, "cancelCycleRun").mockResolvedValue({
      ...runningCycleRunFixture,
      status: "cancelled",
    });
    return { sccStart, cycleStart, sccPoll, cyclePoll };
  }

  async function advance(milliseconds: number) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(milliseconds);
    });
  }

  it.each(["SCC", "cycle"] as const)(
    "retains the original %s run through a 503 and network polling failure",
    async (operation) => {
      vi.useFakeTimers();
      const mocks = mockRunningOperations();
      const failure = new topologyApi.ApiError(
        503,
        "authentication_capacity_unavailable",
        "Busy.",
        "2",
      );
      mocks.sccPoll
        .mockRejectedValueOnce(failure)
        .mockRejectedValueOnce(new TypeError("Network failure"));
      mocks.cyclePoll
        .mockRejectedValueOnce(failure)
        .mockRejectedValueOnce(new TypeError("Network failure"));
      const storageKey =
        operation === "SCC" ? ACTIVE_SCC_RUN_STORAGE_KEY : ACTIVE_CYCLE_RUN_STORAGE_KEY;
      const poll = operation === "SCC" ? mocks.sccPoll : mocks.cyclePoll;
      const start = operation === "SCC" ? mocks.sccStart : mocks.cycleStart;
      const view = render(
        <TopologyExplorer access={accessFixture} topology={topologyFixture} resetGeneration={0} />,
      );
      try {
        fireEvent.click(
          screen.getByRole("button", {
            name: operation === "SCC" ? "Run cyclic components" : "Run structural cycles",
          }),
        );
        await advance(0);
        const recovery = sessionStorage.getItem(storageKey);
        expect(recovery).not.toBeNull();
        await advance(600);
        expect(poll).toHaveBeenCalledTimes(1);
        expect(sessionStorage.getItem(storageKey)).toBe(recovery);
        expect(screen.getByRole("button", { name: "Cancel analysis" })).toBeEnabled();
        expect(screen.getByRole("button", { name: /running…/ })).toHaveAttribute("aria-disabled", "true");
        await advance(1999);
        expect(poll).toHaveBeenCalledTimes(1);
        await advance(1);
        expect(poll).toHaveBeenCalledTimes(2);
        expect(sessionStorage.getItem(storageKey)).toBe(recovery);
        await advance(2000);
        expect(poll).toHaveBeenCalledTimes(3);
        expect(start).toHaveBeenCalledTimes(1);
        expect(sessionStorage.getItem(storageKey)).toBeNull();
        expect(screen.queryByRole("button", { name: "Cancel analysis" })).not.toBeInTheDocument();
      } finally {
        view.unmount();
        vi.useRealTimers();
      }
    },
  );

  it.each(["SCC", "cycle"] as const)(
    "bounds uncertain %s admission retries and manually recovers with the original key",
    async (operation) => {
      vi.useFakeTimers();
      const mocks = mockRunningOperations();
      mocks.sccStart
        .mockRejectedValueOnce(new TypeError("Lost admission response"))
        .mockRejectedValueOnce(new TypeError("Offline"))
        .mockRejectedValueOnce(new TypeError("Offline"));
      mocks.cycleStart
        .mockRejectedValueOnce(new TypeError("Lost admission response"))
        .mockRejectedValueOnce(new TypeError("Offline"))
        .mockRejectedValueOnce(new TypeError("Offline"));
      const start = operation === "SCC" ? mocks.sccStart : mocks.cycleStart;
      const storageKey =
        operation === "SCC" ? ACTIVE_SCC_RUN_STORAGE_KEY : ACTIVE_CYCLE_RUN_STORAGE_KEY;
      const view = render(
        <TopologyExplorer access={accessFixture} topology={topologyFixture} resetGeneration={0} />,
      );
      try {
        fireEvent.click(
          screen.getByRole("button", {
            name: operation === "SCC" ? "Run cyclic components" : "Run structural cycles",
          }),
        );
        await advance(0);
        const recovery = sessionStorage.getItem(storageKey);
        expect(JSON.parse(recovery!).runId).toBeNull();
        await advance(1000);
        await advance(2000);
        expect(start).toHaveBeenCalledTimes(3);
        expect(sessionStorage.getItem(storageKey)).toBe(recovery);
        expect(screen.getByRole("button", { name: "Retry analysis recovery" })).toBeEnabled();
        await advance(30_000);
        expect(start).toHaveBeenCalledTimes(3);
        fireEvent.click(screen.getByRole("button", { name: "Retry analysis recovery" }));
        await advance(0);
        expect(start).toHaveBeenCalledTimes(4);
        expect(
          start.mock.calls.every(
            (args) => JSON.stringify(args) === JSON.stringify(start.mock.calls[0]),
          ),
        ).toBe(true);
        fireEvent.click(screen.getByRole("button", { name: "Cancel analysis" }));
        await advance(0);
        expect(sessionStorage.getItem(storageKey)).toBeNull();
        await advance(30_000);
        expect(start).toHaveBeenCalledTimes(4);
      } finally {
        view.unmount();
        vi.useRealTimers();
      }
    },
  );

  it.each(["SCC", "cycle"] as const)(
    "keeps known %s reload recovery cancellable through transient failures",
    async (operation) => {
      vi.useFakeTimers();
      const mocks = mockRunningOperations();
      const failure = new topologyApi.ApiError(503, "service_unavailable", "Busy.", "1");
      mocks.sccPoll.mockRejectedValueOnce(failure).mockRejectedValueOnce(new TypeError("Offline"));
      mocks.cyclePoll
        .mockRejectedValueOnce(failure)
        .mockRejectedValueOnce(new TypeError("Offline"));
      const storageKey =
        operation === "SCC" ? ACTIVE_SCC_RUN_STORAGE_KEY : ACTIVE_CYCLE_RUN_STORAGE_KEY;
      const recovery = JSON.stringify({
        operation: operation === "SCC" ? "cyclic_scc" : "structural_cycles",
        source: topologyFixture.source,
        analysisId: topologyFixture.analysisId,
        snapshotRevision: topologyFixture.snapshotRevision,
        idempotencyKey: "original-key",
        runId: operation === "SCC" ? runningSccRunFixture.runId : runningCycleRunFixture.runId,
        maxCycles: 20,
      });
      sessionStorage.setItem(storageKey, recovery);
      const view = render(
        <TopologyExplorer access={accessFixture} topology={topologyFixture} resetGeneration={0} />,
      );
      try {
        await advance(0);
        expect(sessionStorage.getItem(storageKey)).toBe(recovery);
        expect(screen.getByRole("button", { name: "Cancel analysis" })).toBeEnabled();
        await advance(1000);
        expect(sessionStorage.getItem(storageKey)).toBe(recovery);
        await advance(2000);
        expect(sessionStorage.getItem(storageKey)).toBeNull();
        expect(mocks.sccStart).not.toHaveBeenCalled();
        expect(mocks.cycleStart).not.toHaveBeenCalled();
      } finally {
        view.unmount();
        vi.useRealTimers();
      }
    },
  );

  it.each(
    (["SCC", "cycle"] as const).flatMap((operation) =>
      [401, 404, 409].map((status) => ({ operation, status })),
    ),
  )(
    "clears $operation recovery for definitive status $status without retrying",
    async ({ operation, status }) => {
      vi.useFakeTimers();
      const mocks = mockRunningOperations();
      const failure = new topologyApi.ApiError(
        status,
        status === 409 ? "snapshot_revision_mismatch" : "not_found",
        "Unavailable.",
        null,
      );
      mocks.sccPoll.mockRejectedValue(failure);
      mocks.cyclePoll.mockRejectedValue(failure);
      const storageKey =
        operation === "SCC" ? ACTIVE_SCC_RUN_STORAGE_KEY : ACTIVE_CYCLE_RUN_STORAGE_KEY;
      const view = render(
        <TopologyExplorer access={accessFixture} topology={topologyFixture} resetGeneration={0} />,
      );
      try {
        fireEvent.click(
          screen.getByRole("button", {
            name: operation === "SCC" ? "Run cyclic components" : "Run structural cycles",
          }),
        );
        await advance(0);
        await advance(600);
        expect(sessionStorage.getItem(storageKey)).toBeNull();
        await advance(30_000);
        expect(operation === "SCC" ? mocks.sccPoll : mocks.cyclePoll).toHaveBeenCalledTimes(1);
      } finally {
        view.unmount();
        vi.useRealTimers();
      }
    },
  );

  it.each(["SCC", "cycle"] as const)(
    "stops %s backoff callbacks when authentication workspace replacement starts",
    async (operation) => {
      vi.useFakeTimers();
      const mocks = mockRunningOperations();
      mocks.sccStart.mockRejectedValue(new TypeError("Offline"));
      mocks.cycleStart.mockRejectedValue(new TypeError("Offline"));
      let current = true;
      const storageKey =
        operation === "SCC" ? ACTIVE_SCC_RUN_STORAGE_KEY : ACTIVE_CYCLE_RUN_STORAGE_KEY;
      const view = render(
        <TopologyExplorer
          access={accessFixture}
          topology={topologyFixture}
          resetGeneration={0}
          isWorkspaceCurrent={() => current}
        />,
      );
      try {
        fireEvent.click(
          screen.getByRole("button", {
            name: operation === "SCC" ? "Run cyclic components" : "Run structural cycles",
          }),
        );
        await advance(0);
        current = false;
        sessionStorage.setItem(storageKey, "replacement-workspace");
        await advance(30_000);
        expect(operation === "SCC" ? mocks.sccStart : mocks.cycleStart).toHaveBeenCalledTimes(1);
        expect(sessionStorage.getItem(storageKey)).toBe("replacement-workspace");
      } finally {
        view.unmount();
        vi.useRealTimers();
      }
    },
  );

  it.each(["SCC", "cycle"] as const)(
    "keeps exhausted %s status recovery cancellable without creating a new run",
    async (operation) => {
      vi.useFakeTimers();
      const mocks = mockRunningOperations();
      mocks.sccPoll.mockRejectedValue(new TypeError("Offline"));
      mocks.cyclePoll.mockRejectedValue(new TypeError("Offline"));
      const storageKey =
        operation === "SCC" ? ACTIVE_SCC_RUN_STORAGE_KEY : ACTIVE_CYCLE_RUN_STORAGE_KEY;
      const poll = operation === "SCC" ? mocks.sccPoll : mocks.cyclePoll;
      const view = render(
        <TopologyExplorer access={accessFixture} topology={topologyFixture} resetGeneration={0} />,
      );
      try {
        fireEvent.click(
          screen.getByRole("button", {
            name: operation === "SCC" ? "Run cyclic components" : "Run structural cycles",
          }),
        );
        await advance(0);
        const recovery = sessionStorage.getItem(storageKey);
        await advance(600);
        await advance(1000);
        await advance(2000);
        expect(poll).toHaveBeenCalledTimes(3);
        expect(sessionStorage.getItem(storageKey)).toBe(recovery);
        expect(screen.getByRole("button", { name: "Retry analysis recovery" })).toBeEnabled();
        await advance(30_000);
        expect(poll).toHaveBeenCalledTimes(3);
        fireEvent.click(screen.getByRole("button", { name: "Cancel analysis" }));
        await advance(0);
        expect(sessionStorage.getItem(storageKey)).toBeNull();
        expect(
          screen.queryByRole("button", { name: "Retry analysis recovery" }),
        ).not.toBeInTheDocument();
      } finally {
        view.unmount();
        vi.useRealTimers();
      }
    },
  );

  it.each(["SCC", "cycle"] as const)(
    "does not retry or automatically restart a terminal %s failure",
    async (operation) => {
      vi.useFakeTimers();
      const mocks = mockRunningOperations();
      mocks.sccStart.mockResolvedValue({
        ...runningSccRunFixture,
        status: "failed",
        error: { code: "worker_failed", message: "Analysis failed." },
      });
      mocks.cycleStart.mockResolvedValue({
        ...runningCycleRunFixture,
        status: "failed",
        error: { code: "worker_failed", message: "Analysis failed." },
      });
      const view = render(
        <TopologyExplorer access={accessFixture} topology={topologyFixture} resetGeneration={0} />,
      );
      try {
        const start = screen.getByRole("button", {
          name: operation === "SCC" ? "Run cyclic components" : "Run structural cycles",
        });
        fireEvent.click(start);
        await advance(30_000);
        expect(operation === "SCC" ? mocks.sccStart : mocks.cycleStart).toHaveBeenCalledTimes(1);
        expect(start).toBeEnabled();
        expect(
          screen.queryByRole("button", { name: "Retry analysis recovery" }),
        ).not.toBeInTheDocument();
      } finally {
        view.unmount();
        vi.useRealTimers();
      }
    },
  );

  const teardownCases = (["SCC", "cycle"] as const).flatMap((operation) =>
    (["start", "cancel"] as const).flatMap((action) =>
      (["running", "404"] as const).map((response) => ({ operation, action, response })),
    ),
  );

  it.each(teardownCases)(
    "ignores late $operation $action $response after teardown",
    async ({ operation, action, response }) => {
      const user = userEvent.setup();
      const pendingScc = deferred<SccRunResponse>();
      const pendingCycle = deferred<CycleRunResponse>();
      const onSnapshotUnavailable = vi.fn();
      const storageKey =
        operation === "SCC" ? ACTIVE_SCC_RUN_STORAGE_KEY : ACTIVE_CYCLE_RUN_STORAGE_KEY;
      if (operation === "SCC") {
        vi.spyOn(topologyApi, "startSccRun").mockReturnValue(
          action === "start" ? pendingScc.promise : Promise.resolve(runningSccRunFixture),
        );
        vi.spyOn(topologyApi, "cancelSccRun").mockReturnValue(pendingScc.promise);
      } else {
        vi.spyOn(topologyApi, "startCycleRun").mockReturnValue(
          action === "start" ? pendingCycle.promise : Promise.resolve(runningCycleRunFixture),
        );
        vi.spyOn(topologyApi, "cancelCycleRun").mockReturnValue(pendingCycle.promise);
      }
      const view = render(
        <TopologyExplorer
          access={accessFixture}
          topology={topologyFixture}
          resetGeneration={0}
          onSnapshotUnavailable={onSnapshotUnavailable}
        />,
      );
      await user.click(
        screen.getByRole("button", {
          name: operation === "SCC" ? "Run cyclic components" : "Run structural cycles",
        }),
      );
      if (action === "cancel")
        await user.click(await screen.findByRole("button", { name: "Cancel analysis" }));

      sessionStorage.clear();
      view.unmount();
      const replacementRecovery = response === "404" ? "new-workspace-recovery" : null;
      if (replacementRecovery) sessionStorage.setItem(storageKey, replacementRecovery);
      await act(async () => {
        if (response === "404") {
          const error = new topologyApi.ApiError(
            404,
            "not_found",
            "The requested resource was not found.",
            null,
          );
          if (operation === "SCC") pendingScc.reject(error);
          else pendingCycle.reject(error);
        } else if (operation === "SCC") pendingScc.resolve(runningSccRunFixture);
        else pendingCycle.resolve(runningCycleRunFixture);
        await (operation === "SCC" ? pendingScc.promise : pendingCycle.promise).catch(
          () => undefined,
        );
      });

      expect(sessionStorage.getItem(storageKey)).toBe(replacementRecovery);
      expect(onSnapshotUnavailable).not.toHaveBeenCalled();
    },
  );

  it("does not reconcile a stale cycle-detail failure after unmount", async () => {
    const user = userEvent.setup();
    const detail = deferred<CycleDetail>();
    const onSnapshotUnavailable = vi.fn();
    vi.spyOn(topologyApi, "startCycleRun").mockResolvedValue(succeededCycleRunFixture);
    vi.spyOn(topologyApi, "fetchCyclePage").mockResolvedValue(cyclePageFixture);
    vi.spyOn(topologyApi, "fetchCycleDetail").mockReturnValue(detail.promise);
    const view = render(
      <TopologyExplorer
        access={accessFixture}
        topology={topologyFixture}
        resetGeneration={0}
        onSnapshotUnavailable={onSnapshotUnavailable}
      />,
    );
    await user.click(screen.getByRole("button", { name: "Run structural cycles" }));
    await user.click(await screen.findByRole("button", { name: /Cycle 2/ }));
    view.unmount();

    await act(async () => {
      detail.reject(
        new topologyApi.ApiError(404, "not_found", "The requested resource was not found.", null),
      );
      await detail.promise.catch(() => undefined);
    });

    expect(onSnapshotUnavailable).not.toHaveBeenCalled();
  });

  it.each(["SCC", "cycle"])(
    "invalidates pending %s callbacks when workspace cleanup begins before unmount",
    async (operation) => {
      const user = userEvent.setup();
      const pendingScc = deferred<SccRunResponse>();
      const pendingCycle = deferred<CycleRunResponse>();
      vi.spyOn(topologyApi, "startSccRun").mockReturnValue(pendingScc.promise);
      vi.spyOn(topologyApi, "startCycleRun").mockReturnValue(pendingCycle.promise);
      let workspaceCurrent = true;
      render(
        <TopologyExplorer
          access={accessFixture}
          topology={topologyFixture}
          resetGeneration={0}
          isWorkspaceCurrent={() => workspaceCurrent}
        />,
      );
      await user.click(
        screen.getByRole("button", {
          name: operation === "SCC" ? "Run cyclic components" : "Run structural cycles",
        }),
      );
      workspaceCurrent = false;
      sessionStorage.clear();

      await act(async () => {
        if (operation === "SCC") pendingScc.resolve(runningSccRunFixture);
        else pendingCycle.resolve(runningCycleRunFixture);
        await (operation === "SCC" ? pendingScc.promise : pendingCycle.promise);
      });

      expect(sessionStorage.getItem(ACTIVE_SCC_RUN_STORAGE_KEY)).toBeNull();
      expect(sessionStorage.getItem(ACTIVE_CYCLE_RUN_STORAGE_KEY)).toBeNull();
      expect(screen.queryByRole("button", { name: "Cancel analysis" })).not.toBeInTheDocument();
    },
  );

  it("starts with default exchanges visible and applies the SDK-backed toggle", async () => {
    const user = userEvent.setup();
    render(
      <TopologyExplorer access={accessFixture} topology={topologyFixture} resetGeneration={0} />,
    );

    expect(screen.getByText(/Node IDs:/)).toHaveTextContent("n1.default-exchange");
    const toggle = screen.getByRole("checkbox", { name: "Hide default exchanges" });
    expect(toggle).not.toBeChecked();

    await user.click(toggle);

    expect(screen.getByText(/Node IDs:/)).not.toHaveTextContent("n1.default-exchange");
    expect(screen.getByText(/Node IDs:/)).toHaveTextContent("n1.orders-exchange");
  });

  it("shows an explicit empty state when no vhost is selected", async () => {
    const user = userEvent.setup();
    render(
      <TopologyExplorer access={accessFixture} topology={topologyFixture} resetGeneration={0} />,
    );

    await user.click(screen.getByRole("button", { name: "Select none" }));

    expect(
      screen.getByText("No vhosts selected. Select at least one vhost to render resources."),
    ).toBeVisible();
    expect(screen.queryByLabelText("Test topology canvas")).not.toBeInTheDocument();
  });

  it("removes hidden kinds and their incident relationships", async () => {
    const user = userEvent.setup();
    render(
      <TopologyExplorer access={accessFixture} topology={topologyFixture} resetGeneration={0} />,
    );

    await user.click(screen.getByRole("checkbox", { name: "Exchanges ◇" }));

    expect(screen.getByText(/Node IDs:/)).not.toHaveTextContent("n1.orders-exchange");
    expect(screen.getByText(/Node IDs:/)).not.toHaveTextContent("m1.missing-dlx");
    expect(screen.getByText(/Edge IDs:/)).toHaveTextContent("e1.2,e1.3,e1.4");
  });

  it("highlights a case-insensitive search without filtering membership", async () => {
    const user = userEvent.setup();
    render(
      <TopologyExplorer access={accessFixture} topology={topologyFixture} resetGeneration={0} />,
    );
    const originalNodeIds = screen.getByText(/Node IDs:/).textContent;

    await user.type(screen.getByRole("searchbox", { name: "Resource name" }), "ORDERS.PRO");

    expect(screen.getByText("1 visible match")).toBeVisible();
    expect(screen.getByText(/Search IDs:/)).toHaveTextContent("n1.orders-queue");
    expect(screen.getByText(/Node IDs:/).textContent).toBe(originalNodeIds);
  });

  it("clears hidden inspection without resurrecting it when the resource is revealed", async () => {
    const user = userEvent.setup();
    render(
      <TopologyExplorer access={accessFixture} topology={topologyFixture} resetGeneration={0} />,
    );
    await user.click(screen.getByRole("button", { name: "Inspect test queue" }));
    const details = within(screen.getByRole("region", { name: "Safe details" }));
    expect(
      details.queryByText("Select a resource or relationship to inspect it."),
    ).not.toBeInTheDocument();
    const queues = screen.getByRole("checkbox", { name: "Queues ▤" });
    await user.click(queues);
    expect(details.getByText("Select a resource or relationship to inspect it.")).toBeVisible();
    await user.click(queues);
    expect(details.getByText("Select a resource or relationship to inspect it.")).toBeVisible();
  });

  it("clears the preceding SCC focus atomically when a new run succeeds", async () => {
    const user = userEvent.setup();
    vi.spyOn(topologyApi, "startSccRun")
      .mockResolvedValueOnce(succeededSccRunFixture)
      .mockResolvedValueOnce({ ...succeededSccRunFixture, runId: "new-scc-run" });
    render(
      <TopologyExplorer access={accessFixture} topology={topologyFixture} resetGeneration={0} />,
    );
    const start = screen.getByRole("button", { name: "Run cyclic components" });
    await user.click(start);
    await user.click(await screen.findByRole("checkbox", { name: /Cyclic component 1/ }));
    expect(screen.getByRole("checkbox", { name: /Cyclic component 1/ })).toBeChecked();
    await user.click(start);
    await waitFor(() =>
      expect(screen.getByRole("checkbox", { name: /Cyclic component 1/ })).not.toBeChecked(),
    );
    expect(screen.getByText(/Node IDs:/)).toHaveTextContent("n1.default-exchange");
  });

  it("initializes the recovered cycle limit before the recovery request finishes", () => {
    const pending = deferred<CycleRunResponse>();
    vi.spyOn(topologyApi, "fetchCycleRun").mockReturnValue(pending.promise);
    sessionStorage.setItem(
      ACTIVE_CYCLE_RUN_STORAGE_KEY,
      JSON.stringify({
        operation: "structural_cycles",
        source: topologyFixture.source,
        analysisId: topologyFixture.analysisId,
        snapshotRevision: topologyFixture.snapshotRevision,
        maxCycles: 0,
        idempotencyKey: "recover-cycle",
        runId: runningCycleRunFixture.runId,
      }),
    );
    render(
      <TopologyExplorer access={accessFixture} topology={topologyFixture} resetGeneration={0} />,
    );
    expect(screen.getByRole("spinbutton", { name: "Maximum single-vhost cycles" })).toHaveValue(0);
    expect(screen.getByRole("spinbutton", { name: "Maximum single-vhost cycles" })).toBeDisabled();
    expect(screen.getByLabelText("Structural-cycle operation status")).toHaveTextContent(
      "Recovering active structural-cycle analysis…",
    );
  });

  it("derives loading for the latest cycle query and ignores an obsolete page response", async () => {
    const user = userEvent.setup();
    const obsolete = deferred<CyclePage>();
    const latest = deferred<CyclePage>();
    vi.spyOn(topologyApi, "startCycleRun").mockResolvedValue(succeededCycleRunFixture);
    vi.spyOn(topologyApi, "fetchCyclePage")
      .mockResolvedValueOnce(cyclePageFixture)
      .mockReturnValueOnce(obsolete.promise)
      .mockReturnValueOnce(latest.promise);
    render(
      <TopologyExplorer access={accessFixture} topology={topologyFixture} resetGeneration={0} />,
    );
    await user.click(screen.getByRole("button", { name: "Run structural cycles" }));
    await screen.findByRole("button", { name: /Cycle 2/ });
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Sort summaries" }),
      "length_desc",
    );
    await user.type(screen.getByRole("spinbutton", { name: "Minimum length" }), "2");
    expect(screen.getByLabelText("Cycle summary status")).toHaveTextContent(
      "Loading cycle summaries…",
    );
    await act(async () => obsolete.resolve({ ...cyclePageFixture, total: 99 }));
    expect(screen.getByLabelText("Cycle summary status")).toHaveTextContent(
      "Loading cycle summaries…",
    );
    await act(async () =>
      latest.resolve({ ...cyclePageFixture, total: 1, items: [cyclePageFixture.items[0]!] }),
    );
    expect(screen.getByLabelText("Cycle summary status")).toHaveTextContent(
      "1 cycle match the current filters.",
    );
    expect(screen.queryByRole("button", { name: /Cycle 2/ })).not.toBeInTheDocument();
  });

  it("keeps a newer completed cycle run when an old page expires in the same update", async () => {
    const user = userEvent.setup();
    const oldPage = deferred<CyclePage>();
    const newStart = deferred<CycleRunResponse>();
    const newerRun = { ...succeededCycleRunFixture, runId: "newer-cycle-run" };
    const newerPage = { ...cyclePageFixture, runId: newerRun.runId };
    const onSnapshotUnavailable = vi.fn();
    vi.spyOn(topologyApi, "startCycleRun")
      .mockResolvedValueOnce(succeededCycleRunFixture)
      .mockReturnValueOnce(newStart.promise);
    vi.spyOn(topologyApi, "fetchCyclePage")
      .mockResolvedValueOnce(cyclePageFixture)
      .mockReturnValueOnce(oldPage.promise)
      .mockResolvedValue(newerPage);
    render(
      <TopologyExplorer
        access={accessFixture}
        topology={topologyFixture}
        resetGeneration={0}
        onSnapshotUnavailable={onSnapshotUnavailable}
      />,
    );
    await user.click(screen.getByRole("button", { name: "Run structural cycles" }));
    await screen.findByRole("button", { name: /Cycle 2/ });
    await user.selectOptions(screen.getByRole("combobox", { name: "Sort summaries" }), "length_desc");
    await user.click(screen.getByRole("button", { name: "Run structural cycles" }));
    await act(async () => {
      newStart.resolve(newerRun);
      await newStart.promise;
      oldPage.reject(new topologyApi.ApiError(404, "not_found", "Not found.", null));
      await oldPage.promise.catch(() => undefined);
    });

    expect(await screen.findByRole("button", { name: /Cycle 2/ })).toBeVisible();
    expect(onSnapshotUnavailable).not.toHaveBeenCalled();
    expect(screen.getByLabelText("Structural-cycle operation status")).not.toHaveTextContent(
      "no longer available",
    );
  });

  it("runs SCC analysis and lets selected complete memberships override exploration hiding", async () => {
    const user = userEvent.setup();
    vi.spyOn(topologyApi, "startSccRun").mockResolvedValue(succeededSccRunFixture);
    render(
      <TopologyExplorer access={accessFixture} topology={topologyFixture} resetGeneration={0} />,
    );
    await user.click(screen.getByRole("checkbox", { name: "Exchanges ◇" }));
    expect(screen.getByText(/Node IDs:/)).not.toHaveTextContent("n1.orders-exchange");

    await user.click(screen.getByRole("button", { name: "Run cyclic components" }));
    const component = await screen.findByRole("checkbox", { name: /Cyclic component 1/ });
    expect(resultRegion("Cyclic components").getByText("2 cyclic components found.")).toBeVisible();
    await user.click(component);

    expect(screen.getByText(/Node IDs:/)).toHaveTextContent("n1.orders-exchange,n1.orders-queue");
    expect(screen.getByText(/Edge IDs:/)).toHaveTextContent("e1.0");
    await user.click(screen.getAllByText("Show complete membership")[0]!);
    expect(screen.getByText("exchange orders.events in /orders")).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Clear component focus" }));
    expect(screen.getByText(/Node IDs:/)).not.toHaveTextContent("n1.orders-exchange");
    expect(screen.getByText(/Node IDs:/)).toHaveTextContent("n1.orders-queue");
  });

  it("reconciles a missing operation snapshot without discarding the rendered graph first", async () => {
    const user = userEvent.setup();
    const onSnapshotUnavailable = vi.fn();
    vi.spyOn(topologyApi, "startSccRun").mockRejectedValue(
      new topologyApi.ApiError(404, "not_found", "The requested resource was not found.", null),
    );
    render(
      <TopologyExplorer
        access={accessFixture}
        topology={topologyFixture}
        resetGeneration={0}
        onSnapshotUnavailable={onSnapshotUnavailable}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Run cyclic components" }));

    await waitFor(() => expect(onSnapshotUnavailable).toHaveBeenCalledOnce());
    expect(screen.getByLabelText("Test topology canvas")).toBeVisible();
    expect(
      screen.getByRole("status", { name: "Cyclic-component operation status" }),
    ).toHaveTextContent("no longer available");
  });

  it("restores an active run from session storage without starting duplicate work", async () => {
    const fetchRun = vi.spyOn(topologyApi, "fetchSccRun").mockResolvedValue(succeededSccRunFixture);
    const startRun = vi.spyOn(topologyApi, "startSccRun");
    sessionStorage.setItem(
      ACTIVE_SCC_RUN_STORAGE_KEY,
      JSON.stringify({
        operation: "cyclic_scc",
        source: topologyFixture.source,
        analysisId: topologyFixture.analysisId,
        snapshotRevision: topologyFixture.snapshotRevision,
        idempotencyKey: "20000000-0000-4000-8000-000000000002",
        runId: succeededSccRunFixture.runId,
      }),
    );

    render(
      <TopologyExplorer access={accessFixture} topology={topologyFixture} resetGeneration={0} />,
    );

    expect(
      await resultRegion("Cyclic components").findByText("2 cyclic components found."),
    ).toBeVisible();
    expect(fetchRun).toHaveBeenCalledWith(
      succeededSccRunFixture.runId,
      topologyFixture.snapshotRevision,
    );
    expect(startRun).not.toHaveBeenCalled();
    expect(sessionStorage.getItem(ACTIVE_SCC_RUN_STORAGE_KEY)).toBeNull();
  });

  it("shows a cancel action while a run is active", async () => {
    const user = userEvent.setup();
    vi.spyOn(topologyApi, "startSccRun").mockResolvedValue(runningSccRunFixture);
    vi.spyOn(topologyApi, "cancelSccRun").mockResolvedValue({
      ...runningSccRunFixture,
      status: "cancelled",
      finishedAt: "2026-08-11T08:00:01Z",
      heartbeatExpiresAt: null,
      resultExpiresAt: "2026-08-11T12:00:01Z",
    });
    render(
      <TopologyExplorer access={accessFixture} topology={topologyFixture} resetGeneration={0} />,
    );

    await user.click(screen.getByRole("button", { name: "Run cyclic components" }));
    await user.click(await screen.findByRole("button", { name: "Cancel analysis" }));

    await waitFor(() => {
      expect(
        resultRegion("Cyclic components").getByText("Cyclic-component analysis cancelled."),
      ).toBeVisible();
    });
    expect(screen.queryByRole("button", { name: "Cancel analysis" })).not.toBeInTheDocument();
  });

  it("warns before unbounded cycle enumeration and rejects invalid integer input", async () => {
    const user = userEvent.setup();
    render(
      <TopologyExplorer access={accessFixture} topology={topologyFixture} resetGeneration={0} />,
    );
    const maxCycles = screen.getByRole("spinbutton", { name: "Maximum single-vhost cycles" });

    await user.clear(maxCycles);
    await user.type(maxCycles, "0");

    expect(
      screen.getByText(/Zero enumerates all cycles and may require substantial time or memory/),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "Run structural cycles" })).toBeEnabled();

    fireEvent.change(maxCycles, { target: { value: "00" } });

    expect(
      screen.getByText(/Zero enumerates all cycles and may require substantial time or memory/),
    ).toBeVisible();

    await user.clear(maxCycles);

    expect(screen.getByText("Maximum cycles must be a non-negative integer.")).toBeVisible();
    expect(screen.getByRole("button", { name: "Run structural cycles" })).toHaveAttribute("aria-disabled", "true");
  });

  it("pages and filters cycle summaries without starting new work, then loads exact detail", async () => {
    const user = userEvent.setup();
    const startRun = vi
      .spyOn(topologyApi, "startCycleRun")
      .mockResolvedValue({
        ...succeededCycleRunFixture,
        result: { cycleCount: 2, truncated: true, maxCycles: 20, crossVhostCount: 1 },
      });
    const fetchPage = vi.spyOn(topologyApi, "fetchCyclePage").mockResolvedValue(cyclePageFixture);
    const fetchDetail = vi
      .spyOn(topologyApi, "fetchCycleDetail")
      .mockResolvedValue(cycleDetailFixture);
    render(
      <TopologyExplorer access={accessFixture} topology={topologyFixture} resetGeneration={0} />,
    );

    await user.click(screen.getByRole("button", { name: "Run structural cycles" }));

    expect(
      await resultRegion("Structural cycles").findByText(/2 structural cycles found/),
    ).toHaveTextContent("1 cycle spans multiple vhosts. Single-vhost results were limited to 20.");
    expect(screen.queryByRole("group", { name: "Touches vhost" })).not.toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Sort summaries" })).toHaveValue("sdk");
    expect(startRun).toHaveBeenCalledWith(
      topologyFixture.source,
      topologyFixture.analysisId,
      topologyFixture.snapshotRevision,
      20,
      expect.any(String),
      true,
    );
    expect(await screen.findByRole("button", { name: /Cycle 2/ })).toBeVisible();

    await user.selectOptions(
      screen.getByRole("combobox", { name: "Sort summaries" }),
      "length_desc",
    );
    await user.type(screen.getByRole("spinbutton", { name: "Minimum length" }), "2");

    await waitFor(() => {
      expect(fetchPage).toHaveBeenLastCalledWith(
        succeededCycleRunFixture.runId,
        topologyFixture.snapshotRevision,
        {
          offset: 0,
          limit: 50,
          sort: "length_desc",
          minLength: 2,
          maxLength: undefined,
        },
      );
    });
    expect(startRun).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole("button", { name: /Cycle 2/ }));

    expect(await screen.findByRole("article", { name: "Cycle 2 path" })).toHaveTextContent(
      "queue Orders.Process in /orders",
    );
    expect(screen.getByRole("article", { name: "Cycle 2 path" })).toHaveTextContent(
      "shovel orders-to-audit in /orders",
    );
    expect(fetchDetail).toHaveBeenCalledWith(
      succeededCycleRunFixture.runId,
      1,
      topologyFixture.snapshotRevision,
    );
  });

  it("moves through cycle summary pages with Next and Previous without restarting SDK work", async () => {
    const user = userEvent.setup();
    const startRun = vi
      .spyOn(topologyApi, "startCycleRun")
      .mockResolvedValue(succeededCycleRunFixture);
    const fetchPage = vi
      .spyOn(topologyApi, "fetchCyclePage")
      .mockImplementation(async (runId, snapshotRevision, options = {}) => ({
        ...cyclePageFixture,
        runId,
        snapshotRevision,
        offset: options.offset ?? 0,
        total: 101,
        items: [cyclePageFixture.items[0]!],
      }));
    render(
      <TopologyExplorer access={accessFixture} topology={topologyFixture} resetGeneration={0} />,
    );

    await user.click(screen.getByRole("button", { name: "Run structural cycles" }));
    const pagination = await screen.findByLabelText("Cycle summary pages");
    const next = within(pagination).getByRole("button", { name: "Next" });
    const previous = within(pagination).getByRole("button", { name: "Previous" });

    expect(previous).toBeDisabled();
    await user.click(next);

    await waitFor(() => {
      expect(fetchPage).toHaveBeenLastCalledWith(
        succeededCycleRunFixture.runId,
        topologyFixture.snapshotRevision,
        expect.objectContaining({ offset: 50, limit: 50 }),
      );
    });
    expect(within(pagination).getByText("51–51 of 101")).toBeVisible();

    await user.click(previous);

    await waitFor(() => {
      expect(fetchPage).toHaveBeenLastCalledWith(
        succeededCycleRunFixture.runId,
        topologyFixture.snapshotRevision,
        expect.objectContaining({ offset: 0, limit: 50 }),
      );
    });
    expect(startRun).toHaveBeenCalledTimes(1);
  });

  it("renders exact ordered edge hops and excludes an unselected same-endpoint parallel edge", async () => {
    const user = userEvent.setup();
    const parallelEdge = {
      ...topologyFixture.edges[2],
      id: "e1.parallel-shovel",
    } as const;
    const topologyWithParallelEdge = {
      ...topologyFixture,
      cluster: {
        ...topologyFixture.cluster,
        edgeCount: topologyFixture.cluster.edgeCount + 1,
      },
      edges: [...topologyFixture.edges, parallelEdge],
    } as const satisfies TopologyResponse;
    vi.spyOn(topologyApi, "startCycleRun").mockResolvedValue(succeededCycleRunFixture);
    vi.spyOn(topologyApi, "fetchCyclePage").mockResolvedValue(cyclePageFixture);
    vi.spyOn(topologyApi, "fetchCycleDetail").mockResolvedValue(cycleDetailFixture);
    render(
      <TopologyExplorer
        access={accessFixture}
        topology={topologyWithParallelEdge}
        resetGeneration={0}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Run structural cycles" }));
    await user.click(await screen.findByRole("button", { name: /Cycle 2/ }));

    const detail = await screen.findByRole("article", { name: "Cycle 2 path" });
    expect(detail).toHaveTextContent("Hop 1:");
    expect(detail).toHaveTextContent("shovel edge e1.2");
    expect(detail).toHaveTextContent("dead letter edge e1.4");
    expect(detail).toHaveTextContent("closes the cycle");
    expect(detail).not.toHaveTextContent(parallelEdge.id);
    expect(screen.getByText(/Edge IDs:/)).toHaveTextContent("e1.2,e1.3,e1.4");
    expect(screen.getByText(/Edge IDs:/)).not.toHaveTextContent(parallelEdge.id);
  });

  it("ignores a late detail from an older completed run after a newer run succeeds", async () => {
    const user = userEvent.setup();
    const lateDetail = deferred<CycleDetail>();
    const newerRun = {
      ...succeededCycleRunFixture,
      runId: "60000000-0000-4000-8000-000000000006",
      result: { cycleCount: 1, truncated: false, maxCycles: 20 },
    } as const satisfies CycleRunResponse;
    const newerPage = {
      ...cyclePageFixture,
      runId: newerRun.runId,
      total: 1,
      truncated: false,
      items: [cyclePageFixture.items[0]!],
    } as const satisfies CyclePage;
    vi.spyOn(topologyApi, "startCycleRun")
      .mockResolvedValueOnce(succeededCycleRunFixture)
      .mockResolvedValueOnce(newerRun);
    const fetchPage = vi
      .spyOn(topologyApi, "fetchCyclePage")
      .mockImplementation(async (runId) =>
        runId === newerRun.runId ? newerPage : cyclePageFixture,
      );
    vi.spyOn(topologyApi, "fetchCycleDetail").mockReturnValue(lateDetail.promise);
    render(
      <TopologyExplorer access={accessFixture} topology={topologyFixture} resetGeneration={0} />,
    );

    await user.click(screen.getByRole("button", { name: "Run structural cycles" }));
    await user.click(await screen.findByRole("button", { name: /Cycle 2/ }));
    expect(screen.getByRole("status", { name: "Cycle detail status" })).toHaveTextContent(
      "Loading exact cycle path",
    );

    await user.click(screen.getByRole("button", { name: "Run structural cycles" }));
    await waitFor(() => {
      expect(fetchPage).toHaveBeenCalledWith(
        newerRun.runId,
        topologyFixture.snapshotRevision,
        expect.any(Object),
      );
    });

    await act(async () => {
      lateDetail.resolve(cycleDetailFixture);
    });

    expect(screen.queryByRole("article", { name: "Cycle 2 path" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Clear cycle focus" })).not.toBeInTheDocument();
    expect(screen.getByText(/Edge IDs:/)).toHaveTextContent("e1.0,e1.1,e1.2,e1.3,e1.4");
    expect(screen.getByRole("status", { name: "Cycle detail status" })).toBeEmptyDOMElement();
  });

  it("keeps cycle completion and truncation announcements mounted while the drawer is collapsed", async () => {
    const user = userEvent.setup();
    vi.spyOn(topologyApi, "startCycleRun").mockResolvedValue(succeededCycleRunFixture);
    vi.spyOn(topologyApi, "fetchCyclePage").mockResolvedValue(cyclePageFixture);
    render(
      <TopologyExplorer access={accessFixture} topology={topologyFixture} resetGeneration={0} />,
    );

    await user.click(screen.getByRole("button", { name: "Results & inspection" }));
    expect(screen.queryByRole("region", { name: "Structural cycles" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Run structural cycles" }));

    const status = screen.getByRole("status", { name: "Structural-cycle operation status" });
    await waitFor(() => {
      expect(status).toHaveTextContent("2 structural cycles found");
      expect(status).toHaveTextContent("Results were truncated at maxCycles=2");
    });
    expect(
      screen.getAllByRole("status", { name: "Structural-cycle operation status" }),
    ).toHaveLength(1);
    expect(screen.queryByRole("region", { name: "Structural cycles" })).not.toBeInTheDocument();
  });

  it("restores an active cycle run and its maxCycles without duplicate work", async () => {
    const fetchRun = vi
      .spyOn(topologyApi, "fetchCycleRun")
      .mockResolvedValue(succeededCycleRunFixture);
    const startRun = vi.spyOn(topologyApi, "startCycleRun");
    vi.spyOn(topologyApi, "fetchCyclePage").mockResolvedValue(cyclePageFixture);
    sessionStorage.setItem(
      ACTIVE_CYCLE_RUN_STORAGE_KEY,
      JSON.stringify({
        operation: "structural_cycles",
        source: topologyFixture.source,
        analysisId: topologyFixture.analysisId,
        snapshotRevision: topologyFixture.snapshotRevision,
        maxCycles: 2,
        idempotencyKey: "40000000-0000-4000-8000-000000000004",
        runId: succeededCycleRunFixture.runId,
      }),
    );

    render(
      <TopologyExplorer access={accessFixture} topology={topologyFixture} resetGeneration={0} />,
    );

    expect(
      await resultRegion("Structural cycles").findByText(/2 structural cycles found/),
    ).toBeVisible();
    expect(screen.getByRole("spinbutton", { name: "Maximum single-vhost cycles" })).toHaveValue(2);
    expect(fetchRun).toHaveBeenCalledWith(
      succeededCycleRunFixture.runId,
      topologyFixture.snapshotRevision,
    );
    expect(startRun).not.toHaveBeenCalled();
    expect(sessionStorage.getItem(ACTIVE_CYCLE_RUN_STORAGE_KEY)).toBeNull();
  });

  it("cancels an active structural-cycle worker through the shared operation control", async () => {
    const user = userEvent.setup();
    vi.spyOn(topologyApi, "startCycleRun").mockResolvedValue(runningCycleRunFixture);
    const cancelRun = vi.spyOn(topologyApi, "cancelCycleRun").mockResolvedValue({
      ...runningCycleRunFixture,
      status: "cancelled",
      finishedAt: "2026-08-11T09:00:01Z",
      heartbeatExpiresAt: null,
      resultExpiresAt: "2026-08-11T13:00:01Z",
    });
    render(
      <TopologyExplorer access={accessFixture} topology={topologyFixture} resetGeneration={0} />,
    );

    await user.click(screen.getByRole("button", { name: "Run structural cycles" }));
    expect(screen.getByRole("button", { name: "Run cyclic components" })).toHaveAttribute("aria-disabled", "true");
    await user.click(await screen.findByRole("button", { name: "Cancel analysis" }));

    expect(
      await resultRegion("Structural cycles").findByText("Structural-cycle analysis cancelled."),
    ).toBeVisible();
    expect(cancelRun).toHaveBeenCalledWith(
      runningCycleRunFixture.runId,
      topologyFixture.snapshotRevision,
    );
  });

  it("reopens inspection for an explicit selection from a collapsed result tab", async () => {
    const user = userEvent.setup();
    render(<TopologyExplorer access={accessFixture} topology={topologyFixture} resetGeneration={0} />);
    await user.click(screen.getByRole("tab", { name: "Cycles" }));
    await user.click(screen.getByRole("button", { name: "Results & inspection" }));
    await user.click(screen.getByRole("button", { name: "Inspect test queue" }));
    expect(screen.getByRole("button", { name: "Results & inspection" })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("tab", { name: "Inspection" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("region", { name: "Safe details" })).toHaveTextContent("Orders.Process");
    expect(screen.getByRole("button", { name: "Details: queue Orders.Process, vhost /orders" })).toBeVisible();
  });

  it("disambiguates search choices by wire ID and clears a choice removed by visibility", async () => {
    const user = userEvent.setup();
    const original = topologyFixture.nodes.find((node) => node.id === "n1.orders-queue")!;
    const duplicate = { ...original, id: "n1.other-queue", data: { ...original.data, vhost: "/other" } };
    const topology = { ...topologyFixture, nodes: [...topologyFixture.nodes, duplicate] };
    const fetchTopology = vi.spyOn(topologyApi, "fetchTopology");
    render(<TopologyExplorer access={accessFixture} topology={topology} resetGeneration={0} />);
    const search = screen.getByRole("searchbox", { name: "Resource name" });
    await user.type(search, "ORDERS.PROCESS");
    expect(screen.getByText("2 visible matches")).toBeVisible();
    await user.tab();
    const choices = screen.getByRole("group", { name: "Visible resource matches" });
    expect(within(choices).getByRole("button", { name: "queue Orders.Process, vhost /orders" })).toHaveFocus();
    await user.keyboard("{End}{Enter}");
    let details = within(screen.getByRole("region", { name: "Safe details" }));
    expect(details.getByText("/other", { exact: true })).toBeVisible();
    expect(details.getByText(duplicate.id, { exact: true })).toBeVisible();
    await user.keyboard("{Home}{Enter}");
    details = within(screen.getByRole("region", { name: "Safe details" }));
    expect(details.getByText("/orders", { exact: true })).toBeVisible();
    await user.click(within(screen.getByRole("group", { name: "Vhost scope" })).getByRole("checkbox", { name: "/orders" }));
    expect(within(choices).queryByRole("button", { pressed: true })).not.toBeInTheDocument();
    expect(screen.getByText("1 visible match")).toBeVisible();
    expect(screen.getByRole("region", { name: "Safe details" })).toHaveTextContent("Select a resource");
    await user.clear(search);
    await user.type(search, "no-such-resource");
    expect(screen.getByText("0 visible matches")).toBeVisible();
    expect(screen.queryByRole("group", { name: "Visible resource matches" })).not.toBeInTheDocument();
    expect(screen.getByText(/Node IDs:/)).toHaveTextContent(duplicate.id);
    expect(fetchTopology).not.toHaveBeenCalled();
  });

  it("explains complete component focus after selecting no vhosts and clears it from the graph", async () => {
    const user = userEvent.setup();
    vi.spyOn(topologyApi, "startSccRun").mockResolvedValue(succeededSccRunFixture);
    const fetchTopology = vi.spyOn(topologyApi, "fetchTopology");
    render(<TopologyExplorer access={accessFixture} topology={topologyFixture} resetGeneration={0} />);
    await user.click(screen.getByRole("button", { name: "Select none" }));
    await user.click(screen.getByRole("button", { name: "Run cyclic components" }));
    await user.click(await screen.findByRole("checkbox", { name: /Cyclic component 1/ }));
    await user.click(screen.getByRole("button", { name: "Results & inspection" }));
    const mode = screen.getByRole("region", { name: "Graph presentation" });
    expect(mode).toHaveTextContent("Component focus: Cyclic component 1");
    expect(mode).toHaveTextContent("overrides vhost scope: 2 resources hidden by exploration settings");
    expect(screen.getByText(/Both operations analyze the entire capture/)).toBeVisible();
    await user.click(within(mode).getByRole("button", { name: "Clear component filter" }));
    expect(mode).toHaveTextContent("Exploration view");
    expect(screen.getByText(/No vhosts selected/)).toBeVisible();
    expect(fetchTopology).not.toHaveBeenCalled();
  });

  it("composes exact cycle focus over retained SCC and exploration state without refetching", async () => {
    const user = userEvent.setup();
    vi.spyOn(topologyApi, "startSccRun").mockResolvedValue(succeededSccRunFixture);
    vi.spyOn(topologyApi, "startCycleRun").mockResolvedValue(succeededCycleRunFixture);
    vi.spyOn(topologyApi, "fetchCyclePage").mockResolvedValue(cyclePageFixture);
    vi.spyOn(topologyApi, "fetchCycleDetail").mockResolvedValue(cycleDetailFixture);
    const fetchTopology = vi.spyOn(topologyApi, "fetchTopology");
    render(
      <TopologyExplorer access={accessFixture} topology={topologyFixture} resetGeneration={0} />,
    );

    const explorationVhosts = screen.getByRole("group", { name: "Vhost scope" });
    await user.click(within(explorationVhosts).getByRole("checkbox", { name: "/audit" }));
    await user.click(screen.getByRole("checkbox", { name: "Shovels ⇢" }));
    expect(screen.getByText(/Node IDs:/)).not.toHaveTextContent("n1.audit-queue");
    expect(screen.getByText(/Node IDs:/)).not.toHaveTextContent("n1.cross-vhost-shovel");

    await user.click(screen.getByRole("button", { name: "Run cyclic components" }));
    const firstComponent = await screen.findByRole("checkbox", { name: /Cyclic component 1/ });
    await user.click(firstComponent);
    expect(screen.getByText(/Node IDs:/)).toHaveTextContent("n1.orders-exchange,n1.orders-queue");

    await user.click(screen.getByRole("button", { name: "Run structural cycles" }));
    await user.click(await screen.findByRole("button", { name: /Cycle 2/ }));

    expect(await screen.findByRole("button", { name: "Clear cycle focus" })).toBeVisible();
    expect(screen.getByText(/Node IDs:/)).toHaveTextContent(
      "n1.orders-queue,n1.audit-queue,n1.cross-vhost-shovel",
    );
    expect(screen.getByText(/Edge IDs:/)).toHaveTextContent("e1.2,e1.3,e1.4");
    expect(screen.getByText(/Cycle node steps:/)).toHaveTextContent(
      "n1.orders-queue:P1,n1.cross-vhost-shovel:P2,n1.audit-queue:P3",
    );
    expect(screen.getByText(/Cycle edge steps:/)).toHaveTextContent("e1.2:P1,e1.3:P2,e1.4:P3");
    expect(screen.getByRole("region", { name: "Graph presentation" })).toHaveTextContent("Cycle 2 focus");
    expect(screen.getByRole("region", { name: "Graph presentation" })).toHaveTextContent("overrides vhost scope, resource visibility");
    expect(resultRegion("Structural cycles").getByText(/2 structural cycles found/)).toBeVisible();
    await user.click(screen.getByRole("tab", { name: "Components" }));
    expect(resultRegion("Cyclic components").getByText("2 cyclic components found.")).toBeVisible();
    await user.click(screen.getByRole("tab", { name: "Cycles" }));

    await user.click(screen.getByRole("button", { name: "Clear cycle filter" }));

    expect(screen.getByText(/Node IDs:/)).toHaveTextContent("n1.orders-exchange,n1.orders-queue");
    expect(screen.getByText(/Cycle node steps:/)).not.toHaveTextContent("P1");
    expect(screen.getByRole("region", { name: "Graph presentation" })).toHaveTextContent("Component focus");
    await user.click(screen.getByRole("tab", { name: "Components" }));
    await user.click(firstComponent);
    expect(screen.getByText(/Node IDs:/)).not.toHaveTextContent("n1.audit-queue");
    expect(screen.getByText(/Node IDs:/)).not.toHaveTextContent("n1.cross-vhost-shovel");
    expect(fetchTopology).not.toHaveBeenCalled();
  });
});
