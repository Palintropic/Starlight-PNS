import type { Decision, DecisionMap, DecisionValue, Turn } from './types';
import type { FactsResponse, ScenesMap } from './world/types';

/** 一次失败的请求。`category` 是后端给的稳定类别，UI 可以据此决定说什么。 */
export class ApiError extends Error {
  readonly status: number;
  readonly category: string | null;

  constructor(message: string, status: number, category: string | null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.category = category;
  }
}

/** 从 FastAPI 的 `detail` 里取一句能给人看的话。
 *
 * detail 有三种形状，三种都要认：旧路由给字符串；持久世界路由给
 * `{category, message}`；请求体校验失败给一个数组。认不出来就退回状态行，
 * 绝不把 `[object Object]` 摆到后台上。
 */
function describe(detail: unknown): { message: string | null; category: string | null } {
  if (typeof detail === 'string' && detail) return { message: detail, category: null };
  if (Array.isArray(detail)) {
    const parts = detail
      .map((item) =>
        item && typeof item === 'object' && typeof (item as { msg?: unknown }).msg === 'string'
          ? (item as { msg: string }).msg
          : null,
      )
      .filter((part): part is string => part !== null);
    return { message: parts.length ? parts.join('；') : null, category: 'invalid_request' };
  }
  if (detail && typeof detail === 'object') {
    const record = detail as { message?: unknown; category?: unknown };
    return {
      message: typeof record.message === 'string' ? record.message : null,
      category: typeof record.category === 'string' ? record.category : null,
    };
  }
  return { message: null, category: null };
}

/** 会话失效时全局广播一次。
 *
 * 每个调用点各自处理 401 的话，总会漏掉一两个，于是后台会停在一个"什么都
 * 加载不出来"的页面上，而真正的原因（会话过期了）没人说出来。广播让这件事
 * 只有一处判断、一处反应。
 */
export const UNAUTHENTICATED_EVENT = 'pns:unauthenticated';

async function json<T>(res: Response, options: { authRoute?: boolean } = {}): Promise<T> {
  if (!res.ok) {
    // 服务器出错时正文不一定是 JSON（代理的 502、断掉的连接、静态兜底页）。
    // 解析失败就用状态行，别让一次 SyntaxError 盖住真正的错误。
    const body = await res.json().catch(() => null);
    const { message, category } = describe(body && (body as { detail?: unknown }).detail);
    // 登录接口自己的 401 是"这次密码不对"，不是"会话没了"——广播它会把用户
    // 从登录框上弹走，然后什么也没发生。
    //
    // 判据是调用点显式传进来的，不是从 res.url 反推的：`Response.url` 在
    // 某些环境下是空串，而一个"多数时候对"的判据会在最难复现的那一次出错。
    if (res.status === 401 && !options.authRoute) {
      window.dispatchEvent(new CustomEvent(UNAUTHENTICATED_EVENT));
    }
    throw new ApiError(message || `${res.status} ${res.statusText}`, res.status, category);
  }
  return res.json() as Promise<T>;
}

// ─── 操作者会话（DEPLOY-1）──────────────────────────────────────────────
//
// 管理凭据**只**在登录那一刻经过浏览器，换成一张 HttpOnly Cookie 之后就不再
// 出现在前端任何地方：不写 localStorage、不进 URL、不进构建产物。所以这里
// 没有、也不该有任何"记住 token"的东西。

export interface AuthSession {
  /** 'production' | 'development' */
  mode: string;
  /** 这台服务器要不要凭据。false 表示它是一台没配 token 的开发服务器。 */
  auth_required: boolean;
  authenticated: boolean;
}

export const fetchAuthSession = (): Promise<AuthSession> =>
  fetch('/api/auth/session').then((res) => json(res, { authRoute: true }));

export const login = (token: string): Promise<AuthSession> =>
  fetch('/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token }),
  }).then((res) => json(res, { authRoute: true }));

export const logout = (): Promise<AuthSession> =>
  fetch('/api/auth/logout', { method: 'POST' }).then((res) => json(res, { authRoute: true }));

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

// ─── 配置重载边界 ──────────────────────────────────────────────────────

export interface RegistrySummary {
  revision: number;
  built_at: string;
  pack: string;
  scene_count: number;
  default_scene: string;
  fact_count: number;
  character_count: number;
  ready_characters: string[];
}

export interface ReloadResult {
  status: 'ok' | 'failed' | 'busy';
  revision: number;
  finished_at: string;
  stopped_sessions: string[];
  pending_sessions: string[];
  error: string | null;
  registry: RegistrySummary | null;
}

export interface ReloadStatus {
  reloading: boolean;
  stop_timeout: number;
  accepting_sessions: boolean;
  live_sessions: string[];
  registry: RegistrySummary | null;
  last_reload: ReloadResult | null;
}

export const fetchReloadStatus = (): Promise<ReloadStatus> =>
  fetch('/api/config/reload').then((res) => json(res));

export const reloadConfig = (): Promise<ReloadResult> =>
  fetch('/api/config/reload', { method: 'POST' }).then((res) => json(res));

// ─── 持久世界（WEB-1）──────────────────────────────────────────────────
//
// 字段名与后端 P12 的状态词汇一一对应，一个都不为了 UI 好看而改名。
// `null` 与 `false` 不是一回事：本进程没开着的世界，它的 running / dirty /
// clean 是**不知道**（null），不是"否"。

export interface WorldOwner {
  world_id: string;
  pid: number;
  host: string;
  acquired_at: string;
  renewed_at: string;
  state: string;
}

export interface WorldCheckpointPolicy {
  every_boundaries: number | null;
  min_interval_seconds: number;
  on_close: boolean;
}

/** 驱动的节拍与单次 Start 的额度。服务器侧配置，浏览器只能读。 */
export interface WorldDriverCadence {
  tick_minutes: number;
  interval_seconds: number;
  stop_timeout_seconds: number;
  max_activations_per_run: number;
}

/** **这一轮** Start 的额度。用完了驱动自己停下，再按一次 Start 就重置。 */
export interface WorldRunBudget {
  limit: number;
  used: number;
  remaining: number;
}

/** 这个世界**一生**的动作用量与上限。
 *
 * 用量从耐久的 Agency 日志推导，所以重启和恢复都换不来新的额度。`cap` 为
 * null 表示读不出上限（不知道），不表示没有上限。
 */
export interface WorldActionUsage {
  committed: number | null;
  cap: number | null;
  remaining: number | null;
}

/** 上一次 tick 的样子。失败的那次只有 `failed`。 */
export interface WorldDriverTick {
  failed: boolean;
  from_clock: string | null;
  to_clock: string | null;
  minutes: number | null;
  due: number | null;
  processed: number | null;
  outcomes: Record<string, number>;
  checkpoint_revision: number | null;
}

/** 自主驱动此刻的样子（MVP-1）。
 *
 * 它跟 P12 的 `running` 是两件事：`running` 说的是"这个世界的运行时还接不接受
 * 写入"，`state` 说的是"服务器此刻在不在推它"。running=true 而 state='stopped'
 * 就是"开着但没人推"——新建和恢复之后的默认状态，因为自动模型调用是 opt-in。
 *
 * `state === 'stopping'` 的意思很具体：**还没停干净**，当前那一轮仍然可能落地
 * 一次提交。UI 不许把它显示成"已停止"。
 */
export interface WorldDriverStatus {
  world_id: string;
  state: string;
  running: boolean;
  stopping: boolean;
  stopped: boolean;
  stop_reason: string | null;
  exit_reason: string | null;
  ticks: number;
  failures: number;
  consecutive_failures: number;
  last_error: string | null;
  last_tick_at: string | null;
  last_tick: WorldDriverTick | null;
  next_due_at: string | null;
  cadence: WorldDriverCadence;
  /** 按 Start 重置的那道边界。 */
  run_budget: WorldRunBudget;
  /** 跟着世界一辈子的那道边界。 */
  world_actions: WorldActionUsage;
}

export interface PersistentWorldStatus {
  world_id: string;
  session_id: string | null;
  revision: number | null;
  durable_revision: number | null;
  dirty: boolean | null;
  closed: boolean | null;
  clean: boolean | null;
  /** 本进程此刻持有这个世界。它不回答"别的进程是不是拥有它"。 */
  owned: boolean;
  owner: WorldOwner | null;
  /** 上一个拥有者**崩掉**时留下的记录；干净释放过的世界这里是 null。 */
  recovered_from: WorldOwner | null;
  last_saved_at: string | null;
  last_checkpoint_reason: string | null;
  durable: boolean | null;
  directory_synced: boolean | null;
  last_error: string | null;
  error: string | null;
  residue: string[];
  running: boolean | null;
  stop_reason: string | null;
  clock: string | null;
  archive_path: string | null;
  boundaries_since_checkpoint: number | null;
  policy: WorldCheckpointPolicy | null;
  /** `null` = 这台服务器从来没为这个世界起过驱动；跟"起过、现在停着"不是一回事。 */
  autonomy: WorldDriverStatus | null;
}

const worldPath = (worldId: string) =>
  `/api/persistent-worlds/${encodeURIComponent(worldId)}`;

export const fetchPersistentWorlds = (): Promise<{ worlds: PersistentWorldStatus[] }> =>
  fetch('/api/persistent-worlds').then((res) => json(res));

export const fetchPersistentWorld = (worldId: string): Promise<PersistentWorldStatus> =>
  fetch(worldPath(worldId)).then((res) => json(res));

export const createPersistentWorld = (
  worldId: string,
  scene: string,
  characters: string[],
): Promise<PersistentWorldStatus> =>
  fetch('/api/persistent-worlds', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ world_id: worldId, scene, characters }),
  }).then((res) => json(res));

export const restorePersistentWorld = (worldId: string): Promise<PersistentWorldStatus> =>
  fetch(`${worldPath(worldId)}/restore`, { method: 'POST' }).then((res) => json(res));

export const checkpointPersistentWorld = (worldId: string): Promise<PersistentWorldStatus> =>
  fetch(`${worldPath(worldId)}/checkpoint`, { method: 'POST' }).then((res) => json(res));

export const closePersistentWorld = (worldId: string): Promise<PersistentWorldStatus> =>
  fetch(`${worldPath(worldId)}/close`, { method: 'POST' }).then((res) => json(res));

/** 开始自动推这个世界。**唯一**会让服务器自己花 API 额度的入口。 */
export const startWorldAutonomy = (worldId: string): Promise<PersistentWorldStatus> =>
  fetch(`${worldPath(worldId)}/autonomy/start`, { method: 'POST' }).then((res) => json(res));

/** 请驱动暂停。可重启，不关闭世界。 */
export const stopWorldAutonomy = (worldId: string): Promise<PersistentWorldStatus> =>
  fetch(`${worldPath(worldId)}/autonomy/stop`, { method: 'POST' }).then((res) => json(res));
