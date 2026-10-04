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

// ─── 账户与会话（DEPLOY-1 / AUTH-1）────────────────────────────────────
//
// 凭据**只**在提交那一刻经过浏览器，换成一张 HttpOnly Cookie 之后就不再出现
// 在前端任何地方：不写 localStorage、不进 URL、不进构建产物。所以这里没有、
// 也不该有任何"记住密码"的东西。
//
// `PNS_ADMIN_TOKEN` 不在这条路上：那是 break-glass / 自动化用的 bearer，
// 浏览器登录只认用户名 + 密码。

/** 权限名。服务端的 scope 词汇，前端只用来决定显示什么。 */
export const SCOPE_READ = 'read';
export const SCOPE_OPERATE = 'operate';
export const SCOPE_ACCOUNTS = 'accounts:manage';

/** 当前请求认下来的主体。`kind: 'service'` 是 break-glass 或开放的开发服务器。 */
export interface AuthPrincipal {
  principal_id: string;
  username: string;
  kind: string;
  role: string;
  scopes: string[];
  /** 'session' | 'bearer' | 'open-development' */
  via: string;
}

export interface AuthSession {
  /** 'production' | 'development' */
  mode: string;
  /** 这台服务器要不要凭据。false 表示它是一台没配账户也没配 token 的开发服务器。 */
  auth_required: boolean;
  authenticated: boolean;
  principal: AuthPrincipal | null;
}

export const fetchAuthSession = (): Promise<AuthSession> =>
  fetch('/api/auth/session').then((res) => json(res, { authRoute: true }));

export const login = (username: string, password: string): Promise<AuthSession> =>
  fetch('/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  }).then((res) => json(res, { authRoute: true }));

export const logout = (): Promise<AuthSession> =>
  fetch('/api/auth/logout', { method: 'POST' }).then((res) => json(res, { authRoute: true }));

/** 改自己的密码。**成功之后当前会话也没了**，服务端不会补发新的。 */
export const changePassword = (
  currentPassword: string,
  newPassword: string,
): Promise<AuthSession> =>
  fetch('/api/auth/password', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
  }).then((res) => json(res, { authRoute: true }));

// ─── 账户管理（AUTH-1，需要 accounts:manage）──────────────────────────

export interface Account {
  principal_id: string;
  username: string;
  kind: string;
  role: string;
  scopes: string[];
  enabled: boolean;
  created_at: string;
  updated_at: string;
  /** 这次操作踢掉了几张会话。只在改权威/重置密码的响应里出现。 */
  revoked_sessions?: number;
}

export interface AuditRecord {
  sequence: number;
  occurred_at: string;
  actor_principal_id: string | null;
  target_principal_id: string | null;
  actor_username: string | null;
  target_username: string | null;
  action: string;
  result: string;
  detail: Record<string, unknown>;
}

const accountPath = (principalId: string) =>
  `/api/accounts/${encodeURIComponent(principalId)}`;

export const fetchAccounts = (): Promise<{ users: Account[] }> =>
  fetch('/api/accounts').then((res) => json(res));

export const fetchAuditRecords = (limit = 200): Promise<{ records: AuditRecord[] }> =>
  fetch(`/api/accounts/audit?limit=${limit}`).then((res) => json(res));

export const createAccount = (
  username: string,
  password: string,
  role: string,
): Promise<Account> =>
  fetch('/api/accounts', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password, role }),
  }).then((res) => json(res));

export const setAccountRole = (principalId: string, role: string): Promise<Account> =>
  fetch(`${accountPath(principalId)}/role`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ role }),
  }).then((res) => json(res));

export const setAccountEnabled = (
  principalId: string,
  enabled: boolean,
): Promise<Account> =>
  fetch(`${accountPath(principalId)}/enabled`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled }),
  }).then((res) => json(res));

export const resetAccountPassword = (
  principalId: string,
  password: string,
): Promise<Account> =>
  fetch(`${accountPath(principalId)}/password`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ password }),
  }).then((res) => json(res));

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

/** 时钟 worker 的节拍与单次 Start 的额度。服务器侧配置，浏览器只能读。 */
export interface WorldDriverCadence {
  interval_seconds: number;
  /** 模拟时间相对现实时间的倍率。生产恒为 1。 */
  rate: number;
  max_steps_per_iteration: number;
  fault_threshold: number;
  stop_timeout_seconds: number;
  max_activations_per_run: number;
}

/** **这一份**授权的额度。
 *
 * 不续额（`renewal` 为 null）时一次 Start 一份，用完了认知以 run_budget_exhausted
 * 关闭，再按一次 Start 就重置。带续额策略时是"每个世界日最多 limit 次"：
 * used / remaining 是这一天的，`renews_at` 到了续满，直到 Stop 或重启（COG-1）。
 */
export interface WorldRunBudget {
  /** 三个都为 null = 不限额（Start 没给上限）。还没 Start 时是 Start 会给的配置值。 */
  limit: number | null;
  used: number | null;
  remaining: number | null;
  /** 续额策略 id；null = 这份额度不续。 */
  renewal?: string | null;
  /** 下一次续满的模拟时刻。Stop / 还没 Start / 不续额时为 null。有值不承诺到时能调用模型。 */
  renews_at?: string | null;
  /** 这一天额度的起点（Start 或上一次续额）。 */
  day_start?: string | null;
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

/** 时钟 worker 与认知此刻的样子（WORLD-1）。
 *
 * 它跟 P12 的 `running` 是两件事：`running` 说的是"这个世界的运行时还接不接受
 * 写入"，`state` 说的是"服务器会不会替角色花模型调用"。running=true 而
 * state='stopped' 就是"时间在走、没人做决定"——新建和恢复之后的默认状态，因为
 * 自动模型调用是 opt-in。时间本身走没走看 `clock_state`。
 *
 * Stop 是一次认知时间线转换，不等任何线程，所以 `stopping` 恒为 false。
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
  worker_alive: boolean;
  worker_exit_reason: string | null;
  /** catching_up / healthy / faulted */
  clock_state: string | null;
  /** 现实时间换算出的此刻比世界时钟超前多少模拟分钟（负数：现实时钟落后）。 */
  clock_lag_minutes: number | null;
  last_clock_progress: string | null;
  fault_since: string | null;
  last_clock_error: string | null;
  cognition_available: boolean;
  /** 认知不可用的全部原因（not_started / operator_paused / fault …）。 */
  cognition_causes: string[];
}

/** 「记录安静的分钟」开关（WORLD-1 存档增长设计 §3）。 */
export interface QuietTimeEvents {
  /** true = 每个时钟步都记一条时间事件（默认）；false = 安静的步不记。 */
  record: boolean;
  /** 当前值从哪一刻起生效；从没拨过是 null。 */
  since_sim: string | null;
  since_wall: string | null;
  flips: number;
}

/** 存档在磁盘上占了多少、分成了几卷。 */
export interface ArchiveFootprint {
  total_bytes: number;
  world_bytes: number | null;
  segments: number;
  sealed_events: number;
  sealed_bytes: number;
  active_events: number;
}

/** 一个登记过作息、此刻不能被作息驱动的居民（CONTENT-4）。 */
export interface HeldSubject {
  subject: string;
  character_id: string;
  /** pending / declined / deferred / missing_definition / adopted_absent */
  reason: string;
  conflict_id: string | null;
  conflict_status: string | null;
}

/** 世界打开着、却因为作息内容待决而不运行。 */
export interface WorldHeld {
  reason: string;
  subjects: HeldSubject[];
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
  /** 作息内容待决、运行时已终局停止。正常世界和没开着的世界都是 null。 */
  held: WorldHeld | null;
  clock: string | null;
  archive_path: string | null;
  boundaries_since_checkpoint: number | null;
  policy: WorldCheckpointPolicy | null;
  /** 没有存档、或存档读不出来时是 null。 */
  quiet_time_events: QuietTimeEvents | null;
  archive: ArchiveFootprint | null;
  /** `null` = 这个世界没有在本进程里开着。开着的世界一定有时钟 worker。 */
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

/** 按正式世界的规则开局（WORLD-1「夜明け前」）。请求体为空：身份、时区、开局时刻都在服务器侧。 */
export const bootstrapFormalWorld = (worldId: string): Promise<PersistentWorldStatus> =>
  fetch(`${worldPath(worldId)}/bootstrap`, { method: 'POST' }).then((res) => json(res));

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

/** 拨「记录安静的分钟」。只影响之后；拨动本身记进运维账本并立即存盘。 */
export const setQuietTimeEvents = (
  worldId: string,
  record: boolean,
): Promise<PersistentWorldStatus> =>
  fetch(`${worldPath(worldId)}/quiet-time-events`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ record }),
  }).then((res) => json(res));

// ── 世界概览（只读）─────────────────────────────────────────────────────
//
// World History 这一层：客观记录，不是任何居民的经历或记忆。只有开着的世界才有。

export type ResidentAvailability = 'available' | 'busy' | 'asleep';

export interface OverviewResident {
  id: string;
  name: string;
  /** 所属团的 unit id；内容包里已经没有这个角色时是 null。 */
  unit: string | null;
  /** 没有物理位置（只挂在频道上）时是 null。 */
  location_id: string | null;
  /** 模拟时钟，本地世界时间，无时区。 */
  activity: { kind: string; since: string };
  availability: ResidentAvailability;
  /** 远程在场的频道。不是第二个物理位置。 */
  channels: string[];
}

export interface OverviewLocation {
  id: string;
  name: string;
  parent_id: string | null;
}

export interface OverviewChannel {
  id: string;
  name: string;
}

export interface OverviewEvent {
  /** 在整份世界历史里的序号（含被略去的时间推进事件）。 */
  seq: number;
  event_id: string;
  type: string;
  at: string;
  scope: string;
  actor: string | null;
  participants: string[];
  location_id: string | null;
  channel_id: string | null;
  payload: Record<string, unknown>;
}

export interface WorldOverview {
  world_id: string;
  clock: string;
  revision: number;
  autonomy: WorldDriverStatus | null;
  residents: OverviewResident[];
  locations: OverviewLocation[];
  channels: OverviewChannel[];
  /** 最近的非时间推进事件，旧的在前。 */
  events: OverviewEvent[];
  total_events: number;
  first_event_at: string | null;
}

export const fetchWorldOverview = (worldId: string, limit = 200): Promise<WorldOverview> =>
  fetch(`${worldPath(worldId)}/overview?limit=${limit}`).then((res) => json(res));
