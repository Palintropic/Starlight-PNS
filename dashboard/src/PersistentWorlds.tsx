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
import { useCallback, useEffect, useRef, useState } from 'react';
import {
  ApiError,
  checkpointPersistentWorld,
  closePersistentWorld,
  createPersistentWorld,
  fetchPersistentWorlds,
  fetchReloadStatus,
  fetchWorldScenes,
  restorePersistentWorld,
  type PersistentWorldStatus,
} from './api';
import './worlds.css';

type Action = 'create' | 'restore' | 'checkpoint' | 'close';

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

const clockText = (iso: string | null): string =>
  iso === null ? '—' : iso.replace('T', ' ').slice(0, 16);

const boolText = (value: boolean | null, yes: string, no: string): string =>
  value === null ? '未知' : value ? yes : no;

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

  const onClose = (worldId: string) => {
    // 关闭会停掉一个正在跑的世界，所以先确认。
    const confirmed = window.confirm(
      `关闭世界「${worldId}」？\n\n` +
        '会先停止接受新的行动、等在跑的事务落定、写下最后一份存档，然后归还所有权。' +
        '存不下去时不会假装关干净了，世界会继续开着。',
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

  return (
    <div className="worlds">
      <div className="worlds-head">
        <div>
          <h2>持久世界</h2>
          <p className="worlds-note">
            每个世界都有自己的权威状态和一把独占锁。恢复只能回到
            <strong>最后一次成功的 checkpoint</strong>
            ——它之后的内存工作在进程被强杀时会丢，这里没有 WAL。
          </p>
        </div>
        <button className="btn btn-approve" onClick={refresh}>
          刷新
        </button>
      </div>

      {loadError ? <div className="worlds-error">{loadError}</div> : null}

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
                className={`worlds-chip ${newCharacters.includes(id) ? 'on' : ''}`}
                onClick={() => toggleCharacter(id)}
              >
                {id}
              </button>
            ))}
          </div>
        </div>
        <div className="worlds-create-actions">
          <button
            className="btn btn-approve"
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
                  <span className="worlds-meta">
                    第 {world.revision ?? '—'} 版
                    {world.dirty === true ? ' · 有未存的改动' : ''}
                    {world.durable === false ? ' · 耐久性未证实' : ''}
                  </span>
                  <span className="worlds-meta">{clockText(world.clock)}</span>
                  <span className="worlds-actions">
                    {world.owned ? (
                      <>
                        <button
                          className="btn btn-approve"
                          disabled={busy('checkpoint')}
                          onClick={() => onCheckpoint(world.world_id)}
                        >
                          {busy('checkpoint') ? '存档中…' : '存一次'}
                        </button>
                        <button
                          className="btn btn-reject"
                          disabled={busy('close')}
                          onClick={() => onClose(world.world_id)}
                        >
                          {busy('close') ? '关闭中…' : '关闭'}
                        </button>
                      </>
                    ) : (
                      <button
                        className="btn btn-approve"
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
                      <dt>上次操作错误</dt>
                      <dd>{world.last_error ?? '无'}</dd>
                    </div>
                    <div>
                      <dt>读取状态时的错误</dt>
                      <dd>{world.error ?? '无'}</dd>
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
