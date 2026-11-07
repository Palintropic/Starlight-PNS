// dashboard/src/Simulate.test.tsx — 模拟页处理 `stopped`（FIX-WS-1 P3）
//
// 盯的是 typecheck 证明不了的两件事：
//
//   1. 身份在一轮中途失效：服务端发了 `generating`，接着发 `stopped`
//      （reason=authorization_lost），然后以 1008 关闭、不发 `done`。
//      页面要停下来、说清楚原因，那一行不能一直停在"生成中…"。
//   2. 配置重载造成的停止后面还有 `done`，这时不能提前当成结束。
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import Simulate from './Simulate';
import * as api from './api';
import type { ConfigStatus } from './api';
import type { ScenesMap } from './world/types';

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  sent: string[] = [];
  closed = false;

  url: string;

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }
  send(data: string) {
    this.sent.push(data);
  }
  close() {
    if (this.closed) return;
    this.closed = true;
    this.onclose?.();
  }
  open() {
    act(() => this.onopen?.());
  }
  receive(msg: Record<string, unknown>) {
    act(() => this.onmessage?.({ data: JSON.stringify(msg) }));
  }
  serverClose() {
    act(() => this.close());
  }
}

const scenes = {
  gate: { label: '校门口', trigger: '放学', time: '17:00', location: '校门' },
} as unknown as ScenesMap;

const start = {
  type: 'start',
  session_id: 's1',
  scene: { id: 'gate', label: '校门口', trigger: '放学', time: '17:00', location: '校门' },
  world: {},
  max_turns: 8,
  model: 'm',
};

const turn1 = {
  type: 'turn',
  turn: 1,
  character: 'mizuki',
  char_name: '瑞希',
  reply: '第一句',
  score: 1,
  is_ooc: false,
  drift_type: 'none',
  reason: '',
  correction: null,
  needs_human_review: false,
};

async function startRun(): Promise<FakeWebSocket> {
  render(<Simulate />);
  const button = await screen.findByRole('button', { name: /开始模拟/ });
  await vi.waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(button);
  const ws = FakeWebSocket.instances.at(-1)!;
  ws.open();
  ws.receive(start);
  ws.receive({ type: 'generating', turn: 1, character: 'mizuki', char_name: '瑞希' });
  ws.receive(turn1);
  ws.receive({ type: 'generating', turn: 2, character: 'ena', char_name: '绘名' });
  return ws;
}

beforeEach(() => {
  FakeWebSocket.instances = [];
  vi.stubGlobal('WebSocket', FakeWebSocket);
  vi.spyOn(api, 'fetchConfig').mockResolvedValue({ model: 'm', default_scene: 'gate' } as unknown as ConfigStatus);
  vi.spyOn(api, 'fetchWorldScenes').mockResolvedValue(scenes);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('Simulate stopped handling', () => {
  it('stops and explains when identity is lost mid-turn', async () => {
    const ws = await startRun();
    expect(screen.getByText('生成中…')).toBeTruthy();

    ws.receive({ type: 'stopped', session_id: 's1', turn: 2, reason: 'authorization_lost' });
    ws.serverClose();

    expect(screen.queryByText('生成中…')).toBeNull();
    expect(screen.getByText('本轮未完成，已丢弃')).toBeTruthy();
    expect(screen.getByText(/登录已失效或权限已收回/)).toBeTruthy();
    // 状态停在身份失效，而不是被 onclose 改成"已断开"。
    expect(screen.getByText('身份已失效')).toBeTruthy();
    expect(screen.queryByText('已断开')).toBeNull();
    // 已完成的那一轮保留。
    expect(screen.getByText('第一句')).toBeTruthy();
    expect((screen.getByRole('button', { name: /开始模拟/ }) as HTMLButtonElement).disabled).toBe(false);
  });

  it('keeps waiting for done after a reload stop', async () => {
    const ws = await startRun();

    ws.receive({ type: 'stopped', session_id: 's1', turn: 2, reason: 'config_reload' });
    expect(screen.getByText('正在收尾')).toBeTruthy();
    expect((screen.getByRole('button', { name: /停止/ }) as HTMLButtonElement).disabled).toBe(false);

    ws.receive({
      type: 'done',
      session_id: 's1',
      stats: { total_turns: 1, ooc_count: 0, corrections: 0, avg_score: 1, max_score: 1 },
      history_file: 'h.json',
    });
    expect(screen.getByText('完成')).toBeTruthy();
    expect(screen.queryByText('生成中…')).toBeNull();
  });
});
