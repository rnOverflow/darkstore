import type { StyleSpecification } from 'maplibre-gl';
import { MAP_BG, OSM_ATTRIBUTION, OSM_TILES } from './tokens';

/** Grey background + OSM raster tiles. If tiles fail, overlays still render on the grey. */
export const baseMapStyle: StyleSpecification = {
  version: 8,
  sources: {
    osm: {
      type: 'raster',
      tiles: [OSM_TILES],
      tileSize: 256,
      maxzoom: 19,
      attribution: OSM_ATTRIBUTION,
    },
  },
  layers: [
    { id: 'bg', type: 'background', paint: { 'background-color': MAP_BG } },
    { id: 'osm', type: 'raster', source: 'osm' },
  ],
};

export function markerEl(className: string, label?: string, testId?: string): HTMLDivElement {
  const el = document.createElement('div');
  el.className = className;
  if (label) el.textContent = label;
  if (testId) el.dataset.testid = testId;
  return el;
}
