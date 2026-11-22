import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import App from "./App";
import type { VisibleTopology } from "./graph/presentation";
import { ACTIVE_CYCLE_RUN_STORAGE_KEY } from "./operations/cycles";
import { ACTIVE_SCC_RUN_STORAGE_KEY } from "./operations/scc";
import {
  accessFixture,
  basicAuthSessionFixture,
  catalogueFixture,
  installApiFixtures,
  topologyFixture,
  type ApiFixture,
} from "./test/api-fixtures";
import type { AnalysisSource, AnalysisSummary, TopologyResponse } from "./types/topology";

vi.mock("./graph/TopologyCanvas", () => ({
  TopologyCanvas: ({ topology }: { topology: VisibleTopology }) => (
    <div aria-label="RabbitMQ topology graph">
      {topology.nodes.map((node) => node.data.name).join(", ")}
    </div>
  ),
}));

const original = catalogueFixture.static[0];
const revised = { ...original, snapshotRevision: `sha256:${"b".repeat(64)}` };
const other = { ...original, analysisId: "other-analysis", label: "Other capture" };
const unavailable: ApiFixture = {
  status: 503,
  body: { error: { code: "service_unavailable", message: "Temporarily unavailable.", issues: [] } },
};

function topologyUrl(analysis: Pick<AnalysisSummary, "source" | "analysisId">): string {
  return `/api/topology?source=${analysis.source}&analysis_id=${analysis.analysisId}`;
}

function graph(analysis: AnalysisSummary, name: string): TopologyResponse {
  return {
    ...topologyFixture,
    source: analysis.source,
    analysisId: analysis.analysisId,
    snapshotRevision: analysis.snapshotRevision,
    nodes: topologyFixture.nodes.map((node, index) =>
      index === 0 ? { ...node, data: { ...node.data, name } } : node,
    ),
  };
}

function catalogueFor(analysis: AnalysisSummary) {
  return {
    static: analysis.source === "static" ? [analysis] : [],
    session: analysis.source === "session" ? [analysis] : [],
  };
}

function installWorkspace(source: AnalysisSource = "static") {
  const selected = { ...original, source };
  const updated = { ...revised, source };
  const routes: Record<string, ApiFixture> = {
    "/api/access": { body: accessFixture },
    "/api/analyses": { body: catalogueFor(selected) },
    [topologyUrl(selected)]: { body: graph(updated, "Updated orders") },
    [topologyUrl(other)]: { body: graph(other, "Other topology") },
  };
  const fetchMock = installApiFixtures(routes);
  const reads = (url: string) => fetchMock.mock.calls.filter(([input]) => input === url).length;
  return { routes, fetchMock, reads, selected, updated };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((complete) => {
    resolve = complete;
  });
  return { promise, resolve };
}

function storeObsoleteRecovery(analysis: AnalysisSummary) {
  const snapshot = {
    source: analysis.source,
    analysisId: analysis.analysisId,
    snapshotRevision: analysis.snapshotRevision,
    idempotencyKey: "40000000-0000-4000-8000-000000000004",
    runId: "50000000-0000-4000-8000-000000000005",
  };
  sessionStorage.setItem(
    ACTIVE_SCC_RUN_STORAGE_KEY,
    JSON.stringify({ ...snapshot, operation: "cyclic_scc" }),
  );
  sessionStorage.setItem(
    ACTIVE_CYCLE_RUN_STORAGE_KEY,
    JSON.stringify({ ...snapshot, operation: "structural_cycles", maxCycles: 20 }),
  );
}

describe("retrying a changed snapshot in App", () => {
  it.each(["static", "session"] as const)(
    "refreshes the catalogue and reloads the exact %s identity at its new revision",
    async (source) => {
      const user = userEvent.setup();
      const { routes, reads, selected, updated } = installWorkspace(source);
      render(<App />);
      expect(await screen.findByRole("alert")).toHaveTextContent(/snapshot changed/i);
      expect(reads("/api/analyses")).toBe(1);
      expect(reads(topologyUrl(selected))).toBe(1);

      // A static and a private capture may have the same analysis ID.
      const decoy = { ...updated, source: source === "session" ? "static" : "session" } as const;
      routes["/api/analyses"] = {
        body: {
          static: [source === "static" ? updated : decoy],
          session: [source === "session" ? updated : decoy],
        },
      };
      storeObsoleteRecovery(selected);
      await user.click(screen.getByRole("button", { name: "Retry" }));

      expect(await screen.findByLabelText("RabbitMQ topology graph")).toHaveTextContent(
        "Updated orders",
      );
      expect(screen.getByRole("combobox", { name: "Capture" })).toHaveValue(
        `${source}:${selected.analysisId}`,
      );
      expect(sessionStorage.getItem(ACTIVE_SCC_RUN_STORAGE_KEY)).toBeNull();
      expect(sessionStorage.getItem(ACTIVE_CYCLE_RUN_STORAGE_KEY)).toBeNull();
      expect(reads(topologyUrl(decoy))).toBe(0);
      expect(screen.getByRole("button", { name: "Run structural cycles" })).toBeEnabled();
      await user.click(screen.getByRole("checkbox", { name: "Hide default exchanges" }));
      await user.click(screen.getByRole("button", { name: "Reset layout" }));
      expect(reads("/api/analyses")).toBe(2);
      expect(reads(topologyUrl(selected))).toBe(2);
    },
  );

  it("settles persistent inconsistency after one catalogue and topology request per retry", async () => {
    const user = userEvent.setup();
    const { routes, reads } = installWorkspace();
    render(<App />);
    await screen.findByRole("alert");
    storeObsoleteRecovery(original);
    await user.click(screen.getByRole("button", { name: "Retry" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/snapshot changed/i);
    await act(async () => {});
    expect(reads("/api/analyses")).toBe(2);
    expect(reads(topologyUrl(original))).toBe(2);
    expect(screen.getByRole("button", { name: "Retry" })).toBeEnabled();
    expect(screen.queryByLabelText("RabbitMQ topology graph")).not.toBeInTheDocument();
    expect(sessionStorage.getItem(ACTIVE_SCC_RUN_STORAGE_KEY)).toBeNull();
    expect(sessionStorage.getItem(ACTIVE_CYCLE_RUN_STORAGE_KEY)).toBeNull();

    routes["/api/analyses"] = { body: catalogueFor(revised) };
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByLabelText("RabbitMQ topology graph")).toHaveTextContent(
      "Updated orders",
    );
    expect(reads("/api/analyses")).toBe(3);
    expect(reads(topologyUrl(original))).toBe(3);
  });

  it("keeps retry available when refreshing the catalogue fails", async () => {
    const user = userEvent.setup();
    const { routes, reads } = installWorkspace();
    render(<App />);
    await screen.findByRole("alert");
    routes["/api/analyses"] = unavailable;
    await user.click(screen.getByRole("button", { name: "Retry" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Temporarily unavailable.");
    expect(reads(topologyUrl(original))).toBe(1);
    expect(screen.getByRole("button", { name: "Retry" })).toBeEnabled();
    routes["/api/analyses"] = { body: catalogueFor(revised) };
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByLabelText("RabbitMQ topology graph")).toHaveTextContent(
      "Updated orders",
    );
    expect(reads("/api/analyses")).toBe(3);
  });

  it.each(["fallback", "empty"] as const)(
    "uses the established %s state when the selected identity was removed",
    async (state) => {
      const user = userEvent.setup();
      const { routes, reads } = installWorkspace();
      render(<App />);
      await screen.findByRole("alert");
      routes["/api/analyses"] = {
        body: { static: state === "fallback" ? [other] : [], session: [] },
      };
      await user.click(screen.getByRole("button", { name: "Retry" }));

      if (state === "fallback") {
        expect(await screen.findByLabelText("RabbitMQ topology graph")).toHaveTextContent(
          "Other topology",
        );
        expect(screen.getByRole("combobox", { name: "Capture" })).toHaveValue(
          `static:${other.analysisId}`,
        );
      } else {
        expect(await screen.findByText("No captures available")).toBeVisible();
        expect(screen.queryByLabelText("RabbitMQ topology graph")).not.toBeInTheDocument();
        expect(screen.getByRole("button", { name: "Upload capture" })).toBeEnabled();
      }
      expect(reads("/api/analyses")).toBe(2);
      expect(reads(topologyUrl(original))).toBe(1);
    },
  );

  it("retries an ordinary topology failure at an unchanged revision without duplicate loads", async () => {
    const user = userEvent.setup();
    const { routes, reads } = installWorkspace();
    routes[topologyUrl(original)] = unavailable;
    render(<App />);
    await screen.findByRole("alert");
    routes[topologyUrl(original)] = { body: topologyFixture };
    await user.click(screen.getByRole("button", { name: "Retry" }));

    expect(await screen.findByLabelText("RabbitMQ topology graph")).toHaveTextContent(
      "orders.events",
    );
    expect(reads("/api/analyses")).toBe(2);
    expect(reads(topologyUrl(original))).toBe(2);
  });

  const races = (["catalogue", "topology"] as const).flatMap((request) =>
    (["selection", "logout", "unmount"] as const).flatMap((boundary) =>
      (["success", "failure"] as const).map((result) => ({ request, boundary, result })),
    ),
  );

  it.each(races)(
    "ignores a late retry $request $result after $boundary",
    async ({ request, boundary, result }) => {
      const user = userEvent.setup();
      const { routes, fetchMock, reads } = installWorkspace();
      routes["/api/analyses"] = { body: { static: [original, other], session: [] } };
      routes["/api/auth/session"] = { body: basicAuthSessionFixture };
      routes["/api/access"] = { body: { ...accessFixture, authMode: "basic" } };
      routes["/api/auth/logout"] = { body: null, status: 204 };
      const mounted = render(<App />);
      await screen.findByRole("alert");
      routes["/api/analyses"] = { body: { static: [revised, other], session: [] } };
      const pending = deferred<Response>();
      const respond = fetchMock.getMockImplementation()!;
      const pendingUrl = request === "catalogue" ? "/api/analyses" : topologyUrl(original);
      let held = false;
      fetchMock.mockImplementation((input, init) => {
        if (input === pendingUrl && !held) {
          held = true;
          return pending.promise;
        }
        return respond(input, init);
      });
      await user.click(screen.getByRole("button", { name: "Retry" }));
      await waitFor(() => expect(held).toBe(true));

      if (boundary === "selection") {
        await user.selectOptions(
          screen.getByRole("combobox", { name: "Capture" }),
          `static:${other.analysisId}`,
        );
        expect(await screen.findByLabelText("RabbitMQ topology graph")).toHaveTextContent(
          "Other topology",
        );
      } else if (boundary === "logout") {
        await user.click(screen.getByRole("button", { name: "Sign out" }));
        routes["/api/analyses"] = { body: { static: [original, other], session: [] } };
        routes[topologyUrl(original)] = { body: topologyFixture };
        await user.click(await screen.findByRole("button", { name: "Authenticate" }));
        expect(await screen.findByLabelText("RabbitMQ topology graph")).toHaveTextContent(
          "orders.events",
        );
      } else {
        mounted.unmount();
      }
      sessionStorage.setItem(ACTIVE_CYCLE_RUN_STORAGE_KEY, "new-workspace-recovery");
      const topologyReads = reads(topologyUrl(original));
      await act(async () => {
        const fixture =
          result === "failure"
            ? unavailable
            : {
                body:
                  request === "catalogue"
                    ? catalogueFor(revised)
                    : graph(revised, "Updated orders"),
              };
        pending.resolve(Response.json(fixture.body, { status: fixture.status ?? 200 }));
        await pending.promise;
      });

      expect(reads(topologyUrl(original))).toBe(topologyReads);
      expect(sessionStorage.getItem(ACTIVE_CYCLE_RUN_STORAGE_KEY)).toBe("new-workspace-recovery");
      if (boundary !== "unmount") {
        expect(screen.queryByRole("alert")).not.toBeInTheDocument();
        expect(screen.getByLabelText("RabbitMQ topology graph")).toHaveTextContent(
          boundary === "selection" ? "Other topology" : "orders.events",
        );
        expect(screen.getByRole("combobox", { name: "Capture" })).toHaveValue(
          `static:${boundary === "selection" ? other.analysisId : original.analysisId}`,
        );
      }
    },
  );
});
