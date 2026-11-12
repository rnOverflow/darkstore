import { HEAT_DEMAND, HEAT_NEUTRAL, HEAT_SUPPLY } from '../shared/tokens';
import type { HeatCell } from '../shared/types';

function hexToRgb(h: string): [number, number, number] {
  const n = parseInt(h.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function mix(a: string, b: string, t: number): string {
  const [ar, ag, ab] = hexToRgb(a);
  const [br, bg, bb] = hexToRgb(b);
  const c = (x: number, y: number) => Math.round(x + (y - x) * t);
  return `rgb(${c(ar, br)},${c(ag, bg)},${c(ab, bb)})`;
}

/**
 * Diverging colour for a cell: balance = supply - demand, scaled by `maxAbs`.
 * Supply surplus -> blue, balanced -> neutral, demand surplus -> orange/red.
 */
export function heatColor(supply: number, demand: number, maxAbs: number): string {
  const t = Math.max(-1, Math.min(1, (supply - demand) / Math.max(1, maxAbs)));
  return t >= 0 ? mix(HEAT_NEUTRAL, HEAT_SUPPLY, t) : mix(HEAT_NEUTRAL, HEAT_DEMAND, -t);
}

export interface HeatGeoJson {
  type: 'FeatureCollection';
  features: {
    type: 'Feature';
    properties: { cell: string; supply: number; demand: number; color: string };
    geometry: { type: 'Polygon'; coordinates: [number, number][][] };
  }[];
}

/** Cells ([lat,lng] boundaries) -> GeoJSON polygons ([lng,lat], closed rings). */
export function heatToGeoJson(cells: HeatCell[]): HeatGeoJson {
  const maxAbs = cells.reduce((m, c) => Math.max(m, Math.abs(c.supply - c.demand)), 1);
  return {
    type: 'FeatureCollection',
    features: cells
      .filter((c) => c.boundary.length >= 3)
      .map((c) => {
        const ring = c.boundary.map(([lat, lng]) => [lng, lat] as [number, number]);
        const first = ring[0];
        const last = ring[ring.length - 1];
        if (first[0] !== last[0] || first[1] !== last[1]) ring.push(first);
        return {
          type: 'Feature' as const,
          properties: { cell: c.cell, supply: c.supply, demand: c.demand, color: heatColor(c.supply, c.demand, maxAbs) },
          geometry: { type: 'Polygon' as const, coordinates: [ring] },
        };
      }),
  };
}
