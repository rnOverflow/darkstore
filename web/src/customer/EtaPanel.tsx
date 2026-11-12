import { formatEtaRange, formatEtaText } from '../shared/eta';
import type { OrderState } from '../shared/orderReducer';
import { useNow } from '../shared/useNow';

export function EtaPanel({ order }: { order: OrderState }) {
  const now = useNow(1000);
  const { eta, status } = order;
  let text: string;
  let range = '—';
  if (status === 'delivered') text = 'Delivered';
  else if (status === 'cancelled' || status === 'rejected') text = 'No ETA';
  else if (!eta) text = 'Estimating…';
  else {
    text = formatEtaText(eta, now);
    range = formatEtaRange(eta, now);
  }
  return (
    <div className="eta">
      <div className="eta-text" data-testid="eta">
        {text}
      </div>
      <div className="eta-meta">
        <span data-testid="eta-range">{range}</span>
        {eta && status !== 'delivered' && (
          <small className="eta-source" data-testid="eta-source">
            {eta.source}
          </small>
        )}
      </div>
    </div>
  );
}
