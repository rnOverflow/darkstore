import { STATUS_LABELS } from '../shared/tokens';
import { STATUSES, type Status } from '../shared/types';

const BAR_H = 20;
const GAP = 8;
const LABEL_W = 130;
const VALUE_W = 44;
const WIDTH = 360;

const COLORS: Partial<Record<Status, string>> = { cancelled: '#e03131', rejected: '#e03131', delivered: '#2f9e44' };

/** Horizontal bar funnel in plain SVG. */
export function Funnel({ funnel }: { funnel: Record<Status, number> }) {
  const max = Math.max(1, ...STATUSES.map((s) => funnel[s] ?? 0));
  const barMax = WIDTH - LABEL_W - VALUE_W;
  const height = STATUSES.length * (BAR_H + GAP);
  return (
    <svg
      className="funnel"
      viewBox={`0 0 ${WIDTH} ${height}`}
      role="img"
      aria-label="Order funnel"
      data-testid="ops-funnel"
    >
      {STATUSES.map((s, i) => {
        const n = funnel[s] ?? 0;
        const y = i * (BAR_H + GAP);
        return (
          <g key={s} data-status={s} data-count={n}>
            <text x={0} y={y + BAR_H * 0.72} className="funnel-label">
              {STATUS_LABELS[s]}
            </text>
            <rect
              x={LABEL_W}
              y={y}
              width={Math.max(n > 0 ? 2 : 0, (n / max) * barMax)}
              height={BAR_H}
              rx={3}
              fill={COLORS[s] ?? '#1c7ed6'}
            />
            <text x={WIDTH} y={y + BAR_H * 0.72} textAnchor="end" className="funnel-value">
              {n}
            </text>
          </g>
        );
      })}
    </svg>
  );
}
