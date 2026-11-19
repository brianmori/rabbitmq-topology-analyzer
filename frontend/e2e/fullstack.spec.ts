import { expect, test, type Page, type Response } from "@playwright/test";
import { resolve } from "node:path";
import { readFile } from "node:fs/promises";

const cyclicDefinitions = resolve(import.meta.dirname, "fixtures/static/cycle.definitions.json");

test.use({
  launchOptions: {
    args: ["--host-resolver-rules=MAP topology.test 127.0.0.1", "--no-proxy-server"],
  },
});

async function layoutReady(page: Page) {
  await page.waitForFunction(() => {
    const canvas = document.querySelector(".topology-canvas");
    const generation = canvas?.getAttribute("data-layout-generation");
    return generation && generation !== "0" && generation === canvas?.getAttribute("data-layout-completed-generation");
  });
}

async function expectLegendMatchesDrawing(page: Page) {
  for (const kind of ["binding", "dead_letter", "alternate_exchange", "shovel"]) {
    const actual = page.locator(`.topology-canvas .topology-edge--${kind}`).first();
    if (await actual.count() === 0) continue;
    const sample = page.locator(`.legend-edge[data-relationship="${kind}"] path`);
    for (const property of ["stroke-dasharray", "stroke-linecap"]) {
      await expect.poll(async () => {
        const [legend, drawing] = await Promise.all([sample, actual].map((locator) =>
          locator.evaluate((element, property) => getComputedStyle(element).getPropertyValue(property), property)));
        return legend === drawing;
      }).toBe(true);
    }
  }
}

function isApiResponse(response: Response, method: string, pathname: string): boolean {
  return response.request().method() === method && new URL(response.url()).pathname === pathname;
}

test("loads the catalogue and runs real analyses over plain HTTP on a private hostname", async ({
  page,
  baseURL,
}) => {
  const origin = new URL("/", baseURL);
  origin.hostname = "topology.test";
  await page.goto(origin.href);
  expect(await page.evaluate(() => isSecureContext)).toBe(false);
  await expect(page.getByRole("combobox", { name: "Analysis" })).toHaveValue("static:e2e-static-cycle");

  const scc = page.waitForResponse((response) => isApiResponse(response, "POST", "/api/scc-runs"));
  await page.getByRole("button", { name: "Run cyclic components" }).click();
  expect((await scc).status()).toBe(202);
  await expect(page.getByRole("status", { name: "Cyclic-component operation status" })).toContainText("1 cyclic component found.");

  const cycle = page.waitForResponse((response) => isApiResponse(response, "POST", "/api/cycle-runs"));
  await page.getByRole("button", { name: "Run structural cycles" }).click();
  expect((await cycle).status()).toBe(202);
  await expect(page.getByRole("status", { name: "Structural-cycle operation status" })).toContainText("1 structural cycle found.");
});

for (const width of [1440, 1024]) {
  test(`keeps the real two-node fixture readable after framing at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: width === 1440 ? 900 : 768 });
    await page.goto("/");
    await page.waitForFunction(() => {
      const canvas = document.querySelector(".topology-canvas");
      const generation = canvas?.getAttribute("data-layout-generation");
      return generation && generation !== "0" && generation === canvas?.getAttribute("data-layout-completed-generation");
    });
    for (const action of ["initial", "fit"]) {
      if (action === "fit") {
        await page.getByRole("button", { name: /zoom in/i }).click();
        await page.getByRole("button", { name: "Fit view", exact: true }).click();
      }
      const labels = await page.locator(".resource-node__copy strong").evaluateAll((labels) =>
        labels.map((label) => {
          const node = label.closest(".resource-node") as HTMLElement;
          const scale = node.getBoundingClientRect().width / node.offsetWidth;
          return Number.parseFloat(getComputedStyle(label).fontSize) * scale;
        }));
      expect(labels).toHaveLength(2);
      expect(Math.min(...labels)).toBeGreaterThanOrEqual(10);
    }
  });
}

test("serves the production app through FastAPI and completes a real SDK workflow", async ({
  page,
}) => {
  test.setTimeout(60_000);

  const staticTopologyResponse = page.waitForResponse((response) =>
    isApiResponse(response, "GET", "/api/topology"),
  );
  await page.goto("/");

  const picker = page.getByRole("combobox", { name: "Analysis" });
  await expect(page).toHaveTitle("RabbitMQ Topology Analyzer");
  await expect(picker).toHaveValue("static:e2e-static-cycle");
  await expect(page.locator(".topology-canvas")).toHaveAttribute("data-layout-state", "ready");

  const staticTopology = await staticTopologyResponse;
  expect(staticTopology.status()).toBe(200);
  expect(await staticTopology.json()).toMatchObject({
    source: "static",
    analysisId: "e2e-static-cycle",
    cluster: { clusterName: "E2E cycle broker" },
  });
  await expect(page.getByRole("button", { name: "Inspect queue loop-q, vhost /" })).toBeVisible();

  await page.getByRole("button", { name: "Upload capture" }).click();
  const dialog = page.getByRole("dialog", { name: "Manage captured analysis" });
  await dialog.getByLabel("Analysis ID").fill("e2e-session-cycle");
  await dialog.getByLabel("Display label (optional)").fill("E2E session cycle");
  await dialog.getByLabel("Definitions export").setInputFiles(cyclicDefinitions);

  const uploadResponse = page.waitForResponse((response) =>
    isApiResponse(response, "POST", "/api/definitions/upload"),
  );
  await dialog.getByRole("button", { name: "Upload capture" }).click();

  const uploaded = await uploadResponse;
  expect(uploaded.status()).toBe(201);
  expect(await uploaded.json()).toMatchObject({
    analysis: {
      source: "session",
      analysisId: "e2e-session-cycle",
      label: "E2E session cycle",
      cluster: { clusterName: "E2E cycle broker" },
    },
    replaced: false,
  });
  await expect(dialog.getByText("Uploaded E2E session cycle.")).toBeVisible();
  await dialog.getByRole("button", { name: "Close" }).click();

  await expect(picker).toHaveValue("session:e2e-session-cycle");
  await expect(page.locator(".topology-canvas")).toHaveAttribute("data-layout-state", "ready");
  await expect(page.getByText("Definitions only")).toBeVisible();

  const startResponse = page.waitForResponse((response) =>
    isApiResponse(response, "POST", "/api/scc-runs"),
  );
  await page.getByRole("button", { name: "Run cyclic components" }).click();

  const started = await startResponse;
  expect(started.status()).toBe(202);
  expect(await started.json()).toMatchObject({
    operation: "cyclic_scc",
    source: "session",
    analysisId: "e2e-session-cycle",
  });

  const results = page.getByRole("region", { name: "Cyclic components" });
  await expect(results.getByText("1 cyclic component found.")).toBeVisible({ timeout: 20_000 });
  const component = results.getByRole("checkbox", { name: /Cyclic component 1/ });
  await component.check();
  await expect(page.getByRole("button", { name: "Clear component focus" })).toBeVisible();
});

async function expectClearRoutes(page: Page) {
  const canvas = page.locator(".topology-canvas");
  await page.waitForFunction(() => {
    const canvas = document.querySelector(".topology-canvas");
    const completed = canvas?.getAttribute("data-layout-completed-generation");
    return completed !== "0" && completed === canvas?.getAttribute("data-layout-generation");
  });
  await canvas.scrollIntoViewIfNeeded();
  const collisions = await canvas.evaluate((canvas) => {
    type Point = { x: number; y: number };
    const selfCrosses = (points: Point[]) => {
      const bins = new Map<string, number[]>();
      const cross = (a: Point, b: Point) => a.x * b.y - a.y * b.x;
      const intersects = (a: Point, b: Point, c: Point, d: Point) => {
        const r = { x: b.x - a.x, y: b.y - a.y };
        const s = { x: d.x - c.x, y: d.y - c.y };
        const offset = { x: c.x - a.x, y: c.y - a.y };
        const denominator = cross(r, s);
        if (Math.abs(denominator) < 0.000001) {
          if (Math.abs(cross(offset, r)) > 0.000001) return false;
          const axis = Math.abs(r.x) > Math.abs(r.y) ? "x" : "y";
          return Math.max(Math.min(a[axis], b[axis]), Math.min(c[axis], d[axis])) <=
            Math.min(Math.max(a[axis], b[axis]), Math.max(c[axis], d[axis]));
        }
        const along = cross(offset, s) / denominator;
        const other = cross(offset, r) / denominator;
        return along >= 0 && along <= 1 && other >= 0 && other <= 1;
      };
      for (let index = 1; index < points.length; index += 1) {
        const start = points[index - 1]!;
        const end = points[index]!;
        const keys: string[] = [];
        for (let x = Math.floor(Math.min(start.x, end.x) / 16); x <= Math.floor(Math.max(start.x, end.x) / 16); x += 1) {
          for (let y = Math.floor(Math.min(start.y, end.y) / 16); y <= Math.floor(Math.max(start.y, end.y) / 16); y += 1) keys.push(`${x}:${y}`);
        }
        const candidates = new Set(keys.flatMap((key) => bins.get(key) ?? []));
        for (const previous of candidates) {
          if (previous < index - 1 && intersects(points[previous - 1]!, points[previous]!, start, end)) return true;
        }
        for (const key of keys) {
          const entries = bins.get(key) ?? [];
          entries.push(index);
          bins.set(key, entries);
        }
      }
      return false;
    };
    const obstacles = [...canvas.querySelectorAll(".resource-node, .vhost-group__label")].map((element) => ({
      name: element.textContent,
      rect: element.getBoundingClientRect(),
    }));
    return [...canvas.querySelectorAll<SVGPathElement>(".topology-edge")].flatMap((path) => {
      const matrix = path.getScreenCTM()!;
      const length = path.getTotalLength();
      const samples = Math.ceil(length / 2);
      const hits = new Set<string>();
      const points: Point[] = [];
      for (let index = 0; index <= samples; index += 1) {
        const point = path.getPointAtLength(index * length / samples);
        points.push({ x: point.x, y: point.y });
        const screen = new DOMPoint(point.x, point.y).matrixTransform(matrix);
        for (const { name, rect } of obstacles) {
          if (screen.x > rect.left + 0.5 && screen.x < rect.right - 0.5 &&
            screen.y > rect.top + 0.5 && screen.y < rect.bottom - 0.5) hits.add(name ?? "");
        }
      }
      if (selfCrosses(points)) hits.add("self-crossing or retraced path");
      return [...hits].map((node) => ({ edge: path.id, node }));
    });
  });
  expect(collisions).toEqual([]);
}

async function expectSeparateChannels(page: Page) {
  const violations = await page.locator(".topology-canvas").evaluate((canvas) => {
    type Point = { x: number; y: number };
    type Segment = { edge: string; start: Point; end: Point; horizontal: boolean };
    const segments: Segment[] = [];
    const borders = [...canvas.querySelectorAll(".vhost-group.react-flow__node")].map((node) => node.getBoundingClientRect());
    const failures: string[] = [];
    for (const path of canvas.querySelectorAll<SVGPathElement>(".topology-edge")) {
      const length = path.getTotalLength();
      const samples = Math.ceil(length / 2);
      let run: Segment | null = null;
      let previous = path.getPointAtLength(0);
      const save = () => {
        if (run && Math.hypot(run.end.x - run.start.x, run.end.y - run.start.y) > 20) segments.push(run);
        run = null;
      };
      for (let index = 1; index <= samples; index += 1) {
        const point = path.getPointAtLength(index * length / samples);
        const horizontal = Math.abs(point.y - previous.y) < 0.0001;
        const vertical = Math.abs(point.x - previous.x) < 0.0001;
        if (!horizontal && !vertical) save();
        else if (run && run.horizontal === horizontal) run.end = { x: point.x, y: point.y };
        else {
          save();
          run = { edge: path.id, start: { x: previous.x, y: previous.y }, end: { x: point.x, y: point.y }, horizontal };
        }
        previous = point;
      }
      save();
      const inverse = path.getScreenCTM()!.inverse();
      for (const segment of segments.filter((segment) => segment.edge === path.id)) {
        const along = segment.horizontal ? "x" : "y";
        const across = segment.horizontal ? "y" : "x";
        for (const border of borders) {
          const low = new DOMPoint(border.left, border.top).matrixTransform(inverse);
          const high = new DOMPoint(border.right, border.bottom).matrixTransform(inverse);
          if (Math.max(segment.start[along], segment.end[along]) <= low[along] ||
            Math.min(segment.start[along], segment.end[along]) >= high[along]) continue;
          if (Math.min(Math.abs(segment.start[across] - low[across]), Math.abs(segment.start[across] - high[across])) < 23.5) {
            failures.push(`${path.id} runs too close to a container border`);
          }
        }
      }
    }
    for (const [index, segment] of segments.entries()) {
      const along = segment.horizontal ? "x" : "y";
      const across = segment.horizontal ? "y" : "x";
      for (const other of segments.slice(index + 1)) {
        if (segment.edge === other.edge || segment.horizontal !== other.horizontal) continue;
        const overlap = Math.min(Math.max(segment.start[along], segment.end[along]), Math.max(other.start[along], other.end[along])) -
          Math.max(Math.min(segment.start[along], segment.end[along]), Math.min(other.start[along], other.end[along]));
        if (Math.abs(segment.start[across] - other.start[across]) < 0.5 && overlap > 8) {
          failures.push(`${segment.edge} and ${other.edge} share a path segment`);
        }
      }
    }
    return failures;
  });
  expect(violations).toEqual([]);
}

test("routes real SDK loops around every resource and the vhost title in exploration, SCC and cycle views", async ({ page }, testInfo) => {
  test.setTimeout(60_000);
  await page.goto("/");
  await expect(page.getByRole("button", { name: "Upload capture" })).toBeVisible();
  await page.getByRole("button", { name: "Upload capture" }).click();
  const dialog = page.getByRole("dialog", { name: "Manage captured analysis" });
  await dialog.getByLabel("Analysis ID").fill("e2e-routing-loops");
  await dialog.getByLabel("Definitions export").setInputFiles(resolve(import.meta.dirname, "fixtures/loops.definitions.json"));
  await dialog.getByRole("button", { name: "Upload capture" }).click();
  await expect(dialog.getByText("Uploaded e2e-routing-loops.")).toBeVisible();
  await dialog.getByRole("button", { name: "Close" }).click();
  const canvas = page.locator(".topology-canvas");
  await expect(canvas).toHaveAttribute("data-layout-node-count", "17");
  await expectClearRoutes(page);
  await expectSeparateChannels(page);
  const viewport = page.viewportSize()!;
  await page.setViewportSize({ width: 2382, height: 1700 });
  await page.getByRole("button", { name: "Fit view", exact: true }).click();
  await expectClearRoutes(page);
  await expectSeparateChannels(page);
  await canvas.screenshot({ path: testInfo.outputPath("loops-exploration.png") });
  await page.setViewportSize(viewport);
  await page.getByRole("button", { name: "Fit view", exact: true }).click();
  await page.getByRole("button", { name: "Run cyclic components" }).click();
  await expect(page.getByRole("region", { name: "Cyclic components" }).getByText("7 cyclic components found.")).toBeVisible();
  const components = page.getByRole("checkbox", { name: /Cyclic component/ });
  for (const component of await components.all()) await component.check();
  await expectClearRoutes(page);
  await expectSeparateChannels(page);
  await page.screenshot({ path: testInfo.outputPath("loops-components.png") });
  await page.getByRole("button", { name: "Run structural cycles" }).click();
  await page.locator(".cycle-summary-row").filter({ hasText: "alternate.a" }).click();
  await expect(canvas).toHaveAttribute("data-layout-node-count", "2");
  await expectClearRoutes(page);
  await expectSeparateChannels(page);
  await page.screenshot({ path: testInfo.outputPath("loops-alternate-cycle.png") });
});

test("explains focused scope and restores exploration while retaining real SDK results", async ({ page }, testInfo) => {
  test.setTimeout(60_000);
  let topologyRequests = 0;
  page.on("request", (request) => {
    if (new URL(request.url()).pathname === "/api/topology") topologyRequests += 1;
  });
  await page.goto("/");
  await layoutReady(page);
  await page.getByRole("button", { name: "Select none" }).click();
  await page.getByRole("button", { name: "Run cyclic components" }).click();
  await page.getByRole("checkbox", { name: /Cyclic component 1/ }).check();
  await layoutReady(page);
  const toggle = page.getByRole("button", { name: "Results & inspection" });
  await toggle.click();
  const mode = page.getByRole("region", { name: "Graph presentation" });
  await page.locator(".topology-canvas").scrollIntoViewIfNeeded();
  await expect(mode).toBeInViewport();
  await expect(mode).toContainText("Component focus: Cyclic component 1");
  await expect(mode).toContainText("overrides vhost scope: 2 resources");
  await page.getByRole("button", { name: "Clear component filter" }).click();
  await expect(mode).toContainText("Exploration view");
  await expect(page.getByText(/No vhosts selected/)).toBeVisible();
  expect(topologyRequests).toBe(1);

  await page.getByRole("button", { name: "Upload capture" }).click();
  const dialog = page.getByRole("dialog", { name: "Manage captured analysis" });
  await dialog.getByLabel("Analysis ID").fill("e2e-scope-shovel");
  await dialog.getByLabel("Definitions export").setInputFiles(resolve(import.meta.dirname, "fixtures/shovel3.definitions.json"));
  await dialog.getByRole("button", { name: "Upload capture" }).click();
  await expect(dialog.getByText("Uploaded e2e-scope-shovel.")).toBeVisible();
  await dialog.getByRole("button", { name: "Close" }).click();
  await layoutReady(page);
  await page.getByRole("button", { name: "Select none" }).click();
  const scope = page.getByRole("group", { name: "Vhost scope" });
  await scope.getByRole("checkbox", { name: "topology-demo.region-a", exact: true }).check();
  await layoutReady(page);
  const canvas = page.locator(".topology-canvas");
  await expect(canvas).toHaveAttribute("data-layout-node-count", "2");
  await page.getByRole("button", { name: "Run cyclic components" }).click();
  await page.getByRole("checkbox", { name: /Cyclic component 1/ }).check();
  await layoutReady(page);
  await expectLegendMatchesDrawing(page);
  await toggle.click();
  await canvas.scrollIntoViewIfNeeded();
  await expect(mode).toBeInViewport();
  await expect(mode).toContainText("overrides vhost scope: 7 resources");
  await expect(canvas).toHaveAttribute("data-layout-node-count", "9");
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await page.getByRole("button", { name: "Run structural cycles" }).click();
  await expect(page.getByRole("status", { name: "Structural-cycle operation status" })).toContainText("1 structural cycle found.");
  await page.getByRole("button", { name: "Open cycles", exact: true }).click();
  await page.locator(".cycle-summary-row").filter({ hasText: "shovel3.ab" }).click();
  await expect(mode).toContainText("Cycle 1 focus");
  await layoutReady(page);
  await toggle.click();
  await canvas.scrollIntoViewIfNeeded();
  await expect(mode).toBeInViewport();
  await expect(mode).toContainText("Cycle 1 focus");
  await expect(mode).toContainText("overrides vhost scope: 7 resources");
  await expectLegendMatchesDrawing(page);
  const shovelEdge = page.getByRole("button", { name: /^Inspect shovel relationship .* from shovel shovel3.ab in vhost topology-demo.control to exchange shovel3.in in vhost topology-demo.region-b/ });
  await expect(shovelEdge).toHaveCSS("opacity", "1");
  const edgeId = await shovelEdge.getAttribute("data-edge-id");
  await shovelEdge.click();
  await page.getByRole("button", { name: /^Details: relationship/ }).click();
  const details = page.getByRole("region", { name: "Safe details" });
  await expect(details.locator(".relationship-summary")).toHaveText("shovel shovel3.ab in vhost topology-demo.control → exchange shovel3.in in vhost topology-demo.region-b");
  await expect(details.locator(".relationship-summary")).toBeInViewport();
  await expect(details.getByText(edgeId!, { exact: true })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("shovel-relationship-details.png") });
  await toggle.click();
  await page.getByRole("button", { name: "Clear cycle filter" }).click();
  await layoutReady(page);
  await expect(mode).toContainText("Component focus: Cyclic component 1");
  await expect(canvas).toHaveAttribute("data-layout-node-count", "9");
  await page.getByRole("button", { name: "Clear component filter" }).click();
  await layoutReady(page);
  await expect(mode).toContainText("Exploration view");
  await expect(mode).not.toContainText("overrides");
  await expect(canvas).toHaveAttribute("data-layout-node-count", "2");
  await expect(scope.getByRole("checkbox", { name: "topology-demo.region-a", exact: true })).toBeChecked();
  await expect(scope.getByRole("checkbox", { name: "topology-demo.region-b", exact: true })).not.toBeChecked();
  await page.getByRole("button", { name: "Components (1)", exact: true }).click();
  await expect(page.getByRole("checkbox", { name: /Cyclic component 1/ })).not.toBeChecked();
  await page.getByRole("tab", { name: "Cycles", exact: true }).click();
  await expect(page.locator(".cycle-summary-row")).toHaveCount(1);
  expect(topologyRequests).toBe(2);
});

for (const motion of ["reduce", "no-preference"] as const) {
  test(`inspects exact real-SDK parallel relationships and matching legend patterns (${motion})`, async ({ page }, testInfo) => {
    test.setTimeout(60_000);
    await page.emulateMedia({ reducedMotion: motion });
    const capture = JSON.parse(await readFile(resolve(import.meta.dirname, "fixtures/loops.definitions.json"), "utf8"));
    capture.bindings.push({ ...capture.bindings[0], routing_key: "parallel" });
    await page.goto("/");
    await page.getByRole("button", { name: "Upload capture" }).click();
    const dialog = page.getByRole("dialog", { name: "Manage captured analysis" });
    await dialog.getByLabel("Analysis ID").fill("e2e-parallel-inspection");
    await dialog.getByLabel("Definitions export").setInputFiles({
      name: "loops.parallel.definitions.json", mimeType: "application/json", buffer: Buffer.from(JSON.stringify(capture)),
    });
    await dialog.getByRole("button", { name: "Upload capture" }).click();
    await expect(dialog.getByText("Uploaded e2e-parallel-inspection.")).toBeVisible();
    await dialog.getByRole("button", { name: "Close" }).click();
    await layoutReady(page);
    await expectLegendMatchesDrawing(page);
    const canvas = page.locator(".topology-canvas");
    const parallel = canvas.getByRole("button", { name: /^Inspect binding relationship .* from exchange binding.a in vhost topology-demo.loops to exchange binding.b in vhost topology-demo.loops/ });
    await expect(parallel).toHaveCount(2);
    const ids = await parallel.evaluateAll((buttons) => buttons.map((button) => button.getAttribute("data-edge-id")));
    expect(new Set(ids).size).toBe(2);
    const details = page.getByRole("region", { name: "Safe details" });
    for (const [index, id] of ids.entries()) {
      const button = canvas.locator(`.edge-inspect-button[data-edge-id="${id}"]`);
      await expect(button).toHaveCSS("opacity", "1");
      if (index === 0) await button.click();
      else {
        await button.focus();
        await page.keyboard.press("Enter");
      }
      await expect(details.getByText(id!, { exact: true })).toBeVisible();
      await expect(details.locator(".relationship-summary")).toHaveText("exchange binding.a in vhost topology-demo.loops → exchange binding.b in vhost topology-demo.loops");
    }
    await page.getByRole("button", { name: "Run cyclic components" }).click();
    await page.locator(".scc-component-row").filter({ hasText: "binding.a" }).getByRole("checkbox").check();
    await expect(canvas).toHaveAttribute("data-layout-node-count", "3");
    await layoutReady(page);
    await expectLegendMatchesDrawing(page);
    await page.getByRole("button", { name: "Run structural cycles" }).click();
    await page.locator(".cycle-summary-row").filter({ hasText: "binding.a" }).first().click();
    await expect(page.getByRole("region", { name: "Graph presentation" })).toContainText(/Cycle \d+ focus/);
    await expect(canvas.locator(".topology-edge:not(.topology-edge--cycle)")).toHaveCount(0);
    await layoutReady(page);
    await expectLegendMatchesDrawing(page);
    const path = canvas.locator(".topology-edge--cycle").first();
    await expect(path).toHaveCSS("animation-name", motion === "reduce" ? "none" : "cycle-flow");
    const selectedEdge = canvas.locator(".edge-inspect-button").first();
    const selectedId = await selectedEdge.getAttribute("data-edge-id");
    await selectedEdge.focus();
    await page.keyboard.press("Enter");
    await expect(details.getByText(selectedId!, { exact: true })).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath(`parallel-inspection-${motion}.png`) });
  });
}

test("gives the real SDK three-region shovel cycle separate channels and container clearance", async ({ page }, testInfo) => {
  test.setTimeout(60_000);
  await page.goto("/");
  await page.getByRole("button", { name: "Upload capture" }).click();
  const dialog = page.getByRole("dialog", { name: "Manage captured analysis" });
  await expect(dialog.getByRole("button", { name: "Close" })).toBeFocused();
  await dialog.getByLabel("Analysis ID").fill("e2e-shovel-three");
  await dialog.getByLabel("Definitions export").setInputFiles(resolve(import.meta.dirname, "fixtures/shovel3.definitions.json"));
  await dialog.getByRole("button", { name: "Upload capture" }).click();
  await expect(dialog.getByText("Uploaded e2e-shovel-three.")).toBeVisible();
  await dialog.getByRole("button", { name: "Close" }).click();
  const canvas = page.locator(".topology-canvas");
  await expect(canvas).toHaveAttribute("data-layout-node-count", "9");
  await expectClearRoutes(page);
  await expectSeparateChannels(page);
  await page.getByRole("button", { name: "Run structural cycles" }).click();
  await page.locator(".cycle-summary-row").filter({ hasText: "shovel3.ab" }).click();
  await expectClearRoutes(page);
  await expectSeparateChannels(page);
  await page.screenshot({ path: testInfo.outputPath("shovel3-cycle.png") });
});
