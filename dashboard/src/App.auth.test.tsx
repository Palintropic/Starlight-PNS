// dashboard/src/App.auth.test.tsx — 登录门与角色可见性（DEPLOY-1 / AUTH-1）
//
// 只盯 lint / typecheck / build 都证明不了的事：
//
//   1. **要凭据的服务器上，没登录之前一条管理请求都不发。** 否则后台会拿着
//      一串 401 把界面停在"加载配置失败"上，而真正该出现的是登录框。
//   2. **会话中途失效要退回登录。** 会话过期是随时会发生的事（服务器重启过、
//      TTL 到了、别处登出了、账户被停用），"什么都加载不出来但不说为什么"
//      是最难查的坏法。
//   3. **不要凭据的开发服务器行为不变。** 生产的严格不是靠给本地开发加一道
//      门换来的。
//   4. **角色决定看得到哪些入口。** observer 看不到会花额度或改状态的页签，
//      operator 看不到用户管理。这一层只管显示——服务端独立地拒绝，所以这几
//      条用例证明的是"不摆一个点了会 403 的按钮"，不是"权限在这里把住了"。
//
// 凭据只在登录那一刻经过浏览器；这里也顺带钉住"它不进 localStorage"。
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import App from './App';
import * as api from './api';
import type { AuthPrincipal, AuthSession } from './api';

const principal = (
  role: string,
  scopes: string[],
  extra: Partial<AuthPrincipal> = {},
): AuthPrincipal => ({
  principal_id: `p-${role}`,
  username: role === 'admin' ? 'mizuki' : role,
  kind: 'human',
  role,
  scopes,
  via: 'session',
  ...extra,
});

const production: AuthSession = {
  mode: 'production',
  auth_required: true,
  authenticated: false,
  principal: null,
};

const session = (role: string, scopes: string[]): AuthSession => ({
  mode: 'production',
  auth_required: true,
  authenticated: true,
  principal: principal(role, scopes),
});

const loggedIn = session('admin', ['read', 'operate', 'accounts:manage']);
const asOperator = session('operator', ['read', 'operate']);
const asObserver = session('observer', ['read']);

const development: AuthSession = {
  mode: 'development',
  auth_required: false,
  authenticated: true,
  principal: {
    principal_id: 'svc-open-development',
    username: 'open-development',
    kind: 'service',
    role: 'admin',
    scopes: ['read', 'operate', 'accounts:manage'],
    via: 'open-development',
  },
};

const config = {
  has_key: true,
  model: 'm',
  generator_model: 'm',
  evaluator_model: 'm',
  api_format: 'openai',
  default_scene: 'gate',
};

/** 只读账户默认落在"持久世界"页签上，那一页开局要拉三样东西。 */
function stubWorldTab() {
  vi.spyOn(api, 'fetchPersistentWorlds').mockResolvedValue({ worlds: [] });
  vi.spyOn(api, 'fetchWorldScenes').mockResolvedValue({});
  vi.spyOn(api, 'fetchReloadStatus').mockResolvedValue({
    reloading: false,
    stop_timeout: 30,
    accepting_sessions: true,
    live_sessions: [],
    registry: null,
    last_reload: null,
  });
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  localStorage.clear();
});

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe('登录门', () => {
  it('没登录时显示登录框，并且一条管理请求都不发', async () => {
    vi.spyOn(api, 'fetchAuthSession').mockResolvedValue(production);
    const fetchConfig = vi.spyOn(api, 'fetchConfig').mockResolvedValue(config);

    render(<App />);
    await flush();

    expect(screen.getByText('需要登录')).toBeTruthy();
    expect(fetchConfig).not.toHaveBeenCalled();
  });

  it('用用户名和密码登录，登录成功之后才去取配置', async () => {
    const fetchSession = vi
      .spyOn(api, 'fetchAuthSession')
      .mockResolvedValueOnce(production)
      .mockResolvedValue(loggedIn);
    const login = vi.spyOn(api, 'login').mockResolvedValue(loggedIn);
    const fetchConfig = vi.spyOn(api, 'fetchConfig').mockResolvedValue(config);

    render(<App />);
    await flush();

    const username = screen.getByPlaceholderText('用户名') as HTMLInputElement;
    const password = screen.getByPlaceholderText('密码') as HTMLInputElement;
    // 密码框必须是密码框：肩窥和截图是这台机器最现实的威胁。
    expect(password.type).toBe('password');
    expect(username.type).toBe('text');

    await act(async () => {
      fireEvent.change(username, { target: { value: 'mizuki' } });
      fireEvent.change(password, { target: { value: 'a-very-long-password-000' } });
    });
    await act(async () => {
      fireEvent.click(screen.getByText('登录'));
    });
    await flush();

    expect(login).toHaveBeenCalledWith('mizuki', 'a-very-long-password-000');
    expect(fetchSession).toHaveBeenCalledTimes(2);
    expect(fetchConfig).toHaveBeenCalled();
    // 凭据不许留在浏览器里——会话是一张 HttpOnly Cookie，不是 localStorage。
    expect(localStorage.length).toBe(0);
  });

  it('登录框不提 PNS_ADMIN_TOKEN —— 那把 token 不是网页口令', async () => {
    vi.spyOn(api, 'fetchAuthSession').mockResolvedValue(production);
    vi.spyOn(api, 'fetchConfig').mockResolvedValue(config);

    render(<App />);
    await flush();

    expect(screen.queryByPlaceholderText('PNS_ADMIN_TOKEN')).toBeNull();
    expect(document.body.textContent).not.toContain('PNS_ADMIN_TOKEN');
  });

  it('会话中途失效时退回登录', async () => {
    vi.spyOn(api, 'fetchAuthSession').mockResolvedValue(loggedIn);
    vi.spyOn(api, 'fetchConfig').mockResolvedValue(config);

    render(<App />);
    await flush();
    expect(screen.queryByText('需要登录')).toBeNull();

    await act(async () => {
      window.dispatchEvent(new CustomEvent(api.UNAUTHENTICATED_EVENT));
    });
    await flush();

    expect(screen.getByText('需要登录')).toBeTruthy();
  });

  it('没配凭据的开发服务器不弹登录框', async () => {
    vi.spyOn(api, 'fetchAuthSession').mockResolvedValue(development);
    const fetchConfig = vi.spyOn(api, 'fetchConfig').mockResolvedValue(config);

    render(<App />);
    await flush();

    expect(screen.queryByText('需要登录')).toBeNull();
    expect(fetchConfig).toHaveBeenCalled();
    // 登出按钮只在真的有会话可登出的时候出现。
    expect(screen.queryByText('登出')).toBeNull();
    // 服务主体没有密码可改。
    expect(screen.queryByText('修改密码')).toBeNull();
  });
});

describe('角色可见性', () => {
  it('管理员看得到用户管理和自己的身份', async () => {
    vi.spyOn(api, 'fetchAuthSession').mockResolvedValue(loggedIn);
    vi.spyOn(api, 'fetchConfig').mockResolvedValue(config);

    render(<App />);
    await flush();

    expect(screen.getByText('用户管理')).toBeTruthy();
    expect(screen.getByText('mizuki · admin')).toBeTruthy();
    expect(screen.getByText('修改密码')).toBeTruthy();
  });

  it('operator 看不到用户管理，但仍然看得到会花额度的入口', async () => {
    vi.spyOn(api, 'fetchAuthSession').mockResolvedValue(asOperator);
    vi.spyOn(api, 'fetchConfig').mockResolvedValue(config);

    render(<App />);
    await flush();

    expect(screen.queryByText('用户管理')).toBeNull();
    expect(screen.getByText('模拟')).toBeTruthy();
    expect(screen.getByText('重新加载配置')).toBeTruthy();
  });

  it('observer 看不到任何写入口', async () => {
    stubWorldTab();
    vi.spyOn(api, 'fetchAuthSession').mockResolvedValue(asObserver);
    vi.spyOn(api, 'fetchConfig').mockResolvedValue(config);

    render(<App />);
    await flush();

    expect(screen.queryByText('用户管理')).toBeNull();
    expect(screen.queryByText('模拟')).toBeNull();
    expect(screen.queryByText('重新加载配置')).toBeNull();
    // 只读账户不该落在一个空白页签上：持久世界那一页在（页签 + 标题各一个），
    // 但那一页上的新建表单和每行的操作按钮都不该出现。
    expect(screen.getAllByText('持久世界').length).toBeGreaterThan(0);
    expect(screen.queryByText('新建世界')).toBeNull();
  });
});
