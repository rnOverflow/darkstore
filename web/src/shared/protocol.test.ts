import { describe, expect, it } from 'vitest';
import { parseServerMessage } from './protocol';

const frame = (data: unknown) => JSON.stringify({ type: 'event', id: '5-0', kind: 'meta', data });

describe('parseServerMessage: meta event', () => {
  it('accepts the order store and destination', () => {
    const m = parseServerMessage(frame({ store: { id: 's0', lat: 1, lng: 2 }, dest: { lat: 3, lng: 4 } }));
    expect(m).not.toBeNull();
    expect(m).toMatchObject({ type: 'event', kind: 'meta', id: '5-0' });
  });

  it('rejects a meta event without usable coordinates', () => {
    expect(parseServerMessage(frame({ store: { id: 's0', lat: 'x', lng: 2 }, dest: { lat: 3, lng: 4 } }))).toBeNull();
    expect(parseServerMessage(frame({ store: { id: 's0', lat: 1, lng: 2 } }))).toBeNull();
    expect(parseServerMessage(frame({}))).toBeNull();
  });
});
