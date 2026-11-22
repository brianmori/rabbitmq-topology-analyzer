import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import { describe, expect, it, vi } from "vitest";
import * as topologyApi from "./api/topology";
import type { GraphSelection } from "./components/DetailsPanel";
import type { VisibleTopology } from "./graph/presentation";
import App from "./App";
import { ACTIVE_CYCLE_RUN_STORAGE_KEY } from "./operations/cycles";
import { ACTIVE_SCC_RUN_STORAGE_KEY } from "./operations/scc";
import {
  accessFixture,
  type ApiFixture,
  basicAuthSessionFixture,
  catalogueFixture,
  cycleDetailFixture,
  cyclePageFixture,
  installApiFixtures,
  noneAuthSessionFixture,
  oidcAuthSessionFixture,
  runningSccRunFixture,
  runningCycleRunFixture,
  succeededSccRunFixture,
  succeededCycleRunFixture,
  topologyFixture,
} from "./test/api-fixtures";
import type {
  AccessResponse,
  AnalysisCatalogueResponse,
  AnalysisSummary,
  AuthMode,
  AuthSessionResponse,
  TopologyResponse,
} from "./types/topology";

vi.mock("./graph/TopologyCanvas", () => ({
  TopologyCanvas: ({
    topology,
    onSelectionChange,
  }: {
    topology: VisibleTopology;
    onSelectionChange: (selection: GraphSelection) => void;
  }) => (
    <div aria-label="RabbitMQ topology graph">
      <span>
        Rendered resources:{" "}
        {topology.nodes.map((node) => node.data.name || "(default exchange)").join(", ")}
      </span>
      <span>Rendered edges: {topology.edges.length}</span>
      {topology.nodes[0] && (
        <button
          type="button"
          onClick={() => onSelectionChange({ kind: "node", id: topology.nodes[0]!.id })}
        >
          Select first resource
        </button>
      )}
    </div>
  ),
}));

const topologyUrl = "/api/topology?source=static&analysis_id=orders-production";

const authSessionByMode = {
  none: noneAuthSessionFixture,
  oidc: oidcAuthSessionFixture,
  basic: basicAuthSessionFixture,
} as const satisfies Record<AuthMode, AuthSessionResponse>;

const roleProfiles = [
  {
    name: "no role",
    roles: [],
    capabilities: {
      viewTopology: false,
      runGraphOperations: false,
      manageUploads: false,
    },
  },
  {
    name: "viewer",
    roles: ["viewer"],
    capabilities: {
      viewTopology: true,
      runGraphOperations: true,
      manageUploads: false,
    },
  },
  {
    name: "analyst",
    roles: ["analyst"],
    capabilities: {
      viewTopology: true,
      runGraphOperations: true,
      manageUploads: true,
    },
  },
] as const;

const modeRoleCases = (["none", "oidc", "basic"] as const).flatMap((authMode) =>
  roleProfiles.map((profile) => ({ authMode, profile })),
);

function sessionAnalysis(revision: string = topologyFixture.snapshotRevision): AnalysisSummary {
  return {
    ...catalogueFixture.static[0],
    source: "session",
    analysisId: "session-capture",
    label: "Session capture",
    snapshotRevision: revision,
    expiresAt: "2026-08-11T18:00:00Z",
  };
}

function sessionTopology(revision: string = topologyFixture.snapshotRevision): TopologyResponse {
  return {
    ...topologyFixture,
    source: "session",
    analysisId: "session-capture",
    snapshotRevision: revision,
  };
}

function sessionCycleRun(revision: string = topologyFixture.snapshotRevision) {
  return {
    ...succeededCycleRunFixture,
    source: "session" as const,
    analysisId: "session-capture",
    snapshotRevision: revision,
  };
}

function sessionCyclePage(revision: string = topologyFixture.snapshotRevision) {
  return { ...cyclePageFixture, snapshotRevision: revision };
}

function sessionCycleDetail(revision: string = topologyFixture.snapshotRevision) {
  return { ...cycleDetailFixture, snapshotRevision: revision };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((complete) => {
    resolve = complete;
  });
  return { promise, resolve };
}

describe("App", () => {
  it("shows application load time in UTC and an accessible full revision when switching sources", async () => {
    const user = userEvent.setup();
    const first = catalogueFixture.static[0];
    const second = {
      ...sessionAnalysis(`sha256:${"b".repeat(64)}`),
      label: first.label,
      loadedAt: "2026-08-11T09:30:45.123456Z",
    };
    installApiFixtures({
      "/api/access": { body: accessFixture },
      "/api/analyses": { body: { static: [first], session: [second] } },
      [topologyUrl]: { body: topologyFixture },
      "/api/topology?source=session&analysis_id=session-capture": {
        body: sessionTopology(second.snapshotRevision),
      },
    });
    render(<App />);
    await screen.findByLabelText("RabbitMQ topology graph");

    const provenance = screen.getByRole("group", { name: "Selected snapshot" });
    const picker = screen.getByRole("combobox", { name: "Capture" });
    expect(picker).toHaveAccessibleDescription("Loaded in app: 2026-08-10 08:00:00 UTC");
    expect(within(provenance).getByText("2026-08-10 08:00:00 UTC")).toHaveAttribute(
      "datetime", first.loadedAt,
    );
    expect(within(provenance).getByText("Revision:")).toHaveTextContent("aaaaaaaaaaaa…");
    expect(within(provenance).getByText(first.snapshotRevision)).not.toBeVisible();
    picker.focus();
    await user.tab();
    expect(within(provenance).getByText("Revision:")).toHaveFocus();
    // jsdom lacks native summary keyboard activation; Chromium covers Enter.
    await user.click(within(provenance).getByText("Revision:"));
    expect(within(provenance).getByText(first.snapshotRevision)).toBeVisible();

    await user.selectOptions(picker, "session:session-capture");
    await screen.findByLabelText("RabbitMQ topology graph");
    expect(picker).toHaveAccessibleDescription("Loaded in app: 2026-08-11 09:30:45 UTC");
    expect(within(provenance).getByText("2026-08-11 09:30:45 UTC")).toHaveAttribute(
      "datetime", second.loadedAt,
    );
    expect(within(provenance).getByText(second.snapshotRevision)).toBeVisible();
    expect(within(provenance).queryByText(first.snapshotRevision)).not.toBeInTheDocument();

    await user.selectOptions(picker, "static:orders-production");
    expect(picker).toHaveAccessibleDescription("Loaded in app: 2026-08-10 08:00:00 UTC");
    expect(within(provenance).getByText(first.snapshotRevision)).toBeVisible();
  });

  it.each(modeRoleCases)(
    "isolates $authMode mode with the $profile.name capability view",
    async ({ authMode, profile }) => {
      const access = {
        authMode,
        roles: profile.roles,
        capabilities: profile.capabilities,
      } satisfies AccessResponse;
      const routes: Record<string, ApiFixture> = {
        "/api/auth/session": { body: authSessionByMode[authMode] },
        "/api/access": { body: access },
      };
      if (profile.capabilities.viewTopology) {
        routes["/api/analyses"] = { body: catalogueFixture };
        routes[topologyUrl] = { body: topologyFixture };
      }
      const fetchMock = installApiFixtures(routes);

      render(<App />);

      if (profile.capabilities.viewTopology) {
        expect(await screen.findByLabelText("RabbitMQ topology graph")).toBeVisible();
      } else {
        expect(await screen.findByText("Topology access unavailable")).toBeVisible();
      }
      if (profile.capabilities.manageUploads) {
        expect(screen.getByRole("button", { name: "Upload capture" })).toBeEnabled();
      } else {
        expect(screen.queryByRole("button", { name: "Upload capture" })).not.toBeInTheDocument();
      }
      if (profile.capabilities.runGraphOperations) {
        expect(screen.getByRole("button", { name: "Run cyclic components" })).toBeEnabled();
        expect(screen.getByRole("button", { name: "Run structural cycles" })).toBeEnabled();
      } else {
        expect(
          screen.queryByRole("button", { name: "Run cyclic components" }),
        ).not.toBeInTheDocument();
        expect(
          screen.queryByRole("button", { name: "Run structural cycles" }),
        ).not.toBeInTheDocument();
      }
      if (authMode === "none") {
        expect(screen.queryByRole("button", { name: "Sign out" })).not.toBeInTheDocument();
      } else {
        expect(screen.getByRole("button", { name: "Sign out" })).toBeEnabled();
      }
      expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/auth/session");
      expect(fetchMock.mock.calls[1]?.[0]).toBe("/api/access");
      expect(fetchMock.mock.calls.some(([url]) => url === "/api/analyses")).toBe(
        profile.capabilities.viewTopology,
      );
    },
  );

  it("bootstraps authentication before access and skips the catalogue without topology capability", async () => {
    const noTopologyAccess = {
      ...accessFixture,
      roles: [],
      capabilities: {
        viewTopology: false,
        runGraphOperations: false,
        manageUploads: false,
      },
    } as const;
    const fetchMock = installApiFixtures({
      "/api/access": { body: noTopologyAccess },
    });

    render(<App />);

    expect(await screen.findByText("Topology access unavailable")).toBeVisible();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual(["/api/auth/session", "/api/access"]);
    expect(screen.queryByText(/could not load the application catalogue/i)).not.toBeInTheDocument();
  });

  it("keeps an unauthenticated OIDC deployment in a safe top-level login shell", async () => {
    const fetchMock = installApiFixtures({
      "/api/auth/session": {
        body: {
          ...oidcAuthSessionFixture,
          authenticated: false,
          loginPath: "/api/auth/oidc/login",
          logoutPath: null,
          csrfToken: null,
        },
      },
    });

    render(<App />);

    const login = await screen.findByRole("link", { name: "Sign in" });
    expect(login).toHaveAttribute("href", "/api/auth/oidc/login");
    expect(screen.getByText(/configured identity provider/i)).toBeVisible();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/auth/session");
    expect(screen.queryByRole("combobox", { name: "Capture" })).not.toBeInTheDocument();
  });

  it("re-bootstraps after an expired OIDC cookie is cleared and settles in sign-in", async () => {
    let sessionCalls = 0;
    const fetchMock = vi.fn<typeof fetch>(async (input) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url !== "/api/auth/session") throw new Error(`Unexpected API request: ${url}`);
      sessionCalls += 1;
      if (sessionCalls === 1) {
        return Response.json(
          { error: { code: "unauthorized", message: "Authentication is required." } },
          {
            status: 401,
            headers: {
              "Set-Cookie":
                "rabbitmq_topology_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax; Secure",
            },
          },
        );
      }
      return Response.json({
        ...oidcAuthSessionFixture,
        authenticated: false,
        loginPath: "/api/auth/oidc/login",
        logoutPath: null,
        csrfToken: null,
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);

    expect(await screen.findByRole("link", { name: "Sign in" })).toHaveAttribute(
      "href",
      "/api/auth/oidc/login",
    );
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "/api/auth/session",
      "/api/auth/session",
    ]);
    for (const [, options] of fetchMock.mock.calls) {
      expect(options?.credentials).toBe("same-origin");
    }
    expect(screen.queryByText(/Loading analysis catalogue/)).not.toBeInTheDocument();
  });

  it("uses the browser's native Basic challenge without rendering a password form", async () => {
    let sessionCalls = 0;
    const unauthenticatedBasic = {
      ...basicAuthSessionFixture,
      authenticated: false,
      logoutPath: null,
    } as const;
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(async (input) => {
        const url = typeof input === "string" ? input : input.toString();
        if (url === "/api/auth/session") {
          sessionCalls += 1;
          return Response.json(sessionCalls === 1 ? unauthenticatedBasic : basicAuthSessionFixture);
        }
        if (url === "/api/access") {
          return Response.json({ ...accessFixture, authMode: "basic" });
        }
        if (url === "/api/analyses") return Response.json(catalogueFixture);
        if (url === topologyUrl) return Response.json(topologyFixture);
        throw new Error(`Unexpected API request: ${url}`);
      }),
    );

    render(<App />);

    expect(await screen.findByLabelText("RabbitMQ topology graph")).toBeVisible();
    expect(document.querySelector('input[type="password"]')).toBeNull();
    expect(
      vi
        .mocked(fetch)
        .mock.calls.slice(0, 3)
        .map(([url]) => url),
    ).toEqual(["/api/auth/session", "/api/access", "/api/auth/session"]);
  });

  it("shows the unverified Basic shell after a rejected challenge and retries successfully", async () => {
    const user = userEvent.setup();
    let sessionCalls = 0;
    let accessCalls = 0;
    const unauthenticatedBasic = {
      ...basicAuthSessionFixture,
      authenticated: false,
      logoutPath: null,
    } as const;
    const fetchMock = vi.fn<typeof fetch>(async (input) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url === "/api/auth/session") {
        sessionCalls += 1;
        return Response.json(sessionCalls < 3 ? unauthenticatedBasic : basicAuthSessionFixture);
      }
      if (url === "/api/access") {
        accessCalls += 1;
        if (accessCalls === 1) {
          return Response.json(
            { error: { code: "unauthorized", message: "Authentication is required." } },
            {
              status: 401,
              headers: { "WWW-Authenticate": 'Basic realm="Topology", charset="UTF-8"' },
            },
          );
        }
        return Response.json({ ...accessFixture, authMode: "basic" });
      }
      if (url === "/api/analyses") return Response.json(catalogueFixture);
      if (url === topologyUrl) return Response.json(topologyFixture);
      throw new Error(`Unexpected API request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);

    expect(await screen.findByText(/native HTTP Basic credential prompt/i)).toBeVisible();
    expect(screen.getByRole("button", { name: "Authenticate" })).toBeEnabled();
    expect(document.querySelector('input[type="password"]')).toBeNull();
    expect(fetchMock.mock.calls.some(([url]) => url === "/api/analyses")).toBe(false);

    await user.click(screen.getByRole("button", { name: "Authenticate" }));

    expect(await screen.findByLabelText("RabbitMQ topology graph")).toBeVisible();
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "/api/auth/session",
      "/api/access",
      "/api/auth/session",
      "/api/access",
      "/api/auth/session",
      "/api/analyses",
      topologyUrl,
    ]);
    expect(document.querySelector('input[type="password"]')).toBeNull();
  });

  it("clears the protected workspace and recovery data after a runtime 401", async () => {
    const routes: Record<string, ApiFixture> = {
      "/api/auth/session": { body: oidcAuthSessionFixture },
      "/api/access": { body: { ...accessFixture, authMode: "oidc" } },
      "/api/analyses": { body: catalogueFixture },
      [topologyUrl]: { body: topologyFixture },
    };
    installApiFixtures(routes);
    render(<App />);
    await screen.findByLabelText("RabbitMQ topology graph");
    sessionStorage.setItem(ACTIVE_SCC_RUN_STORAGE_KEY, "owned-scc-recovery");
    sessionStorage.setItem(ACTIVE_CYCLE_RUN_STORAGE_KEY, "owned-cycle-recovery");
    routes["/api/access"] = {
      body: { error: { code: "authentication_required", message: "Authentication required." } },
      status: 401,
    };

    await act(async () => {
      await topologyApi.fetchAccess().catch(() => undefined);
    });

    expect(await screen.findByText("Authentication required")).toBeVisible();
    expect(screen.getByRole("link", { name: "Sign in" })).toHaveAttribute(
      "href",
      "/api/auth/oidc/login",
    );
    expect(screen.queryByLabelText("RabbitMQ topology graph")).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Capture" })).not.toBeInTheDocument();
    expect(sessionStorage.getItem(ACTIVE_SCC_RUN_STORAGE_KEY)).toBeNull();
    expect(sessionStorage.getItem(ACTIVE_CYCLE_RUN_STORAGE_KEY)).toBeNull();
  });

  it.each(["unverified", "rejected"])(
    "preserves the newer Basic workspace and logout after a late StrictMode %s bootstrap",
    async (oldState) => {
      const user = userEvent.setup();
      const older = deferred<Response>();
      const fixtureFetch = installApiFixtures({
        "/api/auth/session": { body: basicAuthSessionFixture },
        "/api/access": { body: { ...accessFixture, authMode: "basic" } },
        "/api/analyses": { body: catalogueFixture },
        [topologyUrl]: { body: topologyFixture },
        "/api/auth/logout": { body: null, status: 204 },
      });
      let bootstrapCalls = 0;
      vi.stubGlobal(
        "fetch",
        vi.fn<typeof fetch>((input, init) => {
          if (input === "/api/auth/session" && bootstrapCalls++ === 0) return older.promise;
          return fixtureFetch(input, init);
        }),
      );
      render(
        <StrictMode>
          <App />
        </StrictMode>,
      );
      await screen.findByLabelText("RabbitMQ topology graph");

      await act(async () => {
        older.resolve(
          oldState === "unverified"
            ? Response.json({ ...basicAuthSessionFixture, authenticated: false, logoutPath: null })
            : Response.json(
                { error: { code: "authentication_required", message: "Authentication required." } },
                {
                  status: 401,
                  headers: { "WWW-Authenticate": 'Basic realm="Topology", charset="UTF-8"' },
                },
              ),
        );
        await older.promise;
      });

      expect(screen.getByLabelText("RabbitMQ topology graph")).toBeVisible();
      await user.click(screen.getByRole("button", { name: "Sign out" }));
      expect(await screen.findByText("Signed out")).toBeVisible();
      expect(screen.queryByText("Logout is not available.")).not.toBeInTheDocument();
    },
  );

  it("keeps the current workspace and recovery when a superseded protected request returns 401", async () => {
    const older = deferred<Response>();
    const fixtureFetch = installApiFixtures({
      "/api/auth/session": { body: oidcAuthSessionFixture },
      "/api/access": { body: { ...accessFixture, authMode: "oidc" } },
      "/api/analyses": { body: catalogueFixture },
      [topologyUrl]: { body: topologyFixture },
    });
    render(<App />);
    await screen.findByLabelText("RabbitMQ topology graph");
    sessionStorage.setItem(ACTIVE_SCC_RUN_STORAGE_KEY, "current-recovery");
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>((input, init) =>
        input === "/api/access" ? older.promise : fixtureFetch(input, init),
      ),
    );
    const oldRequest = topologyApi.fetchAccess().catch(() => undefined);
    await act(async () => {
      await topologyApi.fetchAuthSession();
    });

    await act(async () => {
      older.resolve(
        Response.json(
          { error: { code: "authentication_required", message: "Authentication required." } },
          { status: 401 },
        ),
      );
      await oldRequest;
    });

    expect(screen.getByLabelText("RabbitMQ topology graph")).toBeVisible();
    expect(sessionStorage.getItem(ACTIVE_SCC_RUN_STORAGE_KEY)).toBe("current-recovery");
    expect(screen.queryByText("Authentication required")).not.toBeInTheDocument();
  });

  it.each([204, 503])(
    "ignores a late logout %s after Basic authentication loss and retry",
    async (status) => {
      const user = userEvent.setup();
      const older = deferred<Response>();
      const routes: Record<string, ApiFixture> = {
        "/api/auth/session": { body: basicAuthSessionFixture },
        "/api/access": { body: { ...accessFixture, authMode: "basic" } },
        "/api/analyses": { body: catalogueFixture },
        [topologyUrl]: { body: topologyFixture },
        "/api/auth/logout": { body: null, status: 204 },
      };
      const fixtureFetch = installApiFixtures(routes);
      let logoutCalls = 0;
      vi.stubGlobal(
        "fetch",
        vi.fn<typeof fetch>((input, init) => {
          if (input === "/api/auth/logout" && logoutCalls++ === 0) return older.promise;
          return fixtureFetch(input, init);
        }),
      );
      render(<App />);
      await screen.findByLabelText("RabbitMQ topology graph");
      await user.click(screen.getByRole("button", { name: "Sign out" }));
      routes["/api/access"] = {
        body: { error: { code: "authentication_required", message: "Authentication required." } },
        status: 401,
      };
      await act(async () => {
        await topologyApi.fetchAccess().catch(() => undefined);
      });
      await screen.findByRole("button", { name: "Authenticate" });
      routes["/api/access"] = { body: { ...accessFixture, authMode: "basic" } };
      await user.click(screen.getByRole("button", { name: "Authenticate" }));
      await screen.findByLabelText("RabbitMQ topology graph");
      const currentOwner = sessionStorage.getItem("rabbitmq-topology-session-token");

      await act(async () => {
        older.resolve(
          status === 204
            ? new Response(null, { status })
            : Response.json(
                { error: { code: "logout_failed", message: "Old logout failed." } },
                { status },
              ),
        );
        await older.promise;
      });

      expect(screen.getByLabelText("RabbitMQ topology graph")).toBeVisible();
      expect(screen.getByRole("button", { name: "Sign out" })).toBeEnabled();
      expect(screen.queryByText("Old logout failed.")).not.toBeInTheDocument();
      expect(sessionStorage.getItem("rabbitmq-topology-session-token")).toBe(currentOwner);
    },
  );

  const pendingOperationCleanupCases = (["SCC", "cycle"] as const).flatMap((operation) =>
    (["logout", "401"] as const).map((boundary) => ({ operation, boundary })),
  );

  it.each(pendingOperationCleanupCases)(
    "keeps recovery cleared when a pending $operation start completes after $boundary",
    async ({ operation, boundary }) => {
      const user = userEvent.setup();
      const older = deferred<Response>();
      const routes: Record<string, ApiFixture> = {
        "/api/auth/session": { body: oidcAuthSessionFixture },
        "/api/access": { body: { ...accessFixture, authMode: "oidc" } },
        "/api/analyses": { body: catalogueFixture },
        [topologyUrl]: { body: topologyFixture },
        "/api/auth/logout": { body: null, status: 204 },
      };
      const fixtureFetch = installApiFixtures(routes);
      vi.stubGlobal(
        "fetch",
        vi.fn<typeof fetch>((input, init) =>
          input === (operation === "SCC" ? "/api/scc-runs" : "/api/cycle-runs")
            ? older.promise
            : fixtureFetch(input, init),
        ),
      );
      render(<App />);
      await screen.findByLabelText("RabbitMQ topology graph");
      await user.click(
        screen.getByRole("button", {
          name: operation === "SCC" ? "Run cyclic components" : "Run structural cycles",
        }),
      );
      expect(
        sessionStorage.getItem(
          operation === "SCC" ? ACTIVE_SCC_RUN_STORAGE_KEY : ACTIVE_CYCLE_RUN_STORAGE_KEY,
        ),
      ).not.toBeNull();

      if (boundary === "logout") {
        await user.click(screen.getByRole("button", { name: "Sign out" }));
        await screen.findByText("Signed out");
      } else {
        routes["/api/access"] = {
          body: { error: { code: "authentication_required", message: "Authentication required." } },
          status: 401,
        };
        await act(async () => {
          await topologyApi.fetchAccess().catch(() => undefined);
        });
        await screen.findByText("Authentication required");
      }
      expect(sessionStorage.getItem(ACTIVE_SCC_RUN_STORAGE_KEY)).toBeNull();
      expect(sessionStorage.getItem(ACTIVE_CYCLE_RUN_STORAGE_KEY)).toBeNull();
      await act(async () => {
        older.resolve(
          Response.json(operation === "SCC" ? runningSccRunFixture : runningCycleRunFixture, {
            status: 202,
          }),
        );
        await older.promise;
      });

      expect(sessionStorage.getItem(ACTIVE_SCC_RUN_STORAGE_KEY)).toBeNull();
      expect(sessionStorage.getItem(ACTIVE_CYCLE_RUN_STORAGE_KEY)).toBeNull();
      expect(screen.queryByLabelText("RabbitMQ topology graph")).not.toBeInTheDocument();
    },
  );

  it("signs an OIDC application session out through the shared CSRF-aware API", async () => {
    const user = userEvent.setup();
    const fetchMock = installApiFixtures({
      "/api/auth/session": { body: oidcAuthSessionFixture },
      "/api/access": { body: { ...accessFixture, authMode: "oidc" } },
      "/api/analyses": { body: catalogueFixture },
      [topologyUrl]: { body: topologyFixture },
      "/api/auth/logout": { body: null, status: 204 },
    });
    render(<App />);
    await screen.findByLabelText("RabbitMQ topology graph");

    await user.click(screen.getByRole("button", { name: "Sign out" }));

    expect(await screen.findByText("Signed out")).toBeVisible();
    expect(screen.getByText(/provider may keep a separate sign-in session/i)).toBeVisible();
    expect(screen.getByRole("link", { name: "Sign in" })).toHaveAttribute(
      "href",
      "/api/auth/oidc/login",
    );
    const logoutCall = fetchMock.mock.calls.find(([url]) => url === "/api/auth/logout");
    expect(logoutCall?.[1]?.credentials).toBe("same-origin");
    const headers = new Headers(logoutCall?.[1]?.headers);
    expect(headers.get("X-CSRF-Token")).toBe(oidcAuthSessionFixture.csrfToken);
    expect(headers.has("X-Session-Token")).toBe(false);
  });

  it("explains Basic logout cleanup limits and clears its tab owner", async () => {
    const user = userEvent.setup();
    installApiFixtures({
      "/api/auth/session": { body: basicAuthSessionFixture },
      "/api/access": { body: { ...accessFixture, authMode: "basic" } },
      "/api/analyses": { body: catalogueFixture },
      [topologyUrl]: { body: topologyFixture },
      "/api/auth/logout": { body: null, status: 204 },
    });
    render(<App />);
    await screen.findByLabelText("RabbitMQ topology graph");
    expect(sessionStorage.getItem("rabbitmq-topology-session-token")).not.toBeNull();

    await user.click(screen.getByRole("button", { name: "Sign out" }));

    expect(await screen.findByText(/local application session/i)).toBeVisible();
    expect(screen.getByText(/may retain and resend HTTP Basic credentials/i)).toBeVisible();
    expect(document.querySelector('input[type="password"]')).toBeNull();
    expect(sessionStorage.getItem("rabbitmq-topology-session-token")).toBeNull();
  });

  it("loads the first analysis into the complete workbench", async () => {
    installApiFixtures({
      "/api/access": { body: accessFixture },
      "/api/analyses": { body: catalogueFixture },
      [topologyUrl]: { body: topologyFixture },
    });

    render(<App />);

    expect(screen.getByText("RabbitMQ Topology Analyzer")).toBeVisible();
    expect(await screen.findByText("Complete capture")).toBeVisible();
    expect(screen.getByRole("combobox", { name: "Capture" })).toHaveValue(
      "static:orders-production",
    );
    expect(screen.getByLabelText("RabbitMQ topology graph")).toHaveTextContent("orders.events");
    expect(screen.getByText(/offline configuration snapshot/i)).toBeVisible();
    expect(screen.getByRole("button", { name: "Upload capture" })).toBeEnabled();
  });

  it("keeps a late topology response from replacing the newer selection", async () => {
    const user = userEvent.setup();
    let resolveFirst: ((response: Response) => void) | undefined;
    const firstResponse = new Promise<Response>((resolve) => {
      resolveFirst = resolve;
    });
    const secondSummary = {
      ...catalogueFixture.static[0],
      analysisId: "billing-production",
      label: "Billing production",
      snapshotRevision: `sha256:${"b".repeat(64)}`,
    };
    const secondTopology = {
      ...topologyFixture,
      analysisId: secondSummary.analysisId,
      snapshotRevision: secondSummary.snapshotRevision,
      nodes: [
        {
          ...topologyFixture.nodes[2],
          id: "n1.billing-queue",
          data: { ...topologyFixture.nodes[2].data, name: "billing.queue" },
        },
      ],
      edges: [],
    } as const satisfies TopologyResponse;
    const catalogue = {
      static: [catalogueFixture.static[0], secondSummary],
      session: [],
    };

    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(async (input) => {
        const url = typeof input === "string" ? input : input.toString();
        if (url === "/api/auth/session") {
          return Response.json(noneAuthSessionFixture);
        }
        if (url === "/api/access") {
          return Response.json(accessFixture);
        }
        if (url === "/api/analyses") {
          return Response.json(catalogue);
        }
        if (url === topologyUrl) {
          return firstResponse;
        }
        if (url.endsWith("analysis_id=billing-production")) {
          return Response.json(secondTopology);
        }
        throw new Error(`Unexpected API request: ${url}`);
      }),
    );

    render(<App />);
    const picker = await screen.findByRole("combobox", { name: "Capture" });
    await user.selectOptions(picker, "static:billing-production");

    expect(await screen.findByText(/Rendered resources: billing\.queue/)).toBeVisible();
    resolveFirst?.(Response.json(topologyFixture));

    await waitFor(() => {
      expect(screen.getByText(/Rendered resources: billing\.queue/)).toBeVisible();
    });
    expect(screen.queryByText(/Rendered resources: orders\.events/)).not.toBeInTheDocument();
  });

  it("keeps primary workbench controls keyboard focusable", async () => {
    const user = userEvent.setup();
    installApiFixtures({
      "/api/access": { body: accessFixture },
      "/api/analyses": { body: catalogueFixture },
      [topologyUrl]: { body: topologyFixture },
    });
    render(<App />);

    const picker = await screen.findByRole("combobox", { name: "Capture" });
    picker.focus();
    await user.tab();
    expect(screen.getByText("Revision:")).toHaveFocus();
    await user.tab();
    expect(screen.getByRole("button", { name: "Upload capture" })).toHaveFocus();
    await user.tab();
    expect(screen.getByRole("button", { name: "Reset layout" })).toHaveFocus();
  });

  it("makes the workbench inert while the capture dialog is open and restores focus", async () => {
    const user = userEvent.setup();
    installApiFixtures({
      "/api/access": { body: accessFixture },
      "/api/analyses": { body: catalogueFixture },
      [topologyUrl]: { body: topologyFixture },
    });
    render(<App />);
    const opener = await screen.findByRole("button", { name: "Upload capture" });

    await user.click(opener);

    const dialog = screen.getByRole("dialog", { name: "Manage captured analysis" });
    const shell = document.querySelector(".app-shell");
    expect(dialog).toBeVisible();
    expect(shell).toHaveAttribute("inert");
    expect(shell).toHaveAttribute("aria-hidden", "true");
    await waitFor(() =>
      expect(within(dialog).getByRole("button", { name: "Close" })).toHaveFocus(),
    );

    await user.keyboard("{Escape}");

    expect(
      screen.queryByRole("dialog", { name: "Manage captured analysis" }),
    ).not.toBeInTheDocument();
    expect(shell).not.toHaveAttribute("inert");
    expect(opener).toHaveFocus();
  });

  it("removes all upload management affordances when the capability is false", async () => {
    installApiFixtures({
      "/api/access": {
        body: {
          ...accessFixture,
          roles: ["viewer"],
          capabilities: { ...accessFixture.capabilities, manageUploads: false },
        },
      },
      "/api/analyses": { body: catalogueFixture },
      [topologyUrl]: { body: topologyFixture },
    });

    render(<App />);

    await screen.findByLabelText("RabbitMQ topology graph");
    expect(screen.queryByRole("button", { name: "Upload capture" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete capture" })).not.toBeInTheDocument();
  });

  it("clears stale recovery so an empty upload-only catalogue keeps upload available", async () => {
    sessionStorage.setItem(ACTIVE_CYCLE_RUN_STORAGE_KEY, "stale-after-backend-restart");
    installApiFixtures({
      "/api/access": { body: accessFixture },
      "/api/analyses": { body: { static: [], session: [] } },
    });

    render(<App />);

    expect(await screen.findByText(/Upload a session capture/)).toBeVisible();
    expect(screen.getByRole("button", { name: "Upload capture" })).toBeEnabled();
    expect(sessionStorage.getItem(ACTIVE_CYCLE_RUN_STORAGE_KEY)).toBeNull();
  });

  it("loads the exact stored recovery target instead of the first catalogue analysis", async () => {
    const recoveringAnalysis = sessionAnalysis();
    const recoveringTopology = sessionTopology();
    const catalogue = {
      static: catalogueFixture.static,
      session: [recoveringAnalysis],
    } satisfies AnalysisCatalogueResponse;
    const activeRun = {
      ...runningCycleRunFixture,
      source: "session" as const,
      analysisId: recoveringAnalysis.analysisId,
    };
    sessionStorage.setItem(
      ACTIVE_CYCLE_RUN_STORAGE_KEY,
      JSON.stringify({
        operation: "structural_cycles",
        source: "session",
        analysisId: recoveringAnalysis.analysisId,
        snapshotRevision: recoveringAnalysis.snapshotRevision,
        maxCycles: 20,
        idempotencyKey: "40000000-0000-4000-8000-000000000004",
        runId: activeRun.runId,
      }),
    );
    vi.spyOn(topologyApi, "fetchCycleRun").mockResolvedValue(activeRun);
    const fetchMock = installApiFixtures({
      "/api/access": { body: accessFixture },
      "/api/analyses": { body: catalogue },
      "/api/topology?source=session&analysis_id=session-capture": { body: recoveringTopology },
    });

    render(<App />);

    expect(await screen.findByLabelText("RabbitMQ topology graph")).toBeVisible();
    expect(screen.getByRole("combobox", { name: "Capture" })).toHaveValue(
      "session:session-capture",
    );
    await waitFor(() =>
      expect(topologyApi.fetchCycleRun).toHaveBeenCalledWith(
        activeRun.runId,
        recoveringAnalysis.snapshotRevision,
      ),
    );
    expect(fetchMock.mock.calls.filter(([url]) => url === topologyUrl)).toHaveLength(0);
  });

  it("refreshes the catalogue and falls back when a recovery topology returns 404", async () => {
    const recoveringAnalysis = sessionAnalysis();
    sessionStorage.setItem(
      ACTIVE_CYCLE_RUN_STORAGE_KEY,
      JSON.stringify({
        operation: "structural_cycles",
        source: "session",
        analysisId: recoveringAnalysis.analysisId,
        snapshotRevision: recoveringAnalysis.snapshotRevision,
        maxCycles: 20,
        idempotencyKey: "40000000-0000-4000-8000-000000000004",
        runId: runningCycleRunFixture.runId,
      }),
    );
    const fetchMock = installApiFixtures({
      "/api/access": { body: accessFixture },
      "/api/analyses": {
        body: { static: catalogueFixture.static, session: [recoveringAnalysis] },
      },
      "/api/topology?source=session&analysis_id=session-capture": {
        body: { detail: "Analysis not found." },
        status: 404,
      },
      [topologyUrl]: { body: topologyFixture },
    });

    render(<App />);

    expect(await screen.findByLabelText("RabbitMQ topology graph")).toBeVisible();
    expect(screen.getByRole("combobox", { name: "Capture" })).toHaveValue(
      "static:orders-production",
    );
    expect(screen.getByRole("button", { name: "Upload capture" })).toBeEnabled();
    expect(sessionStorage.getItem(ACTIVE_CYCLE_RUN_STORAGE_KEY)).toBeNull();
    expect(fetchMock.mock.calls.filter(([url]) => url === "/api/analyses")).toHaveLength(2);
  });

  it("uploads the complete form, updates catalogue selection, and fetches only the new topology", async () => {
    const user = userEvent.setup();
    const uploaded = sessionAnalysis(`sha256:${"b".repeat(64)}`);
    const uploadedTopology = sessionTopology(uploaded.snapshotRevision);
    const fetchMock = installApiFixtures({
      "/api/access": { body: accessFixture },
      "/api/analyses": { body: catalogueFixture },
      [topologyUrl]: { body: topologyFixture },
      "/api/definitions/upload": {
        body: { analysis: uploaded, replaced: false },
        status: 201,
      },
      "/api/topology?source=session&analysis_id=session-capture": {
        body: uploadedTopology,
      },
    });
    render(<App />);
    await screen.findByLabelText("RabbitMQ topology graph");

    await user.click(screen.getByRole("button", { name: "Upload capture" }));
    const dialog = screen.getByRole("dialog", { name: "Manage captured analysis" });
    await user.type(within(dialog).getByLabelText("Analysis ID"), "session-capture");
    await user.type(within(dialog).getByLabelText("Display label (optional)"), "Session capture");
    await user.upload(
      within(dialog).getByLabelText("Definitions export"),
      new File(["definitions"], "definitions.json", { type: "application/json" }),
    );
    await user.upload(
      within(dialog).getByLabelText("Queue observations"),
      new File(["queues"], "queues.json", { type: "application/json" }),
    );
    await user.upload(
      within(dialog).getByLabelText("Exchange observations"),
      new File(["exchanges"], "exchanges.json", { type: "application/json" }),
    );
    await user.type(
      within(dialog).getByLabelText(/^In-cluster AMQP hosts \(optional\)/),
      "rabbit-a{enter}rabbit-b",
    );
    await user.click(within(dialog).getByRole("button", { name: "Upload capture" }));
    await within(dialog).findByText("Uploaded Session capture.");
    await user.click(within(dialog).getByRole("button", { name: "Close" }));

    await waitFor(() => {
      expect(screen.getByRole("combobox", { name: "Capture" })).toHaveValue(
        "session:session-capture",
      );
    });
    expect(screen.getByText("Complete capture")).toBeVisible();
    expect(fetchMock.mock.calls.filter(([url]) => url === "/api/analyses")).toHaveLength(1);
    expect(
      fetchMock.mock.calls.filter(
        ([url]) => url === "/api/topology?source=session&analysis_id=session-capture",
      ),
    ).toHaveLength(1);
  });

  it("preserves completed SCC focus and skips topology refetch after an identical refresh", async () => {
    const user = userEvent.setup();
    const analysis = sessionAnalysis();
    const topology = sessionTopology();
    const catalogue = { static: [], session: [analysis] } satisfies AnalysisCatalogueResponse;
    const succeeded = {
      ...succeededSccRunFixture,
      source: "session" as const,
      analysisId: analysis.analysisId,
    };
    vi.spyOn(topologyApi, "startSccRun").mockResolvedValue(succeeded);
    const fetchMock = installApiFixtures({
      "/api/access": { body: accessFixture },
      "/api/analyses": { body: catalogue },
      "/api/topology?source=session&analysis_id=session-capture": { body: topology },
      "/api/definitions/upload": {
        body: {
          analysis: { ...analysis, expiresAt: "2026-08-11T19:00:00Z" },
          replaced: true,
        },
      },
    });
    render(<App />);
    await screen.findByLabelText("RabbitMQ topology graph");
    const provenance = screen.getByRole("group", { name: "Selected snapshot" });
    await user.click(within(provenance).getByText("Revision:"));
    const originalMetadata = provenance.textContent;
    await user.click(screen.getByRole("button", { name: "Run cyclic components" }));
    const component = await screen.findByRole("checkbox", { name: /Cyclic component 1/ });
    await user.click(component);
    expect(component).toBeChecked();

    await user.click(screen.getByRole("button", { name: "Upload capture" }));
    const dialog = screen.getByRole("dialog", { name: "Manage captured analysis" });
    await user.upload(
      within(dialog).getByLabelText("Definitions export"),
      new File(["same"], "definitions.json", { type: "application/json" }),
    );
    await user.click(within(dialog).getByRole("button", { name: "Replace capture" }));
    await within(dialog).findByText("Replaced Session capture.");
    await user.click(within(dialog).getByRole("button", { name: "Close" }));

    await waitFor(() => expect(component).toBeChecked());
    expect(provenance.textContent).toBe(originalMetadata);
    expect(within(provenance).getByText("2026-08-10 08:00:00 UTC")).toHaveAttribute(
      "datetime", analysis.loadedAt,
    );
    expect(within(provenance).getByText(analysis.snapshotRevision)).toBeVisible();
    expect(within(screen.getByRole("region", { name: "Cyclic components" })).getByRole("button", { name: "Clear component filter" })).toBeVisible();
    expect(
      fetchMock.mock.calls.filter(
        ([url]) => url === "/api/topology?source=session&analysis_id=session-capture",
      ),
    ).toHaveLength(1);
    expect(fetchMock.mock.calls.filter(([url]) => url === "/api/analyses")).toHaveLength(1);
  });

  it("clears stale operation and recovery state after a changed refresh", async () => {
    const user = userEvent.setup();
    const analysis = sessionAnalysis();
    const topology = sessionTopology();
    const changed = {
      ...sessionAnalysis(`sha256:${"c".repeat(64)}`),
      loadedAt: "2026-08-11T09:30:45Z",
    };
    const changedTopology = sessionTopology(changed.snapshotRevision);
    const catalogue = { static: [], session: [analysis] } satisfies AnalysisCatalogueResponse;
    const succeeded = {
      ...succeededSccRunFixture,
      source: "session" as const,
      analysisId: analysis.analysisId,
    };
    vi.spyOn(topologyApi, "startSccRun").mockResolvedValue(succeeded);
    installApiFixtures({
      "/api/access": { body: accessFixture },
      "/api/analyses": { body: catalogue },
      "/api/topology?source=session&analysis_id=session-capture": { body: changedTopology },
      "/api/definitions/upload": { body: { analysis: changed, replaced: true } },
    });
    vi.spyOn(topologyApi, "fetchTopology")
      .mockResolvedValueOnce(topology)
      .mockResolvedValueOnce(changedTopology);
    render(<App />);
    await screen.findByLabelText("RabbitMQ topology graph");
    await user.click(screen.getByRole("button", { name: "Run cyclic components" }));
    await user.click(await screen.findByRole("checkbox", { name: /Cyclic component 1/ }));
    sessionStorage.setItem(ACTIVE_SCC_RUN_STORAGE_KEY, "stale-scc");
    sessionStorage.setItem(ACTIVE_CYCLE_RUN_STORAGE_KEY, "stale-cycle");

    await user.click(screen.getByRole("button", { name: "Upload capture" }));
    const dialog = screen.getByRole("dialog", { name: "Manage captured analysis" });
    await user.upload(
      within(dialog).getByLabelText("Definitions export"),
      new File(["changed"], "definitions.json", { type: "application/json" }),
    );
    await user.click(within(dialog).getByRole("button", { name: "Replace capture" }));
    await within(dialog).findByText("Replaced Session capture.");
    await user.click(within(dialog).getByRole("button", { name: "Close" }));

    await waitFor(() => expect(topologyApi.fetchTopology).toHaveBeenCalledTimes(2));
    const provenance = screen.getByRole("group", { name: "Selected snapshot" });
    expect(within(provenance).getByText("2026-08-11 09:30:45 UTC")).toHaveAttribute(
      "datetime", changed.loadedAt,
    );
    expect(within(provenance).getByText("Revision:")).toHaveTextContent("cccccccccccc…");
    await user.click(within(provenance).getByText("Revision:"));
    expect(within(provenance).getByText(changed.snapshotRevision)).toBeVisible();
    expect(within(provenance).queryByText(analysis.snapshotRevision)).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: /Cyclic component 1/ })).not.toBeInTheDocument();
    expect(sessionStorage.getItem(ACTIVE_SCC_RUN_STORAGE_KEY)).toBeNull();
    expect(sessionStorage.getItem(ACTIVE_CYCLE_RUN_STORAGE_KEY)).toBeNull();
    expect(topologyApi.fetchTopology).toHaveBeenCalledTimes(2);
  });

  it("preserves cycle page, detail, and recovery state after an identical refresh", async () => {
    const user = userEvent.setup();
    const analysis = sessionAnalysis();
    const topology = sessionTopology();
    const cycleRun = sessionCycleRun();
    const catalogue = { static: [], session: [analysis] } satisfies AnalysisCatalogueResponse;
    vi.spyOn(topologyApi, "startCycleRun").mockResolvedValue(cycleRun);
    vi.spyOn(topologyApi, "fetchCyclePage").mockResolvedValue(sessionCyclePage());
    vi.spyOn(topologyApi, "fetchCycleDetail").mockResolvedValue(sessionCycleDetail());
    const fetchMock = installApiFixtures({
      "/api/access": { body: accessFixture },
      "/api/analyses": { body: catalogue },
      "/api/topology?source=session&analysis_id=session-capture": { body: topology },
      "/api/definitions/upload": {
        body: {
          analysis: { ...analysis, expiresAt: "2026-08-11T19:00:00Z" },
          replaced: true,
        },
      },
    });
    render(<App />);
    await screen.findByLabelText("RabbitMQ topology graph");
    await user.click(screen.getByRole("button", { name: "Run structural cycles" }));
    await user.click(await screen.findByRole("button", { name: /Cycle 2/ }));
    expect(await screen.findByRole("article", { name: "Cycle 2 path" })).toBeVisible();
    sessionStorage.setItem(ACTIVE_CYCLE_RUN_STORAGE_KEY, "identical-cycle-recovery");

    await user.click(screen.getByRole("button", { name: "Upload capture" }));
    const dialog = screen.getByRole("dialog", { name: "Manage captured analysis" });
    await user.upload(
      within(dialog).getByLabelText("Definitions export"),
      new File(["same"], "definitions.json", { type: "application/json" }),
    );
    await user.click(within(dialog).getByRole("button", { name: "Replace capture" }));
    await within(dialog).findByText("Replaced Session capture.");
    await user.click(within(dialog).getByRole("button", { name: "Close" }));

    expect(await screen.findByRole("article", { name: "Cycle 2 path" })).toBeVisible();
    expect(within(screen.getByRole("article", { name: "Cycle 2 path" })).getByRole("button", { name: "Clear cycle filter" })).toBeVisible();
    expect(sessionStorage.getItem(ACTIVE_CYCLE_RUN_STORAGE_KEY)).toBe("identical-cycle-recovery");
    expect(
      fetchMock.mock.calls.filter(
        ([url]) => url === "/api/topology?source=session&analysis_id=session-capture",
      ),
    ).toHaveLength(1);
  });

  it("clears cycle page, detail, and recovery state after a changed refresh", async () => {
    const user = userEvent.setup();
    const analysis = sessionAnalysis();
    const topology = sessionTopology();
    const changed = sessionAnalysis(`sha256:${"d".repeat(64)}`);
    const changedTopology = sessionTopology(changed.snapshotRevision);
    const catalogue = { static: [], session: [analysis] } satisfies AnalysisCatalogueResponse;
    vi.spyOn(topologyApi, "startCycleRun").mockResolvedValue(sessionCycleRun());
    vi.spyOn(topologyApi, "fetchCyclePage").mockResolvedValue(sessionCyclePage());
    vi.spyOn(topologyApi, "fetchCycleDetail").mockResolvedValue(sessionCycleDetail());
    installApiFixtures({
      "/api/access": { body: accessFixture },
      "/api/analyses": { body: catalogue },
      "/api/definitions/upload": { body: { analysis: changed, replaced: true } },
    });
    vi.spyOn(topologyApi, "fetchTopology")
      .mockResolvedValueOnce(topology)
      .mockResolvedValueOnce(changedTopology);
    render(<App />);
    await screen.findByLabelText("RabbitMQ topology graph");
    await user.click(screen.getByRole("button", { name: "Run structural cycles" }));
    await user.click(await screen.findByRole("button", { name: /Cycle 2/ }));
    expect(await screen.findByRole("article", { name: "Cycle 2 path" })).toBeVisible();
    sessionStorage.setItem(ACTIVE_CYCLE_RUN_STORAGE_KEY, "changed-cycle-recovery");

    await user.click(screen.getByRole("button", { name: "Upload capture" }));
    const dialog = screen.getByRole("dialog", { name: "Manage captured analysis" });
    await user.upload(
      within(dialog).getByLabelText("Definitions export"),
      new File(["changed"], "definitions.json", { type: "application/json" }),
    );
    await user.click(within(dialog).getByRole("button", { name: "Replace capture" }));
    await within(dialog).findByText("Replaced Session capture.");
    await user.click(within(dialog).getByRole("button", { name: "Close" }));

    await waitFor(() => expect(topologyApi.fetchTopology).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole("article", { name: "Cycle 2 path" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Cycle 2/ })).not.toBeInTheDocument();
    expect(sessionStorage.getItem(ACTIVE_CYCLE_RUN_STORAGE_KEY)).toBeNull();
  });

  it("deletes the selected session capture without refetching the catalogue", async () => {
    const user = userEvent.setup();
    const analysis = sessionAnalysis();
    const catalogue = { static: [], session: [analysis] } satisfies AnalysisCatalogueResponse;
    const fetchMock = installApiFixtures({
      "/api/access": { body: accessFixture },
      "/api/analyses": { body: catalogue },
      "/api/topology?source=session&analysis_id=session-capture": { body: sessionTopology() },
      "/api/analyses/session-capture": { body: null, status: 204 },
    });
    render(<App />);
    await screen.findByLabelText("RabbitMQ topology graph");

    await user.click(screen.getByRole("button", { name: "Upload capture" }));
    await user.click(screen.getByRole("button", { name: "Delete capture" }));

    expect(await screen.findByText("No captures available")).toBeVisible();
    expect(fetchMock.mock.calls.filter(([url]) => url === "/api/analyses")).toHaveLength(1);
  });

  it("refreshes catalogue and selects a fallback after an expired delete returns 404", async () => {
    const user = userEvent.setup();
    const analysis = sessionAnalysis();
    const initialCatalogue = {
      static: catalogueFixture.static,
      session: [analysis],
    } satisfies AnalysisCatalogueResponse;
    let catalogueRequestCount = 0;
    const fetchMock: ReturnType<typeof vi.fn<typeof fetch>> = vi.fn<typeof fetch>(
      async (input, options): Promise<Response> => {
        const url = typeof input === "string" ? input : input.toString();
        if (url === "/api/auth/session") return Response.json(noneAuthSessionFixture);
        if (url === "/api/access") return Response.json(accessFixture);
        if (url === "/api/analyses") {
          catalogueRequestCount += 1;
          return Response.json(catalogueRequestCount === 1 ? initialCatalogue : catalogueFixture);
        }
        if (url === topologyUrl) return Response.json(topologyFixture);
        if (url === "/api/topology?source=session&analysis_id=session-capture") {
          return Response.json(sessionTopology());
        }
        if (url === "/api/analyses/session-capture" && options?.method === "DELETE") {
          return Response.json({ detail: "Analysis not found." }, { status: 404 });
        }
        throw new Error(`Unexpected API request: ${url}`);
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<App />);
    await screen.findByLabelText("RabbitMQ topology graph");
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Capture" }),
      "session:session-capture",
    );

    await user.click(screen.getByRole("button", { name: "Upload capture" }));
    const dialog = screen.getByRole("dialog", { name: "Manage captured analysis" });
    await user.click(within(dialog).getByRole("button", { name: "Delete capture" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/catalogue was refreshed/i);
    await user.click(within(dialog).getByRole("button", { name: "Close" }));
    expect(screen.getByRole("combobox", { name: "Capture" })).toHaveValue(
      "static:orders-production",
    );
    expect(fetchMock.mock.calls.filter(([url]) => url === "/api/analyses")).toHaveLength(2);
  });
});
