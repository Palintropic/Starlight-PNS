import { useState } from 'react';
import ReviewDashboard from './ReviewDashboard';
import WorldEditor from './world/WorldEditor';
import './App.css';

type Tab = 'review' | 'world';

function App() {
  const [tab, setTab] = useState<Tab>('review');

  return (
    <div className="shell">
      <nav className="tabbar">
        <button className={`tab-btn ${tab === 'review' ? 'active' : ''}`} onClick={() => setTab('review')}>
          审核
        </button>
        <button className={`tab-btn ${tab === 'world' ? 'active' : ''}`} onClick={() => setTab('world')}>
          World Editor
        </button>
      </nav>
      <div className="shell-body">
        {tab === 'review' ? <ReviewDashboard /> : <WorldEditor />}
      </div>
    </div>
  );
}

export default App;
