import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import Login from './Login';
import { followSsoLogout, type AuthSession } from './api';

afterEach(() => {
  cleanup();
  window.history.replaceState(null, '', '/');
  vi.restoreAllMocks();
});

it('offers SSO first and keeps local login collapsed, with no automatic redirect', () => {
  render(<Login onDone={() => undefined} ssoEnabled />);
  const link = screen.getByRole('link', { name: '用 Palintropic 账户登录' });
  expect(link.getAttribute('href')).toBe('/api/auth/sso/login');
  const local = screen.getByText('使用本地账户').closest('details')!;
  expect(local.open).toBe(false);
  fireEvent.click(screen.getByText('使用本地账户'));
  expect(local.open).toBe(true);
  expect(screen.getByLabelText('用户名')).toBeTruthy();
  expect(window.location.pathname).toBe('/');
});

it('keeps the existing local form when SSO is not configured', () => {
  render(<Login onDone={() => undefined} />);
  expect(screen.queryByRole('link', { name: '用 Palintropic 账户登录' })).toBeNull();
  expect(screen.queryByText('使用本地账户')).toBeNull();
  expect(screen.getByLabelText('密码')).toBeTruthy();
});

it.each([
  ['idp_unavailable', '账户服务暂时连不上，可以先用本地账户登录'],
  ['sso_busy', '现在发起不了 Palintropic 登录，稍后再试，或者先用本地账户登录'],
  ['no_role', '这个 Palintropic 账户没有 PNS 的权限'],
  ['account_disabled', '这个账户已被 PNS 停用，请联系管理员'],
  ['unknown_role', '这个账户的登录配置需要管理员处理'],
  ['ambiguous_role', '这个账户的登录配置需要管理员处理'],
  ['account_conflict', '这个账户的登录配置需要管理员处理'],
  ['state_invalid', '登录没有完成，请重试'],
  ['idp_denied', '登录没有完成，请重试'],
  ['token_invalid', '登录没有完成，请重试'],
  ['private-raw-exception', '登录没有完成，请重试'],
])('shows actionable SSO error %s without echoing arbitrary query text', (category, message) => {
  window.history.replaceState(null, '', '/?sso_error='+category);
  render(<Login onDone={() => undefined} ssoEnabled />);
  expect(screen.getByRole('alert').textContent).toBe(message);
  expect(window.location.search).toBe('');
});

it('reports partial logout when PNS has already revoked its session', () => {
  render(<Login onDone={() => undefined} ssoEnabled ssoLogoutIncomplete />);
  expect(screen.getByRole('status').textContent).toBe('PNS 已退出，Palintropic 账户可能还登着');
});

it('follows the server logout URL and tolerates failed navigation', () => {
  const session: AuthSession = { mode: 'production', auth_required: true,
    authenticated: false, principal: null, sso_logout_url: 'https://id.example/logout', sso_signed_out: false };
  const navigate = vi.fn();
  expect(followSsoLogout(session, navigate).authenticated).toBe(false);
  expect(navigate).toHaveBeenCalledWith(session.sso_logout_url);
  const failed = followSsoLogout(session, () => { throw new Error('offline'); });
  expect(failed.sso_signed_out).toBe(false);
  expect(failed.authenticated).toBe(false);
  navigate.mockClear();
  followSsoLogout({ ...session, sso_logout_url: null }, navigate);
  expect(navigate).not.toHaveBeenCalled();
});
