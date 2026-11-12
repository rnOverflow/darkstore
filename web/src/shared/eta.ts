import type { EtaData } from './types';

export interface RemainingEta {
  p50: number;
  p90: number;
}

/** Remaining wall-clock seconds, counting down from the eta event's `at`. */
export function remainingEta(eta: EtaData, nowMs: number): RemainingEta {
  const at = Date.parse(eta.at);
  const elapsed = Number.isNaN(at) ? 0 : Math.max(0, (nowMs - at) / 1000);
  return {
    p50: Math.max(0, eta.p50_real_s - elapsed),
    p90: Math.max(0, eta.p90_real_s - elapsed),
  };
}

/** "8-12 min", "5 min", "<1 min" or "now". */
export function formatRange(p50s: number, p90s: number): string {
  if (p90s <= 0) return 'now';
  if (p90s < 60) return '<1 min';
  const lo = Math.max(1, Math.ceil(p50s / 60));
  const hi = Math.max(lo, Math.ceil(p90s / 60));
  return lo === hi ? `${lo} min` : `${lo}-${hi} min`;
}

export function formatEtaRange(eta: EtaData, nowMs: number): string {
  const r = remainingEta(eta, nowMs);
  return formatRange(r.p50, r.p90);
}

/** "Arriving in 8-12 min" / "Arriving now". */
export function formatEtaText(eta: EtaData, nowMs: number): string {
  const range = formatEtaRange(eta, nowMs);
  return range === 'now' ? 'Arriving now' : `Arriving in ${range}`;
}
