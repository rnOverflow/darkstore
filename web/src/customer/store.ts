import { create } from 'zustand';
import {
  applyEvent,
  applySnapshot,
  initialOrderState,
  type OrderState,
} from '../shared/orderReducer';
import type { ConnectionState, OrderEvent, Snapshot } from '../shared/types';

interface CustomerState {
  order: OrderState | null;
  token: string | null;
  conn: ConnectionState;
  placing: boolean;
  error: string | null;
  begin: (orderId: string, token: string) => void;
  reset: () => void;
  snapshot: (s: Snapshot) => void;
  event: (e: OrderEvent) => void;
  setConn: (c: ConnectionState) => void;
  setPlacing: (p: boolean) => void;
  setError: (e: string | null) => void;
}

export const useCustomerStore = create<CustomerState>((set) => ({
  order: null,
  token: null,
  conn: 'connecting',
  placing: false,
  error: null,
  begin: (orderId, token) =>
    set({ order: initialOrderState(orderId), token, conn: 'connecting', error: null }),
  reset: () => set({ order: null, token: null, conn: 'connecting' }),
  snapshot: (s) => set((st) => (st.order ? { order: applySnapshot(st.order, s) } : st)),
  event: (e) => set((st) => (st.order ? { order: applyEvent(st.order, e) } : st)),
  setConn: (conn) => set({ conn }),
  setPlacing: (placing) => set({ placing }),
  setError: (error) => set({ error }),
}));
