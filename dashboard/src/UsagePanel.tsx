// dashboard/src/UsagePanel.tsx — 模型用量与断供提示（COST-1）
//
// 两样东西：
//
//   * `UsageAlert`：一条横幅。模型调用连续失败到阈值时标红（世界此刻是哑的），
//     本地估算余额低于阈值时标黄，账本写不进去时也说一声。放在「世界」页
//     最上面，打开后台第一眼就能看到——10-08 那次欠费哑了 14 小时才被发现。
//   * `UsagePanel`：完整的用量面板，放在持久世界页：今天花了多少、各类 token、
//     缓存命中率、按住民拆开、余额估算和锚点输入。
//
// 这里的每个钱数都是**服务器按本地价目表的估算**：MiMo 没有余额接口，计费
// 有延迟，超时的请求可能扣了钱却没有用量。所以界面上永远写"估算"，并把
// 没算进去的次数一起列出来，而不是给一个看起来很确定的数。
import { useState } from 'react';
import { SCOPE_OPERATE, setBalanceAnchor, type UsageBucket } from './api';
import { useCan } from './principal';
import { alertLines, beijingMinute, percent, tokens, useUsageSummary, yuan } from './usageAlerts';
import './usage.css';

export function UsageAlert() {
  const { summary } = useUsageSummary(1);
  if (!summary) return null;
  const lines = alertLines(summary);
  if (lines.length === 0) return null;
  return (
    <div className="usage-alert" role="alert">
      {lines.map((line) => (
        <p key={line.text} className={`usage-alert-line ${line.tone}`}>
          {line.text}
        </p>
      ))}
    </div>
  );
}

function Tile({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div className="usage-tile">
      <span className="usage-tile-label">{label}</span>
      <span className="usage-tile-value">{value}</span>
      {note ? <span className="usage-tile-note">{note}</span> : null}
    </div>
  );
}

const unknownNote = (bucket: UsageBucket): string | undefined => {
  const parts: string[] = [];
  if (bucket.billing_uncertain) parts.push(`${bucket.billing_uncertain} 次可能已扣费但没有用量`);
  if (bucket.no_usage) parts.push(`${bucket.no_usage} 次读不出用量`);
  if (bucket.unpriced) parts.push(`${bucket.unpriced} 次模型未定价`);
  return parts.length ? `另有${parts.join('、')}，没算进去` : undefined;
};

export function UsagePanel() {
  const { summary, error, reload } = useUsageSummary(7);
  const canOperate = useCan(SCOPE_OPERATE);
  const [draft, setDraft] = useState('');
  const [saving, setSaving] = useState(false);
  const [feedback, setFeedback] = useState<{ kind: 'ok' | 'error'; message: string } | null>(null);

  const draftValue = draft.trim() === '' ? null : Number(draft);
  const draftValid = draftValue !== null && Number.isFinite(draftValue);

  const saveAnchor = () => {
    if (!draftValid || draftValue === null) return;
    setSaving(true);
    setBalanceAnchor(draftValue)
      .then(() => {
        setFeedback({ kind: 'ok', message: `记下了：此刻余额 ${yuan(draftValue)}` });
        setDraft('');
        reload();
      })
      .catch((e: unknown) =>
        setFeedback({ kind: 'error', message: e instanceof Error ? e.message : String(e) }),
      )
      .finally(() => setSaving(false));
  };

  if (error && !summary) return <section className="usage"><p className="usage-error">{error}</p></section>;
  if (!summary) return null;

  const today = summary.by_day[summary.days[1]];
  const week = summary.total;
  const characters = Object.entries(summary.by_character).sort(
    ([, a], [, b]) => b.cost_yuan - a.cost_yuan,
  );
  const balance = summary.balance;
  const lines = alertLines(summary);

  return (
    <section className="usage">
      <h3>模型用量</h3>
      {lines.map((line) => (
        <p key={line.text} className={`usage-alert-line ${line.tone}`}>
          {line.text}
        </p>
      ))}

      <div className="usage-tiles">
        <Tile
          label="今天（北京时间）"
          value={today ? yuan(today.cost_yuan) : '¥0'}
          note={today ? `${today.calls} 次调用` : '还没有调用'}
        />
        <Tile label="近 7 天" value={yuan(week.cost_yuan)} note={`${week.calls} 次调用`} />
        <Tile label="新鲜输入" value={tokens(today?.fresh_input ?? 0)} />
        <Tile label="缓存命中" value={tokens(today?.cache_read ?? 0)} />
        <Tile label="输出" value={tokens(today?.output ?? 0)} />
        <Tile label="缓存命中率" value={percent(today?.cache_hit_rate ?? null)} />
      </div>
      {today && unknownNote(today) ? <p className="usage-note">{unknownNote(today)}</p> : null}

      <div className="usage-balance">
        <h4>余额（本地估算）</h4>
        {balance ? (
          <>
            <p className="usage-balance-value">{yuan(balance.estimated_yuan)}</p>
            <p className="usage-note">
              从 {beijingMinute(balance.anchor.at)} 记下的 {yuan(balance.anchor.balance_yuan)} 往下扣了{' '}
              {yuan(balance.spent_since_anchor_yuan)}
              {balance.uncounted_calls ? `；${balance.uncounted_calls} 次调用没有花费数据，没算进去` : ''}
              {balance.billing_uncertain_calls
                ? `（其中 ${balance.billing_uncertain_calls} 次可能已经扣了钱）`
                : ''}
              {balance.anchor_before_ledger ? '；这个时间早于账本起点，之前的花费没记下来' : ''}
              。MiMo 计费有延迟，以控制台为准。
            </p>
          </>
        ) : (
          <p className="usage-note">还没记过余额。去 MiMo 控制台看一眼，把余额填在下面。</p>
        )}
        {canOperate ? (
          <div className="usage-anchor">
            <label>
              控制台上此刻的余额（元）
              <input
                type="number"
                step="0.01"
                inputMode="decimal"
                value={draft}
                placeholder="比如 99.42"
                onChange={(e) => setDraft(e.target.value)}
              />
            </label>
            <button className="btn" disabled={!draftValid || saving} onClick={saveAnchor}>
              {saving ? '记下中…' : '记下余额'}
            </button>
            <span className="usage-hint">填余额本身，不是充了多少。</span>
          </div>
        ) : null}
        {feedback ? <p className={`usage-feedback ${feedback.kind}`}>{feedback.message}</p> : null}
      </div>

      {characters.length ? (
        <div className="usage-people">
          <h4>近 7 天按住民</h4>
          <ul>
            {characters.map(([id, bucket]) => (
              <li key={id}>
                <span className="usage-person">{id}</span>
                <span>{yuan(bucket.cost_yuan)}</span>
                <span className="usage-dim">{bucket.calls} 次</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {summary.corrupt_lines ? (
        <p className="usage-note">账本里有 {summary.corrupt_lines} 行读不出来，已跳过。</p>
      ) : null}
    </section>
  );
}
