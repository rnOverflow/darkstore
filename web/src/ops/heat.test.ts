import { describe, expect, it } from 'vitest';
import { heatColor, heatToGeoJson } from './heat';

describe('heat', () => {
  it('diverges: supply blue, demand orange, balanced neutral', () => {
    expect(heatColor(0, 0, 4)).toBe('rgb(241,243,245)');
    expect(heatColor(4, 0, 4)).toBe('rgb(43,108,176)');
    expect(heatColor(0, 4, 4)).toBe('rgb(217,72,15)');
  });
  it('converts [lat,lng] boundaries to closed [lng,lat] rings', () => {
    const g = heatToGeoJson([
      { cell: 'a', supply: 1, demand: 0, boundary: [[1, 2], [3, 4], [5, 6]] },
      { cell: 'bad', supply: 1, demand: 0, boundary: [] },
    ]);
    expect(g.features).toHaveLength(1);
    expect(g.features[0].geometry.coordinates[0]).toEqual([[2, 1], [4, 3], [6, 5], [2, 1]]);
  });
});
