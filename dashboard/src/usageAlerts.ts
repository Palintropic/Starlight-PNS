// dashboard/src/usageAlerts.ts — 用量面板的纯函数与数据 hook（COST-1）
//
// 跟组件分开放，是为了让组件文件只导出组件（fast refresh 的要求），也让
// alertLines 这类纯函数能单独测。
import { useCallback, useEffect, useRef, useState } from 'react';
import { fetchUsageSummary, type UsageFailure, type UsageSummary } from './api';

const POLL_MS = 30_000;

export const FAILURE_TEXT: Record<UsageFailure, string> = {
  payment: '欠费（余额不足）',
  auth: 'key 无效或没有权限',
  rate_limited: '被限流',
  bad_request: '请求被拒',
  server: 'provider 服务器出错',
  network: '连不上或超时',
  unknown: '原因不明',
  unusable: '有响应但没有可用的结果',
  truncated: '输出总在长度上限处被截断',
};

/** 北京时间 MM-DD HH:mm。 */
export const beijingMinute = (iso: string): string => {
  const parts = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).formatToParts(new Date(iso));
  const get = (type: string) => parts.find((p) => p.type === type)?.value ?? '';
  return `${get('month')}-${get('day')} ${get('hour')}:${get('minute')}`;
};

export const yuan = (value: number): string =>
  Math.abs(value) >= 1 ? `¥${value.toFixed(2)}` : `¥${value.toFixed(4)}`;

export const tokens = (value: number): string =>
  value >= 1_000_000
    ? `${(value / 1_000_000).toFixed(2)}M`
    : value >= 1_000
      ? `${(value / 1_000).toFixed(1)}K`
      : String(value);

export const percent = (rate: number | null): string =>
  rate === null ? '—' : `${(rate * 100).toFixed(1)}%`;

/** 定时拉汇总；页面不可见时不拉。 */
export function useUsageSummary(days = 7) {
  const [summary, setSummary] = useState<UsageSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const alive = useRef(true);
  const load = useCallback(() => {
    fetchUsageSummary(days)
      .then((data) => {
        if (!alive.current) return;
        setSummary(data);
        setError(null);
      })
      .catch((e: unknown) => {
        if (!alive.current) return;
        setError(e instanceof Error ? e.message : String(e));
      });
  }, [days]);
  useEffect(() => {
    alive.current = true;
    load();
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') load();
    }, POLL_MS);
    return () => {
      alive.current = false;
      window.clearInterval(timer);
    };
  }, [load]);
  return { summary, error, reload: load };
}

/** 横幅里要说的几句话。纯函数，单独可测。 */
export function alertLines(summary: UsageSummary): { tone: 'bad' | 'caution'; text: string }[] {
  const lines: { tone: 'bad' | 'caution'; text: string }[] = [];
  const streak = summary.failure_streak;
  if (streak.alert && streak.since) {
    // 后端的枚举是封闭的，但界面不该因为一个没见过的值写出 "undefined"。
    const reason =
      (streak.last_failure && FAILURE_TEXT[streak.last_failure]) || FAILURE_TEXT.unknown;
    lines.push({
      tone: 'bad',
      text:
        `模型调用从北京时间 ${beijingMinute(streak.since)} 起连续失败 ${streak.consecutive} 次：${reason}。` +
        '世界里的人现在说不了话。',
    });
  }
  const balance = summary.balance;
  if (balance && balance.low) {
    lines.push({
      tone: 'caution',
      text: `估算余额只剩 ${yuan(balance.estimated_yuan)}（本地估算，低于 ${yuan(balance.warn_yuan)}）。`,
    });
  }
  if (summary.ledger_write_failures > 0) {
    lines.push({
      tone: 'caution',
      text: `用量账本有 ${summary.ledger_write_failures} 次没写进去，统计会偏少。`,
    });
  }
  return lines;
}
