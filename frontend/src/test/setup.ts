import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach, vi } from "vitest";
import { clearAuthenticationState } from "../api/topology";
import {
  installDeterministicRandomUuid,
  installReducedMotionMatchMedia,
  resetSessionStorage,
} from "./browser";

beforeEach(() => {
  vi.unstubAllGlobals();
  clearAuthenticationState();
  resetSessionStorage();
  installDeterministicRandomUuid();
  installReducedMotionMatchMedia();
});

afterEach(() => {
  cleanup();
  clearAuthenticationState();
  resetSessionStorage();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
