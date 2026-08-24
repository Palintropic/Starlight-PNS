// dashboard/src/ChangePassword.tsx — 改自己的密码
//
// 成功之后**会话就没了**，服务端不补发新的：改密码的常见理由是"我怀疑它泄露
// 了"，那种时候留着当前这张会话恰好留错了。所以这里不假装还登着——直接告诉
// 用户要重新登录，然后把界面交回登录框。
import { useState } from 'react';
import { changePassword } from './api';
import './App.css';

interface ChangePasswordProps {
  username: string;
  onDone: () => void;
  onCancel: () => void;
}

export default function ChangePassword({ username, onDone, onCancel }: ChangePasswordProps) {
  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [confirm, setConfirm] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  const mismatch = confirm.length > 0 && next !== confirm;
  const ready = current.length > 0 && next.length > 0 && next === confirm;

  const submit = async () => {
    if (!ready || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      await changePassword(current, next);
      setCurrent('');
      setNext('');
      setConfirm('');
      setDone(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : '改密码失败');
      setSubmitting(false);
    }
  };

  if (done) {
    return (
      <div className="setup-shell">
        <div className="setup-wizard">
          <div className="logo">PNS <span>Console</span></div>
          <h2>密码已修改</h2>
          <p className="subtitle">
            这个账户的所有会话（包括当前这一张）都已经作废。请用新密码重新登录。
          </p>
          <button className="btn btn-approve" onClick={onDone}>回到登录</button>
        </div>
      </div>
    );
  }

  return (
    <div className="setup-shell">
      <div className="setup-wizard">
        <div className="logo">PNS <span>Console</span></div>
        <h2>修改密码</h2>
        <p className="subtitle">
          当前账户：{username}。改完之后所有会话都会失效，需要用新密码重新登录。
        </p>

        <label className="setup-field">
          当前密码
          <input
            type="password"
            value={current}
            autoComplete="current-password"
            onChange={(e) => setCurrent(e.target.value)}
          />
        </label>
        <label className="setup-field">
          新密码
          <input
            type="password"
            value={next}
            autoComplete="new-password"
            onChange={(e) => setNext(e.target.value)}
          />
        </label>
        <label className="setup-field">
          再输一次
          <input
            type="password"
            value={confirm}
            autoComplete="new-password"
            onChange={(e) => setConfirm(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') submit(); }}
          />
        </label>

        {mismatch ? <p className="setup-error">两次输入的新密码不一致</p> : null}
        {error ? <p className="setup-error">{error}</p> : null}

        <div className="accounts-row-actions">
          <button className="btn btn-approve" onClick={submit} disabled={!ready || submitting}>
            {submitting ? '提交中…' : '修改密码'}
          </button>
          <button className="btn btn-reject" onClick={onCancel} disabled={submitting}>
            取消
          </button>
        </div>
      </div>
    </div>
  );
}
