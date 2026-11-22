import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import App from "./App";
import type { GraphSelection } from "./components/DetailsPanel";
import type { VisibleTopology } from "./graph/presentation";
import { ACTIVE_CYCLE_RUN_STORAGE_KEY } from "./operations/cycles";
import { ACTIVE_SCC_RUN_STORAGE_KEY } from "./operations/scc";
import {
  accessFixture,
  catalogueFixture,
  cycleDetailFixture,
  cyclePageFixture,
  installApiFixtures,
  basicAuthSessionFixture,
  runningCycleRunFixture,
  runningSccRunFixture,
  succeededCycleRunFixture,
  succeededSccRunFixture,
  topologyFixture,
  type ApiFixture,
} from "./test/api-fixtures";

vi.mock("./graph/TopologyCanvas", () => ({
  TopologyCanvas: ({
    topology,
    onSelectionChange,
  }: {
    topology: VisibleTopology;
    onSelectionChange: (selection: GraphSelection) => void;
  }) => (
    <div aria-label="RabbitMQ topology graph">
      Node IDs: {topology.nodes.map((node) => node.id).join(",")}
      {topology.nodes[0] && (
        <button
          type="button"
          onClick={() =>
            onSelectionChange({
              kind: "node",
              id: topology.nodes[0]!.id,
            })
          }
        >
          Inspect first resource
        </button>
      )}
    </div>
  ),
}));

const topologyUrl = "/api/topology?source=static&analysis_id=orders-production";
const revisionQuery = new URLSearchParams({ snapshot_revision: topologyFixture.snapshotRevision });
const expiredRun = succeededCycleRunFixture;
const freshRun = { ...expiredRun, runId: "50000000-0000-4000-8000-000000000005" };
const notFound: ApiFixture = {
  status: 404,
  body: {
    error: { code: "not_found", message: "The requested resource was not found.", issues: [] },
  },
};

function pageUrl(runId: string): string {
  return `/api/cycle-runs/${runId}/cycles?${revisionQuery}&offset=0&limit=50&sort=sdk`;
}

function detailUrl(index: number): string {
  return `/api/cycle-runs/${expiredRun.runId}/cycles/${index}?${revisionQuery}`;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((complete) => {
    resolve = complete;
  });
  return { promise, resolve };
}

function boundUnavailableReads(fetchMock: ReturnType<typeof installApiFixtures>, url: string) {
  const respond = fetchMock.getMockImplementation()!;
  let reads = 0;
  fetchMock.mockImplementation((input, init) => {
    if (input === url) {
      reads += 1;
      // Keep a broken implementation's retry loop bounded during this test.
      if (reads > 2) return new Promise<Response>(() => {});
    }
    return respond(input, init);
  });
  return () => reads;
}

function installWorkspace() {
  const routes: Record<string, ApiFixture> = {
    "/api/access": { body: accessFixture },
    "/api/analyses": { body: catalogueFixture },
    [topologyUrl]: { body: topologyFixture },
    "/api/scc-runs": { body: succeededSccRunFixture },
    "/api/cycle-runs": { body: expiredRun },
    [pageUrl(expiredRun.runId)]: notFound,
    [pageUrl(freshRun.runId)]: { body: { ...cyclePageFixture, runId: freshRun.runId } },
  };
  const fetchMock = installApiFixtures(routes);
  return { routes, fetchMock };
}

function addOtherAnalysis(routes: Record<string, ApiFixture>) {
  const other = {
    ...catalogueFixture.static[0],
    analysisId: "other-analysis",
    label: "Other capture",
  };
  routes["/api/analyses"] = { body: { static: [...catalogueFixture.static, other], session: [] } };
  routes["/api/topology?source=static&analysis_id=other-analysis"] = {
    body: { ...topologyFixture, analysisId: other.analysisId },
  };
  return other;
}

function enableBasicSession(routes: Record<string, ApiFixture>) {
  routes["/api/auth/session"] = { body: basicAuthSessionFixture };
  routes["/api/access"] = { body: { ...accessFixture, authMode: "basic" } };
  routes["/api/auth/logout"] = { body: null, status: 204 };
}

describe("expired operation results in App", () => {
  it("settles a cycle-page 404 with an unchanged catalogue and permits a fresh run", async () => {
    const user = userEvent.setup();
    const { routes, fetchMock } = installWorkspace();
    const expiredPageReads = boundUnavailableReads(fetchMock, pageUrl(expiredRun.runId));
    render(<App />);
    await screen.findByLabelText("RabbitMQ topology graph");
    await user.click(screen.getByRole("button", { name: "Run cyclic components" }));
    const component = await screen.findByRole("checkbox", { name: /Cyclic component 1/ });
    await user.click(component);
    await user.click(screen.getByRole("button", { name: "Run structural cycles" }));
    await waitFor(() =>
      expect(fetchMock.mock.calls.filter(([url]) => url === "/api/analyses")).toHaveLength(2),
    );
    await act(async () => {});

    expect(expiredPageReads()).toBe(1);
    expect(screen.getByLabelText("Structural-cycle operation status")).toHaveTextContent(
      "no longer available",
    );
    expect(component).toBeChecked();
    expect(screen.getByLabelText("RabbitMQ topology graph")).toHaveTextContent(
      "n1.orders-exchange,n1.orders-queue",
    );
    expect(screen.queryByRole("button", { name: /Cycle 2/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Sort summaries" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run structural cycles" })).toBeEnabled();
    await user.click(screen.getByRole("checkbox", { name: "Hide default exchanges" }));
    expect(expiredPageReads()).toBe(1);
    expect(fetchMock.mock.calls.filter(([url]) => url === topologyUrl)).toHaveLength(1);

    routes["/api/cycle-runs"] = { body: freshRun };
    await user.click(screen.getByRole("button", { name: "Run structural cycles" }));
    expect(
      await within(screen.getByRole("region", { name: "Structural cycles" })).findByRole("button", {
        name: /Cycle 2/,
      }),
    ).toBeVisible();
    expect(expiredPageReads()).toBe(1);
    expect(fetchMock.mock.calls.filter(([url]) => url === "/api/analyses")).toHaveLength(2);
    expect(fetchMock.mock.calls.filter(([url]) => url === topologyUrl)).toHaveLength(1);
  });

  it("clears expired detail, focus, selection, and matching recovery while restoring SCC focus", async () => {
    const user = userEvent.setup();
    const { routes, fetchMock } = installWorkspace();
    routes[pageUrl(expiredRun.runId)] = { body: cyclePageFixture };
    routes[detailUrl(1)] = { body: cycleDetailFixture };
    routes[detailUrl(0)] = notFound;
    render(<App />);
    await screen.findByLabelText("RabbitMQ topology graph");
    await user.click(screen.getByRole("button", { name: "Run cyclic components" }));
    const component = await screen.findByRole("checkbox", { name: /Cyclic component 1/ });
    await user.click(component);
    await user.click(screen.getByRole("button", { name: "Run structural cycles" }));
    await user.click(await screen.findByRole("button", { name: /Cycle 2/ }));
    await screen.findByRole("article", { name: "Cycle 2 path" });
    await user.click(screen.getByRole("button", { name: "Inspect first resource" }));
    expect(
      screen.queryByText("Select a resource or relationship to inspect it."),
    ).not.toBeInTheDocument();
    sessionStorage.setItem(
      ACTIVE_CYCLE_RUN_STORAGE_KEY,
      JSON.stringify({
        ...expiredRun,
        maxCycles: 2,
        idempotencyKey: "expired-cycle",
      }),
    );

    await user.click(screen.getByRole("tab", { name: "Cycles" }));
    await user.click(screen.getByRole("button", { name: /Cycle 1/ }));
    await waitFor(() =>
      expect(screen.getByLabelText("Structural-cycle operation status")).toHaveTextContent(
        "no longer available",
      ),
    );
    await act(async () => {});

    expect(screen.queryByRole("article", { name: "Cycle 2 path" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Clear cycle filter" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Cycle 1/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "Inspection" }));
    expect(screen.getByText("Select a resource or relationship to inspect it.")).toBeVisible();
    expect(component).toBeChecked();
    expect(screen.getByLabelText("RabbitMQ topology graph")).toHaveTextContent(
      "n1.orders-exchange,n1.orders-queue",
    );
    expect(sessionStorage.getItem(ACTIVE_CYCLE_RUN_STORAGE_KEY)).toBeNull();
    expect(fetchMock.mock.calls.filter(([url]) => url === detailUrl(0))).toHaveLength(1);
    expect(fetchMock.mock.calls.filter(([url]) => url === "/api/analyses")).toHaveLength(2);
    expect(fetchMock.mock.calls.filter(([url]) => url === topologyUrl)).toHaveLength(1);
  });

  it.each(["recover", "recover-result", "poll", "cancel"] as const)(
    "settles a cycle-run 404 during %s without retrying the old run",
    async (phase) => {
      const user = userEvent.setup();
      const { routes, fetchMock } = installWorkspace();
      const runUrl = `/api/cycle-runs/${expiredRun.runId}?${revisionQuery}`;
      routes[runUrl] = phase === "recover-result" ? { body: expiredRun } : notFound;
      routes["/api/cycle-runs"] = { body: runningCycleRunFixture, status: 202 };
      const reads = boundUnavailableReads(fetchMock, runUrl);
      const recovering = phase === "recover" || phase === "recover-result";
      if (recovering)
        sessionStorage.setItem(
          ACTIVE_CYCLE_RUN_STORAGE_KEY,
          JSON.stringify({
            operation: "structural_cycles",
            source: expiredRun.source,
            analysisId: expiredRun.analysisId,
            snapshotRevision: expiredRun.snapshotRevision,
            maxCycles: 20,
            idempotencyKey: "expired-cycle",
            runId: expiredRun.runId,
          }),
        );
      render(<App />);
      await screen.findByLabelText("RabbitMQ topology graph");
      if (!recovering)
        await user.click(screen.getByRole("button", { name: "Run structural cycles" }));
      if (phase === "cancel")
        await user.click(await screen.findByRole("button", { name: "Cancel analysis" }));
      await waitFor(() =>
        expect(screen.getByLabelText("Structural-cycle operation status")).toHaveTextContent(
          "no longer available",
        ),
      );
      await act(async () => {});
      expect(reads()).toBe(1);
      expect(sessionStorage.getItem(ACTIVE_CYCLE_RUN_STORAGE_KEY)).toBeNull();
      expect(screen.queryByRole("button", { name: "Cancel analysis" })).not.toBeInTheDocument();
      expect(screen.getByRole("combobox", { name: "Capture" })).toBeEnabled();
      expect(screen.getByRole("button", { name: "Run structural cycles" })).toBeEnabled();

      routes["/api/cycle-runs"] = { body: freshRun };
      await user.click(screen.getByRole("button", { name: "Run structural cycles" }));
      expect(await screen.findByRole("button", { name: /Cycle 2/ })).toBeVisible();
      expect(reads()).toBe(1);
      expect(fetchMock.mock.calls.filter(([url]) => url === "/api/analyses")).toHaveLength(2);
      expect(fetchMock.mock.calls.filter(([url]) => url === topologyUrl)).toHaveLength(1);
    },
  );

  it("keeps an unrelated active SCC run and its recovery alive after cycle expiry", async () => {
    const user = userEvent.setup();
    const { routes, fetchMock } = installWorkspace();
    routes[pageUrl(expiredRun.runId)] = { body: cyclePageFixture };
    routes[detailUrl(0)] = notFound;
    routes["/api/scc-runs"] = { body: runningSccRunFixture, status: 202 };
    const sccUrl = `/api/scc-runs/${runningSccRunFixture.runId}?${revisionQuery}`;
    routes[sccUrl] = { body: succeededSccRunFixture };
    render(<App />);
    await screen.findByLabelText("RabbitMQ topology graph");
    await user.click(screen.getByRole("button", { name: "Run structural cycles" }));
    await screen.findByRole("button", { name: /Cycle 1/ });
    await user.click(screen.getByRole("button", { name: "Run cyclic components" }));
    await screen.findByRole("button", { name: "Cancel analysis" });
    const sccRecovery = sessionStorage.getItem(ACTIVE_SCC_RUN_STORAGE_KEY);
    expect(sccRecovery).not.toBeNull();
    await user.click(screen.getByRole("tab", { name: "Cycles" }));
    await user.click(screen.getByRole("button", { name: /Cycle 1/ }));
    await waitFor(() =>
      expect(screen.getByLabelText("Structural-cycle operation status")).toHaveTextContent(
        "no longer available",
      ),
    );
    expect(sessionStorage.getItem(ACTIVE_SCC_RUN_STORAGE_KEY)).toBe(sccRecovery);
    await user.click(screen.getByRole("tab", { name: "Components" }));
    expect(await screen.findByRole("checkbox", { name: /Cyclic component 1/ })).toBeVisible();
    expect(fetchMock.mock.calls.filter(([url]) => url === sccUrl)).toHaveLength(1);
    expect(fetchMock.mock.calls.filter(([url]) => url === "/api/analyses")).toHaveLength(2);
  });

  it("ignores a pending detail success after its cycle page expires", async () => {
    const user = userEvent.setup();
    const { routes, fetchMock } = installWorkspace();
    routes[pageUrl(expiredRun.runId)] = { body: cyclePageFixture };
    const sortedUrl = pageUrl(expiredRun.runId).replace("sort=sdk", "sort=length_desc");
    routes[sortedUrl] = notFound;
    const pending = deferred<Response>();
    const respond = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation((input, init) =>
      input === detailUrl(1) ? pending.promise : respond(input, init),
    );
    render(<App />);
    await screen.findByLabelText("RabbitMQ topology graph");
    await user.click(screen.getByRole("button", { name: "Run structural cycles" }));
    await user.click(await screen.findByRole("button", { name: /Cycle 2/ }));
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Sort summaries" }),
      "length_desc",
    );
    await waitFor(() =>
      expect(screen.getByLabelText("Structural-cycle operation status")).toHaveTextContent(
        "no longer available",
      ),
    );
    await act(async () => {
      pending.resolve(Response.json(cycleDetailFixture));
      await pending.promise;
    });
    expect(screen.queryByRole("article", { name: "Cycle 2 path" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Clear cycle filter" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("RabbitMQ topology graph")).toHaveTextContent(
      "n1.default-exchange",
    );
    expect(fetchMock.mock.calls.filter(([url]) => url === "/api/analyses")).toHaveLength(2);
  });

  it("coalesces concurrent unavailable SCC and cycle requests into one catalogue refresh", async () => {
    const user = userEvent.setup();
    const { routes, fetchMock } = installWorkspace();
    routes[pageUrl(expiredRun.runId)] = { body: cyclePageFixture };
    routes["/api/scc-runs"] = { body: runningSccRunFixture, status: 202 };
    const sccUrl = `/api/scc-runs/${runningSccRunFixture.runId}?${revisionQuery}`;
    const scc = deferred<Response>();
    const detail = deferred<Response>();
    const catalogue = deferred<Response>();
    const respond = fetchMock.getMockImplementation()!;
    let catalogueReads = 0;
    fetchMock.mockImplementation((input, init) => {
      if (input === sccUrl) return scc.promise;
      if (input === detailUrl(0)) return detail.promise;
      if (input === "/api/analyses" && ++catalogueReads > 1) return catalogue.promise;
      return respond(input, init);
    });
    render(<App />);
    await screen.findByLabelText("RabbitMQ topology graph");
    await user.click(screen.getByRole("button", { name: "Run structural cycles" }));
    await screen.findByRole("button", { name: /Cycle 1/ });
    await user.click(screen.getByRole("button", { name: "Run cyclic components" }));
    await user.click(screen.getByRole("tab", { name: "Cycles" }));
    await user.click(screen.getByRole("button", { name: /Cycle 1/ }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => url === sccUrl)).toBe(true));
    await act(async () => {
      scc.resolve(Response.json(notFound.body, { status: 404 }));
      detail.resolve(Response.json(notFound.body, { status: 404 }));
      await Promise.all([scc.promise, detail.promise]);
    });
    expect(catalogueReads).toBe(2);
    await act(async () => {
      catalogue.resolve(Response.json(catalogueFixture));
      await catalogue.promise;
    });
    expect(catalogueReads).toBe(2);
    expect(screen.getByRole("button", { name: "Run cyclic components" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Run structural cycles" })).toBeEnabled();
    expect(fetchMock.mock.calls.filter(([url]) => url === topologyUrl)).toHaveLength(1);
  });

  it.each(["missing", "revised"] as const)(
    "reconciles a %s snapshot after cycle-result expiry",
    async (state) => {
      const user = userEvent.setup();
      const { routes, fetchMock } = installWorkspace();
      render(<App />);
      await screen.findByLabelText("RabbitMQ topology graph");
      const revised = {
        ...catalogueFixture.static[0],
        snapshotRevision: `sha256:${"b".repeat(64)}`,
      };
      routes["/api/analyses"] = {
        body: { static: state === "missing" ? [] : [revised], session: [] },
      };
      routes[topologyUrl] = {
        body: { ...topologyFixture, snapshotRevision: revised.snapshotRevision },
      };
      await user.click(screen.getByRole("button", { name: "Run structural cycles" }));

      if (state === "missing") {
        expect(await screen.findByText("No captures available")).toBeVisible();
        expect(screen.queryByLabelText("RabbitMQ topology graph")).not.toBeInTheDocument();
      } else {
        await waitFor(() =>
          expect(fetchMock.mock.calls.filter(([url]) => url === topologyUrl)).toHaveLength(2),
        );
        expect(await screen.findByLabelText("RabbitMQ topology graph")).toBeVisible();
        expect(screen.queryByRole("combobox", { name: "Sort summaries" })).not.toBeInTheDocument();
        expect(screen.getByRole("button", { name: "Run structural cycles" })).toBeEnabled();
      }
      expect(sessionStorage.getItem(ACTIVE_CYCLE_RUN_STORAGE_KEY)).toBeNull();
      expect(fetchMock.mock.calls.filter(([url]) => url === "/api/analyses")).toHaveLength(2);
      expect(
        fetchMock.mock.calls.filter(([url]) => url === pageUrl(expiredRun.runId)),
      ).toHaveLength(1);
    },
  );

  it("keeps the cached graph and permits a new run when catalogue refresh fails", async () => {
    const user = userEvent.setup();
    const { routes, fetchMock } = installWorkspace();
    render(<App />);
    await screen.findByLabelText("RabbitMQ topology graph");
    routes["/api/analyses"] = {
      status: 503,
      body: {
        error: { code: "service_unavailable", message: "Temporarily unavailable.", issues: [] },
      },
    };
    await user.click(screen.getByRole("button", { name: "Run structural cycles" }));
    await waitFor(() =>
      expect(screen.getByLabelText("Structural-cycle operation status")).toHaveTextContent(
        "no longer available",
      ),
    );
    await act(async () => {});
    expect(screen.getByLabelText("RabbitMQ topology graph")).toBeVisible();
    routes["/api/cycle-runs"] = { body: freshRun };
    await user.click(screen.getByRole("button", { name: "Run structural cycles" }));
    expect(await screen.findByRole("button", { name: /Cycle 2/ })).toBeVisible();
    expect(fetchMock.mock.calls.filter(([url]) => url === "/api/analyses")).toHaveLength(2);
    expect(fetchMock.mock.calls.filter(([url]) => url === topologyUrl)).toHaveLength(1);
  });

  const lateResultCases = (["page", "detail"] as const).flatMap((resource) =>
    (["selection", "logout"] as const).map((boundary) => ({ resource, boundary })),
  );

  it("invalidates an old page immediately when selection changes before unmount", async () => {
    const user = userEvent.setup();
    const { routes, fetchMock } = installWorkspace();
    const other = addOtherAnalysis(routes);
    const pending = deferred<Response>();
    const respond = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation((input, init) =>
      input === pageUrl(expiredRun.runId) ? pending.promise : respond(input, init),
    );
    render(<App />);
    await screen.findByLabelText("RabbitMQ topology graph");
    await user.click(screen.getByRole("button", { name: "Run structural cycles" }));
    await act(async () => {
      fireEvent.change(screen.getByRole("combobox", { name: "Capture" }), {
        target: { value: `static:${other.analysisId}` },
      });
      pending.resolve(Response.json(notFound.body, { status: 404 }));
      await pending.promise;
    });

    expect(await screen.findByLabelText("RabbitMQ topology graph")).toBeVisible();
    expect(screen.getByRole("combobox", { name: "Capture" })).toHaveValue(
      `static:${other.analysisId}`,
    );
    expect(fetchMock.mock.calls.filter(([url]) => url === "/api/analyses")).toHaveLength(1);
  });

  it.each(lateResultCases)(
    "ignores an old $resource 404 after $boundary and preserves the new workspace recovery",
    async ({ resource, boundary }) => {
      const user = userEvent.setup();
      const { routes, fetchMock } = installWorkspace();
      const other = addOtherAnalysis(routes);
      enableBasicSession(routes);
      if (resource === "detail") routes[pageUrl(expiredRun.runId)] = { body: cyclePageFixture };
      const pending = deferred<Response>();
      const pendingUrl = resource === "page" ? pageUrl(expiredRun.runId) : detailUrl(0);
      const respond = fetchMock.getMockImplementation()!;
      fetchMock.mockImplementation((input, init) =>
        input === pendingUrl ? pending.promise : respond(input, init),
      );
      render(<App />);
      await screen.findByLabelText("RabbitMQ topology graph");
      await user.click(screen.getByRole("button", { name: "Run structural cycles" }));
      if (resource === "detail")
        await user.click(await screen.findByRole("button", { name: /Cycle 1/ }));
      await waitFor(() =>
        expect(fetchMock.mock.calls.some(([url]) => url === pendingUrl)).toBe(true),
      );
      if (boundary === "selection") {
        await user.selectOptions(
          screen.getByRole("combobox", { name: "Capture" }),
          `static:${other.analysisId}`,
        );
      } else {
        await user.click(screen.getByRole("button", { name: "Sign out" }));
        await user.click(await screen.findByRole("button", { name: "Authenticate" }));
      }
      await screen.findByLabelText("RabbitMQ topology graph");
      sessionStorage.setItem(ACTIVE_CYCLE_RUN_STORAGE_KEY, "new-workspace-cycle-recovery");
      const catalogueReads = fetchMock.mock.calls.filter(([url]) => url === "/api/analyses").length;
      await act(async () => {
        pending.resolve(Response.json(notFound.body, { status: 404 }));
        await pending.promise;
      });
      expect(screen.getByLabelText("RabbitMQ topology graph")).toBeVisible();
      expect(screen.getByRole("combobox", { name: "Capture" })).toHaveValue(
        `static:${boundary === "selection" ? other.analysisId : topologyFixture.analysisId}`,
      );
      expect(sessionStorage.getItem(ACTIVE_CYCLE_RUN_STORAGE_KEY)).toBe(
        "new-workspace-cycle-recovery",
      );
      expect(fetchMock.mock.calls.filter(([url]) => url === "/api/analyses")).toHaveLength(
        catalogueReads,
      );
    },
  );

  it.each(["selection", "logout"] as const)(
    "ignores late catalogue reconciliation after %s",
    async (boundary) => {
      const user = userEvent.setup();
      const { routes, fetchMock } = installWorkspace();
      const other = addOtherAnalysis(routes);
      enableBasicSession(routes);
      const pending = deferred<Response>();
      const respond = fetchMock.getMockImplementation()!;
      let catalogueReads = 0;
      fetchMock.mockImplementation((input, init) => {
        if (input === "/api/analyses" && ++catalogueReads === 2) return pending.promise;
        return respond(input, init);
      });
      render(<App />);
      await screen.findByLabelText("RabbitMQ topology graph");
      await user.click(screen.getByRole("button", { name: "Run structural cycles" }));
      await waitFor(() => expect(catalogueReads).toBe(2));
      if (boundary === "selection") {
        await user.selectOptions(
          screen.getByRole("combobox", { name: "Capture" }),
          `static:${other.analysisId}`,
        );
      } else {
        await user.click(screen.getByRole("button", { name: "Sign out" }));
        await user.click(await screen.findByRole("button", { name: "Authenticate" }));
      }
      await screen.findByLabelText("RabbitMQ topology graph");
      sessionStorage.setItem(ACTIVE_SCC_RUN_STORAGE_KEY, "new-workspace-scc-recovery");
      const topologyReads = fetchMock.mock.calls.filter(([url]) =>
        String(url).startsWith("/api/topology?"),
      ).length;
      await act(async () => {
        pending.resolve(Response.json({ static: [], session: [] }));
        await pending.promise;
      });
      expect(screen.getByLabelText("RabbitMQ topology graph")).toBeVisible();
      expect(screen.getByRole("combobox", { name: "Capture" })).toHaveValue(
        `static:${boundary === "selection" ? other.analysisId : topologyFixture.analysisId}`,
      );
      expect(sessionStorage.getItem(ACTIVE_SCC_RUN_STORAGE_KEY)).toBe("new-workspace-scc-recovery");
      expect(
        fetchMock.mock.calls.filter(([url]) => String(url).startsWith("/api/topology?")),
      ).toHaveLength(topologyReads);
    },
  );
});
