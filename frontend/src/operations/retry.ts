import { ApiError, AuthenticationSupersededError } from "../api/topology";

export function isTransientOperationError(error: unknown): boolean {
  if (error instanceof AuthenticationSupersededError) return false;
  if (!(error instanceof ApiError)) return true;
  return error.status === 408 || error.status === 429 || error.status >= 500;
}

export function isSnapshotRevisionMismatch(error: unknown): boolean {
  return (
    error instanceof ApiError &&
    error.status === 409 &&
    error.code === "snapshot_revision_mismatch"
  );
}

export function operationRecoveryInvalid(error: unknown, admission = false): boolean {
  return (
    error instanceof AuthenticationSupersededError ||
    (error instanceof ApiError &&
      (error.status === 401 ||
        error.status === 404 ||
        isSnapshotRevisionMismatch(error) ||
        (admission &&
          error.status >= 400 &&
          error.status < 500 &&
          !isTransientOperationError(error))))
  );
}

export function operationRetryDelay(error: unknown, attempt: number): number {
  const backoff = 1000 * 2 ** attempt;
  const header = error instanceof ApiError ? error.retryAfter : null;
  if (!header) return backoff;
  const seconds = /^\d+$/.test(header) ? Number(header) : null;
  const requested = seconds === null ? Date.parse(header) - Date.now() : seconds * 1000;
  // Browsers overflow larger timer values into an immediate callback.
  return Number.isFinite(requested)
    ? Math.min(2_147_483_647, Math.max(backoff, requested))
    : backoff;
}

function waitForRetry(delay: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const abort = () => {
      window.clearTimeout(timeout);
      reject(new AuthenticationSupersededError());
    };
    const timeout = window.setTimeout(() => {
      signal.removeEventListener("abort", abort);
      resolve();
    }, delay);
    signal.addEventListener("abort", abort, { once: true });
    if (signal.aborted) abort();
  });
}

/** Retry the same request at most three times within its original workspace. */
export async function retryOperationRequest<T>(
  request: () => Promise<T>,
  signal: AbortSignal,
  isCurrent: () => boolean,
  onRetry: () => void,
): Promise<T> {
  for (let attempt = 0; ; attempt += 1) {
    if (signal.aborted || !isCurrent()) throw new AuthenticationSupersededError();
    try {
      const result = await request();
      if (signal.aborted || !isCurrent()) throw new AuthenticationSupersededError();
      return result;
    } catch (error) {
      if (signal.aborted || !isCurrent()) throw new AuthenticationSupersededError();
      if (!isTransientOperationError(error) || attempt === 2) throw error;
      onRetry();
      await waitForRetry(operationRetryDelay(error, attempt), signal);
    }
  }
}
