import { useState } from 'react';
import { login } from './api';
import './App.css';

interface LoginProps {
  onDone: () => void;
}

/** 操作者登录。
 *
 * token 只活在这个组件的 state 里，提交成功之后连这份都丢掉：服务端换回来的
 * 是一张 HttpOnly Cookie，浏览器此后不需要、也拿不到那个密钥。这里刻意没有
 * 「记住我」——那只会把一个管理凭据写进 localStorage。
 */
function Login({ onDone }: LoginProps) {
  const [token, setToken] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async () => {
    if (!token.trim() || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      await login(token.trim());
      setToken('');
      onDone();
    } catch (e) {
      setError(e instanceof Error ? e.message : '登录失败');
      setSubmitting(false);
    }
  };

  return (
    <div className="setup-shell">
      <div className="setup-wizard">
        <div className="logo">PNS <span>Console</span></div>
        <h2>需要管理凭据</h2>
        <p className="subtitle">
          这台服务器上的创建、恢复、推进、停止、关闭、重载和活动修改都需要凭据。
          凭据由部署时的 PNS_ADMIN_TOKEN 提供，登录后浏览器只保留一张会话 Cookie。
        </p>

        <label className="setup-field">
          管理凭据
          <input
            type="password"
            value={token}
            autoComplete="off"
            spellCheck={false}
            onChange={(e) => setToken(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') handleSubmit(); }}
            placeholder="PNS_ADMIN_TOKEN"
          />
        </label>

        {error && <p className="setup-error">{error}</p>}

        <button
          className="btn btn-approve"
          onClick={handleSubmit}
          disabled={!token.trim() || submitting}
        >
          {submitting ? '登录中…' : '登录'}
        </button>
      </div>
    </div>
  );
}

export default Login;
