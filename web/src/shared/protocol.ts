import type { ServerMessage, Status } from './types';
import { STATUSES } from './types';

type Obj = Record<string, unknown>;

function isObj(v: unknown): v is Obj {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}
function isNum(v: unknown): v is number {
  return typeof v === 'number' && Number.isFinite(v);
}
export function isStatus(v: unknown): v is Status {
  return typeof v === 'string' && (STATUSES as readonly string[]).includes(v);
}

/** Parse a server frame. Returns null for anything malformed or unknown (never throws). */
export function parseServerMessage(raw: unknown): ServerMessage | null {
  if (typeof raw !== 'string') return null;
  let v: unknown;
  try {
    v = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!isObj(v) || typeof v.type !== 'string') return null;
  switch (v.type) {
    case 'auth_ok':
      return v.role === 'customer' || v.role === 'ops'
        ? { type: 'auth_ok', role: v.role, server_time: isNum(v.server_time) ? v.server_time : undefined }
        : null;
    case 'heartbeat':
      return { type: 'heartbeat', ts: isNum(v.ts) ? v.ts : 0 };
    case 'error':
      return {
        type: 'error',
        code: typeof v.code === 'string' ? v.code : 'unknown',
        message: typeof v.message === 'string' ? v.message : '',
      };
    case 'snapshot':
      return typeof v.order_id === 'string' && isStatus(v.status)
        ? (v as unknown as ServerMessage)
        : null;
    case 'event': {
      if (typeof v.id !== 'string' || !isObj(v.data)) return null;
      const d = v.data;
      if (v.kind === 'status' && isStatus(d.status)) return v as unknown as ServerMessage;
      if (v.kind === 'eta' && isNum(d.p50_real_s) && isNum(d.p90_real_s)) {
        return v as unknown as ServerMessage;
      }
      if (v.kind === 'position' && isNum(d.lat) && isNum(d.lng)) {
        return v as unknown as ServerMessage;
      }
      return null;
    }
    default:
      return null;
  }
}
