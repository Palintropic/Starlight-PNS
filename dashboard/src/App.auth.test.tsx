// dashboard/src/App.auth.test.tsx — 登录门（DEPLOY-1）
//
// 只盯三件 lint / typecheck / build 都证明不了的事：
//
//   1. **要凭据的服务器上，没登录之前一条管理请求都不发。** 否则后台会拿着
//      一串 401 把界面停在"加载配置失败"上，而真正该出现的是登录框。
//   2. **会话中途失效要退回登录。** 会话过期是随时会发生的事（服务器重启过、
//      TTL 到了、别处登出了），"什么都加载不出来但不说为什么"是最难查的坏法。
//   3. **不要凭据的开发服务器行为不变。** 生产的严格不是靠给本地开发加一道
//      门换来的。
//
// 管理凭据只在登录那一刻经过浏览器；这里也顺带钉住"它不进 localStorage"。
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import App from './App';
import * as api from './api';

const production = { mode: 'production', auth_required: true, authenticated: false };
const loggedIn = { mode: 'production', auth_required: true, authenticated: true };
const development = { mode: 'development', auth_required: false, authenticated: true };

const config = {
  has_key: true,
  model: 'm',
  generator_model: 'm',
  evaluator_model: 'm',
  api_format: 'openai',
  default_scene: 'gate',
};

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

    expect(screen.getByText('需要管理凭据')).toBeTruthy();
    expect(fetchConfig).not.toHaveBeenCalled();
  });

  it('登录成功之后才去取配置', async () => {
    const session = vi
      .spyOn(api, 'fetchAuthSession')
      .mockResolvedValueOnce(production)
      .mockResolvedValue(loggedIn);
    const login = vi.spyOn(api, 'login').mockResolvedValue(loggedIn);
    const fetchConfig = vi.spyOn(api, 'fetchConfig').mockResolvedValue(config);

    render(<App />);
    await flush();

    const input = screen.getByPlaceholderText('PNS_ADMIN_TOKEN') as HTMLInputElement;
    // 凭据输入框必须是密码框：肩窥和截图是这台机器最现实的威胁。
    expect(input.type).toBe('password');

    await act(async () => {
      fireEvent.change(input, { target: { value: 'a-very-long-admin-token-value-000000' } });
    });
    await act(async () => {
      fireEvent.click(screen.getByText('登录'));
    });
    await flush();

    expect(login).toHaveBeenCalledWith('a-very-long-admin-token-value-000000');
    expect(session).toHaveBeenCalledTimes(2);
    expect(fetchConfig).toHaveBeenCalled();
    // 凭据不许留在浏览器里——会话是一张 HttpOnly Cookie，不是 localStorage。
    expect(localStorage.length).toBe(0);
  });

  it('会话中途失效时退回登录', async () => {
    vi.spyOn(api, 'fetchAuthSession').mockResolvedValue(loggedIn);
    vi.spyOn(api, 'fetchConfig').mockResolvedValue(config);

    render(<App />);
    await flush();
    expect(screen.queryByText('需要管理凭据')).toBeNull();

    await act(async () => {
      window.dispatchEvent(new CustomEvent(api.UNAUTHENTICATED_EVENT));
    });
    await flush();

    expect(screen.getByText('需要管理凭据')).toBeTruthy();
  });

  it('没配凭据的开发服务器不弹登录框', async () => {
    vi.spyOn(api, 'fetchAuthSession').mockResolvedValue(development);
    const fetchConfig = vi.spyOn(api, 'fetchConfig').mockResolvedValue(config);

    render(<App />);
    await flush();

    expect(screen.queryByText('需要管理凭据')).toBeNull();
    expect(fetchConfig).toHaveBeenCalled();
    // 登出按钮只在真的有会话可登出的时候出现。
    expect(screen.queryByText('登出')).toBeNull();
  });
});
