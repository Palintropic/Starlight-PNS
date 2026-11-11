import { afterEach, describe, expect, it, vi } from 'vitest';
import { fetchWorldOverview, keepWellFormedEvents } from './api';

const good = {
  seq: 7,
  event_id: 'e7',
  type: 'dialogue.spoken',
  at: '2026-10-31T01:30:00',
  scope: 'location',
  actor: 'mizuki',
  participants: ['mizuki', 'ena'],
  location_id: 'mizuki_room',
  channel_id: null,
  payload: { text: '喵' },
};

describe('keepWellFormedEvents', () => {
  it('keeps events that match the contract', () => {
    expect(keepWellFormedEvents([good])).toEqual([good]);
  });

  it('accepts null actor, location and channel', () => {
    const e = { ...good, actor: null, location_id: null, channel_id: 'nightcord' };
    expect(keepWellFormedEvents([e])).toEqual([e]);
  });

  it.each([
    ['missing at', { ...good, at: undefined }],
    ['participants not an array', { ...good, participants: 'mizuki' }],
    ['participants with a non-string', { ...good, participants: ['mizuki', 3] }],
    ['payload null', { ...good, payload: null }],
    ['payload an array', { ...good, payload: [] }],
    ['seq not a number', { ...good, seq: '7' }],
    ['seq NaN', { ...good, seq: Number.NaN }],
    ['actor a number', { ...good, actor: 1 }],
    ['location_id missing', { ...good, location_id: undefined }],
  ])('drops an event with %s and keeps the rest', (_label, bad) => {
    expect(keepWellFormedEvents([bad, good])).toEqual([good]);
  });

  it('drops non-object entries', () => {
    expect(keepWellFormedEvents([null, 'x', 1, [], good])).toEqual([good]);
  });

  it('returns an empty list when events is not an array', () => {
    expect(keepWellFormedEvents(undefined)).toEqual([]);
    expect(keepWellFormedEvents({ 0: good })).toEqual([]);
  });
});

describe('fetchWorldOverview', () => {
  afterEach(() => vi.restoreAllMocks());

  it('filters malformed events out of the response', async () => {
    const body = {
      world_id: 'yoake-mae',
      clock: '2026-10-31T01:30:00',
      revision: 1,
      autonomy: null,
      residents: [],
      locations: [],
      channels: [],
      events: [{ ...good, payload: null }, good],
      total_events: 2,
      first_event_at: null,
      speech_occupancy_checked_from: null,
    };
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    const data = await fetchWorldOverview('yoake-mae');
    expect(data.events).toEqual([good]);
    expect(data.total_events).toBe(2);
  });
});
