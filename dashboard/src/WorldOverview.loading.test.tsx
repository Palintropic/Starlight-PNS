// dashboard/src/WorldOverview.loading.test.tsx — how the World tab loads and polls.
//
// Pins three promises the component makes in loadOverview: a late response to an older
// request never overwrites a newer one; a failed poll keeps the last good view and says it may
// be stale; world_not_open re-reads which worlds are open instead of showing an error.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen } from '@testing-library/react';
import WorldOverview from './WorldOverview';
import * as api from './api';
import type { OverviewEvent, WorldOverview as WorldOverviewData } from './api';

const POLL_MS = 5000;

const said = (seq: number, text: string): OverviewEvent => ({
  seq,
  event_id: `e${seq}`,
  type: 'dialogue.spoken',
  at: '2026-10-03T21:40:00',
  scope: 'location',
  actor: 'mizuki',
  participants: ['mizuki'],
  location_id: 'mizuki_home_room',
  channel_id: null,
  payload: { text },
});

const overview = (text: string, revision = 1): WorldOverviewData => ({
  world_id: 'yoake-mae',
  clock: '2026-10-03T21:50:00',
  revision,
  autonomy: null,
  residents: [
    {
      id: 'mizuki',
      name: '晓山瑞希',
      unit: '25ji',
      location_id: 'mizuki_home_room',
      activity: { kind: 'idle', since: '2026-10-03T21:00:00' },
      availability: 'available',
      channels: [],
    },
  ],
  locations: [
    { id: 'tokyo', name: '东京', parent_id: null },
    { id: 'mizuki_home_room', name: '瑞希的房间', parent_id: 'tokyo' },
  ],
  channels: [],
  events: [said(revision, text)],
  total_events: 1,
  first_event_at: '2026-10-03T21:40:00',
  speech_occupancy_checked_from: 0,
});

const openWorlds = (...ids: string[]) => ({
  worlds: ids.map((world_id) => ({ world_id, owned: true }) as api.PersistentWorldStatus),
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

const poll = () =>
  act(async () => {
    await vi.advanceTimersByTimeAsync(POLL_MS);
  });

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible');
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('World tab loading', () => {
  it('ignores a late response to an older request', async () => {
    vi.spyOn(api, 'fetchPersistentWorlds').mockResolvedValue(openWorlds('yoake-mae'));
    const slow = deferred<WorldOverviewData>();
    vi.spyOn(api, 'fetchWorldOverview')
      .mockReturnValueOnce(slow.promise)
      .mockResolvedValueOnce(overview('新的一句', 2))
      .mockResolvedValue(overview('新的一句', 2));

    render(<WorldOverview />);
    // Let the first (slow) request go out and the poll timer start, then poll once.
    await vi.waitFor(() => expect(api.fetchWorldOverview).toHaveBeenCalledTimes(1));
    await poll();
    await screen.findByText('新的一句');

    await act(async () => {
      slow.resolve(overview('旧的一句', 1));
      await slow.promise;
    });

    expect(screen.getByText('新的一句')).toBeTruthy();
    expect(screen.queryByText('旧的一句')).toBeNull();
  });

  it('keeps the last good view when a poll fails, and says it may be stale', async () => {
    vi.spyOn(api, 'fetchPersistentWorlds').mockResolvedValue(openWorlds('yoake-mae'));
    vi.spyOn(api, 'fetchWorldOverview')
      .mockResolvedValueOnce(overview('先前的一句'))
      .mockRejectedValue(new api.ApiError('世界正忙', 409, 'world_busy'));

    render(<WorldOverview />);
    await screen.findByText('先前的一句');

    await poll();

    expect(screen.getByText('先前的一句')).toBeTruthy();
    const note = await screen.findByRole('status');
    expect(note.textContent).toContain('这一页可能不是最新的');
    expect(note.textContent).toContain('世界正忙');
  });

  it('re-reads the open worlds when the world was closed elsewhere', async () => {
    const worlds = vi
      .spyOn(api, 'fetchPersistentWorlds')
      .mockResolvedValueOnce(openWorlds('yoake-mae'))
      .mockResolvedValue(openWorlds());
    const fetchOverview = vi
      .spyOn(api, 'fetchWorldOverview')
      .mockRejectedValue(new api.ApiError('世界没开', 409, 'world_not_open'));

    render(<WorldOverview />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });

    expect(fetchOverview).toHaveBeenCalledTimes(1);
    expect(worlds).toHaveBeenCalledTimes(2);
    // world_not_open is not shown as an error: the tab just follows the new set of open worlds.
    expect(screen.queryByText(/世界没开/)).toBeNull();

    // With no open world left, polling stops asking for that world's overview.
    await poll();
    expect(fetchOverview).toHaveBeenCalledTimes(1);
  });
});
