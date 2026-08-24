// dashboard/src/testPrincipal.tsx — 测试里的主体上下文
//
// `useCan` 在**没有主体**时返回 false，所以脱离 `PrincipalProvider` 单独渲染
// 一个控制面组件，得到的是只读视图。那是刻意的失败方向（漏装上下文 → 少显示
// 按钮，而不是多显示），但它意味着盯写入口的用例必须自己给一个主体。
import type { ReactElement } from 'react';
import { render } from '@testing-library/react';
import { PrincipalProvider } from './PrincipalProvider';
import type { AuthPrincipal } from './api';

export const OPERATOR: AuthPrincipal = {
  principal_id: 'p-operator',
  username: 'operator',
  kind: 'human',
  role: 'operator',
  scopes: ['read', 'operate'],
  via: 'session',
};

export function renderAs(principal: AuthPrincipal | null, element: ReactElement) {
  return render(<PrincipalProvider principal={principal}>{element}</PrincipalProvider>);
}
