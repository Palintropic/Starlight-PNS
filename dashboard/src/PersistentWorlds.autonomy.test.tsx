// dashboard/src/PersistentWorlds.autonomy.test.tsx — 自动推进那对按钮（MVP-1）
//
// 这个文件只盯三件 lint / typecheck / build 都证明不了的事：
//
//   1. **按钮跟着服务器状态走，不跟着本地猜测走。** 一个 running=true 但驱动
//      停着的世界，给的必须是「开始自动推进」；反过来也一样。
//   2. **`stopping` 不许被显示成"已停止"。** 那一轮还在跑，还可能落地一次
//      提交 —— 说它停了就是一句会被事实拆穿的话，而这一页正是操作者判断
//      "现在还在不在花钱"的地方。
//   3. **新按钮沿用 WEB-1 那套时序保护。** 它们跟 checkpoint / close 共用
//      同一个 run()，所以"慢的刷新吞掉操作结果"这类 bug 不许因为多了两个
//      动作又长回来。
//
// 每个用例都用手动兑现的 promise，不靠计时器：竞态测试不该赌调度。
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen } from '@testing-library/react';
import PersistentWorlds from './PersistentWorlds';
import * as api from './api';
import type { PersistentWorldStatus, WorldDriverStatus } from './api';

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const driver = (
  world_id: string,
  state: 'running' | 'stopping' | 'stopped',
  overrides: Partial<WorldDriverStatus> = {},
): WorldDriverStatus => ({
  world_id,
  state,
  running: state === 'running',
  stopping: state === 'stopping',
  stopped: state === 'stopped',
  stop_reason: null,
  exit_reason: null,
  ticks: 3,
  failures: 0,
  consecutive_failures: 0,
  last_error: null,
  last_tick_at: '2026-08-23T02:10:00',
  last_tick: {
    failed: false,
    from_clock: '2026-08-23T02:00:00',
    to_clock: '2026-08-23T02:05:00',
    minutes: 5,
    due: 1,
    processed: 1,
    outcomes: { acted: 1 },
    checkpoint_revision: 4,
  },
  next_due_at: '2026-08-23T02:20:00',
  cadence: {
    tick_minutes: 5,
    interval_seconds: 30,
    stop_timeout_seconds: 10,
    max_activations_per_run: 200,
  },
  run_budget: { limit: 200, used: 3, remaining: 197 },
  world_actions: { committed: 3, cap: 100000, remaining: 99997 },
  ...overrides,
});

const world = (
  world_id: string,
  overrides: Partial<PersistentWorldStatus> = {},
): PersistentWorldStatus => ({
  world_id,
  session_id: `${world_id}_session`,
  revision: 4,
  durable_revision: 4,
  dirty: false,
  closed: false,
  clean: false,
  owned: true,
  owner: null,
  recovered_from: null,
  last_saved_at: null,
  last_checkpoint_reason: 'autonomy_tick',
  durable: true,
  directory_synced: true,
  last_error: null,
  error: null,
  residue: [],
  running: true,
  stop_reason: null,
  clock: '2026-08-23T02:05:00',
  archive_path: `/tmp/worlds/${world_id}/world.json`,
  boundaries_since_checkpoint: 0,
  policy: { every_boundaries: 1, min_interval_seconds: 60, on_close: true },
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

describe('自动推进的控制与状态', () => {
  it('开着但没人推的世界，给的是「开始自动推进」', async () => {
    stubMountFetches();
    vi.spyOn(api, 'fetchPersistentWorlds').mockResolvedValue({
      worlds: [world('alpha')],
    });
    render(<PersistentWorlds />);
    await screen.findByRole('button', { name: '开始自动推进' });
    expect(screen.queryByRole('button', { name: '停止自动推进' })).toBeNull();
    // P12 的「运行中」和驱动的「未启动」是两件事，两个都要看得见。
    expect(screen.getByText('运行中')).toBeTruthy();
    expect(screen.getByText('未启动')).toBeTruthy();
  });

  it('正在推的世界，给的是「停止自动推进」', async () => {
    stubMountFetches();
    vi.spyOn(api, 'fetchPersistentWorlds').mockResolvedValue({
      worlds: [world('alpha', { autonomy: driver('alpha', 'running') })],
    });
    render(<PersistentWorlds />);
    await screen.findByRole('button', { name: '停止自动推进' });
    expect(screen.queryByRole('button', { name: '开始自动推进' })).toBeNull();
    expect(screen.getByText('自动推进中')).toBeTruthy();
  });

  it('stopping 既不显示成已停止，也不给出「开始」按钮', async () => {
    stubMountFetches();
    vi.spyOn(api, 'fetchPersistentWorlds').mockResolvedValue({
      worlds: [world('alpha', { autonomy: driver('alpha', 'stopping') })],
    });
    render(<PersistentWorlds />);
    // 还没停干净 —— 这一轮仍然可能落地一次提交。
    await screen.findByText('正在停止…');
    expect(screen.queryByText('已停')).toBeNull();
    expect(screen.queryByRole('button', { name: '开始自动推进' })).toBeNull();
    expect(screen.getByRole('button', { name: '停止自动推进' })).toBeTruthy();
  });

  it('停止请求超时时，报的是"还没结束"，不是"已停止"', async () => {
    stubMountFetches();
    vi.spyOn(api, 'fetchPersistentWorlds').mockResolvedValue({
      worlds: [world('alpha', { autonomy: driver('alpha', 'running') })],
    });
    const pending = deferred<PersistentWorldStatus>();
    vi.spyOn(api, 'stopWorldAutonomy').mockReturnValue(pending.promise);

    render(<PersistentWorlds />);
    const button = await screen.findByRole('button', { name: '停止自动推进' });
    await act(async () => {
      button.click();
    });
    await act(async () => {
      pending.resolve(world('alpha', { autonomy: driver('alpha', 'stopping') }));
      await pending.promise;
    });
    await screen.findByText(
      '停止请求已发出，但当前这一轮还没结束——它仍然可能落地一次提交',
    );
  });

  it('停干净了才说"已停止"', async () => {
    stubMountFetches();
    vi.spyOn(api, 'fetchPersistentWorlds').mockResolvedValue({
      worlds: [world('alpha', { autonomy: driver('alpha', 'running') })],
    });
    const pending = deferred<PersistentWorldStatus>();
    vi.spyOn(api, 'stopWorldAutonomy').mockReturnValue(pending.promise);

    render(<PersistentWorlds />);
    const button = await screen.findByRole('button', { name: '停止自动推进' });
    await act(async () => {
      button.click();
    });
    await act(async () => {
      pending.resolve(world('alpha', { autonomy: driver('alpha', 'stopped') }));
      await pending.promise;
    });
    await screen.findByText('已停止自动推进（世界仍然开着，可以再启动）');
  });

  it('连点两下「开始自动推进」只发一次请求', async () => {
    stubMountFetches();
    vi.spyOn(api, 'fetchPersistentWorlds').mockResolvedValue({
      worlds: [world('alpha')],
    });
    const pending = deferred<PersistentWorldStatus>();
    const start = vi.spyOn(api, 'startWorldAutonomy').mockReturnValue(pending.promise);

    render(<PersistentWorlds />);
    const button = await screen.findByRole('button', { name: '开始自动推进' });
    await act(async () => {
      button.click();
      button.click();
    });
    expect(start).toHaveBeenCalledTimes(1);

    await act(async () => {
      pending.resolve(world('alpha', { autonomy: driver('alpha', 'running') }));
      await pending.promise;
    });
  });

  it('启动被拒（409）时，说的是服务器给的那句话', async () => {
    stubMountFetches();
    vi.spyOn(api, 'fetchPersistentWorlds').mockResolvedValue({
      worlds: [world('alpha')],
    });
    const pending = deferred<PersistentWorldStatus>();
    vi.spyOn(api, 'startWorldAutonomy').mockReturnValue(pending.promise);

    render(<PersistentWorlds />);
    const button = await screen.findByRole('button', { name: '开始自动推进' });
    await act(async () => {
      button.click();
    });
    await act(async () => {
      pending.reject(
        new api.ApiError('世界 alpha 的驱动正在停止，等它停干净再启动', 409, 'autonomy_busy'),
      );
      await pending.promise.catch(() => undefined);
    });
    await screen.findByText('世界 alpha 的驱动正在停止，等它停干净再启动');
  });

  it('一次慢的列表刷新，吞不掉启动的结果', async () => {
    stubMountFetches();
    const listing = deferred<{ worlds: PersistentWorldStatus[] }>();
    let call = 0;
    vi.spyOn(api, 'fetchPersistentWorlds').mockImplementation(() => {
      call += 1;
      return call === 1
        ? Promise.resolve({ worlds: [world('alpha')] })
        : listing.promise;
    });
    const pending = deferred<PersistentWorldStatus>();
    vi.spyOn(api, 'startWorldAutonomy').mockReturnValue(pending.promise);

    render(<PersistentWorlds />);
    const button = await screen.findByRole('button', { name: '开始自动推进' });
    const refresh = await screen.findByRole('button', { name: '刷新' });
    await act(async () => {
      button.click();
    });
    // 操作还在飞的时候，操作者手点了一次刷新，而且刷新先回来了。
    await act(async () => {
      refresh.click();
      listing.resolve({ worlds: [world('alpha')] });
      await listing.promise;
    });
    await act(async () => {
      pending.resolve(world('alpha', { autonomy: driver('alpha', 'running') }));
      await pending.promise;
    });
    await screen.findByText('已开始自动推进：每 30 秒推 5 模拟分钟');
  });
});
