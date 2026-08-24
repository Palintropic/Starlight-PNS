// dashboard/src/Accounts.tsx — 用户管理（需要 accounts:manage）
//
// 这个视图只对有 `accounts:manage` 的主体显示，但**它不是那道门**：服务端在
// 中间件和路由依赖里各拒绝一次，所以直接 curl 过来的 operator 一样是 403。
// 这里藏起来的只是一个点了会报错的入口。
//
// 两件刻意的事：
//
//   * **密码提交之后不回显、不留在 state 里。** 新建和重置都清空输入框，
//     界面上任何地方都不会再出现刚才那串字符。
//   * **改权威要先确认，并且如实说出后果。** 停用、改角色、重置密码都会立刻
//     踢掉目标的全部会话——那是安全承诺，不是副作用，所以它出现在确认文案里，
//     也出现在操作结果里（"踢掉了 N 张会话"）。
import { useCallback, useEffect, useState } from 'react';
import {
  createAccount,
  fetchAccounts,
  fetchAuditRecords,
  resetAccountPassword,
  setAccountEnabled,
  setAccountRole,
  type Account,
  type AuditRecord,
} from './api';
import { usePrincipal } from './principal';
import './accounts.css';

const ROLES = ['admin', 'operator', 'observer'] as const;

const ROLE_LABEL: Record<string, string> = {
  admin: '管理员',
  operator: '操作者',
  observer: '观察者',
};

const ROLE_NOTE: Record<string, string> = {
  admin: '账户管理 + 全部操作 + 只读',
  operator: '世界/配置/审核操作 + 只读',
  observer: '只读',
};

type Feedback = { kind: 'ok' | 'error'; message: string } | null;

export default function Accounts() {
  const me = usePrincipal();
  const [users, setUsers] = useState<Account[] | null>(null);
  const [records, setRecords] = useState<AuditRecord[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<Feedback>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const [newName, setNewName] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [newRole, setNewRole] = useState<string>('observer');

  const refresh = useCallback(() => {
    setLoadError(null);
    fetchAccounts()
      .then((payload) => setUsers(payload.users))
      .catch((e) => setLoadError(e instanceof Error ? e.message : '加载账户失败'));
    fetchAuditRecords(100)
      .then((payload) => setRecords(payload.records))
      .catch(() => undefined);
  }, []);

  useEffect(refresh, [refresh]);

  const run = async (key: string, action: () => Promise<Account>, describe: (a: Account) => string) => {
    setBusy(key);
    setFeedback(null);
    try {
      const updated = await action();
      setFeedback({ kind: 'ok', message: describe(updated) });
      refresh();
    } catch (e) {
      setFeedback({ kind: 'error', message: e instanceof Error ? e.message : '操作失败' });
    } finally {
      setBusy(null);
    }
  };

  const revokedNote = (account: Account) =>
    account.revoked_sessions ? `，踢掉了 ${account.revoked_sessions} 张会话` : '';

  const onCreate = (event: React.FormEvent) => {
    event.preventDefault();
    const username = newName.trim();
    const password = newPassword;
    if (!username || !password) return;
    // 先清空再发请求：密码不该在任何一次重渲染里还留在输入框上。
    setNewPassword('');
    void run(
      'create',
      () => createAccount(username, password, newRole),
      (account) => {
        setNewName('');
        return `已创建 ${account.username}（${ROLE_LABEL[account.role] ?? account.role}）`;
      },
    );
  };

  const onRole = (account: Account, role: string) => {
    if (role === account.role) return;
    const confirmed = window.confirm(
      `把 ${account.username} 改成${ROLE_LABEL[role] ?? role}？` +
        '这会立刻作废该账户的全部会话。',
    );
    if (!confirmed) return;
    void run(
      `${account.principal_id}:role`,
      () => setAccountRole(account.principal_id, role),
      (updated) =>
        `${updated.username} 现在是${ROLE_LABEL[updated.role] ?? updated.role}${revokedNote(updated)}`,
    );
  };

  const onToggle = (account: Account) => {
    const next = !account.enabled;
    const confirmed = window.confirm(
      next
        ? `启用 ${account.username}？`
        : `停用 ${account.username}？这会立刻作废该账户的全部会话，之后它也登不进来。`,
    );
    if (!confirmed) return;
    void run(
      `${account.principal_id}:enabled`,
      () => setAccountEnabled(account.principal_id, next),
      (updated) =>
        `${updated.username} 已${updated.enabled ? '启用' : '停用'}${revokedNote(updated)}`,
    );
  };

  const onReset = (account: Account) => {
    const password = window.prompt(
      `给 ${account.username} 设一个新密码（至少 12 个字符）。` +
        '提交后该账户的全部会话立刻失效。',
    );
    if (!password) return;
    void run(
      `${account.principal_id}:password`,
      () => resetAccountPassword(account.principal_id, password),
      (updated) => `已重置 ${updated.username} 的密码${revokedNote(updated)}`,
    );
  };

  return (
    <div className="accounts">
      <div className="accounts-head">
        <div>
          <h2>用户管理</h2>
          <p className="accounts-note">
            账户是<strong>控制面主体</strong>，跟世界里的角色是两种东西——
            用户名叫 mizuki 不会让谁变成那个 mizuki。停用、改角色和重置密码都会
            <strong>立刻作废目标的全部会话</strong>。
          </p>
        </div>
        <button className="btn btn-approve" onClick={refresh}>刷新</button>
      </div>

      {loadError ? <div className="accounts-error">{loadError}</div> : null}
      {feedback ? (
        <div className={`accounts-feedback ${feedback.kind}`}>{feedback.message}</div>
      ) : null}

      <form className="accounts-create" onSubmit={onCreate}>
        <h3>新建账户</h3>
        <div className="accounts-create-row">
          <label>
            <span>用户名</span>
            <input
              value={newName}
              autoComplete="off"
              spellCheck={false}
              onChange={(e) => setNewName(e.target.value)}
              placeholder="字母数字和 . _ -"
            />
          </label>
          <label>
            <span>初始密码</span>
            <input
              type="password"
              value={newPassword}
              autoComplete="new-password"
              onChange={(e) => setNewPassword(e.target.value)}
              placeholder="至少 12 个字符"
            />
          </label>
          <label>
            <span>角色</span>
            <select value={newRole} onChange={(e) => setNewRole(e.target.value)}>
              {ROLES.map((role) => (
                <option key={role} value={role}>
                  {ROLE_LABEL[role]}（{ROLE_NOTE[role]}）
                </option>
              ))}
            </select>
          </label>
        </div>
        <div className="accounts-create-actions">
          <button
            className="btn btn-approve"
            type="submit"
            disabled={busy === 'create' || !newName.trim() || !newPassword}
          >
            {busy === 'create' ? '创建中…' : '创建'}
          </button>
          <span className="accounts-hint">
            密码提交后不会回显，也不会保存在浏览器里。
          </span>
        </div>
      </form>

      {users === null ? (
        <div className="accounts-empty">加载中…</div>
      ) : (
        <table className="accounts-table">
          <thead>
            <tr>
              <th>用户名</th>
              <th>角色</th>
              <th>状态</th>
              <th>创建于</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {users.map((account) => {
              const isMe = me !== null && me.principal_id === account.principal_id;
              const rowBusy = busy !== null && busy.startsWith(account.principal_id);
              return (
                <tr key={account.principal_id} className={account.enabled ? '' : 'off'}>
                  <td>
                    {account.username}
                    {isMe ? <span className="accounts-self">（你）</span> : null}
                  </td>
                  <td>
                    <select
                      value={account.role}
                      disabled={rowBusy}
                      onChange={(e) => onRole(account, e.target.value)}
                    >
                      {ROLES.map((role) => (
                        <option key={role} value={role}>{ROLE_LABEL[role]}</option>
                      ))}
                    </select>
                  </td>
                  <td>
                    <span className={`accounts-badge ${account.enabled ? 'ok' : 'off'}`}>
                      {account.enabled ? '启用' : '已停用'}
                    </span>
                  </td>
                  <td className="accounts-time">{account.created_at.slice(0, 19).replace('T', ' ')}</td>
                  <td className="accounts-row-actions">
                    <button
                      className={account.enabled ? 'btn btn-reject' : 'btn btn-approve'}
                      disabled={rowBusy}
                      onClick={() => onToggle(account)}
                    >
                      {account.enabled ? '停用' : '启用'}
                    </button>
                    <button
                      className="btn btn-approve"
                      disabled={rowBusy}
                      onClick={() => onReset(account)}
                    >
                      重置密码
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}

      <section className="accounts-audit">
        <h3>安全审计</h3>
        <p className="accounts-hint">
          登录成功/失败、登出、改密码、建号、改角色、停用启用和重置密码都在这里。
          记录里没有密码、没有哈希、也没有尝试过的用户名。
        </p>
        {records.length === 0 ? (
          <div className="accounts-empty">（还没有记录）</div>
        ) : (
          <table className="accounts-table">
            <thead>
              <tr>
                <th>时间</th>
                <th>动作</th>
                <th>结果</th>
                <th>操作者</th>
                <th>目标</th>
                <th>说明</th>
              </tr>
            </thead>
            <tbody>
              {records.map((record) => (
                <tr key={record.sequence} className={record.result === 'failure' ? 'warn' : ''}>
                  <td className="accounts-time">
                    {record.occurred_at.slice(0, 19).replace('T', ' ')}
                  </td>
                  <td>{record.action}</td>
                  <td>{record.result === 'success' ? '成功' : '失败'}</td>
                  <td>{record.actor_username ?? record.actor_principal_id ?? '—'}</td>
                  <td>{record.target_username ?? record.target_principal_id ?? '—'}</td>
                  <td className="accounts-detail">
                    {Object.keys(record.detail).length
                      ? JSON.stringify(record.detail)
                      : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}
