import type {
  City,
  CreatedOrder,
  HeatCell,
  LateOrder,
  OpsRider,
  OpsSummary,
} from './types';

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, init);
  if (!res.ok) throw new ApiError(res.status, `${init?.method ?? 'GET'} ${path} -> ${res.status}`);
  return (await res.json()) as T;
}

export const fetchCity = (): Promise<City> => request<City>('/api/city');

export interface NewOrder {
  customer_id: string;
  store_id: string;
  items: { sku: string; qty: number }[];
  lat: number;
  lng: number;
}

export const createOrder = (body: NewOrder): Promise<CreatedOrder> =>
  request<CreatedOrder>('/api/orders', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });

function ops<T>(path: string, token: string): Promise<T> {
  return request<T>(`/api/ops/${path}`, { headers: { authorization: `Bearer ${token}` } });
}

export const fetchOpsSummary = (t: string): Promise<OpsSummary> => ops<OpsSummary>('summary', t);
export const fetchOpsRiders = async (t: string): Promise<OpsRider[]> =>
  (await ops<{ riders: OpsRider[] }>('riders', t)).riders;
export const fetchOpsHeatmap = async (t: string): Promise<HeatCell[]> =>
  (await ops<{ cells: HeatCell[] }>('heatmap', t)).cells;
export const fetchOpsLate = async (t: string): Promise<LateOrder[]> =>
  (await ops<{ late: LateOrder[] }>('late', t)).late;
