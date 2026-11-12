export const STATUSES = [
  'created',
  'reserved',
  'picking',
  'picked',
  'assigned',
  'out_for_delivery',
  'delivered',
  'cancelled',
  'rejected',
] as const;
export type Status = (typeof STATUSES)[number];

/** Forward-only happy-path statuses, in order. */
export const FORWARD_STATUSES = [
  'created',
  'reserved',
  'picking',
  'picked',
  'assigned',
  'out_for_delivery',
  'delivered',
] as const;
export type ForwardStatus = (typeof FORWARD_STATUSES)[number];

export type EtaStage = 'created' | 'picking' | 'assigned' | 'out_for_delivery';
export type EtaSource = 'model' | 'heuristic';

export interface LatLng {
  lat: number;
  lng: number;
}
export interface StoreRef extends LatLng {
  id: string;
}
export interface RiderPos extends LatLng {
  rider_id: string;
}
export interface EtaData {
  stage: EtaStage;
  p50_real_s: number;
  p90_real_s: number;
  source: EtaSource;
  at: string;
}
export interface HistoryItem {
  status: Status;
  at: string;
}
export interface StatusData {
  status: Status;
  at: string;
}
export interface PositionData extends RiderPos {
  at: string;
}

export interface Snapshot {
  type: 'snapshot';
  order_id: string;
  last_event_id: string | null;
  status: Status;
  store: StoreRef | null;
  dest: LatLng | null;
  eta: EtaData | null;
  rider: RiderPos | null;
  history: HistoryItem[];
}

export type OrderEvent =
  | { type: 'event'; id: string; kind: 'status'; data: StatusData }
  | { type: 'event'; id: string; kind: 'eta'; data: EtaData }
  | { type: 'event'; id: string; kind: 'position'; data: PositionData };

export type ErrorCode = 'unauthorized' | 'forbidden' | 'bad_request' | 'timeout';

export type ServerMessage =
  | { type: 'auth_ok'; role: 'customer' | 'ops'; server_time?: number }
  | Snapshot
  | OrderEvent
  | { type: 'heartbeat'; ts: number }
  | { type: 'error'; code: string; message: string };

export type ConnectionState = 'connecting' | 'live' | 'reconnecting' | 'expired';

export interface City {
  center: [number, number];
  bounds: [[number, number], [number, number]];
  stores: Record<string, [number, number]>;
  sim_speed: number;
}

export interface CreatedOrder {
  order_id: string;
  status: Status;
  tracking_token: string;
}

// ---- ops ----
export type RiderStatus = 'idle' | 'assigned' | 'cooldown';
export interface OpsSummary {
  funnel: Record<Status, number>;
  pick_p95_sim_min: number | null;
  pick_samples: number;
  orders_last_5m: number;
  riders: Record<RiderStatus, number>;
  sim_speed: number;
  generated_at: string;
}
export interface OpsRider {
  id: string;
  lat: number;
  lng: number;
  status: RiderStatus;
  order_id: string | null;
}
export interface HeatCell {
  cell: string;
  supply: number;
  demand: number;
  boundary: [number, number][];
}
export interface LateOrder {
  order_id: string;
  status: Status;
  store_id: string;
  elapsed_sim_min: number;
  promised_p90_sim_min: number;
  over_by_sim_min: number;
}
