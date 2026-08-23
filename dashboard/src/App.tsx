import { useCallback, useEffect, useState } from 'react';
import ReviewDashboard from './ReviewDashboard';
import WorldEditor from './world/WorldEditor';
import SetupWizard from './SetupWizard';
import Login from './Login';
import ConfigReload from './ConfigReload';
import PersistentWorlds from './PersistentWorlds';
import Simulate from './Simulate';
import {
  UNAUTHENTICATED_EVENT,
  fetchAuthSession,
  fetchConfig,
  logout,
  type AuthSession,
  type ConfigStatus,
} from './api';
import './App.css';

type Tab = 'simulate' | 'review' | 'world' | 'worlds';

function App() {
  const [tab, setTab] = useState<Tab>('simulate');
  const [session, setSession] = useState<AuthSession | null>(null);
  const [sessionError, setSessionError] = useState<string | null>(null);
  const [config, setConfig] = useState<ConfigStatus | null>(null);
  const [configError, setConfigError] = useState<string | null>(null);

  const loadSession = useCallback(() => {
    setSessionError(null);
    fetchAuthSession()
      .then(setSession)
      .catch((e) => setSessionError(e instanceof Error ? e.message : '读取会话状态失败'));
  }, []);

  useEffect(() => {
    loadSession();
  }, [loadSession]);

  // 任何一条请求撞上 401 都把界面退回登录：会话过期是随时可能发生的事
  // （服务器重启过、TTL 到了、别处登出了），而"什么都加载不出来但不说为什么"
  // 是最难查的那种坏法。
  useEffect(() => {
    const handler = () => {
      setSession((current) => (current ? { ...current, authenticated: false } : current));
      setConfig(null);
    };
    window.addEventListener(UNAUTHENTICATED_EVENT, handler);
    return () => window.removeEventListener(UNAUTHENTICATED_EVENT, handler);
  }, []);

  const authenticated = session !== null && (!session.auth_required || session.authenticated);

  const loadConfig = useCallback(() => {
    setConfigError(null);
    fetchConfig()
      .then(setConfig)
      .catch((e) => setConfigError(e instanceof Error ? e.message : '加载配置失败'));
  }, []);

  // 配置要等到确认能进来之后再取：没登录时取它只会拿到一条 401，然后把一个
  // 本该显示登录框的界面变成一条错误信息。
  useEffect(() => {
    if (authenticated) loadConfig();
  }, [authenticated, loadConfig]);

  const handleLogout = () => {
    logout()
      .then(setSession)
      .catch(() => loadSession());
  };

  if (sessionError) {
    return (
      <div className="state-msg error">
        {sessionError}
        <button className="btn btn-approve" onClick={loadSession}>重试</button>
      </div>
    );
  }

  if (session === null) {
    return <div className="state-msg">加载中…</div>;
  }

  if (!authenticated) {
    return <Login onDone={loadSession} />;
  }

  if (configError) {
    return (
      <div className="state-msg error">
        {configError}
        <button className="btn btn-approve" onClick={loadConfig}>重试</button>
      </div>
    );
  }

  if (config === null) {
    return <div className="state-msg">加载中…</div>;
  }

  if (!config.has_key) {
    return <SetupWizard onDone={loadConfig} />;
  }

  return (
    <div className="shell">
      <nav className="tabbar">
        <button className={`tab-btn ${tab === 'simulate' ? 'active' : ''}`} onClick={() => setTab('simulate')}>
          模拟
        </button>
        <button className={`tab-btn ${tab === 'review' ? 'active' : ''}`} onClick={() => setTab('review')}>
          审核
        </button>
        <button className={`tab-btn ${tab === 'world' ? 'active' : ''}`} onClick={() => setTab('world')}>
          World Editor
        </button>
        <button className={`tab-btn ${tab === 'worlds' ? 'active' : ''}`} onClick={() => setTab('worlds')}>
          持久世界
        </button>
        <ConfigReload />
        {session.auth_required && (
          <button className="tab-btn" onClick={handleLogout} title="作废当前会话">
            登出
          </button>
        )}
      </nav>
      <div className="shell-body">
        {tab === 'simulate' ? (
          <Simulate />
        ) : tab === 'review' ? (
          <ReviewDashboard />
        ) : tab === 'world' ? (
          <WorldEditor />
        ) : (
          <PersistentWorlds />
        )}
      </div>
    </div>
  );
}

export default App;
