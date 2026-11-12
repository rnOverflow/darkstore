import { reconnectDelay } from './backoff';
import { setServerTime } from './clock';
import { parseServerMessage } from './protocol';
import type { ConnectionState, OrderEvent, Snapshot } from './types';

/** Minimal socket surface (the browser WebSocket is adapted to this; tests fake it). */
export interface SocketLike {
  onopen: (() => void) | null;
  onmessage: ((ev: { data: unknown }) => void) | null;
  onclose: ((ev: { code: number }) => void) | null;
  onerror: (() => void) | null;
  send(data: string): void;
  close(): void;
}

export type SocketFactory = (url: string) => SocketLike;

export function browserSocketFactory(url: string): SocketLike {
  const ws = new WebSocket(url);
  const s: SocketLike = {
    onopen: null,
    onmessage: null,
    onclose: null,
    onerror: null,
    send: (d) => ws.send(d),
    close: () => ws.close(),
  };
  ws.onopen = () => s.onopen?.();
  ws.onmessage = (e) => s.onmessage?.({ data: e.data });
  ws.onclose = (e) => s.onclose?.({ code: e.code });
  ws.onerror = () => s.onerror?.();
  return s;
}

export const DEAD_AFTER_MS = 40_000;
export const FATAL_CLOSE_CODES: readonly number[] = [4401, 4403];

export interface TrackingSocketOptions {
  url: string;
  token: string;
  orderId: string;
  /** Read at subscribe time so resume always uses the latest applied id. */
  getLastEventId: () => string | null;
  onSnapshot: (s: Snapshot) => void;
  onEvent: (e: OrderEvent) => void;
  onState: (s: ConnectionState) => void;
  factory?: SocketFactory;
  random?: () => number;
}

/**
 * Tracking WebSocket client: auth -> subscribe(last_event_id) -> snapshot -> events.
 * Reconnects with exponential backoff + jitter, answers heartbeats, treats 4401/4403 as fatal
 * (no retry) and recycles the connection if no frame arrives for 40 s.
 */
const ACK_EVERY_MS = 1000;
const ACK_EVERY_FRAMES = 100; // a burst inside one second must still be acknowledged

export class TrackingSocket {
  private ws: SocketLike | null = null;
  private gen = 0;
  private attempt = 0;
  private everLive = false;
  private running = false;
  private fatalHint = false;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private deadTimer: ReturnType<typeof setTimeout> | null = null;
  private state: ConnectionState | null = null;
  private lastAck = 0;
  private framesSinceAck = 0;

  constructor(private readonly o: TrackingSocketOptions) {}

  start(): void {
    if (this.running) return;
    this.running = true;
    this.open();
  }

  stop(): void {
    this.running = false;
    this.clearTimers();
    this.detach();
  }

  private setState(s: ConnectionState): void {
    if (this.state === s) return;
    this.state = s;
    this.o.onState(s);
  }

  private open(): void {
    this.retryTimer = null;
    this.fatalHint = false;
    const gen = ++this.gen;
    this.setState(this.everLive || this.attempt > 0 ? 'reconnecting' : 'connecting');
    const ws = (this.o.factory ?? browserSocketFactory)(this.o.url);
    this.ws = ws;
    this.armDeadTimer(gen);
    ws.onopen = () => {
      if (gen !== this.gen) return;
      ws.send(JSON.stringify({ type: 'auth', token: this.o.token }));
    };
    ws.onmessage = (ev) => {
      if (gen !== this.gen) return;
      this.armDeadTimer(gen);
      this.handleFrame(ws, ev.data);
    };
    ws.onclose = (ev) => {
      if (gen !== this.gen) return;
      this.handleClose(ev.code);
    };
    ws.onerror = () => {
      /* a close event always follows */
    };
  }

  private handleFrame(ws: SocketLike, raw: unknown): void {
    const msg = parseServerMessage(raw);
    if (!msg) return;
    switch (msg.type) {
      case 'auth_ok':
        if (msg.server_time !== undefined) setServerTime(msg.server_time);
        ws.send(
          JSON.stringify({
            type: 'subscribe',
            order_id: this.o.orderId,
            last_event_id: this.o.getLastEventId(),
          }),
        );
        return;
      case 'snapshot':
        this.attempt = 0;
        this.everLive = true;
        this.setState('live');
        this.o.onSnapshot(msg);
        this.maybeAck(ws);
        return;
      case 'event':
        this.o.onEvent(msg);
        this.maybeAck(ws);
        return;
      case 'heartbeat':
        ws.send(JSON.stringify({ type: 'pong' }));
        return;
      case 'error':
        if (msg.code === 'unauthorized' || msg.code === 'forbidden') this.fatalHint = true;
        return;
    }
  }

  /** Flow control: tell the server we are reading (at most once a second). Without acks the
   *  server pauses sending after its window and eventually drops the connection. */
  private maybeAck(ws: SocketLike): void {
    const now = Date.now();
    this.framesSinceAck += 1;
    if (now - this.lastAck < ACK_EVERY_MS && this.framesSinceAck < ACK_EVERY_FRAMES) return;
    this.lastAck = now;
    this.framesSinceAck = 0;
    ws.send(JSON.stringify({ type: 'ack' }));
  }

  private handleClose(code: number): void {
    this.clearTimers();
    this.detach();
    if (!this.running) return;
    if (FATAL_CLOSE_CODES.includes(code) || this.fatalHint) {
      this.running = false;
      this.setState('expired');
      return;
    }
    this.scheduleReconnect();
  }

  private scheduleReconnect(): void {
    this.setState('reconnecting');
    const rand = (this.o.random ?? Math.random)();
    const delay = reconnectDelay(this.attempt, rand);
    this.attempt += 1;
    this.retryTimer = setTimeout(() => {
      if (this.running) this.open();
    }, delay);
  }

  private armDeadTimer(gen: number): void {
    if (this.deadTimer) clearTimeout(this.deadTimer);
    this.deadTimer = setTimeout(() => {
      if (gen !== this.gen || !this.running) return;
      this.detach();
      this.scheduleReconnect();
    }, DEAD_AFTER_MS);
  }

  private clearTimers(): void {
    if (this.retryTimer) clearTimeout(this.retryTimer);
    if (this.deadTimer) clearTimeout(this.deadTimer);
    this.retryTimer = null;
    this.deadTimer = null;
  }

  /** Drop the current socket; its late events are ignored via the generation counter. */
  private detach(): void {
    this.gen += 1;
    const ws = this.ws;
    this.ws = null;
    if (!ws) return;
    ws.onopen = ws.onmessage = ws.onclose = ws.onerror = null;
    try {
      ws.close();
    } catch {
      /* already closed */
    }
  }
}

export function defaultWsUrl(): string {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  return `${proto}://${location.host}/ws`;
}
