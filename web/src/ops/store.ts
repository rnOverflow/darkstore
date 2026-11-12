import { create } from 'zustand';
import type { City, HeatCell, LateOrder, OpsRider, OpsSummary } from '../shared/types';

interface OpsState {
  summary: OpsSummary | null;
  riders: OpsRider[];
  heat: HeatCell[];
  late: LateOrder[];
  city: City | null;
  expired: boolean;
  error: string | null;
  updatedAt: number | null;
  setData: (d: { summary: OpsSummary; riders: OpsRider[]; heat: HeatCell[]; late: LateOrder[] }) => void;
  setCity: (c: City) => void;
  setExpired: () => void;
  setError: (e: string | null) => void;
}

export const useOpsStore = create<OpsState>((set) => ({
  summary: null,
  riders: [],
  heat: [],
  late: [],
  city: null,
  expired: false,
  error: null,
  updatedAt: null,
  setData: (d) => set({ ...d, error: null, updatedAt: Date.now() }),
  setCity: (city) => set({ city }),
  setExpired: () => set({ expired: true }),
  setError: (error) => set({ error }),
}));
