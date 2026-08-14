import { useEffect, useMemo, useState } from 'react';
import { fetchDecisions, fetchTurns, submitDecision } from './api';
import { decisionKey } from './types';
import type { Decision, DecisionMap, DecisionValue, Turn } from './types';
import './App.css';

function scoreLevel(score: number): 'ok' | 'warn' | 'ooc' {
  return score <= 2 ? 'ok' : score <= 5 ? 'warn' : 'ooc';
}

const DECISION_LABEL: Record<DecisionValue, string> = {
  approve: '✓ 通过',
  reject: '✕ 拒绝',
  rewrite: '✎ 需要重写',
};

const DIMENSION_LABEL: Record<string, string> = {
  character_facts: '角色事实',
  psychological_mechanism: '心理机制',
  language_structure: '语言结构',
  media_authenticity: '媒介真实性',
  task_compliance: '任务遵循',
  unsupported_invention: '无依据补写',
  timeline_boundary: '时间线边界',
};

function Avatar({ character, name }: { character: string; name: string }) {
  return <span className={`avatar ${character}`}>{name.charAt(0)}</span>;
}

function Skeleton() {
  return (
    <div className="review-app">
      <header className="topbar">
        <div className="logo">PNS <span>Review</span> Dashboard</div>
      </header>
      <div className="stats-strip">
        {Array.from({ length: 4 }).map((_, i) => (
          <div key={i} className="stat-tile skel-block" />
        ))}
      </div>
      <div className="columns">
        {[0, 1, 2].map((c) => (
          <section key={c} className="col">
            <div className="col-header"><div className="skel-block skel-title" /></div>
            <div className="skel-body">
              {Array.from({ length: c === 0 ? 4 : 2 }).map((_, i) => (
                <div key={i} className="skel-block skel-card" />
              ))}
            </div>
          </section>
        ))}
      </div>
    </div>
  );
}

function ReviewDashboard() {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [decisions, setDecisions] = useState<DecisionMap>({});
  const [selected, setSelected] = useState<string | null>(null);
  const [note, setNote] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    Promise.all([fetchTurns(), fetchDecisions()])
      .then(([turnList, decisionMap]) => {
        setTurns(turnList);
        setDecisions(decisionMap);
        if (turnList.length > 0) {
          setSelected(decisionKey(turnList[0].session_id, turnList[0].turn));
        }
      })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, []);

  const selectedTurn = useMemo(
    () => turns.find((t) => decisionKey(t.session_id, t.turn) === selected) ?? null,
    [turns, selected],
  );

  const selectedDecision: Decision | undefined = selected ? decisions[selected] : undefined;

  useEffect(() => {
    setNote(selectedDecision?.note ?? '');
  }, [selected, selectedDecision?.note]);

  const sessions = useMemo(() => {
    const map = new Map<string, Turn[]>();
    for (const t of turns) {
      const list = map.get(t.session_id) ?? [];
      list.push(t);
      map.set(t.session_id, list);
    }
    return [...map.entries()];
  }, [turns]);

  const stats = useMemo(() => {
    const total = turns.length;
    const needsReview = turns.filter((t) => t.needs_human_review).length;
    const avg = total ? turns.reduce((s, t) => s + t.drift_score, 0) / total : 0;
    const decided = Object.keys(decisions).length;
    return { total, needsReview, avg: Math.round(avg * 10) / 10, decided };
  }, [turns, decisions]);

  async function handleDecide(decision: DecisionValue) {
    if (!selectedTurn) return;
    setSubmitting(true);
    try {
      const record = await submitDecision(selectedTurn, decision, note || undefined);
      setDecisions((prev) => ({
        ...prev,
        [decisionKey(record.session_id, record.turn)]: record,
      }));
    } catch (e) {
      setError(String(e));
    } finally {
      setSubmitting(false);
    }
  }

  if (loading) return <Skeleton />;
  if (error) return <div className="state-msg error">出错了：{error}</div>;

  return (
    <div className="review-app">
      <header className="topbar">
        <div className="logo">PNS <span>Review</span> Dashboard</div>
        <span className="subtitle">人工审核 · Router 打分复核</span>
      </header>

      <div className="stats-strip">
        <div className="stat-tile">
          <div className="stat-value">{stats.total}</div>
          <div className="stat-label">总轮次</div>
        </div>
        <div className="stat-tile">
          <div className={`stat-value ${stats.needsReview > 0 ? 'warn' : 'ok'}`}>{stats.needsReview}</div>
          <div className="stat-label">待复核</div>
        </div>
        <div className="stat-tile">
          <div className={`stat-value ${scoreLevel(stats.avg)}`}>{stats.avg}</div>
          <div className="stat-label">平均漂移</div>
        </div>
        <div className="stat-tile">
          <div className="stat-value">
            {stats.decided}<span className="stat-value-of">/{stats.total}</span>
          </div>
          <div className="stat-label">已决策</div>
        </div>
      </div>

      <div className="columns">
        {/* ① 对话展示 */}
        <section className="col col-transcript">
          <div className="col-header">
            <span className="col-title">对话</span>
            <span className="col-context">{turns.length} 轮</span>
          </div>
          <div className="turn-list">
            {sessions.map(([sessionId, sessionTurns]) => (
              <div key={sessionId} className="session-group">
                <div className="session-header">{sessionId}</div>
                {sessionTurns.map((t) => {
                  const key = decisionKey(t.session_id, t.turn);
                  const d = decisions[key];
                  return (
                    <button
                      key={key}
                      className={`turn-item ${t.character} ${key === selected ? 'active' : ''}`}
                      onClick={() => setSelected(key)}
                    >
                      <Avatar character={t.character} name={t.char_name} />
                      <div className="turn-item-body">
                        <div className="turn-item-head">
                          <span className={`char-name ${t.character}`}>{t.char_name}</span>
                          <span className="turn-num">#{t.turn}</span>
                          <span className={`score-dot ${scoreLevel(t.drift_score)}`} />
                          {d && <span className={`decision-chip ${d.decision}`}>{DECISION_LABEL[d.decision]}</span>}
                        </div>
                        <div className="turn-item-text">{t.text}</div>
                      </div>
                    </button>
                  );
                })}
              </div>
            ))}
          </div>
        </section>

        {/* ② Router 打分 */}
        <section className="col col-router">
          <div className="col-header">
            <span className="col-title">Router 打分</span>
            {selectedTurn && (
              <span className="col-context">
                <Avatar character={selectedTurn.character} name={selectedTurn.char_name} />
                {selectedTurn.char_name} · #{selectedTurn.turn}
              </span>
            )}
          </div>
          {selectedTurn ? (
            <div className="router-detail" key={selected}>
              <div className={`score-badge ${scoreLevel(selectedTurn.drift_score)}`}>
                {selectedTurn.drift_score}<span className="score-badge-max">/10</span>
              </div>
              <div className="score-bar">
                <div
                  className={`score-bar-fill ${scoreLevel(selectedTurn.drift_score)}`}
                  style={{ width: `${Math.min(100, (selectedTurn.drift_score / 10) * 100)}%` }}
                />
              </div>
              <div className="detail-row">
                <span className="detail-label">类型</span>
                <span>{selectedTurn.drift_type}</span>
              </div>
              <div className="detail-row">
                <span className="detail-label">置信度</span>
                <span>{Math.round(selectedTurn.confidence * 100)}%</span>
              </div>
              <div className="detail-row">
                <span className="detail-label">需人工复核</span>
                <span>{selectedTurn.needs_human_review ? '是 ⚑' : '否'}</span>
              </div>
              <div className="detail-block">
                <div className="detail-label">原因</div>
                <p>{selectedTurn.reason}</p>
              </div>
              {selectedTurn.dimensions && (
                <div className="dimension-list">
                  <div className="detail-label">七维验收</div>
                  {Object.entries(selectedTurn.dimensions).map(([key, value]) => (
                    <div className="dimension-item" key={key} title={value.reason}>
                      <span>{DIMENSION_LABEL[key] ?? key}</span>
                      <strong className={scoreLevel(value.score)}>{value.score}/10</strong>
                    </div>
                  ))}
                </div>
              )}
              {selectedTurn.correction && (
                <div className="detail-block correction">
                  <div className="detail-label">⚡ 建议纠正</div>
                  <p>{selectedTurn.correction}</p>
                </div>
              )}
            </div>
          ) : (
            <div className="empty-hint">选择左侧一条台词查看打分</div>
          )}
        </section>

        {/* ③ 人工决策 */}
        <section className="col col-decision">
          <div className="col-header">
            <span className="col-title">人工决策</span>
            {selectedTurn && (
              <span className="col-context">
                <Avatar character={selectedTurn.character} name={selectedTurn.char_name} />
                {selectedTurn.char_name} · #{selectedTurn.turn}
              </span>
            )}
          </div>
          {selectedTurn ? (
            <div className="decision-panel" key={selected}>
              {selectedDecision && (
                <div className={`current-decision ${selectedDecision.decision}`} key={selectedDecision.decided_at}>
                  当前：{DECISION_LABEL[selectedDecision.decision]}
                  <div className="decided-at">{selectedDecision.decided_at}</div>
                </div>
              )}
              <textarea
                className="note-input"
                placeholder="备注（可选）…"
                value={note}
                onChange={(e) => setNote(e.target.value)}
              />
              <div className="decision-buttons">
                <button
                  className="btn btn-approve"
                  disabled={submitting}
                  onClick={() => handleDecide('approve')}
                >
                  ✓ 通过
                </button>
                <button
                  className="btn btn-rewrite"
                  disabled={submitting}
                  onClick={() => handleDecide('rewrite')}
                >
                  ✎ 需要重写
                </button>
                <button
                  className="btn btn-reject"
                  disabled={submitting}
                  onClick={() => handleDecide('reject')}
                >
                  ✕ 拒绝
                </button>
              </div>
            </div>
          ) : (
            <div className="empty-hint">选择左侧一条台词进行审核</div>
          )}
        </section>
      </div>
    </div>
  );
}

export default ReviewDashboard;
