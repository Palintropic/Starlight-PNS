// dashboard/src/ConfigReload.tsx — 后台的「重新加载配置」按钮
//
// 点一次 = 关闭准入闸门 → 停掉所有正在跑的会话并等它们退出 → 从磁盘重建并校验
// 全部配置 → 成功就整体切换、失败就继续用上一份可用配置。请求会一直挂到重载结束，
// 所以按钮在这期间是禁用的。改 Python 代码不走这里：那属于 cold update，必须停服
// 替换文件再重启。
import { useEffect, useState } from 'react';
import { fetchReloadStatus, reloadConfig, type ReloadStatus } from './api';

type Outcome =
  | { kind: 'idle' }
  | { kind: 'running' }
  | { kind: 'ok'; revision: number; stopped: string[] }
  | { kind: 'error'; message: string };

export default function ConfigReload() {
  const [status, setStatus] = useState<ReloadStatus | null>(null);
  const [outcome, setOutcome] = useState<Outcome>({ kind: 'idle' });

  const refresh = () => {
    fetchReloadStatus()
      .then(setStatus)
      .catch(() => undefined);
  };

  useEffect(refresh, []);

  const onClick = () => {
    if (outcome.kind === 'running') return;
    setOutcome({ kind: 'running' });
    reloadConfig()
      .then((result) => {
        setOutcome({
          kind: 'ok',
          revision: result.revision,
          stopped: result.stopped_sessions,
        });
        refresh();
      })
      .catch((e: unknown) => {
        setOutcome({
          kind: 'error',
          message: e instanceof Error ? e.message : '重新加载失败',
        });
        refresh();
      });
  };

  const live = status?.live_sessions.length ?? 0;

  return (
    <div className="reload-box">
      <button
        className="reload-btn"
        onClick={onClick}
        disabled={outcome.kind === 'running'}
        title={
          live > 0
            ? `当前有 ${live} 个会话在跑，重新加载会先把它们停掉并等它们退出`
            : '从磁盘重新读取并校验全部配置'
        }
      >
        {outcome.kind === 'running' ? '重新加载中…' : '重新加载配置'}
      </button>
      <span className={`reload-status ${outcome.kind}`}>
        {outcome.kind === 'idle' && status?.registry
          ? `配置 rev.${status.registry.revision}`
          : null}
        {outcome.kind === 'running' ? '停止会话、等待退出、重建配置…' : null}
        {outcome.kind === 'ok'
          ? `已生效 rev.${outcome.revision}` +
            (outcome.stopped.length ? ` · 停止了 ${outcome.stopped.length} 个会话` : '')
          : null}
        {outcome.kind === 'error' ? outcome.message : null}
      </span>
    </div>
  );
}
