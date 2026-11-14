import { setWorkerUrl, type StyleSpecification } from 'maplibre-gl';
import workerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?url';
import { MAP_BG, OSM_ATTRIBUTION, OSM_TILES } from './tokens';

// maplibre-gl 6 finds its worker with `new URL('./maplibre-gl-worker.mjs', import.meta.url)`, which Vite cannot see, so
// the worker was never emitted. nginx answered the missing file with index.html and the worker failed to start, which
// left every GeoJSON layer empty (ops map riders and heat cells, customer map trail). Importing it as an asset fixes that.
setWorkerUrl(workerUrl);

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
