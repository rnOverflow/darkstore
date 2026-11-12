import { describe, expect, it } from 'vitest';
import { compareStreamIds, isNewerId, maxId, parseStreamId } from './eventId';

describe('stream ids', () => {
  it('parses (ms, seq)', () => {
    expect(parseStreamId('1731000000000-7')).toEqual({ ms: 1731000000000, seq: 7 });
    expect(parseStreamId('abc')).toBeNull();
    expect(parseStreamId('1-')).toBeNull();
  });
  it('compares numerically, not lexically', () => {
    expect(compareStreamIds('9-0', '10-0')).toBeLessThan(0);
    expect(compareStreamIds('100-9', '100-10')).toBeLessThan(0);
    expect(compareStreamIds('100-1', '99-99')).toBeGreaterThan(0);
    expect(compareStreamIds('5-5', '5-5')).toBe(0);
  });
  it('isNewerId / maxId', () => {
    expect(isNewerId('1-0', null)).toBe(true);
    expect(isNewerId('1-1', '1-1')).toBe(false);
    expect(isNewerId('1-2', '1-10')).toBe(false);
    expect(isNewerId('garbage', '1-1')).toBe(false);
    expect(maxId('1-2', '1-10')).toBe('1-10');
    expect(maxId(null, '3-0')).toBe('3-0');
  });
});
