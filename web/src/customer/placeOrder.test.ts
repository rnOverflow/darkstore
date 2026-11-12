import { afterEach, describe, expect, it, vi } from 'vitest';
import { readTrackingFromUrl, trackingUrl } from './placeOrder';

afterEach(() => vi.unstubAllGlobals());

describe('tracking link', () => {
  it('puts the order and token in the URL FRAGMENT, never the query string', () => {
    vi.stubGlobal('location', { pathname: '/' });
    const url = trackingUrl('order-1', 'tok.en');
    expect(url.startsWith('/#')).toBe(true);
    expect(url).not.toContain('?');
    expect(readTrackingFromUrl(url.slice(1))).toEqual({ orderId: 'order-1', token: 'tok.en' });
  });

  it('reads a fragment with or without the leading #, and rejects incomplete links', () => {
    expect(readTrackingFromUrl('#order=a&token=b')).toEqual({ orderId: 'a', token: 'b' });
    expect(readTrackingFromUrl('order=a&token=b')).toEqual({ orderId: 'a', token: 'b' });
    expect(readTrackingFromUrl('#order=a')).toBeNull();
    expect(readTrackingFromUrl('')).toBeNull();
  });
});
