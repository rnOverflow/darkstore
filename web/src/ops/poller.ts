import {
  ApiError,
  fetchCity,
  fetchOpsHeatmap,
  fetchOpsLate,
  fetchOpsRiders,
  fetchOpsSummary,
} from '../shared/api';
import { useOpsStore } from './store';

export const POLL_MS = 2000;

/** Poll the four ops endpoints every 2 s. Stops for good on HTTP 401/403. Returns a stop function. */
export function startPolling(token: string): () => void {
  let stopped = false;
  let timer: ReturnType<typeof setTimeout> | null = null;
  const store = useOpsStore.getState();

  void fetchCity()
    .then((c) => store.setCity(c))
    .catch(() => store.setError('could not load city'));

  const tick = async (): Promise<void> => {
    try {
      const [summary, riders, heat, late] = await Promise.all([
        fetchOpsSummary(token),
        fetchOpsRiders(token),
        fetchOpsHeatmap(token),
        fetchOpsLate(token),
      ]);
      if (!stopped) useOpsStore.getState().setData({ summary, riders, heat, late });
    } catch (e) {
      if (stopped) return;
      if (e instanceof ApiError && (e.status === 401 || e.status === 403)) {
        stopped = true;
        useOpsStore.getState().setExpired();
        return;
      }
      if (!stopped) useOpsStore.getState().setError('connection problem, retrying');
    }
    if (!stopped) timer = setTimeout(() => void tick(), POLL_MS);
  };
  void tick();

  return () => {
    stopped = true;
    if (timer) clearTimeout(timer);
  };
}

export function tokenFromHash(hash: string): string | null {
  const t = new URLSearchParams(hash.replace(/^#/, '')).get('token');
  return t && t.length > 0 ? t : null;
}
