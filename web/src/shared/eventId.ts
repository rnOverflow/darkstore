export interface StreamId {
  ms: number;
  seq: number;
}

/** Parse a Redis stream id like "1731000000000-0". Returns null if malformed. */
export function parseStreamId(id: string): StreamId | null {
  const m = /^(\d+)-(\d+)$/.exec(id);
  if (!m) return null;
  return { ms: Number(m[1]), seq: Number(m[2]) };
}

/** Compare as (ms, seq) integer pairs. Returns <0, 0, >0. Throws on malformed ids. */
export function compareStreamIds(a: string, b: string): number {
  const pa = parseStreamId(a);
  const pb = parseStreamId(b);
  if (!pa || !pb) throw new Error(`malformed stream id: ${!pa ? a : b}`);
  return pa.ms !== pb.ms ? pa.ms - pb.ms : pa.seq - pb.seq;
}

/** True when `id` is well formed and strictly newer than `last` (null = nothing applied yet). */
export function isNewerId(id: string, last: string | null): boolean {
  if (parseStreamId(id) === null) return false;
  if (last === null || parseStreamId(last) === null) return true;
  return compareStreamIds(id, last) > 0;
}

/** The larger of two optional ids. */
export function maxId(a: string | null, b: string | null): string | null {
  if (a === null) return b;
  if (b === null) return a;
  if (!parseStreamId(a)) return b;
  if (!parseStreamId(b)) return a;
  return compareStreamIds(a, b) >= 0 ? a : b;
}
