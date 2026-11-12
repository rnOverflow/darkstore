import { describe, expect, it } from 'vitest';
import { baseDelay, reconnectDelay } from './backoff';

describe('backoff', () => {
  it('doubles from 500 ms up to a 10 s cap', () => {
    expect([0, 1, 2, 3, 4, 5, 6, 20].map(baseDelay)).toEqual([500, 1000, 2000, 4000, 8000, 10000, 10000, 10000]);
  });
  it('middle jitter equals the base delay', () => {
    expect([0, 1, 2, 3, 4, 5].map((a) => reconnectDelay(a, 0.5))).toEqual([500, 1000, 2000, 4000, 8000, 10000]);
  });
  it('jitter stays within +/-25% and never exceeds the cap', () => {
    for (let a = 0; a < 10; a++) {
      const lo = reconnectDelay(a, 0);
      const hi = reconnectDelay(a, 0.999999);
      expect(lo).toBeGreaterThanOrEqual(Math.round(baseDelay(a) * 0.75));
      expect(hi).toBeLessThanOrEqual(10_000);
      expect(hi).toBeGreaterThanOrEqual(lo);
    }
  });
});
