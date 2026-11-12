import maplibregl from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
import { useEffect, useRef } from 'react';
import { nextSegment, positionAt, type Segment } from '../shared/interpolate';
import { baseMapStyle, markerEl } from '../shared/mapStyle';
import { useCustomerStore } from './store';

const TRAIL_SRC = 'trail';

/** Customer map: store + customer markers, an animated rider marker and the trail line. */
export function MapView() {
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
      /* tile failures are fine: markers still render on the grey background */
    });

    let loaded = false;
    let orderId: string | null = null;
    let fitted = false;
    let lastSeq = 0;
    let segment: Segment | null = null;
    let lastArrival: number | null = null;
    let storeMarker: maplibregl.Marker | null = null;
    let destMarker: maplibregl.Marker | null = null;
    let riderMarker: maplibregl.Marker | null = null;
    let raf = 0;

    const clearMarkers = () => {
      storeMarker?.remove();
      destMarker?.remove();
      riderMarker?.remove();
      storeMarker = destMarker = riderMarker = null;
      segment = null;
      lastArrival = null;
      lastSeq = 0;
      fitted = false;
    };

    const setTrail = (coords: [number, number][]) => {
      const src = map.getSource(TRAIL_SRC);
      if (src && 'setData' in src) {
        (src as maplibregl.GeoJSONSource).setData({
          type: 'Feature',
          properties: {},
          geometry: { type: 'LineString', coordinates: coords },
        });
      }
    };

    const sync = () => {
      const order = useCustomerStore.getState().order;
      if (!order || order.orderId !== orderId) {
        clearMarkers();
        orderId = order?.orderId ?? null;
        if (loaded) setTrail([]);
      }
      if (!order) return;
      if (order.store && !storeMarker) {
        storeMarker = new maplibregl.Marker({
          element: markerEl('marker marker-store', 'S', 'marker-store'),
        })
          .setLngLat([order.store.lng, order.store.lat])
          .addTo(map);
      }
      if (order.dest && !destMarker) {
        destMarker = new maplibregl.Marker({
          element: markerEl('marker marker-customer', 'C', 'marker-customer'),
        })
          .setLngLat([order.dest.lng, order.dest.lat])
          .addTo(map);
      }
      if (!fitted && order.store && order.dest) {
        fitted = true;
        const b = new maplibregl.LngLatBounds([order.store.lng, order.store.lat], [order.store.lng, order.store.lat]);
        b.extend([order.dest.lng, order.dest.lat]);
        map.fitBounds(b, { padding: 70, maxZoom: 15, duration: 0 });
      }
      if (order.rider && order.positionSeq !== lastSeq) {
        lastSeq = order.positionSeq;
        const now = performance.now();
        segment = nextSegment(segment, [order.rider.lng, order.rider.lat], now, lastArrival);
        lastArrival = now;
        if (!riderMarker) {
          riderMarker = new maplibregl.Marker({
            element: markerEl('marker marker-rider', 'R', 'marker-rider'),
          })
            .setLngLat(segment.from)
            .addTo(map);
        }
      }
    };

    let lastTrailPos: [number, number] | null = null;
    const frame = () => {
      raf = requestAnimationFrame(frame);
      const order = useCustomerStore.getState().order;
      if (!segment || !riderMarker || !order) return;
      const pos = positionAt(segment, performance.now());
      riderMarker.setLngLat(pos);
      if (loaded && (!lastTrailPos || pos[0] !== lastTrailPos[0] || pos[1] !== lastTrailPos[1])) {
        lastTrailPos = pos;
        setTrail([...order.trail.slice(0, -1), pos]);
      }
    };

    map.on('style.load', () => {
      map.addSource(TRAIL_SRC, {
        type: 'geojson',
        data: { type: 'Feature', properties: {}, geometry: { type: 'LineString', coordinates: [] } },
      });
      map.addLayer({
        id: 'trail-line',
        type: 'line',
        source: TRAIL_SRC,
        layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: { 'line-color': '#1c7ed6', 'line-width': 4, 'line-opacity': 0.8 },
      });
      loaded = true;
      sync();
    });

    const unsub = useCustomerStore.subscribe(sync);
    sync();
    raf = requestAnimationFrame(frame);
    return () => {
      cancelAnimationFrame(raf);
      unsub();
      map.remove();
    };
  }, []);

  return <div ref={container} className="map" data-testid="customer-map" />;
}
