// dashboard/src/PersistentWorlds.autonomy.test.tsx — 认知开关与世界时钟（WORLD-1）
//
// 这个文件只盯 lint / typecheck / build 都证明不了的事：
//
//   1. **按钮跟着服务器状态走，不跟着本地猜测走。** 认知没开的世界给的是
//      「开始认知」；开着的给「停止认知」。
//   2. **认知不可用要把原因说出来。** Start 了但此刻有故障、现实时钟落后，
//      不许显示成"运行中"，也不许只说一句"已停"。
//   3. **时间跟认知是两件事。** 认知没开，世界时钟照样在走，两个都要看得见。
//   4. **按钮沿用 WEB-1 那套时序保护。** 它们跟 checkpoint / close 共用同一个
//      run()，所以"慢的刷新吞掉操作结果"这类 bug 不许长回来。
//
// 每个用例都用手动兑现的 promise，不靠计时器：竞态测试不该赌调度。
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, screen } from '@testing-library/react';
import PersistentWorlds from './PersistentWorlds';
import { OPERATOR, renderAs } from './testPrincipal';
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
  state: 'running' | 'stopped',
  overrides: Partial<WorldDriverStatus> = {},
): WorldDriverStatus => ({
  world_id,
  state,
  running: state === 'running',
  stopping: false,
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
    interval_seconds: 5,
    rate: 1,
    max_steps_per_iteration: 240,
    fault_threshold: 5,
    stop_timeout_seconds: 10,
    max_activations_per_run: 200,
  },
  run_budget: { limit: 200, used: 3, remaining: 197 },
  world_actions: { committed: 3, cap: 100000, remaining: 99997 },
  worker_alive: true,
  worker_exit_reason: null,
  clock_state: 'healthy',
  clock_lag_minutes: 0,
  last_clock_progress: '2026-08-23T02:10:00+00:00',
  fault_since: null,
  last_clock_error: null,
  cognition_available: state === 'running',
  cognition_causes: state === 'running' ? [] : ['not_started'],
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
  last_checkpoint_reason: 'clock_step',
  durable: true,
  directory_synced: true,
  last_error: null,
  error: null,
  residue: [],
  running: true,
  stop_reason: null,
  held: null,
  clock: '2026-08-23T02:05:00',
  archive_path: `/tmp/worlds/${world_id}/world.json`,
  boundaries_since_checkpoint: 0,
  policy: { every_boundaries: 1, min_interval_seconds: 60, on_close: true },
  quiet_time_events: { record: true, since_sim: null, since_wall: null, flips: 0 },
  archive: {
    total_bytes: 3_400_000,
    world_bytes: 1_300_000,
    segments: 2,
    sealed_events: 2880,
    sealed_bytes: 2_100_000,
    active_events: 700,
  },
  autonomy: driver(world_id, 'stopped'),
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

describe('认知开关与世界时钟', () => {
  it('认知没开的世界，给的是「开始认知」，时钟照样在走', async () => {
    stubMountFetches();
    vi.spyOn(api, 'fetchPersistentWorlds').mockResolvedValue({
      worlds: [world('alpha')],
    });
    renderAs(OPERATOR, <PersistentWorlds />);
    await screen.findByRole('button', { name: '开始认知' });
    expect(screen.queryByRole('button', { name: '停止认知' })).toBeNull();
    // P12 的「运行中」和认知的「未开启」是两件事，两个都要看得见。
    expect(screen.getByText('运行中')).toBeTruthy();
    expect(screen.getByText('认知未开启')).toBeTruthy();
  });

  it('认知开着的世界，给的是「停止认知」', async () => {
    stubMountFetches();
    vi.spyOn(api, 'fetchPersistentWorlds').mockResolvedValue({
      worlds: [world('alpha', { autonomy: driver('alpha', 'running') })],
    });
    renderAs(OPERATOR, <PersistentWorlds />);
    await screen.findByRole('button', { name: '停止认知' });
    expect(screen.queryByRole('button', { name: '开始认知' })).toBeNull();
    expect(screen.getByText('认知运行中')).toBeTruthy();
  });

  it('Start 了但此刻有故障，不显示成运行中', async () => {
    stubMountFetches();
    vi.spyOn(api, 'fetchPersistentWorlds').mockResolvedValue({
      worlds: [
        world('alpha', {
          autonomy: driver('alpha', 'running', {
            cognition_available: false,
            cognition_causes: ['fault'],
            clock_state: 'faulted',
          }),
        }),
      ],
    });
    renderAs(OPERATOR, <PersistentWorlds />);
    await screen.findByText('认知暂不可用');
    expect(screen.queryByText('认知运行中')).toBeNull();
    expect(screen.getByRole('button', { name: '停止认知' })).toBeTruthy();
  });

  it('每天续额的授权用完了：说几点续，并且给的是「停止认知」', async () => {
    stubMountFetches();
    vi.spyOn(api, 'fetchPersistentWorlds').mockResolvedValue({
      worlds: [
        world('yoake-mae', {
          autonomy: driver('yoake-mae', 'stopped', {
            exit_reason: 'run_budget_exhausted',
            cognition_causes: ['run_budget_exhausted'],
            run_budget: {
              limit: 300,
              used: 300,
              remaining: 0,
              renewal: 'world-day-0500',
              renews_at: '2026-08-24T05:00:00',
              day_start: '2026-08-23T05:00:00',
            },
          }),
        }),
      ],
    });
    renderAs(OPERATOR, <PersistentWorlds />);
    await screen.findByText('今天的额度用完了，08-24 05:00 续');
    expect(screen.getByRole('button', { name: '停止认知' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: '开始认知' })).toBeNull();
    expect(screen.queryByText(/再按一次/)).toBeNull();
  });

  it('耗尽后 Stop：先说已停，不承诺续额，也不催人再按 Start', async () => {
    stubMountFetches();
    vi.spyOn(api, 'fetchPersistentWorlds').mockResolvedValue({
      worlds: [
        world('yoake-mae', {
          autonomy: driver('yoake-mae', 'stopped', {
            stop_reason: 'operator',
            exit_reason: 'run_budget_exhausted',
            cognition_causes: ['operator_paused', 'run_budget_exhausted'],
            run_budget: {
              limit: 300,
              used: 300,
              remaining: 0,
              renewal: 'world-day-0500',
              renews_at: null,
              day_start: '2026-08-23T05:00:00',
            },
          }),
        }),
      ],
    });
    renderAs(OPERATOR, <PersistentWorlds />);
    await screen.findByText('已停');
    expect(screen.getByRole('button', { name: '开始认知' })).toBeTruthy();
    const toggle = screen.getByRole('button', { name: /yoake-mae/ });
    await act(async () => {
      toggle.click();
    });
    expect(screen.getByText('每天 300 次，今天用了 300（已停，不再续）')).toBeTruthy();
    expect(screen.queryByText(/续$/)).toBeNull();
    expect(screen.queryByText(/再按一次/)).toBeNull();
  });

  it.each([
    ['2026-08-23T04:10:00', '每天 300 次，今天还剩 288，08-23 05:00 续'],
    ['2026-08-23T05:00:00', '每天 300 次，今天还剩 288，本分钟处理完后续'],
  ])('续额授权开着，时钟 %s：每天 N 次、今天还剩几次、几点续', async (clock, text) => {
    stubMountFetches();
    vi.spyOn(api, 'fetchPersistentWorlds').mockResolvedValue({
      worlds: [
        world('yoake-mae', {
          clock,
          autonomy: driver('yoake-mae', 'running', {
            run_budget: {
              limit: 300,
              used: 12,
              remaining: 288,
              renewal: 'world-day-0500',
              renews_at: '2026-08-23T05:00:00',
              day_start: '2026-08-22T05:00:00',
            },
          }),
        }),
      ],
    });
    renderAs(OPERATOR, <PersistentWorlds />);
    const toggle = await screen.findByRole('button', { name: /yoake-mae/ });
    await act(async () => {
      toggle.click();
    });
    expect(screen.getByText(text)).toBeTruthy();
    // 彩蛋：悬停在「今天的额度」上才看得到。
    expect(screen.getByText('今天的额度').getAttribute('title')).toBe(
      '阿戈摩托之眼：每个世界日，额度被拨回同一个起点',
    );
  });

  it('不限额的授权说不限额，不显示成 0 / N', async () => {
    stubMountFetches();
    vi.spyOn(api, 'fetchPersistentWorlds').mockResolvedValue({
      worlds: [
        world('alpha', {
          autonomy: driver('alpha', 'running', {
            run_budget: { limit: null, used: null, remaining: null, renewal: null, renews_at: null },
          }),
        }),
      ],
    });
    renderAs(OPERATOR, <PersistentWorlds />);
    const toggle = await screen.findByRole('button', { name: /alpha/ });
    await act(async () => {
      toggle.click();
    });
    expect(screen.getByText('不限额')).toBeTruthy();
    expect(screen.queryByText(/条激活/)).toBeNull();
  });

  it('不续额的世界还是「本轮」，用完了才说再按一次', async () => {
    stubMountFetches();
    vi.spyOn(api, 'fetchPersistentWorlds').mockResolvedValue({
      worlds: [
        world('alpha', {
          autonomy: driver('alpha', 'stopped', {
            exit_reason: 'run_budget_exhausted',
            cognition_causes: ['run_budget_exhausted'],
            run_budget: { limit: 200, used: 200, remaining: 0, renewal: null, renews_at: null },
          }),
        }),
      ],
    });
    renderAs(OPERATOR, <PersistentWorlds />);
    await screen.findByText('本轮额度用完');
    expect(screen.getByRole('button', { name: '开始认知' })).toBeTruthy();
    const toggle = screen.getByRole('button', { name: /alpha/ });
    await act(async () => {
      toggle.click();
    });
    expect(screen.getByText('本轮额度').getAttribute('title')).toBeNull();
    expect(screen.getByText(/再按一次「开始认知」就是新的一轮/)).toBeTruthy();
  });

  it('展开之后，时钟状态与认知不可用的全部原因都看得见', async () => {
    stubMountFetches();
    vi.spyOn(api, 'fetchPersistentWorlds').mockResolvedValue({
      worlds: [
        world('alpha', {
          autonomy: driver('alpha', 'stopped', {
            cognition_causes: ['not_started', 'wall_clock_behind'],
            clock_state: 'healthy',
            clock_lag_minutes: -8,
          }),
        }),
      ],
    });
    renderAs(OPERATOR, <PersistentWorlds />);
    const toggle = await screen.findByRole('button', { name: /alpha/ });
    await act(async () => {
      toggle.click();
    });
    expect(screen.getByText('不可用：还没 Start、现实时钟落后于存档')).toBeTruthy();
    expect(screen.getByText(/现实时钟落后 8 分钟/)).toBeTruthy();
  });

  it('停止之后说清楚：时间照走', async () => {
    stubMountFetches();
    vi.spyOn(api, 'fetchPersistentWorlds').mockResolvedValue({
      worlds: [world('alpha', { autonomy: driver('alpha', 'running') })],
    });
    const pending = deferred<PersistentWorldStatus>();
    vi.spyOn(api, 'stopWorldAutonomy').mockReturnValue(pending.promise);

    renderAs(OPERATOR, <PersistentWorlds />);
    const button = await screen.findByRole('button', { name: '停止认知' });
    await act(async () => {
      button.click();
    });
    await act(async () => {
      pending.resolve(world('alpha', { autonomy: driver('alpha', 'stopped') }));
      await pending.promise;
    });
    await screen.findByText('已停止认知（时间与作息照走，可以再启动）');
  });

  it('Start 之后认知仍不可用时，把原因说出来', async () => {
    stubMountFetches();
    vi.spyOn(api, 'fetchPersistentWorlds').mockResolvedValue({
      worlds: [world('alpha')],
    });
    const pending = deferred<PersistentWorldStatus>();
    vi.spyOn(api, 'startWorldAutonomy').mockReturnValue(pending.promise);

    renderAs(OPERATOR, <PersistentWorlds />);
    const button = await screen.findByRole('button', { name: '开始认知' });
    await act(async () => {
      button.click();
    });
    await act(async () => {
      pending.resolve(
        world('alpha', {
          autonomy: driver('alpha', 'stopped', {
            exit_reason: 'world_action_cap',
            cognition_causes: ['world_action_cap'],
          }),
        }),
      );
      await pending.promise;
    });
    await screen.findByText('已 Start，但认知此刻仍不可用：世界动作上限');
  });

  it('连点两下「开始认知」只发一次请求', async () => {
    stubMountFetches();
    vi.spyOn(api, 'fetchPersistentWorlds').mockResolvedValue({
      worlds: [world('alpha')],
    });
    const pending = deferred<PersistentWorldStatus>();
    const start = vi.spyOn(api, 'startWorldAutonomy').mockReturnValue(pending.promise);

    renderAs(OPERATOR, <PersistentWorlds />);
    const button = await screen.findByRole('button', { name: '开始认知' });
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

  it('额度格空着就用服务器默认；写了数字就按这个数 Start', async () => {
    stubMountFetches();
    vi.spyOn(api, 'fetchPersistentWorlds').mockResolvedValue({
      worlds: [world('alpha')],
    });
    const start = vi
      .spyOn(api, 'startWorldAutonomy')
      .mockResolvedValue(world('alpha', { autonomy: driver('alpha', 'running') }));

    renderAs(OPERATOR, <PersistentWorlds />);
    const button = await screen.findByRole('button', { name: '开始认知' });
    await act(async () => {
      button.click();
    });
    expect(start).toHaveBeenLastCalledWith('alpha', undefined);

    cleanup();
    start.mockClear();
    renderAs(OPERATOR, <PersistentWorlds />);
    const input = await screen.findByRole('textbox', { name: '这次 Start 的额度' });
    fireEvent.change(input, { target: { value: ' 300 ' } });
    await act(async () => {
      screen.getByRole('button', { name: '开始认知' }).click();
    });
    expect(start).toHaveBeenLastCalledWith('alpha', 300);
  });

  it('额度不是 1–100000 的整数就不许按 Start', async () => {
    stubMountFetches();
    vi.spyOn(api, 'fetchPersistentWorlds').mockResolvedValue({
      worlds: [world('alpha')],
    });
    const start = vi.spyOn(api, 'startWorldAutonomy');

    renderAs(OPERATOR, <PersistentWorlds />);
    const input = await screen.findByRole('textbox', { name: '这次 Start 的额度' });
    const button = screen.getByRole('button', { name: '开始认知' }) as HTMLButtonElement;
    for (const bad of ['0', '-1', '2.5', 'abc', '100001']) {
      fireEvent.change(input, { target: { value: bad } });
      expect(button.disabled).toBe(true);
    }
    await act(async () => {
      button.click();
    });
    expect(start).not.toHaveBeenCalled();
    fireEvent.change(input, { target: { value: '100000' } });
    expect(button.disabled).toBe(false);
  });

  it('启动被拒（409）时，说的是服务器给的那句话', async () => {
    stubMountFetches();
    vi.spyOn(api, 'fetchPersistentWorlds').mockResolvedValue({
      worlds: [world('alpha')],
    });
    const pending = deferred<PersistentWorldStatus>();
    vi.spyOn(api, 'startWorldAutonomy').mockReturnValue(pending.promise);

    renderAs(OPERATOR, <PersistentWorlds />);
    const button = await screen.findByRole('button', { name: '开始认知' });
    await act(async () => {
      button.click();
    });
    await act(async () => {
      pending.reject(new api.ApiError('世界 alpha 已经关闭', 409, 'autonomy_refused'));
      await pending.promise.catch(() => undefined);
    });
    await screen.findByText('世界 alpha 已经关闭');
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

    renderAs(OPERATOR, <PersistentWorlds />);
    const button = await screen.findByRole('button', { name: '开始认知' });
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
    await screen.findByText('已开始认知：从下一个完整模拟分钟起，角色开始自己做决定');
  });
});

describe('正式世界开局', () => {
  it('还没有「夜明け前」时给出开局按钮，确认之后只发一次请求', async () => {
    stubMountFetches();
    vi.spyOn(api, 'fetchPersistentWorlds').mockResolvedValue({ worlds: [world('alpha')] });
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const pending = deferred<PersistentWorldStatus>();
    const bootstrap = vi.spyOn(api, 'bootstrapFormalWorld').mockReturnValue(pending.promise);

    renderAs(OPERATOR, <PersistentWorlds />);
    const button = await screen.findByRole('button', { name: '建立「夜明け前」' });
    await act(async () => {
      button.click();
      button.click();
    });
    expect(bootstrap).toHaveBeenCalledTimes(1);
    expect(bootstrap).toHaveBeenCalledWith('yoake-mae');
    await act(async () => {
      pending.resolve(
        world('yoake-mae', { revision: 1, clock: '2026-09-27T19:00:00' }),
      );
      await pending.promise;
    });
    await screen.findByText('已建立「夜明け前」，开局于 2026-09-27 19:00，存档第 1 版');
  });

  it('取消确认就什么都不发', async () => {
    stubMountFetches();
    vi.spyOn(api, 'fetchPersistentWorlds').mockResolvedValue({ worlds: [] });
    vi.spyOn(window, 'confirm').mockReturnValue(false);
    const bootstrap = vi.spyOn(api, 'bootstrapFormalWorld');
    renderAs(OPERATOR, <PersistentWorlds />);
    const button = await screen.findByRole('button', { name: '建立「夜明け前」' });
    await act(async () => {
      button.click();
    });
    expect(bootstrap).not.toHaveBeenCalled();
  });

  it('已经有「夜明け前」就不再给开局按钮', async () => {
    stubMountFetches();
    vi.spyOn(api, 'fetchPersistentWorlds').mockResolvedValue({
      worlds: [world('yoake-mae')],
    });
    renderAs(OPERATOR, <PersistentWorlds />);
    await screen.findByText('yoake-mae');
    expect(screen.queryByRole('button', { name: '建立「夜明け前」' })).toBeNull();
  });

  it('展开之后看得见「记录安静的分钟」与存档大小', async () => {
    stubMountFetches();
    vi.spyOn(api, 'fetchPersistentWorlds').mockResolvedValue({
      worlds: [world('alpha')],
    });
    renderAs(OPERATOR, <PersistentWorlds />);
    const toggle = await screen.findByRole('button', { name: /alpha/ });
    await act(async () => {
      toggle.click();
    });
    expect(screen.getByText(/^记录（默认）/)).toBeTruthy();
    expect(
      screen.getByText('3.2 MB（已封存 2 卷、2880 条事件；world.json 里 700 条）'),
    ).toBeTruthy();
    expect(screen.getByRole('button', { name: '不再记录' })).toBeTruthy();
  });

  it('拨开关要先确认；取消就什么都不发', async () => {
    stubMountFetches();
    vi.spyOn(api, 'fetchPersistentWorlds').mockResolvedValue({
      worlds: [world('alpha')],
    });
    const flip = vi.spyOn(api, 'setQuietTimeEvents');
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    renderAs(OPERATOR, <PersistentWorlds />);
    const toggle = await screen.findByRole('button', { name: /alpha/ });
    await act(async () => {
      toggle.click();
    });
    await act(async () => {
      screen.getByRole('button', { name: '不再记录' }).click();
    });
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(String(confirm.mock.calls[0][0])).toContain('只影响之后');
    expect(flip).not.toHaveBeenCalled();
  });

  it('确认之后拨下去，并说清楚从哪一刻起', async () => {
    stubMountFetches();
    vi.spyOn(api, 'fetchPersistentWorlds').mockResolvedValue({
      worlds: [world('alpha')],
    });
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const pending = deferred<PersistentWorldStatus>();
    const flip = vi.spyOn(api, 'setQuietTimeEvents').mockReturnValue(pending.promise);
    renderAs(OPERATOR, <PersistentWorlds />);
    const toggle = await screen.findByRole('button', { name: /alpha/ });
    await act(async () => {
      toggle.click();
    });
    await act(async () => {
      screen.getByRole('button', { name: '不再记录' }).click();
    });
    expect(flip).toHaveBeenCalledWith('alpha', false);
    await act(async () => {
      pending.resolve(
        world('alpha', {
          quiet_time_events: {
            record: false,
            since_sim: '2026-08-23T02:05:00',
            since_wall: '2026-08-22T17:05:00+00:00',
            flips: 1,
          },
        }),
      );
      await pending.promise;
    });
    await screen.findByText('已停止记录安静的分钟（从 2026-08-23 02:05 起）');
  });

  it('没开着的世界只显示开关状态，不给拨', async () => {
    stubMountFetches();
    vi.spyOn(api, 'fetchPersistentWorlds').mockResolvedValue({
      worlds: [
        world('alpha', {
          owned: false,
          running: null,
          autonomy: null,
          quiet_time_events: {
            record: false,
            since_sim: '2026-08-23T02:05:00',
            since_wall: null,
            flips: 1,
          },
        }),
      ],
    });
    renderAs(OPERATOR, <PersistentWorlds />);
    const toggle = await screen.findByRole('button', { name: /alpha/ });
    await act(async () => {
      toggle.click();
    });
    expect(screen.getByText(/^不记录（从 2026-08-23 02:05 起）/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: '重新记录' })).toBeNull();
  });
});
