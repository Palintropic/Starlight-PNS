import { useState, type FormEvent } from 'react';
import { updateSsoLink } from './api';

export default function AccountLink({ username, linked, onCancel, onUnlinked }: {
  username: string; linked?: string | null; onCancel: () => void; onUnlinked: () => void;
}) {
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (busy || !password) return;
    setBusy(true);
    setError('');
    try {
      const result = await updateSsoLink(password, linked ? 'unlink' : 'link');
      setPassword('');
      if (result.url) window.location.assign(result.url);
      else onUnlinked();
    } catch (e) {
      setError(e instanceof Error ? e.message : '账户操作失败');
      setBusy(false);
    }
  };
  return <main className="setup-shell"><form className="setup-wizard" onSubmit={submit}>
    <h2>{linked ? '解除 Palintropic 绑定' : '绑定 Palintropic 账户'}</h2>
    <p>{linked ? `解除后，${linked} 将恢复独立登录，${username} 保留本地密码。所有现有会话会失效。`
      : `验证 ${username} 的密码后，将前往 Palintropic 验证身份。返回时可确认账户和权限。`}</p>
    <label className="setup-field">当前密码
      <input type="password" autoComplete="current-password" value={password}
        onChange={event => setPassword(event.target.value)} required />
    </label>
    {error && <p role="alert">{error}</p>}
    <div className="accounts-row-actions">
      <button className="btn btn-accent" disabled={busy || !password}>{linked ? '确认解除绑定' : '验证并继续'}</button>
      <button type="button" className="btn" onClick={onCancel} disabled={busy}>取消</button>
    </div>
  </form></main>;
}
