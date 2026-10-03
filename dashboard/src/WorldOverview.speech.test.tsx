// dashboard/src/WorldOverview.speech.test.tsx — the World tab shows where each line was said.
//
// speechSetting.test.ts pins the classification; this file pins that the timeline actually
// renders it: one line per setting, with the label and the company the record supports.
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import WorldOverview from './WorldOverview';
import * as api from './api';
import type { OverviewEvent, OverviewResident, WorldOverview as WorldOverviewData } from './api';

const resident = (id: string, name: string, location_id: string, channels: string[] = []): OverviewResident => ({
  id,
  name,
  unit: '25ji',
  location_id,
  activity: { kind: 'idle', since: '2026-10-03T21:00:00' },
  availability: 'available',
  channels,
});

let seq = 0;
const line = (overrides: Partial<OverviewEvent>): OverviewEvent => ({
  seq: ++seq,
  event_id: `e${seq}`,
  type: 'dialogue.spoken',
  at: '2026-10-03T21:40:00',
  scope: 'location',
  actor: null,
  participants: [],
  location_id: null,
  channel_id: null,
  payload: {},
  ...overrides,
});

const overview = (events: OverviewEvent[]): WorldOverviewData => ({
  world_id: 'yoake-mae',
  clock: '2026-10-03T21:50:00',
  revision: 7,
  autonomy: null,
  residents: [
    resident('mizuki', '晓山瑞希', 'mizuki_home_room'),
    resident('kanade', '宵崎奏', 'kanade_home'),
    resident('mafuyu', '朝比奈真冬', 'kanade_home'),
    resident('ena', '东云绘名', 'ena_home_studio', ['nightcord']),
  ],
  locations: [
    { id: 'tokyo', name: '东京', parent_id: null },
    { id: 'mizuki_home', name: '瑞希家', parent_id: 'tokyo' },
    { id: 'mizuki_home_room', name: '瑞希的房间', parent_id: 'mizuki_home' },
    { id: 'kanade_home', name: '宵崎家', parent_id: 'tokyo' },
    { id: 'ena_home', name: '东云家', parent_id: 'tokyo' },
    { id: 'ena_home_studio', name: '绘名的画室', parent_id: 'ena_home' },
  ],
  channels: [{ id: 'nightcord', name: 'Nightcord' }],
  events,
  total_events: events.length,
  first_event_at: events[0]?.at ?? null,
});

const timelineRow = (quote: string) => screen.getByText(quote).closest('li') as HTMLElement;

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('World tab speech settings', () => {
  it('labels solo, face-to-face and online lines differently', async () => {
    vi.spyOn(api, 'fetchPersistentWorlds').mockResolvedValue({
      worlds: [{ world_id: 'yoake-mae', owned: true } as api.PersistentWorldStatus],
    });
    vi.spyOn(api, 'fetchWorldOverview').mockResolvedValue(
      overview([
        line({
          actor: 'mizuki',
          participants: ['mizuki'],
          location_id: 'mizuki_home_room',
          payload: { text: '分镜还差两格。' },
        }),
        line({
          actor: 'kanade',
          participants: ['kanade', 'mafuyu'],
          location_id: 'kanade_home',
          payload: { text: '要不要先吃点东西？' },
        }),
        line({
          type: 'message.sent',
          scope: 'channel',
          actor: 'ena',
          participants: ['ena', 'mizuki'],
          location_id: 'ena_home_studio',
          channel_id: 'nightcord',
          payload: { text: '我先上线了' },
        }),
        line({
          actor: 'mizuki',
          participants: [],
          location_id: 'mizuki_home_room',
          payload: { text: '没有在场记录的一句' },
        }),
      ]),
    );

    render(<WorldOverview />);
    await screen.findByText('分镜还差两格。');

    const solo = timelineRow('分镜还差两格。');
    expect(solo.querySelector('.wo-speech-tag')?.textContent).toBe('独自');
    expect(solo.textContent).toContain('在 瑞希的房间 说');

    const together = timelineRow('要不要先吃点东西？');
    expect(together.querySelector('.wo-speech-tag')?.textContent).toBe('当面');
    expect(together.textContent).toContain('在场：朝比奈真冬');
    expect(together.textContent).not.toContain('宵崎奏、');

    const online = timelineRow('我先上线了');
    expect(online.querySelector('.wo-speech-tag')?.textContent).toBe('线上');
    expect(online.textContent).toContain('在 Nightcord 发了消息');
    expect(online.textContent).toContain('在线：晓山瑞希');

    const unrecorded = timelineRow('没有在场记录的一句');
    expect(unrecorded.querySelector('.wo-speech-tag')).toBeNull();
  });
});
