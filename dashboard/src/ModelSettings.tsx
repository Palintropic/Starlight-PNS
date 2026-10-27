// dashboard/src/ModelSettings.tsx — 模型线路（CFG-1，需要 accounts:manage）
//
// 生成、判分两条线路，判分可以跟随生成。每条线路挂在一个计费账户下。
//
// 几件刻意的事：
//
//   * **token 只活在输入框里。** 服务端只给"有没有"和末 4 位，这里从不回填；
//     点保存的那一刻就从 state 里擦掉（失败也一样，要重填），离开这个页面
//     组件卸载，state 跟着没了。不进 localStorage，不打日志。
//   * **token 留空 = 沿用已存的。** 解析在服务端（§4.2）：从 .env 第一次切到
//     面板时没有"已存的"，服务端会拒绝，这里只在提示里说清楚。
//   * **有世界开着就保存不了。** 服务端回 409 world_open，照它那句话说；这里
//     不先查一遍世界列表替它判断。
//   * **.env 凭据不完整时常驻提示不能直接回滚**（§9）：旧镜像只认 .env。
import { useCallback, useEffect, useState } from 'react';
import {
  fetchBillingAccounts,
  fetchProviderSettings,
  resetProviderSettings,
  saveProviderSettings,
  type ApiFormat,
  type BillingAccount,
  type JudgeThinking,
  type ProviderLine,
  type ProviderLineInput,
  type ProviderSettingsView,
} from './api';
import './accounts.css';

type LineName = 'generation' | 'judge';
type Draft = Omit<ProviderLineInput, 'token'>;
type Feedback = { kind: 'ok' | 'error'; message: string } | null;

const LINE_LABEL: Record<LineName, string> = { generation: '生成线路', judge: '判分线路' };

const THINKING_LABEL: Record<JudgeThinking, string> = {
  auto: '自动（官方 MiMo 地址才关）',
  off: '关（经转发地址用 MiMo 时选这个）',
  provider: '不管（交给服务商默认）',
};

const draftOf = (line: ProviderLine): Draft => ({
  account: line.account,
  api_format: line.api_format,
  base_url: line.base_url,
  model: line.model,
  judge_thinking: line.judge_thinking,
});

const tokenNote = (line: ProviderLine | null): string =>
  line?.has_token
    ? `留空沿用已存的${line.token_hint ? `（末四位 ${line.token_hint}）` : ''}`
    : '必填';

export default function ModelSettings() {
  const [view, setView] = useState<ProviderSettingsView | null>(null);
  const [accounts, setAccounts] = useState<Record<string, BillingAccount>>({});
  const [loadError, setLoadError] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<Feedback>(null);
  const [busy, setBusy] = useState(false);

  const [drafts, setDrafts] = useState<Record<LineName, Draft> | null>(null);
  const [follow, setFollow] = useState(false);
  const [tokens, setTokens] = useState<Record<LineName, string>>({ generation: '', judge: '' });

  const adopt = useCallback((next: ProviderSettingsView) => {
    setView(next);
    setDrafts({
      generation: draftOf(next.settings.generation),
      judge: draftOf(next.settings.judge),
    });
    setFollow(next.settings.judge.follow === 'generation');
  }, []);

  const refresh = useCallback(() => {
    setLoadError(null);
    fetchProviderSettings()
      .then(adopt)
      .catch((e) => setLoadError(e instanceof Error ? e.message : '加载模型设置失败'));
    fetchBillingAccounts()
      .then((payload) => setAccounts(payload.accounts))
      .catch((e) => setLoadError(e instanceof Error ? e.message : '加载计费账户失败'));
  }, [adopt]);

  useEffect(refresh, [refresh]);

  const edit = (name: LineName, patch: Partial<Draft>) =>
    setDrafts((current) => (current ? { ...current, [name]: { ...current[name], ...patch } } : current));

  const run = async (action: () => Promise<ProviderSettingsView>, done: string) => {
    setBusy(true);
    setFeedback(null);
    try {
      const next = await action();
      adopt(next);
      setFeedback({ kind: 'ok', message: `${done}（配置 revision ${next.config_revision}）` });
    } catch (e) {
      setFeedback({ kind: 'error', message: e instanceof Error ? e.message : '保存失败' });
    } finally {
      setBusy(false);
    }
  };

  const onSave = (event: React.FormEvent) => {
    event.preventDefault();
    if (drafts === null) return;
    // 先从 state 擦掉，再把局部变量交给请求：token 不在任何一次重渲染里留着。
    const sent = tokens;
    setTokens({ generation: '', judge: '' });
    const generation = { ...drafts.generation, token: sent.generation };
    const judge = follow ? ({ follow: 'generation' } as const) : { ...drafts.judge, token: sent.judge };
    void run(() => saveProviderSettings({ generation, judge }), '已保存，新线路已生效');
  };

  const onReset = () => {
    const confirmed = window.confirm(
      '删掉面板存的两条线路，回到 .env 里那一套？账户目录和余额锚点不受影响。',
    );
    if (!confirmed) return;
    setTokens({ generation: '', judge: '' });
    void run(resetProviderSettings, '已恢复为 .env 默认');
  };

  const choices = (current: string) =>
    Object.values(accounts)
      .filter((account) => !account.retired_at || account.id === current)
      .sort((a, b) => a.id.localeCompare(b.id));

  const lineFields = (name: LineName, draft: Draft) => {
    const saved = view?.settings[name] ?? null;
    // 解除跟随时判分线路"已存的"token 其实是生成线路的（服务端会显式拷贝）。
    const note = name === 'judge' && saved && view?.settings.judge.follow
      ? '留空沿用生成线路的'
      : tokenNote(saved);
    return (
      <fieldset className="model-line" key={name}>
        <legend>{LINE_LABEL[name]}</legend>
        <div className="accounts-create-row">
          <label>
            <span>计费账户</span>
            <select
              value={draft.account}
              onChange={(e) => edit(name, { account: e.target.value })}
            >
              {choices(draft.account).map((account) => (
                <option key={account.id} value={account.id}>
                  {account.name}（{account.id}，{account.currency}）
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>协议</span>
            <select
              value={draft.api_format}
              onChange={(e) => edit(name, { api_format: e.target.value as ApiFormat })}
            >
              <option value="anthropic">anthropic</option>
              <option value="openai">openai</option>
            </select>
          </label>
          <label>
            <span>Base URL</span>
            <input
              value={draft.base_url}
              autoComplete="off"
              spellCheck={false}
              onChange={(e) => edit(name, { base_url: e.target.value })}
            />
          </label>
          <label>
            <span>模型名</span>
            <input
              value={draft.model}
              autoComplete="off"
              spellCheck={false}
              onChange={(e) => edit(name, { model: e.target.value })}
            />
          </label>
          <label>
            <span>Access token</span>
            <input
              type="password"
              aria-label={`${LINE_LABEL[name]} token`}
              value={tokens[name]}
              autoComplete="new-password"
              onChange={(e) => {
                const value = e.target.value;
                setTokens((current) => ({ ...current, [name]: value }));
              }}
              placeholder={note}
            />
          </label>
          <label>
            <span>判分时 thinking</span>
            <select
              value={draft.judge_thinking}
              onChange={(e) => edit(name, { judge_thinking: e.target.value as JudgeThinking })}
            >
              {(Object.keys(THINKING_LABEL) as JudgeThinking[]).map((value) => (
                <option key={value} value={value}>{THINKING_LABEL[value]}</option>
              ))}
            </select>
          </label>
        </div>
      </fieldset>
    );
  };

  const settings = view?.settings ?? null;

  return (
    <div className="accounts entrance">
      <div className="accounts-head">
        <p className="accounts-note">
          换模型要先<strong>关闭所有世界</strong>，保存后再恢复世界；恢复时会照常检查内容采用、补跑离线时间。
          token 提交后不回显，也不会保存在浏览器里。
        </p>
        <button className="btn" onClick={refresh}>刷新</button>
      </div>

      {loadError ? <div className="accounts-error">{loadError}</div> : null}
      {view?.quarantined ? (
        <div className="accounts-error" role="alert">
          模型设置处于隔离状态：上一次保存没能确认结果。先在服务器上用 scripts/provider_resolve.py
          决定保留哪一份，期间不能保存。
        </div>
      ) : null}
      {settings && !settings.env_complete ? (
        <div className="accounts-error" role="alert">
          .env 里那一套凭据不完整：当前不可直接回滚到旧镜像，回滚前先补 .env。
        </div>
      ) : null}
      {feedback ? (
        <div className={`accounts-feedback ${feedback.kind}`}>{feedback.message}</div>
      ) : null}

      {settings === null || drafts === null ? (
        <div className="accounts-empty">加载中…</div>
      ) : (
        <form className="accounts-create" onSubmit={onSave}>
          <h3>
            当前来源：{settings.source === 'panel' ? '面板' : '.env（面板还没存过）'}
          </h3>
          {settings.source === 'env' ? (
            <p className="accounts-hint">第一次从 .env 切到面板时，两条线路的 token 都要填。</p>
          ) : null}
          {lineFields('generation', drafts.generation)}
          <label className="model-follow">
            <input
              type="checkbox"
              checked={follow}
              onChange={(e) => setFollow(e.target.checked)}
            />
            <span>判分跟随生成线路（同一个账户、地址、模型和 token）</span>
          </label>
          {follow ? null : lineFields('judge', drafts.judge)}
          <div className="accounts-create-actions">
            <button className="btn btn-accent" type="submit" disabled={busy || view?.quarantined}>
              {busy ? '保存中…' : '保存'}
            </button>
            {settings.source === 'panel' ? (
              <button
                className="btn"
                type="button"
                disabled={busy || view?.quarantined}
                onClick={onReset}
              >
                恢复 .env 默认
              </button>
            ) : null}
          </div>
        </form>
      )}
    </div>
  );
}
