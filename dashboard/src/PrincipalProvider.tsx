// dashboard/src/PrincipalProvider.tsx — 把当前主体发给整棵树
// 判据本身和读它的钩子在 principal.ts 里。
import type { ReactNode } from 'react';
import { PrincipalContext } from './principal';
import type { AuthPrincipal } from './api';

export function PrincipalProvider({
  principal,
  children,
}: {
  principal: AuthPrincipal | null;
  children: ReactNode;
}) {
  return (
    <PrincipalContext.Provider value={principal}>{children}</PrincipalContext.Provider>
  );
}

export default PrincipalProvider;
