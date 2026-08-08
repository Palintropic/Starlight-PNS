import { useEffect, useState } from 'react';
import { fetchWorldScenes, saveWorldScenes } from '../api';
import { blankScene } from './types';
import type { Scene, ScenesMap } from './types';
import { SCENE_FIELDS } from './scenesSchema';
import SourceEditor from './SourceEditor';

const GATE_LETTERS = ['A', 'B', 'C'] as const;

function SceneEditor() {
  const [scenes, setScenes] = useState<ScenesMap>({});
  const [selected, setSelected] = useState<string | null>(null);
  const [draft, setDraft] = useState<Scene | null>(null);
  const [mode, setMode] = useState<'form' | 'source'>('form');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = () => {
    setLoading(true);
    setError(null);
    fetchWorldScenes()
      .then((data) => {
        setScenes(data);
        setSelected((prev) => (prev && data[prev] ? prev : Object.keys(data)[0] ?? null));
      })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  };

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { load(); }, []);

  useEffect(() => {
    setDraft(selected ? (scenes[selected] ?? null) : null);
  }, [selected, scenes]);

  function updateDraft<K extends keyof Scene>(key: K, value: Scene[K]) {
    setDraft((prev) => (prev ? { ...prev, [key]: value } : prev));
  }

  async function persist(next: ScenesMap, afterSaveSelect?: string | null) {
    setSaving(true);
    setError(null);
    try {
      const saved = await saveWorldScenes(next);
      setScenes(saved);
      if (afterSaveSelect !== undefined) setSelected(afterSaveSelect);
    } catch (e) {
      setError(String(e));
    } finally {
      setSaving(false);
    }
  }

  function handleSave() {
    if (!draft) return;
    persist({ ...scenes, [draft.id]: draft });
  }

  function handleAdd() {
    const id = window.prompt('新场景 ID（英文/下划线，作为唯一标识）：');
    if (!id) return;
    if (scenes[id]) {
      window.alert('这个 ID 已经存在了');
      return;
    }
    persist({ ...scenes, [id]: blankScene(id) }, id);
  }

  function handleDelete(id: string) {
    if (!window.confirm(`确定删除场景 "${id}"？这个操作不能撤销。`)) return;
    const next = { ...scenes };
    delete next[id];
    persist(next, Object.keys(next)[0] ?? null);
  }

  const otherSceneIds = draft ? Object.keys(scenes).filter((sid) => sid !== draft.id) : [];

  if (loading) return <div className="empty-hint">加载中…</div>;

  return (
    <div className="world-panel">
      <aside className="world-sidebar">
        <button className="btn btn-add" onClick={handleAdd} disabled={saving}>
          ＋ 新增场景
        </button>
        <div className="world-list">
          {Object.keys(scenes).map((id) => (
            <button
              key={id}
              className={`world-list-item ${id === selected ? 'active' : ''}`}
              onClick={() => setSelected(id)}
            >
              <span className="world-list-id">{id}</span>
              <span className="world-list-label">{scenes[id].label}</span>
            </button>
          ))}
        </div>
      </aside>

      <section className="world-main">
        <div className="world-main-header">
          <span className="col-title">场景表单</span>
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
          <SourceEditor target="scenes" onSaved={load} />
        ) : draft ? (
          <div className="world-form">
            {SCENE_FIELDS.map((field) => {
              const value = draft[field.key];
              if (field.type === 'readonly') {
                return (
                  <label key={field.key} className="world-field">
                    <span>{field.label}</span>
                    <input value={String(value ?? '')} readOnly disabled />
                  </label>
                );
              }
              if (field.type === 'select') {
                return (
                  <label key={field.key} className="world-field">
                    <span>{field.label}</span>
                    <select
                      value={String(value ?? '')}
                      onChange={(e) => updateDraft(field.key, e.target.value as never)}
                    >
                      {field.options!.map((opt) => (
                        <option key={opt} value={opt}>
                          {opt}
                        </option>
                      ))}
                    </select>
                  </label>
                );
              }
              if (field.type === 'textarea') {
                return (
                  <label key={field.key} className="world-field world-field-wide">
                    <span>{field.label}</span>
                    <textarea
                      value={String(value ?? '')}
                      onChange={(e) => updateDraft(field.key, e.target.value as never)}
                    />
                  </label>
                );
              }
              return (
                <label key={field.key} className="world-field">
                  <span>{field.label}</span>
                  <input
                    value={String(value ?? '')}
                    onChange={(e) => updateDraft(field.key, e.target.value as never)}
                  />
                </label>
              );
            })}

            <label className="world-field">
              <span>自动流向场景</span>
              <select
                value={draft.auto_next ?? ''}
                onChange={(e) => updateDraft('auto_next', (e.target.value || null) as never)}
              >
                <option value="">无</option>
                {otherSceneIds.map((sid) => (
                  <option key={sid} value={sid}>
                    {sid}
                  </option>
                ))}
              </select>
            </label>

            <label className="world-field">
              <span>自动流向轮数</span>
              <input
                type="number"
                value={draft.auto_turns ?? ''}
                onChange={(e) =>
                  updateDraft('auto_turns', (e.target.value === '' ? null : Number(e.target.value)) as never)
                }
              />
            </label>

            <label className="world-field world-field-wide">
              <span>Gate 开场备注（可选）</span>
              <textarea
                value={draft.gate_opening_note ?? ''}
                onChange={(e) => updateDraft('gate_opening_note', (e.target.value || null) as never)}
              />
            </label>

            <div className="world-field world-field-wide">
              <span>Gate Triggers（可选，A/B/C 三个切入点）</span>
              {GATE_LETTERS.map((letter) => (
                <div key={letter} className="gate-trigger-row">
                  <span className="gate-trigger-letter">{letter}</span>
                  <textarea
                    value={draft.gate_triggers?.[letter] ?? ''}
                    onChange={(e) => {
                      const next = { ...(draft.gate_triggers ?? {}) };
                      if (e.target.value) {
                        next[letter] = e.target.value;
                      } else {
                        delete next[letter];
                      }
                      updateDraft('gate_triggers', (Object.keys(next).length ? next : null) as never);
                    }}
                  />
                </div>
              ))}
            </div>

            <div className="world-actions">
              <button className="btn btn-approve" disabled={saving} onClick={handleSave}>
                {saving ? '保存中…' : '保存'}
              </button>
              <button className="btn btn-reject" disabled={saving} onClick={() => handleDelete(draft.id)}>
                删除这个场景
              </button>
            </div>
          </div>
        ) : (
          <div className="empty-hint">左侧选一个场景，或新增一个</div>
        )}
      </section>
    </div>
  );
}

export default SceneEditor;
