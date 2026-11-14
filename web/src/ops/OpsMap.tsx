import * as maplibregl from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
import { useEffect, useRef } from 'react';
import { baseMapStyle, markerEl } from '../shared/mapStyle';
import { RIDER_COLORS } from '../shared/tokens';
import { heatToGeoJson } from './heat';
import { useOpsStore } from './store';

const EMPTY = { type: 'FeatureCollection' as const, features: [] };

export function OpsMap() {
  const container = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = container.current;
    if (!el) return;
    const map = new maplibregl.Map({
      container: el,
      style: baseMapStyle,
      center: [77.5946, 12.9716],
      zoom: 11,
    });
    map.on('error', () => {
      /* tiles optional */
    });
    let loaded = false;
    let fitted = false;
    let storeMarkers: maplibregl.Marker[] = [];
    let storesFor: unknown = null;

    const setSrc = (id: string, data: GeoJSON.FeatureCollection) => {
      const s = map.getSource(id);
      if (s && 'setData' in s) (s as maplibregl.GeoJSONSource).setData(data);
    };

    const sync = () => {
      const st = useOpsStore.getState();
      const city = st.city;
      if (city && storesFor !== city) {
        storesFor = city;
        storeMarkers.forEach((m) => m.remove());
        storeMarkers = Object.entries(city.stores).map(([id, [lat, lng]]) =>
          new maplibregl.Marker({ element: markerEl('marker marker-store', 'S', `ops-store-${id}`) })
            .setLngLat([lng, lat])
            .setPopup(new maplibregl.Popup({ offset: 16 }).setText(id))
            .addTo(map),
        );
      }
      if (city && !fitted) {
        fitted = true;
        const [[la1, ln1], [la2, ln2]] = city.bounds;
        map.fitBounds(
          [
            [ln1, la1],
            [ln2, la2],
          ],
          { padding: 30, duration: 0 },
        );
      }
      if (!loaded) return;
      setSrc('heat', heatToGeoJson(st.heat) as unknown as GeoJSON.FeatureCollection);
      setSrc('riders', {
        type: 'FeatureCollection',
        features: st.riders.map((r) => ({
          type: 'Feature',
          properties: { id: r.id, status: r.status },
          geometry: { type: 'Point', coordinates: [r.lng, r.lat] },
        })),
      });
    };

    map.on('style.load', () => {
      map.addSource('heat', { type: 'geojson', data: EMPTY });
      map.addSource('riders', { type: 'geojson', data: EMPTY });
      map.addLayer({
        id: 'heat-fill',
        type: 'fill',
        source: 'heat',
        paint: { 'fill-color': ['get', 'color'], 'fill-opacity': 0.55 },
      });
      map.addLayer({
        id: 'heat-line',
        type: 'line',
        source: 'heat',
        paint: { 'line-color': '#868e96', 'line-width': 0.6, 'line-opacity': 0.6 },
      });
      map.addLayer({
        id: 'riders',
        type: 'circle',
        source: 'riders',
        paint: {
          'circle-radius': 6,
          'circle-stroke-width': 2,
          'circle-stroke-color': '#ffffff',
          'circle-color': [
            'match',
            ['get', 'status'],
            'idle', RIDER_COLORS.idle,
            'assigned', RIDER_COLORS.assigned,
            'cooldown', RIDER_COLORS.cooldown,
            '#868e96',
          ],
        },
      });
      loaded = true;
      sync();
    });

    const unsub = useOpsStore.subscribe(sync);
    return () => {
      unsub();
      map.remove();
    };
  }, []);

  return <div ref={container} className="ops-map" data-testid="ops-map" />;
}
