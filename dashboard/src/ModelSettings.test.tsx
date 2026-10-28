// dashboard/src/ModelSettings.test.tsx — 模型卡片面（CFG-1 卡片补丁）
//
// 盯 typecheck / lint 证明不了的事：
//
//   1. **卡片列表只来自服务端投影**：token 不回填，末四位只用服务端给的。
//   2. **token 只活在编辑表单里**：提交前同步清空（成功、失败都清），取消、换编辑
//      对象也清；提交的那一份确实带上了；提交之后页面上找不到它。
//   3. **只在成功之后才点亮卡片**：请求挂着时不亮；失败不亮，并重新拉权威状态；
//      文案照服务端那句话说（world_open 和 card_in_use 各说各的）。
//   4. **"编辑"按钮的按键不冒泡成一次切换**。
//   5. **新建只保存、不启用**；默认顺手建同名账户；账户建好、卡片没写成时，表单
//      改成挂到那个账户上，再交一次不再建账户。
//   6. **切换生成卡片不动独立判分**；判分的模式、卡片、覆盖模型一次提交。
//   7. 余额按账户币种和估算状态显示；隔离时卡片点不动；.env 不完整常驻提示；
//      删掉全部卡片要先确认。
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import ModelSettings from './ModelSettings';
import * as api from './api';
import type { AccountBalance, ModelCard, ProviderLine, ProviderSettingsView, UsageSummary } from './api';

const card = (over: Partial<ModelCard> = {}): ModelCard => ({
  id: 'c-relay001',
  name: '朋友的转发',
  account: 'c-relay001',
  api_format: 'anthropic',
  base_url: 'https://relay.example/anthropic',
  model: 'mimo-v2.6-pro',
  has_token: true,
  token_hint: 'abcd',
  judge_thinking: 'off',
  created_at: '2026-10-28T00:00:00+00:00',
  builtin: false,
  ...over,
});

const DS = card({
  id: 'c-ds000002', name: 'DeepSeek', account: 'ds', api_format: 'openai',
  base_url: 'https://api.deepseek.com', model: 'deepseek-chat', token_hint: 'wxyz', judge_thinking: 'auto',
});

const line = (over: Partial<ProviderLine> = {}): ProviderLine => ({
  card: 'env', account: 'env', api_format: 'anthropic', base_url: 'https://api.xiaomimimo.com/anthropic',
  model: 'mimo-v2.6-pro', has_token: true, token_hint: '9999', judge_thinking: 'auto', ...over,
});

const view = (over: Partial<ProviderSettingsView['settings']> = {}, top: Partial<ProviderSettingsView> = {}): ProviderSettingsView => ({
  settings: {
    source: 'panel',
    cards: [card(), DS],
    env_card: {
      id: 'env', name: '.env', account: 'env', builtin: true, available: true,
      api_format: 'anthropic', base_url: 'https://api.xiaomimimo.com/anthropic',
      model: 'mimo-v2.6-pro', judge_model: 'mimo-v2.6-pro', has_token: true, token_hint: '9999',
    },
    active: { generation: 'c-relay001', judge: { follow: 'generation' } },
    generation: line({ card: 'c-relay001' }),
    judge: line({ card: 'c-relay001' }),
    generator_model: 'mimo-v2.6-pro',
    evaluator_model: 'mimo-v2.6-pro',
    ooc_threshold: 5,
    env_complete: true,
    ...over,
  },
  config_revision: 7,
  quarantined: false,
  transaction_pending: false,
  ...top,
});

const acct = (id: string, over: Partial<api.BillingAccount> = {}): api.BillingAccount => ({
  id, name: id, kind: 'other', balance: 'manual', currency: 'CNY', multiplier: '1', created_at: '', ...over,
});

const accounts = { env: acct('env'), ds: acct('ds', { name: 'DeepSeek' }), 'c-relay001': acct('c-relay001', { name: '朋友的转发', multiplier: '1.2' }) };

const balance = (over: Partial<AccountBalance> = {}): AccountBalance => ({
  currency: 'CNY', retired: false, anchor: { balance: 50, currency: 'CNY', at: '' }, anchor_problem: null,
  spent_since_anchor_yuan: 7.5, estimated: 42.5, uncounted_calls: 0, billing_uncertain_calls: 0,
  anchor_before_ledger: false, low: false, warn_yuan: 5, ...over,
});

function stub(initial = view(), balances: Record<string, AccountBalance> | null = {}, unavailable = false) {
  const fetchSettings = vi.spyOn(api, 'fetchProviderSettings').mockResolvedValue(initial);
  vi.spyOn(api, 'fetchBillingAccounts').mockResolvedValue({ accounts });
  vi.spyOn(api, 'fetchUsageSummary').mockResolvedValue({ balances, balances_unavailable: unavailable } as unknown as UsageSummary);
  return fetchSettings;
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

async function flush() {
  await act(async () => {
    for (let i = 0; i < 5; i += 1) await Promise.resolve();
  });
}

const cardEl = (name: string) => screen.getByRole('button', { name: new RegExp(`^${name.replace('.', '\\.')}`) });
const tokenInput = () => screen.getByLabelText('卡片 token') as HTMLInputElement;
const field = (labelText: string) =>
  screen.getByText(labelText, { selector: 'label > span' }).parentElement!.querySelector('input, select') as HTMLInputElement;

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe('模型卡片', () => {
  it('卡片来自服务端投影：token 不回填，只给服务端的末四位', async () => {
    stub();
    render(<ModelSettings />);
    await flush();
    expect(cardEl('朋友的转发').getAttribute('aria-pressed')).toBe('true');
    expect(within(cardEl('DeepSeek')).getByText('api.deepseek.com')).toBeTruthy();
    expect(within(cardEl('.env')).getByText('来自 .env · 只读')).toBeTruthy();
    expect(within(cardEl('.env')).queryByRole('button', { name: '编辑' })).toBeNull();
    fireEvent.click(within(cardEl('DeepSeek')).getByRole('button', { name: '编辑' }));
    expect(tokenInput().value).toBe('');
    expect(tokenInput().placeholder).toContain('wxyz');
  });

  it('点卡片只换生成，判分原样带上；成功之后才点亮', async () => {
    const judged = view({ active: { generation: 'c-relay001', judge: { card: 'env', model: 'small' } } });
    stub(judged);
    const pending = deferred<ProviderSettingsView>();
    const sw = vi.spyOn(api, 'switchActiveCards').mockReturnValue(pending.promise);
    render(<ModelSettings />);
    await flush();
    fireEvent.click(cardEl('DeepSeek'));
    expect(sw).toHaveBeenCalledWith({ generation: 'c-ds000002', judge: { card: 'env', model: 'small' } });
    expect(cardEl('DeepSeek').getAttribute('aria-pressed')).toBe('false');
    pending.resolve(view({ active: { generation: 'c-ds000002', judge: { card: 'env', model: 'small' } } }));
    await flush();
    expect(cardEl('DeepSeek').getAttribute('aria-pressed')).toBe('true');
  });

  it('失败不点亮，重新拉权威状态，照服务端那句话说', async () => {
    const fetchSettings = stub();
    vi.spyOn(api, 'switchActiveCards').mockRejectedValue(
      new api.ApiError('换模型要先关闭所有世界', 409, 'world_open', { worlds: ['nightcord'] }),
    );
    vi.spyOn(api, 'deleteModelCard').mockRejectedValue(
      new api.ApiError('这张卡片正在被生成或判分使用，先切到别的卡片再删除', 409, 'card_in_use'),
    );
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    render(<ModelSettings />);
    await flush();
    const loads = fetchSettings.mock.calls.length;
    fireEvent.click(cardEl('DeepSeek'));
    await flush();
    expect(cardEl('DeepSeek').getAttribute('aria-pressed')).toBe('false');
    expect(screen.getByRole('status').textContent).toBe('换模型要先关闭所有世界（开着的：nightcord）');
    expect(fetchSettings.mock.calls.length).toBeGreaterThan(loads);
    fireEvent.click(within(cardEl('朋友的转发')).getByRole('button', { name: '编辑' }));
    fireEvent.click(screen.getByRole('button', { name: '删除这张卡片' }));
    await flush();
    expect(screen.getByRole('status').textContent).toBe('这张卡片正在被生成或判分使用，先切到别的卡片再删除');
  });

  it('编辑按钮的点击和按键都不会变成一次切换', async () => {
    stub();
    const sw = vi.spyOn(api, 'switchActiveCards');
    render(<ModelSettings />);
    await flush();
    const edit = within(cardEl('DeepSeek')).getByRole('button', { name: '编辑' });
    fireEvent.keyDown(edit, { key: 'Enter' });
    fireEvent.keyDown(edit, { key: ' ' });
    fireEvent.click(edit);
    expect(sw).not.toHaveBeenCalled();
    fireEvent.keyDown(cardEl('DeepSeek'), { key: 'Enter' });
    expect(sw).toHaveBeenCalledTimes(1);
  });

  it('编辑：提交带上 token，提交那一刻就清空，之后页面上找不到它', async () => {
    stub();
    const pending = deferred<ProviderSettingsView>();
    const update = vi.spyOn(api, 'updateModelCard').mockReturnValue(pending.promise);
    render(<ModelSettings />);
    await flush();
    fireEvent.click(within(cardEl('DeepSeek')).getByRole('button', { name: '编辑' }));
    fireEvent.change(tokenInput(), { target: { value: 'sk-rotated-deepseek' } });
    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    expect(tokenInput().value).toBe('');
    expect(update).toHaveBeenCalledWith('c-ds000002', expect.objectContaining({ token: 'sk-rotated-deepseek', account: 'ds' }));
    pending.resolve(view());
    await flush();
    expect(document.body.innerHTML).not.toContain('sk-rotated-deepseek');
  });

  it('失败、取消、换编辑对象都清掉 token', async () => {
    stub();
    vi.spyOn(api, 'updateModelCard').mockRejectedValue(new api.ApiError('新设置没有生效', 400, 'rolled_back'));
    render(<ModelSettings />);
    await flush();
    fireEvent.click(within(cardEl('DeepSeek')).getByRole('button', { name: '编辑' }));
    fireEvent.change(tokenInput(), { target: { value: 'sk-a' } });
    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    await flush();
    expect(tokenInput().value).toBe('');
    fireEvent.change(tokenInput(), { target: { value: 'sk-b' } });
    fireEvent.click(within(cardEl('朋友的转发')).getByRole('button', { name: '编辑' }));
    expect(tokenInput().value).toBe('');
    fireEvent.change(tokenInput(), { target: { value: 'sk-c' } });
    fireEvent.click(screen.getByRole('button', { name: '取消' }));
    fireEvent.click(cardEl('添加服务商'));
    expect(tokenInput().value).toBe('');
    expect(document.body.innerHTML).not.toMatch(/sk-[abc]\b/);
  });

  it('新建只保存不启用：token 必填，默认顺手建同名账户', async () => {
    stub();
    const sw = vi.spyOn(api, 'switchActiveCards');
    const create = vi.spyOn(api, 'createModelCard').mockResolvedValue({ ...view(), card: 'c-new00003', account: 'c-new00003' });
    render(<ModelSettings />);
    await flush();
    fireEvent.click(cardEl('添加服务商'));
    const form = screen.getByRole('heading', { name: '添加服务商' }).closest('form') as HTMLElement;
    fireEvent.click(within(form).getByRole('button', { name: 'DeepSeek' }));
    fireEvent.change(field('模型'), { target: { value: 'deepseek-chat' } });
    expect((screen.getByRole('button', { name: '添加' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(tokenInput(), { target: { value: 'sk-new-card' } });
    fireEvent.click(screen.getByRole('button', { name: '添加' }));
    expect(tokenInput().value).toBe('');
    await flush();
    expect(create).toHaveBeenCalledWith(
      { name: 'DeepSeek', api_format: 'openai', base_url: 'https://api.deepseek.com', model: 'deepseek-chat', token: 'sk-new-card', judge_thinking: 'auto' },
      { kind: 'deepseek', currency: 'CNY', multiplier: '1' },
    );
    expect(sw).not.toHaveBeenCalled();
    expect(screen.queryByLabelText('卡片 token')).toBeNull();
  });

  it('账户建好、卡片没写成：改成挂到那个账户上，再交一次不再建账户', async () => {
    stub();
    const create = vi.spyOn(api, 'createModelCard')
      .mockRejectedValueOnce(new api.ApiError('账户已经建好，卡片没有写成：设置文件没写成', 500, 'card_failed', { account: 'c-half0004' }))
      .mockResolvedValueOnce({ ...view(), card: 'c-new00005', account: null });
    render(<ModelSettings />);
    await flush();
    fireEvent.click(cardEl('添加服务商'));
    fireEvent.change(field('名字'), { target: { value: '转发二号' } });
    fireEvent.change(field('地址（Base URL）'), { target: { value: 'https://relay2.example/anthropic' } });
    fireEvent.change(field('模型'), { target: { value: 'mimo-v2.6-pro' } });
    fireEvent.change(tokenInput(), { target: { value: 'sk-first' } });
    fireEvent.click(screen.getByRole('button', { name: '添加' }));
    await flush();
    expect(screen.getByRole('status').textContent).toContain('c-half0004');
    expect(tokenInput().value).toBe('');
    fireEvent.change(tokenInput(), { target: { value: 'sk-second' } });
    fireEvent.click(screen.getByRole('button', { name: '添加' }));
    await flush();
    expect(create).toHaveBeenLastCalledWith(expect.objectContaining({ account: 'c-half0004', token: 'sk-second' }));
    expect(create.mock.calls[1]).toHaveLength(1);
  });

  it('判分：模式、卡片、覆盖模型一次提交', async () => {
    stub();
    const sw = vi.spyOn(api, 'switchActiveCards').mockResolvedValue(view());
    render(<ModelSettings />);
    await flush();
    expect(screen.queryByRole('button', { name: '应用判分设置' })).toBeNull();
    fireEvent.click(screen.getByRole('radio', { name: '用指定的卡片' }));
    fireEvent.change(screen.getByLabelText('判分卡片'), { target: { value: 'env' } });
    fireEvent.click(screen.getByRole('button', { name: '▸ 换个模型名' }));
    fireEvent.change(field('判分模型（留空用卡片自己的）'), { target: { value: 'mimo-small' } });
    expect(sw).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '应用判分设置' }));
    await flush();
    expect(sw).toHaveBeenCalledTimes(1);
    expect(sw).toHaveBeenCalledWith({ generation: 'c-relay001', judge: { card: 'env', model: 'mimo-small' } });
  });

  it('余额按账户币种和估算状态显示', async () => {
    stub(view(), {
      'c-relay001': balance({ low: true, estimated: 3.2 }),
      ds: balance({ currency: 'USD', estimated: null, anchor: { balance: 12, currency: 'USD', at: '' } }),
      env: balance({ anchor: null, estimated: null }),
    });
    render(<ModelSettings />);
    await flush();
    expect(within(cardEl('朋友的转发')).getByText('¥3.20').className).toContain('mcards-balance-low');
    expect(within(cardEl('DeepSeek')).getByText('US$12.00（锚点）')).toBeTruthy();
    expect(within(cardEl('.env')).getByText('余额 —')).toBeTruthy();
  });

  it('余额估不出来时说出来', async () => {
    stub(view(), null, true);
    render(<ModelSettings />);
    await flush();
    expect(screen.getByText(/余额暂时估不出来/)).toBeTruthy();
  });

  it('隔离时卡片点不动，并说清楚', async () => {
    stub(view({}, { quarantined: true }));
    const sw = vi.spyOn(api, 'switchActiveCards');
    render(<ModelSettings />);
    await flush();
    fireEvent.click(cardEl('DeepSeek'));
    expect(sw).not.toHaveBeenCalled();
    expect(screen.getAllByRole('alert')[0].textContent).toContain('隔离');
  });

  it('.env 凭据不完整时提示不可直接回滚', async () => {
    stub(view({ env_complete: false }));
    render(<ModelSettings />);
    await flush();
    expect(screen.getByRole('alert').textContent).toContain('不可直接回滚');
  });

  it('删掉全部卡片要先确认，取消就不发请求', async () => {
    stub();
    const clear = vi.spyOn(api, 'clearModelCards');
    vi.spyOn(window, 'confirm').mockReturnValue(false);
    render(<ModelSettings />);
    await flush();
    fireEvent.click(screen.getByRole('button', { name: '▸ 高级：计费账户、删掉全部卡片' }));
    fireEvent.click(screen.getByRole('button', { name: '删掉全部卡片，回到 .env' }));
    expect(clear).not.toHaveBeenCalled();
  });
});
