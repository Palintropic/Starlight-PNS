import { useState } from 'react';
import SceneEditor from './SceneEditor';
import FactsEditor from './FactsEditor';
import './world.css';

type Section = 'scenes' | 'facts';

function WorldEditor() {
  const [section, setSection] = useState<Section>('scenes');

  return (
    <div className="world-editor">
      <div className="world-subtabs">
        <button className={section === 'scenes' ? 'active' : ''} onClick={() => setSection('scenes')}>
          场景 Scenes
        </button>
        <button className={section === 'facts' ? 'active' : ''} onClick={() => setSection('facts')}>
          世界设定 Facts
        </button>
      </div>
      {section === 'scenes' ? <SceneEditor /> : <FactsEditor />}
    </div>
  );
}

export default WorldEditor;
