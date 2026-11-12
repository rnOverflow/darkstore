import { defaultWsUrl, TrackingSocket } from '../shared/trackingSocket';
import { useCustomerStore } from './store';

let current: TrackingSocket | null = null;

/** Start (or restart) tracking an order over the WebSocket, feeding the customer store. */
export function startTracking(orderId: string, token: string): void {
  current?.stop();
  const store = useCustomerStore.getState();
  store.begin(orderId, token);
  current = new TrackingSocket({
    url: defaultWsUrl(),
    token,
    orderId,
    getLastEventId: () => useCustomerStore.getState().order?.lastEventId ?? null,
    onSnapshot: (s) => useCustomerStore.getState().snapshot(s),
    onEvent: (e) => useCustomerStore.getState().event(e),
    onState: (c) => useCustomerStore.getState().setConn(c),
  });
  current.start();
}

export function stopTracking(): void {
  current?.stop();
  current = null;
}
