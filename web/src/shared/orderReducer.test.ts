import { describe, expect, it } from 'vitest';
import { applyEvent, applySnapshot, applyStatus, initialOrderState } from './orderReducer';
import type { EtaData, OrderEvent, Snapshot, Status } from './types';

const T = '2024-01-01T00:00:00Z';
const status = (id: string, s: Status): OrderEvent => ({
  type: 'event',
  id,
  kind: 'status',
  data: { status: s, at: T },
});
const pos = (id: string, lat: number, lng: number): OrderEvent => ({
  type: 'event',
  id,
  kind: 'position',
  data: { rider_id: 'r1', lat, lng, at: T },
});
const eta = (id: string, p50: number): OrderEvent => ({
  type: 'event',
  id,
  kind: 'eta',
  data: { stage: 'assigned', p50_real_s: p50, p90_real_s: p50 * 1.5, source: 'model', at: T },
});
const snap = (over: Partial<Snapshot> = {}): Snapshot => ({
  type: 'snapshot',
  order_id: 'o1',
  last_event_id: '100-0',
  status: 'picking',
  store: { id: 's0', lat: 1, lng: 2 },
  dest: { lat: 3, lng: 4 },
  eta: null,
  rider: null,
  history: [
    { status: 'created', at: T },
    { status: 'reserved', at: T },
    { status: 'picking', at: T },
  ],
  ...over,
});
const fresh = () => initialOrderState('o1');

describe('applyEvent', () => {
  it('applies events in order and tracks last id', () => {
    let s = fresh();
    s = applyEvent(s, status('1-0', 'reserved'));
    s = applyEvent(s, status('1-1', 'picking'));
    expect(s.status).toBe('picking');
    expect(s.lastEventId).toBe('1-1');
  });

  it('ignores duplicates and ids <= last applied', () => {
    let s = applyEvent(fresh(), pos('5-3', 1, 1));
    const again = applyEvent(s, pos('5-3', 9, 9));
    expect(again).toBe(s);
    expect(applyEvent(s, pos('5-2', 9, 9))).toBe(s);
    expect(applyEvent(s, pos('4-99', 9, 9))).toBe(s);
    s = applyEvent(s, pos('5-4', 2, 2));
    expect(s.positionSeq).toBe(2);
  });

  it('compares ids as (ms, seq) integer pairs', () => {
    let s = applyEvent(fresh(), pos('9-0', 1, 1));
    s = applyEvent(s, pos('10-0', 2, 2)); // lexically "10-0" < "9-0" but numerically newer
    expect(s.rider?.lat).toBe(2);
    s = applyEvent(s, pos('10-2', 3, 3));
    s = applyEvent(s, pos('10-10', 4, 4)); // seq 10 > 2
    expect(s.rider?.lat).toBe(4);
    expect(s.lastEventId).toBe('10-10');
  });

  it('never regresses status', () => {
    let s = applyEvent(fresh(), status('1-0', 'out_for_delivery'));
    s = applyEvent(s, status('2-0', 'picking'));
    expect(s.status).toBe('out_for_delivery');
    expect(s.lastEventId).toBe('2-0');
  });

  it('terminal statuses are final', () => {
    let s = applyEvent(fresh(), status('1-0', 'picking'));
    s = applyEvent(s, status('2-0', 'cancelled'));
    expect(s.status).toBe('cancelled');
    expect(s.furthest).toBe('picking');
    s = applyEvent(s, status('3-0', 'delivered'));
    expect(s.status).toBe('cancelled');
    expect(applyStatus(s, 'assigned', T)).toBe(s);
  });

  it('delivered clears eta and stops moving the rider', () => {
    let s = applyEvent(fresh(), eta('1-0', 100));
    expect(s.eta?.p50_real_s).toBe(100);
    s = applyEvent(s, status('2-0', 'delivered'));
    expect(s.eta).toBeNull();
    s = applyEvent(s, pos('3-0', 5, 5));
    expect(s.rider).toBeNull();
  });

  it('appends positions to the trail as [lng, lat]', () => {
    let s = applyEvent(fresh(), pos('1-0', 10, 20));
    s = applyEvent(s, pos('1-1', 11, 21));
    expect(s.trail).toEqual([
      [20, 10],
      [21, 11],
    ]);
  });
});

describe('applySnapshot', () => {
  it('seeds state from the snapshot', () => {
    const rider = { rider_id: 'r1', lat: 5, lng: 6 };
    const e: EtaData = { stage: 'picking', p50_real_s: 60, p90_real_s: 90, source: 'heuristic', at: T };
    const s = applySnapshot(fresh(), snap({ rider, eta: e }));
    expect(s.loaded).toBe(true);
    expect(s.status).toBe('picking');
    expect(s.furthest).toBe('picking');
    expect(s.store?.id).toBe('s0');
    expect(s.dest).toEqual({ lat: 3, lng: 4 });
    expect(s.eta).toEqual(e);
    expect(s.rider).toEqual(rider);
    expect(s.lastEventId).toBe('100-0');
    expect(s.history).toHaveLength(3);
  });

  it('ignores a snapshot for another order', () => {
    const s = fresh();
    expect(applySnapshot(s, snap({ order_id: 'other' }))).toBe(s);
  });

  it('snapshot then replay: older replayed events are dropped, newer applied', () => {
    let s = applySnapshot(fresh(), snap());
    s = applyEvent(s, status('99-5', 'reserved')); // covered by snapshot
    s = applyEvent(s, pos('100-0', 1, 1)); // equal to snapshot id: ignored
    expect(s.rider).toBeNull();
    s = applyEvent(s, status('100-1', 'picked'));
    s = applyEvent(s, pos('101-0', 7, 8));
    expect(s.status).toBe('picked');
    expect(s.rider).toMatchObject({ lat: 7, lng: 8 });
    expect(s.lastEventId).toBe('101-0');
  });

  it('a snapshot on reconnect keeps newer local state (no regress, no id regress)', () => {
    let s = applySnapshot(fresh(), snap());
    s = applyEvent(s, status('200-0', 'assigned'));
    s = applyEvent(s, pos('200-1', 9, 9));
    const again = applySnapshot(s, snap({ last_event_id: '100-0' }));
    expect(again.status).toBe('assigned');
    expect(again.lastEventId).toBe('200-1');
    expect(again.rider?.lat).toBe(9);
  });

  it('a newer snapshot after reconnect advances state', () => {
    let s = applySnapshot(fresh(), snap());
    s = applySnapshot(
      s,
      snap({
        last_event_id: '300-0',
        status: 'out_for_delivery',
        rider: { rider_id: 'r1', lat: 1, lng: 1 },
        history: [{ status: 'out_for_delivery', at: T }],
      }),
    );
    expect(s.status).toBe('out_for_delivery');
    expect(s.lastEventId).toBe('300-0');
    expect(s.rider).not.toBeNull();
  });
});

const meta = (id: string): OrderEvent => ({
  type: 'event',
  id,
  kind: 'meta',
  data: { store: { id: 's0', lat: 1, lng: 2 }, dest: { lat: 3, lng: 4 } },
});

describe('applyEvent: meta', () => {
  it('sets the store and destination for a client whose snapshot was taken before order.created was ingested', () => {
    let s = applySnapshot(fresh(), snap({ store: null, dest: null, status: 'created', history: [], last_event_id: null }));
    expect(s.store).toBeNull();
    expect(s.dest).toBeNull();
    s = applyEvent(s, meta('7-0'));
    expect(s.store).toEqual({ id: 's0', lat: 1, lng: 2 });
    expect(s.dest).toEqual({ lat: 3, lng: 4 });
    expect(s.lastEventId).toBe('7-0');
  });

  it('does not change the status and ignores a duplicate', () => {
    let s = applyEvent(fresh(), status('1-0', 'reserved'));
    s = applyEvent(s, meta('2-0'));
    expect(s.status).toBe('reserved');
    expect(applyEvent(s, meta('2-0'))).toBe(s);
  });
});
