import { createOrder, fetchCity, startSession } from '../shared/api';
import { buildDemoOrder } from './demoOrder';
import { startTracking } from './tracker';
import { useCustomerStore } from './store';

/** The token goes in the URL FRAGMENT: browsers never send it to the server, so it stays out of
 *  nginx access logs (which Loki ingests) and Referer headers. */
export function trackingUrl(orderId: string, token: string): string {
  const q = new URLSearchParams({ order: orderId, token });
  return `${location.pathname}#${q.toString()}`;
}

export function readTrackingFromUrl(hash: string): { orderId: string; token: string } | null {
  const q = new URLSearchParams(hash.replace(/^#/, ''));
  const orderId = q.get('order');
  const token = q.get('token');
  return orderId && token ? { orderId, token } : null;
}

export async function placeDemoOrder(): Promise<void> {
  const st = useCustomerStore.getState();
  if (st.placing) return;
  st.setPlacing(true);
  st.setError(null);
  try {
    const city = await fetchCity();
    const session = await startSession();
    const created = await createOrder(buildDemoOrder(city), session.token);
    history.pushState(null, '', trackingUrl(created.order_id, created.tracking_token));
    startTracking(created.order_id, created.tracking_token);
  } catch (e) {
    st.setError(e instanceof Error ? e.message : 'Could not place order');
  } finally {
    useCustomerStore.getState().setPlacing(false);
  }
}
