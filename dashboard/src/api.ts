import type { Decision, DecisionMap, DecisionValue, Turn } from './types';
import type { FactsResponse, ScenesMap } from './world/types';

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new Error((body && body.detail) || `${res.status} ${res.statusText}`);
  }
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

// ─── World Editor ─────────────────────────────────────────────────────

export const fetchWorldScenes = (): Promise<ScenesMap> =>
  fetch('/api/world/scenes').then((res) => json(res));

export const saveWorldScenes = (scenes: ScenesMap): Promise<ScenesMap> =>
  fetch('/api/world/scenes', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(scenes),
  }).then((res) => json(res));

export const fetchWorldScenesSource = (): Promise<{ source: string }> =>
  fetch('/api/world/scenes/source').then((res) => json(res));

export const saveWorldScenesSource = (source: string): Promise<{ source: string }> =>
  fetch('/api/world/scenes/source', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ source }),
  }).then((res) => json(res));

export const fetchWorldFacts = (): Promise<FactsResponse> =>
  fetch('/api/world/facts').then((res) => json(res));

export const saveWorldFacts = (facts: Record<string, string>): Promise<FactsResponse> =>
  fetch('/api/world/facts', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ facts }),
  }).then((res) => json(res));

export const fetchWorldFactsSource = (): Promise<{ source: string }> =>
  fetch('/api/world/facts/source').then((res) => json(res));

export const saveWorldFactsSource = (source: string): Promise<{ source: string }> =>
  fetch('/api/world/facts/source', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ source }),
  }).then((res) => json(res));

// ─── Setup / Config ────────────────────────────────────────────────────

export interface ConfigStatus {
  has_key: boolean;
  model: string;
  generator_model: string;
  evaluator_model: string;
  api_format: string;
  default_scene: string;
}

export interface ProviderOption {
  name: string;
  models: string[];
}

export const fetchConfig = (): Promise<ConfigStatus> =>
  fetch('/api/config').then((res) => json(res));

export const fetchConfigProviders = (): Promise<Record<string, ProviderOption>> =>
  fetch('/api/config/providers').then((res) => json(res));

export const submitConfig = (
  providerKey: string,
  generatorModel: string,
  evaluatorModel: string,
  apiKey: string,
): Promise<{ configured: boolean }> =>
  fetch('/api/config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      provider_key: providerKey,
      model: generatorModel,
      generator_model: generatorModel,
      evaluator_model: evaluatorModel,
      api_key: apiKey,
    }),
  }).then((res) => json(res));
