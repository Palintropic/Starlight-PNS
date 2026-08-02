import { useEffect, useState } from 'react';
import { fetchConfigProviders, submitConfig, type ProviderOption } from './api';
import './App.css';

interface SetupWizardProps {
  onDone: () => void;
}

function SetupWizard({ onDone }: SetupWizardProps) {
  const [providers, setProviders] = useState<Record<string, ProviderOption>>({});
  const [providerKey, setProviderKey] = useState('');
  const [model, setModel] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchConfigProviders().then(setProviders).catch((e) => setError(e.message));
  }, []);

  const selectedProvider = providerKey ? providers[providerKey] : null;
  const canSubmit = Boolean(providerKey && model && apiKey.trim()) && !submitting;

  const handleSubmit = async () => {
    if (!canSubmit) {
      setError('请完整填写所有字段');
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await submitConfig(providerKey, model, apiKey.trim());
      onDone();
    } catch (e) {
      setError(e instanceof Error ? e.message : '配置失败');
      setSubmitting(false);
    }
  };

  return (
    <div className="setup-shell">
      <div className="setup-wizard">
        <div className="logo">PNS <span>Setup</span></div>
        <h2>首次运行配置</h2>
        <p className="subtitle">选择模型提供商并填入 API Key，配置会保存到本地 .env 文件。</p>

        <label className="setup-field">
          模型提供商
          <select
            value={providerKey}
            onChange={(e) => { setProviderKey(e.target.value); setModel(''); }}
          >
            <option value="">请选择</option>
            {Object.entries(providers).map(([key, p]) => (
              <option key={key} value={key}>{p.name}</option>
            ))}
          </select>
        </label>

        {selectedProvider && (
          <label className="setup-field">
            模型
            <select value={model} onChange={(e) => setModel(e.target.value)}>
              <option value="">请选择</option>
              {selectedProvider.models.map((m) => (
                <option key={m} value={m}>{m}</option>
              ))}
            </select>
          </label>
        )}

        <label className="setup-field">
          API Key
          <input
            type="password"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') handleSubmit(); }}
            placeholder="输入 API Key"
          />
        </label>

        {error && <p className="setup-error">{error}</p>}

        <button className="btn btn-approve" onClick={handleSubmit} disabled={!canSubmit}>
          {submitting ? '保存中…' : '保存并进入'}
        </button>
      </div>
    </div>
  );
}

export default SetupWizard;
