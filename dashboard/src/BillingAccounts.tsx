// dashboard/src/BillingAccounts.tsx — 计费账户目录（CFG-1 §4.3，挂在模型页下面）
//
// 账户是"谁付钱"：余额属于账户、token 属于线路。几条规则都在服务端，这里只是
// 把它们说清楚，不替服务端先判：
//
//   * id 一经创建不复用，退役的也算占用；`env` 是保留名；
//   * 只能改显示名和倍率，币种和服务商要换就新建一个账户；
//   * 被线路用着的账户不能退役（409，照服务端那句话说）；退役要先确认；
//     内建的 `env` 账户不给退役按钮。
import { useState } from 'react';
import {
  createBillingAccount,
  retireBillingAccount,
  updateBillingAccount,
  type BillingAccount,
} from './api';

const KINDS = ['mimo', 'deepseek', 'novita', 'openrouter', 'stepfun', 'siliconflow', 'other'] as const;

type Feedback = { kind: 'ok' | 'error'; message: string } | null;

interface Props {
  accounts: Record<string, BillingAccount>;
  onChange: (accounts: Record<string, BillingAccount>) => void;
}

export default function BillingAccounts({ accounts, onChange }: Props) {
  const [feedback, setFeedback] = useState<Feedback>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [draft, setDraft] = useState({ id: '', name: '', kind: 'other', currency: 'CNY' as 'CNY' | 'USD', multiplier: '1' });
  const [editing, setEditing] = useState<{ id: string; name: string; multiplier: string } | null>(null);

  const run = async (
    key: string,
    action: () => Promise<{ account: BillingAccount; accounts: Record<string, BillingAccount> }>,
    describe: (account: BillingAccount) => string,
  ) => {
    setBusy(key);
    setFeedback(null);
    try {
      const result = await action();
      onChange(result.accounts);
      setFeedback({ kind: 'ok', message: describe(result.account) });
      return true;
    } catch (e) {
      setFeedback({ kind: 'error', message: e instanceof Error ? e.message : '操作失败' });
      return false;
    } finally {
      setBusy(null);
    }
  };

  const onCreate = async (event: React.FormEvent) => {
    event.preventDefault();
    const input = { ...draft, id: draft.id.trim(), name: draft.name.trim() };
    const ok = await run('create', () => createBillingAccount(input), (a) => `已新建账户 ${a.name}（${a.id}）`);
    if (ok) setDraft({ id: '', name: '', kind: 'other', currency: 'CNY', multiplier: '1' });
  };

  const onSaveEdit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (editing === null) return;
    const { id, name, multiplier } = editing;
    const ok = await run(`${id}:edit`, () => updateBillingAccount(id, { name, multiplier }), (a) => `已修改 ${a.name}（倍率 ${a.multiplier}）`);
    if (ok) setEditing(null);
  };

  const onRetire = (account: BillingAccount) => {
    const confirmed = window.confirm(
      `退役账户 ${account.name}（${account.id}）？它的余额锚点随之失效，历史账仍记在它名下；id 以后也不能再用。`,
    );
    if (!confirmed) return;
    void run(`${account.id}:retire`, () => retireBillingAccount(account.id), (a) => `已退役 ${a.name}`);
  };

  const rows = Object.values(accounts).sort((a, b) => a.id.localeCompare(b.id));

  return (
    <section className="accounts-audit">
      <h3>计费账户</h3>
      <p className="accounts-hint">
        余额属于账户，token 属于线路。倍率是服务商在价目表之上的加价或折扣（不是汇率）；
        改倍率只影响之后的调用。币种和服务商不能改，要换就新建一个账户。
      </p>
      {feedback ? (
        <div className={`accounts-feedback ${feedback.kind}`}>{feedback.message}</div>
      ) : null}

      <table className="accounts-table">
        <thead>
          <tr>
            <th>id</th>
            <th>显示名</th>
            <th>服务商</th>
            <th>币种</th>
            <th>倍率</th>
            <th>状态</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((account) => {
            const rowBusy = busy !== null && busy.startsWith(`${account.id}:`);
            const builtin = account.id === 'env';
            return (
              <tr key={account.id} className={account.retired_at ? 'off' : ''}>
                <td>{account.id}</td>
                <td>{account.name}</td>
                <td>{account.kind}</td>
                <td>{account.currency}</td>
                <td>{account.multiplier}</td>
                <td>
                  <span className={`accounts-badge ${account.retired_at ? 'off' : 'ok'}`}>
                    {account.retired_at ? '已退役' : '在用'}
                  </span>
                </td>
                <td className="accounts-row-actions">
                  {account.retired_at ? null : (
                    <>
                      <button
                        className="btn"
                        disabled={rowBusy}
                        onClick={() => setEditing({ id: account.id, name: account.name, multiplier: account.multiplier })}
                      >
                        修改
                      </button>
                      {builtin ? null : (
                        <button className="btn" disabled={rowBusy} onClick={() => onRetire(account)}>
                          退役
                        </button>
                      )}
                    </>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      {editing ? (
        <form className="accounts-reset" onSubmit={onSaveEdit}>
          <h3>修改 {editing.id}</h3>
          <div className="accounts-create-row">
            <label>
              <span>显示名</span>
              <input value={editing.name} onChange={(e) => setEditing({ ...editing, name: e.target.value })} />
            </label>
            <label>
              <span>倍率</span>
              <input
                value={editing.multiplier}
                inputMode="decimal"
                onChange={(e) => setEditing({ ...editing, multiplier: e.target.value })}
              />
            </label>
          </div>
          <div className="accounts-create-actions">
            <button className="btn btn-accent" type="submit" disabled={busy !== null}>保存</button>
            <button className="btn" type="button" onClick={() => setEditing(null)}>取消</button>
          </div>
        </form>
      ) : null}

      <form className="accounts-create" onSubmit={onCreate}>
        <h3>新建计费账户</h3>
        <div className="accounts-create-row">
          <label>
            <span>id</span>
            <input
              value={draft.id}
              autoComplete="off"
              spellCheck={false}
              placeholder="字母数字和 - _，建了不能改"
              onChange={(e) => setDraft({ ...draft, id: e.target.value })}
            />
          </label>
          <label>
            <span>显示名</span>
            <input value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
          </label>
          <label>
            <span>服务商</span>
            <select value={draft.kind} onChange={(e) => setDraft({ ...draft, kind: e.target.value })}>
              {KINDS.map((kind) => (
                <option key={kind} value={kind}>{kind}</option>
              ))}
            </select>
          </label>
          <label>
            <span>币种</span>
            <select
              value={draft.currency}
              onChange={(e) => setDraft({ ...draft, currency: e.target.value as 'CNY' | 'USD' })}
            >
              <option value="CNY">CNY</option>
              <option value="USD">USD（余额不相减）</option>
            </select>
          </label>
          <label>
            <span>倍率</span>
            <input
              value={draft.multiplier}
              inputMode="decimal"
              onChange={(e) => setDraft({ ...draft, multiplier: e.target.value })}
            />
          </label>
        </div>
        <div className="accounts-create-actions">
          <button
            className="btn btn-accent"
            type="submit"
            disabled={busy === 'create' || !draft.id.trim() || !draft.name.trim()}
          >
            {busy === 'create' ? '新建中…' : '新建'}
          </button>
        </div>
      </form>
    </section>
  );
}
