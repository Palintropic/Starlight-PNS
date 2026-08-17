import { useEffect, useState } from 'react';
import ReviewDashboard from './ReviewDashboard';
import WorldEditor from './world/WorldEditor';
import SetupWizard from './SetupWizard';
import Simulate from './Simulate';
import { fetchConfig, type ConfigStatus } from './api';
import './App.css';

type Tab = 'simulate' | 'review' | 'world';

function App() {
  const [tab, setTab] = useState<Tab>('simulate');
  const [config, setConfig] = useState<ConfigStatus | null>(null);
  const [configError, setConfigError] = useState<string | null>(null);

  const loadConfig = () => {
    setConfigError(null);
    fetchConfig()
      .then(setConfig)
      .catch((e) => setConfigError(e instanceof Error ? e.message : '加载配置失败'));
  };

  useEffect(() => {
    loadConfig();
  }, []);

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
      </nav>
      <div className="shell-body">
        {tab === 'simulate' ? <Simulate /> : tab === 'review' ? <ReviewDashboard /> : <WorldEditor />}
      </div>
    </div>
  );
}

export default App;
