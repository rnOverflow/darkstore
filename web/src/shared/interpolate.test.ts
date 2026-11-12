import { describe, expect, it } from 'vitest';
import { nextSegment, positionAt, type Segment } from './interpolate';

const seg: Segment = { from: [0, 0], to: [10, 20], startMs: 1000, durationMs: 1000 };

describe('positionAt', () => {
  it('is linear and clamped', () => {
    expect(positionAt(seg, 1000)).toEqual([0, 0]);
    expect(positionAt(seg, 1500)).toEqual([5, 10]);
    expect(positionAt(seg, 2000)).toEqual([10, 20]);
    expect(positionAt(seg, 9000)).toEqual([10, 20]);
    expect(positionAt(seg, 0)).toEqual([0, 0]);
  });
  it('handles zero duration', () => {
    expect(positionAt({ ...seg, durationMs: 0 }, 1000)).toEqual([10, 20]);
  });
});

describe('nextSegment', () => {
  it('first position starts in place', () => {
    const s = nextSegment(null, [3, 4], 500, null);
    expect(s.from).toEqual([3, 4]);
    expect(s.to).toEqual([3, 4]);
    expect(s.durationMs).toBe(1000);
  });
  it('starts from the current displayed position (never jumps)', () => {
    const s = nextSegment(seg, [20, 40], 1500, 1000);
    expect(s.from).toEqual([5, 10]);
    expect(s.to).toEqual([20, 40]);
    expect(s.durationMs).toBe(500);
    expect(positionAt(s, 1500)).toEqual([5, 10]);
  });
  it('clamps duration to a sane range', () => {
    expect(nextSegment(seg, [1, 1], 100_000, 0).durationMs).toBe(2500);
    expect(nextSegment(seg, [1, 1], 100_010, 100_000).durationMs).toBe(250);
  });
});
