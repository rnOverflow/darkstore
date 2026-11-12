export type LngLat = [number, number];

export interface Segment {
  from: LngLat;
  to: LngLat;
  startMs: number;
  durationMs: number;
}

export const MIN_SEGMENT_MS = 250;
export const MAX_SEGMENT_MS = 2500;
export const DEFAULT_SEGMENT_MS = 1000;

/** Position along a segment at time `nowMs` (clamped, linear). */
export function positionAt(seg: Segment, nowMs: number): LngLat {
  const t = seg.durationMs <= 0 ? 1 : Math.min(1, Math.max(0, (nowMs - seg.startMs) / seg.durationMs));
  return [seg.from[0] + (seg.to[0] - seg.from[0]) * t, seg.from[1] + (seg.to[1] - seg.from[1]) * t];
}

/**
 * Start a new segment toward `target`. It begins from wherever the marker currently is (so the
 * marker never jumps) and lasts roughly one update interval (time since the previous update).
 */
export function nextSegment(
  current: Segment | null,
  target: LngLat,
  nowMs: number,
  lastArrivalMs: number | null,
): Segment {
  const from = current ? positionAt(current, nowMs) : target;
  const interval = lastArrivalMs === null ? DEFAULT_SEGMENT_MS : nowMs - lastArrivalMs;
  const durationMs = Math.min(MAX_SEGMENT_MS, Math.max(MIN_SEGMENT_MS, interval));
  return { from, to: target, startMs: nowMs, durationMs };
}
