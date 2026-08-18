import { useEffect, useRef, useState } from 'react';
import { fetchConfig, fetchWorldScenes, type ConfigStatus } from './api';
import type { ScenesMap } from './world/types';
import './simulate.css';

interface SimStats {
  total_turns: number;
  ooc_count: number;
  corrections: number;
  avg_score: number;
  max_score: number;
}

type ScriptItem =
  | { kind: 'scene'; label: string; trigger: string; time: string; location: string }
  | {
      kind: 'line';
      turn: number;
      charKey: string;
      charName: string;
      phase: 'generating' | 'judging' | 'filled';
      reply?: string;
      score?: number;
      driftType?: string;
      reason?: string;
      correction?: string | null;
      needsReview?: boolean;
    }
  | { kind: 'sys'; text: string; error?: boolean }
  | { kind: 'done'; stats: SimStats; historyFile?: string };

const levelOf = (score: number) => (score <= 2 ? 'ok' : score <= 5 ? 'warn' : 'ooc');

function Simulate() {
  const [config, setConfig] = useState<ConfigStatus | null>(null);
  const [scenes, setScenes] = useState<ScenesMap>({});
  const [sceneId, setSceneId] = useState('');
  const [maxTurns, setMaxTurns] = useState(8);
  const [temperature, setTemperature] = useState(0.85);
  const [apiDelay, setApiDelay] = useState(1);

  const [running, setRunning] = useState(false);
  const [status, setStatus] = useState('就绪');
  const [statusError, setStatusError] = useState(false);
  const [items, setItems] = useState<ScriptItem[]>([]);
  const [stats, setStats] = useState<SimStats | null>(null);

  const wsRef = useRef<WebSocket | null>(null);
  const scriptRef = useRef<HTMLDivElement | null>(null);
  const endedRef = useRef(false);

  useEffect(() => {
    fetchConfig().then(setConfig).catch(() => {});
    fetchWorldScenes()
      .then((s) => {
        setScenes(s);
        const firstId = Object.keys(s)[0];
        if (firstId) setSceneId(firstId);
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    if (config?.default_scene && scenes[config.default_scene]) {
      setSceneId(config.default_scene);
    }
  }, [config, scenes]);

  useEffect(() => {
    const el = scriptRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [items]);

  useEffect(() => () => wsRef.current?.close(), []);

  const updateLine = (turn: number, patch: Partial<Extract<ScriptItem, { kind: 'line' }>>) => {
    setItems((prev) =>
      prev.map((item) => (item.kind === 'line' && item.turn === turn ? { ...item, ...patch } : item)),
    );
  };

  const clearScript = () => {
    setItems([]);
    setStats(null);
  };

  const startRun = () => {
    if (running) return;
    clearScript();
    endedRef.current = false;
    setRunning(true);
    setStatus('连接中…');
    setStatusError(false);

    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${proto}://${location.host}/ws/run`);
    wsRef.current = ws;

    ws.onopen = () => {
      setStatus('模拟中');
      ws.send(
        JSON.stringify({
          scene: sceneId,
          max_turns: maxTurns,
          temperature,
          api_delay: apiDelay,
        }),
      );
    };

    ws.onmessage = (e) => {
      const msg = JSON.parse(e.data);
      switch (msg.type) {
        case 'start':
          setItems((prev) => [
            ...prev,
            { kind: 'scene', label: msg.scene.label, trigger: msg.scene.trigger, time: msg.scene.time, location: msg.scene.location },
          ]);
          break;
        case 'generating':
          setItems((prev) => [
            ...prev,
            { kind: 'line', turn: msg.turn, charKey: msg.character, charName: msg.char_name, phase: 'generating' },
          ]);
          setStatus(`第 ${msg.turn} 轮 · ${msg.char_name} 生成中`);
          break;
        case 'judging':
          updateLine(msg.turn, { phase: 'judging' });
          setStatus(`第 ${msg.turn} 轮 · Router 判断中`);
          break;
        case 'turn':
          updateLine(msg.turn, {
            phase: 'filled',
            reply: msg.reply,
            score: msg.score,
            driftType: msg.drift_type,
            reason: msg.reason,
            correction: msg.correction,
            needsReview: msg.needs_human_review,
          });
          break;
        case 'done':
          endedRef.current = true;
          setItems((prev) => [...prev, { kind: 'done', stats: msg.stats, historyFile: msg.history_file }]);
          setStats(msg.stats);
          setRunning(false);
          setStatus('完成');
          ws.close();
          break;
        case 'error':
          endedRef.current = true;
          setItems((prev) => [...prev, { kind: 'sys', text: `错误：${msg.message}`, error: true }]);
          setRunning(false);
          setStatus('错误');
          setStatusError(true);
          ws.close();
          break;
      }
    };

    ws.onclose = () => {
      if (!endedRef.current) {
        setRunning(false);
        setStatus('已断开');
      }
    };
    ws.onerror = () => {
      endedRef.current = true;
      setItems((prev) => [...prev, { kind: 'sys', text: 'WebSocket 连接失败', error: true }]);
      setRunning(false);
      setStatus('连接失败');
      setStatusError(true);
    };
  };

  const stopRun = () => {
    endedRef.current = true;
    wsRef.current?.close();
    setRunning(false);
    setStatus('已停止');
    setItems((prev) => [...prev, { kind: 'sys', text: '模拟已手动停止' }]);
  };

  return (
    <div className="simulate-app">
      <header className="sim-topbar">
        <div className={`status-dot${running ? ' running' : ''}${statusError ? ' error' : ''}`} />
        <span className="sim-status-label" style={statusError ? { color: 'var(--warn)' } : undefined}>
          {status}
        </span>
        <div className="sim-spacer" />
        <div className="model-badge">{config?.model || '—'}</div>
      </header>

      <div className="sim-body">
        <aside className="sim-sidebar">
          <div className="sim-control-group">
            <div className="sim-section-label">场景</div>
            <label>选择场景</label>
            <select value={sceneId} onChange={(e) => setSceneId(e.target.value)}>
              {Object.entries(scenes).map(([id, s]) => (
                <option key={id} value={id}>
                  {s.label}
                </option>
              ))}
            </select>
          </div>

          <div className="sim-control-group">
            <div className="sim-section-label">参数</div>
            <label>最大轮次</label>
            <div className="sim-range-row">
              <input
                type="range"
                min={2}
                max={20}
                step={1}
                value={maxTurns}
                onChange={(e) => setMaxTurns(Number(e.target.value))}
              />
              <span className="sim-range-val">{maxTurns}</span>
            </div>
            <label>Temperature</label>
            <div className="sim-range-row">
              <input
                type="range"
                min={0.5}
                max={1.2}
                step={0.05}
                value={temperature}
                onChange={(e) => setTemperature(Number(e.target.value))}
              />
              <span className="sim-range-val">{temperature.toFixed(2)}</span>
            </div>
            <label>API 延迟 (s)</label>
            <div className="sim-range-row">
              <input
                type="range"
                min={0}
                max={5}
                step={0.5}
                value={apiDelay}
                onChange={(e) => setApiDelay(Number(e.target.value))}
              />
              <span className="sim-range-val">{apiDelay.toFixed(1)}</span>
            </div>
          </div>

          <div className="sim-control-group" style={{ marginTop: 'auto' }}>
            <div className="sim-section-label">操作</div>
            <button className="btn btn-start" disabled={running || !sceneId} onClick={startRun}>
              ▶ 开始模拟
            </button>
            <button className="btn btn-stop" disabled={!running} onClick={stopRun}>
              ■ 停止
            </button>
            <button className="btn btn-clear" disabled={running} onClick={clearScript}>
              清空记录
            </button>
          </div>

          <div>
            <div className="sim-section-label" style={{ marginBottom: 10 }}>
              本次统计
            </div>
            <div className="sim-stats-grid">
              <div className="sim-stat-card">
                <div className="stat-value">{stats?.total_turns ?? '—'}</div>
                <div className="sim-stat-lbl">总轮次</div>
              </div>
              <div className="sim-stat-card">
                <div className={`stat-value${stats ? ` ${stats.ooc_count === 0 ? 'ok' : stats.ooc_count <= 2 ? 'warn' : 'ooc'}` : ''}`}>
                  {stats?.ooc_count ?? '—'}
                </div>
                <div className="sim-stat-lbl">OOC次数</div>
              </div>
              <div className="sim-stat-card">
                <div className={`stat-value${stats ? ` ${levelOf(stats.avg_score)}` : ''}`}>{stats?.avg_score ?? '—'}</div>
                <div className="sim-stat-lbl">平均漂移</div>
              </div>
              <div className="sim-stat-card">
                <div className={`stat-value${stats ? ` ${levelOf(stats.max_score)}` : ''}`}>{stats?.max_score ?? '—'}</div>
                <div className="sim-stat-lbl">最高漂移</div>
              </div>
            </div>
          </div>
        </aside>

        <main className="sim-main">
          <div className="sim-script-area" ref={scriptRef}>
            {items.length === 0 && (
              <div className="sim-empty-state">
                <div className="sim-empty-big">🎭</div>
                <p>
                  选择场景并点击「开始模拟」
                  <br />
                  对话将以剧本格式流式显示
                </p>
              </div>
            )}
            {items.map((item, i) => {
              if (item.kind === 'scene') {
                return (
                  <div className="sim-scene-header" key={i}>
                    <div className="sim-scene-label">{item.label}</div>
                    <div className="sim-scene-trigger">{item.trigger}</div>
                    <div className="sim-scene-meta">
                      {item.time} · {item.location}
                    </div>
                  </div>
                );
              }
              if (item.kind === 'line') {
                const level = item.score !== undefined ? levelOf(item.score) : null;
                return (
                  <div className={`sim-script-line ${item.charKey}`} key={i}>
                    <div className="sim-char-col">
                      <div className={`char-name ${item.charKey}`}>{item.charName}</div>
                      <div className="turn-num">#{item.turn}</div>
                    </div>
                    <div className="sim-line-col">
                      <div className={`sim-line-text${item.phase !== 'filled' ? ' generating' : ''}`}>
                        {item.phase === 'filled' ? item.reply : '生成中…'}
                      </div>
                      {level && (
                        <div className={`sim-router-badge ${level}`}>
                          <span className={`score-dot ${level}`} /> Router {item.score}/10 · {item.driftType || '—'}
                        </div>
                      )}
                      {item.reason && (
                        <div className="sim-router-reason">
                          {item.reason}
                          {item.needsReview ? ' ⚑' : ''}
                        </div>
                      )}
                      {item.correction && <div className="sim-correction-pill">⚡ {item.correction}</div>}
                    </div>
                  </div>
                );
              }
              if (item.kind === 'sys') {
                return (
                  <div className={`sim-sys-line${item.error ? ' error' : ''}`} key={i}>
                    {item.text}
                  </div>
                );
              }
              return (
                <div className="sim-done-block" key={i}>
                  <h3>✦ 模拟结束</h3>
                  <div className="sim-done-stats">
                    <div className="sim-done-stat">
                      总轮次 <b>{item.stats.total_turns}</b>
                    </div>
                    <div className="sim-done-stat">
                      OOC次数 <b>{item.stats.ooc_count}</b>
                    </div>
                    <div className="sim-done-stat">
                      Router介入 <b>{item.stats.corrections}次</b>
                    </div>
                    <div className="sim-done-stat">
                      平均漂移 <b>{item.stats.avg_score}/10</b>
                    </div>
                    <div className="sim-done-stat">
                      最高漂移 <b>{item.stats.max_score}/10</b>
                    </div>
                    {item.historyFile && (
                      <div className="sim-done-stat" style={{ gridColumn: '1/-1', color: 'var(--text-muted)', fontFamily: 'var(--mono)', fontSize: 11 }}>
                        📄 {item.historyFile}
                      </div>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </main>
      </div>
    </div>
  );
}

export default Simulate;
