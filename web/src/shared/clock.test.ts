import { afterEach, describe, expect, it } from 'vitest';
import { clockOffsetMs, serverNow, setServerTime } from './clock';
import { remainingEta } from './eta';

afterEach(() => setServerTime(Date.now() / 1000));

describe('server clock offset', () => {
  it('learns the offset from the server time', () => {
    setServerTime(1_000, 500_000); // server says t=1000 s while the client reads 500 s
    expect(clockOffsetMs()).toBe(500_000);
  });

  it('a client clock that is 2 minutes behind still counts the ETA down', () => {
    const serverAtMs = Date.parse('2026-01-01T00:00:00Z');
    const clientNow = serverAtMs - 120_000; // the browser thinks it is 2 minutes earlier
    setServerTime(serverAtMs / 1000, clientNow);
    const eta = { stage: 'assigned', p50_real_s: 100, p90_real_s: 160, source: 'model', at: '2026-01-01T00:00:00Z' } as const;
    // 30 real seconds later on BOTH clocks
    const nowOnClient = clientNow + 30_000;
    const corrected = nowOnClient + clockOffsetMs();
    expect(Math.round(remainingEta(eta, corrected).p50)).toBe(70);
    // without the correction the countdown would be frozen at the full 100 s
    expect(remainingEta(eta, nowOnClient).p50).toBe(100);
  });

  it('serverNow() is the local time shifted by the learned offset', () => {
    setServerTime(Date.now() / 1000 + 60);
    expect(Math.abs(serverNow() - (Date.now() + 60_000))).toBeLessThan(50);
  });
});
