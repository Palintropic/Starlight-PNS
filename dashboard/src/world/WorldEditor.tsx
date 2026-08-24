import { useState } from 'react';
import SceneEditor from './SceneEditor';
import FactsEditor from './FactsEditor';
import { SCOPE_OPERATE } from '../api';
import { useCan } from '../principal';
import './world.css';

type Section = 'scenes' | 'facts';

function WorldEditor() {
  const [section, setSection] = useState<Section>('scenes');
  const canWrite = useCan(SCOPE_OPERATE);

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
      {canWrite ? null : (
        <p className="world-readonly">
          当前账户只有只读权限：内容看得到，保存按钮是灰的。服务端也会拒绝写入，
          所以这不是一层只做样子的禁用。
        </p>
      )}
      {section === 'scenes' ? <SceneEditor /> : <FactsEditor />}
    </div>
  );
}

export default WorldEditor;
