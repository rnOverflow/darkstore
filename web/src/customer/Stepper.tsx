import { forwardRank, type OrderState } from '../shared/orderReducer';
import { STEP_LABELS } from '../shared/tokens';
import { FORWARD_STATUSES, type ForwardStatus } from '../shared/types';

export type StepState = 'done' | 'active' | 'todo';

/** State of one stepper step for the order. Pure. */
export function stepState(order: Pick<OrderState, 'status' | 'furthest'>, step: ForwardStatus): StepState {
  const rank = forwardRank(step);
  if (order.status === 'delivered') return 'done';
  if (order.status === 'cancelled' || order.status === 'rejected') {
    return rank <= forwardRank(order.furthest) ? 'done' : 'todo';
  }
  const cur = forwardRank(order.status);
  return rank < cur ? 'done' : rank === cur ? 'active' : 'todo';
}

export function Stepper({ order }: { order: OrderState }) {
  const failed = order.status === 'cancelled' || order.status === 'rejected';
  return (
    <ol className={`stepper${failed ? ' stepper-failed' : ''}`} data-testid="stepper">
      {FORWARD_STATUSES.map((s) => {
        const state = stepState(order, s);
        return (
          <li key={s} className={`step step-${state}`} data-testid={`step-${s}`} data-state={state}>
            <span className="step-dot" aria-hidden="true" />
            <span className="step-label">{STEP_LABELS[s]}</span>
          </li>
        );
      })}
    </ol>
  );
}
