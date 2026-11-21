import { expect, test, type Locator, type Page, type Route } from "@playwright/test";
import type { TopologyResponse } from "../src/types/topology.js";
import { completedLayoutGeneration } from "./layout-support.js";

const REVISION_A = `sha256:${"a".repeat(64)}`;
const REVISION_B = `sha256:${"b".repeat(64)}`;

type Source = "static" | "session";
type OperationMode = "succeeded" | "running" | "missing";

interface Analysis {
  analysisId: string;
  label: string;
  source: Source;
  snapshotRevision: string;
  loadedAt: string;
  captureCompleteness: "complete" | "definitions_only";
  cluster: typeof CLUSTER;
  description: null;
  ownerContact: null;
  expiresAt: string | null;
}

interface MockApiState {
  catalogue: { static: Analysis[]; session: Analysis[] };
  topologies: Map<string, ReturnType<typeof topologyFor>>;
  fingerprints: Map<string, string>;
  topologyRequests: string[];
  uploadRequests: number;
  sccStarts: number;
  sccPolls: number;
  sccCancels: number;
  cycleStarts: number;
  cyclePolls: number;
  cycleCancels: number;
  cyclePageOffsets: number[];
  sccMode: OperationMode;
  cycleMode: OperationMode;
  lastSccRun: ReturnType<typeof sccRun> | null;
  lastCycleRun: ReturnType<typeof cycleRun> | null;
}

const CLUSTER = {
  clusterId: "cluster-release",
  clusterName: "rabbit@release",
  clusterTags: {},
  vhostCount: 2,
  nodeCount: 6,
  edgeCount: 5,
} as const;

function analysis(
  analysisId: string,
  label: string,
  source: Source,
  snapshotRevision: string,
  completeness: "complete" | "definitions_only" = "complete",
): Analysis {
  return {
    analysisId,
    label,
    source,
    snapshotRevision,
    loadedAt: "2026-08-13T08:00:00Z",
    captureCompleteness: completeness,
    cluster: CLUSTER,
    description: null,
    ownerContact: null,
    expiresAt: source === "session" ? "2026-08-13T16:00:00Z" : null,
  };
}

function topologyFor(summary: Analysis, exchangeName = "orders.events"): TopologyResponse {
  return {
    source: summary.source,
    analysisId: summary.analysisId,
    snapshotRevision: summary.snapshotRevision,
    cluster: summary.cluster,
    nodes: [
      {
        id: "n.exchange",
        type: "exchange",
        position: { x: 0, y: 0 },
        data: {
          kind: "exchange",
          name: exchangeName,
          vhost: "/orders",
          clusterId: "cluster-release",
          isMissing: false,
          exchangeType: "topic",
          durable: true,
          internal: false,
          isDefault: false,
        },
      },
      {
        id: "n.default",
        type: "exchange",
        position: { x: 0, y: 0 },
        data: {
          kind: "exchange",
          name: "",
          vhost: "/orders",
          clusterId: "cluster-release",
          isMissing: false,
          exchangeType: "direct",
          durable: true,
          internal: false,
          isDefault: true,
        },
      },
      {
        id: "n.orders",
        type: "queue",
        position: { x: 0, y: 0 },
        data: {
          kind: "queue",
          name: "Orders.Process",
          vhost: "/orders",
          clusterId: "cluster-release",
          isMissing: false,
          queueType: "quorum",
          durable: true,
        },
      },
      {
        id: "n.audit",
        type: "queue",
        position: { x: 0, y: 0 },
        data: {
          kind: "queue",
          name: "audit",
          vhost: "/audit",
          clusterId: "cluster-release",
          isMissing: false,
          queueType: "classic",
          durable: true,
        },
      },
      {
        id: "n.shovel",
        type: "shovel",
        position: { x: 0, y: 0 },
        data: {
          kind: "shovel",
          name: "orders-to-audit",
          vhost: "/orders",
          clusterId: "cluster-release",
          isMissing: false,
          source: {
            vhost: "/orders",
            resourceName: "Orders.Process",
            resourceKind: "queue",
            routingKey: null,
            isConfirmedLocal: true,
          },
          destination: {
            vhost: "/audit",
            resourceName: "audit",
            resourceKind: "queue",
            routingKey: null,
            isConfirmedLocal: true,
          },
          isCrossVhost: true,
          hasUnconfirmedEndpoint: false,
        },
      },
      {
        id: "m.dlx",
        type: "exchange",
        position: { x: 0, y: 0 },
        data: {
          kind: "exchange",
          name: "missing.dlx",
          vhost: "/orders",
          clusterId: "cluster-release",
          isMissing: true,
          exchangeType: null,
          durable: null,
          internal: null,
          isDefault: null,
        },
      },
    ],
    edges: [
      {
        id: "e.binding",
        source: "n.exchange",
        target: "n.orders",
        type: "binding",
        data: { kind: "binding", routingKey: "orders.*" },
      },
      {
        id: "e.dlx",
        source: "n.orders",
        target: "m.dlx",
        type: "dead_letter",
        data: { kind: "dead_letter", routingKey: null },
      },
      {
        id: "e.shovel.1",
        source: "n.orders",
        target: "n.shovel",
        type: "shovel",
        data: { kind: "shovel", routingKey: null },
      },
      {
        id: "e.shovel.2",
        source: "n.shovel",
        target: "n.audit",
        type: "shovel",
        data: { kind: "shovel", routingKey: null },
      },
      {
        id: "e.return",
        source: "n.audit",
        target: "n.orders",
        type: "dead_letter",
        data: { kind: "dead_letter", routingKey: null },
      },
    ],
  } as const;
}

function sccRun(summary: Analysis, status: "running" | "succeeded" | "cancelled") {
  return {
    runId: "10000000-0000-4000-8000-000000000001",
    operation: "cyclic_scc",
    status,
    source: summary.source,
    analysisId: summary.analysisId,
    snapshotRevision: summary.snapshotRevision,
    createdAt: "2026-08-13T09:00:00Z",
    startedAt: "2026-08-13T09:00:00Z",
    finishedAt: status === "running" ? null : "2026-08-13T09:00:01Z",
    heartbeatExpiresAt: status === "running" ? "2026-08-13T09:02:00Z" : null,
    resultExpiresAt: status === "succeeded" ? "2026-08-13T13:00:01Z" : null,
    error: null,
    result:
      status === "succeeded"
        ? {
            components: [
              {
                componentIndex: 0,
                isCyclic: true,
                nodeCount: 2,
                vhosts: ["/orders"],
                nodeIds: ["n.exchange", "n.orders"],
              },
              {
                componentIndex: 1,
                isCyclic: true,
                nodeCount: 3,
                vhosts: ["/orders", "/audit"],
                nodeIds: ["n.orders", "n.shovel", "n.audit"],
              },
            ],
          }
        : null,
  } as const;
}

function cycleRun(summary: Analysis, status: "running" | "succeeded" | "cancelled") {
  return {
    runId: "30000000-0000-4000-8000-000000000003",
    operation: "structural_cycles",
    status,
    source: summary.source,
    analysisId: summary.analysisId,
    snapshotRevision: summary.snapshotRevision,
    createdAt: "2026-08-13T10:00:00Z",
    startedAt: "2026-08-13T10:00:00Z",
    finishedAt: status === "running" ? null : "2026-08-13T10:00:01Z",
    heartbeatExpiresAt: status === "running" ? "2026-08-13T10:02:00Z" : null,
    resultExpiresAt: status === "succeeded" ? "2026-08-13T14:00:01Z" : null,
    error: null,
    result:
      status === "succeeded" ? { cycleCount: 52, truncated: true, maxCycles: 52 } : null,
  } as const;
}

function createState(): MockApiState {
  const first = analysis("orders-production", "Orders production", "static", REVISION_A);
  const second = analysis("audit-production", "Audit production", "static", REVISION_B);
  return {
    catalogue: { static: [first, second], session: [] },
    topologies: new Map([
      [`static:${first.analysisId}`, topologyFor(first)],
      [`static:${second.analysisId}`, topologyFor(second)],
    ]),
    fingerprints: new Map(),
    topologyRequests: [],
    uploadRequests: 0,
    sccStarts: 0,
    sccPolls: 0,
    sccCancels: 0,
    cycleStarts: 0,
    cyclePolls: 0,
    cycleCancels: 0,
    cyclePageOffsets: [],
    sccMode: "succeeded",
    cycleMode: "succeeded",
    lastSccRun: null,
    lastCycleRun: null,
  };
}

function errorBody(code: string, message: string) {
  return { error: { code, message, issues: [] } };
}

async function json(route: Route, body: unknown, status = 200, headers = {}) {
  await route.fulfill({
    status,
    contentType: "application/json",
    headers,
    body: JSON.stringify(body),
  });
}

function requestAnalysis(state: MockApiState, body: string | null): Analysis {
  const parsed = JSON.parse(body ?? "{}") as {
    source?: Source;
    analysisId?: string;
    snapshotRevision?: string;
  };
  const summary = [...state.catalogue.static, ...state.catalogue.session].find(
    (item) =>
      item.source === parsed.source &&
      item.analysisId === parsed.analysisId &&
      item.snapshotRevision === parsed.snapshotRevision,
  );
  if (!summary) throw new Error("Acceptance API received an unknown analysis request");
  return summary;
}

function multipartValue(body: string, name: string): string {
  const match = body.match(new RegExp(`name="${name}"\\r\\n\\r\\n([^\\r]+)`));
  return match?.[1] ?? "";
}

function definitionsFingerprint(body: string): string {
  const match = body.match(
    /name="definitions_file"; filename="[^"]+"\r\nContent-Type:[^\r]+\r\n\r\n([\s\S]*?)\r\n--/,
  );
  return match?.[1] ?? "unknown";
}

async function installMockApi(page: Page, state: MockApiState) {
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const method = request.method();

    if (method === "GET" && url.pathname === "/api/auth/session") {
      await json(route, {
        authMode: "none",
        authenticated: true,
        loginPath: null,
        logoutPath: null,
        csrfToken: null,
      });
      return;
    }
    if (method === "GET" && url.pathname === "/api/access") {
      await json(route, {
        authMode: "none",
        roles: ["viewer", "analyst"],
        capabilities: { viewTopology: true, runGraphOperations: true, manageUploads: true },
      });
      return;
    }
    if (method === "GET" && url.pathname === "/api/analyses") {
      await json(route, state.catalogue);
      return;
    }
    if (method === "GET" && url.pathname === "/api/topology") {
      const key = `${url.searchParams.get("source")}:${url.searchParams.get("analysis_id")}`;
      state.topologyRequests.push(key);
      const topology = state.topologies.get(key);
      if (!topology) {
        await json(route, errorBody("not_found", "The requested resource was not found."), 404);
      } else {
        await json(route, topology);
      }
      return;
    }
    if (method === "POST" && url.pathname === "/api/definitions/upload") {
      state.uploadRequests += 1;
      const body = request.postData() ?? "";
      const analysisId = multipartValue(body, "analysis_id");
      const label = multipartValue(body, "label") || analysisId;
      const fingerprint = definitionsFingerprint(body);
      const previous = state.catalogue.session.find((item) => item.analysisId === analysisId);
      const identical = previous !== undefined && state.fingerprints.get(analysisId) === fingerprint;
      const snapshotRevision = identical
        ? previous.snapshotRevision
        : `sha256:${String.fromCharCode(99 + state.uploadRequests).repeat(64)}`;
      const complete = body.includes('name="queues_file"') && body.includes('name="exchanges_file"');
      const uploaded = analysis(
        analysisId,
        label,
        "session",
        snapshotRevision,
        complete ? "complete" : "definitions_only",
      );
      uploaded.loadedAt = identical ? previous.loadedAt : `2026-08-13T11:00:0${state.uploadRequests}Z`;
      uploaded.expiresAt = `2026-08-13T${16 + state.uploadRequests}:00:00Z`;
      state.fingerprints.set(analysisId, fingerprint);
      state.catalogue.session = [
        ...state.catalogue.session.filter((item) => item.analysisId !== analysisId),
        uploaded,
      ];
      state.topologies.set(`session:${analysisId}`, topologyFor(uploaded));
      await json(route, { analysis: uploaded, replaced: previous !== undefined }, previous ? 200 : 201);
      return;
    }
    if (method === "DELETE" && url.pathname.startsWith("/api/analyses/")) {
      const analysisId = decodeURIComponent(url.pathname.slice("/api/analyses/".length));
      const before = state.catalogue.session.length;
      state.catalogue.session = state.catalogue.session.filter(
        (item) => item.analysisId !== analysisId,
      );
      state.topologies.delete(`session:${analysisId}`);
      await route.fulfill({ status: before === state.catalogue.session.length ? 404 : 204 });
      return;
    }
    if (method === "POST" && url.pathname === "/api/scc-runs") {
      state.sccStarts += 1;
      const selected = requestAnalysis(state, request.postData());
      if (state.sccMode === "missing") {
        state.catalogue.static = state.catalogue.static.filter(
          (item) => item.analysisId !== selected.analysisId,
        );
        state.topologies.delete(`${selected.source}:${selected.analysisId}`);
        await json(route, errorBody("not_found", "The requested resource was not found."), 404);
        return;
      }
      state.lastSccRun = sccRun(selected, state.sccMode);
      await json(route, state.lastSccRun, 202);
      return;
    }
    if (url.pathname.startsWith("/api/scc-runs/")) {
      if (!state.lastSccRun) throw new Error("SCC run was not started");
      if (method === "DELETE") {
        state.sccCancels += 1;
        state.lastSccRun = sccRun(
          [...state.catalogue.static, ...state.catalogue.session].find(
            (item) => item.analysisId === state.lastSccRun?.analysisId,
          )!,
          "cancelled",
        );
      } else {
        state.sccPolls += 1;
        state.lastSccRun = sccRun(
          [...state.catalogue.static, ...state.catalogue.session].find(
            (item) => item.analysisId === state.lastSccRun?.analysisId,
          )!,
          "succeeded",
        );
      }
      await json(route, state.lastSccRun);
      return;
    }
    if (method === "POST" && url.pathname === "/api/cycle-runs") {
      state.cycleStarts += 1;
      const selected = requestAnalysis(state, request.postData());
      state.lastCycleRun = cycleRun(selected, state.cycleMode === "missing" ? "running" : state.cycleMode);
      await json(route, state.lastCycleRun, 202);
      return;
    }
    if (/^\/api\/cycle-runs\/[^/]+$/.test(url.pathname)) {
      if (!state.lastCycleRun) throw new Error("Cycle run was not started");
      const selected = [...state.catalogue.static, ...state.catalogue.session].find(
        (item) => item.analysisId === state.lastCycleRun?.analysisId,
      )!;
      if (method === "DELETE") {
        state.cycleCancels += 1;
        state.lastCycleRun = cycleRun(selected, "cancelled");
      } else {
        state.cyclePolls += 1;
        state.lastCycleRun = cycleRun(selected, "succeeded");
      }
      await json(route, state.lastCycleRun);
      return;
    }
    if (/^\/api\/cycle-runs\/[^/]+\/cycles$/.test(url.pathname)) {
      const offset = Number(url.searchParams.get("offset") ?? "0");
      state.cyclePageOffsets.push(offset);
      const items =
        offset === 0
          ? [
              {
                cycleIndex: 0,
                length: 2,
                vhosts: ["/orders"],
                previewNodeIds: ["n.exchange", "n.orders"],
              },
              {
                cycleIndex: 1,
                length: 3,
                vhosts: ["/orders", "/audit"],
                previewNodeIds: ["n.orders", "n.shovel", "n.audit"],
              },
            ]
          : [
              {
                cycleIndex: 51,
                length: 3,
                vhosts: ["/orders", "/audit"],
                previewNodeIds: ["n.orders", "n.shovel", "n.audit"],
              },
            ];
      await json(route, {
        runId: state.lastCycleRun?.runId,
        snapshotRevision: state.lastCycleRun?.snapshotRevision,
        offset,
        limit: 50,
        total: 52,
        sort: "sdk",
        truncated: true,
        items,
      });
      return;
    }
    const detailMatch = url.pathname.match(/^\/api\/cycle-runs\/[^/]+\/cycles\/(\d+)$/);
    if (detailMatch) {
      const cycleIndex = Number(detailMatch[1]);
      await json(route, {
        runId: state.lastCycleRun?.runId,
        snapshotRevision: state.lastCycleRun?.snapshotRevision,
        cycleIndex,
        length: 3,
        vhosts: ["/orders", "/audit"],
        nodeIds: ["n.orders", "n.shovel", "n.audit"],
        edgeIds: ["e.shovel.1", "e.shovel.2", "e.return"],
      });
      return;
    }
    throw new Error(`Unhandled acceptance API request: ${method} ${url.pathname}${url.search}`);
  });
}

async function openReadyApp(page: Page, state: MockApiState) {
  await installMockApi(page, state);
  await page.goto("/");
  await expect(page.locator(".topology-canvas")).toHaveAttribute("data-layout-state", "ready");
  if (page.viewportSize()!.width <= 780) {
    await page.locator(".controls-disclosure > summary").click();
  }
}

async function viewportTransform(page: Page): Promise<string> {
  return page.locator(".react-flow__viewport").evaluate((element) => element.style.transform);
}

async function expectViewportUnchanged(page: Page, transform: string) {
  // Observe beyond the search animation duration so a delayed recenter cannot
  // pass on the first unchanged frame. This is the production React Flow DOM.
  const transforms = await page.locator(".react-flow__viewport").evaluate(async (element) => {
    const samples = new Set<string>();
    const end = performance.now() + 350;
    do {
      samples.add(element.style.transform);
      await new Promise(requestAnimationFrame);
    } while (performance.now() < end);
    return [...samples];
  });
  expect(transforms).toEqual([transform]);
}

async function expectSearchCentered(page: Page, count: number) {
  const matches = page.locator(".resource-node--search-match");
  await expect(matches).toHaveCount(count);
  await expect
    .poll(async () => {
      const canvas = await page.locator(".topology-canvas").boundingBox();
      const boxes = await matches.evaluateAll((elements) =>
        elements.map((element) => {
          const { x, y, width, height } = element.getBoundingClientRect();
          return { x, y, width, height };
        }),
      );
      const centerX =
        (Math.min(...boxes.map((box) => box.x)) +
          Math.max(...boxes.map((box) => box.x + box.width))) / 2;
      const centerY =
        (Math.min(...boxes.map((box) => box.y)) +
          Math.max(...boxes.map((box) => box.y + box.height))) / 2;
      return Math.hypot(
        centerX - canvas!.x - canvas!.width / 2,
        centerY - canvas!.y - canvas!.height / 2,
      );
    })
    .toBeLessThan(2);
}

async function panCanvas(page: Page) {
  const pane = (await page.locator(".react-flow__pane").boundingBox())!;
  await page.mouse.move(pane.x + 30, pane.y + pane.height - 90);
  await page.mouse.down();
  await page.mouse.move(pane.x + 130, pane.y + pane.height - 40, { steps: 10 });
  await page.mouse.up();
}

function relativeLuminance(color: string): number {
  const channels = color.match(/[\d.]+/g)?.slice(0, 3).map(Number) ?? [];
  if (channels.length !== 3) throw new Error(`Cannot parse computed color ${color}`);
  const [red, green, blue] = channels.map((value) => {
    const normalized = value / 255;
    return normalized <= 0.04045
      ? normalized / 12.92
      : ((normalized + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * red! + 0.7152 * green! + 0.0722 * blue!;
}

async function contrastRatio(locator: Locator): Promise<number> {
  const pair = await locator.evaluate((element) => {
    const style = getComputedStyle(element);
    return { foreground: style.color, background: style.backgroundColor };
  });
  const foreground = relativeLuminance(pair.foreground);
  const background = relativeLuminance(pair.background);
  return (Math.max(foreground, background) + 0.05) / (Math.min(foreground, background) + 0.05);
}

async function tabTo(page: Page, target: Locator, backwards = false) {
  for (let index = 0; index < 24; index += 1) {
    await page.keyboard.press(backwards ? "Shift+Tab" : "Tab");
    if (await target.evaluate((element) => element === document.activeElement)) return;
  }
  await expect(target).toBeFocused();
}

test("waits for the latest layout generation when an observed generation is superseded", async ({ page }) => {
  await page.setContent(`<div class="topology-canvas" data-layout-state="ready"
    data-layout-generation="2" data-layout-completed-generation="0"></div>`);
  const canvas = page.locator(".topology-canvas");
  const supersedeLayout = () => page.evaluate(() => {
    const element = document.querySelector(".topology-canvas")!;
    element.setAttribute("data-layout-generation", "3");
    element.setAttribute("data-layout-completed-generation", "3");
  });
  // Force a newer completed layout immediately after the helper observes the
  // pending generation, through either an individual read or a DOM snapshot.
  const readAttribute = canvas.getAttribute.bind(canvas);
  canvas.getAttribute = async (name, options) => {
    const value = await readAttribute(name, options);
    if (name === "data-layout-generation") await supersedeLayout();
    return value;
  };
  const evaluate = canvas.evaluate.bind(canvas);
  canvas.evaluate = async (callback, argument, options) => {
    const value = await evaluate(callback, argument, options);
    await supersedeLayout();
    return value;
  };
  const locate = page.locator.bind(page);
  page.locator = (selector, options) => selector === ".topology-canvas"
    ? canvas : locate(selector, options);

  expect(await completedLayoutGeneration(page)).toBe("3");
});

for (const viewport of [{ width: 1440, height: 900 }, { width: 1024, height: 768 }]) {
  test(`exposes inspection and completed results without traversing the drawing at ${viewport.width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize(viewport);
    const state = createState();
    await openReadyApp(page, state);
    const sidebar = page.getByRole("complementary", { name: "Topology exploration controls" });
    await expect(sidebar.getByRole("button", { name: "Return to graph", exact: true })).toHaveCount(0);
    const generation = await completedLayoutGeneration(page);
    const transform = await viewportTransform(page);
    const toggle = page.getByRole("button", { name: "Results & inspection" });
    await toggle.click();
    const resource = page.getByRole("button", { name: "Inspect queue Orders.Process, vhost /orders", exact: true });
    await resource.click();
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
    await expect(page.getByRole("tab", { name: "Inspection", exact: true })).toHaveAttribute("aria-selected", "true");
    const detailsControl = page.getByRole("button", { name: "Details: queue Orders.Process, vhost /orders", exact: true });
    await expect(detailsControl).toBeInViewport();
    await expect(page.getByRole("region", { name: "Safe details" })).toContainText("Orders.Process");
    await toggle.click();
    const edge = page.getByRole("button", { name: /^Inspect shovel relationship/ }).first();
    await edge.focus();
    await page.keyboard.press("Enter");
    await expect(edge).toBeFocused();
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
    await expect(page.getByRole("button", { name: /^Details: relationship/ })).toBeInViewport();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("button", { name: /^Details: relationship/ })).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(page.getByRole("tab", { name: "Inspection", exact: true })).toBeFocused();

    for (const [startLabel, openLabel, tab, status, count] of [
      ["Run cyclic components", "Open components", "Components", "Cyclic-component operation status", "2 cyclic components found."],
      ["Run structural cycles", "Open cycles", "Cycles", "Structural-cycle operation status", "52 structural cycles found."],
    ]) {
      if (await toggle.getAttribute("aria-expanded") === "true") await toggle.click();
      const start = page.getByRole("button", { name: startLabel, exact: true });
      await start.focus();
      await page.keyboard.press("Enter");
      await expect(page.getByRole("status", { name: status })).toContainText(count);
      await expect(start).toBeFocused();
      await expect(toggle).toHaveAttribute("aria-expanded", "false");
      await page.keyboard.press("Tab");
      await expect(page.getByRole("button", { name: openLabel, exact: true })).toBeFocused();
      await page.keyboard.press("Enter");
      const activeTab = page.getByRole("tab", { name: tab, exact: true });
      await expect(activeTab).toBeFocused();
      await expect(activeTab).toBeInViewport();
      await page.keyboard.press("ArrowLeft");
      await page.keyboard.press("ArrowRight");
      await expect(activeTab).toBeFocused();
      await page.keyboard.press("Tab");
      await expect(page.locator(".drawer-content").getByRole("button", { name: "Return to graph", exact: true })).toBeFocused();
      await page.keyboard.press("Enter");
      await expect(toggle).toHaveAttribute("aria-expanded", "false");
      await expect(page.getByRole("button", { name: "Return to graph", exact: true })).toHaveCount(0);
      await expect(page.getByRole("button", { name: /^Details: relationship/ })).toBeFocused();
    }
    await page.getByRole("button", { name: "Components (2)", exact: true }).click();
    await expect(page.getByRole("region", { name: "Cyclic components" })).toContainText("2 cyclic components found.");
    await page.keyboard.press("ArrowRight");
    await expect(page.getByRole("region", { name: "Structural cycles" })).toContainText("52 structural cycles found.");
    expect(await completedLayoutGeneration(page)).toBe(generation);
    await expectViewportUnchanged(page, transform);
    expect(state.topologyRequests).toHaveLength(1);
    await page.screenshot({ path: testInfo.outputPath("accessible-result-tabs.png") });
  });
}

test("puts the initial narrow-screen graph and capture evidence within reach", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const state = createState();
  state.catalogue.static[0]!.captureCompleteness = "definitions_only";
  await installMockApi(page, state);
  await page.goto("/");
  await completedLayoutGeneration(page);
  const disclosure = page.locator(".controls-disclosure > summary");
  await expect(disclosure).toBeInViewport();
  await expect(page.locator(".analysis-picker .snapshot-status")).toContainText("Definitions only");
  await expect(page.locator(".analysis-picker .snapshot-status")).toContainText("6 nodes · 5 edges");
  const intersectsScreen = () => page.locator(".resource-node").evaluateAll((nodes) =>
    nodes.some((node) => {
      const box = node.getBoundingClientRect();
      return box.bottom > 0 && box.top < innerHeight && box.left < innerWidth && box.right > 0;
    }));
  expect(await intersectsScreen()).toBe(true);
  await expect(page.locator(".canvas-region > .legend")).toBeInViewport();
  await page.screenshot({ path: testInfo.outputPath("narrow-initial-graph.png") });
  await disclosure.focus();
  await page.keyboard.press("Enter");
  const filter = page.getByRole("checkbox", { name: "Hide default exchanges" });
  await tabTo(page, filter);
  await page.keyboard.press("Space");
  await completedLayoutGeneration(page);
  await expect(page.locator('.react-flow__node[data-id="n.default"]')).toHaveCount(0);
  await tabTo(page, page.locator(".operations-sidebar").getByRole("button", { name: "Return to graph", exact: true }), true);
  await page.keyboard.press("Enter");
  await expect(page.getByRole("button", { name: "Open inspection", exact: true })).toBeFocused();
  expect(await intersectsScreen()).toBe(true);
  // A return can be activated before the browser delivers the opening
  // disclosure's asynchronous toggle event.
  const opened = await disclosure.evaluate((summary) => {
    const controls = summary.closest("details")!;
    (summary as HTMLElement).click();
    const opened = controls.open;
    controls.querySelector<HTMLButtonElement>(".operations-sidebar .text-button")!.click();
    return opened;
  });
  expect(opened).toBe(true);
  await expect(disclosure.locator("..")).not.toHaveAttribute("open");
  await expect(page.getByRole("button", { name: "Open inspection", exact: true })).toBeFocused();
  expect(await intersectsScreen()).toBe(true);
  const transform = await viewportTransform(page);
  await panCanvas(page);
  await expect.poll(() => viewportTransform(page)).not.toBe(transform);
  await page.getByRole("button", { name: /zoom in/i }).click();
  await page.getByRole("button", { name: "Fit view", exact: true }).click();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.setViewportSize({ width: 1024, height: 768 });
  await page.reload();
  await completedLayoutGeneration(page);
  await expect(page.locator(".analysis-picker .snapshot-status")).toBeInViewport();
  await expect(page.locator(".analysis-picker .snapshot-status")).toContainText("static");
  await expect(page.locator(".analysis-picker .snapshot-status")).toContainText("Definitions only");
});

test("selects static snapshots and inspects graph resources with the keyboard", async ({ page }) => {
  const state = createState();
  await openReadyApp(page, state);

  const picker = page.getByRole("combobox", { name: "Analysis" });
  await expect(picker).toHaveValue("static:orders-production");
  await picker.selectOption("static:audit-production");
  await expect(picker).toHaveValue("static:audit-production");
  await expect(page.locator(".topology-canvas")).toHaveAttribute("data-layout-state", "ready");

  const queue = page.getByRole("button", { name: "Inspect queue Orders.Process, vhost /orders" });
  await queue.focus();
  await page.keyboard.press("Enter");
  const details = page.getByRole("region", { name: "Safe details" });
  await expect(details.getByText("Orders.Process", { exact: true })).toBeVisible();
  const shovelEdge = page.getByRole("button", { name: /^Inspect shovel relationship/ }).first();
  await shovelEdge.focus();
  await page.keyboard.press("Space");
  await expect(details.getByText("Relationship kind", { exact: true }).locator("..").locator("dd")).toHaveText("shovel");

  await expect(page.getByText(/offline configuration snapshot/i)).toContainText(
    "Configured paths do not guarantee message delivery",
  );
  expect(await contrastRatio(page.getByRole("button", { name: "Reset layout" }))).toBeGreaterThanOrEqual(
    4.5,
  );
  expect(await contrastRatio(page.locator(".brand-mark"))).toBeGreaterThanOrEqual(4.5);
  expect(await contrastRatio(page.locator(".snapshot-notice"))).toBeGreaterThanOrEqual(4.5);
  expect(state.topologyRequests).toEqual([
    "static:orders-production",
    "static:audit-production",
  ]);
});

test.describe("selected snapshot provenance", () => {
  test.use({ timezoneId: "Pacific/Honolulu" });

  test("formats UTC provenance and updates the revision when equally labelled snapshots change", async ({ page }) => {
    const state = createState();
    const first = state.catalogue.static[0]!;
    const second = state.catalogue.static[1]!;
    second.label = first.label;
    second.loadedAt = "2026-08-14T10:30:45.123456Z";
    await openReadyApp(page, state);

    const picker = page.getByRole("combobox", { name: "Analysis" });
    const provenance = page.getByRole("group", { name: "Selected snapshot" });
    await expect(picker).toHaveAccessibleDescription("Loaded in app: 2026-08-13 08:00:00 UTC");
    await expect(provenance.locator("time")).toHaveAttribute("datetime", first.loadedAt);
    const summary = provenance.locator("summary");
    await expect(summary).toHaveText("Revision: aaaaaaaaaaaa…");
    await expect(provenance.getByText(first.snapshotRevision, { exact: true })).toBeHidden();
    await picker.focus();
    await page.keyboard.press("Tab");
    await expect(summary).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(provenance.getByText(first.snapshotRevision, { exact: true })).toBeVisible();
    expect(await summary.evaluate((element) => getComputedStyle(element).outlineStyle)).not.toBe("none");

    await picker.selectOption("static:audit-production");
    await expect(provenance.locator("time")).toHaveText("2026-08-14 10:30:45 UTC");
    await expect(provenance.locator("time")).toHaveAttribute("datetime", second.loadedAt);
    await expect(provenance.getByText(second.snapshotRevision, { exact: true })).toBeVisible();
    await expect(provenance.getByText(first.snapshotRevision, { exact: true })).toHaveCount(0);
    await expect(page.locator(".topology-canvas")).toHaveAttribute("data-layout-state", "ready");
  });

  for (const width of [1440, 1024, 390, 320]) {
    test(`keeps load time and full revision accessible near the picker at ${width}px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 });
      const state = createState();
      const first = state.catalogue.static[0]!;
      await openReadyApp(page, state);

      const picker = page.getByRole("combobox", { name: "Analysis" });
      const provenance = page.getByRole("group", { name: "Selected snapshot" });
      await expect(provenance).toBeVisible();
      await expect(provenance.locator("time")).toBeVisible();
      await provenance.locator("summary").click();
      await expect(provenance.getByText(first.snapshotRevision, { exact: true })).toBeVisible();

      const pickerBox = (await picker.boundingBox())!;
      const metadataBox = (await provenance.boundingBox())!;
      expect(metadataBox.x).toBeGreaterThanOrEqual(0);
      expect(metadataBox.x + metadataBox.width).toBeLessThanOrEqual(width);
      expect(metadataBox.y).toBeGreaterThanOrEqual(pickerBox.y + pickerBox.height);
      expect(metadataBox.y - pickerBox.y - pickerBox.height).toBeLessThan(12);
      expect(await provenance.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true);
      const contrast = await provenance.evaluate((element) => ({
        foreground: getComputedStyle(element).color,
        background: getComputedStyle(element.closest("header")!).backgroundColor,
      }));
      const foreground = relativeLuminance(contrast.foreground);
      const background = relativeLuminance(contrast.background);
      expect((Math.max(foreground, background) + 0.05) / (Math.min(foreground, background) + 0.05)).toBeGreaterThanOrEqual(4.5);

      await page.screenshot({ path: test.info().outputPath("snapshot-provenance.png") });
    });
  }
});

for (const reducedMotion of ["reduce", "no-preference"] as const) {
  test(`preserves a panned search viewport during selection and drag (${reducedMotion})`, async ({
    page,
  }) => {
    await page.emulateMedia({ reducedMotion });
    const state = createState();
    await openReadyApp(page, state);
    const generation = await completedLayoutGeneration(page);
    const topologyRequests = state.topologyRequests.length;
    const search = page.getByRole("searchbox", { name: "Resource name" });
    await search.fill("Orders");
    await expectSearchCentered(page, 3);
    // Let the optional 250ms animation finish before the user pans.
    await page.waitForTimeout(300);
    const centered = await viewportTransform(page);
    await panCanvas(page);
    const panned = await viewportTransform(page);
    expect(panned).not.toBe(centered);

    const queue = page.getByRole("button", { name: "Inspect queue Orders.Process, vhost /orders" });
    await queue.focus();
    await expectViewportUnchanged(page, panned);
    await page.keyboard.press("Enter");
    await expect(queue).toHaveClass(/resource-node--selected/);
    await expectViewportUnchanged(page, panned);

    const exchange = page.getByRole("button", { name: "Inspect exchange orders.events, vhost /orders" });
    await exchange.click();
    await expect(exchange).toHaveClass(/resource-node--selected/);
    await expectViewportUnchanged(page, panned);

    const positionBefore = await queue.boundingBox();
    await page.mouse.move(positionBefore!.x + 35, positionBefore!.y + 30);
    await page.mouse.down();
    await page.mouse.move(positionBefore!.x + 60, positionBefore!.y + 50, { steps: 10 });
    await page.mouse.up();
    const positionAfter = await queue.boundingBox();
    expect(positionAfter!.x - positionBefore!.x).toBeGreaterThan(15);
    expect(positionAfter!.y - positionBefore!.y).toBeGreaterThan(10);
    await expectViewportUnchanged(page, panned);
    expect(await completedLayoutGeneration(page)).toBe(generation);
    expect(state.topologyRequests).toHaveLength(topologyRequests);

    // A new query centers the matches at their current, user-moved positions.
    await search.fill("ORDERS.PRO");
    await expectSearchCentered(page, 1);
    expect(await completedLayoutGeneration(page)).toBe(generation);
    expect(state.topologyRequests).toHaveLength(topologyRequests);
  });
}

test("centers active search matches after visibility layouts, cached restoration, and reset", async ({
  page,
}) => {
  const state = createState();
  await openReadyApp(page, state);
  let generation = await completedLayoutGeneration(page);
  const topologyRequests = state.topologyRequests.length;
  await page.getByRole("searchbox", { name: "Resource name" }).fill("Orders");
  await expectSearchCentered(page, 3);

  const queues = page.getByRole("checkbox", { name: "Queues ▤" });
  for (const visible of [false, true]) {
    await queues.setChecked(visible);
    await expect(page.locator(".topology-canvas")).not.toHaveAttribute(
      "data-layout-generation",
      generation,
    );
    generation = await completedLayoutGeneration(page);
    await expectSearchCentered(page, visible ? 3 : 2);
  }

  await panCanvas(page);
  await page.getByRole("button", { name: "Reset layout" }).click();
  await expect(page.locator(".topology-canvas")).not.toHaveAttribute(
    "data-layout-generation",
    generation,
  );
  await completedLayoutGeneration(page);
  await expectSearchCentered(page, 3);
  expect(state.topologyRequests).toHaveLength(topologyRequests);
});

test("uploads both evidence modes, preserves an identical refresh, replaces, and deletes", async ({
  page,
}) => {
  const state = createState();
  await openReadyApp(page, state);
  const opener = page.getByRole("button", { name: "Upload capture" });

  await opener.focus();
  await page.keyboard.press("Enter");
  const dialog = page.getByRole("dialog", { name: "Manage captured analysis" });
  await expect(dialog.getByRole("button", { name: "Close" })).toBeFocused();
  await dialog.getByLabel("Analysis ID").fill("session-demo");
  await dialog.getByLabel("Display label (optional)").fill("Session demo");
  await dialog.getByLabel("Definitions export").setInputFiles({
    name: "definitions.json",
    mimeType: "application/json",
    buffer: Buffer.from('{"capture":"version-one"}'),
  });
  await dialog.getByLabel("Analysis ID").fill("session-demo-");
  await dialog.getByRole("button", { name: "Upload capture" }).press("Enter");
  await expect(dialog.getByLabel("Analysis ID")).toBeFocused();
  await expect(dialog.getByLabel("Analysis ID")).toHaveAccessibleDescription(/first and last characters must be letters or digits/);
  expect(await dialog.getByLabel("Definitions export").evaluate((input: HTMLInputElement) => input.files?.[0]?.name)).toBe("definitions.json");
  expect(state.uploadRequests).toBe(0);
  await dialog.getByLabel("Analysis ID").fill("session-demo");
  await dialog.getByRole("button", { name: "Upload capture" }).focus();
  await page.keyboard.press("Enter");
  await expect(dialog.getByText("Uploaded Session demo.")).toBeVisible();
  await dialog.getByRole("button", { name: "Close" }).focus();
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(page.getByRole("button", { name: "Upload capture" })).toBeFocused();
  await expect(page.getByText("Definitions only")).toBeVisible();
  const initial = state.catalogue.session[0]!;
  const provenance = page.getByRole("group", { name: "Selected snapshot" });
  await expect(provenance.locator("time")).toHaveAttribute("datetime", initial.loadedAt);
  await expect(provenance.locator("summary")).toHaveText("Revision: dddddddddddd…");

  await opener.click();
  await expect(dialog.getByRole("button", { name: "Close" })).toBeFocused();
  await expect(dialog.getByRole("button", { name: "Replace capture" })).toBeEnabled();
  await expect(dialog.getByText("Replace session capture: Session demo (session-demo).")).toBeVisible();
  await expect(dialog.getByText(/Changed effective inputs replace its topology and clear retained results/)).toBeVisible();
  await dialog.getByLabel("Definitions export").setInputFiles({
    name: "definitions.json",
    mimeType: "application/json",
    buffer: Buffer.from('{"capture":"version-two"}'),
  });
  await dialog.getByLabel("Queue observations").setInputFiles({
    name: "queues.json",
    mimeType: "application/json",
    buffer: Buffer.from("[]"),
  });
  await dialog.getByLabel("Exchange observations").setInputFiles({
    name: "exchanges.json",
    mimeType: "application/json",
    buffer: Buffer.from("[]"),
  });
  await dialog.getByRole("button", { name: "Replace capture" }).press("Enter");
  await expect(dialog.getByText("Replaced Session demo.")).toBeVisible();
  await dialog.getByRole("button", { name: "Close" }).click();
  await expect(page.getByText("Complete capture")).toBeVisible();
  const changed = state.catalogue.session[0]!;
  expect(changed.snapshotRevision).not.toBe(initial.snapshotRevision);
  await expect(provenance.locator("time")).toHaveText("2026-08-13 11:00:02 UTC");
  await expect(provenance.locator("time")).toHaveAttribute("datetime", changed.loadedAt);
  await provenance.locator("summary").press("Enter");
  await expect(provenance.getByText(changed.snapshotRevision, { exact: true })).toBeVisible();
  const changedMetadata = await provenance.textContent();
  const topologyRequestsAfterChange = state.topologyRequests.length;

  await opener.click();
  await expect(dialog.getByRole("button", { name: "Close" })).toBeFocused();
  await dialog.getByLabel("Definitions export").setInputFiles({
    name: "definitions.json",
    mimeType: "application/json",
    buffer: Buffer.from('{"capture":"version-two"}'),
  });
  await dialog.getByLabel("Queue observations").setInputFiles({
    name: "queues.json",
    mimeType: "application/json",
    buffer: Buffer.from("[]"),
  });
  await dialog.getByLabel("Exchange observations").setInputFiles({
    name: "exchanges.json",
    mimeType: "application/json",
    buffer: Buffer.from("[]"),
  });
  await dialog.getByRole("button", { name: "Replace capture" }).press("Enter");
  await expect(dialog.getByText("Replaced Session demo.")).toBeVisible();
  const identical = state.catalogue.session[0]!;
  expect(identical.snapshotRevision).toBe(changed.snapshotRevision);
  expect(identical.loadedAt).toBe(changed.loadedAt);
  expect(identical.expiresAt).not.toBe(changed.expiresAt);
  expect(state.topologyRequests).toHaveLength(topologyRequestsAfterChange);

  await dialog.getByRole("button", { name: "Close" }).click();
  await expect(provenance).toHaveText(changedMetadata!);
  await expect(provenance.locator("time")).toHaveAttribute("datetime", changed.loadedAt);
  await expect(provenance.getByText(changed.snapshotRevision, { exact: true })).toBeVisible();
  await opener.click();
  await expect(dialog.getByRole("button", { name: "Close" })).toBeFocused();
  await dialog.getByRole("button", { name: "Delete capture" }).focus();
  await page.keyboard.press("Enter");
  await expect(dialog).toBeHidden();
  await expect(page.getByRole("combobox", { name: "Analysis" })).toHaveValue(
    "static:orders-production",
  );
  expect(state.catalogue.session).toHaveLength(0);
});

test("retains SCC and cycle results, pages, and focuses a cross-vhost shovel path", async ({
  page,
}) => {
  const state = createState();
  await openReadyApp(page, state);
  const topologyRequests = state.topologyRequests.length;

  const startScc = page.getByRole("button", { name: "Run cyclic components" });
  await startScc.focus();
  await page.keyboard.press("Enter");
  const canvas = page.locator(".topology-canvas");
  const componentResults = page.getByRole("region", { name: "Cyclic components" });
  await expect(componentResults.getByText("C1", { exact: true })).toBeVisible();
  await expect(componentResults.getByText("C2", { exact: true })).toBeVisible();
  const firstComponent = page.getByRole("checkbox", { name: /Cyclic component 1/ });
  await firstComponent.check();
  await completedLayoutGeneration(page);
  await expect(canvas.locator(".resource-node--scc.scc-tone-0")).toHaveCount(2);
  await expect(canvas.getByText(/^C\d+$/)).toHaveCount(0);
  await expect(canvas.locator(".resource-node__component")).toHaveCount(0);
  const exchange = page.getByRole("button", { name: "Inspect exchange orders.events, vhost /orders" });
  await exchange.click();
  await expect(exchange).toHaveClass(/resource-node--selected/);
  await page.getByRole("tab", { name: "Components", exact: true }).click();
  const crossVhostComponent = page.getByRole("checkbox", { name: /Cyclic component 2/ });
  await crossVhostComponent.focus();
  await page.keyboard.press("Space");
  await expect(page.getByRole("button", { name: "Clear component focus" })).toBeVisible();
  await expect(canvas.locator(".resource-node--scc")).toHaveCount(4);
  await expect(canvas.locator(".resource-node--scc.scc-tone-0")).toHaveCount(1);
  await expect(canvas.locator(".resource-node--scc.scc-tone-1")).toHaveCount(3);
  await expect(canvas.getByText(/^C\d+$/)).toHaveCount(0);
  const crossVhostResult = componentResults.locator(".scc-component-row").nth(1);
  await crossVhostResult.getByText("Show complete membership").click();
  await expect(crossVhostResult.getByText("shovel orders-to-audit in /orders", { exact: true })).toBeVisible();

  const startCycles = page.getByRole("button", { name: "Run structural cycles" });
  await startCycles.focus();
  await page.keyboard.press("Enter");
  const cycle = page.getByRole("button", { name: /Cycle 2/ });
  await cycle.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("article", { name: "Cycle 2 path" })).toContainText(
    "shovel orders-to-audit in /orders",
  );
  await expect(page.getByRole("button", { name: "Inspect shovel orders-to-audit, vhost /orders" })).toBeVisible();
  await expect(page.locator(".topology-edge--cycle-animated")).toHaveCount(0);
  await completedLayoutGeneration(page);
  const nodeSteps = await canvas.locator(".resource-node__cycle-step").allTextContents();
  const edgeSteps = await canvas.locator(".edge-inspect-button").allTextContents();
  expect(nodeSteps.sort()).toEqual(["P1", "P2", "P3"]);
  expect(edgeSteps.sort()).toEqual(["P1", "P2", "P3"]);
  await expect(canvas.getByText(/^C\d+$/)).toHaveCount(0);
  await page.getByRole("tab", { name: "Components", exact: true }).click();
  await expect(componentResults.getByText("C1", { exact: true })).toBeVisible();
  await expect(componentResults.getByText("C2", { exact: true })).toBeVisible();
  await page.getByRole("tab", { name: "Cycles", exact: true }).click();
  expect(state.topologyRequests).toHaveLength(topologyRequests);

  const next = page.getByRole("button", { name: "Next" });
  await next.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("button", { name: /Cycle 52/ })).toBeVisible();
  expect(state.cyclePageOffsets).toContain(50);

  const clearCycle = page.getByRole("button", { name: "Clear cycle focus" });
  await clearCycle.focus();
  await page.keyboard.press("Enter");
  await page.getByRole("tab", { name: "Components", exact: true }).click();
  await expect(page.getByRole("button", { name: "Clear component focus" })).toBeVisible();
  await page.getByRole("button", { name: "Clear component focus" }).press("Enter");
  expect(state.sccStarts).toBe(1);
  expect(state.cycleStarts).toBe(1);
});

test("reloads an active run without a duplicate start", async ({ page }) => {
  const state = createState();
  state.sccMode = "running";
  await openReadyApp(page, state);

  await page.getByRole("button", { name: "Run cyclic components" }).click();
  await expect(
    page.getByRole("status", { name: "Cyclic-component operation status" }),
  ).toContainText("running");
  await page.reload();

  await expect(
    page
      .getByRole("region", { name: "Cyclic components" })
      .getByText("2 cyclic components found."),
  ).toBeVisible();
  expect(state.sccStarts).toBe(1);
  expect(state.sccPolls).toBeGreaterThanOrEqual(1);
});

test("cancels active work and reconciles an expired snapshot", async ({ page }) => {
  const state = createState();
  state.cycleMode = "running";
  await openReadyApp(page, state);

  await page.getByRole("button", { name: "Run structural cycles" }).press("Enter");
  const cancel = page.getByRole("button", { name: "Cancel analysis" });
  await cancel.focus();
  await page.keyboard.press("Enter");
  await expect(
    page.getByRole("status", { name: "Structural-cycle operation status" }),
  ).toContainText("cancelled");
  expect(state.cycleCancels).toBe(1);

  state.sccMode = "missing";
  await page.getByRole("button", { name: "Run cyclic components" }).press("Enter");
  await expect(page.getByRole("combobox", { name: "Analysis" })).toHaveValue(
    "static:audit-production",
  );
  await expect(page.locator(".topology-canvas")).toHaveAttribute("data-layout-state", "ready");
});

function edgeGeometryState() {
  const state = createState();
  const selected = state.catalogue.static[0]!;
  const topology = topologyFor(selected);
  const edges: TopologyResponse["edges"] = [
    { id: "e.geometry.alpha", source: "n.exchange", target: "n.default", type: "binding", data: { kind: "binding", routingKey: "alpha" } },
    { id: "e.geometry.beta", source: "n.exchange", target: "n.default", type: "binding", data: { kind: "binding", routingKey: "beta" } },
    { id: "e.geometry.alternate", source: "n.exchange", target: "n.default", type: "alternate_exchange", data: { kind: "alternate_exchange", routingKey: null } },
    { id: "e.geometry.return", source: "n.default", target: "n.exchange", type: "binding", data: { kind: "binding", routingKey: "return" } },
    { id: "e.geometry.loop", source: "n.exchange", target: "n.exchange", type: "binding", data: { kind: "binding", routingKey: "self" } },
  ];
  state.topologies.set(`static:${selected.analysisId}`, {
    ...topology,
    nodes: topology.nodes.map((node) => node.id === "n.default" && node.data.kind === "exchange"
      ? { ...node, data: { ...node.data, name: "peer.events", isDefault: false } }
      : node),
    edges,
  });
  return { state, edges, selected };
}

async function renderedEdgeGeometry(page: Page, edges: TopologyResponse["edges"]) {
  return page.locator(".topology-canvas").evaluate((canvas, edges) => {
    const center = (element: Element) => {
      const rect = element.getBoundingClientRect();
      return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
    };
    return edges.map((edge) => {
      const path = canvas.querySelector<SVGPathElement>(`.react-flow__edge[data-id="${edge.id}"] .topology-edge`)!;
      const inspector = canvas.querySelector<HTMLButtonElement>(`.edge-inspect-button[data-edge-id="${edge.id}"]`)!;
      const source = canvas.querySelector(`.react-flow__node[data-id="${edge.source}"]`)!;
      const target = canvas.querySelector(`.react-flow__node[data-id="${edge.target}"]`)!;
      const matrix = path.getScreenCTM()!;
      const pointAt = (length: number) => {
        const point = path.getPointAtLength(length);
        const screen = new DOMPoint(point.x, point.y).matrixTransform(matrix);
        return { x: screen.x, y: screen.y };
      };
      const length = path.getTotalLength();
      const end = path.getPointAtLength(length);
      const approach = path.getPointAtLength(Math.max(0, length - 24));
      const button = center(inspector);
      const hit = document.elementFromPoint(button.x, button.y);
      const body = source.querySelector(".resource-node")!.getBoundingClientRect();
      const samples = Array.from({ length: 201 }, (_, index) => pointAt(index * length / 200));
      return {
        id: edge.id,
        path: path.getAttribute("d"),
        marker: path.getAttribute("marker-end"),
        markerUnits: canvas.querySelector("marker")?.getAttribute("markerUnits"),
        approach: { x: end.x - approach.x, y: end.y - approach.y },
        start: pointAt(0),
        end: pointAt(length),
        beforeEnd: pointAt(Math.max(0, length - 2)),
        sourceHandle: {
          ...center(source.querySelector(".react-flow__handle.source")!),
          x: source.querySelector(".react-flow__handle.source")!.getBoundingClientRect().right,
        },
        targetHandle: {
          ...center(target.querySelector(".react-flow__handle.target")!),
          x: target.querySelector(".react-flow__handle.target")!.getBoundingClientRect().left,
        },
        button,
        pointerReachable: hit === inspector || (hit !== null && inspector.contains(hit)),
        hit: hit?.outerHTML.slice(0, 250),
        label: inspector.getAttribute("aria-label"),
        selfLoopOutsideBody: !samples.some((point) => point.x > body.left + 1 && point.x < body.right - 1
          && point.y > body.top + 1 && point.y < body.bottom - 1),
        topBend: Math.min(...samples.map((point) => point.y)),
        bottomBend: Math.max(...samples.map((point) => point.y)),
        nodeTop: body.top,
        nodeBottom: body.bottom,
        scale: Math.hypot(matrix.a, matrix.b),
      };
    });
  }, edges);
}

test("draws distinct parallel, reciprocal and self-loop paths with individually reachable inspectors after drag", async ({ page }, testInfo) => {
  const { state, edges } = edgeGeometryState();
  await openReadyApp(page, state);
  const generation = await completedLayoutGeneration(page);
  const viewport = await viewportTransform(page);
  const before = await renderedEdgeGeometry(page, edges);
  await page.screenshot({ path: testInfo.outputPath("edge-geometry.png") });
  expect(new Set(before.map((edge) => edge.path)).size).toBe(edges.length);
  expect(new Set(before.map((edge) => edge.label)).size).toBe(edges.length);
  for (const edge of before) expect(edge.pointerReachable, JSON.stringify(edge)).toBe(true);
  for (const edge of before) expect(edge.markerUnits).toBe("userSpaceOnUse");
  const loop = before.find((edge) => edge.id === "e.geometry.loop")!;
  expect(loop.selfLoopOutsideBody).toBe(true);
  // An obstructed upper lane can require a lower loop. Measure its clearance
  // in graph coordinates so fit-to-view zoom does not change this assertion.
  expect(Math.max(loop.nodeTop - loop.topBend, loop.bottomBend - loop.nodeBottom) / loop.scale)
    .toBeGreaterThanOrEqual(15);
  const details = page.getByRole("region", { name: "Safe details" });
  const inspectors = page.locator(".edge-inspect-button");
  for (const edge of edges) {
    const inspector = page.locator(`.edge-inspect-button[data-edge-id="${edge.id}"]`);
    await inspector.click();
    await expect(details.getByText(edge.id, { exact: true })).toBeVisible();
    if (edge.data.routingKey) await expect(details.getByText(edge.data.routingKey, { exact: true })).toBeVisible();
  }
  // React Flow can reorder selected edges by elevation. Check the current
  // native tab sequence, then activate each exact wire ID independently.
  const tabOrder = await inspectors.evaluateAll((buttons) =>
    buttons.map((button) => button.getAttribute("data-edge-id")));
  await inspectors.first().focus();
  for (const [index, id] of tabOrder.entries()) {
    await expect(page.locator(`.edge-inspect-button[data-edge-id="${id}"]`)).toBeFocused();
    if (index < tabOrder.length - 1) await page.keyboard.press("Tab");
  }
  for (const [index, edge] of edges.entries()) {
    const inspector = page.locator(`.edge-inspect-button[data-edge-id="${edge.id}"]`);
    await inspector.focus();
    await page.keyboard.press(index % 2 ? "Space" : "Enter");
    await expect(details.getByText(edge.id, { exact: true })).toBeVisible();
    await expect(inspector).toBeFocused();
  }
  await expectViewportUnchanged(page, viewport);
  const node = page.getByRole("button", { name: "Inspect exchange orders.events, vhost /orders" });
  const position = (await node.boundingBox())!;
  await page.mouse.move(position.x + 35, position.y + 30);
  await page.mouse.down();
  await page.mouse.move(position.x + 60, position.y + 45, { steps: 10 });
  await page.mouse.up();
  await expect.poll(async () => (await node.boundingBox())!.x - position.x).toBeGreaterThan(10);
  await expect.poll(async () => (await renderedEdgeGeometry(page, edges)).every((edge) =>
    Math.hypot(edge.start.x - edge.sourceHandle.x, edge.start.y - edge.sourceHandle.y) < 2 &&
    Math.hypot(edge.end.x - edge.targetHandle.x, edge.end.y - edge.targetHandle.y) < 2,
  )).toBe(true);
  const after = await renderedEdgeGeometry(page, edges);
  for (const [index, edge] of after.entries()) {
    expect(edge.path, edge.id).not.toBe(before[index]!.path);
    expect(edge.marker).toBeTruthy();
    expect(Math.hypot(edge.start.x - edge.sourceHandle.x, edge.start.y - edge.sourceHandle.y), JSON.stringify(edge)).toBeLessThan(2);
    expect(Math.hypot(edge.end.x - edge.targetHandle.x, edge.end.y - edge.targetHandle.y)).toBeLessThan(2);
    expect(edge.end.x).toBeGreaterThan(edge.beforeEnd.x);
    expect(edge.pointerReachable, JSON.stringify(edge)).toBe(true);
  }
  expect(after.find((edge) => edge.id === loop.id)!.selfLoopOutsideBody).toBe(true);
  expect(await completedLayoutGeneration(page)).toBe(generation);
  await expectViewportUnchanged(page, viewport);
  expect(state.topologyRequests).toHaveLength(1);
  const draggedPositions = await page.locator(".react-flow__node").evaluateAll((nodes) =>
    nodes.map((node) => (node as HTMLElement).style.transform));
  await page.getByRole("button", { name: "Fit view", exact: true }).click();
  await expect.poll(async () => (await renderedEdgeGeometry(page, edges))
    .every((edge) => edge.pointerReachable)).toBe(true);
  expect(await completedLayoutGeneration(page)).toBe(generation);
  expect(await page.locator(".react-flow__node").evaluateAll((nodes) =>
    nodes.map((node) => (node as HTMLElement).style.transform))).toEqual(draggedPositions);
  await page.screenshot({ path: testInfo.outputPath("edge-geometry.png") });
});

test("separates cross-vhost return lanes and leaves a horizontal approach to SCC and cycle arrows", async ({ page }, testInfo) => {
  const state = createState();
  const selected = state.catalogue.static[0]!;
  const topology = topologyFor(selected);
  const resource = (id: string, name: string, vhost: string, kind: "exchange" | "queue" | "shovel") => {
    const template = topology.nodes.find((node) => node.type === kind)!;
    return { ...template, id, data: { ...template.data, name, vhost } };
  };
  const nodes = [
    resource("a.ab", "complex.ab", "topology-demo.control", "shovel"),
    resource("b.entry", "complex.entry", "topology-demo.region-b", "exchange"),
    resource("b.fallback", "complex.fallback", "topology-demo.region-b", "exchange"),
    resource("b.work", "complex.work.q", "topology-demo.region-b", "queue"),
    resource("b.dlx", "complex.dlx", "topology-demo.region-b", "exchange"),
    resource("b.return", "complex.return.q", "topology-demo.region-b", "queue"),
    resource("a.ba", "complex.ba", "topology-demo.control", "shovel"),
    resource("c.work", "complex.work", "topology-demo.region-a", "exchange"),
    resource("c.queue", "complex.work.q", "topology-demo.region-a", "queue"),
    resource("c.dlx", "complex.dlx", "topology-demo.region-a", "exchange"),
    resource("c.forward", "complex.forward.q", "topology-demo.region-a", "queue"),
  ];
  const edges = nodes.map((node, index): TopologyResponse["edges"][number] => ({
    id: `e.return.${String(index).padStart(2, "0")}`,
    source: node.id,
    target: nodes[(index + 1) % nodes.length]!.id,
    type: "shovel",
    data: { kind: "shovel", routingKey: null },
  }));
  state.topologies.set(`static:${selected.analysisId}`, { ...topology, nodes, edges });
  const vhosts = [...new Set(nodes.map((node) => node.data.vhost))];
  const nodeIds = nodes.map((node) => node.id);
  await openReadyApp(page, state);
  const scc = {
    ...sccRun(selected, "succeeded"),
    result: { components: [{ componentIndex: 0, isCyclic: true, nodeCount: nodes.length, vhosts, nodeIds }] },
  };
  await page.route("**/api/scc-runs**", (route) => json(route, scc, route.request().method() === "POST" ? 202 : 200));
  const cycle = { ...cycleRun(selected, "succeeded"), result: { cycleCount: 1, truncated: false, maxCycles: 20 } };
  await page.route("**/api/cycle-runs**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/cycles")) return json(route, {
      runId: cycle.runId, snapshotRevision: REVISION_A, offset: 0, limit: 50, total: 1,
      sort: "sdk", truncated: false, items: [{ cycleIndex: 0, length: nodes.length, vhosts, previewNodeIds: nodeIds.slice(0, 3) }],
    });
    if (path.endsWith("/cycles/0")) return json(route, {
      runId: cycle.runId, snapshotRevision: REVISION_A, cycleIndex: 0, length: nodes.length,
      vhosts, nodeIds, edgeIds: edges.map((edge) => edge.id),
    });
    return json(route, cycle, route.request().method() === "POST" ? 202 : 200);
  });
  await page.getByRole("button", { name: "Run cyclic components" }).click();
  await page.getByRole("checkbox", { name: /Cyclic component 1/ }).check();
  const returns = [edges[5]!, edges[10]!];
  for (const mode of ["scc", "cycle"]) {
    if (mode === "cycle") {
      await page.getByRole("button", { name: "Run structural cycles" }).click();
      await page.getByRole("button", { name: /Cycle 1 / }).click();
    }
    await completedLayoutGeneration(page);
    await page.locator(".topology-canvas").scrollIntoViewIfNeeded();
    const geometry = await renderedEdgeGeometry(page, returns);
    expect(Math.abs(geometry[0]!.button.y - geometry[1]!.button.y)).toBeGreaterThan(8);
    for (const edge of geometry) {
      expect(edge.markerUnits).toBe("userSpaceOnUse");
      expect(edge.approach.x).toBeCloseTo(24);
      expect(edge.approach.y).toBeCloseTo(0);
      expect(edge.pointerReachable, JSON.stringify(edge)).toBe(true);
    }
    await page.screenshot({ path: testInfo.outputPath(`${mode}-return-paths.png`) });
  }
  expect(state.topologyRequests).toHaveLength(1);
});

test("retains exact parallel and self-loop wire IDs through real-canvas cycle focus and filters", async ({ page }) => {
  const { state, edges, selected } = edgeGeometryState();
  await openReadyApp(page, state);
  const run = {
    ...cycleRun(selected, "succeeded"),
    result: { cycleCount: 2, truncated: false, maxCycles: 20 },
  };
  await page.route("**/api/cycle-runs**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/cycle-runs") return json(route, run, 202);
    if (path.endsWith("/cycles")) return json(route, {
      runId: run.runId, snapshotRevision: REVISION_A, offset: 0, limit: 50, total: 2,
      sort: "sdk", truncated: false, items: [
        { cycleIndex: 0, length: 2, vhosts: ["/orders"], previewNodeIds: ["n.exchange", "n.default"] },
        { cycleIndex: 1, length: 1, vhosts: ["/orders"], previewNodeIds: ["n.exchange"] },
      ],
    });
    const selfLoop = path.endsWith("/1");
    return json(route, {
      runId: run.runId, snapshotRevision: REVISION_A, cycleIndex: selfLoop ? 1 : 0,
      length: selfLoop ? 1 : 2, vhosts: ["/orders"],
      nodeIds: selfLoop ? ["n.exchange"] : ["n.exchange", "n.default"],
      edgeIds: selfLoop ? ["e.geometry.loop"] : ["e.geometry.beta", "e.geometry.return"],
    });
  });
  await page.getByRole("button", { name: "Run structural cycles" }).click();
  const ids = () => page.locator(".edge-inspect-button").evaluateAll((buttons) =>
    buttons.map((button) => button.getAttribute("data-edge-id")).sort());
  await page.getByRole("button", { name: /Cycle 1 / }).click();
  await expect.poll(ids).toEqual(["e.geometry.beta", "e.geometry.return"]);
  await completedLayoutGeneration(page);
  await page.locator(".topology-canvas").scrollIntoViewIfNeeded();
  for (const edge of await renderedEdgeGeometry(page, [edges[1]!, edges[3]!])) {
    expect(edge.pointerReachable, JSON.stringify(edge)).toBe(true);
  }
  await page.getByRole("button", { name: /Cycle 2 / }).click();
  await expect.poll(ids).toEqual(["e.geometry.loop"]);
  await completedLayoutGeneration(page);
  await page.locator(".topology-canvas").scrollIntoViewIfNeeded();
  expect((await renderedEdgeGeometry(page, [edges[4]!]))[0]!.selfLoopOutsideBody).toBe(true);
  await page.getByRole("button", { name: "Clear cycle focus" }).click();
  await expect.poll(ids).toEqual(edges.map((edge) => edge.id).sort());
  await completedLayoutGeneration(page);
  await page.locator(".topology-canvas").scrollIntoViewIfNeeded();
  await page.getByRole("checkbox", { name: /Exchanges/ }).uncheck();
  await expect.poll(ids).toEqual([]);
  await page.getByRole("checkbox", { name: /Exchanges/ }).check();
  await expect.poll(ids).toEqual(edges.map((edge) => edge.id).sort());
  await completedLayoutGeneration(page);
  expect(new Set((await renderedEdgeGeometry(page, edges)).map((edge) => edge.path)).size).toBe(edges.length);
  expect(state.topologyRequests).toHaveLength(1);
});

for (const operation of ["scc", "cycle"] as const) {
  test(`recovers a ready workspace after a stale ${operation} start reports a changed revision`, async ({ page }) => {
    const state = createState();
    let catalogueReads = 0;
    page.on("request", (request) => {
      if (new URL(request.url()).pathname === "/api/analyses") catalogueReads += 1;
    });
    await openReadyApp(page, state);
    await page.getByRole("button", { name: "Run cyclic components" }).click();
    await page.getByRole("checkbox", { name: /Cyclic component 1/ }).check();
    await page.getByRole("button", { name: "Run structural cycles" }).click();
    await page.getByRole("button", { name: /Cycle 2 / }).click();
    await expect(page.getByRole("article", { name: "Cycle 2 path" })).toBeVisible();

    const selected = { ...state.catalogue.static[0]!, snapshotRevision: REVISION_B };
    state.catalogue.static[0] = selected;
    state.topologies.set(`static:${selected.analysisId}`, topologyFor(selected, "revised.orders.events"));
    const requests: { source: Source; analysisId: string; snapshotRevision: string }[] = [];
    const keys: (string | undefined)[] = [];
    await page.route(`**/api/${operation}-runs`, async (route) => {
      const payload = route.request().postDataJSON();
      requests.push(payload);
      keys.push(route.request().headers()["idempotency-key"]);
      if (payload.snapshotRevision !== selected.snapshotRevision) {
        await json(route, errorBody("snapshot_revision_mismatch", "The snapshot revision changed."), 409);
      } else await route.fallback();
    });
    const label = operation === "scc" ? "Run cyclic components" : "Run structural cycles";
    await page.getByRole("button", { name: label }).click();
    await expect(page.getByRole("button", { name: "Inspect exchange revised.orders.events, vhost /orders" })).toBeVisible();
    await expect(page.locator(".topology-canvas")).toHaveAttribute("data-layout-state", "ready");
    await expect(page.getByRole("combobox", { name: "Analysis" })).toHaveValue(`static:${selected.analysisId}`);
    await expect(page.getByRole("button", { name: /Clear .* focus/ })).toHaveCount(0);
    await expect(page.getByRole("article", { name: "Cycle 2 path" })).toHaveCount(0);
    expect(await page.evaluate(() => [
      sessionStorage.getItem("rabbitmq-topology-active-scc-run"),
      sessionStorage.getItem("rabbitmq-topology-active-cycle-run"),
    ])).toEqual([null, null]);
    expect(catalogueReads).toBe(2);
    expect(state.topologyRequests).toHaveLength(2);
    await page.getByRole("button", { name: label }).click();
    await expect.poll(() => requests.length).toBe(2);
    expect(requests.map((request) => request.snapshotRevision)).toEqual([REVISION_A, REVISION_B]);
    expect(requests[1]).toMatchObject({ source: "static", analysisId: selected.analysisId });
    expect(keys[1]).not.toBe(keys[0]);
    expect(catalogueReads).toBe(2);
    expect(state.topologyRequests).toHaveLength(2);
  });
}

for (const operation of ["scc", "cycle"] as const) {
  const storageKey = `rabbitmq-topology-active-${operation}-run`;
  const startLabel = operation === "scc" ? "Run cyclic components" : "Run structural cycles";
  const statusLabel =
    operation === "scc" ? "Cyclic-component operation status" : "Structural-cycle operation status";

  test(`${operation} polling survives capacity and network failures on the original run`, async ({
    page,
  }) => {
    const state = createState();
    state.sccMode = "running";
    state.cycleMode = "running";
    await openReadyApp(page, state);
    const times: number[] = [];
    await page.route(`**/api/${operation}-runs/*`, async (route) => {
      if (route.request().method() !== "GET") return route.fallback();
      times.push(Date.now());
      if (times.length === 1) {
        await json(
          route,
          errorBody(
            "authentication_capacity_unavailable",
            "Authentication capacity is temporarily unavailable.",
          ),
          503,
          { "Retry-After": "2" },
        );
      } else if (times.length === 2) {
        await route.abort("failed");
      } else {
        await route.fallback();
      }
    });
    await page.getByRole("button", { name: startLabel }).click();
    await expect(page.getByRole("button", { name: "Cancel analysis" })).toBeVisible();
    const recovery = await page.evaluate((key) => sessionStorage.getItem(key), storageKey);
    expect(JSON.parse(recovery!).runId).toBeTruthy();
    await expect.poll(() => times.length).toBe(1);
    expect(await page.evaluate((key) => sessionStorage.getItem(key), storageKey)).toBe(recovery);
    await expect(page.getByRole("button", { name: "Cancel analysis" })).toBeEnabled();
    await expect(page.getByRole("button", { name: /running…/ })).toBeDisabled();
    await expect.poll(() => times.length, { timeout: 10_000 }).toBe(3);
    await expect(page.getByRole("status", { name: statusLabel })).toContainText(
      operation === "scc" ? "2 cyclic components found" : "structural cycles found",
    );
    expect(times[1]! - times[0]!).toBeGreaterThanOrEqual(1900);
    expect(times[2]! - times[1]!).toBeGreaterThanOrEqual(1900);
    expect(operation === "scc" ? state.sccStarts : state.cycleStarts).toBe(1);
    expect(await page.evaluate((key) => sessionStorage.getItem(key), storageKey)).toBeNull();
  });

  test(`${operation} replays lost admission with its key and cancels during transient reload recovery`, async ({
    page,
  }) => {
    const state = createState();
    await openReadyApp(page, state);
    const admissions: { key: string | undefined; body: string | null }[] = [];
    await page.route(`**/api/${operation}-runs`, async (route) => {
      admissions.push({
        key: route.request().headers()["idempotency-key"],
        body: route.request().postData(),
      });
      const selected = requestAnalysis(state, route.request().postData());
      if (operation === "scc") {
        state.sccStarts += 1;
        state.lastSccRun = sccRun(selected, "running");
      } else {
        state.cycleStarts += 1;
        state.lastCycleRun = cycleRun(selected, "running");
      }
      if (admissions.length === 1) await route.abort("failed");
      else await json(route, operation === "scc" ? state.lastSccRun : state.lastCycleRun, 202);
    });
    await page.getByRole("button", { name: startLabel }).click();
    await expect.poll(() => admissions.length).toBe(1);
    const unknown = await page.evaluate((key) => sessionStorage.getItem(key), storageKey);
    expect(JSON.parse(unknown!).runId).toBeNull();
    await expect(page.getByRole("button", { name: "Cancel analysis" })).toBeVisible();
    expect(admissions).toHaveLength(2);
    expect(admissions[0]!.key).toBeTruthy();
    expect(admissions[1]).toEqual(admissions[0]);

    let recoveryPolls = 0;
    await page.route(`**/api/${operation}-runs/*`, async (route) => {
      if (route.request().method() !== "GET") return route.fallback();
      recoveryPolls += 1;
      await json(route, errorBody("service_unavailable", "Temporarily unavailable."), 503, {
        "Retry-After": "2",
      });
    });
    await page.reload();
    await expect.poll(() => recoveryPolls).toBe(1);
    const known = await page.evaluate((key) => sessionStorage.getItem(key), storageKey);
    expect(JSON.parse(known!).idempotencyKey).toBe(admissions[0]!.key);
    expect(JSON.parse(known!).runId).toBeTruthy();
    await page.getByRole("button", { name: "Cancel analysis" }).click();
    await expect(page.getByRole("status", { name: statusLabel })).toContainText("cancelled");
    expect(await page.evaluate((key) => sessionStorage.getItem(key), storageKey)).toBeNull();
    await page.waitForTimeout(2200);
    expect(recoveryPolls).toBe(1);
    expect(admissions).toHaveLength(2);
    expect(operation === "scc" ? state.sccCancels : state.cycleCancels).toBe(1);
  });
}

for (const width of [320, 390, 780, 781, 800, 820, 900, 1024, 1440]) {
  // Every width checks layout; exercise the complete interaction workflow only
  // once on each side of the mobile breakpoint.
  const exerciseInteractions = width === 390 || width === 1440;
  test(`keeps the responsive drawer and graph within ${width}px`, async ({ page }, testInfo) => {
    const state = createState();
    await page.setViewportSize({ width, height: 900 });
    await openReadyApp(page, state);
    const canvas = page.locator(".topology-canvas");
    const drawer = page.locator(".drawer-content");
    const toggle = page.getByRole("button", { name: "Results & inspection" });

    const expectDocumentFits = async () => {
      await expect.poll(() => page.evaluate(() => Math.max(
        document.documentElement.scrollWidth,
        document.body.scrollWidth,
      ))).toBeLessThanOrEqual(width);
      await expect(canvas).toHaveCSS("overflow", "hidden");
      expect((await canvas.boundingBox())!.width).toBeLessThanOrEqual(width);
      const tabs = drawer.getByRole("tablist");
      if (await tabs.count()) {
        await expect.poll(() => tabs.evaluate((element) => element.scrollWidth - element.clientWidth))
          .toBeLessThanOrEqual(0);
      }
    };

    await expect(toggle).toHaveAttribute("aria-expanded", "true");
    await expectDocumentFits();
    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    await expect(drawer).toHaveCount(0);
    await expectDocumentFits();
    await toggle.press("Enter");
    await expect(toggle).toHaveAttribute("aria-expanded", "true");

    await page.getByRole("button", { name: "Run cyclic components" }).click();
    const component = page.getByRole("checkbox", { name: /Cyclic component 1/ });
    await expect(component).toBeVisible();
    await page.getByRole("button", { name: "Run structural cycles" }).click();
    const cycle = page.getByRole("button", { name: /Cycle 2 / });
    await expect(cycle).toBeVisible();
    await expectDocumentFits();
    await expect(drawer).toHaveCSS("overflow", "visible");

    const sort = page.getByRole("combobox", { name: "Sort summaries" });
    const minimum = page.getByRole("spinbutton", { name: "Minimum length" });
    const maximum = page.getByRole("spinbutton", { name: "Maximum length" });
    const next = page.getByRole("button", { name: "Next", exact: true });
    for (const control of [sort, minimum, maximum, next, cycle]) {
      await control.scrollIntoViewIfNeeded();
      await expect(control).toBeInViewport();
      const bounds = (await control.boundingBox())!;
      expect(bounds.x).toBeGreaterThanOrEqual(0);
      expect(bounds.x + bounds.width).toBeLessThanOrEqual(width);
    }
    if (exerciseInteractions) {
      for (const control of [sort, minimum, maximum, next, cycle]) {
        await control.focus();
        await expect(control).toBeFocused();
      }
      await sort.selectOption("length_desc");
      await minimum.fill("2");
      await maximum.fill("5");
      await expect(sort).toHaveValue("length_desc");
      await expect(minimum).toHaveValue("2");
      await expect(maximum).toHaveValue("5");
      await next.click();
      await expect(page.getByRole("button", { name: /Cycle 52 / })).toBeVisible();
      await page.getByRole("button", { name: "Previous", exact: true }).click();
    }
    await cycle.click();
    const detail = page.getByRole("article", { name: "Cycle 2 path" });
    await expect(detail).toContainText("shovel orders-to-audit in /orders");
    await expectDocumentFits();
    if (width === 800 || width === 820) {
      await page.screenshot({ path: testInfo.outputPath("responsive-drawer.png"), fullPage: true });
    }

    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    await expectDocumentFits();
    await toggle.press("Enter");
    await expect(detail).toBeVisible();
    await expectDocumentFits();

    if (exerciseInteractions) {
      await completedLayoutGeneration(page);
      await canvas.scrollIntoViewIfNeeded();
      const before = await viewportTransform(page);
      const bounds = (await canvas.boundingBox())!;
      const scrollY = await page.evaluate(() => window.scrollY);
      await page.mouse.move(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2);
      await page.mouse.wheel(0, -160);
      await expect.poll(() => viewportTransform(page)).not.toBe(before);
      expect(await page.evaluate(() => window.scrollY)).toBe(scrollY);
      expect(await canvas.evaluate((element) => [element.scrollLeft, element.scrollTop])).toEqual([0, 0]);
      await expectDocumentFits();
    }
  });
}
