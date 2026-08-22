// dashboard/src/PersistentWorlds.timing.test.tsx — 控制面的并发时序
//
// 这个文件只盯一件 lint / typecheck / build 都证明不了的事：**哪些响应算数**。
// 它不是这一页的通用 UI 测试，也不打算变成那个 —— 加进来是因为 Codex review
// 的 Finding 2 指出的那类 bug（一次操作的成功/失败被另一次操作触发的刷新
// 悄悄吞掉）只在特定的到达顺序下出现，人眼点几下根本碰不到。
//
// 四条要求，逐条一个用例：
//
//   1. A、B 同时在飞，A 先落地 → B 仍然报出自己的结果。
//   2. 操作在飞时手动刷新完成 → 操作仍然报出自己的结果。
//   3. 旧的列表响应比新的晚回来 → 覆盖不掉新的那份。
//   4. 在飞时组件卸载 → 一次 setState 都不发生。
//
// 每个用例都用**手动兑现的 promise**，不靠计时器：竞态测试不该赌调度。
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import PersistentWorlds from './PersistentWorlds';
import * as api from './api';
import type { PersistentWorldStatus } from './api';

/** 一个可以从外面兑现的 promise。 */
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const world = (
  world_id: string,
  overrides: Partial<PersistentWorldStatus> = {},
): PersistentWorldStatus => ({
  world_id,
  session_id: `${world_id}_session`,
  revision: 1,
  durable_revision: 1,
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
  running: true,
  stop_reason: null,
  clock: '2026-08-22T02:00:00',
  archive_path: `/tmp/worlds/${world_id}/world.json`,
  boundaries_since_checkpoint: 0,
  policy: { every_boundaries: null, min_interval_seconds: 0, on_close: true },
  ...overrides,
});

/** 页面挂载时会拉的另外两个接口；这些用例不关心它们，给个空的就行。 */
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

describe('持久世界控制面的并发时序', () => {
  it('先落地的操作触发的刷新，不许吞掉另一次操作的结果', async () => {
    stubMountFetches();
    // 列表始终给两个开着的世界，于是两行都有「存一次」和「关闭」。
    vi.spyOn(api, 'fetchPersistentWorlds').mockResolvedValue({
      worlds: [world('alpha'), world('beta')],
    });

    const first = deferred<PersistentWorldStatus>();
    const second = deferred<PersistentWorldStatus>();
    vi.spyOn(api, 'checkpointPersistentWorld').mockImplementation((id: string) =>
      id === 'alpha' ? first.promise : second.promise,
    );

    render(<PersistentWorlds />);
    const rows = await screen.findAllByRole('button', { name: '存一次' });
    expect(rows).toHaveLength(2);

    // A、B 先后开跑，都还没落地。
    await act(async () => {
      rows[0].click();
      rows[1].click();
    });

    // A 先落地 —— 它的 finally 会触发一次刷新。
    await act(async () => {
      first.resolve(world('alpha', { revision: 2 }));
      await first.promise;
    });
    await screen.findByText('已存下第 2 版');

    // B 后落地。它的结果**必须**还在：这正是 Finding 2 里被吞掉的那一条。
    await act(async () => {
      second.reject(new api.ApiError('世界 beta 的 checkpoint 失败', 500, 'checkpoint_failed'));
      await second.promise.catch(() => undefined);
    });
    await screen.findByText('世界 beta 的 checkpoint 失败');
    // 两条并存：它们回答的是两个不同的问题。
    expect(screen.getByText('已存下第 2 版')).toBeTruthy();
  });

  it('操作在飞时手动刷新完成，操作结果仍然报得出来', async () => {
    stubMountFetches();
    vi.spyOn(api, 'fetchPersistentWorlds').mockResolvedValue({
      worlds: [world('alpha')],
    });
    const pendingClose = deferred<PersistentWorldStatus>();
    vi.spyOn(api, 'closePersistentWorld').mockReturnValue(pendingClose.promise);
    vi.spyOn(window, 'confirm').mockReturnValue(true);

    render(<PersistentWorlds />);
    const closeButton = await screen.findByRole('button', { name: '关闭' });
    await act(async () => {
      closeButton.click();
    });

    // 操作还在飞的时候，操作者手点了一次刷新，而且刷新先回来了。
    await act(async () => {
      (await screen.findByRole('button', { name: '刷新' })).click();
    });

    await act(async () => {
      pendingClose.reject(
        new api.ApiError('世界 alpha 存不下去，没有干净关闭', 500, 'checkpoint_failed'),
      );
      await pendingClose.promise.catch(() => undefined);
    });

    // 关闭失败是这一页上最不能丢的一条消息。
    await screen.findByText('世界 alpha 存不下去，没有干净关闭');
  });

  it('晚到的旧列表响应覆盖不掉新的那一份', async () => {
    stubMountFetches();
    const stale = deferred<{ worlds: PersistentWorldStatus[] }>();
    const fresh = deferred<{ worlds: PersistentWorldStatus[] }>();
    let call = 0;
    vi.spyOn(api, 'fetchPersistentWorlds').mockImplementation(() => {
      call += 1;
      return call === 1 ? stale.promise : fresh.promise;
    });

    render(<PersistentWorlds />);
    // 挂载那次请求还挂着，操作者又点了一次刷新。
    await act(async () => {
      (await screen.findByRole('button', { name: '刷新' })).click();
    });

    // 新的先回来。
    await act(async () => {
      fresh.resolve({ worlds: [world('fresh-world')] });
      await fresh.promise;
    });
    await screen.findByText('fresh-world');

    // 旧的后回来，带着一份过时的世界列表。它必须被丢掉。
    await act(async () => {
      stale.resolve({ worlds: [world('stale-world')] });
      await stale.promise;
    });
    await waitFor(() => expect(screen.queryByText('stale-world')).toBeNull());
    expect(screen.getByText('fresh-world')).toBeTruthy();
  });

  it('在飞时卸载组件，不再发生任何 setState', async () => {
    stubMountFetches();
    vi.spyOn(api, 'fetchPersistentWorlds').mockResolvedValue({
      worlds: [world('alpha')],
    });
    const pending = deferred<PersistentWorldStatus>();
    vi.spyOn(api, 'checkpointPersistentWorld').mockReturnValue(pending.promise);

    const errors: unknown[] = [];
    const spy = vi.spyOn(console, 'error').mockImplementation((...args) => {
      errors.push(args);
    });

    const view = render(<PersistentWorlds />);
    const button = await screen.findByRole('button', { name: '存一次' });
    await act(async () => {
      button.click();
    });
    view.unmount();

    await act(async () => {
      pending.resolve(world('alpha', { revision: 2 }));
      await pending.promise;
    });

    // 卸载之后既不该更新状态，也就不该有 React 的相关告警。
    expect(errors).toEqual([]);
    spy.mockRestore();
  });

  it('同一个按钮连点两下只发一次请求', async () => {
    stubMountFetches();
    vi.spyOn(api, 'fetchPersistentWorlds').mockResolvedValue({
      worlds: [world('alpha')],
    });
    const pending = deferred<PersistentWorldStatus>();
    const checkpoint = vi
      .spyOn(api, 'checkpointPersistentWorld')
      .mockReturnValue(pending.promise);

    render(<PersistentWorlds />);
    const button = await screen.findByRole('button', { name: '存一次' });
    await act(async () => {
      button.click();
      button.click();
    });

    expect(checkpoint).toHaveBeenCalledTimes(1);

    await act(async () => {
      pending.resolve(world('alpha', { revision: 2 }));
      await pending.promise;
    });
  });
});
