// dashboard/src/PersistentWorlds.held.test.tsx — 作息内容待决的世界（CONTENT-4）
//
// 两件事：一个打开着却被搁置的世界不能看起来只是"已停"，要说清是内容待决、
// 是谁、为什么；服务器拒绝恢复（409 content_not_adopted）时，它给的原因要原样
// 到达页面，而不是被一句笼统的失败盖掉。
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, screen } from '@testing-library/react';
import PersistentWorlds from './PersistentWorlds';
import { OPERATOR, renderAs } from './testPrincipal';
import * as api from './api';
import type { PersistentWorldStatus } from './api';

const world = (overrides: Partial<PersistentWorldStatus> = {}): PersistentWorldStatus => ({
  world_id: 'yoake-mae',
  session_id: 'yoake-mae_session',
  revision: 3,
  durable_revision: 3,
  dirty: false,
  closed: false,
  clean: false,
  owned: true,
  owner: null,
  recovered_from: null,
  last_saved_at: null,
  last_checkpoint_reason: null,
  durable: true,
  directory_synced: true,
  last_error: null,
  error: null,
  residue: [],
  running: false,
  stop_reason: 'content_pending',
  held: null,
  clock: '2026-10-03T02:30:00',
  archive_path: '/tmp/worlds/yoake-mae/world.json',
  boundaries_since_checkpoint: 0,
  policy: { every_boundaries: 1, min_interval_seconds: 60, on_close: true },
  quiet_time_events: null,
  archive: null,
  autonomy: null,
  ...overrides,
});

function stubMountFetches() {
  vi.spyOn(api, 'fetchWorldScenes').mockResolvedValue({});
  vi.spyOn(api, 'fetchReloadStatus').mockResolvedValue({
    reloading: false,
    stop_timeout: 5,
    accepting_sessions: true,
    live_sessions: [],
    registry: null,
    last_reload: null,
  });
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('作息内容待决的世界', () => {
  it('搁置的世界标明内容待决和原因，不只是"已停"', async () => {
    stubMountFetches();
    vi.spyOn(api, 'fetchPersistentWorlds').mockResolvedValue({
      worlds: [
        world({
          held: {
            reason: 'content_pending',
            subjects: [
              {
                subject: 'rhythm:ena',
                character_id: 'ena',
                reason: 'pending',
                conflict_id: 'rhythm:ena@aaaa#0',
                conflict_status: 'pending',
              },
              {
                subject: 'rhythm:mafuyu',
                character_id: 'mafuyu',
                reason: 'adopted_absent',
                conflict_id: null,
                conflict_status: null,
              },
            ],
          },
        }),
      ],
    });

    renderAs(OPERATOR, <PersistentWorlds />);
    const label = await screen.findByText(/搁置：内容待决/);
    expect(label.textContent).toContain('rhythm:ena（pending）');
    expect(label.textContent).toContain('rhythm:mafuyu（adopted_absent）');
    expect(screen.queryByText('已停：content_pending')).toBeNull();
  });

  it('同样停着、但没有搁置的世界照旧显示"已停"', async () => {
    stubMountFetches();
    vi.spyOn(api, 'fetchPersistentWorlds').mockResolvedValue({
      worlds: [world({ stop_reason: 'operator' })],
    });

    renderAs(OPERATOR, <PersistentWorlds />);
    expect(await screen.findByText('已停：operator')).toBeTruthy();
    expect(screen.queryByText(/搁置/)).toBeNull();
  });

  it('恢复被内容门拒绝时，服务器给的原因原样出现在页面上', async () => {
    stubMountFetches();
    vi.spyOn(api, 'fetchPersistentWorlds').mockResolvedValue({
      worlds: [world({ owned: false, closed: null, running: null, stop_reason: null })],
    });
    const message =
      "世界 'yoake-mae' 的作息内容待决，未恢复运行：rhythm:ena（pending）。" +
      '冲突已经存盘，世界已关闭；用内容维护脚本记下决定后再恢复';
    vi.spyOn(api, 'restorePersistentWorld').mockRejectedValue(
      new api.ApiError(message, 409, 'content_not_adopted'),
    );

    renderAs(OPERATOR, <PersistentWorlds />);
    const restore = await screen.findByRole('button', { name: '恢复' });
    await act(async () => {
      restore.click();
    });
    expect(await screen.findByText(message)).toBeTruthy();
  });
});
