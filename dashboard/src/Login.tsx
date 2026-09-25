import { useState } from 'react';
import { login } from './api';
import './App.css';

interface LoginProps {
  onDone: () => void;
}

/** 操作者登录。
 *
 * 用户名 + 密码；提交成功之后连这份 state 都丢掉：服务端换回来的是一张
 * HttpOnly Cookie，浏览器此后不需要、也拿不到任何凭据。这里刻意没有
 * 「记住我」——那只会把一个凭据写进 localStorage。
 *
 * `PNS_ADMIN_TOKEN` **不能**从这里登录：那把 token 是 break-glass /
 * 自动化用的 bearer，只走 `Authorization` 头。让它同时当网页口令，等于把一把
 * 不属于任何人、撤销要重启进程的钥匙发给每一个用浏览器的人。
 */
function Login({ onDone }: LoginProps) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const ready = username.trim().length > 0 && password.length > 0;

  const handleSubmit = async () => {
    if (!ready || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      await login(username.trim(), password);
      setUsername('');
      setPassword('');
      onDone();
    } catch (e) {
      setError(e instanceof Error ? e.message : '登录失败');
      setSubmitting(false);
    }
  };

  return (
    <div className="auth-shell">
      <main className="auth-content">
        <section className="auth-panel">
          <p className="pivot-title">Nightcord Sanctuary</p>
          <h1>需要登录</h1>
          <p className="auth-note">
            使用你的操作者账户继续。浏览器只保留 HttpOnly 会话 Cookie；密码不会写入本地。
          </p>

          <label className="auth-field">
            用户名
            <input
              type="text"
              value={username}
              autoComplete="username"
              spellCheck={false}
              onChange={(e) => setUsername(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') handleSubmit(); }}
              placeholder="用户名"
            />
          </label>

          <label className="auth-field">
            密码
            <input
              type="password"
              value={password}
              autoComplete="current-password"
              spellCheck={false}
              onChange={(e) => setPassword(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') handleSubmit(); }}
              placeholder="密码"
            />
          </label>

          {error && <p className="auth-error">{error}</p>}

          <button className="btn btn-accent auth-submit" onClick={handleSubmit} disabled={!ready || submitting}>
            {submitting ? '登录中…' : '登录'}
          </button>
        </section>
      </main>
    </div>
  );
}

export default Login;
