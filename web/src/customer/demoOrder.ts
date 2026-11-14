import type { NewOrder } from '../shared/api';
import type { City } from '../shared/types';

export const MAX_RADIUS_KM = 2.5;
const KM_PER_DEG_LAT = 111.32;

const randInt = (rand: () => number, lo: number, hi: number): number =>
  lo + Math.floor(rand() * (hi - lo + 1));

/** Random point within `radiusKm` of [lat, lng], uniform over the disc. */
export function randomNear(
  [lat, lng]: [number, number],
  radiusKm: number,
  rand: () => number,
): [number, number] {
  const r = Math.sqrt(rand()) * radiusKm;
  const theta = rand() * 2 * Math.PI;
  const dLat = (r * Math.sin(theta)) / KM_PER_DEG_LAT;
  const dLng = (r * Math.cos(theta)) / (KM_PER_DEG_LAT * Math.cos((lat * Math.PI) / 180));
  return [lat + dLat, lng + dLng];
}

/** Build a demo order: random store, point within ~2.5 km, 1-3 distinct skus (sku-0..19), qty 1-3. */
export function buildDemoOrder(city: City, rand: () => number = Math.random): NewOrder {
  const ids = Object.keys(city.stores);
  if (ids.length === 0) throw new Error('city has no stores');
  const storeId = ids[Math.floor(rand() * ids.length)];
  const store = city.stores[storeId];
  const [lat, lng] = randomNear(store, MAX_RADIUS_KM, rand);
  const n = randInt(rand, 1, 3);
  const skus = new Set<number>();
  while (skus.size < n) skus.add(randInt(rand, 0, 19));
  return {
    store_id: storeId,
    items: [...skus].map((i) => ({ sku: `sku-${i}`, qty: randInt(rand, 1, 3) })),
    lat,
    lng,
  };
}
