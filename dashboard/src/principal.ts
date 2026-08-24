// dashboard/src/principal.ts — 当前主体与"能不能做"
//
// 前端这一层**只决定显示什么**，不决定允许什么。服务端在中间件里独立地按
// scope 拒绝，所以把一个按钮藏起来是体验问题，不是安全边界——把它显示出来
// 也不会让 observer 真的关掉一个世界。这条分工写在这里，是为了不让以后有人
// 把某个检查"挪到前端做就够了"。
import { createContext, useContext } from 'react';
import type { AuthPrincipal } from './api';

export const PrincipalContext = createContext<AuthPrincipal | null>(null);

export function usePrincipal(): AuthPrincipal | null {
  return useContext(PrincipalContext);
}

/** 当前主体有没有这个 scope。**取不到主体时是 false**，不是 true：
 *  一个"不知道所以先显示"的默认，会让 observer 看到一排点了就报错的按钮。
 *  失败方向也因此是安全的——漏装 Provider 只会少显示按钮。 */
export function useCan(scope: string): boolean {
  const principal = usePrincipal();
  return principal !== null && principal.scopes.includes(scope);
}
