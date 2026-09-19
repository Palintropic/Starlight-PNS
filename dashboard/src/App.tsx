import { useCallback, useEffect, useState, type KeyboardEvent } from 'react';
import ReviewDashboard from './ReviewDashboard';
import WorldEditor from './world/WorldEditor';
import SetupWizard from './SetupWizard';
import Login from './Login';
import ChangePassword from './ChangePassword';
import Accounts from './Accounts';
import ConfigReload from './ConfigReload';
import PersistentWorlds from './PersistentWorlds';
import Simulate from './Simulate';
import {
  SCOPE_ACCOUNTS,
  SCOPE_OPERATE,
  UNAUTHENTICATED_EVENT,
  fetchAuthSession,
  fetchConfig,
  logout,
  type AuthSession,
  type ConfigStatus,
} from './api';
import { PrincipalProvider } from './PrincipalProvider';
import './App.css';

type Tab = 'simulate' | 'review' | 'world' | 'worlds' | 'accounts';

// Pivot 表头的文字就是页面标题：W10 的 Pivot 不在内容区再重复一遍。
const TAB_LABEL: Record<Tab, string> = {
  simulate: '模拟',
  review: '审核',
  world: '世界编辑',
  worlds: '持久世界',
  accounts: '用户管理',
};

function App() {
  const [tab, setTab] = useState<Tab>('simulate');
  const [session, setSession] = useState<AuthSession | null>(null);
  const [sessionError, setSessionError] = useState<string | null>(null);
  const [config, setConfig] = useState<ConfigStatus | null>(null);
  const [configError, setConfigError] = useState<string | null>(null);
  const [changingPassword, setChangingPassword] = useState(false);

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
  // （服务器重启过、TTL 到了、别处登出了、账户被停用或改了权限），而
  // "什么都加载不出来但不说为什么"是最难查的那种坏法。
  useEffect(() => {
    const handler = () => {
      setSession((current) =>
        current ? { ...current, authenticated: false, principal: null } : current,
      );
      setConfig(null);
    };
    window.addEventListener(UNAUTHENTICATED_EVENT, handler);
    return () => window.removeEventListener(UNAUTHENTICATED_EVENT, handler);
  }, []);

  const authenticated = session !== null && (!session.auth_required || session.authenticated);
  const principal = authenticated ? session?.principal ?? null : null;
  const scopes = principal?.scopes ?? [];
  const canOperate = scopes.includes(SCOPE_OPERATE);
  const canManageAccounts = scopes.includes(SCOPE_ACCOUNTS);

  // 只读账户没有"模拟"和"用户管理"这两个页签。当前停在一个自己已经没权限的
  // 页签上时把它拨回来——否则换账户之后会停在一个空白页上。
  useEffect(() => {
    if (!authenticated) return;
    if (tab === 'simulate' && !canOperate) setTab('worlds');
    if (tab === 'accounts' && !canManageAccounts) setTab('worlds');
  }, [authenticated, tab, canOperate, canManageAccounts]);

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
        <button className="btn btn-accent" onClick={loadSession}>重试</button>
      </div>
    );
  }

  if (session === null) {
    return <div className="state-msg">加载中…</div>;
  }

  if (!authenticated) {
    return <Login onDone={loadSession} />;
  }

  if (changingPassword) {
    return (
      <ChangePassword
        username={principal?.username ?? ''}
        onDone={() => {
          setChangingPassword(false);
          loadSession();
        }}
        onCancel={() => setChangingPassword(false)}
      />
    );
  }

  if (configError) {
    return (
      <div className="state-msg error">
        {configError}
        <button className="btn btn-accent" onClick={loadConfig}>重试</button>
      </div>
    );
  }

  if (config === null) {
    return <div className="state-msg">加载中…</div>;
  }

  if (!config.has_key) {
    return <SetupWizard onDone={loadConfig} />;
  }

  const tabs: Tab[] = [
    ...(canOperate ? (['simulate'] as const) : []),
    'review',
    'world',
    'worlds',
    ...(canManageAccounts ? (['accounts'] as const) : []),
  ];

  // Pivot 表头支持左右方向键切换，和 UWP 的 Pivot 一致。
  const onPivotKey = (event: KeyboardEvent) => {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
    const step = event.key === 'ArrowRight' ? 1 : -1;
    const next = tabs[(tabs.indexOf(tab) + step + tabs.length) % tabs.length];
    setTab(next);
    document.getElementById(`pivot-${next}`)?.focus();
  };

  return (
    <PrincipalProvider principal={principal}>
      <div className="shell">
        <header className="pivot">
          <div className="pivot-title">Nightcord Sanctuary</div>
          <nav className="pivot-headers" role="tablist" aria-label="后台功能" onKeyDown={onPivotKey}>
            {tabs.map((t) => (
              <button
                key={t}
                id={`pivot-${t}`}
                role="tab"
                aria-selected={tab === t}
                tabIndex={tab === t ? 0 : -1}
                className={`pivot-header-item${tab === t ? ' selected' : ''}`}
                onClick={() => setTab(t)}
              >
                {TAB_LABEL[t]}
              </button>
            ))}
          </nav>
        </header>
        <main className="pivot-item" role="tabpanel" aria-labelledby={`pivot-${tab}`} key={tab}>
          {tab === 'simulate' && canOperate ? <Simulate /> : tab === 'review' ? <ReviewDashboard /> : tab === 'world' ? <WorldEditor /> : tab === 'accounts' && canManageAccounts ? <Accounts /> : <PersistentWorlds />}
        </main>
        <footer className="command-bar">
          <span className="command-bar-account" title={principal ? `principal ${principal.principal_id}` : undefined}>{principal ? `${principal.username} · ${principal.role}` : null}</span>
          <div className="command-bar-commands">
            {canOperate ? <ConfigReload /> : null}
            {principal?.via === 'session' ? <button className="command" onClick={() => setChangingPassword(true)}>修改密码</button> : null}
            {session.auth_required ? <button className="command" onClick={handleLogout} title="作废当前会话">登出</button> : null}
          </div>
        </footer>
      </div>
    </PrincipalProvider>
  );
}

export default App;
