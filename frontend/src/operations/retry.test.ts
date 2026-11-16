import { describe, expect, it, vi } from "vitest";
import { ApiError, AuthenticationSupersededError } from "../api/topology";
import { operationRecoveryInvalid, operationRetryDelay, retryOperationRequest } from "./retry";

describe("operation request retries", () => {
  it("honors numeric and date Retry-After values while backing off malformed values", () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(new Date("2026-10-18T10:00:00Z"));
      const busy = (header: string) => new ApiError(503, "busy", "Busy.", header);
      expect(operationRetryDelay(busy("4"), 0)).toBe(4000);
      expect(operationRetryDelay(busy("Sun, 18 Oct 2026 10:00:05 GMT"), 0)).toBe(5000);
      expect(operationRetryDelay(busy("invalid"), 1)).toBe(2000);
      expect(operationRetryDelay(busy("0"), 1)).toBe(2000);
      expect(operationRetryDelay(busy("9999999999"), 0)).toBe(2_147_483_647);
    } finally {
      vi.useRealTimers();
    }
  });

  it("does not treat a forbidden run read as proof that an admitted run ended", () => {
    const forbidden = new ApiError(403, "forbidden", "Forbidden.", null);
    expect(operationRecoveryInvalid(forbidden)).toBe(false);
    expect(operationRecoveryInvalid(forbidden, true)).toBe(true);
    expect(operationRecoveryInvalid(new AuthenticationSupersededError())).toBe(true);
  });

  it("clears retry timers when the workspace aborts during backoff", async () => {
    vi.useFakeTimers();
    const controller = new AbortController();
    const request = vi.fn().mockRejectedValue(new TypeError("Offline"));
    try {
      const result = retryOperationRequest(request, controller.signal, () => true, vi.fn());
      const rejected = expect(result).rejects.toBeInstanceOf(AuthenticationSupersededError);
      await vi.advanceTimersByTimeAsync(0);
      controller.abort();
      await rejected;
      expect(vi.getTimerCount()).toBe(0);
      expect(request).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });
});
