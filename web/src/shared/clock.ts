/** Offset between the server clock and this browser's clock, learned from the server's `auth_ok`.
 *  ETA events carry the SERVER's timestamp, so a countdown measured against an unadjusted local
 *  clock freezes (clock behind) or runs short (clock ahead). Network latency (<100 ms) is ignored. */
let offsetMs = 0;

export function setServerTime(serverSeconds: number, clientNowMs: number = Date.now()): void {
  offsetMs = serverSeconds * 1000 - clientNowMs;
}

export function clockOffsetMs(): number {
  return offsetMs;
}

/** `Date.now()` corrected to server time. */
export function serverNow(): number {
  return Date.now() + offsetMs;
}
