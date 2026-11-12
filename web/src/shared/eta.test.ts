import { describe, expect, it } from 'vitest';
import { formatEtaRange, formatEtaText, formatRange, remainingEta } from './eta';
import type { EtaData } from './types';

const at = '2024-01-01T00:00:00.000Z';
const t0 = Date.parse(at);
const eta = (p50: number, p90: number): EtaData => ({
  stage: 'out_for_delivery',
  p50_real_s: p50,
  p90_real_s: p90,
  source: 'model',
  at,
});

describe('eta', () => {
  it('formats ranges', () => {
    expect(formatRange(480, 720)).toBe('8-12 min');
    expect(formatRange(300, 300)).toBe('5 min');
    expect(formatRange(300, 310)).toBe('5-6 min');
    expect(formatRange(10, 40)).toBe('<1 min');
    expect(formatRange(0, 0)).toBe('now');
    expect(formatRange(20, 100)).toBe('1-2 min');
  });
  it('counts down in wall-clock time from the event `at`', () => {
    const e = eta(480, 720);
    expect(remainingEta(e, t0)).toEqual({ p50: 480, p90: 720 });
    expect(remainingEta(e, t0 + 120_000)).toEqual({ p50: 360, p90: 600 });
    expect(formatEtaRange(e, t0 + 120_000)).toBe('6-10 min');
    expect(formatEtaText(e, t0)).toBe('Arriving in 8-12 min');
  });
  it('clamps at zero and says now', () => {
    expect(remainingEta(eta(10, 20), t0 + 60_000)).toEqual({ p50: 0, p90: 0 });
    expect(formatEtaText(eta(10, 20), t0 + 60_000)).toBe('Arriving now');
  });
  it('does not count backwards if the clock is behind the event', () => {
    expect(remainingEta(eta(100, 200), t0 - 5000)).toEqual({ p50: 100, p90: 200 });
  });
});
