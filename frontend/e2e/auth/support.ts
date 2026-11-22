import {
  expect,
  type APIRequestContext,
  type Browser,
  type BrowserContext,
  type Page,
  type Response,
} from "@playwright/test";
import { resolve } from "node:path";

export const oidcOrigin = "https://127.0.0.1:4176";
export const basicOrigin = "https://127.0.0.1:4177";
export const providerOrigin = "http://localhost:4175";
export const sessionCookieName = "__Host-browser_session";
export const sessionStorageKey = "rabbitmq-topology-session-token";
export const basicPassword = "local-browser-test-password";
export const definitions = resolve(
  import.meta.dirname,
  "../fixtures/static/cycle.definitions.json",
);
export type Profile = "analyst" | "viewer" | "no-role";

export interface HarnessState {
  seconds: number;
  authSessions: number;
  uploads: number;
  runs: number;
  activeProcesses: number;
  resultFiles: number;
  probes: Record<string, { bodyReads: number; status: number; unauthorizedResponses: number }>;
}

export interface ProviderState {
  seconds: number;
  discovery: number;
  jwks: number;
  exchange: number;
  refresh: number;
  pkceVerified: number;
  revocations: number;
}

export async function providerState(request: APIRequestContext): Promise<ProviderState> {
  const response = await request.get(`${providerOrigin}/__test/state`);
  expect(response.status()).toBe(200);
  return response.json();
}

export async function browserContext(
  browser: Browser,
  origin: string,
  profile?: Profile,
): Promise<BrowserContext> {
  const context = await browser.newContext({
    baseURL: origin,
    ignoreHTTPSErrors: true,
    reducedMotion: "reduce",
    serviceWorkers: "block",
    ...(profile ? { httpCredentials: { username: profile, password: basicPassword, origin } } : {}),
  });
  // This guard only forbids external traffic; it never mocks an API response.
  await context.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (
      ![origin, providerOrigin, oidcOrigin, basicOrigin, "http://127.0.0.1:4174"].includes(
        url.origin,
      )
    ) {
      throw new Error(`Unexpected browser network origin: ${url.origin}`);
    }
    await route.continue();
  });
  return context;
}

export function apiResponse(response: Response, method: string, path: string): boolean {
  return response.request().method() === method && new URL(response.url()).pathname === path;
}

export async function providerControl(
  request: APIRequestContext,
  values: Record<string, unknown>,
): Promise<ProviderState> {
  const response = await request.post(`${providerOrigin}/__test/control`, { data: values });
  expect(response.status()).toBe(200);
  return response.json();
}

export async function harnessState(
  request: APIRequestContext,
  origin: string,
): Promise<HarnessState> {
  const response = await request.get(`${origin}/__test/state`, { ignoreHTTPSErrors: true });
  expect(response.status()).toBe(200);
  return response.json();
}

export async function advanceOidcClock(request: APIRequestContext, seconds: number): Promise<void> {
  const state = await harnessState(request, oidcOrigin);
  const next = state.seconds + seconds;
  await providerControl(request, { seconds: next });
  const response = await request.post(`${oidcOrigin}/__test/clock`, {
    data: { seconds: next },
    ignoreHTTPSErrors: true,
  });
  expect(response.status()).toBe(200);
}

export async function loginOidc(page: Page, profile: Profile): Promise<Response> {
  await page.goto(oidcOrigin);
  await expect(page.getByRole("link", { name: "Sign in" })).toBeVisible();
  await page.getByRole("link", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Local OIDC provider" })).toBeVisible();
  await page.getByLabel("Test identity").selectOption(profile);
  const callback = page.waitForResponse((response) =>
    apiResponse(response, "GET", "/api/auth/oidc/callback"),
  );
  await page.getByRole("button", { name: "Continue" }).click();
  return callback;
}

export async function readyGraph(page: Page): Promise<void> {
  await expect(page.locator(".topology-canvas")).toHaveAttribute("data-layout-state", "ready");
  await expect(page.getByRole("button", { name: "Inspect queue loop-q, vhost /" })).toBeVisible();
}

export async function upload(
  page: Page,
  analysisId: string,
): Promise<{ snapshotRevision: string }> {
  await page.getByRole("button", { name: "Upload capture" }).click();
  const dialog = page.getByRole("dialog", { name: "Manage captured analysis" });
  await dialog.getByLabel("Analysis ID").fill(analysisId);
  await dialog.getByLabel("Definitions export").setInputFiles(definitions);
  const response = page.waitForResponse((value) =>
    apiResponse(value, "POST", "/api/definitions/upload"),
  );
  await dialog.getByRole("button", { name: "Upload capture" }).click();
  const created = await response;
  expect(created.status()).toBe(201);
  const payload = await created.json();
  await expect(dialog.getByText(`Uploaded ${analysisId}.`)).toBeVisible();
  await dialog.getByRole("button", { name: "Close" }).click();
  await readyGraph(page);
  return payload.analysis;
}

export async function runScc(page: Page): Promise<{ runId: string; snapshotRevision: string }> {
  const start = page.waitForResponse((value) => apiResponse(value, "POST", "/api/scc-runs"));
  await page.getByRole("button", { name: "Run cyclic components" }).click();
  const response = await start;
  expect(response.status()).toBe(202);
  const run = await response.json();
  await expect(
    page.getByRole("region", { name: "Cyclic components" }).getByText("1 cyclic component found."),
  ).toBeVisible({ timeout: 20_000 });
  return run;
}

export async function signOut(page: Page): Promise<Response> {
  const response = page.waitForResponse((value) => apiResponse(value, "POST", "/api/auth/logout"));
  await page.getByRole("button", { name: "Sign out" }).click();
  const result = await response;
  expect(result.status()).toBe(204);
  await expect(page.getByText("Signed out")).toBeVisible();
  return result;
}

export async function runCycle(page: Page): Promise<{ runId: string; snapshotRevision: string }> {
  const start = page.waitForResponse((value) => apiResponse(value, "POST", "/api/cycle-runs"));
  await page.getByRole("button", { name: "Run structural cycles" }).click();
  const response = await start;
  expect(response.status()).toBe(202);
  const run = await response.json();
  await expect(
    page.getByRole("region", { name: "Structural cycles" }).getByText("1 structural cycle found."),
  ).toBeVisible({ timeout: 20_000 });
  await page.getByRole("button", { name: /Cycle 1/ }).click();
  const path = page.getByRole("article", { name: "Cycle 1 path" });
  await expect(path.getByRole("button", { name: "Clear cycle filter" })).toBeVisible();
  return run;
}

/** Test-driver access only; components continue to obtain CSRF solely in src/api. */
export async function csrf(context: BrowserContext): Promise<string> {
  const response = await context.request.get(`${oidcOrigin}/api/auth/session`);
  expect(response.status()).toBe(200);
  const payload = await response.json();
  expect(payload.csrfToken).toEqual(expect.any(String));
  return payload.csrfToken;
}

export async function uploadProbe(
  page: Page,
  probe: string,
  headers: Record<string, string> = {},
): Promise<{ status: number; body: unknown }> {
  return page.evaluate(
    async ({ probe, headers }) => {
      const form = new FormData();
      form.set("analysis_id", "forbidden-upload");
      form.set(
        "definitions_file",
        new Blob(["x".repeat(1024 * 1024)], { type: "application/json" }),
        "definitions.json",
      );
      const response = await fetch("/api/definitions/upload", {
        method: "POST",
        credentials: "same-origin",
        headers: { ...headers, "X-Test-Probe": probe },
        body: form,
      });
      return { status: response.status, body: await response.json() };
    },
    { probe, headers },
  );
}

/** Inspect explicit fetch header names without retaining any credential value. */
export async function observeFetchHeaders(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const calls: string[][] = [];
    const instrumented = window as typeof window & { __testFetchHeaderNames: string[][] };
    instrumented.__testFetchHeaderNames = calls;
    const nativeFetch = window.fetch.bind(window);
    window.fetch = (input, init) => {
      const headers = new Headers(
        init?.headers ?? (input instanceof Request ? input.headers : undefined),
      );
      calls.push([...headers.keys()]);
      return nativeFetch(input, init);
    };
  });
}

export async function assertNoExplicitAuthorization(page: Page): Promise<void> {
  const headers = await page.evaluate(
    () => (window as typeof window & { __testFetchHeaderNames: string[][] }).__testFetchHeaderNames,
  );
  expect(headers.length).toBeGreaterThan(0);
  expect(headers.flat()).not.toContain("authorization");
}

export function observeOidcBoundary(page: Page) {
  const responses: Promise<string>[] = [];
  const requests: Promise<string>[] = [];
  const providerPaths: string[] = [];
  const consoles: string[] = [];
  page.on("console", (message) => consoles.push(message.text()));
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.origin === providerOrigin) providerPaths.push(url.pathname);
    if (url.origin === oidcOrigin) {
      requests.push(
        request.allHeaders().then((headers) => JSON.stringify({ url: request.url(), headers })),
      );
    }
  });
  page.on("response", (response) => {
    if (
      new URL(response.url()).origin === oidcOrigin &&
      new URL(response.url()).pathname.startsWith("/api/")
    ) {
      responses.push(response.text().catch(() => ""));
    }
  });
  return async () => {
    const browserState = await page.evaluate(() => ({
      html: document.documentElement.outerHTML,
      localStorage: { ...localStorage },
      sessionStorage: { ...sessionStorage },
      readableCookies: document.cookie,
    }));
    const observed = JSON.stringify({
      browserState,
      cookies: await page.context().cookies(),
      requests: await Promise.all(requests),
      responses: await Promise.all(responses),
      consoles,
    });
    const observedRequests = (await Promise.all(requests)).join("");
    expect(observedRequests).not.toContain('"authorization"');
    expect(observedRequests).not.toContain('"x-session-token"');
    for (const forbidden of [
      "browser-provider-access-",
      "browser-provider-refresh-",
      "provider_claim_marker",
      "browser-provider-claim-must-stay-server-side",
      "browser-user-",
      "local-test-client-secret",
    ]) {
      expect(observed).not.toContain(forbidden);
    }
    expect(observed).not.toMatch(/eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/);
    expect(providerPaths.every((path) => path === "/authorize")).toBe(true);
    expect(await page.evaluate((key) => sessionStorage.getItem(key), sessionStorageKey)).toBeNull();
    await assertNoExplicitAuthorization(page);
  };
}

/** A browser-originated request, including the browser's real cookies/Origin. */
export async function browserGet(
  page: Page,
  path: string,
  ownerToken?: string,
): Promise<{ status: number; body: unknown }> {
  return page.evaluate(
    async ({ path, ownerToken }) => {
      const response = await fetch(path, {
        credentials: "same-origin",
        headers: ownerToken ? { "X-Session-Token": ownerToken } : {},
      });
      return { status: response.status, body: await response.json() };
    },
    { path, ownerToken },
  );
}
