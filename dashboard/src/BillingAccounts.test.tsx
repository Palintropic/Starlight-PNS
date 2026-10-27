// dashboard/src/BillingAccounts.test.tsx — 计费账户目录（CFG-1 §4.3）
//
//   1. 新建提交去掉首尾空白的 id 和显示名，成功后目录交给上层、表单清空；
//      失败时表单留着、照服务端那句话说；
//   2. 修改只发显示名和倍率；
//   3. 退役要先确认，取消就不发请求；`env` 没有退役按钮，退役了的账户没有操作；
//   4. 新建的账户马上出现在模型页的账户下拉里。
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import BillingAccounts from './BillingAccounts';
import ModelSettings from './ModelSettings';
import * as api from './api';
import type { BillingAccount } from './api';

const acct = (id: string, over: Partial<BillingAccount> = {}): BillingAccount => ({
  id,
  name: id === 'env' ? '.env 凭据' : `账户 ${id}`,
  kind: id === 'env' ? 'env' : 'other',
  balance: 'manual',
  currency: 'CNY',
  multiplier: '1',
  created_at: '2026-10-25T00:00:00+00:00',
  ...over,
});

const catalog = (...list: BillingAccount[]) => Object.fromEntries(list.map((a) => [a.id, a]));

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

const field = (form: HTMLElement, label: string) =>
  within(form).getByText(label, { selector: 'span' }).parentElement!.querySelector('input, select') as HTMLInputElement;

const createForm = () => screen.getByText('新建计费账户').closest('form') as HTMLElement;

describe('计费账户', () => {
  it('新建：提交去掉空白，成功后交出目录、清空表单', async () => {
    const relay = acct('relay', { name: '朋友的转发' });
    const create = vi.spyOn(api, 'createBillingAccount').mockResolvedValue({
      account: relay, accounts: catalog(acct('env'), relay),
    });
    const onChange = vi.fn();
    render(<BillingAccounts accounts={catalog(acct('env'))} onChange={onChange} />);
    const form = createForm();
    fireEvent.change(field(form, 'id'), { target: { value: ' relay ' } });
    fireEvent.change(field(form, '显示名'), { target: { value: '朋友的转发 ' } });
    fireEvent.change(field(form, '倍率'), { target: { value: '0.5' } });
    fireEvent.click(within(form).getByRole('button', { name: '新建' }));
    await flush();
    expect(create).toHaveBeenCalledWith({ id: 'relay', name: '朋友的转发', kind: 'other', currency: 'CNY', multiplier: '0.5' });
    expect(onChange).toHaveBeenCalledWith(catalog(acct('env'), relay));
    expect(field(createForm(), 'id').value).toBe('');
  });

  it('新建失败：表单留着，照服务端那句话说', async () => {
    vi.spyOn(api, 'createBillingAccount').mockRejectedValue(
      new api.ApiError('这个账户 id 已经用过了（含已退役的），换一个', 400, 'invalid_account'),
    );
    const onChange = vi.fn();
    render(<BillingAccounts accounts={catalog(acct('env'))} onChange={onChange} />);
    const form = createForm();
    fireEvent.change(field(form, 'id'), { target: { value: 'old' } });
    fireEvent.change(field(form, '显示名'), { target: { value: '旧的' } });
    fireEvent.click(within(form).getByRole('button', { name: '新建' }));
    await flush();
    expect(screen.getByText('这个账户 id 已经用过了（含已退役的），换一个')).toBeTruthy();
    expect(field(createForm(), 'id').value).toBe('old');
    expect(onChange).not.toHaveBeenCalled();
  });

  it('修改只发显示名和倍率', async () => {
    const ds = acct('ds');
    const update = vi.spyOn(api, 'updateBillingAccount').mockResolvedValue({
      account: { ...ds, multiplier: '1.2' }, accounts: catalog(acct('env'), { ...ds, multiplier: '1.2' }),
    });
    render(<BillingAccounts accounts={catalog(acct('env'), ds)} onChange={vi.fn()} />);
    const row = screen.getByText('ds').closest('tr') as HTMLElement;
    fireEvent.click(within(row).getByRole('button', { name: '修改' }));
    const form = screen.getByText('修改 ds').closest('form') as HTMLElement;
    fireEvent.change(field(form, '倍率'), { target: { value: '1.2' } });
    fireEvent.click(within(form).getByRole('button', { name: '保存' }));
    await flush();
    expect(update).toHaveBeenCalledWith('ds', { name: '账户 ds', multiplier: '1.2' });
    expect(screen.queryByText('修改 ds')).toBeNull();
  });

  it('退役要确认；env 没有退役按钮；退役了的没有操作', async () => {
    const retire = vi.spyOn(api, 'retireBillingAccount');
    vi.spyOn(window, 'confirm').mockReturnValue(false);
    render(
      <BillingAccounts
        accounts={catalog(acct('env'), acct('ds'), acct('gone', { retired_at: '2026-10-26T00:00:00+00:00' }))}
        onChange={vi.fn()}
      />,
    );
    const row = (id: string) =>
      screen.getAllByRole('row').find((r) => (r as HTMLTableRowElement).cells[0]?.textContent === id) as HTMLElement;
    expect(within(row('env')).queryByRole('button', { name: '退役' })).toBeNull();
    expect(within(row('gone')).queryAllByRole('button')).toHaveLength(0);
    fireEvent.click(within(row('ds')).getByRole('button', { name: '退役' }));
    expect(retire).not.toHaveBeenCalled();
  });

  it('线路正用着时照服务端那句话说', async () => {
    vi.spyOn(api, 'retireBillingAccount').mockRejectedValue(
      new api.ApiError('这个账户正被模型线路使用，先把线路换到别的账户再退役', 409, 'account_in_use'),
    );
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    render(<BillingAccounts accounts={catalog(acct('env'), acct('ds'))} onChange={vi.fn()} />);
    const row = screen.getByText('ds', { selector: 'td' }).closest('tr') as HTMLElement;
    fireEvent.click(within(row).getByRole('button', { name: '退役' }));
    await flush();
    expect(screen.getByText('这个账户正被模型线路使用，先把线路换到别的账户再退役')).toBeTruthy();
  });

  it('新建的账户马上出现在模型页的账户下拉里', async () => {
    const line = {
      account: 'env', api_format: 'anthropic' as const, base_url: 'https://x.example/anthropic',
      model: 'm', has_token: true, token_hint: null, judge_thinking: 'auto' as const,
    };
    vi.spyOn(api, 'fetchProviderSettings').mockResolvedValue({
      settings: { source: 'env', generation: line, judge: { ...line, follow: null }, ooc_threshold: 5, env_complete: true },
      config_revision: 1, quarantined: false, transaction_pending: false,
    });
    vi.spyOn(api, 'fetchBillingAccounts').mockResolvedValue({ accounts: catalog(acct('env')) });
    const relay = acct('relay', { name: '朋友的转发' });
    vi.spyOn(api, 'createBillingAccount').mockResolvedValue({ account: relay, accounts: catalog(acct('env'), relay) });
    render(<ModelSettings />);
    await flush();
    const form = createForm();
    fireEvent.change(field(form, 'id'), { target: { value: 'relay' } });
    fireEvent.change(field(form, '显示名'), { target: { value: '朋友的转发' } });
    fireEvent.click(within(form).getByRole('button', { name: '新建' }));
    await flush();
    const generation = screen.getByText('生成线路').closest('fieldset') as HTMLElement;
    const options = Array.from(field(generation, '计费账户').querySelectorAll('option')).map((o) => o.value);
    expect(options).toEqual(['env', 'relay']);
  });
});
