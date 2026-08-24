// dashboard/src/Accounts.test.tsx — 用户管理面（AUTH-1）
//
// 盯四件 typecheck / lint 证明不了的事：
//
//   1. **密码提交之后不回显。** 一个还留着刚才那串字符的输入框，等于把凭据
//      留在屏幕上和 DOM 里。
//   2. **改权威要先确认。** 停用会把目标踢下线，取消就必须**一条请求都不发**。
//   3. **踢掉了几张会话要说出来。** 那是这个操作的安全承诺，不是副作用；
//      不说出来的话，操作者没法确认撤销真的发生了。
//   4. **失败要照服务端那句话说。** 最后一个管理员那条 409 是有用信息，
//      不能被翻译成一句泛泛的"操作失败"。
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import Accounts from './Accounts';
import * as api from './api';
import type { Account } from './api';

const account = (over: Partial<Account> = {}): Account => ({
  principal_id: 'p-1',
  username: 'mizuki',
  kind: 'human',
  role: 'admin',
  scopes: ['read', 'operate', 'accounts:manage'],
  enabled: true,
  created_at: '2026-08-24T10:00:00+00:00',
  updated_at: '2026-08-24T10:00:00+00:00',
  ...over,
});

const ena = account({ principal_id: 'p-2', username: 'ena', role: 'operator' });

function stubList(users: Account[]) {
  return vi.spyOn(api, 'fetchAccounts').mockResolvedValue({ users });
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

describe('用户管理', () => {
  it('列出服务端给的账户，不自己编一份', async () => {
    stubList([account(), ena]);
    vi.spyOn(api, 'fetchAuditRecords').mockResolvedValue({ records: [] });

    render(<Accounts />);
    await flush();

    expect(screen.getByText('mizuki')).toBeTruthy();
    expect(screen.getByText('ena')).toBeTruthy();
  });

  it('创建之后密码框被清空，密码不留在 DOM 里', async () => {
    stubList([account()]);
    vi.spyOn(api, 'fetchAuditRecords').mockResolvedValue({ records: [] });
    const create = vi
      .spyOn(api, 'createAccount')
      .mockResolvedValue(account({ principal_id: 'p-3', username: 'kanade', role: 'observer' }));

    render(<Accounts />);
    await flush();

    const name = screen.getByPlaceholderText('字母数字和 . _ -') as HTMLInputElement;
    const password = screen.getByPlaceholderText('至少 12 个字符') as HTMLInputElement;
    expect(password.type).toBe('password');

    await act(async () => {
      fireEvent.change(name, { target: { value: 'kanade' } });
      fireEvent.change(password, { target: { value: 'a-very-long-password-1' } });
    });
    await act(async () => {
      fireEvent.click(screen.getByText('创建'));
    });
    await flush();

    expect(create).toHaveBeenCalledWith('kanade', 'a-very-long-password-1', 'observer');
    expect(password.value).toBe('');
    expect(document.body.innerHTML).not.toContain('a-very-long-password-1');
  });

  it('停用被取消时一条请求都不发', async () => {
    stubList([account(), ena]);
    vi.spyOn(api, 'fetchAuditRecords').mockResolvedValue({ records: [] });
    const disable = vi.spyOn(api, 'setAccountEnabled');
    vi.spyOn(window, 'confirm').mockReturnValue(false);

    render(<Accounts />);
    await flush();

    await act(async () => {
      fireEvent.click(screen.getAllByText('停用')[1]);
    });
    await flush();

    expect(disable).not.toHaveBeenCalled();
  });

  it('停用成功之后说出踢掉了几张会话', async () => {
    stubList([account(), ena]);
    vi.spyOn(api, 'fetchAuditRecords').mockResolvedValue({ records: [] });
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    vi.spyOn(api, 'setAccountEnabled').mockResolvedValue(
      account({ ...ena, enabled: false, revoked_sessions: 2 }),
    );

    render(<Accounts />);
    await flush();

    await act(async () => {
      fireEvent.click(screen.getAllByText('停用')[1]);
    });
    await flush();

    expect(screen.getByText('ena 已停用，踢掉了 2 张会话')).toBeTruthy();
  });

  it('最后一个管理员那条 409 照服务端的话说', async () => {
    stubList([account()]);
    vi.spyOn(api, 'fetchAuditRecords').mockResolvedValue({ records: [] });
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    vi.spyOn(api, 'setAccountEnabled').mockRejectedValue(
      new api.ApiError('这是最后一个可用的管理员，不能停用或降级', 409, 'last_admin'),
    );

    render(<Accounts />);
    await flush();

    await act(async () => {
      fireEvent.click(screen.getByText('停用'));
    });
    await flush();

    expect(screen.getByText(/最后一个可用的管理员/)).toBeTruthy();
  });
});
