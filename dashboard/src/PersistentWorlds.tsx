// dashboard/src/PersistentWorlds.tsx — 持久世界控制面（WEB-1）
//
// 这一页是**操作台**，不是第二份真相。它刻意不维护自己的生命周期状态机：
// 按钮开不开可以看最近一次服务器响应，但"这个世界归谁、第几版、脏没脏、
// 在不在跑"永远只有服务器知道。所以每次操作之后都重新拉一遍权威状态，
// 而不是把本地那份猜测往前推。
//
// 三条约束写在这里，免得以后被顺手改掉：
//
//   1. **过期的动作要拿到冲突，而不是被本地拦下。** 另一个标签页把世界关了
//      之后，这里的"存一次"按钮可能还亮着 —— 点下去会拿到 409，然后刷新。
//      本地拦截会让 UI 看起来对、实际上在按一份过期的假设做决定。
//   2. **慢的列表响应不许覆盖新的，但它也不许株连操作结果。** 这两件事各有
//      各的时序，必须分开管：列表回答"某一刻的世界长什么样"，会过期；操作
//      结果回答"操作者刚才按下的那一下得到了什么答复"，不会过期。共用一个
//      序号的话，先回来的那次操作触发的刷新会把后回来的那次操作的成功/失败
//      直接吞掉 —— 一次 close 失败就这么从屏幕上消失了。
//   3. **关闭要确认。** 它会停掉一个正在跑的世界。
//   4. **认知是显式开关，而且它跟"世界开着"、"时间在走"都是两件事。**（WORLD-1）
//      `running` 是 P12 的"运行时还接不接受写入"；时间从世界打开起就跟着现实
//      走（`clock_state`）；`autonomy.state` 是"服务器会不会替角色花模型调用"。
//      开始认知 = 服务器开始自己花 API 额度，所以它只能由操作者按下；认知不可用
//      时要把**全部**原因摆出来，而不是一句"已停"。
import { useCallback, useEffect, useRef, useState } from 'react';
import {
  ApiError,
  bootstrapFormalWorld,
  checkpointPersistentWorld,
  closePersistentWorld,
  createPersistentWorld,
  fetchPersistentWorlds,
  fetchReloadStatus,
  fetchWorldScenes,
  restorePersistentWorld,
  setQuietTimeEvents,
  startWorldAutonomy,
  stopWorldAutonomy,
  SCOPE_OPERATE,
  type ArchiveFootprint,
  type PersistentWorldStatus,
  type WorldDriverStatus,
} from './api';
import { useCan } from './principal';
import './worlds.css';

type Action =
  | 'create'
  | 'bootstrap'
  | 'restore'
  | 'checkpoint'
  | 'close'
  | 'autonomy-start'
  | 'autonomy-stop'
  | 'quiet-time';

interface Feedback {
  worldId: string;
  kind: 'ok' | 'error';
  message: string;
}

interface SceneOption {
  id: string;
  label: string;
}

// 建世界那一格的 pending key。用空格开头，跟任何合法 world_id 都撞不上
// （world_id 只允许小写字母、数字和 . _ -，且必须以字母或数字开头）。
const CREATE_KEY = ' create';

// 正式世界（WORLD-1）。身份、时区、开局时刻都在服务器侧定义，这里只认 ID。
const FORMAL_WORLD_ID = 'yoake-mae';
const FORMAL_WORLD_NAME = '夜明け前';

const describe = (e: unknown, fallback: string): string =>
  e instanceof ApiError ? e.message : e instanceof Error ? e.message : fallback;

/** 一句给人看的状态。只由服务器字段推出来，不掺本地记忆。 */
function summarize(world: PersistentWorldStatus): { label: string; tone: string } {
  if (world.owned && world.running) return { label: '运行中', tone: 'ok' };
  if (world.owned) {
    return { label: world.stop_reason ? `已停：${world.stop_reason}` : '已停', tone: 'warn' };
  }
  if (world.error) return { label: '存档读不出来', tone: 'ooc' };
  if (world.revision === null) return { label: '没有存档', tone: 'dim' };
  return { label: '已归档', tone: 'dim' };
}

/** 认知那一格给人看的一句话。只由服务器字段推出来。 */
function describeDriver(driver: WorldDriverStatus | null): { label: string; tone: string } {
  if (driver === null) return { label: '未打开', tone: 'dim' };
  if (driver.state === 'running') {
    // 操作者开着，但此刻可能因为故障、现实时钟落后而用不了：如实说。
    return driver.cognition_available
      ? { label: '认知运行中', tone: 'ok' }
      : { label: '认知暂不可用', tone: 'warn' };
  }
  // 操作者按过 Stop 就先说"已停"：耗尽仍是并列原因，exit_reason 可能还是
  // run_budget_exhausted，但授权已经收回，不会再续，也不该催人"再按 Start"。
  if (driver.cognition_causes.includes('operator_paused')) {
    return { label: '已停', tone: 'dim' };
  }
  // 两种"花完了"必须分开说：一种再按一次 Start（或等续额）就好，另一种按多少次都没用。
  if (driver.exit_reason === 'run_budget_exhausted') {
    return driver.run_budget.renews_at
      ? { label: `今天的额度用完了，${simMinute(driver.run_budget.renews_at)} 续`, tone: 'warn' }
      : { label: '本轮额度用完', tone: 'warn' };
  }
  if (driver.exit_reason === 'world_action_cap') {
    return { label: '已达世界动作上限', tone: 'ooc' };
  }
  return { label: '认知未开启', tone: 'dim' };
}

const CAUSE_TEXT: Record<string, string> = {
  not_started: '还没 Start',
  operator_paused: '操作者已停止',
  run_budget_exhausted: '额度用完',
  world_action_cap: '世界动作上限',
  process_stopped: '停机期间',
  fault: '时钟故障',
  wall_clock_behind: '现实时钟落后于存档',
};

const CLOCK_STATE_TEXT: Record<string, string> = {
  catching_up: '补跑中',
  healthy: '与现实同步',
  faulted: '故障（退避重试中）',
};

/** 模拟时刻（ISO，无时区）给人看的形状：月-日 时:分。 */
const simMinute = (iso: string): string => iso.slice(5, 16).replace('T', ' ');

/** 额度那一格。续额只在 renews_at 有值时承诺；故障、世界上限照样由"认知"那一格并列说。 */
function describeBudget(driver: WorldDriverStatus, clock: string | null): string {
  const budget = driver.run_budget;
  const exhausted = driver.exit_reason === 'run_budget_exhausted';
  if (budget.limit === null) {
    // 不限额的授权没有 N，也没有剩余可说。
    return '不限额';
  }
  if (budget.renewal) {
    if (!budget.renews_at) {
      // Stop 了：授权收回，不写续额时刻。
      return `每天 ${budget.limit} 次，今天用了 ${budget.used}（已停，不再续）`;
    }
    const when =
      clock !== null && budget.renews_at === clock
        ? '本分钟处理完后续'
        : `${simMinute(budget.renews_at)} 续`;
    return exhausted
      ? `今天的额度用完了，${when}`
      : `每天 ${budget.limit} 次，今天还剩 ${budget.remaining}，${when}`;
  }
  return (
    `${budget.used} / ${budget.limit} 条激活` +
    (exhausted && !driver.cognition_causes.includes('operator_paused')
      ? '（已用完；再按一次「开始认知」就是新的一轮）'
      : '')
  );
}

const causesText = (causes: string[]): string =>
  causes.map((cause) => CAUSE_TEXT[cause] ?? cause).join('、');

const clockText = (iso: string | null): string =>
  iso === null ? '—' : iso.replace('T', ' ').slice(0, 16);

const boolText = (value: boolean | null, yes: string, no: string): string =>
  value === null ? '未知' : value ? yes : no;

const bytesText = (bytes: number): string => {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
};

const archiveText = (archive: ArchiveFootprint | null): string =>
  archive === null
    ? '—'
    : `${bytesText(archive.total_bytes)}（已封存 ${archive.segments} 卷、` +
      `${archive.sealed_events} 条事件；world.json 里 ${archive.active_events} 条）`;

export default function PersistentWorlds() {
  const [worlds, setWorlds] = useState<PersistentWorldStatus[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [pending, setPending] = useState<Record<string, Action | undefined>>({});
  // 每个世界一条自己的反馈，外加建世界表单那一条。用一张表而不是一个格子：
  // 同时对两个世界动手时，先回来的那条结果不该被后回来的挤掉 —— 它们回答的
  // 是两个不同的问题。
  const [rowFeedback, setRowFeedback] = useState<Record<string, Feedback>>({});
  const [createFeedback, setCreateFeedback] = useState<Feedback | null>(null);

  const [scenes, setScenes] = useState<SceneOption[]>([]);
  const [characterPool, setCharacterPool] = useState<string[]>([]);
  const [newId, setNewId] = useState('');
  const [newScene, setNewScene] = useState('');
  const [newCharacters, setNewCharacters] = useState<string[]>([]);

  // 列表请求的顺序票。**只**管列表：拿回来的票不是最新的，就说明这份列表
  // 描述的是一个已经过时的时刻，丢掉。
  //
  // 操作结果**不**参与这个比较，这一条是要害。操作结果不是"某一刻的世界长
  // 什么样"，而是"操作者刚才按下的那一下得到了什么答复"——它没有过期一说。
  // 早先把两者共用一个序号，会导致：A 先回来 → A 的 finally 触发刷新 → 序号
  // 前进 → B 回来时发现票过期，于是把自己那条成功/失败**丢掉**。一次
  // checkpoint 或 close 的失败就这么从操作反馈区消失了。
  const listSequence = useRef(0);
  // 已卸载之后不再 setState。
  const alive = useRef(true);
  // 在飞的操作。用 ref 而不是只看 state：重复提交保护要在**事件发生的那一刻**
  // 成立，而 state 要等重渲染才更新。
  const inFlight = useRef<Set<string>>(new Set());
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  /** 让所有在飞的列表请求作废。操作开始时也要叫一次：它们带回来的数据
   *  描述的是这次操作**之前**的世界。 */
  const invalidateList = () => ++listSequence.current;

  const refresh = useCallback(() => {
    const ticket = invalidateList();
    fetchPersistentWorlds()
      .then((data) => {
        if (!alive.current || ticket !== listSequence.current) return;
        setWorlds(data.worlds);
        setLoadError(null);
      })
      .catch((e: unknown) => {
        if (!alive.current || ticket !== listSequence.current) return;
        setLoadError(describe(e, '加载持久世界列表失败'));
      });
  }, []);

  useEffect(() => {
    refresh();
    // 建世界的选项来自现有的内容接口：场景表，以及当前生效配置里的可用角色。
    // 这一页不为此新增接口——服务器仍然是校验这两样的唯一权威。
    fetchWorldScenes()
      .then((map) => {
        if (!alive.current) return;
        setScenes(
          Object.entries(map).map(([id, scene]) => ({
            id,
            label: (scene as { label?: string }).label || id,
          })),
        );
      })
      .catch(() => undefined);
    fetchReloadStatus()
      .then((status) => {
        const registry = status.registry;
        if (!alive.current || !registry) return;
        setCharacterPool(registry.ready_characters);
        setNewScene((current) => current || registry.default_scene);
      })
      .catch(() => undefined);
  }, [refresh]);

  /** 把一条反馈放进它该去的格子：建世界那条留在表单里，其余的挂在对应的行上。 */
  const report = (key: string, worldId: string, note: Feedback | null) => {
    if (key === CREATE_KEY) {
      setCreateFeedback(note);
      return;
    }
    setRowFeedback((current) => {
      const next = { ...current };
      if (note === null) delete next[worldId];
      else next[worldId] = note;
      return next;
    });
  };

  const run = (
    key: string,
    action: Action,
    worldId: string,
    call: () => Promise<PersistentWorldStatus>,
    okText: (status: PersistentWorldStatus) => string,
    onOk?: () => void,
  ) => {
    // 重复提交保护：同一个按钮在飞的时候，第二次点击什么都不做。
    if (inFlight.current.has(key)) return;
    inFlight.current.add(key);
    setPending((current) => ({ ...current, [key]: action }));
    // 只清掉这一格的旧反馈。别的世界那条是别的问题的答案，跟这次无关。
    report(key, worldId, null);
    // 这次操作会改变世界，所以在飞的列表请求全部作废 —— 它们描述的是操作
    // 之前的样子。注意这里**不**给自己领票：操作结果不参与列表的新旧比较。
    invalidateList();
    call()
      .then((status) => {
        if (!alive.current) return;
        report(key, worldId, { worldId, kind: 'ok', message: okText(status) });
        onOk?.();
      })
      .catch((e: unknown) => {
        if (!alive.current) return;
        report(key, worldId, {
          worldId,
          kind: 'error',
          message: describe(e, '操作失败'),
        });
      })
      .finally(() => {
        inFlight.current.delete(key);
        if (!alive.current) return;
        setPending((current) => {
          const next = { ...current };
          delete next[key];
          return next;
        });
        // 不论成败都回头拿一次权威状态：本地这份可能已经过期了。
        refresh();
      });
  };

  const onCreate = (event: React.FormEvent) => {
    event.preventDefault();
    const worldId = newId.trim();
    if (!worldId || !newScene || newCharacters.length === 0) return;
    run(
      CREATE_KEY,
      'create',
      worldId,
      () => createPersistentWorld(worldId, newScene, newCharacters),
      (status) => `已创建「${status.world_id}」，存档第 ${status.revision} 版`,
      // 只在真的建成之后才清空。失败时留着，操作者能看见自己填的是什么。
      () => setNewId(''),
    );
  };

  const onBootstrap = () => {
    // 开局之后这个世界的时间就一直跟着现实走，而且它只能开局一次。
    const confirmed = window.confirm(
      `建立「${FORMAL_WORLD_NAME}」（${FORMAL_WORLD_ID}）？\n\n` +
        '世界从东京时间当天 19:00 开局，时间从此跟着现实走；角色要等你按「开始认知」' +
        '才会自己做决定。这个世界只能开局一次。',
    );
    if (!confirmed) return;
    run(
      CREATE_KEY,
      'bootstrap',
      FORMAL_WORLD_ID,
      () => bootstrapFormalWorld(FORMAL_WORLD_ID),
      (status) =>
        `已建立「${FORMAL_WORLD_NAME}」，开局于 ${clockText(status.clock)}，存档第 ${status.revision} 版`,
    );
  };

  const onRestore = (worldId: string) =>
    run(
      `${worldId}:restore`,
      'restore',
      worldId,
      () => restorePersistentWorld(worldId),
      (status) => `已恢复到第 ${status.revision} 版`,
    );

  const onCheckpoint = (worldId: string) =>
    run(
      `${worldId}:checkpoint`,
      'checkpoint',
      worldId,
      () => checkpointPersistentWorld(worldId),
      (status) => `已存下第 ${status.revision} 版`,
    );

  const onStartAutonomy = (worldId: string) =>
    run(
      `${worldId}:autonomy-start`,
      'autonomy-start',
      worldId,
      () => startWorldAutonomy(worldId),
      (status) => {
        const autonomy = status.autonomy;
        if (autonomy && !autonomy.cognition_available && autonomy.cognition_causes.length) {
          return `已 Start，但认知此刻仍不可用：${causesText(autonomy.cognition_causes)}`;
        }
        return '已开始认知：从下一个完整模拟分钟起，角色开始自己做决定';
      },
    );

  const onStopAutonomy = (worldId: string) =>
    run(
      `${worldId}:autonomy-stop`,
      'autonomy-stop',
      worldId,
      () => stopWorldAutonomy(worldId),
      () =>
        // 正在飞的那次调用回来之后，提交时按新区间判为不可用：不会再落地。
        '已停止认知（时间与作息照走，可以再启动）',
    );

  const onQuietTime = (worldId: string, record: boolean) => {
    // 这个开关决定之后的世界历史长什么样，所以拨之前确认一次。
    const confirmed = window.confirm(
      record
        ? `让「${worldId}」重新记录安静的分钟？\n\n` +
            '从此刻起每个时钟步都记一条时间事件。之前没记的那段不会补上——' +
            '那段时间里确实什么都没发生，账本里记着它从哪一刻开始不记。'
        : `让「${worldId}」不再记录安静的分钟？\n\n` +
            '只影响之后：从此刻起，没有到期、没有作息变化的时钟步照样往前走，但不再记成' +
            '世界事件。已经存下的每一条都不动；这次拨动本身会记进运维账本，随时可以拨回来。',
    );
    if (!confirmed) return;
    run(
      `${worldId}:quiet-time`,
      'quiet-time',
      worldId,
      () => setQuietTimeEvents(worldId, record),
      (status) => {
        const since = status.quiet_time_events?.since_sim ?? null;
        return record
          ? `已开始记录安静的分钟（从 ${clockText(since)} 起）`
          : `已停止记录安静的分钟（从 ${clockText(since)} 起）`;
      },
    );
  };

  const onClose = (worldId: string) => {
    // 关闭会停掉一个正在跑的世界，所以先确认。
    const confirmed = window.confirm(
      `关闭世界「${worldId}」？\n\n` +
        '会先停下世界时钟、停止接受新的行动、等在跑的事务落定、写下最后一份存档，' +
        '然后归还所有权。存不下去时不会假装关干净了，世界会继续开着。',
    );
    if (!confirmed) return;
    run(
      `${worldId}:close`,
      'close',
      worldId,
      () => closePersistentWorld(worldId),
      (status) => `已关闭，最后一版是第 ${status.revision} 版`,
    );
  };

  const toggleCharacter = (id: string) =>
    setNewCharacters((current) =>
      current.includes(id) ? current.filter((c) => c !== id) : [...current, id],
    );

  const creating = pending[CREATE_KEY] !== undefined;
  // 只读账户看不到这一页上的任何写入口。**服务端独立地拒绝**（中间件按方法
  // 判 scope），所以这里藏起来的只是一个点了会拿到 403 的按钮。
  const canOperate = useCan(SCOPE_OPERATE);

  return (
    <div className="worlds entrance">
      <div className="worlds-head">
        <div>
          <p className="worlds-note">
            每个世界都有自己的权威状态和一把独占锁。恢复只能回到
            <strong>最后一次成功的 checkpoint</strong>
            ——它之后的内存工作在进程被强杀时会丢，这里没有 WAL。
          </p>
        </div>
        <button className="btn" onClick={refresh}>
          刷新
        </button>
      </div>

      {loadError ? <div className="worlds-error">{loadError}</div> : null}

      {canOperate &&
      worlds !== null &&
      !worlds.some((world) => world.world_id === FORMAL_WORLD_ID) ? (
        <div className="worlds-create">
          <h3>正式世界</h3>
          <div className="worlds-create-actions">
            <button className="btn btn-accent" disabled={creating} onClick={onBootstrap}>
              {pending[CREATE_KEY] === 'bootstrap'
                ? '开局中…'
                : `建立「${FORMAL_WORLD_NAME}」`}
            </button>
          </div>
        </div>
      ) : null}

      {canOperate ? (
      <form className="worlds-create" onSubmit={onCreate}>
        <h3>新建世界</h3>
        <div className="worlds-create-row">
          <label>
            <span>world_id</span>
            <input
              value={newId}
              onChange={(e) => setNewId(e.target.value)}
              placeholder="nightcord"
              maxLength={64}
            />
          </label>
          <label>
            <span>起始场景</span>
            <select value={newScene} onChange={(e) => setNewScene(e.target.value)}>
              <option value="">选择场景…</option>
              {scenes.map((scene) => (
                <option key={scene.id} value={scene.id}>
                  {scene.label}（{scene.id}）
                </option>
              ))}
            </select>
          </label>
        </div>
        <div className="worlds-characters">
          <span>角色</span>
          <div className="worlds-chips">
            {characterPool.map((id) => (
              <button
                key={id}
                type="button"
                className={`toggle worlds-chip${newCharacters.includes(id) ? ' on' : ''}`}
                onClick={() => toggleCharacter(id)}
              >
                {id}
              </button>
            ))}
          </div>
        </div>
        <div className="worlds-create-actions">
          <button
            className="btn btn-accent"
            type="submit"
            disabled={creating || !newId.trim() || !newScene || newCharacters.length === 0}
          >
            {creating ? '创建中…' : '创建'}
          </button>
          <span className="worlds-hint">
            world_id 只允许小写字母、数字和 . _ -，而且创建不会覆盖已经存在的存档。
          </span>
        </div>
        {createFeedback ? (
          <div className={`worlds-feedback ${createFeedback.kind}`}>
            {createFeedback.message}
          </div>
        ) : null}
      </form>
      ) : null}

      {worlds === null ? (
        <div className="state-msg">加载中…</div>
      ) : worlds.length === 0 ? (
        <div className="empty-hint">还没有任何持久世界。</div>
      ) : (
        <ul className="worlds-list">
          {worlds.map((world) => {
            const state = summarize(world);
            const isOpen = expanded === world.world_id;
            const note = rowFeedback[world.world_id] ?? null;
            const busy = (action: Action) => pending[`${world.world_id}:${action}`] !== undefined;
            const driver = world.autonomy;
            const driverState = describeDriver(driver);
            // 额度用完、等续额的授权也算开着：它到边界会自己续满接着花，所以
            // 这时给的必须是「停止认知」，否则操作者没有地方收回它（COG-1 §4）。
            const driving =
              driver !== null && (driver.running || Boolean(driver.run_budget.renews_at));
            return (
              <li key={world.world_id} className="worlds-item">
                <div className="worlds-item-head">
                  <button
                    className="worlds-toggle"
                    onClick={() => setExpanded(isOpen ? null : world.world_id)}
                    aria-expanded={isOpen}
                  >
                    <span className="worlds-caret">{isOpen ? '−' : '+'}</span>
                    {world.world_id}
                  </button>
                  <span className={`worlds-badge ${state.tone}`}>{state.label}</span>
                  {world.owned ? (
                    <span className={`worlds-badge ${driverState.tone}`}>
                      {driverState.label}
                    </span>
                  ) : null}
                  <span className="worlds-meta">
                    第 {world.revision ?? '—'} 版
                    {world.dirty === true ? ' · 有未存的改动' : ''}
                    {world.durable === false ? ' · 耐久性未证实' : ''}
                  </span>
                  <span className="worlds-meta">{clockText(world.clock)}</span>
                  <span className="worlds-actions">
                    {!canOperate ? null : world.owned ? (
                      <>
                        {driving ? (
                          <button
                            className="btn"
                            disabled={busy('autonomy-stop')}
                            onClick={() => onStopAutonomy(world.world_id)}
                          >
                            {busy('autonomy-stop') ? '停止中…' : '停止认知'}
                          </button>
                        ) : (
                          <button
                            className="btn"
                            disabled={busy('autonomy-start')}
                            onClick={() => onStartAutonomy(world.world_id)}
                          >
                            {busy('autonomy-start') ? '启动中…' : '开始认知'}
                          </button>
                        )}
                        <button
                          className="btn"
                          disabled={busy('checkpoint')}
                          onClick={() => onCheckpoint(world.world_id)}
                        >
                          {busy('checkpoint') ? '存档中…' : '存一次'}
                        </button>
                        <button
                          className="btn"
                          disabled={busy('close')}
                          onClick={() => onClose(world.world_id)}
                        >
                          {busy('close') ? '关闭中…' : '关闭'}
                        </button>
                      </>
                    ) : (
                      <button
                        className="btn"
                        disabled={busy('restore')}
                        onClick={() => onRestore(world.world_id)}
                      >
                        {busy('restore') ? '恢复中…' : '恢复'}
                      </button>
                    )}
                  </span>
                </div>

                {note ? <div className={`worlds-feedback ${note.kind}`}>{note.message}</div> : null}

                {isOpen ? (
                  <dl className="worlds-detail">
                    <div>
                      <dt>会话身份</dt>
                      <dd>{world.session_id ?? '—'}</dd>
                    </div>
                    <div>
                      <dt>修订号 / 可恢复到</dt>
                      <dd>
                        {world.revision ?? '—'} / {world.durable_revision ?? '—'}
                      </dd>
                    </div>
                    <div>
                      <dt>未存的改动</dt>
                      <dd>{boolText(world.dirty, '有', '没有')}</dd>
                    </div>
                    <div>
                      <dt>本进程持有</dt>
                      <dd>
                        {world.owned
                          ? `是（pid ${world.owner?.pid ?? '—'} @ ${world.owner?.host ?? '—'}）`
                          : '否'}
                      </dd>
                    </div>
                    <div>
                      <dt>在跑</dt>
                      <dd>{boolText(world.running, '是', '否')}</dd>
                    </div>
                    <div>
                      <dt>干净关闭</dt>
                      <dd>{boolText(world.clean, '是', '否')}</dd>
                    </div>
                    <div>
                      <dt>耐久性</dt>
                      <dd>
                        {world.durable === null
                          ? '未知（这份句柄由存档恢复而来，没有携带目录同步证据）'
                          : world.durable
                            ? '已证实'
                            : '未证实：那一版在磁盘上，但掉电后可能回到上一版'}
                      </dd>
                    </div>
                    <div>
                      <dt>目录已同步</dt>
                      <dd>{boolText(world.directory_synced, '是', '否')}</dd>
                    </div>
                    <div>
                      <dt>上次成功保存</dt>
                      <dd>
                        {world.last_saved_at ?? '—'}
                        {world.last_checkpoint_reason ? `（${world.last_checkpoint_reason}）` : ''}
                      </dd>
                    </div>
                    <div>
                      <dt>模拟时钟</dt>
                      <dd>{world.clock ?? '—'}</dd>
                    </div>
                    <div>
                      <dt>接管自崩掉的拥有者</dt>
                      <dd>
                        {world.recovered_from
                          ? `pid ${world.recovered_from.pid} @ ${world.recovered_from.host}` +
                            `（${world.recovered_from.acquired_at}）`
                          : '否'}
                      </dd>
                    </div>
                    <div>
                      <dt>残留临时文件</dt>
                      <dd>{world.residue.length ? world.residue.join('、') : '无'}</dd>
                    </div>
                    <div>
                      <dt>checkpoint 策略</dt>
                      <dd>
                        {world.policy
                          ? `手动${world.policy.on_close ? ' + 干净关闭时存一次' : ''}` +
                            (world.policy.every_boundaries
                              ? `，每 ${world.policy.every_boundaries} 个边界自动存`
                              : '')
                          : '—'}
                      </dd>
                    </div>
                    <div>
                      <dt>世界时钟</dt>
                      <dd>
                        {driver === null
                          ? '—'
                          : (driver.worker_alive
                              ? (CLOCK_STATE_TEXT[driver.clock_state ?? ''] ??
                                driver.clock_state ??
                                '—')
                              : `已停（${driver.worker_exit_reason ?? '—'}）`) +
                            (driver.clock_lag_minutes !== null && driver.clock_lag_minutes !== 0
                              ? driver.clock_lag_minutes > 0
                                ? `，落后现实 ${driver.clock_lag_minutes} 分钟`
                                : `，现实时钟落后 ${-driver.clock_lag_minutes} 分钟`
                              : '') +
                            (driver.fault_since ? `　故障始于 ${driver.fault_since}` : '') +
                            `　已跑 ${driver.ticks} 轮` +
                            (driver.failures ? `，失败 ${driver.failures} 次` : '')}
                      </dd>
                    </div>
                    <div>
                      <dt>认知</dt>
                      <dd>
                        {driver === null
                          ? '—'
                          : driver.cognition_available
                            ? '可用'
                            : `不可用：${causesText(driver.cognition_causes)}`}
                      </dd>
                    </div>
                    <div>
                      <dt>时钟节拍</dt>
                      <dd>
                        {driver === null
                          ? '—'
                          : `每 ${driver.cadence.interval_seconds} 秒追一次现实时间` +
                            (driver.cadence.rate !== 1 ? `（开发倍率 ×${driver.cadence.rate}）` : '')}
                      </dd>
                    </div>
                    <div>
                      <dt>{driver?.run_budget.renewal ? '今天的额度' : '本轮额度'}</dt>
                      <dd>{driver === null ? '—' : describeBudget(driver, world.clock)}</dd>
                    </div>
                    <div>
                      <dt>世界一生的动作</dt>
                      <dd>
                        {driver === null
                          ? '—'
                          : `${driver.world_actions.committed ?? '—'} / ` +
                            `${driver.world_actions.cap ?? '未知'}` +
                            (driver.exit_reason === 'world_action_cap'
                              ? '（已到顶；这个数字跨重启和恢复都成立，'
                                + '要接着跑得先调高服务器侧的上限，再重新打开这个世界）'
                              : '')}
                      </dd>
                    </div>
                    <div>
                      <dt>下一条排期到期</dt>
                      <dd>{driver === null ? '—' : (driver.next_due_at ?? '队列是空的')}</dd>
                    </div>
                    <div>
                      <dt>上一轮</dt>
                      <dd>
                        {driver === null || driver.last_tick === null
                          ? '还没跑过'
                          : driver.last_tick.failed
                            ? `失败（${driver.last_tick_at ?? '—'}）`
                            : `${driver.last_tick.from_clock ?? '—'} → ` +
                              `${driver.last_tick.to_clock ?? '—'}，` +
                              `到期 ${driver.last_tick.due ?? 0} 条，` +
                              `处理 ${driver.last_tick.processed ?? 0} 条` +
                              (driver.last_tick.checkpoint_revision !== null
                                ? `，存下第 ${driver.last_tick.checkpoint_revision} 版`
                                : '')}
                      </dd>
                    </div>
                    <div>
                      <dt>上次时钟错误</dt>
                      <dd>{driver?.last_clock_error ?? '无'}</dd>
                    </div>
                    <div>
                      <dt>上次操作错误</dt>
                      <dd>{world.last_error ?? '无'}</dd>
                    </div>
                    <div>
                      <dt>读取状态时的错误</dt>
                      <dd>{world.error ?? '无'}</dd>
                    </div>
                    <div>
                      <dt>记录安静的分钟</dt>
                      <dd>
                        {world.quiet_time_events === null
                          ? '—'
                          : (world.quiet_time_events.record ? '记录' : '不记录') +
                            (world.quiet_time_events.since_sim
                              ? `（从 ${clockText(world.quiet_time_events.since_sim)} 起）`
                              : '（默认）')}
                        {canOperate && world.owned && world.quiet_time_events !== null ? (
                          <button
                            className="btn worlds-inline-btn"
                            disabled={busy('quiet-time')}
                            onClick={() =>
                              onQuietTime(world.world_id, !world.quiet_time_events!.record)
                            }
                          >
                            {busy('quiet-time')
                              ? '拨动中…'
                              : world.quiet_time_events.record
                                ? '不再记录'
                                : '重新记录'}
                          </button>
                        ) : null}
                      </dd>
                    </div>
                    <div>
                      <dt>存档大小</dt>
                      <dd>{archiveText(world.archive)}</dd>
                    </div>
                    <div>
                      <dt>存档位置</dt>
                      <dd className="worlds-path">{world.archive_path ?? '—'}</dd>
                    </div>
                  </dl>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
