import { expect, test } from "@playwright/test";
import {
  browserContext,
  apiResponse,
  advanceOidcClock,
  basicOrigin,
  browserGet,
  csrf,
  harnessState,
  loginOidc,
  observeFetchHeaders,
  observeOidcBoundary,
  oidcOrigin,
  providerControl,
  providerOrigin,
  providerState,
  readyGraph,
  runScc,
  runCycle,
  signOut,
  sessionCookieName,
  uploadProbe,
  upload,
  type Profile,
} from "./support.js";

test.beforeEach(async ({ request }) => {
  await providerControl(request, {
    fault: "none",
    refreshDenied: false,
    revocationDenied: false,
    refreshProfile: null,
  });
});

test("configured origins match Chromium serialization for default ports, DNS case and IPv6", async ({
  page,
  request,
}) => {
  const response = await request.get(`${oidcOrigin}/__test/public-origins`);
  expect(response.status()).toBe(200);
  const configured: Array<{ configured: string; canonical: string }> = await response.json();
  const serialized = await page.evaluate(
    (origins) =>
      origins.map(({ configured }) => ({ configured, canonical: new URL(configured).origin })),
    configured,
  );
  expect(configured).toEqual(serialized);
});

test("completes a real OIDC browser redirect with Secure opaque application cookies", async ({
  browser,
}) => {
  const context = await browserContext(browser, oidcOrigin);
  try {
    const page = await context.newPage();
    await observeFetchHeaders(page);
    const assertBoundary = observeOidcBoundary(page);
    const callback = await loginOidc(page, "analyst");
    expect(callback.status()).toBe(303);
    await readyGraph(page);
    const cookie = (await context.cookies(oidcOrigin)).find(
      (value) => value.name === sessionCookieName,
    );
    expect(cookie).toMatchObject({
      httpOnly: true,
      secure: true,
      sameSite: "Lax",
      domain: "127.0.0.1",
      path: "/",
    });
    expect(cookie?.value).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(await page.evaluate(() => document.cookie)).not.toContain(sessionCookieName);
    expect(await context.cookies(oidcOrigin)).toHaveLength(1);
    await assertBoundary();
  } finally {
    await context.close();
  }
});

for (const profile of ["viewer", "analyst", "no-role"] satisfies Profile[]) {
  test(`enforces the real OIDC ${profile} capability view and backend role checks`, async ({
    browser,
    request,
  }) => {
    const context = await browserContext(browser, oidcOrigin);
    try {
      const page = await context.newPage();
      await observeFetchHeaders(page);
      const assertBoundary = observeOidcBoundary(page);
      expect((await loginOidc(page, profile)).status()).toBe(303);
      if (profile === "no-role") {
        await expect(
          page.getByRole("heading", { name: "Topology access unavailable" }),
        ).toBeVisible();
        expect(
          (await browserGet(page, "/api/topology?source=static&analysis_id=e2e-static-cycle"))
            .status,
        ).toBe(403);
        await expect(page.getByRole("button", { name: "Run cyclic components" })).toHaveCount(0);
        await expect(page.getByRole("button", { name: "Run structural cycles" })).toHaveCount(0);
        for (const operation of ["scc-runs", "cycle-runs"]) {
          const denied = await context.request.post(`${oidcOrigin}/api/${operation}`, {
            headers: {
              Origin: oidcOrigin,
              "X-CSRF-Token": await csrf(context),
              "Idempotency-Key": crypto.randomUUID(),
            },
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
        const probe = `oidc-role-${profile}`;
        const denied = await uploadProbe(page, probe, { "X-CSRF-Token": await csrf(context) });
        expect(denied.status).toBe(403);
        expect((await harnessState(request, oidcOrigin)).probes[probe]).toMatchObject({
          bodyReads: 0,
          status: 403,
        });
      }
      await assertBoundary();
    } finally {
      await context.close();
    }
  });
}

test("preserves private uploads and both SDK results across real server-side refresh and key rotation", async ({
  browser,
  request,
}) => {
  const context = await browserContext(browser, oidcOrigin);
  try {
    const page = await context.newPage();
    await observeFetchHeaders(page);
    const assertBoundary = observeOidcBoundary(page);
    await loginOidc(page, "analyst");
    await readyGraph(page);
    await upload(page, "oidc-refresh-owned");
    const scc = await runScc(page);
    const cycles = await runCycle(page);
    const cookieBefore = (await context.cookies(oidcOrigin)).find(
      (cookie) => cookie.name === sessionCookieName,
    );
    const protocolBefore = await providerState(request);
    await providerControl(request, { rotate: true });
    await advanceOidcClock(request, 120);

    await page.reload();
    await readyGraph(page);
    await expect(
      page
        .getByRole("combobox", { name: "Analysis" })
        .getByRole("option", { name: /oidc-refresh-owned/ }),
    ).toHaveCount(1);
    const runQuery = `?snapshot_revision=${encodeURIComponent(scc.snapshotRevision)}`;
    expect((await browserGet(page, `/api/scc-runs/${scc.runId}${runQuery}`)).status).toBe(200);
    expect(
      (await browserGet(page, `/api/cycle-runs/${cycles.runId}/cycles${runQuery}`)).status,
    ).toBe(200);
    expect(
      (await browserGet(page, `/api/cycle-runs/${cycles.runId}/cycles/0${runQuery}`)).status,
    ).toBe(200);
    expect(
      (await context.cookies(oidcOrigin)).find((cookie) => cookie.name === sessionCookieName)
        ?.value,
    ).toBe(cookieBefore?.value);
    const protocolAfter = await providerState(request);
    expect(protocolAfter.refresh).toBe(protocolBefore.refresh + 1);
    expect(protocolAfter.jwks).toBe(protocolBefore.jwks + 1);
    expect(protocolAfter.discovery).toBeGreaterThanOrEqual(1);
    await assertBoundary();
    await signOut(page);
  } finally {
    await context.close();
  }
});

test("applies refreshed signed roles without changing private resource ownership", async ({
  browser,
  request,
}) => {
  const context = await browserContext(browser, oidcOrigin);
  try {
    const page = await context.newPage();
    await observeFetchHeaders(page);
    const assertBoundary = observeOidcBoundary(page);
    await loginOidc(page, "analyst");
    await readyGraph(page);
    await upload(page, "oidc-downgraded-owner");
    const run = await runScc(page);
    await providerControl(request, { refreshProfile: "viewer" });
    await advanceOidcClock(request, 120);
    await page.reload();
    await readyGraph(page);
    await expect(page.getByRole("button", { name: "Upload capture" })).toHaveCount(0);
    expect(
      (await browserGet(page, "/api/topology?source=session&analysis_id=oidc-downgraded-owner"))
        .status,
    ).toBe(200);
    expect(
      (
        await browserGet(
          page,
          `/api/scc-runs/${run.runId}?snapshot_revision=${encodeURIComponent(run.snapshotRevision)}`,
        )
      ).status,
    ).toBe(200);
    expect(
      (await uploadProbe(page, "oidc-refreshed-viewer", { "X-CSRF-Token": await csrf(context) }))
        .status,
    ).toBe(403);
    expect((await harnessState(request, oidcOrigin)).probes["oidc-refreshed-viewer"]).toMatchObject(
      { bodyReads: 0, status: 403 },
    );
    await assertBoundary();
    await signOut(page);
  } finally {
    await context.close();
  }
});

test("rejects unsafe browser uploads without CSRF and foreign-origin requests before body reads", async ({
  browser,
  request,
}) => {
  const context = await browserContext(browser, oidcOrigin);
  try {
    const page = await context.newPage();
    await loginOidc(page, "analyst");
    await readyGraph(page);
    const cookieBefore = (await context.cookies(oidcOrigin)).find(
      (cookie) => cookie.name === sessionCookieName,
    );
    for (const [probe, headers] of [
      ["oidc-csrf-missing", {}],
      ["oidc-csrf-invalid", { "X-CSRF-Token": "wrong-csrf" }],
    ] as const) {
      expect((await uploadProbe(page, probe, headers)).status).toBe(403);
      expect((await harnessState(request, oidcOrigin)).probes[probe]).toMatchObject({
        bodyReads: 0,
        status: 403,
      });
    }
    const originProbe = "oidc-origin-invalid";
    const foreignOrigin = await context.request.post(`${oidcOrigin}/api/definitions/upload`, {
      headers: {
        Origin: basicOrigin,
        "X-CSRF-Token": await csrf(context),
        "X-Test-Probe": originProbe,
      },
      multipart: {
        analysis_id: "forbidden-upload",
        definitions_file: {
          name: "definitions.json",
          mimeType: "application/json",
          buffer: Buffer.alloc(1024 * 1024, "x"),
        },
      },
    });
    expect(foreignOrigin.status()).toBe(403);
    expect((await harnessState(request, oidcOrigin)).probes[originProbe]).toMatchObject({
      bodyReads: 0,
      status: 403,
    });
    expect(
      (await context.cookies(oidcOrigin)).find((cookie) => cookie.name === sessionCookieName)
        ?.value,
    ).toBe(cookieBefore?.value);
    await upload(page, "oidc-csrf-valid");
    await signOut(page);
  } finally {
    await context.close();
  }
});

test("rejects a genuine cross-origin browser form POST with the application cookie", async ({
  browser,
  request,
}) => {
  const context = await browserContext(browser, oidcOrigin);
  try {
    const page = await context.newPage();
    await loginOidc(page, "analyst");
    await readyGraph(page);
    const foreign = await context.newPage();
    await foreign.goto(basicOrigin);
    await expect(foreign.getByRole("button", { name: "Authenticate" })).toBeVisible();
    const attempted = foreign.waitForRequest(
      (request) =>
        request.method() === "POST" &&
        new URL(request.url()).pathname === "/api/definitions/upload",
    );
    const browserResult = await foreign.evaluate(async (origin) => {
      const form = new FormData();
      form.set("analysis_id", "foreign-origin-upload");
      form.set("definitions_file", new Blob(["x".repeat(1024 * 1024)]), "definitions.json");
      try {
        const response = await fetch(
          `${origin}/api/definitions/upload?__test_probe=oidc-browser-foreign-origin`,
          { method: "POST", credentials: "include", body: form },
        );
        return String(response.status);
      } catch {
        return "CORS blocked";
      }
    }, oidcOrigin);
    // Chromium withholds a CORS-blocked response from the page response API.
    // Inspect the actual request and the outer server's observed status instead.
    const headers = await (await attempted).allHeaders();
    expect(headers.origin).toBe(basicOrigin);
    expect(headers.cookie).toContain(sessionCookieName);
    expect(browserResult).toBe("CORS blocked");
    expect(
      (await harnessState(request, oidcOrigin)).probes["oidc-browser-foreign-origin"],
    ).toMatchObject({ bodyReads: 0, status: 403 });
    await signOut(page);
  } finally {
    await context.close();
  }
});

test("isolates same-subject browser sessions and cleans only the signing-out owner", async ({
  browser,
  request,
}) => {
  const owner = await browserContext(browser, oidcOrigin);
  const other = await browserContext(browser, oidcOrigin);
  try {
    const page = await owner.newPage();
    const otherPage = await other.newPage();
    await loginOidc(page, "analyst");
    await loginOidc(otherPage, "analyst");
    await readyGraph(page);
    await readyGraph(otherPage);
    const analysis = await upload(page, "oidc-owner-a");
    const scc = await runScc(page);
    const cycles = await runCycle(page);
    await upload(otherPage, "oidc-owner-b");
    const otherRun = await runScc(otherPage);
    const query = `?snapshot_revision=${encodeURIComponent(analysis.snapshotRevision)}`;
    const missing = await browserGet(
      otherPage,
      "/api/topology?source=session&analysis_id=unknown-private",
    );
    expect(missing.status).toBe(404);
    for (const path of [
      "/api/topology?source=session&analysis_id=oidc-owner-a",
      `/api/scc-runs/${scc.runId}${query}`,
      `/api/cycle-runs/${cycles.runId}${query}`,
      `/api/cycle-runs/${cycles.runId}/cycles${query}`,
      `/api/cycle-runs/${cycles.runId}/cycles/0${query}`,
    ])
      expect(await browserGet(otherPage, path)).toEqual(missing);
    const catalogue = await browserGet(otherPage, "/api/analyses");
    expect(JSON.stringify(catalogue.body)).not.toContain("oidc-owner-a");
    const mutationHeaders = { Origin: oidcOrigin, "X-CSRF-Token": await csrf(other) };
    for (const path of [
      "/api/analyses/oidc-owner-a",
      `/api/scc-runs/${scc.runId}${query}`,
      `/api/cycle-runs/${cycles.runId}${query}`,
    ]) {
      const rejected = await other.request.delete(`${oidcOrigin}${path}`, {
        headers: mutationHeaders,
      });
      expect(rejected.status()).toBe(404);
      expect(await rejected.json()).toEqual(missing.body);
    }
    const before = await harnessState(request, oidcOrigin);
    const providerBefore = await providerState(request);
    const oldCookie = (await owner.cookies(oidcOrigin)).find(
      (cookie) => cookie.name === sessionCookieName,
    )!;
    // Revocation failure must not restore local access or prevent local cleanup.
    await providerControl(request, { revocationDenied: true });
    await signOut(page);
    const after = await harnessState(request, oidcOrigin);
    expect(after.uploads).toBe(before.uploads - 1);
    expect(after.runs).toBe(before.runs - 2);
    expect(after.resultFiles).toBe(before.resultFiles - 2);
    expect(after.authSessions).toBe(before.authSessions - 1);
    expect((await providerState(request)).revocations).toBe(providerBefore.revocations + 2);
    expect(
      (await owner.cookies(oidcOrigin)).find((cookie) => cookie.name === sessionCookieName),
    ).toBeUndefined();
    expect(
      (
        await owner.request.get(`${oidcOrigin}/api/scc-runs/${scc.runId}${query}`, {
          headers: { Cookie: `${oldCookie.name}=${oldCookie.value}` },
        })
      ).status(),
    ).toBe(401);
    expect(
      (await browserGet(otherPage, "/api/topology?source=session&analysis_id=oidc-owner-b")).status,
    ).toBe(200);
    expect((await browserGet(otherPage, `/api/scc-runs/${otherRun.runId}${query}`)).status).toBe(
      200,
    );
    expect(await page.evaluate(() => ({ ...sessionStorage }))).toEqual({});
    await expect(page.getByText(/provider may keep a separate sign-in session/i)).toBeVisible();
    await signOut(otherPage);
  } finally {
    await owner.close();
    await other.close();
  }
});

test("expires a real cookie session on cold reload and gives a later login a new owner", async ({
  browser,
  request,
}) => {
  const context = await browserContext(browser, oidcOrigin);
  try {
    const page = await context.newPage();
    await loginOidc(page, "analyst");
    await readyGraph(page);
    await upload(page, "oidc-expired-owned");
    const run = await runScc(page);
    const before = await harnessState(request, oidcOrigin);
    const bootstrapStatuses: number[] = [];
    page.on("response", (response) => {
      if (apiResponse(response, "GET", "/api/auth/session"))
        bootstrapStatuses.push(response.status());
    });
    await advanceOidcClock(request, 301);
    expect(
      (await context.cookies(oidcOrigin)).some((cookie) => cookie.name === sessionCookieName),
    ).toBe(true);
    await page.reload();
    await expect(page.getByRole("link", { name: "Sign in" })).toBeVisible();
    expect(bootstrapStatuses).toEqual([401, 200]);
    expect(
      (await context.cookies(oidcOrigin)).some((cookie) => cookie.name === sessionCookieName),
    ).toBe(false);
    const afterExpiry = await harnessState(request, oidcOrigin);
    expect(afterExpiry.uploads).toBe(before.uploads);
    expect(afterExpiry.runs).toBe(before.runs);
    await loginOidc(page, "analyst");
    await readyGraph(page);
    expect(
      (await browserGet(page, "/api/topology?source=session&analysis_id=oidc-expired-owned"))
        .status,
    ).toBe(404);
    expect(
      (
        await browserGet(
          page,
          `/api/scc-runs/${run.runId}?snapshot_revision=${encodeURIComponent(run.snapshotRevision)}`,
        )
      ).status,
    ).toBe(404);
    await signOut(page);
  } finally {
    await context.close();
  }
});

test("clears the running browser workspace when server-side refresh is denied", async ({
  browser,
  request,
}) => {
  const context = await browserContext(browser, oidcOrigin);
  try {
    const page = await context.newPage();
    await loginOidc(page, "analyst");
    await readyGraph(page);
    await upload(page, "oidc-refresh-denied");
    await advanceOidcClock(request, 120);
    await providerControl(request, { refreshDenied: true });
    await context.setExtraHTTPHeaders({ "X-Test-Probe": "oidc-refresh-rejected" });
    const denied = page.waitForResponse((response) =>
      apiResponse(response, "POST", "/api/scc-runs"),
    );
    await page.getByRole("button", { name: "Run cyclic components" }).click();
    expect((await denied).status()).toBe(401);
    await expect(page.getByRole("link", { name: "Sign in" })).toBeVisible();
    await expect(page.locator(".topology-canvas")).toHaveCount(0);
    expect(await page.evaluate(() => ({ ...sessionStorage }))).toEqual({});
    expect((await harnessState(request, oidcOrigin)).probes["oidc-refresh-rejected"]).toMatchObject(
      { bodyReads: 0, status: 401 },
    );
  } finally {
    await context.close();
  }
});

for (const fault of ["state", "nonce", "pkce", "issuer", "audience", "algorithm", "time"]) {
  test(`rejects a real browser callback with invalid ${fault} without exposing provider material`, async ({
    browser,
    request,
  }) => {
    await providerControl(request, { fault });
    const before = await providerState(request);
    const context = await browserContext(browser, oidcOrigin);
    try {
      const page = await context.newPage();
      await observeFetchHeaders(page);
      const assertBoundary = observeOidcBoundary(page);
      const callback = await loginOidc(page, "analyst");
      expect(callback.status()).toBe(401);
      expect(await callback.json()).toEqual({
        error: {
          code: "authentication_failed",
          message: "Authentication could not be completed.",
          issues: [],
        },
      });
      expect(
        (await context.cookies(oidcOrigin)).some((cookie) => cookie.name === sessionCookieName),
      ).toBe(false);
      const after = await providerState(request);
      expect(after.exchange - before.exchange).toBe(fault === "state" ? 0 : 1);
      if (fault !== "state")
        expect(
          (await context.cookies(oidcOrigin)).some((cookie) => cookie.name.endsWith("_login")),
        ).toBe(false);
      await page.goto(oidcOrigin);
      await expect(page.getByRole("link", { name: "Sign in" })).toBeVisible();
      await assertBoundary();
    } finally {
      await context.close();
    }
  });
}

test("rejects callback replay and confines return redirects", async ({ browser, request }) => {
  const context = await browserContext(browser, oidcOrigin);
  try {
    const page = await context.newPage();
    const callback = await loginOidc(page, "analyst");
    await readyGraph(page);
    const before = await providerState(request);
    expect((await page.goto(callback.url()))?.status()).toBe(401);
    expect((await providerState(request)).exchange).toBe(before.exchange);
    const forbidden = await context.request.get(
      `${oidcOrigin}/api/auth/oidc/login?return_to=https://outside.invalid/`,
      { maxRedirects: 0 },
    );
    expect(forbidden.status()).toBe(400);
    await page.goto(oidcOrigin);
    await readyGraph(page);
    await signOut(page);
  } finally {
    await context.close();
  }
});

test("rejects a stolen callback in another actual browser before code exchange", async ({
  browser,
  request,
}) => {
  const initiator = await browserContext(browser, oidcOrigin);
  const victim = await browserContext(browser, oidcOrigin);
  try {
    const page = await initiator.newPage();
    await page.goto(oidcOrigin);
    await page.getByRole("link", { name: "Sign in" }).click();
    await expect(page.getByRole("heading", { name: "Local OIDC provider" })).toBeVisible();
    const fields = (await page
      .locator("form")
      .evaluate((form) => Object.fromEntries(new FormData(form as HTMLFormElement)))) as Record<
      string,
      string
    >;
    const authorization = await initiator.request.post(`${providerOrigin}/authorize`, {
      form: { ...fields, profile: "analyst" },
      maxRedirects: 0,
    });
    expect(authorization.status()).toBe(303);
    const callbackUrl = authorization.headers().location;
    const before = await providerState(request);
    expect((await (await victim.newPage()).goto(callbackUrl))?.status()).toBe(401);
    expect((await page.goto(callbackUrl))?.status()).toBe(401);
    expect((await providerState(request)).exchange).toBe(before.exchange);
    expect(
      (await victim.cookies(oidcOrigin)).some((cookie) => cookie.name === sessionCookieName),
    ).toBe(false);
    expect(
      (await initiator.cookies(oidcOrigin)).some((cookie) => cookie.name === sessionCookieName),
    ).toBe(false);
  } finally {
    await initiator.close();
    await victim.close();
  }
});
