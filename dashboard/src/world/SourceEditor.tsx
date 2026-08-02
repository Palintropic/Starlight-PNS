import { useEffect, useState } from 'react';
import CodeMirror from '@uiw/react-codemirror';
import { python } from '@codemirror/lang-python';
import {
  fetchWorldScenesSource, saveWorldScenesSource,
  fetchWorldFactsSource, saveWorldFactsSource,
} from '../api';

interface Props {
  target: 'scenes' | 'facts';
  onSaved?: () => void;
}

const LOADERS = {
  scenes: fetchWorldScenesSource,
  facts: fetchWorldFactsSource,
};
const SAVERS = {
  scenes: saveWorldScenesSource,
  facts: saveWorldFactsSource,
};

function SourceEditor({ target, onSaved }: Props) {
  const [source, setSource] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = () => {
    setLoading(true);
    setError(null);
    LOADERS[target]()
      .then((data) => setSource(data.source))
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  };

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { load(); }, [target]);

  async function handleSave() {
    setSaving(true);
    setError(null);
    try {
      const saved = await SAVERS[target](source);
      setSource(saved.source);
      onSaved?.();
    } catch (e) {
      setError(String(e));
    } finally {
      setSaving(false);
    }
  }

  if (loading) return <div className="empty-hint">加载源码中…</div>;

  return (
    <div className="source-editor">
      <div className="world-actions">
        <button className="btn btn-approve" onClick={handleSave} disabled={saving}>
          {saving ? '保存中…' : '保存源码'}
        </button>
        <button className="btn btn-reject" onClick={load} disabled={saving}>
          放弃改动并重新加载
        </button>
      </div>
      {error && <div className="state-msg error world-error">{error}</div>}
      <CodeMirror
        value={source}
        height="70vh"
        theme="dark"
        extensions={[python()]}
        onChange={(value) => setSource(value)}
      />
    </div>
  );
}

export default SourceEditor;
