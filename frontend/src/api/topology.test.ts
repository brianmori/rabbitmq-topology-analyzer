import { describe, expect, it, vi } from "vitest";
import {
  ApiError,
  cancelCycleRun,
  cancelSccRun,
  clearAuthenticationState,
  deleteAnalysis,
  fetchAccess,
  fetchAnalyses,
  fetchAuthSession,
  fetchCycleDetail,
  fetchCyclePage,
  fetchCycleRun,
  fetchSccRun,
  fetchTopology,
  logout,
  startSccRun,
  startCycleRun,
  subscribeAuthenticationFailure,
  uploadCapture,
} from "./topology";
import {
  accessFixture,
  basicAuthSessionFixture,
  catalogueFixture,
  cycleDetailFixture,
  cyclePageFixture,
  installApiFixtures,
  noneAuthSessionFixture,
  oidcAuthSessionFixture,
  runningSccRunFixture,
  runningCycleRunFixture,
  topologyFixture,
} from "../test/api-fixtures";
import { SESSION_STORAGE_KEY, TEST_SESSION_TOKEN } from "../test/browser";

type FetchFixtureMock = ReturnType<typeof installApiFixtures>;

function requestOptions(fetchMock: FetchFixtureMock, index: number = 0): RequestInit {
  const options = fetchMock.mock.calls[index]?.[1];
  expect(options?.credentials).toBe("same-origin");
  return options ?? {};
}

function requestHeaders(fetchMock: FetchFixtureMock, index: number = 0): Headers {
  return new Headers(requestOptions(fetchMock, index).headers);
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((complete) => {
    resolve = complete;
  });
  return { promise, resolve };
}

const authenticationFailure = () =>
  Response.json(
    { error: { code: "authentication_required", message: "Authentication required." } },
    { status: 401, headers: { "WWW-Authenticate": 'Basic realm="Topology", charset="UTF-8"' } },
  );

describe("authentication bootstrap and request policy", () => {
  it("bootstraps safe session metadata before protected access without exposing CSRF to React", async () => {
    const fetchMock = installApiFixtures({
      "/api/auth/session": { body: oidcAuthSessionFixture },
      "/api/access": { body: { ...accessFixture, authMode: "oidc" } },
    });

    const session = await fetchAuthSession();
    await fetchAccess();

    expect(session).toEqual({
      authMode: "oidc",
      authenticated: true,
      loginPath: null,
      logoutPath: "/api/auth/logout",
    });
    expect(session).not.toHaveProperty("csrfToken");
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual(["/api/auth/session", "/api/access"]);
    expect(requestHeaders(fetchMock, 0).has("X-CSRF-Token")).toBe(false);
    expect(requestHeaders(fetchMock, 1).has("X-CSRF-Token")).toBe(false);
  });

  it("rejects an authentication bootstrap path outside the fixed same-origin contract", async () => {
    installApiFixtures({
      "/api/auth/session": {
        body: { ...oidcAuthSessionFixture, loginPath: "https://identity.invalid/login" },
      },
    });

    await expect(fetchAuthSession()).rejects.toMatchObject({
      status: 502,
      code: "invalid_auth_session",
    });
  });

  it("rejects unexpected authentication fields instead of accepting provider material", async () => {
    installApiFixtures({
      "/api/auth/session": {
        body: { ...oidcAuthSessionFixture, accessToken: "must-never-cross-the-bff" },
      },
    });

    await expect(fetchAuthSession()).rejects.toMatchObject({
      status: 502,
      code: "invalid_auth_session",
    });
  });

  it("returns a rejected Basic bootstrap to the native-challenge shell", async () => {
    installApiFixtures({
      "/api/auth/session": {
        body: { error: { code: "authentication_required", message: "Authentication required." } },
        status: 401,
        headers: { "WWW-Authenticate": 'Basic realm="Topology", charset="UTF-8"' },
      },
    });
    const listener = vi.fn();
    subscribeAuthenticationFailure(listener);

    await expect(fetchAuthSession()).rejects.toMatchObject({ status: 401 });

    expect(listener).toHaveBeenCalledWith({
      authMode: "basic",
      authenticated: false,
      loginPath: null,
      logoutPath: null,
    });
  });

  it("uses only the OIDC session-bound CSRF value on unsafe cookie-authenticated requests", async () => {
    const analysis = {
      ...catalogueFixture.static[0],
      source: "session" as const,
      analysisId: "uploaded",
      expiresAt: "2026-08-11T12:00:00Z",
    };
    const fetchMock = installApiFixtures({
      "/api/auth/session": { body: oidcAuthSessionFixture },
      "/api/analyses": { body: catalogueFixture },
      "/api/definitions/upload": { body: { analysis, replaced: false }, status: 201 },
      "/api/auth/logout": { body: null, status: 204 },
    });
    await fetchAuthSession();

    await fetchAnalyses();
    await uploadCapture({
      analysisId: "uploaded",
      definitionsFile: new File(["definitions"], "definitions.json"),
      inClusterAmqpHosts: [],
    });
    await logout();

    expect(requestHeaders(fetchMock, 1).has("X-Session-Token")).toBe(false);
    expect(requestHeaders(fetchMock, 1).has("X-CSRF-Token")).toBe(false);
    expect(requestHeaders(fetchMock, 2).get("X-CSRF-Token")).toBe(oidcAuthSessionFixture.csrfToken);
    expect(requestHeaders(fetchMock, 2).has("X-Session-Token")).toBe(false);
    expect(requestHeaders(fetchMock, 3).get("X-CSRF-Token")).toBe(oidcAuthSessionFixture.csrfToken);
    expect(requestHeaders(fetchMock, 3).has("X-Session-Token")).toBe(false);
    expect(sessionStorage.getItem(SESSION_STORAGE_KEY)).toBeNull();
  });

  it("keeps Basic native to the browser while using and clearing its logical session owner", async () => {
    const fetchMock = installApiFixtures({
      "/api/auth/session": { body: basicAuthSessionFixture },
      "/api/analyses": { body: catalogueFixture },
      "/api/auth/logout": { body: null, status: 204 },
    });
    await fetchAuthSession();

    await fetchAnalyses();
    await logout();

    expect(requestHeaders(fetchMock, 1).get("X-Session-Token")).toBe(TEST_SESSION_TOKEN);
    expect(requestHeaders(fetchMock, 2).get("X-Session-Token")).toBe(TEST_SESSION_TOKEN);
    expect(requestHeaders(fetchMock, 2).has("Authorization")).toBe(false);
    expect(requestHeaders(fetchMock, 2).has("X-CSRF-Token")).toBe(false);
    expect(sessionStorage.getItem(SESSION_STORAGE_KEY)).toBeNull();
  });

  it("notifies the application shell when a protected request returns 401", async () => {
    installApiFixtures({
      "/api/auth/session": { body: noneAuthSessionFixture },
      "/api/access": {
        body: { error: { code: "authentication_required", message: "Authentication required." } },
        status: 401,
      },
    });
    const listener = vi.fn();
    const unsubscribe = subscribeAuthenticationFailure(listener);
    await fetchAuthSession();

    await expect(fetchAccess()).rejects.toMatchObject({ status: 401 });

    expect(listener).toHaveBeenCalledOnce();
    unsubscribe();
  });

  it.each(["older first", "newer first"])(
    "lets only the newest overlapping bootstrap commit (%s)",
    async (order) => {
      const older = deferred<Response>();
      const newer = deferred<Response>();
      const fetchMock = vi
        .fn<typeof fetch>()
        .mockReturnValueOnce(older.promise)
        .mockReturnValueOnce(newer.promise)
        .mockResolvedValue(new Response(null, { status: 204 }));
      vi.stubGlobal("fetch", fetchMock);
      const oldBootstrap = fetchAuthSession();
      const oldResult = expect(oldBootstrap).rejects.toMatchObject({
        name: "AuthenticationSupersededError",
      });
      const newBootstrap = fetchAuthSession();
      const unverifiedBasic = {
        ...basicAuthSessionFixture,
        authenticated: false,
        logoutPath: null,
      };

      if (order === "older first") {
        older.resolve(Response.json(unverifiedBasic));
        await oldResult;
      }
      newer.resolve(Response.json(basicAuthSessionFixture));
      await expect(newBootstrap).resolves.toMatchObject({ authenticated: true });
      if (order === "newer first") older.resolve(Response.json(unverifiedBasic));
      await oldResult;

      await expect(logout()).resolves.toMatchObject({ authMode: "basic", authenticated: false });
      expect(fetchMock.mock.calls[2]?.[0]).toBe("/api/auth/logout");
    },
  );

  it("ignores a stale Basic bootstrap challenge after a newer verified session", async () => {
    const older = deferred<Response>();
    const listener = vi.fn();
    subscribeAuthenticationFailure(listener);
    vi.stubGlobal(
      "fetch",
      vi
        .fn<typeof fetch>()
        .mockReturnValueOnce(older.promise)
        .mockResolvedValueOnce(Response.json(basicAuthSessionFixture))
        .mockResolvedValueOnce(new Response(null, { status: 204 })),
    );
    const oldBootstrap = fetchAuthSession();
    const oldResult = expect(oldBootstrap).rejects.toMatchObject({
      name: "AuthenticationSupersededError",
    });
    await fetchAuthSession();

    older.resolve(authenticationFailure());
    await oldResult;

    expect(listener).not.toHaveBeenCalled();
    await expect(logout()).resolves.toMatchObject({ authenticated: false });
  });

  it("guards delayed bootstrap JSON parsing as well as the fetch response", async () => {
    const olderBody = deferred<unknown>();
    const response = Response.json(null);
    vi.spyOn(response, "json").mockReturnValue(olderBody.promise);
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(response)
      .mockResolvedValueOnce(Response.json({ ...oidcAuthSessionFixture, csrfToken: "new-csrf" }))
      .mockResolvedValueOnce(Response.json(runningSccRunFixture));
    vi.stubGlobal("fetch", fetchMock);
    const oldBootstrap = fetchAuthSession();
    const oldResult = expect(oldBootstrap).rejects.toMatchObject({
      name: "AuthenticationSupersededError",
    });
    await vi.waitFor(() => expect(response.json).toHaveBeenCalledOnce());
    await fetchAuthSession();

    olderBody.resolve(oidcAuthSessionFixture);
    await oldResult;
    await startSccRun(
      "static",
      "orders-production",
      topologyFixture.snapshotRevision,
      TEST_SESSION_TOKEN,
    );

    expect(requestHeaders(fetchMock, 2).get("X-CSRF-Token")).toBe("new-csrf");
  });

  it("does not let an old protected 401 invalidate a newer OIDC session", async () => {
    const older = deferred<Response>();
    const listener = vi.fn();
    subscribeAuthenticationFailure(listener);
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(Response.json(oidcAuthSessionFixture))
      .mockReturnValueOnce(older.promise)
      .mockResolvedValueOnce(Response.json({ ...oidcAuthSessionFixture, csrfToken: "new-csrf" }))
      .mockResolvedValueOnce(Response.json(runningSccRunFixture));
    vi.stubGlobal("fetch", fetchMock);
    await fetchAuthSession();
    const oldRequest = fetchAnalyses();
    const oldResult = expect(oldRequest).rejects.toMatchObject({ status: 401 });
    await fetchAuthSession();

    older.resolve(authenticationFailure());
    await oldResult;
    await startSccRun(
      "static",
      "orders-production",
      topologyFixture.snapshotRevision,
      TEST_SESSION_TOKEN,
    );

    expect(listener).not.toHaveBeenCalled();
    expect(requestHeaders(fetchMock, 3).get("X-CSRF-Token")).toBe("new-csrf");
    expect(requestHeaders(fetchMock, 3).has("X-Session-Token")).toBe(false);
  });

  it.each(["logout", "401", "reset"])(
    "does not revive authentication from a pending bootstrap after %s",
    async (boundary) => {
      const older = deferred<Response>();
      const fetchMock = vi
        .fn<typeof fetch>()
        .mockResolvedValueOnce(Response.json(basicAuthSessionFixture))
        .mockReturnValueOnce(older.promise)
        .mockResolvedValueOnce(
          boundary === "401" ? authenticationFailure() : new Response(null, { status: 204 }),
        );
      vi.stubGlobal("fetch", fetchMock);
      await fetchAuthSession();
      const oldBootstrap = fetchAuthSession();
      const oldResult = expect(oldBootstrap).rejects.toMatchObject({
        name: "AuthenticationSupersededError",
      });

      if (boundary === "logout") await logout();
      else if (boundary === "401")
        await expect(fetchAccess()).rejects.toMatchObject({ status: 401 });
      else clearAuthenticationState();
      older.resolve(Response.json(basicAuthSessionFixture));
      await oldResult;

      await expect(logout()).rejects.toMatchObject({ code: "logout_unavailable" });
    },
  );

  it.each([204, 401, 503])(
    "does not clear a newer Basic session or owner when an older logout returns %s",
    async (status) => {
      const olderLogout = deferred<Response>();
      const listener = vi.fn();
      subscribeAuthenticationFailure(listener);
      const fetchMock = vi
        .fn<typeof fetch>()
        .mockResolvedValueOnce(Response.json(basicAuthSessionFixture))
        .mockReturnValueOnce(olderLogout.promise)
        .mockResolvedValueOnce(Response.json(basicAuthSessionFixture))
        .mockResolvedValueOnce(new Response(null, { status: 204 }));
      vi.stubGlobal("fetch", fetchMock);
      await fetchAuthSession();
      const oldLogout = logout();
      const oldResult = expect(oldLogout).rejects.toMatchObject({
        name: "AuthenticationSupersededError",
      });
      await fetchAuthSession();
      sessionStorage.setItem(SESSION_STORAGE_KEY, "new-owner");

      olderLogout.resolve(
        status === 204
          ? new Response(null, { status })
          : Response.json(
              { error: { code: "logout_failed", message: "Old logout failed." } },
              { status },
            ),
      );
      await oldResult;

      expect(sessionStorage.getItem(SESSION_STORAGE_KEY)).toBe("new-owner");
      expect(listener).not.toHaveBeenCalled();
      await expect(logout()).resolves.toMatchObject({ authenticated: false });
    },
  );

  it("still notifies authentication loss for a current logout 401", async () => {
    const listener = vi.fn();
    subscribeAuthenticationFailure(listener);
    vi.stubGlobal(
      "fetch",
      vi
        .fn<typeof fetch>()
        .mockResolvedValueOnce(Response.json(basicAuthSessionFixture))
        .mockResolvedValueOnce(authenticationFailure()),
    );
    await fetchAuthSession();

    await expect(logout()).rejects.toMatchObject({ status: 401 });

    expect(listener).toHaveBeenCalledOnce();
    expect(listener).toHaveBeenCalledWith({
      authMode: "basic",
      authenticated: false,
      loginPath: null,
      logoutPath: null,
    });
    await expect(logout()).rejects.toMatchObject({ code: "logout_unavailable" });
  });
});

describe("fetchAnalyses", () => {
  it("creates one deterministic session token and sends it with the request", async () => {
    const fetchMock = installApiFixtures({
      "/api/analyses": { body: catalogueFixture },
    });

    await expect(fetchAnalyses()).resolves.toEqual(catalogueFixture);
    expect(sessionStorage.getItem(SESSION_STORAGE_KEY)).toBe(TEST_SESSION_TOKEN);
    expect(requestHeaders(fetchMock).get("X-Session-Token")).toBe(TEST_SESSION_TOKEN);
  });
});

describe("fetchTopology", () => {
  it("uses the approved query names and browser-session header", async () => {
    const url = "/api/topology?source=static&analysis_id=orders-production";
    const fetchMock = installApiFixtures({ [url]: { body: topologyFixture } });

    await expect(fetchTopology("static", "orders-production")).resolves.toEqual(topologyFixture);
    expect(fetchMock.mock.calls[0]?.[0]).toBe(url);
    expect(requestHeaders(fetchMock).get("X-Session-Token")).toBe(TEST_SESSION_TOKEN);
  });

  it("preserves safe status metadata for catalogue reconciliation", async () => {
    const url = "/api/topology?source=session&analysis_id=expired";
    installApiFixtures({
      [url]: {
        body: {
          error: {
            code: "not_found",
            message: "The requested resource was not found.",
            issues: [],
          },
        },
        status: 404,
      },
    });

    const error = await fetchTopology("session", "expired").catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 404, code: "not_found", issues: [] });
  });
});

describe("cyclic-component run API", () => {
  it("centralizes idempotent start, polling, and hard cancellation requests", async () => {
    const runUrl = `/api/scc-runs/${runningSccRunFixture.runId}?snapshot_revision=${encodeURIComponent(topologyFixture.snapshotRevision)}`;
    const fetchMock = installApiFixtures({
      "/api/scc-runs": { body: runningSccRunFixture, status: 202 },
      [runUrl]: { body: runningSccRunFixture },
    });
    const idempotencyKey = "20000000-0000-4000-8000-000000000002";

    await startSccRun(
      topologyFixture.source,
      topologyFixture.analysisId,
      topologyFixture.snapshotRevision,
      idempotencyKey,
    );
    await fetchSccRun(runningSccRunFixture.runId, topologyFixture.snapshotRevision);
    await cancelSccRun(runningSccRunFixture.runId, topologyFixture.snapshotRevision);

    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/scc-runs");
    expect(requestOptions(fetchMock, 0)).toMatchObject({
      method: "POST",
      body: JSON.stringify({
        source: topologyFixture.source,
        analysisId: topologyFixture.analysisId,
        snapshotRevision: topologyFixture.snapshotRevision,
      }),
    });
    expect(requestHeaders(fetchMock, 0).get("Content-Type")).toBe("application/json");
    expect(requestHeaders(fetchMock, 0).get("X-Session-Token")).toBe(TEST_SESSION_TOKEN);
    expect(requestHeaders(fetchMock, 0).get("Idempotency-Key")).toBe(idempotencyKey);
    expect(fetchMock.mock.calls[1]?.[0]).toBe(runUrl);
    expect(requestHeaders(fetchMock, 1).get("X-Session-Token")).toBe(TEST_SESSION_TOKEN);
    expect(fetchMock.mock.calls[2]?.[0]).toBe(runUrl);
    expect(requestOptions(fetchMock, 2).method).toBe("DELETE");
    expect(requestHeaders(fetchMock, 2).get("X-Session-Token")).toBe(TEST_SESSION_TOKEN);
  });
});

describe("structural-cycle run API", () => {
  it("centralizes run lifecycle, bounded paging, and original-index detail requests", async () => {
    const runUrl = `/api/cycle-runs/${runningCycleRunFixture.runId}?snapshot_revision=${encodeURIComponent(topologyFixture.snapshotRevision)}`;
    const pageUrl =
      `/api/cycle-runs/${runningCycleRunFixture.runId}/cycles?` +
      `snapshot_revision=${encodeURIComponent(topologyFixture.snapshotRevision)}` +
      "&offset=50&limit=50&sort=length_desc&min_length=2&max_length=4" +
      "&vhost=%2Forders&vhost=%2Faudit";
    const detailUrl =
      `/api/cycle-runs/${runningCycleRunFixture.runId}/cycles/1?` +
      `snapshot_revision=${encodeURIComponent(topologyFixture.snapshotRevision)}`;
    const fetchMock = installApiFixtures({
      "/api/cycle-runs": { body: runningCycleRunFixture, status: 202 },
      [runUrl]: { body: runningCycleRunFixture },
      [pageUrl]: { body: cyclePageFixture },
      [detailUrl]: { body: cycleDetailFixture },
    });
    const idempotencyKey = "40000000-0000-4000-8000-000000000004";

    await startCycleRun(
      topologyFixture.source,
      topologyFixture.analysisId,
      topologyFixture.snapshotRevision,
      0,
      idempotencyKey,
    );
    await fetchCycleRun(runningCycleRunFixture.runId, topologyFixture.snapshotRevision);
    await cancelCycleRun(runningCycleRunFixture.runId, topologyFixture.snapshotRevision);
    await fetchCyclePage(runningCycleRunFixture.runId, topologyFixture.snapshotRevision, {
      offset: 50,
      limit: 50,
      sort: "length_desc",
      vhosts: ["/orders", "/audit"],
      minLength: 2,
      maxLength: 4,
    });
    await fetchCycleDetail(runningCycleRunFixture.runId, 1, topologyFixture.snapshotRevision);

    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/cycle-runs");
    expect(requestOptions(fetchMock, 0)).toMatchObject({
      method: "POST",
      body: JSON.stringify({
        source: topologyFixture.source,
        analysisId: topologyFixture.analysisId,
        snapshotRevision: topologyFixture.snapshotRevision,
        maxCycles: 0,
      }),
    });
    expect(requestHeaders(fetchMock, 0).get("Content-Type")).toBe("application/json");
    expect(requestHeaders(fetchMock, 0).get("X-Session-Token")).toBe(TEST_SESSION_TOKEN);
    expect(requestHeaders(fetchMock, 0).get("Idempotency-Key")).toBe(idempotencyKey);
    expect(fetchMock.mock.calls[1]?.[0]).toBe(runUrl);
    expect(requestHeaders(fetchMock, 1).get("X-Session-Token")).toBe(TEST_SESSION_TOKEN);
    expect(fetchMock.mock.calls[2]?.[0]).toBe(runUrl);
    expect(requestOptions(fetchMock, 2).method).toBe("DELETE");
    expect(requestHeaders(fetchMock, 2).get("X-Session-Token")).toBe(TEST_SESSION_TOKEN);
    expect(fetchMock.mock.calls[3]?.[0]).toBe(pageUrl);
    expect(requestHeaders(fetchMock, 3).get("X-Session-Token")).toBe(TEST_SESSION_TOKEN);
    expect(fetchMock.mock.calls[4]?.[0]).toBe(detailUrl);
    expect(requestHeaders(fetchMock, 4).get("X-Session-Token")).toBe(TEST_SESSION_TOKEN);
  });
});

describe("session capture API", () => {
  it("sends every approved multipart field and repeatable host value", async () => {
    const analysis = {
      ...catalogueFixture.static[0],
      source: "session" as const,
      analysisId: "uploaded",
      label: "Uploaded",
      expiresAt: "2026-08-11T12:00:00Z",
    };
    const fetchMock = installApiFixtures({
      "/api/definitions/upload": { body: { analysis, replaced: false }, status: 201 },
      "/api/analyses/uploaded": { body: null, status: 204 },
    });
    const definitionsFile = new File(["definitions"], "definitions.json");
    const queuesFile = new File(["queues"], "queues.json");
    const exchangesFile = new File(["exchanges"], "exchanges.json");

    await uploadCapture({
      analysisId: "uploaded",
      label: "Uploaded",
      definitionsFile,
      queuesFile,
      exchangesFile,
      inClusterAmqpHosts: ["rabbit-a", "rabbit-b"],
    });
    await deleteAnalysis("uploaded");

    const uploadOptions = fetchMock.mock.calls[0]?.[1];
    expect(requestHeaders(fetchMock).get("X-Session-Token")).toBe(TEST_SESSION_TOKEN);
    expect(uploadOptions?.method).toBe("POST");
    expect(uploadOptions?.body).toBeInstanceOf(FormData);
    const form = uploadOptions?.body as FormData;
    expect(form.get("analysis_id")).toBe("uploaded");
    expect(form.get("label")).toBe("Uploaded");
    expect(form.get("definitions_file")).toBe(definitionsFile);
    expect(form.get("queues_file")).toBe(queuesFile);
    expect(form.get("exchanges_file")).toBe(exchangesFile);
    expect(form.getAll("in_cluster_amqp_host")).toEqual(["rabbit-a", "rabbit-b"]);
    expect(fetchMock.mock.calls[1]?.[0]).toBe("/api/analyses/uploaded");
    expect(requestOptions(fetchMock, 1).method).toBe("DELETE");
    expect(requestHeaders(fetchMock, 1).get("X-Session-Token")).toBe(TEST_SESSION_TOKEN);
  });

  it.each(["SCC", "cycle"])(
    "retains %s polling capacity metadata without an authentication transition",
    async (operation) => {
      const onAuthenticationFailure = vi.fn();
      subscribeAuthenticationFailure(onAuthenticationFailure);
      const fetchMock = vi
        .fn<typeof fetch>()
        .mockResolvedValueOnce(
          new Response(
            JSON.stringify({
              error: {
                code: "authentication_capacity_unavailable",
                message: "Busy.",
                issues: [],
              },
            }),
            { status: 503, headers: { "Retry-After": "2" } },
          ),
        )
        .mockRejectedValueOnce(new TypeError("Network unavailable"));
      vi.stubGlobal("fetch", fetchMock);
      const poll = () =>
        operation === "SCC"
          ? fetchSccRun(runningSccRunFixture.runId, topologyFixture.snapshotRevision)
          : fetchCycleRun(runningCycleRunFixture.runId, topologyFixture.snapshotRevision);
      await expect(poll()).rejects.toMatchObject({
        status: 503,
        code: "authentication_capacity_unavailable",
        retryAfter: "2",
      });
      await expect(poll()).rejects.toBeInstanceOf(TypeError);
      expect(onAuthenticationFailure).not.toHaveBeenCalled();
    },
  );

  it("retains safe status, code, and retry metadata for actionable UI errors", async () => {
    installApiFixtures({
      "/api/definitions/upload": {
        body: {
          error: {
            code: "analysis_capacity_unavailable",
            message: "Analysis capacity is temporarily unavailable.",
            issues: [],
          },
        },
        status: 503,
        headers: { "Retry-After": "2" },
      },
    });

    const failure = uploadCapture({
      analysisId: "uploaded",
      definitionsFile: new File(["definitions"], "definitions.json"),
      inClusterAmqpHosts: [],
    });

    await expect(failure).rejects.toMatchObject({
      status: 503,
      code: "analysis_capacity_unavailable",
      retryAfter: "2",
    });
  });
});
