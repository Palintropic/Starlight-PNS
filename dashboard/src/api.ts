import type { Decision, DecisionMap, DecisionValue, Turn } from './types';

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json() as Promise<T>;
}

export const fetchTurns = (): Promise<Turn[]> =>
  fetch('/api/review/turns').then((res) => json(res));

export const fetchDecisions = (): Promise<DecisionMap> =>
  fetch('/api/review/decisions').then((res) => json(res));

export const submitDecision = (
  turn: Turn,
  decision: DecisionValue,
  note?: string,
): Promise<Decision> =>
  fetch('/api/review/decision', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      session_id: turn.session_id,
      turn: turn.turn,
      character: turn.character,
      decision,
      note: note ?? null,
    }),
  }).then((res) => json(res));
