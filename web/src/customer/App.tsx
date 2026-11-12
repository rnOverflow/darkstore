import { useEffect } from 'react';
import { STATUS_LABELS } from '../shared/tokens';
import { EtaPanel } from './EtaPanel';
import { MapView } from './MapView';
import { placeDemoOrder, readTrackingFromUrl } from './placeOrder';
import { Stepper } from './Stepper';
import { useCustomerStore } from './store';
import { startTracking } from './tracker';

export function App() {
  const order = useCustomerStore((s) => s.order);
  const conn = useCustomerStore((s) => s.conn);
  const placing = useCustomerStore((s) => s.placing);
  const error = useCustomerStore((s) => s.error);

  useEffect(() => {
    const t = readTrackingFromUrl(location.hash);
    if (t) startTracking(t.orderId, t.token);
  }, []);

  const failed = order && (order.status === 'cancelled' || order.status === 'rejected');

  return (
    <div className="layout">
      <aside className="panel">
        <h1>DarkStore</h1>
        <button
          type="button"
          className="primary"
          data-testid="place-order"
          disabled={placing}
          onClick={() => void placeDemoOrder()}
        >
          {placing ? 'Placing…' : 'Place demo order'}
        </button>
        {error && (
          <p className="error" role="alert" data-testid="place-error">
            {error}
          </p>
        )}
        {order ? (
          <>
            <div className="row">
              <span className="muted">Order</span>
              <code data-testid="order-id">{order.orderId}</code>
            </div>
            <div className="row">
              <span className="muted">Status</span>
              <strong data-testid="order-status">{order.status}</strong>
              <span
                className={`conn conn-${conn}`}
                data-testid="connection-state"
                title={conn === 'expired' ? 'Session expired' : undefined}
              >
                {conn}
              </span>
            </div>
            {conn === 'expired' && (
              <p className="error" role="alert" data-testid="session-expired">
                Session expired. Place a new order to keep tracking.
              </p>
            )}
            {failed && (
              <p className="banner-failed" role="alert" data-testid="order-banner">
                Order {STATUS_LABELS[order.status].toLowerCase()}
              </p>
            )}
            <EtaPanel order={order} />
            <Stepper order={order} />
          </>
        ) : (
          <p className="muted">Place a demo order to start live tracking.</p>
        )}
      </aside>
      <main className="map-wrap">
        <MapView />
      </main>
    </div>
  );
}
