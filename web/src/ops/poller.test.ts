import { beforeEach, describe, expect, it, vi } from 'vitest';

const calls = { n: 0 };
let failWith: number | null = null;
vi.mock('../shared/api', () => {
  class ApiError extends Error {
    constructor(public status: number) { super(`HTTP ${status}`); }
  }
  const respond = async () => {
    calls.n += 1;
    if (failWith) throw new ApiError(failWith);
    return {};
  };
  return { ApiError, fetchCity: async () => ({}), fetchOpsSummary: respond, fetchOpsRiders: respond,
           fetchOpsHeatmap: respond, fetchOpsLate: respond };
});

import { startPolling } from './poller';
import { useOpsStore } from './store';

beforeEach(() => { calls.n = 0; failWith = null; useOpsStore.setState({ expired: false }); });

describe('ops polling', () => {
  it.each([401, 403])('HTTP %i ends the session and stops polling', async (status) => {
    failWith = status;
    const stop = startPolling('t');
    await new Promise((r) => setTimeout(r, 30));
    const seen = calls.n;
    expect(useOpsStore.getState().expired).toBe(true);
    await new Promise((r) => setTimeout(r, 60));
    expect(calls.n).toBe(seen);               // no retry loop
    stop();
  });
});
