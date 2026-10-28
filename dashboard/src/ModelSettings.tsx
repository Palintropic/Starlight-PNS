// dashboard/src/ModelSettings.tsx — 模型卡片（CFG-1 卡片补丁，需要 accounts:manage）
//
// 一排卡片，每张是一家配好的服务商；正在生成用的那张亮着，点一下就切过去。
// 判分平时跟着生成走，也可以指定任意一张卡片（含 .env、含当前生成那张）并覆盖
// 模型名。.env 是内建卡片，只读。
//
// 几件刻意的事：
//
//   * **卡片列表只装服务端的公开投影。** 列表直接渲染 view.settings.cards，不往里
//     塞任何草稿；末四位提示也只用服务端给的 token_hint。
//   * **token 只活在编辑表单的一个 state 里。** 提交前同步清空（失败要重填），取消、
//     关表单、换编辑对象时也清空；不回填、不进 localStorage、不打日志。
//   * **只在成功响应之后才点亮卡片。** 失败（409、回退、隔离、网络说不清）一律重新
//     拉一次权威状态，文案照服务端那句话说——不是所有 409 都等于"先关世界"。
//   * **新建只保存、不启用**："添加"不会偷偷换线路。默认顺手建一个同 id 的计费账户
//     （服务端一次编排）；账户建好了、卡片没写成时，表单改成挂到那个账户上重试。
//   * **倍率属于账户。** 卡片表单里只显示，要改去下面的计费账户表——改了会影响挂在
//     这个账户下的所有卡片。
//   * **.env 凭据不完整时常驻提示不能直接回滚**（§9）：旧镜像只认 .env。
import { useCallback, useEffect, useState } from 'react';
import {
  ApiError,
  clearModelCards,
  createModelCard,
  deleteModelCard,
  fetchBillingAccounts,
  fetchProviderSettings,
  fetchUsageSummary,
  switchActiveCards,
  updateModelCard,
  type AccountBalance,
  type ActiveCards,
  type ApiFormat,
  type BillingAccount,
  type EnvCard,
  type JudgeThinking,
  type ModelCard,
  type NewAccountInput,
  type ProviderSettingsView,
} from './api';
import BillingAccounts from './BillingAccounts';
import './accounts.css';
import './modelCards.css';

type Feedback = { kind: 'ok' | 'error'; message: string } | null;

const THINKING_LABEL: Record<JudgeThinking, string> = {
  auto: '自动（官方 MiMo 地址才关）',
  off: '关（经转发地址用 MiMo 时选这个）',
  provider: '交给服务商默认',
};

// 模板只预填协议和 Base URL（和余额接口无关）。
const TEMPLATES: { key: string; label: string; kind: string; format: ApiFormat; baseUrl: string }[] = [
  { key: 'mimo', label: 'MiMo 官方', kind: 'mimo', format: 'anthropic', baseUrl: 'https://api.xiaomimimo.com/anthropic' },
  { key: 'deepseek', label: 'DeepSeek', kind: 'deepseek', format: 'openai', baseUrl: 'https://api.deepseek.com' },
  { key: 'openrouter', label: 'OpenRouter', kind: 'openrouter', format: 'openai', baseUrl: 'https://openrouter.ai/api/v1' },
  { key: 'other', label: '其它', kind: 'other', format: 'anthropic', baseUrl: '' },
];

/** 编辑表单里除 token 以外的东西。token 单独一个 state。 */
interface Editor {
  mode: 'new' | 'edit';
  cardId: string | null;
  name: string;
  api_format: ApiFormat;
  base_url: string;
  model: string;
  judge_thinking: JudgeThinking;
  /** 新建时：'new' = 顺手建同 id 账户；'existing' = 挂到 account 上。编辑时总是 existing。 */
  accountMode: 'new' | 'existing';
  account: string;
  newAccount: NewAccountInput;
}

const blankEditor = (): Editor => ({
  mode: 'new',
  cardId: null,
  name: '',
  api_format: 'anthropic',
  base_url: '',
  model: '',
  judge_thinking: 'auto',
  accountMode: 'new',
  account: '',
  newAccount: { kind: 'other', currency: 'CNY', multiplier: '1' },
});

const editorOf = (card: ModelCard): Editor => ({
  ...blankEditor(),
  mode: 'edit',
  cardId: card.id,
  name: card.name,
  api_format: card.api_format,
  base_url: card.base_url,
  model: card.model,
  judge_thinking: card.judge_thinking,
  accountMode: 'existing',
  account: card.account,
});

const hostOf = (url: string | undefined): string => {
  if (!url) return '';
  try {
    return new URL(url).hostname || url;
  } catch {
    return url;
  }
};

const sameJudge = (a: ActiveCards['judge'], b: ActiveCards['judge']) =>
  JSON.stringify(a) === JSON.stringify(b);

/** 卡片上的余额：按账户币种和估算状态说，不拼字符串猜。 */
function balanceText(balance: AccountBalance | undefined): { text: string; low: boolean } | null {
  if (!balance) return null;
  if (balance.currency === 'CNY' && balance.estimated !== null) {
    return { text: `¥${balance.estimated.toFixed(2)}`, low: balance.low };
  }
  if (balance.anchor && balance.anchor_problem === null) {
    // 非人民币账户只记锚点，不相减。
    const sign = balance.currency === 'USD' ? 'US$' : '';
    return { text: `${sign}${balance.anchor.balance.toFixed(2)}（锚点）`, low: false };
  }
  return null;
}

function describeError(e: unknown): string {
  if (!(e instanceof Error)) return '操作失败';
  if (e instanceof ApiError && e.category === 'world_open') {
    const worlds = e.detail?.worlds;
    return Array.isArray(worlds) && worlds.length ? `${e.message}（开着的：${worlds.join('、')}）` : e.message;
  }
  return e.message;
}

export default function ModelSettings() {
  const [view, setView] = useState<ProviderSettingsView | null>(null);
  const [accounts, setAccounts] = useState<Record<string, BillingAccount>>({});
  const [balances, setBalances] = useState<Record<string, AccountBalance> | null>(null);
  const [balancesUnavailable, setBalancesUnavailable] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<Feedback>(null);
  const [busy, setBusy] = useState(false);

  const [editor, setEditor] = useState<Editor | null>(null);
  const [editorAdvanced, setEditorAdvanced] = useState(false);
  const [token, setToken] = useState('');

  const [judgeDraft, setJudgeDraft] = useState<ActiveCards['judge'] | null>(null);
  const [judgeAdvanced, setJudgeAdvanced] = useState(false);
  const [showAdvanced, setShowAdvanced] = useState(false);

  const adopt = useCallback((next: ProviderSettingsView) => {
    setView(next);
    setJudgeDraft(next.settings.active.judge);
  }, []);

  const load = useCallback(() => {
    fetchProviderSettings()
      .then(adopt)
      .catch((e) => setLoadError(e instanceof Error ? e.message : '加载模型设置失败'));
    fetchBillingAccounts()
      .then((payload) => setAccounts(payload.accounts))
      .catch((e) => setLoadError(e instanceof Error ? e.message : '加载计费账户失败'));
    fetchUsageSummary(1)
      .then((summary) => {
        setBalances(summary.balances ?? null);
        setBalancesUnavailable(Boolean(summary.balances_unavailable));
      })
      .catch(() => setBalances(null));
  }, [adopt]);

  useEffect(load, [load]);

  const refresh = () => {
    setLoadError(null);
    load();
  };

  const closeEditor = () => {
    setToken('');
    setEditor(null);
    setEditorAdvanced(false);
  };

  const openEditor = (next: Editor) => {
    // 换编辑对象也清掉 token：不能把给 A 填的 token 带到 B 上。
    setToken('');
    setEditorAdvanced(false);
    setEditor(next);
  };

  /** 跑一次写操作：成功才采用响应；失败重新拉权威状态。返回是否成功。 */
  const run = async (action: () => Promise<ProviderSettingsView>, done: string): Promise<boolean> => {
    setBusy(true);
    setFeedback(null);
    try {
      const next = await action();
      adopt(next);
      setFeedback({ kind: 'ok', message: `${done}（配置 revision ${next.config_revision}）` });
      return true;
    } catch (e) {
      setFeedback({ kind: 'error', message: describeError(e) });
      refresh();
      return false;
    } finally {
      setBusy(false);
    }
  };

  const settings = view?.settings ?? null;
  const locked = busy || Boolean(view?.quarantined);

  const nameOf = (id: string): string => {
    if (id === 'env') return '.env';
    return settings?.cards.find((card) => card.id === id)?.name ?? id;
  };

  const activate = (id: string) => {
    if (!settings || locked || settings.active.generation === id) return;
    // 只换生成卡片；独立判分的设置原样带上（审查 F10）。
    void run(
      () => switchActiveCards({ generation: id, judge: settings.active.judge }),
      `已切换到「${nameOf(id)}」`,
    );
  };

  const applyJudge = () => {
    if (!settings || judgeDraft === null) return;
    void run(
      () => switchActiveCards({ generation: settings.active.generation, judge: judgeDraft }),
      '判分设置已生效',
    );
  };

  const onSubmitCard = async (event: React.FormEvent) => {
    event.preventDefault();
    if (editor === null) return;
    // 先从 state 擦掉，再把局部变量交给请求：token 不在任何一次重渲染里留着。
    const sent = token;
    setToken('');
    const fields = {
      name: editor.name.trim(),
      api_format: editor.api_format,
      base_url: editor.base_url.trim(),
      model: editor.model.trim(),
      token: sent,
      judge_thinking: editor.judge_thinking,
    };
    if (editor.mode === 'edit' && editor.cardId !== null) {
      const id = editor.cardId;
      const ok = await run(() => updateModelCard(id, { ...fields, account: editor.account }), `已保存「${fields.name}」`);
      if (ok) closeEditor();
      return;
    }
    setBusy(true);
    setFeedback(null);
    try {
      const result =
        editor.accountMode === 'new'
          ? await createModelCard(fields, editor.newAccount)
          : await createModelCard({ ...fields, account: editor.account });
      adopt(result);
      fetchBillingAccounts().then((payload) => setAccounts(payload.accounts)).catch(() => undefined);
      setFeedback({
        kind: 'ok',
        message: `已添加「${fields.name}」，还没启用；点它就切过去${result.account ? '（同时建了同名计费账户）' : ''}`,
      });
      closeEditor();
    } catch (e) {
      const account = e instanceof ApiError && typeof e.detail?.account === 'string' ? e.detail.account : null;
      if (account !== null && (e as ApiError).category !== 'settings_quarantined') {
        // 账户已经建好、卡片没写成：改成挂到这个账户上，重填 token 再提交一次。
        setEditor((current) => (current ? { ...current, accountMode: 'existing', account } : current));
        setFeedback({
          kind: 'error',
          message: `${describeError(e)}——计费账户 ${account} 已经在了，表单已改成挂到它上面，重填 token 再提交一次。`,
        });
      } else {
        setFeedback({ kind: 'error', message: describeError(e) });
      }
      refresh();
    } finally {
      setBusy(false);
    }
  };

  const onDelete = () => {
    if (editor?.cardId == null) return;
    const id = editor.cardId;
    if (!window.confirm(`删掉卡片「${nameOf(id)}」？它的计费账户和历史账都还在。`)) return;
    void run(() => deleteModelCard(id), `已删除「${nameOf(id)}」`).then((ok) => ok && closeEditor());
  };

  const onClearAll = () => {
    if (!window.confirm('删掉全部卡片（连同里面存的 token），回到 .env？计费账户和余额锚点不受影响。')) return;
    closeEditor();
    void run(clearModelCards, '已删掉全部卡片，现在用的是 .env');
  };

  const selectableAccounts = (current: string) =>
    Object.values(accounts)
      .filter((account) => !account.retired_at || account.id === current)
      .sort((a, b) => a.id.localeCompare(b.id));

  const renderCard = (card: ModelCard | EnvCard) => {
    const on = settings?.active.generation === card.id;
    const builtin = card.builtin;
    const balance = balanceText(balances?.[card.account]);
    const hint = builtin
      ? card.available
        ? '来自 .env · 只读'
        : `.env 不可用：${card.problem ?? ''}`
      : hostOf(card.base_url);
    return (
      <div
        key={card.id}
        className={`mcards-card tilt${on ? ' on' : ''}`}
        role="button"
        tabIndex={0}
        aria-pressed={on}
        aria-label={`${card.name}${on ? '（使用中）' : ''}`}
        aria-disabled={locked}
        onClick={() => activate(card.id)}
        onKeyDown={(e) => {
          // 只认卡片自己身上的按键：里面"编辑"按钮的 Enter / 空格不冒泡成一次切换。
          if (e.target !== e.currentTarget) return;
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            activate(card.id);
          }
        }}
      >
        <span className="mcards-name">{card.name}</span>
        <span className="mcards-meta">{card.model ?? '—'}</span>
        <span className="mcards-meta">{hint}</span>
        {balance ? (
          <span className={`mcards-balance${balance.low ? ' mcards-balance-low' : ''}`}>{balance.text}</span>
        ) : (
          <span className="mcards-balance mcards-balance-none">余额 —</span>
        )}
        <span className="mcards-foot">
          <span className="mcards-state">{on ? '使用中' : '点一下启用'}</span>
          {builtin ? null : (
            <button
              type="button"
              className="command mcards-edit"
              disabled={locked}
              onClick={(e) => {
                e.stopPropagation();
                openEditor(editorOf(card));
              }}
            >
              编辑
            </button>
          )}
        </span>
      </div>
    );
  };

  const editingCard =
    editor?.mode === 'edit' ? settings?.cards.find((card) => card.id === editor.cardId) ?? null : null;
  const editorAccount = editor ? accounts[editor.account] : undefined;
  const allCards: (ModelCard | EnvCard)[] = settings ? [...settings.cards, settings.env_card] : [];
  const judgeChanged = settings !== null && judgeDraft !== null && !sameJudge(judgeDraft, settings.active.judge);

  return (
    <div className="mcards entrance">
      <div className="accounts-head">
        <p className="accounts-note">
          切换、添加、编辑、删除卡片都要先<strong>关闭所有世界</strong>，做完再恢复；恢复时会照常检查内容采用、补跑离线时间。
          token 提交后不回显，也不会保存在浏览器里。
        </p>
        <button className="btn" onClick={refresh}>刷新</button>
      </div>

      {loadError ? <div className="accounts-error">{loadError}</div> : null}
      {view?.quarantined ? (
        <div className="accounts-error" role="alert">
          模型设置处于隔离状态：上一次保存没能确认结果。先在服务器上用 scripts/provider_resolve.py
          决定保留哪一份，期间不能切换或修改卡片。
        </div>
      ) : null}
      {settings && !settings.env_complete ? (
        <div className="accounts-error" role="alert">
          .env 里那一套凭据不完整：当前不可直接回滚到旧镜像，回滚前先补 .env。
        </div>
      ) : null}
      {feedback ? (
        <div className={`accounts-feedback ${feedback.kind}`} role="status">{feedback.message}</div>
      ) : null}

      {settings === null ? (
        <div className="accounts-empty">加载中…</div>
      ) : (
        <>
          <section>
            <h3 className="mcards-h">生成用</h3>
            {balancesUnavailable ? (
              <p className="mcards-meta">余额暂时估不出来（账户目录或锚点文件读不出），卡片上先不显示。</p>
            ) : null}
            <div className="mcards-grid">
              {allCards.map(renderCard)}
              <button
                type="button"
                className="mcards-card mcards-add tilt"
                disabled={locked}
                onClick={() => openEditor(blankEditor())}
              >
                <span className="mcards-plus" aria-hidden>+</span>
                <span className="mcards-meta">添加服务商</span>
              </button>
            </div>
          </section>

          {editor ? (
            <form className="mcards-editor" onSubmit={onSubmitCard}>
              <h3 className="mcards-h">{editor.mode === 'new' ? '添加服务商' : `编辑「${editingCard?.name ?? editor.name}」`}</h3>
              {editor.mode === 'new' ? (
                <div className="mcards-row">
                  {TEMPLATES.map((t) => (
                    <button
                      key={t.key}
                      type="button"
                      className={`toggle${t.baseUrl && editor.base_url === t.baseUrl && editor.api_format === t.format ? ' on' : ''}`}
                      onClick={() =>
                        setEditor({
                          ...editor,
                          api_format: t.format,
                          base_url: t.baseUrl,
                          name: editor.name || (t.key === 'other' ? '' : t.label),
                          newAccount: { ...editor.newAccount, kind: t.kind },
                        })
                      }
                    >
                      {t.label}
                    </button>
                  ))}
                </div>
              ) : null}
              <div className="mcards-fields">
                <label>
                  <span>名字</span>
                  <input value={editor.name} onChange={(e) => setEditor({ ...editor, name: e.target.value })} placeholder="比如：朋友的转发" />
                </label>
                <label>
                  <span>地址（Base URL）</span>
                  <input value={editor.base_url} spellCheck={false} autoComplete="off" onChange={(e) => setEditor({ ...editor, base_url: e.target.value })} placeholder="https://…" />
                </label>
                <label>
                  <span>模型</span>
                  <input value={editor.model} spellCheck={false} autoComplete="off" onChange={(e) => setEditor({ ...editor, model: e.target.value })} placeholder="mimo-v2.6-pro" />
                </label>
                <label>
                  <span>Token</span>
                  <input
                    type="password"
                    aria-label="卡片 token"
                    autoComplete="new-password"
                    value={token}
                    onChange={(e) => setToken(e.target.value)}
                    placeholder={
                      editingCard?.has_token
                        ? `留空沿用已存的${editingCard.token_hint ? `（末四位 ${editingCard.token_hint}）` : ''}`
                        : '必填'
                    }
                  />
                </label>
              </div>
              <button type="button" className="command mcards-disclose" onClick={() => setEditorAdvanced(!editorAdvanced)}>
                {editorAdvanced ? '▾' : '▸'} 高级
              </button>
              {editorAdvanced ? (
                <div className="mcards-fields">
                  <label>
                    <span>协议</span>
                    <select value={editor.api_format} onChange={(e) => setEditor({ ...editor, api_format: e.target.value as ApiFormat })}>
                      <option value="anthropic">anthropic</option>
                      <option value="openai">openai</option>
                    </select>
                  </label>
                  <label>
                    <span>判分时 thinking</span>
                    <select value={editor.judge_thinking} onChange={(e) => setEditor({ ...editor, judge_thinking: e.target.value as JudgeThinking })}>
                      {(Object.keys(THINKING_LABEL) as JudgeThinking[]).map((value) => (
                        <option key={value} value={value}>{THINKING_LABEL[value]}</option>
                      ))}
                    </select>
                  </label>
                  {editor.mode === 'new' ? (
                    <label>
                      <span>计费账户</span>
                      <select
                        aria-label="计费账户"
                        value={editor.accountMode === 'new' ? '' : editor.account}
                        onChange={(e) =>
                          setEditor(
                            e.target.value
                              ? { ...editor, accountMode: 'existing', account: e.target.value }
                              : { ...editor, accountMode: 'new', account: '' },
                          )
                        }
                      >
                        <option value="">新建同名账户</option>
                        {selectableAccounts(editor.account).map((account) => (
                          <option key={account.id} value={account.id}>{account.name}（{account.id}，{account.currency}）</option>
                        ))}
                      </select>
                    </label>
                  ) : (
                    <label>
                      <span>计费账户</span>
                      <select aria-label="计费账户" value={editor.account} onChange={(e) => setEditor({ ...editor, account: e.target.value })}>
                        {selectableAccounts(editor.account).map((account) => (
                          <option key={account.id} value={account.id}>
                            {account.name}（{account.id}，{account.currency}）{account.retired_at ? ' · 已退役' : ''}
                          </option>
                        ))}
                      </select>
                    </label>
                  )}
                  {editor.mode === 'new' && editor.accountMode === 'new' ? (
                    <>
                      <label>
                        <span>币种</span>
                        <select
                          value={editor.newAccount.currency}
                          onChange={(e) => setEditor({ ...editor, newAccount: { ...editor.newAccount, currency: e.target.value as 'CNY' | 'USD' } })}
                        >
                          <option value="CNY">CNY</option>
                          <option value="USD">USD（余额不相减）</option>
                        </select>
                      </label>
                      <label>
                        <span>价格倍率</span>
                        <input
                          value={editor.newAccount.multiplier}
                          inputMode="decimal"
                          onChange={(e) => setEditor({ ...editor, newAccount: { ...editor.newAccount, multiplier: e.target.value } })}
                        />
                      </label>
                    </>
                  ) : editorAccount ? (
                    <p className="mcards-meta mcards-advanced">
                      倍率 {editorAccount.multiplier}、币种 {editorAccount.currency} 属于账户「{editorAccount.name}」，
                      在下面「高级 → 计费账户」里改；改倍率会影响挂在这个账户下的所有卡片。
                    </p>
                  ) : null}
                </div>
              ) : null}
              <div className="mcards-row">
                <button
                  className="btn btn-accent"
                  type="submit"
                  disabled={
                    locked || !editor.name.trim() || !editor.base_url.trim() || !editor.model.trim() ||
                    (editor.mode === 'new' && !token)
                  }
                >
                  {busy ? '提交中…' : editor.mode === 'new' ? '添加' : '保存'}
                </button>
                <button className="btn" type="button" onClick={closeEditor}>取消</button>
                {editor.mode === 'edit' ? (
                  <button className="command mcards-danger" type="button" disabled={locked} onClick={onDelete}>
                    删除这张卡片
                  </button>
                ) : null}
              </div>
            </form>
          ) : null}

          <section>
            <h3 className="mcards-h">判分用</h3>
            {judgeDraft !== null ? (
              <>
                <div className="mcards-row" role="radiogroup" aria-label="判分用">
                  <button
                    type="button"
                    role="radio"
                    aria-checked={'follow' in judgeDraft}
                    className={`toggle${'follow' in judgeDraft ? ' on' : ''}`}
                    onClick={() => setJudgeDraft({ follow: 'generation' })}
                  >
                    跟着生成
                  </button>
                  <button
                    type="button"
                    role="radio"
                    aria-checked={'card' in judgeDraft}
                    className={`toggle${'card' in judgeDraft ? ' on' : ''}`}
                    onClick={() => setJudgeDraft('card' in judgeDraft ? judgeDraft : { card: settings.active.generation })}
                  >
                    用指定的卡片
                  </button>
                  {'card' in judgeDraft ? (
                    <select
                      aria-label="判分卡片"
                      value={judgeDraft.card}
                      onChange={(e) => setJudgeDraft({ ...judgeDraft, card: e.target.value })}
                    >
                      {allCards.map((card) => (
                        <option key={card.id} value={card.id}>{card.name}（{card.model ?? '—'}）</option>
                      ))}
                    </select>
                  ) : (
                    <span className="mcards-meta">现在是「{nameOf(settings.active.generation)}」· {settings.evaluator_model}</span>
                  )}
                </div>
                {'card' in judgeDraft ? (
                  <>
                    <button type="button" className="command mcards-disclose" onClick={() => setJudgeAdvanced(!judgeAdvanced)}>
                      {judgeAdvanced ? '▾' : '▸'} 换个模型名
                    </button>
                    {judgeAdvanced ? (
                      <div className="mcards-fields">
                        <label>
                          <span>判分模型（留空用卡片自己的）</span>
                          <input
                            value={judgeDraft.model ?? ''}
                            spellCheck={false}
                            autoComplete="off"
                            onChange={(e) =>
                              setJudgeDraft(
                                e.target.value
                                  ? { card: judgeDraft.card, model: e.target.value }
                                  : { card: judgeDraft.card },
                              )
                            }
                          />
                        </label>
                      </div>
                    ) : null}
                  </>
                ) : null}
                {judgeChanged ? (
                  <div className="mcards-row">
                    <button type="button" className="btn btn-accent" disabled={locked} onClick={applyJudge}>
                      应用判分设置
                    </button>
                    <button type="button" className="btn" onClick={() => setJudgeDraft(settings.active.judge)}>
                      还原
                    </button>
                  </div>
                ) : null}
              </>
            ) : null}
          </section>

          <section>
            <button type="button" className="command mcards-disclose" onClick={() => setShowAdvanced(!showAdvanced)}>
              {showAdvanced ? '▾' : '▸'} 高级：计费账户、删掉全部卡片
            </button>
            {showAdvanced ? (
              <>
                <BillingAccounts accounts={accounts} onChange={setAccounts} />
                <div className="mcards-row">
                  <button
                    type="button"
                    className="command mcards-danger"
                    disabled={locked || settings.cards.length === 0}
                    onClick={onClearAll}
                  >
                    删掉全部卡片，回到 .env
                  </button>
                  <span className="mcards-meta">只想换回 .env、留着卡片的话，点上面的「.env」卡片就行。</span>
                </div>
              </>
            ) : null}
          </section>
        </>
      )}
    </div>
  );
}
