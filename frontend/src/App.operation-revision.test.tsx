import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import App from "./App";
import type { GraphSelection } from "./components/DetailsPanel";
import type { VisibleTopology } from "./graph/presentation";
import { ACTIVE_CYCLE_RUN_STORAGE_KEY } from "./operations/cycles";
import { ACTIVE_SCC_RUN_STORAGE_KEY } from "./operations/scc";
import {
  accessFixture,
  basicAuthSessionFixture,
  catalogueFixture,
  cycleDetailFixture,
  cyclePageFixture,
  installApiFixtures,
  runningCycleRunFixture,
  runningSccRunFixture,
  succeededCycleRunFixture,
  succeededSccRunFixture,
  topologyFixture,
  type ApiFixture,
} from "./test/api-fixtures";
import type { AnalysisSource, AnalysisSummary } from "./types/topology";

vi.mock("./graph/TopologyCanvas", () => ({
  TopologyCanvas: ({ topology, onSelectionChange }: {
    topology: VisibleTopology;
    onSelectionChange: (selection: GraphSelection) => void;
  }) => (
    <div aria-label="RabbitMQ topology graph">
      {topology.nodes.map((node) => node.data.name).join(", ")}
      <button type="button" onClick={() => onSelectionChange({ kind: "node", id: topology.nodes[0]!.id })}>
        Inspect first resource
      </button>
    </div>
  ),
}));

const revision = topologyFixture.snapshotRevision;
const nextRevision = `sha256:${"b".repeat(64)}`;
const mismatch: ApiFixture = {
  status: 409,
  body: { error: {
    code: "snapshot_revision_mismatch", message: "The snapshot revision changed.", issues: [],
  } },
};
const unavailable: ApiFixture = {
  status: 503,
  body: { error: { code: "service_unavailable", message: "Catalogue unavailable.", issues: [] } },
};
const operations = ["scc", "cycle"] as const;
type Operation = (typeof operations)[number];
const startLabel = (operation: Operation) =>
  operation === "scc" ? "Run cyclic components" : "Run structural cycles";
const storageKey = (operation: Operation) =>
  operation === "scc" ? ACTIVE_SCC_RUN_STORAGE_KEY : ACTIVE_CYCLE_RUN_STORAGE_KEY;
const run = (operation: Operation) =>
  operation === "scc" ? runningSccRunFixture : runningCycleRunFixture;
const topologyUrl = (analysis: Pick<AnalysisSummary, "source" | "analysisId">) =>
  `/api/topology?source=${analysis.source}&analysis_id=${analysis.analysisId}`;
const runUrl = (operation: Operation) =>
  `/api/${operation}-runs/${run(operation).runId}?snapshot_revision=${encodeURIComponent(revision)}`;
const pageUrl = (snapshotRevision: string = revision) =>
  `/api/cycle-runs/${run("cycle").runId}/cycles?snapshot_revision=${encodeURIComponent(snapshotRevision)}&offset=0&limit=50&sort=sdk`;
const detailUrl = `/api/cycle-runs/${run("cycle").runId}/cycles/1?snapshot_revision=${encodeURIComponent(revision)}`;
const response = (fixture: ApiFixture) =>
  Response.json(fixture.body, { status: fixture.status ?? 200 });

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((complete) => { resolve = complete; });
  return { promise, resolve };
}

function installWorkspace(source: AnalysisSource = "static") {
  const original = { ...catalogueFixture.static[0], source };
  const revised = { ...original, snapshotRevision: nextRevision };
  const other = { ...original, analysisId: "other-analysis", label: "Other capture" };
  const catalogue = (selected: AnalysisSummary, decoy = false) => {
    const opposite = source === "static" ? "session" : "static";
    const items = [selected, other, ...(decoy ? [{ ...selected, source: opposite }] : [])];
    return {
      static: items.filter((item) => item.source === "static"),
      session: items.filter((item) => item.source === "session"),
    };
  };
  const graph = (selected: AnalysisSummary, name: string) => ({
    ...topologyFixture,
    source: selected.source,
    analysisId: selected.analysisId,
    snapshotRevision: selected.snapshotRevision,
    nodes: topologyFixture.nodes.map((node, index) =>
      index === 0 ? { ...node, data: { ...node.data, name } } : node),
  });
  const routes: Record<string, ApiFixture> = {
    "/api/access": { body: accessFixture },
    "/api/analyses": { body: catalogue(original) },
    [topologyUrl(original)]: { body: graph(original, "Original orders") },
    [topologyUrl(other)]: { body: graph(other, "Other orders") },
    "/api/scc-runs": { body: { ...succeededSccRunFixture, source } },
    "/api/cycle-runs": { body: { ...succeededCycleRunFixture, source } },
    [runUrl("scc")]: { body: { ...runningSccRunFixture, source } },
    [runUrl("cycle")]: { body: { ...runningCycleRunFixture, source } },
    [pageUrl()]: { body: cyclePageFixture },
    [detailUrl]: { body: cycleDetailFixture },
    [pageUrl(nextRevision)]: { body: { ...cyclePageFixture, snapshotRevision: nextRevision } },
  };
  const fetchMock = installApiFixtures(routes);
  const reads = (url: string) => fetchMock.mock.calls.filter(([input]) => input === url).length;
  const publish = () => {
    routes["/api/analyses"] = { body: catalogue(revised, true) };
    routes[topologyUrl(original)] = { body: graph(revised, "Revised orders") };
    routes["/api/scc-runs"] = {
      body: { ...succeededSccRunFixture, source, snapshotRevision: nextRevision },
    };
    routes["/api/cycle-runs"] = {
      body: { ...succeededCycleRunFixture, source, snapshotRevision: nextRevision },
    };
  };
  const failOnce = (url: string, method: string, afterPublish = () => {}) => {
    const respond = fetchMock.getMockImplementation()!;
    let failed = false;
    fetchMock.mockImplementation((input, init) => {
      if (!failed && input === url && (init?.method ?? "GET") === method) {
        failed = true;
        publish();
        afterPublish();
        return Promise.resolve(response(mismatch));
      }
      return respond(input, init);
    });
  };
  const basic = () => {
    routes["/api/auth/session"] = { body: basicAuthSessionFixture };
    routes["/api/access"] = { body: { ...accessFixture, authMode: "basic" } };
    routes["/api/auth/logout"] = { body: null, status: 204 };
  };
  return { original, revised, other, routes, fetchMock, reads, publish, failOnce, basic };
}

async function expectRevised(workspace: ReturnType<typeof installWorkspace>) {
  await waitFor(() => expect(screen.getByLabelText("RabbitMQ topology graph"))
    .toHaveTextContent("Revised orders"));
  expect(screen.getByRole("combobox", { name: "Analysis" }))
    .toHaveValue(`${workspace.original.source}:${workspace.original.analysisId}`);
  expect(workspace.reads("/api/analyses")).toBe(2);
  expect(workspace.reads(topologyUrl(workspace.original))).toBe(2);
  for (const operation of operations) {
    expect(sessionStorage.getItem(storageKey(operation))).toBeNull();
    expect(screen.getByRole("button", { name: startLabel(operation) })).toBeEnabled();
  }
  expect(screen.queryByRole("button", { name: /Clear .* focus/ })).not.toBeInTheDocument();
  expect(screen.queryByRole("article", { name: /Cycle .* path/ })).not.toBeInTheDocument();
  expect(screen.queryByRole("checkbox", { name: /Cyclic component 1/ })).not.toBeInTheDocument();
}

describe("operation revision changes in a ready App", () => {
  it.each(operations.flatMap((operation) =>
    (["static", "session"] as const).map((source) => ({ operation, source }))))(
    "reloads the same $source identity after a stale $operation start and discards old layers",
    async ({ operation, source }) => {
      const user = userEvent.setup();
      const workspace = installWorkspace(source);
      render(<App />);
      await screen.findByLabelText("RabbitMQ topology graph");
      await user.click(screen.getByRole("button", { name: startLabel("scc") }));
      await user.click(await screen.findByRole("checkbox", { name: /Cyclic component 1/ }));
      await user.click(screen.getByRole("button", { name: startLabel("cycle") }));
      await user.click(await screen.findByRole("button", { name: /Cycle 2/ }));
      await screen.findByRole("article", { name: "Cycle 2 path" });
      await user.click(screen.getByRole("button", { name: "Inspect first resource" }));

      workspace.failOnce(`/api/${operation}-runs`, "POST");
      await user.click(screen.getByRole("button", { name: startLabel(operation) }));
      await expectRevised(workspace);
      expect(screen.getByText("Select a resource or relationship to inspect it.")).toBeVisible();
      await user.click(screen.getByRole("button", { name: startLabel(operation) }));
      const calls = workspace.fetchMock.mock.calls.filter(([url]) => url === `/api/${operation}-runs`);
      expect(JSON.parse(String(calls.at(-2)![1]?.body)).snapshotRevision).toBe(revision);
      expect(JSON.parse(String(calls.at(-1)![1]?.body))).toMatchObject({
        source, analysisId: workspace.original.analysisId, snapshotRevision: nextRevision,
      });
      expect(workspace.reads("/api/analyses")).toBe(2);
    },
  );

  it.each(operations.flatMap((operation) =>
    (["recover-start", "recover-run", "poll", "cancel"] as const)
      .map((phase) => ({ operation, phase }))))(
    "reconciles a $operation mismatch during $phase",
    async ({ operation, phase }) => {
      const user = userEvent.setup();
      const workspace = installWorkspace();
      workspace.routes[`/api/${operation}-runs`] = { body: run(operation), status: 202 };
      if (phase.startsWith("recover")) {
        sessionStorage.setItem(storageKey(operation), JSON.stringify({
          operation: run(operation).operation,
          source: workspace.original.source,
          analysisId: workspace.original.analysisId,
          snapshotRevision: revision,
          idempotencyKey: "40000000-0000-4000-8000-000000000004",
          maxCycles: 20,
          runId: phase === "recover-start" ? null : run(operation).runId,
        }));
      }
      const url = phase === "recover-start" ? `/api/${operation}-runs` : runUrl(operation);
      workspace.failOnce(url, phase === "recover-start" ? "POST" : phase === "cancel" ? "DELETE" : "GET");
      render(<App />);
      if (!phase.startsWith("recover")) {
        await user.click(await screen.findByRole("button", { name: startLabel(operation) }));
        if (phase === "cancel") {
          await user.click(await screen.findByRole("button", { name: "Cancel analysis" }));
        }
      }
      await expectRevised(workspace);
      expect(workspace.reads(url)).toBe(1);
    },
  );

  it.each(["page", "detail"] as const)("reconciles a retained cycle %s mismatch", async (phase) => {
    const user = userEvent.setup();
    const workspace = installWorkspace();
    workspace.failOnce(phase === "page" ? pageUrl() : detailUrl, "GET");
    render(<App />);
    await user.click(await screen.findByRole("button", { name: startLabel("cycle") }));
    if (phase === "detail") await user.click(await screen.findByRole("button", { name: /Cycle 2/ }));
    await expectRevised(workspace);
  });

  it.each(["unavailable", "inconsistent"] as const)(
    "bounds an automatic refresh that is %s and offers an explicit catalogue retry",
    async (failure) => {
      const user = userEvent.setup();
      const workspace = installWorkspace();
      render(<App />);
      await screen.findByLabelText("RabbitMQ topology graph");
      workspace.failOnce("/api/scc-runs", "POST", () => {
        workspace.routes["/api/analyses"] = failure === "unavailable" ? unavailable : {
          body: { static: [workspace.original], session: [] },
        };
      });
      await user.click(screen.getByRole("button", { name: startLabel("scc") }));
      await screen.findByRole("button", { name: "Retry" });
      await act(async () => {});
      expect(workspace.reads("/api/analyses")).toBe(2);
      expect(workspace.reads(topologyUrl(workspace.original))).toBe(failure === "unavailable" ? 1 : 2);
      expect(screen.queryByLabelText("RabbitMQ topology graph")).not.toBeInTheDocument();
      expect(sessionStorage.getItem(ACTIVE_SCC_RUN_STORAGE_KEY)).toBeNull();
      workspace.publish();
      await user.click(screen.getByRole("button", { name: "Retry" }));
      await waitFor(() => expect(screen.getByLabelText("RabbitMQ topology graph"))
        .toHaveTextContent("Revised orders"));
      expect(workspace.reads("/api/analyses")).toBe(3);
    },
  );

  it("coalesces concurrent SCC polling and cycle-detail mismatches into one refresh", async () => {
    const user = userEvent.setup();
    const workspace = installWorkspace();
    workspace.routes["/api/scc-runs"] = { body: runningSccRunFixture, status: 202 };
    const scc = deferred<Response>();
    const detail = deferred<Response>();
    const catalogue = deferred<Response>();
    const respond = workspace.fetchMock.getMockImplementation()!;
    workspace.fetchMock.mockImplementation((input, init) => {
      if (input === runUrl("scc")) return scc.promise;
      if (input === detailUrl) return detail.promise;
      if (input === "/api/analyses" && workspace.reads("/api/analyses") > 1) return catalogue.promise;
      return respond(input, init);
    });
    render(<App />);
    await user.click(await screen.findByRole("button", { name: startLabel("cycle") }));
    await screen.findByRole("button", { name: /Cycle 2/ });
    await user.click(screen.getByRole("button", { name: startLabel("scc") }));
    await user.click(screen.getByRole("tab", { name: "Cycles" }));
    await user.click(screen.getByRole("button", { name: /Cycle 2/ }));
    await waitFor(() => expect(workspace.reads(runUrl("scc"))).toBe(1));
    workspace.publish();
    await act(async () => {
      scc.resolve(response(mismatch));
      detail.resolve(response(mismatch));
      await Promise.all([scc.promise, detail.promise]);
    });
    expect(workspace.reads("/api/analyses")).toBe(2);
    expect(screen.getByRole("heading", { name: "Loading topology" })).toBeVisible();
    await act(async () => {
      catalogue.resolve(response(workspace.routes["/api/analyses"]!));
      await catalogue.promise;
    });
    await expectRevised(workspace);
  });

  it.each(["page", "detail"] as const)(
    "ignores an old cycle %s success after another operation establishes a new snapshot",
    async (resource) => {
      const user = userEvent.setup();
      const workspace = installWorkspace();
      const pending = deferred<Response>();
      const respond = workspace.fetchMock.getMockImplementation()!;
      const url = resource === "page" ? pageUrl() : detailUrl;
      workspace.fetchMock.mockImplementation((input, init) =>
        input === url ? pending.promise : respond(input, init));
      render(<App />);
      await user.click(await screen.findByRole("button", { name: startLabel("cycle") }));
      if (resource === "detail") {
        await user.click(await screen.findByRole("button", { name: /Cycle 2/ }));
      }
      await waitFor(() => expect(workspace.reads(url)).toBe(1));
      workspace.failOnce("/api/scc-runs", "POST");
      await user.click(screen.getByRole("button", { name: startLabel("scc") }));
      await expectRevised(workspace);
      await user.click(screen.getByRole("button", { name: startLabel("scc") }));
      const component = await screen.findByRole("checkbox", { name: /Cyclic component 1/ });
      await user.click(component);
      sessionStorage.setItem(ACTIVE_CYCLE_RUN_STORAGE_KEY, "new-workspace-recovery");
      await act(async () => {
        pending.resolve(Response.json(resource === "page" ? cyclePageFixture : cycleDetailFixture));
        await pending.promise;
      });
      expect(component).toBeChecked();
      expect(screen.queryByRole("article", { name: "Cycle 2 path" })).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: /Cycle 2/ })).not.toBeInTheDocument();
      expect(sessionStorage.getItem(ACTIVE_CYCLE_RUN_STORAGE_KEY)).toBe("new-workspace-recovery");
      expect(workspace.reads("/api/analyses")).toBe(2);
    },
  );

  it.each(operations)("keeps the graph and focus for an ordinary %s start conflict", async (operation) => {
    const user = userEvent.setup();
    const workspace = installWorkspace();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: startLabel("scc") }));
    const component = await screen.findByRole("checkbox", { name: /Cyclic component 1/ });
    await user.click(component);
    workspace.routes[`/api/${operation}-runs`] = {
      status: 409,
      body: { error: { code: "analysis_busy", message: "Analysis is busy.", issues: [] } },
    };
    await user.click(screen.getByRole("button", { name: startLabel(operation) }));
    await waitFor(() => expect(screen.getByLabelText(operation === "scc"
      ? "Cyclic-component operation status" : "Structural-cycle operation status"))
      .toHaveTextContent("Analysis is busy."));
    expect(component).toBeChecked();
    expect(workspace.reads("/api/analyses")).toBe(1);
    expect(workspace.reads(topologyUrl(workspace.original))).toBe(1);
  });

  const boundaries = ["selection", "authentication", "unmount"] as const;
  it.each(boundaries)("ignores an obsolete detail mismatch after %s", async (boundary) => {
    const user = userEvent.setup();
    const workspace = installWorkspace();
    workspace.basic();
    const pending = deferred<Response>();
    const respond = workspace.fetchMock.getMockImplementation()!;
    workspace.fetchMock.mockImplementation((input, init) =>
      input === detailUrl ? pending.promise : respond(input, init));
    const view = render(<App />);
    await user.click(await screen.findByRole("button", { name: startLabel("cycle") }));
    await user.click(await screen.findByRole("button", { name: /Cycle 2/ }));
    if (boundary === "authentication") {
      await user.click(screen.getByRole("button", { name: "Sign out" }));
      await user.click(await screen.findByRole("button", { name: "Authenticate" }));
      await screen.findByLabelText("RabbitMQ topology graph");
    }
    const before = workspace.reads("/api/analyses");
    if (boundary !== "selection") {
      sessionStorage.setItem(ACTIVE_CYCLE_RUN_STORAGE_KEY, "new-workspace-recovery");
    }
    await act(async () => {
      if (boundary === "selection") fireEvent.change(screen.getByRole("combobox", { name: "Analysis" }), {
        target: { value: `static:${workspace.other.analysisId}` },
      });
      if (boundary === "unmount") view.unmount();
      pending.resolve(response(mismatch));
      await pending.promise;
    });
    if (boundary !== "unmount") {
      expect(screen.getByLabelText("RabbitMQ topology graph"))
        .toHaveTextContent(boundary === "selection" ? "Other orders" : "Original orders");
    }
    expect(workspace.reads("/api/analyses")).toBe(before);
    if (boundary !== "selection") {
      expect(sessionStorage.getItem(ACTIVE_CYCLE_RUN_STORAGE_KEY)).toBe("new-workspace-recovery");
    }
  });

  it.each((["catalogue", "topology"] as const).flatMap((stage) =>
    boundaries.flatMap((boundary) => (["success", "failure"] as const)
      .map((result) => ({ stage, boundary, result })))))(
    "ignores late automatic $stage reconciliation $result after $boundary",
    async ({ stage, boundary, result }) => {
      const user = userEvent.setup();
      const workspace = installWorkspace();
      workspace.basic();
      const view = render(<App />);
      await screen.findByLabelText("RabbitMQ topology graph");
      workspace.failOnce("/api/scc-runs", "POST");
      const pending = deferred<Response>();
      const respond = workspace.fetchMock.getMockImplementation()!;
      const url = stage === "catalogue" ? "/api/analyses" : topologyUrl(workspace.original);
      let held = false;
      workspace.fetchMock.mockImplementation((input, init) => {
        if (!held && input === url) { held = true; return pending.promise; }
        return respond(input, init);
      });
      await user.click(screen.getByRole("button", { name: startLabel("scc") }));
      await waitFor(() => expect(held).toBe(true));
      if (boundary === "selection") {
        await user.selectOptions(screen.getByRole("combobox", { name: "Analysis" }),
          `static:${workspace.other.analysisId}`);
      } else if (boundary === "authentication") {
        await user.click(screen.getByRole("button", { name: "Sign out" }));
        await user.click(await screen.findByRole("button", { name: "Authenticate" }));
      } else view.unmount();
      if (boundary !== "unmount") await screen.findByLabelText("RabbitMQ topology graph");
      sessionStorage.setItem(ACTIVE_SCC_RUN_STORAGE_KEY, "new-workspace-recovery");
      const before = workspace.reads(topologyUrl(workspace.original));
      await act(async () => {
        pending.resolve(response(result === "failure" ? unavailable : workspace.routes[url]!));
        await pending.promise;
      });
      expect(workspace.reads(topologyUrl(workspace.original))).toBe(before);
      expect(sessionStorage.getItem(ACTIVE_SCC_RUN_STORAGE_KEY)).toBe("new-workspace-recovery");
      if (boundary !== "unmount") {
        expect(screen.queryByRole("alert")).not.toBeInTheDocument();
        expect(screen.getByLabelText("RabbitMQ topology graph"))
          .toHaveTextContent(boundary === "selection" ? "Other orders" : "Revised orders");
      }
    },
  );
});
