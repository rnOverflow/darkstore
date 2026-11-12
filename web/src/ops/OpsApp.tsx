import { useEffect, useState } from 'react';
import { RIDER_COLORS, STATUS_LABELS } from '../shared/tokens';
import { HEAT_DEMAND, HEAT_NEUTRAL, HEAT_SUPPLY } from '../shared/tokens';
import { Funnel } from './Funnel';
import { OpsMap } from './OpsMap';
import { startPolling, tokenFromHash } from './poller';
import { useOpsStore } from './store';

function Legend() {
  return (
    <div className="legend" data-testid="ops-legend">
      <div className="legend-title">Supply vs demand (per cell)</div>
      <div
        className="legend-bar"
        style={{ background: `linear-gradient(90deg, ${HEAT_DEMAND}, ${HEAT_NEUTRAL}, ${HEAT_SUPPLY})` }}
      />
      <div className="legend-labels">
        <span>demand &gt; supply</span>
        <span>balanced</span>
        <span>supply &gt; demand</span>
      </div>
      <div className="legend-riders">
        {(Object.keys(RIDER_COLORS) as (keyof typeof RIDER_COLORS)[]).map((k) => (
          <span key={k}>
            <i style={{ background: RIDER_COLORS[k] }} /> {k}
          </span>
        ))}
      </div>
    </div>
  );
}

export function OpsApp() {
  const [token] = useState(() => {
    const t = tokenFromHash(location.hash);
    if (t) history.replaceState(null, '', location.pathname + location.search); // drop it from history
    return t;
  });
  const summary = useOpsStore((s) => s.summary);
  const late = useOpsStore((s) => s.late);
  const expired = useOpsStore((s) => s.expired);
  const error = useOpsStore((s) => s.error);

  useEffect(() => {
    if (!token) return;
    return startPolling(token);
  }, [token]);

  const riders = summary?.riders;
  const total = riders ? riders.idle + riders.assigned + riders.cooldown : 0;

  return (
    <div className="ops-layout">
      <div className="ops-map-wrap">
        <OpsMap />
        <Legend />
      </div>
      <aside className="ops-side">
        <h1>DarkStore Ops</h1>
        {!token && (
          <p className="error" role="alert" data-testid="ops-no-token">
            Missing token. Open /ops/#token=&lt;ops token&gt;
          </p>
        )}
        {expired && (
          <p className="error" role="alert" data-testid="ops-expired">
            Session expired
          </p>
        )}
        {error && !expired && <p className="warn">{error}</p>}

        <h2>p95 pick time</h2>
        <div className="big" data-testid="ops-pick-p95">
          {summary?.pick_p95_sim_min != null ? summary.pick_p95_sim_min.toFixed(1) : '—'}
          <small> sim min</small>
        </div>
        <div className="muted">{summary ? `${summary.pick_samples} samples` : ''}</div>

        <div className="stats">
          <div>
            <div className="stat-n" data-testid="ops-orders-5m">{summary?.orders_last_5m ?? '—'}</div>
            <div className="muted">orders last 5m</div>
          </div>
          <div
            data-testid="ops-riders-count"
            data-total={total}
            data-idle={riders?.idle ?? 0}
            data-assigned={riders?.assigned ?? 0}
            data-cooldown={riders?.cooldown ?? 0}
          >
            <div className="stat-n">{riders ? total : '—'}</div>
            <div className="muted">
              riders: {riders?.idle ?? 0} idle, {riders?.assigned ?? 0} assigned, {riders?.cooldown ?? 0} cooldown
            </div>
          </div>
        </div>

        <h2>Order funnel</h2>
        {summary ? <Funnel funnel={summary.funnel} /> : <p className="muted">Loading&hellip;</p>}

        <h2>Late orders</h2>
        <table className="late" data-testid="ops-late-table">
          <thead>
            <tr>
              <th>Order</th>
              <th>Status</th>
              <th>Store</th>
              <th>Elapsed</th>
              <th>Over by</th>
            </tr>
          </thead>
          <tbody>
            {late.length === 0 && (
              <tr>
                <td colSpan={5} className="muted">
                  No late orders
                </td>
              </tr>
            )}
            {late.map((o) => (
              <tr key={o.order_id} data-testid="ops-late-row">
                <td title={o.order_id}>{o.order_id.slice(0, 8)}</td>
                <td>{STATUS_LABELS[o.status]}</td>
                <td>{o.store_id}</td>
                <td>{o.elapsed_sim_min.toFixed(1)}m</td>
                <td className="over">+{o.over_by_sim_min.toFixed(1)}m</td>
              </tr>
            ))}
          </tbody>
        </table>
      </aside>
    </div>
  );
}
