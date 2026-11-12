export const BACKOFF_BASE_MS = 500;
export const BACKOFF_MAX_MS = 10_000;
export const BACKOFF_JITTER = 0.25;

/** Un-jittered delay for the Nth consecutive failed attempt (0-based): 500, 1000, 2000 ... 10000. */
export function baseDelay(attempt: number): number {
  return Math.min(BACKOFF_MAX_MS, BACKOFF_BASE_MS * 2 ** Math.max(0, attempt));
}

/** Delay with +/-25% jitter (`rand` in [0,1)), never above the 10 s cap. */
export function reconnectDelay(attempt: number, rand: number): number {
  const factor = 1 - BACKOFF_JITTER + 2 * BACKOFF_JITTER * rand;
  return Math.min(BACKOFF_MAX_MS, Math.round(baseDelay(attempt) * factor));
}
