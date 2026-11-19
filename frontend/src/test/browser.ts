import { vi } from "vitest";

export const SESSION_STORAGE_KEY = "rabbitmq-topology-session-token";
type Uuid = `${string}-${string}-${string}-${string}-${string}`;
export const TEST_SESSION_TOKEN: Uuid = "00000000-0000-4000-8000-000000000001";

/** Clears per-tab state without replacing jsdom's Storage implementation. */
export function resetSessionStorage(): void {
  sessionStorage.clear();
}

/** Seeds the browser-session identity used by owner-scoped API calls. */
export function seedSessionToken(token = TEST_SESSION_TOKEN): void {
  sessionStorage.setItem(SESSION_STORAGE_KEY, token);
}

/** Makes UUID generation repeatable for API-module assertions. */
export function installDeterministicRandomUuid(uuid: Uuid = TEST_SESSION_TOKEN): void {
  vi.spyOn(globalThis.crypto, "randomUUID").mockReturnValue(uuid);
}

/** Installs a configurable matchMedia implementation for motion-sensitive UI. */
export function installReducedMotionMatchMedia(reducedMotion = true): void {
  const matchMedia = vi.fn((query: string): MediaQueryList => ({
    matches: reducedMotion && query === "(prefers-reduced-motion: reduce)",
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(() => true),
  }));

  vi.stubGlobal("matchMedia", matchMedia);
}
