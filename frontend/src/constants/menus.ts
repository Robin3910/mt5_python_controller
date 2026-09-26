// 菜单 code 与后端 permissions.MENU_REGISTRY 一致；顺序即顶部导航顺序，也是无权限时的回退顺序
export const MENU_PATHS: ReadonlyArray<{ code: string; path: string }> = [
  { code: 'dashboard', path: '/' },
  { code: 'nodes', path: '/nodes' },
  { code: 'trend', path: '/trend' },
  { code: 'groups', path: '/groups' },
  { code: 'strategies', path: '/strategies' },
  { code: 'console', path: '/console' },
  { code: 'client_versions', path: '/client-versions' },
  { code: 'events', path: '/events' },
  { code: 'audits', path: '/audits' },
  { code: 'permissions', path: '/permissions' },
]

/** 配置页（个人设置）不挂菜单码，所有登录用户都能进，是最终回退页 */
export const FALLBACK_PATH = '/config'

/** 第一个有权限的菜单路径；一个菜单都没有时回到配置页 */
export function firstAllowedPath(hasMenu: (code: string) => boolean): string {
  return MENU_PATHS.find((m) => hasMenu(m.code))?.path ?? FALLBACK_PATH
}
