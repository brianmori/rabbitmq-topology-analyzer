import { expect, test } from "@playwright/test";
import {
  basicOrigin,
  basicPassword,
  browserContext,
  browserGet,
  harnessState,
  observeFetchHeaders,
  assertNoExplicitAuthorization,
  readyGraph,
  runScc,
  runCycle,
  signOut,
  oidcOrigin,
  sessionStorageKey,
  uploadProbe,
  upload,
  type Profile,
} from "./support.js";
import { randomUUID } from "node:crypto";

test("uses browser-managed HTTP Basic credentials through the real challenge", async ({
  browser,
  request,
}) => {
  const context = await browserContext(browser, basicOrigin, "analyst");
  try {
    const page = await context.newPage();
    await observeFetchHeaders(page);
    await context.setExtraHTTPHeaders({ "X-Test-Probe": "basic-native-challenge" });
    await page.goto(basicOrigin);
    await readyGraph(page);
    await expect(page.getByRole("button", { name: "Upload capture" })).toBeVisible();
    // Chromium handles the first 401 internally. Observe the real server,
    // rather than expecting that intermediate response in the page event API.
    expect(
      (await harnessState(request, basicOrigin)).probes["basic-native-challenge"]
        .unauthorizedResponses,
    ).toBeGreaterThan(0);
    expect(await page.locator('input[type="password"]').count()).toBe(0);
    await assertNoExplicitAuthorization(page);
  } finally {
    await context.close();
  }
});

for (const profile of ["viewer", "analyst", "no-role"] satisfies Profile[]) {
  test(`enforces the real Basic ${profile} capability view and backend role checks`, async ({
    browser,
    request,
  }) => {
    const context = await browserContext(browser, basicOrigin, profile);
    try {
      const page = await context.newPage();
      await observeFetchHeaders(page);
      await page.goto(basicOrigin);
      if (profile === "no-role") {
        await expect(
          page.getByRole("heading", { name: "Topology access unavailable" }),
        ).toBeVisible();
        expect(
          (
            await browserGet(
              page,
              "/api/topology?source=static&analysis_id=e2e-static-cycle",
              randomUUID(),
            )
          ).status,
        ).toBe(403);
        await expect(page.getByRole("button", { name: "Run cyclic components" })).toHaveCount(0);
        await expect(page.getByRole("button", { name: "Run structural cycles" })).toHaveCount(0);
        for (const operation of ["scc-runs", "cycle-runs"]) {
          const denied = await context.request.post(`${basicOrigin}/api/${operation}`, {
            headers: { "X-Session-Token": randomUUID(), "Idempotency-Key": randomUUID() },
            data: {
              source: "static",
              analysisId: "e2e-static-cycle",
              snapshotRevision: `sha256:${"a".repeat(64)}`,
            },
          });
          expect(denied.status()).toBe(403);
        }
      } else {
        await readyGraph(page);
        await runScc(page);
        await runCycle(page);
      }
      await expect(page.getByRole("button", { name: "Upload capture" })).toHaveCount(
        profile === "analyst" ? 1 : 0,
      );
      if (profile !== "analyst") {
        const probe = `basic-role-${profile}`;
        const owner =
          (await page.evaluate((key) => sessionStorage.getItem(key), sessionStorageKey)) ??
          randomUUID();
        expect((await uploadProbe(page, probe, { "X-Session-Token": owner })).status).toBe(403);
        expect((await harnessState(request, basicOrigin)).probes[probe]).toMatchObject({
          bodyReads: 0,
          status: 403,
        });
      }
      await assertNoExplicitAuthorization(page);
      expect(
        await page.evaluate(() => JSON.stringify({ ...sessionStorage, ...localStorage })),
      ).not.toContain("local-browser-test-password");
    } finally {
      await context.close();
    }
  });
}

test("keeps bootstrap and health anonymous-safe, rejects credentials before upload parsing, and retries a cancelled native challenge", async ({
  browser,
  request,
}) => {
  const context = await browserContext(browser, basicOrigin);
  try {
    const page = await context.newPage();
    await observeFetchHeaders(page);
    await page.goto(basicOrigin);
    await expect(page.getByRole("button", { name: "Authenticate" })).toBeVisible();
    await expect(page.getByText(/native HTTP Basic credential prompt/i)).toBeVisible();
    await expect(page.locator(".topology-canvas")).toHaveCount(0);
    const bootstrap = await context.request.get(`${basicOrigin}/api/auth/session`);
    expect(bootstrap.status()).toBe(200);
    expect(await bootstrap.json()).toEqual({
      authMode: "basic",
      authenticated: false,
      loginPath: null,
      logoutPath: null,
      csrfToken: null,
    });
    const challenge = await context.request.get(`${basicOrigin}/api/access`);
    expect(challenge.status()).toBe(401);
    expect(challenge.headers()["www-authenticate"]).toBe(
      'Basic realm="Local browser tests", charset="UTF-8"',
    );
    for (const path of ["/api/health/live", "/api/health/ready"])
      expect((await context.request.get(`${basicOrigin}${path}`)).status()).toBe(200);
    const wrongCredentials = "Basic " + Buffer.from("analyst:wrong-password").toString("base64");
    const invalid = await context.request.post(`${basicOrigin}/api/definitions/upload`, {
      headers: { Authorization: wrongCredentials, "X-Test-Probe": "basic-invalid-credential" },
      multipart: {
        analysis_id: "forbidden-upload",
        definitions_file: {
          name: "definitions.json",
          mimeType: "application/json",
          buffer: Buffer.alloc(1024 * 1024, "x"),
        },
      },
    });
    expect(invalid.status()).toBe(401);
    expect(await invalid.json()).toMatchObject({ error: { code: "unauthorized", issues: [] } });
    expect(
      (await harnessState(request, basicOrigin)).probes["basic-invalid-credential"],
    ).toMatchObject({ bodyReads: 0, status: 401 });
    // No successful credentials have been cached. This changes only the
    // automation's native challenge answer; it never supplies credentials to JS.
    await context.setHTTPCredentials({ username: "analyst", password: basicPassword });
    await page.getByRole("button", { name: "Authenticate" }).click();
    await readyGraph(page);
    await assertNoExplicitAuthorization(page);
    await signOut(page);
  } finally {
    await context.close();
  }
});

test("isolates native Basic sessions and cleans uploads, SDK results, and idempotency for exactly one owner", async ({
  browser,
  request,
}) => {
  const owner = await browserContext(browser, basicOrigin, "analyst");
  const other = await browserContext(browser, basicOrigin, "analyst");
  try {
    const page = await owner.newPage();
    const otherPage = await other.newPage();
    await page.goto(basicOrigin);
    await otherPage.goto(basicOrigin);
    await readyGraph(page);
    await readyGraph(otherPage);
    const analysis = await upload(page, "basic-owner-a");
    const startRequest = page.waitForRequest(
      (value) => value.method() === "POST" && new URL(value.url()).pathname === "/api/scc-runs",
    );
    const scc = await runScc(page);
    const originalStart = await startRequest;
    const cycles = await runCycle(page);
    await upload(otherPage, "basic-owner-b");
    const otherRun = await runScc(otherPage);
    const ownerToken = await page.evaluate((key) => sessionStorage.getItem(key), sessionStorageKey);
    const otherToken = await otherPage.evaluate(
      (key) => sessionStorage.getItem(key),
      sessionStorageKey,
    );
    expect(ownerToken).toMatch(/^[a-f0-9-]{36}$/);
    expect(otherToken).not.toBe(ownerToken);
    const query = `?snapshot_revision=${encodeURIComponent(analysis.snapshotRevision)}`;
    const missing = await browserGet(
      otherPage,
      "/api/topology?source=session&analysis_id=unknown-private",
      otherToken!,
    );
    expect(missing.status).toBe(404);
    for (const path of [
      "/api/topology?source=session&analysis_id=basic-owner-a",
      `/api/scc-runs/${scc.runId}${query}`,
      `/api/cycle-runs/${cycles.runId}${query}`,
      `/api/cycle-runs/${cycles.runId}/cycles${query}`,
      `/api/cycle-runs/${cycles.runId}/cycles/0${query}`,
    ])
      expect(await browserGet(otherPage, path, otherToken!)).toEqual(missing);
    for (const path of [
      "/api/analyses/basic-owner-a",
      `/api/scc-runs/${scc.runId}${query}`,
      `/api/cycle-runs/${cycles.runId}${query}`,
    ]) {
      const rejected = await other.request.delete(`${basicOrigin}${path}`, {
        headers: { "X-Session-Token": otherToken! },
      });
      expect(rejected.status()).toBe(404);
      expect(await rejected.json()).toEqual(missing.body);
    }
    const before = await harnessState(request, basicOrigin);
    const logout = await signOut(page);
    expect((await logout.allHeaders())["clear-site-data"]).toBe('"cache", "cookies", "storage"');
    const after = await harnessState(request, basicOrigin);
    expect(after.uploads).toBe(before.uploads - 1);
    expect(after.runs).toBe(before.runs - 2);
    expect(after.resultFiles).toBe(before.resultFiles - 2);
    expect(await page.evaluate(() => ({ ...sessionStorage }))).toEqual({});
    await expect(page.getByText(/may retain and resend HTTP Basic credentials/i)).toBeVisible();
    expect(
      (
        await browserGet(
          otherPage,
          "/api/topology?source=session&analysis_id=basic-owner-b",
          otherToken!,
        )
      ).status,
    ).toBe(200);
    expect(
      (await browserGet(otherPage, `/api/scc-runs/${otherRun.runId}${query}`, otherToken!)).status,
    ).toBe(200);
    const oldRun = await owner.request.get(`${basicOrigin}/api/scc-runs/${scc.runId}${query}`, {
      headers: { "X-Session-Token": ownerToken! },
    });
    expect(oldRun.status()).toBe(404);
    // Reuse the prior logical owner and key through the browser credential
    // context: a fresh run proves the deleted key does not replay retained data.
    const replay = await owner.request.post(`${basicOrigin}/api/scc-runs`, {
      headers: {
        "X-Session-Token": ownerToken!,
        "Idempotency-Key": originalStart.headers()["idempotency-key"],
      },
      data: {
        source: "static",
        analysisId: "e2e-static-cycle",
        snapshotRevision: scc.snapshotRevision,
      },
    });
    expect(replay.status()).toBe(202);
    expect((await replay.json()).runId).not.toBe(scc.runId);
    expect(
      (
        await owner.request.post(`${basicOrigin}/api/auth/logout`, {
          headers: { "X-Session-Token": ownerToken! },
        })
      ).status(),
    ).toBe(204);
    await signOut(otherPage);
  } finally {
    await owner.close();
    await other.close();
  }
});

test("binds Basic ownership to the verified user as well as the logical browser UUID", async ({
  browser,
}) => {
  const owner = await browserContext(browser, basicOrigin, "analyst");
  const viewer = await browserContext(browser, basicOrigin, "viewer");
  try {
    const page = await owner.newPage();
    await page.goto(basicOrigin);
    await readyGraph(page);
    await upload(page, "basic-principal-owned");
    const run = await runScc(page);
    const token = await page.evaluate((key) => sessionStorage.getItem(key), sessionStorageKey);
    const viewerPage = await viewer.newPage();
    await viewerPage.goto(basicOrigin);
    await readyGraph(viewerPage);
    expect(
      (
        await browserGet(
          viewerPage,
          "/api/topology?source=session&analysis_id=basic-principal-owned",
          token!,
        )
      ).status,
    ).toBe(404);
    expect(
      (
        await browserGet(
          viewerPage,
          `/api/scc-runs/${run.runId}?snapshot_revision=${encodeURIComponent(run.snapshotRevision)}`,
          token!,
        )
      ).status,
    ).toBe(404);
    await signOut(page);
  } finally {
    await owner.close();
    await viewer.close();
  }
});

test("does not offer Basic as an OIDC bypass and keeps mode bootstrap contracts distinct", async ({
  browser,
  request,
}) => {
  const context = await browserContext(browser, basicOrigin, "analyst");
  try {
    const page = await context.newPage();
    await page.goto(basicOrigin);
    await readyGraph(page);
    const basicCookie = await context.cookies(basicOrigin);
    expect(basicCookie).toEqual([]);
    const basicCredential = "Basic " + Buffer.from(`analyst:${basicPassword}`).toString("base64");
    const oidc = await request.get(`${oidcOrigin}/api/access`, {
      ignoreHTTPSErrors: true,
      headers: { Authorization: basicCredential },
    });
    expect(oidc.status()).toBe(401);
    expect(oidc.headers()["www-authenticate"]).toBeUndefined();
    const bootstrap = await request.get(`${oidcOrigin}/api/auth/session`, {
      ignoreHTTPSErrors: true,
    });
    expect(await bootstrap.json()).toMatchObject({
      authMode: "oidc",
      authenticated: false,
      loginPath: "/api/auth/oidc/login",
    });
    expect(
      (
        await context.request.get(`${basicOrigin}/api/auth/oidc/login`, { maxRedirects: 0 })
      ).status(),
    ).toBe(404);
    const none = await request.get("http://127.0.0.1:4174/api/auth/session");
    expect(await none.json()).toEqual({
      authMode: "none",
      authenticated: true,
      loginPath: null,
      logoutPath: null,
      csrfToken: null,
    });
  } finally {
    await context.close();
  }
});
