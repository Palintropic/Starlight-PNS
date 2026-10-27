// dashboard/src/ModelSettings.test.tsx — 模型线路面（CFG-1）
//
// 盯 typecheck / lint 证明不了的事：
//
//   1. **token 不回填、提交即清空**——成功、失败都清；提交的那一份确实带上了。
//   2. **跟随**提交的是 `{follow: 'generation'}`，不是一份长得一样的独立线路。
//   3. **有世界开着时照服务端那句话说**，不翻译成泛泛的"保存失败"。
//   4. **.env 凭据不完整时常驻"不可直接回滚"**（§9）。
//   5. **恢复默认要先确认**，取消就一条请求都不发。
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import ModelSettings from './ModelSettings';
import * as api from './api';
import type { ProviderLine, ProviderSettingsView } from './api';

const line = (over: Partial<ProviderLine> = {}): ProviderLine => ({
  account: 'mimo-main',
  api_format: 'anthropic',
  base_url: 'https://gen.example/anthropic',
  model: 'gen-model',
  has_token: true,
  token_hint: 'abcd',
  judge_thinking: 'auto',
  ...over,
});

const view = (over: Partial<ProviderSettingsView['settings']> = {}): ProviderSettingsView => ({
  settings: {
    source: 'panel',
    generation: line(),
    judge: { ...line({ account: 'ds', api_format: 'openai', base_url: 'https://judge.example/v1', model: 'judge-model', token_hint: 'wxyz' }), follow: null },
    ooc_threshold: 5,
    env_complete: true,
    ...over,
  },
  config_revision: 7,
  quarantined: false,
  transaction_pending: false,
});

const accounts = {
  'mimo-main': { id: 'mimo-main', name: 'MiMo 主账号', kind: 'mimo', balance: 'manual' as const, currency: 'CNY' as const, multiplier: '1', created_at: '' },
  ds: { id: 'ds', name: 'DeepSeek', kind: 'deepseek', balance: 'manual' as const, currency: 'CNY' as const, multiplier: '1', created_at: '' },
};

function stub(initial = view()) {
  vi.spyOn(api, 'fetchProviderSettings').mockResolvedValue(initial);
  vi.spyOn(api, 'fetchBillingAccounts').mockResolvedValue({ accounts });
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

const tokenInput = (name: '生成线路' | '判分线路') =>
  screen.getByLabelText(`${name} token`) as HTMLInputElement;

describe('模型线路', () => {
  it('token 不回填，只给末四位提示', async () => {
    stub();
    render(<ModelSettings />);
    await flush();
    expect(tokenInput('生成线路').value).toBe('');
    expect(tokenInput('生成线路').placeholder).toContain('abcd');
    expect(tokenInput('判分线路').placeholder).toContain('wxyz');
  });

  it('提交带上 token，提交后输入框清空', async () => {
    stub();
    const save = vi.spyOn(api, 'saveProviderSettings').mockResolvedValue(view());
    render(<ModelSettings />);
    await flush();
    fireEvent.change(tokenInput('生成线路'), { target: { value: 'sk-new-generation' } });
    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    expect(tokenInput('生成线路').value).toBe('');
    await flush();
    expect(save).toHaveBeenCalledTimes(1);
    const sent = save.mock.calls[0][0];
    expect(sent.generation.token).toBe('sk-new-generation');
    expect(sent.judge).toMatchObject({ account: 'ds', token: '' });
    expect(document.body.innerHTML).not.toContain('sk-new-generation');
  });

  it('失败也清空 token，并照服务端那句话说', async () => {
    stub();
    vi.spyOn(api, 'saveProviderSettings').mockRejectedValue(
      new api.ApiError('换模型要先关闭所有世界，保存后再恢复', 409, 'world_open'),
    );
    render(<ModelSettings />);
    await flush();
    fireEvent.change(tokenInput('判分线路'), { target: { value: 'sk-judge' } });
    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    await flush();
    expect(tokenInput('判分线路').value).toBe('');
    expect(screen.getByText('换模型要先关闭所有世界，保存后再恢复')).toBeTruthy();
  });

  it('跟随提交的是 follow，不是一份独立线路', async () => {
    stub();
    const save = vi.spyOn(api, 'saveProviderSettings').mockResolvedValue(view());
    render(<ModelSettings />);
    await flush();
    fireEvent.click(screen.getByRole('checkbox'));
    expect(screen.queryByLabelText('判分线路 token')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    await flush();
    expect(save.mock.calls[0][0].judge).toEqual({ follow: 'generation' });
  });

  it('已经跟随时勾选框是勾上的，判分线路不显示', async () => {
    stub(view({ judge: { ...line(), follow: 'generation' } }));
    render(<ModelSettings />);
    await flush();
    expect((screen.getByRole('checkbox') as HTMLInputElement).checked).toBe(true);
    expect(screen.queryByLabelText('判分线路 token')).toBeNull();
  });

  it('.env 凭据不完整时提示不可直接回滚', async () => {
    stub(view({ env_complete: false }));
    render(<ModelSettings />);
    await flush();
    expect(screen.getByRole('alert').textContent).toContain('不可直接回滚');
  });

  it('恢复默认取消就不发请求', async () => {
    stub();
    const reset = vi.spyOn(api, 'resetProviderSettings');
    vi.spyOn(window, 'confirm').mockReturnValue(false);
    render(<ModelSettings />);
    await flush();
    fireEvent.click(screen.getByRole('button', { name: '恢复 .env 默认' }));
    expect(reset).not.toHaveBeenCalled();
  });
});
