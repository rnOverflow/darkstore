import { isNewerId, maxId } from './eventId';
import type {
  EtaData,
  ForwardStatus,
  HistoryItem,
  LatLng,
  OrderEvent,
  RiderPos,
  Snapshot,
  Status,
  StoreRef,
} from './types';
import { FORWARD_STATUSES } from './types';

export const TRAIL_LIMIT = 600;

export interface OrderState {
  orderId: string;
  /** False until the first snapshot has been applied. */
  loaded: boolean;
  status: Status;
  /** Furthest happy-path stage reached (used to render a cancelled order's stepper). */
  furthest: ForwardStatus;
  store: StoreRef | null;
  dest: LatLng | null;
  eta: EtaData | null;
  rider: RiderPos | null;
  /** Received rider positions as [lng, lat]. */
  trail: [number, number][];
  /** Incremented on every accepted position. */
  positionSeq: number;
  history: HistoryItem[];
  lastEventId: string | null;
}

export function initialOrderState(orderId: string): OrderState {
  return {
    orderId,
    loaded: false,
    status: 'created',
    furthest: 'created',
    store: null,
    dest: null,
    eta: null,
    rider: null,
    trail: [],
    positionSeq: 0,
    history: [],
    lastEventId: null,
  };
}

export function isTerminal(s: Status): boolean {
  return s === 'delivered' || s === 'cancelled' || s === 'rejected';
}

export function forwardRank(s: Status): number {
  return (FORWARD_STATUSES as readonly string[]).indexOf(s);
}

/** Apply a status; forward-only, terminal statuses are final. Returns the same object if ignored. */
export function applyStatus(state: OrderState, status: Status, at: string): OrderState {
  if (status === state.status || isTerminal(state.status)) return state;
  const toCancelled = status === 'cancelled' || status === 'rejected';
  if (!toCancelled && forwardRank(status) <= forwardRank(state.status)) return state;
  const history = [...state.history, { status, at }];
  if (toCancelled) return { ...state, status, history };
  return {
    ...state,
    status,
    furthest: forwardRank(status) > forwardRank(state.furthest) ? (status as ForwardStatus) : state.furthest,
    history,
  };
}

function withRider(state: OrderState, rider: RiderPos): OrderState {
  const trail = [...state.trail, [rider.lng, rider.lat] as [number, number]];
  return {
    ...state,
    rider,
    trail: trail.length > TRAIL_LIMIT ? trail.slice(trail.length - TRAIL_LIMIT) : trail,
    positionSeq: state.positionSeq + 1,
  };
}

export function applySnapshot(state: OrderState, snap: Snapshot): OrderState {
  if (snap.order_id !== state.orderId) return state;
  const stale =
    state.lastEventId !== null &&
    snap.last_event_id !== null &&
    maxId(state.lastEventId, snap.last_event_id) === state.lastEventId &&
    state.lastEventId !== snap.last_event_id;

  // Statuses known to the snapshot, in order; applied through the forward-only guard.
  let next: OrderState = { ...state, loaded: true };
  for (const h of snap.history) next = applyStatus(next, h.status, h.at);
  next = applyStatus(next, snap.status, snap.history[snap.history.length - 1]?.at ?? '');
  const known = new Set(next.history.map((h) => h.status));
  const missing = snap.history.filter((h) => !known.has(h.status));
  if (missing.length > 0) {
    const merged = [...missing, ...next.history].sort(
      (a, b) => forwardRank(a.status) - forwardRank(b.status),
    );
    next = { ...next, history: merged };
  }
  next = { ...next, store: snap.store ?? next.store, dest: snap.dest ?? next.dest };
  next = { ...next, lastEventId: maxId(state.lastEventId, snap.last_event_id) };
  if (isTerminal(next.status)) next = { ...next, eta: null }; // nothing is arriving any more
  if (stale) return next; // we already hold newer eta/position than the snapshot
  if (!isTerminal(next.status)) next = { ...next, eta: snap.eta ?? next.eta };
  if (snap.rider) {
    const same =
      next.rider !== null && next.rider.lat === snap.rider.lat && next.rider.lng === snap.rider.lng;
    if (!same) next = withRider(next, snap.rider);
  }
  return next;
}

/** Apply one live/replayed event. Duplicates and out-of-order ids (<= last applied) are ignored. */
export function applyEvent(state: OrderState, ev: OrderEvent): OrderState {
  if (!isNewerId(ev.id, state.lastEventId)) return state;
  let next: OrderState = { ...state, lastEventId: ev.id };
  switch (ev.kind) {
    case 'status':
      next = applyStatus(next, ev.data.status, ev.data.at);
      if (isTerminal(next.status)) next = { ...next, eta: null };
      return next;
    case 'eta':
      return isTerminal(next.status) ? next : { ...next, eta: ev.data };
    case 'position':
      return isTerminal(next.status)
        ? next
        : withRider(next, { rider_id: ev.data.rider_id, lat: ev.data.lat, lng: ev.data.lng });
    case 'meta':
      return { ...next, store: ev.data.store, dest: ev.data.dest };
  }
}
