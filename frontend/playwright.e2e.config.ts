import { defineConfig } from "@playwright/test";
import { resolve } from "node:path";

const frontendRoot = import.meta.dirname;
const repositoryRoot = resolve(frontendRoot, "..");
const backendRoot = resolve(repositoryRoot, "backend");
const uvCacheRoot = resolve(frontendRoot, "test-results/uv-cache");
const server = (mode: string, port: number) =>
  `uv run --directory ${JSON.stringify(backendRoot)} python tests/browser_auth_server.py --mode ${mode} --port ${port}`;

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: "line",
  use: {
    baseURL: "http://127.0.0.1:4174",
    browserName: "chromium",
    headless: true,
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 1,
    serviceWorkers: "block",
    contextOptions: { reducedMotion: "reduce" },
    trace: "retain-on-failure",
  },
  projects: [
    { name: "none", testMatch: ["fullstack.spec.ts", "release.spec.ts"] },
    {
      name: "oidc",
      testMatch: "auth/oidc.spec.ts",
      use: { baseURL: "https://127.0.0.1:4176", ignoreHTTPSErrors: true },
    },
    {
      name: "basic",
      testMatch: "auth/basic.spec.ts",
      use: { baseURL: "https://127.0.0.1:4177", ignoreHTTPSErrors: true },
    },
  ],
  webServer: [
    {
      command: `pnpm build && ${server("none", 4174)}`,
      url: "http://127.0.0.1:4174/api/health/ready",
      reuseExistingServer: false,
      timeout: 120_000,
      env: { UV_CACHE_DIR: uvCacheRoot },
    },
    {
      command: server("provider", 4175),
      url: "http://localhost:4175/__test/state",
      reuseExistingServer: false,
      env: { UV_CACHE_DIR: uvCacheRoot },
    },
    ...(["oidc", "basic"] as const).map((mode, index) => ({
      command: server(mode, 4176 + index),
      url: `https://127.0.0.1:${4176 + index}/api/health/ready`,
      ignoreHTTPSErrors: true,
      reuseExistingServer: false,
      env: { UV_CACHE_DIR: uvCacheRoot },
    })),
  ],
});
