import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { DEAD_AFTER_MS, TrackingSocket, type SocketLike } from './trackingSocket';
import type { ConnectionState, OrderEvent, Snapshot } from './types';

class FakeSocket implements SocketLike {
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: unknown }) => void) | null = null;
  onclose: ((ev: { code: number }) => void) | null = null;
  onerror: (() => void) | null = null;
  sent: Record<string, unknown>[] = [];
  closed = false;
  send(d: string): void {
    this.sent.push(JSON.parse(d) as Record<string, unknown>);
  }
  close(): void {
    this.closed = true;
  }
  open(): void {
    this.onopen?.();
  }
  recv(obj: unknown): void {
    this.onmessage?.({ data: JSON.stringify(obj) });
  }
  drop(code = 1006): void {
    this.onclose?.({ code });
  }
}

const snapshot: Snapshot = {
  type: 'snapshot',
  order_id: 'o1',
  last_event_id: '5-0',
  status: 'created',
  store: null,
  dest: null,
  eta: null,
  rider: null,
  history: [],
};

function setup() {
  const sockets: FakeSocket[] = [];
  const states: ConnectionState[] = [];
  const snaps: Snapshot[] = [];
  const events: OrderEvent[] = [];
  let last: string | null = null;
  const client = new TrackingSocket({
    url: 'ws://x/ws',
    token: 'tok',
    orderId: 'o1',
    getLastEventId: () => last,
    onSnapshot: (s) => snaps.push(s),
    onEvent: (e) => {
      events.push(e);
      last = e.id;
    },
    onState: (s) => states.push(s),
    factory: () => {
      const s = new FakeSocket();
      sockets.push(s);
      return s;
    },
    random: () => 0.5,
  });
  return { client, sockets, states, snaps, events, setLast: (v: string | null) => (last = v) };
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe('TrackingSocket', () => {
  it('auths first, subscribes after auth_ok, goes live on snapshot', () => {
    const { client, sockets, states, snaps } = setup();
    client.start();
    expect(states).toEqual(['connecting']);
    sockets[0].open();
    expect(sockets[0].sent).toEqual([{ type: 'auth', token: 'tok' }]);
    sockets[0].recv({ type: 'auth_ok', role: 'customer' });
    expect(sockets[0].sent[1]).toEqual({ type: 'subscribe', order_id: 'o1', last_event_id: null });
    sockets[0].recv(snapshot);
    expect(states).toEqual(['connecting', 'live']);
    expect(snaps).toHaveLength(1);
  });

  it('acks received events for flow control, at most once per second', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-01-01T00:00:00Z'));
    const { client, sockets, events } = setup();
    client.start();
    sockets[0].open();
    sockets[0].recv({ type: 'auth_ok', role: 'customer' });
    sockets[0].recv({ type: 'snapshot', order_id: 'o1', last_event_id: '1-0', status: 'created', store: null, dest: null, eta: null, rider: null, history: [] });
    const acks = () => sockets[0].sent.filter((m: unknown) => (m as { type: string }).type === 'ack').length;
    expect(acks()).toBe(1);                                   // snapshot acked right away
    for (let i = 2; i < 12; i++) sockets[0].recv({ type: 'event', id: `${i}-0`, kind: 'position', data: { rider_id: 'r', lat: 1, lng: 2, at: 't' } });
    expect(acks()).toBe(1);                                   // burst within a second: no ack storm
    vi.setSystemTime(new Date('2026-01-01T00:00:01.500Z'));
    sockets[0].recv({ type: 'event', id: '13-0', kind: 'position', data: { rider_id: 'r', lat: 1, lng: 2, at: 't' } });
    expect(acks()).toBe(2);
    expect(events.length).toBeGreaterThan(0);
    vi.useRealTimers();
  });

  it('also acks by count, so a burst inside one second cannot stall the server', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-01-01T00:00:00Z'));
    const { client, sockets } = setup();
    client.start();
    sockets[0].open();
    sockets[0].recv({ type: 'auth_ok', role: 'customer' });
    const acks = () => sockets[0].sent.filter((m: unknown) => (m as { type: string }).type === 'ack').length;
    for (let i = 1; i <= 250; i++) {
      sockets[0].recv({ type: 'event', id: `${i}-0`, kind: 'position', data: { rider_id: 'r', lat: 1, lng: 2, at: 't' } });
    }
    expect(acks()).toBeGreaterThanOrEqual(3);                 // 1st frame, then every 100 frames, same instant
    vi.useRealTimers();
  });

  it('forwards events and answers heartbeats with pong', () => {
    const { client, sockets, events } = setup();
    client.start();
    sockets[0].open();
    sockets[0].recv({ type: 'event', id: '6-0', kind: 'status', data: { status: 'reserved', at: 'x' } });
    sockets[0].recv({ type: 'heartbeat', ts: 1 });
    expect(events).toHaveLength(1);
    expect(sockets[0].sent.at(-1)).toEqual({ type: 'pong' });
  });

  it('reconnects with backoff and resumes with last_event_id', () => {
    const { client, sockets, states } = setup();
    client.start();
    sockets[0].open();
    sockets[0].recv({ type: 'auth_ok', role: 'customer' });
    sockets[0].recv(snapshot);
    sockets[0].recv({ type: 'event', id: '7-1', kind: 'status', data: { status: 'reserved', at: 'x' } });
    sockets[0].drop();
    expect(states.at(-1)).toBe('reconnecting');
    vi.advanceTimersByTime(499);
    expect(sockets).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(sockets).toHaveLength(2);
    sockets[1].open();
    sockets[1].recv({ type: 'auth_ok', role: 'customer' });
    expect(sockets[1].sent[1]).toEqual({ type: 'subscribe', order_id: 'o1', last_event_id: '7-1' });
  });

  it('backoff grows across consecutive failures and resets after a snapshot', () => {
    const { client, sockets } = setup();
    client.start();
    const delays: number[] = [];
    for (let i = 0; i < 4; i++) {
      sockets[i].drop();
      const before = sockets.length;
      let waited = 0;
      while (sockets.length === before && waited < 20_000) {
        vi.advanceTimersByTime(100);
        waited += 100;
      }
      delays.push(waited);
    }
    expect(delays).toEqual([500, 1000, 2000, 4000]);
    sockets[4].open();
    sockets[4].recv({ type: 'auth_ok', role: 'customer' });
    sockets[4].recv(snapshot);
    sockets[4].drop();
    vi.advanceTimersByTime(500);
    expect(sockets).toHaveLength(6);
  });

  it.each([4401, 4403])('close code %i is fatal: state expired, no retry', (code) => {
    const { client, sockets, states } = setup();
    client.start();
    sockets[0].open();
    sockets[0].drop(code);
    expect(states.at(-1)).toBe('expired');
    vi.advanceTimersByTime(60_000);
    expect(sockets).toHaveLength(1);
  });

  it('an unauthorized error frame followed by any close is fatal', () => {
    const { client, sockets, states } = setup();
    client.start();
    sockets[0].open();
    sockets[0].recv({ type: 'error', code: 'unauthorized', message: 'no' });
    sockets[0].drop(1006);
    expect(states.at(-1)).toBe('expired');
    vi.advanceTimersByTime(60_000);
    expect(sockets).toHaveLength(1);
  });

  it('other close codes (4408 timeout) retry', () => {
    const { client, sockets } = setup();
    client.start();
    sockets[0].drop(4408);
    vi.advanceTimersByTime(500);
    expect(sockets).toHaveLength(2);
  });

  it('detects a dead connection after 40 s of silence and reconnects', () => {
    const { client, sockets, states } = setup();
    client.start();
    sockets[0].open();
    sockets[0].recv({ type: 'auth_ok', role: 'customer' });
    sockets[0].recv(snapshot);
    vi.advanceTimersByTime(DEAD_AFTER_MS - 1);
    expect(sockets).toHaveLength(1);
    sockets[0].recv({ type: 'heartbeat', ts: 1 }); // any frame resets the timer
    vi.advanceTimersByTime(DEAD_AFTER_MS - 1);
    expect(sockets).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(sockets[0].closed).toBe(true);
    expect(states.at(-1)).toBe('reconnecting');
    vi.advanceTimersByTime(500);
    expect(sockets).toHaveLength(2);
  });

  it('ignores frames from a superseded socket and stop() halts everything', () => {
    const { client, sockets, events } = setup();
    client.start();
    const old = sockets[0];
    old.drop();
    vi.advanceTimersByTime(500);
    old.recv({ type: 'event', id: '9-0', kind: 'status', data: { status: 'reserved', at: 'x' } });
    expect(events).toHaveLength(0);
    client.stop();
    sockets[1].drop();
    vi.advanceTimersByTime(60_000);
    expect(sockets).toHaveLength(2);
  });
});
