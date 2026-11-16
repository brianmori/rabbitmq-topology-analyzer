import { expect, test, type Browser, type Page, type Route } from "@playwright/test";
import { readFile, writeFile } from "node:fs/promises";
import { cpus, totalmem } from "node:os";
import { resolve } from "node:path";
import { completedLayoutGeneration } from "../e2e/layout-support.js";

interface TopologyNode {
  id: string;
  data: { vhost: string; name: string; kind: string };
}

interface TopologyEdge {
  id: string;
  source: string;
  target: string;
}

interface TopologyFixture {
  source: "static";
  analysisId: string;
  snapshotRevision: string;
  cluster: Record<string, unknown>;
  nodes: ReadonlyArray<TopologyNode>;
  edges: ReadonlyArray<TopologyEdge>;
}

interface LayoutMeasurement {
  generation: number;
  duration: number;
  nodeCount: number;
  edgeCount: number;
}

type Scenario = "full" | "scc_filtered" | "cycle_filtered";

const WARM_UP_RUNS = 1;
const RECORDED_RUNS = 5;
const LAYOUT_BUDGET_MS = 5_000;
const viewport = { width: 1440, height: 900 } as const;
const topologyPath = resolve(
  process.env.RABBITMQ_BENCHMARK_TOPOLOGY_PATH ??
    "../benchmarks/.artifacts/representative-topology.json",
);
const reportPath = resolve(
  process.env.RABBITMQ_LAYOUT_BENCHMARK_REPORT ?? "../benchmarks/.artifacts/layout-benchmark.json",
);

declare global {
  interface Window {
    rabbitmqLayoutMeasurements: LayoutMeasurement[];
  }
}

function median(values: ReadonlyArray<number>): number {
  const sorted = [...values].sort((left, right) => left - right);
  return sorted[Math.floor(sorted.length / 2)];
}

function filteredTopology(fixture: TopologyFixture, scenario: Scenario): TopologyFixture {
  if (scenario === "full") return fixture;
  if (scenario === "scc_filtered") {
    const selectedVhosts = new Set(
      [...new Set(fixture.nodes.map((node) => node.data.vhost))].sort().slice(0, 5),
    );
    const nodes = fixture.nodes.filter((node) => selectedVhosts.has(node.data.vhost));
    const nodeIds = new Set(nodes.map((node) => node.id));
    const edges = fixture.edges.filter(
      (edge) => nodeIds.has(edge.source) && nodeIds.has(edge.target),
    );
    return { ...fixture, nodes, edges };
  }
  const edges = fixture.edges.slice(0, 8);
  const nodeIds = new Set(edges.flatMap((edge) => [edge.source, edge.target]));
  const nodes = fixture.nodes.filter((node) => nodeIds.has(node.id));
  return { ...fixture, nodes, edges };
}

async function fulfillApi(route: Route, topology: TopologyFixture): Promise<void> {
  const url = new URL(route.request().url());
  if (url.pathname === "/api/health") {
    await route.fulfill({ json: { status: "ok" } });
    return;
  }
  if (url.pathname === "/api/auth/session") {
    await route.fulfill({
      headers: { "Cache-Control": "no-store" },
      json: {
        authMode: "none",
        authenticated: true,
        loginPath: null,
        logoutPath: null,
        csrfToken: null,
      },
    });
    return;
  }
  if (url.pathname === "/api/access") {
    await route.fulfill({
      json: {
        authMode: "none",
        roles: ["analyst"],
        capabilities: { viewTopology: true, runGraphOperations: true, manageUploads: true },
      },
    });
    return;
  }
  if (url.pathname === "/api/analyses") {
    await route.fulfill({
      json: {
        static: [
          {
            analysisId: topology.analysisId,
            label: "Representative v1 fixture",
            source: "static",
            snapshotRevision: topology.snapshotRevision,
            loadedAt: "2026-08-13T00:00:00Z",
            captureCompleteness: "definitions_only",
            cluster: topology.cluster,
            description: "Real-SDK 50-vhost performance fixture",
            ownerContact: null,
            expiresAt: null,
          },
        ],
        session: [],
      },
    });
    return;
  }
  if (url.pathname === "/api/topology") {
    await route.fulfill({ json: topology });
    return;
  }
  await route.abort("failed");
}

async function measureFreshPage(
  browser: Browser,
  fixture: TopologyFixture,
  scenario: Scenario,
): Promise<LayoutMeasurement> {
  const context = await browser.newContext({
    viewport,
    deviceScaleFactor: 1,
    reducedMotion: "reduce",
    serviceWorkers: "block",
  });
  const page: Page = await context.newPage();
  await page.addInitScript(() => {
    window.rabbitmqLayoutMeasurements = [];
    window.addEventListener("rabbitmq:layout-complete", (event) => {
      window.rabbitmqLayoutMeasurements.push((event as CustomEvent<LayoutMeasurement>).detail);
    });
  });
  const topology = filteredTopology(fixture, scenario);
  await page.route("**/api/**", (route) => fulfillApi(route, topology));
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.waitForFunction(() => window.rabbitmqLayoutMeasurements.length > 0, undefined, {
    timeout: 30_000,
  });
  const measurement = await page.evaluate(() => window.rabbitmqLayoutMeasurements[0]);
  await context.close();
  return measurement;
}

async function readCgroupLimit(path: string): Promise<string | null> {
  try {
    return (await readFile(path, "utf8")).trim();
  } catch {
    return null;
  }
}

async function expectWholeDrawingFits(page: Page) {
  await expect.poll(() => page.locator(".topology-canvas").evaluate((canvas) => {
    const bounds = canvas.getBoundingClientRect();
    const boxes = [...canvas.querySelectorAll(".react-flow__node, .edge-inspect-button")]
      .map((element) => element.getBoundingClientRect());
    for (const path of canvas.querySelectorAll<SVGPathElement>(".topology-edge")) {
      const box = path.getBBox();
      const matrix = path.getScreenCTM()!;
      const top = new DOMPoint(box.x, box.y).matrixTransform(matrix);
      const bottom = new DOMPoint(box.x + box.width, box.y + box.height).matrixTransform(matrix);
      boxes.push(new DOMRect(top.x, top.y, bottom.x - top.x, bottom.y - top.y));
    }
    return boxes.filter((box) => box.left < bounds.left - 2 || box.right > bounds.right + 2
      || box.top < bounds.top - 2 || box.bottom > bounds.bottom + 2).length;
  })).toBe(0);
}

for (const viewport of [{ width: 1440, height: 900 }, { width: 1024, height: 768 }]) {
  test(`fits all 50 vhosts, resources, routed paths and inspectors at ${viewport.width}px`, async ({ page }, testInfo) => {
    const fixture = JSON.parse(await readFile(topologyPath, "utf8")) as TopologyFixture;
    await page.setViewportSize(viewport);
    await page.route("**/api/**", (route) => fulfillApi(route, fixture));
    await page.goto("/");
    const generation = await completedLayoutGeneration(page);
    await expect(page.locator(".vhost-group")).toHaveCount(50);
    await expect(page.locator(".resource-node")).toHaveCount(600);
    await expectWholeDrawingFits(page);
    const positions = await page.locator(".react-flow__node").evaluateAll((nodes) =>
      nodes.map((node) => (node as HTMLElement).style.transform));
    await page.getByRole("button", { name: /zoom in/i }).click();
    await page.getByRole("button", { name: "Fit view", exact: true }).click();
    await expectWholeDrawingFits(page);
    expect(await completedLayoutGeneration(page)).toBe(generation);
    expect(await page.locator(".react-flow__node").evaluateAll((nodes) =>
      nodes.map((node) => (node as HTMLElement).style.transform))).toEqual(positions);
    await page.getByRole("button", { name: "Reset layout" }).click();
    await expect(page.locator(".topology-canvas")).not.toHaveAttribute("data-layout-generation", generation);
    await completedLayoutGeneration(page);
    await expectWholeDrawingFits(page);
    await page.screenshot({ path: testInfo.outputPath("whole-drawing-fit.png") });
  });
}

test("chooses repeated queue names by vhost using the keyboard without relayout or HTTP", async ({ page }) => {
  const fixture = JSON.parse(await readFile(topologyPath, "utf8")) as TopologyFixture;
  let topologyRequests = 0;
  await page.route("**/api/**", (route) => {
    if (new URL(route.request().url()).pathname === "/api/topology") topologyRequests += 1;
    return fulfillApi(route, fixture);
  });
  await page.goto("/");
  const generation = await completedLayoutGeneration(page);
  const search = page.getByRole("searchbox", { name: "Resource name" });
  await search.focus();
  await page.keyboard.type("QUEUE-00");
  await expect(page.getByText("50 visible matches", { exact: true })).toBeVisible();
  await expect(page.locator(".resource-node--search-match")).toHaveCount(50);
  await page.keyboard.press("Tab");
  const matches = page.getByRole("group", { name: "Visible resource matches" });
  await expect(matches.getByRole("button").first()).toBeFocused();
  await page.keyboard.press("End");
  for (let index = 0; index < 12; index += 1) await page.keyboard.press("ArrowUp");
  await page.keyboard.press("Enter");
  const match = fixture.nodes.find((node) => node.data.kind === "queue"
    && node.data.name === "queue-00" && node.data.vhost === "vhost-37")!;
  const details = page.getByRole("region", { name: "Safe details" });
  await expect(details.getByText("vhost-37", { exact: true })).toBeVisible();
  await expect(details.getByText(match.id, { exact: true })).toBeVisible();
  const node = page.getByRole("button", { name: "Inspect queue queue-00, vhost vhost-37", exact: true });
  await expect.poll(async () => (await node.boundingBox())!.width).toBeGreaterThan(200);
  const canvas = (await page.locator(".topology-canvas").boundingBox())!;
  const box = (await node.boundingBox())!;
  expect(Math.abs(box.x + box.width / 2 - canvas.x - canvas.width / 2)).toBeLessThan(2);
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
  await expect(details.getByText("vhost-38", { exact: true })).toBeVisible();
  expect(await completedLayoutGeneration(page)).toBe(generation);
  await expect(page.locator(".resource-node")).toHaveCount(600);
  await page.getByRole("checkbox", { name: "vhost-38", exact: true }).focus();
  await page.keyboard.press("Space");
  await completedLayoutGeneration(page);
  await expect(details.getByText("vhost-38", { exact: true })).toHaveCount(0);
  await expect(matches.getByRole("button", { pressed: true })).toHaveCount(0);
  expect(topologyRequests).toBe(1);
  // Only the explicit visibility change creates a new generation.
  expect(Number(await completedLayoutGeneration(page))).toBe(Number(generation) + 1);
});

test("production Chromium full and filtered ELK layouts meet the v1 budget", async ({
  browser,
}) => {
  test.setTimeout(180_000);
  const fixture = JSON.parse(await readFile(topologyPath, "utf8")) as TopologyFixture;
  const scenarioReports: Array<Record<string, unknown>> = [];

  for (const scenario of ["full", "scc_filtered", "cycle_filtered"] as const) {
    const measurements: LayoutMeasurement[] = [];
    for (let index = 0; index < WARM_UP_RUNS + RECORDED_RUNS; index += 1) {
      const measurement = await measureFreshPage(browser, fixture, scenario);
      if (index >= WARM_UP_RUNS) measurements.push(measurement);
    }
    const samples = measurements.map((measurement) => measurement.duration);
    const measuredMedian = median(samples);
    scenarioReports.push({
      phase: scenario,
      unit: "milliseconds",
      samples: samples.map((sample) => Number(sample.toFixed(3))),
      median: Number(measuredMedian.toFixed(3)),
      budget: LAYOUT_BUDGET_MS,
      passed: measuredMedian <= LAYOUT_BUDGET_MS,
      nodeCount: measurements[0].nodeCount,
      edgeCount: measurements[0].edgeCount,
    });
  }

  const report = {
    environment: {
      playwright: "1.62.1",
      chromium: browser.version(),
      viewport,
      deviceScaleFactor: 1,
      reducedMotion: "reduce",
      logicalCpuCount: cpus().length,
      physicalMemoryBytes: totalmem(),
      cgroupCpuMax: await readCgroupLimit("/sys/fs/cgroup/cpu.max"),
      cgroupMemoryMax: await readCgroupLimit("/sys/fs/cgroup/memory.max"),
      canonicalRunner: { logicalCpuLimit: 4, memoryBytes: 8 * 1024 ** 3 },
      productionBuild: true,
    },
    fixture: {
      nodeCount: fixture.nodes.length,
      edgeCount: fixture.edges.length,
    },
    warmUpRuns: WARM_UP_RUNS,
    recordedRuns: RECORDED_RUNS,
    phases: scenarioReports,
  };
  await writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`, "utf8");
  console.log(JSON.stringify(report, null, 2));

  for (const phase of scenarioReports) {
    expect(phase.passed, `${phase.phase} layout exceeded ${LAYOUT_BUDGET_MS} ms`).toBe(true);
  }
});
