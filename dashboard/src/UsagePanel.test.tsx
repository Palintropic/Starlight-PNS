// dashboard/src/UsagePanel.test.tsx — 用量面板与断供横幅（COST-1）
//
// 盯的是 typecheck 证明不了的几件事：
//
//   1. 连续失败到阈值才标红，并且说出从几点起、失败了几次、为什么；
//   2. 余额永远写"估算"，没算进去的次数要一起说出来；
//   3. 锚点只发余额本身，只读账户看不到输入框；
//   4. 一切正常时横幅什么都不显示；
//   5. CFG-1：余额按账户各显示各的，非人民币不相减，锚点失效说原因；锚点带上
//      选中的账户；低余额横幅按账户报。
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, screen } from '@testing-library/react';
import * as api from './api';
import type { AccountBalance, UsageBucket, UsageSummary } from './api';
import { UsageAlert, UsagePanel } from './UsagePanel';
import { alertLines, beijingMinute } from './usageAlerts';
import { OPERATOR, renderAs } from './testPrincipal';

const bucket = (overrides: Partial<UsageBucket> = {}): UsageBucket => ({
  calls: 10,
  fresh_input: 1200,
  cache_write: 0,
  cache_read: 30000,
  output: 400,
  cost_yuan: 0.0123,
  unpriced: 0,
  no_usage: 0,
  billing_uncertain: 0,
  failed: 0,
  cache_hit_rate: 0.9615,
  ...overrides,
});

const summary = (overrides: Partial<UsageSummary> = {}): UsageSummary => ({
  now: '2026-10-08T14:30:00+00:00',
  timezone: 'Asia/Shanghai',
  days: ['2026-10-02', '2026-10-08'],
  total: bucket({ calls: 70, cost_yuan: 0.5 }),
  by_day: { '2026-10-08': bucket() },
  by_world: { 'yoake-mae': bucket() },
  by_path: { generation: bucket() },
  by_character: { kanade: bucket({ cost_yuan: 0.2 }), mafuyu: bucket({ cost_yuan: 0.3 }) },
  failure_streak: {
    consecutive: 0,
    since: null,
    last_failure: null,
    alert: false,
    alert_threshold: 6,
  },
  balance: null,
  ledger_write_failures: 0,
  corrupt_lines: 0,
  ...overrides,
});

const account = (overrides: Partial<AccountBalance> = {}): AccountBalance => ({
  currency: 'CNY',
  retired: false,
  anchor: null,
  anchor_problem: null,
  spent_since_anchor_yuan: null,
  estimated: null,
  uncounted_calls: 0,
  billing_uncertain_calls: 0,
  anchor_before_ledger: null,
  low: false,
  warn_yuan: 10,
  ...overrides,
});

const anchored = (balance: number, spent: number, overrides: Partial<AccountBalance> = {}) =>
  account({
    anchor: { balance, currency: overrides.currency ?? 'CNY', at: '2026-10-08T14:00:00+00:00' },
    spent_since_anchor_yuan: spent,
    estimated: (overrides.currency ?? 'CNY') === 'CNY' ? balance - spent : null,
    anchor_before_ledger: false,
    ...overrides,
  });

const failing = summary({
  failure_streak: {
    consecutive: 248,
    since: '2026-10-07T23:56:00+00:00',
    last_failure: 'payment',
    alert: true,
    alert_threshold: 6,
  },
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('alertLines', () => {
  it('says nothing when all is well', () => {
    expect(alertLines(summary())).toEqual([]);
  });

  it('turns red at the threshold with when, how many and why', () => {
    const [line] = alertLines(failing);
    expect(line.tone).toBe('bad');
    expect(line.text).toContain('10-08 07:56');
    expect(line.text).toContain('248 次');
    expect(line.text).toContain('欠费');
  });

  it('never writes undefined for a reason it does not know', () => {
    const odd = summary({
      failure_streak: {
        ...failing.failure_streak,
        last_failure: 'failed' as unknown as UsageSummary['failure_streak']['last_failure'],
      },
    });
    const [line] = alertLines(odd);
    expect(line.text).not.toContain('undefined');
    expect(line.text).toContain('原因不明');
  });

  it('does not alert below the threshold', () => {
    const quiet = summary({
      failure_streak: { ...failing.failure_streak, consecutive: 3, alert: false },
    });
    expect(alertLines(quiet)).toEqual([]);
  });

  it('warns on a low estimated balance and on ledger write failures', () => {
    const lines = alertLines(
      summary({
        balance: {
          anchor: { balance_yuan: 12, at: '2026-10-08T14:00:00+00:00' },
          spent_since_anchor_yuan: 3,
          estimated_yuan: 9,
          uncounted_calls: 0,
          billing_uncertain_calls: 0,
          anchor_before_ledger: false,
          low: true,
          warn_yuan: 10,
        },
        ledger_write_failures: 2,
      }),
    );
    expect(lines.map((l) => l.tone)).toEqual(['caution', 'caution']);
    expect(lines[0].text).toContain('估算');
  });

  it('formats in Beijing time', () => {
    expect(beijingMinute('2026-10-08T16:01:00+00:00')).toBe('10-09 00:01');
  });
});

describe('UsageAlert', () => {
  it('shows the red line', async () => {
    vi.spyOn(api, 'fetchUsageSummary').mockResolvedValue(failing);
    await act(async () => {
      renderAs(OPERATOR, <UsageAlert />);
    });
    expect(screen.getByRole('alert').textContent).toContain('说不了话');
  });

  it('renders nothing when healthy', async () => {
    vi.spyOn(api, 'fetchUsageSummary').mockResolvedValue(summary());
    await act(async () => {
      renderAs(OPERATOR, <UsageAlert />);
    });
    expect(screen.queryByRole('alert')).toBeNull();
  });
});

describe('UsagePanel', () => {
  it('labels the balance as an estimate and lists what was not counted', async () => {
    vi.spyOn(api, 'fetchUsageSummary').mockResolvedValue(
      summary({
        balance: {
          anchor: { balance_yuan: 99.42, at: '2026-10-08T14:40:00+00:00' },
          spent_since_anchor_yuan: 0.0123,
          estimated_yuan: 99.4077,
          uncounted_calls: 3,
          billing_uncertain_calls: 2,
          anchor_before_ledger: false,
          low: false,
          warn_yuan: 10,
        },
      }),
    );
    await act(async () => {
      renderAs(OPERATOR, <UsagePanel />);
    });
    expect(screen.getByText('余额（本地估算）')).toBeTruthy();
    expect(screen.getByText('¥99.41')).toBeTruthy();
    const note = screen.getByText(/往下扣了/).textContent ?? '';
    expect(note).toContain('3 次调用没有花费数据');
    expect(note).toContain('2 次可能已经扣了钱');
  });

  it('sends the balance itself as a number', async () => {
    vi.spyOn(api, 'fetchUsageSummary').mockResolvedValue(summary());
    const save = vi.spyOn(api, 'setBalanceAnchor').mockResolvedValue({
      account: 'env', balance: 99.42, currency: 'CNY', at: '2026-10-08T14:40:00+00:00',
    });
    await act(async () => {
      renderAs(OPERATOR, <UsagePanel />);
    });
    fireEvent.change(screen.getByPlaceholderText('比如 99.42'), { target: { value: '99.42' } });
    await act(async () => {
      fireEvent.click(screen.getByText('记下余额'));
    });
    expect(save).toHaveBeenCalledWith(99.42, 'env');
    expect(screen.getByText(/记下了/)).toBeTruthy();
  });

  it('hides the anchor input from read-only accounts', async () => {
    vi.spyOn(api, 'fetchUsageSummary').mockResolvedValue(summary());
    await act(async () => {
      renderAs(null, <UsagePanel />);
    });
    expect(screen.queryByText('记下余额')).toBeNull();
    expect(screen.getByText('模型用量')).toBeTruthy();
  });

  it('lists residents by cost, highest first', async () => {
    vi.spyOn(api, 'fetchUsageSummary').mockResolvedValue(summary());
    await act(async () => {
      renderAs(OPERATOR, <UsagePanel />);
    });
    const people = screen.getAllByText(/^(kanade|mafuyu)$/).map((el) => el.textContent);
    expect(people).toEqual(['mafuyu', 'kanade']);
  });
});

describe('UsagePanel per account (CFG-1)', () => {
  it('shows each account on its own, never subtracting a non-CNY one', async () => {
    vi.spyOn(api, 'fetchUsageSummary').mockResolvedValue(
      summary({
        balances: {
          env: anchored(100, 3),
          relay: anchored(20, 1.5, { currency: 'USD' }),
          ds: account({ anchor_problem: 'account_recreated' }),
          gone: account({ retired: true }),
        },
        by_account: { env: bucket(), relay: bucket() },
      }),
    );
    await act(async () => {
      renderAs(OPERATOR, <UsagePanel />);
    });
    expect(screen.getByText('¥97.00')).toBeTruthy();
    const usd = screen.getByText(/20\.00 USD/).textContent ?? '';
    expect(usd).toContain('之后本账户花了');
    expect(usd).toContain('不相减');
    expect(screen.getByText(/账户被重建过/)).toBeTruthy();
    // 退役、没锚点、没花钱：不占地方，也不出现在锚点的账户下拉里。
    expect(screen.queryByText('gone')).toBeNull();
    const options = Array.from(screen.getByRole('combobox').querySelectorAll('option')).map((o) => o.value);
    expect(options).toEqual(['env', 'relay', 'ds']);
  });

  it('sends the anchor for the chosen account, in its currency', async () => {
    vi.spyOn(api, 'fetchUsageSummary').mockResolvedValue(
      summary({ balances: { env: account(), relay: account({ currency: 'USD' }) } }),
    );
    const save = vi.spyOn(api, 'setBalanceAnchor').mockResolvedValue({
      account: 'relay', balance: 12.5, currency: 'USD', at: '2026-10-08T14:40:00+00:00',
    });
    await act(async () => {
      renderAs(OPERATOR, <UsagePanel />);
    });
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'relay' } });
    expect(screen.getByText(/控制台上此刻的余额（USD）/)).toBeTruthy();
    fireEvent.change(screen.getByPlaceholderText('比如 99.42'), { target: { value: '12.5' } });
    await act(async () => {
      fireEvent.click(screen.getByText('记下余额'));
    });
    expect(save).toHaveBeenCalledWith(12.5, 'relay');
    expect(screen.getByText(/relay 此刻余额 12\.50 USD/)).toBeTruthy();
  });

  it('asks for a new anchor when it is older than the last version switch', async () => {
    vi.spyOn(api, 'fetchUsageSummary').mockResolvedValue(
      summary({
        balances: {
          env: anchored(100, 3, { anchor_before_switch: true }),
          relay: anchored(20, 1, { anchor_before_switch: false }),
        },
      }),
    );
    await act(async () => {
      renderAs(OPERATOR, <UsagePanel />);
    });
    expect(screen.getAllByText(/早于最近一次版本切换/)).toHaveLength(1);
  });

  it('says so when balances could not be estimated', async () => {
    vi.spyOn(api, 'fetchUsageSummary').mockResolvedValue(
      summary({ balances: {}, balances_unavailable: true }),
    );
    await act(async () => {
      renderAs(OPERATOR, <UsagePanel />);
    });
    expect(screen.getByText(/一个余额都没估/)).toBeTruthy();
  });

  it('warns per account when an account runs low', () => {
    const lines = alertLines(
      summary({ balances: { env: anchored(100, 1), relay: anchored(12, 5, { low: true }) } }),
    );
    expect(lines).toHaveLength(1);
    expect(lines[0].text).toContain('账户 relay');
    expect(lines[0].text).toContain('¥7');
  });
});
