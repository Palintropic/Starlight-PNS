import { useEffect, useState } from 'react';
import { fetchWorldFacts, saveWorldFacts } from '../api';
import SourceEditor from './SourceEditor';

interface FactRowProps {
  k: string;
  value: string;
  onChange: (key: string, value: string) => void;
  onRename: (key: string) => void;
  onDelete: (key: string) => void;
}

function FactRow({ k, value, onChange, onRename, onDelete }: FactRowProps) {
  return (
    <div className="fact-row">
      <div className="fact-row-head">
        <button className="fact-key" onClick={() => onRename(k)} title="点击改名（会二次确认）">
          {k}
        </button>
        <button className="fact-delete" onClick={() => onDelete(k)} title="删除">
          ✕
        </button>
      </div>
      <textarea value={value} onChange={(e) => onChange(k, e.target.value)} />
    </div>
  );
}

function FactsEditor() {
  const [groups, setGroups] = useState<Record<string, string[]>>({});
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [mode, setMode] = useState<'form' | 'source'>('form');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = () => {
    setLoading(true);
    setError(null);
    fetchWorldFacts()
      .then((data) => {
        setGroups(data.groups);
        setDraft(data.facts);
        setDirty(false);
      })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  };

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { load(); }, []);

  function updateValue(key: string, value: string) {
    setDraft((prev) => ({ ...prev, [key]: value }));
    setDirty(true);
  }

  function handleRename(oldKey: string) {
    const newKey = window.prompt(`把 "${oldKey}" 改名为：`, oldKey);
    if (!newKey || newKey === oldKey) return;
    if (draft[newKey] !== undefined) {
      window.alert('这个 key 已经存在了');
      return;
    }
    if (!window.confirm(`确定把 "${oldKey}" 改名为 "${newKey}"？其他地方如果引用了旧 key 需要手动改。`)) return;
    setDraft((prev) => {
      const next = { ...prev };
      next[newKey] = next[oldKey];
      delete next[oldKey];
      return next;
    });
    setDirty(true);
  }

  function handleDelete(key: string) {
    if (!window.confirm(`确定删除 "${key}"？这个操作不能撤销。`)) return;
    setDraft((prev) => {
      const next = { ...prev };
      delete next[key];
      return next;
    });
    setDirty(true);
  }

  function handleAdd() {
    const key = window.prompt('新增 key（英文/下划线）：');
    if (!key) return;
    if (draft[key] !== undefined) {
      window.alert('这个 key 已经存在了');
      return;
    }
    setDraft((prev) => ({ ...prev, [key]: '' }));
    setDirty(true);
  }

  async function handleSave() {
    setSaving(true);
    setError(null);
    try {
      const saved = await saveWorldFacts(draft);
      setGroups(saved.groups);
      setDraft(saved.facts);
      setDirty(false);
    } catch (e) {
      setError(String(e));
    } finally {
      setSaving(false);
    }
  }

  const groupedKeys = new Set(Object.values(groups).flat());
  const ungrouped = Object.keys(draft).filter((k) => !groupedKeys.has(k));

  if (loading) return <div className="empty-hint">加载中…</div>;

  return (
    <div className="world-panel world-panel-single">
      <section className="world-main">
        <div className="world-main-header">
          <span className="col-title">世界设定 Facts</span>
          <div className="world-mode-switch">
            <button className={mode === 'form' ? 'active' : ''} onClick={() => setMode('form')}>
              表单模式
            </button>
            <button className={mode === 'source' ? 'active' : ''} onClick={() => setMode('source')}>
              源码模式
            </button>
          </div>
        </div>

        {error && <div className="state-msg error world-error">{error}</div>}

        {mode === 'source' ? (
          <SourceEditor target="facts" onSaved={load} />
        ) : (
          <div className="world-form facts-form">
            <div className="world-actions">
              <button className="btn btn-add" onClick={handleAdd} disabled={saving}>
                ＋ 新增 key
              </button>
              <button className="btn btn-approve" onClick={handleSave} disabled={saving || !dirty}>
                {saving ? '保存中…' : dirty ? '保存所有改动' : '没有改动'}
              </button>
            </div>

            {Object.entries(groups).map(([groupName, keys]) => {
              const present = keys.filter((k) => k in draft);
              if (present.length === 0) return null;
              return (
                <div key={groupName} className="facts-group">
                  <div className="facts-group-title">{groupName}</div>
                  {present.map((key) => (
                    <FactRow
                      key={key}
                      k={key}
                      value={draft[key]}
                      onChange={updateValue}
                      onRename={handleRename}
                      onDelete={handleDelete}
                    />
                  ))}
                </div>
              );
            })}

            {ungrouped.length > 0 && (
              <div className="facts-group">
                <div className="facts-group-title">未分组</div>
                {ungrouped.map((key) => (
                  <FactRow
                    key={key}
                    k={key}
                    value={draft[key]}
                    onChange={updateValue}
                    onRename={handleRename}
                    onDelete={handleDelete}
                  />
                ))}
              </div>
            )}
          </div>
        )}
      </section>
    </div>
  );
}

export default FactsEditor;
