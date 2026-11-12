import type { ForwardStatus, RiderStatus, Status } from './types';

export const STEP_LABELS: Record<ForwardStatus, string> = {
  created: 'Order placed',
  reserved: 'Confirmed (reserved)',
  picking: 'Picking',
  picked: 'Ready (picked)',
  assigned: 'Rider assigned',
  out_for_delivery: 'Out for delivery',
  delivered: 'Delivered',
};

export const STATUS_LABELS: Record<Status, string> = {
  ...STEP_LABELS,
  cancelled: 'Cancelled',
  rejected: 'Rejected',
};

export const RIDER_COLORS: Record<RiderStatus, string> = {
  idle: '#2f9e44',
  assigned: '#1c7ed6',
  cooldown: '#f08c00',
};

/** Diverging heatmap: supply surplus (blue) <-> neutral <-> demand surplus (orange/red). */
export const HEAT_SUPPLY = '#2b6cb0';
export const HEAT_NEUTRAL = '#f1f3f5';
export const HEAT_DEMAND = '#d9480f';

export const MAP_BG = '#e5e7eb';
export const OSM_TILES = 'https://tile.openstreetmap.org/{z}/{x}/{y}.png';
export const OSM_ATTRIBUTION =
  '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';
