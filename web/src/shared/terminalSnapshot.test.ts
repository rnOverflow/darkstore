import { describe, expect, it } from 'vitest';
import { applySnapshot, initialOrderState } from './orderReducer';
import type { Snapshot } from './types';

const eta = { stage: 'out_for_delivery', p50_real_s: 30, p90_real_s: 50, source: 'model', at: '2026-01-01T00:00:00Z' } as const;
const snap = (status: Snapshot['status']): Snapshot => ({
  type: 'snapshot', order_id: 'o1', last_event_id: '5-0', status, store: null, dest: null, eta, rider: null, history: [],
});

describe('snapshot of an order that has finished', () => {
  it('carries no ETA (nothing is arriving any more)', () => {
    for (const s of ['delivered', 'cancelled', 'rejected'] as const) {
      expect(applySnapshot(initialOrderState('o1'), snap(s)).eta).toBeNull();
    }
  });
  it('keeps the ETA for an order still in flight', () => {
    expect(applySnapshot(initialOrderState('o1'), snap('out_for_delivery')).eta).toEqual(eta);
  });
});
