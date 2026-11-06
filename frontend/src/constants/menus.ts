// 菜单 code 与后端 permissions.MENU_REGISTRY 一致；顺序即顶部导航顺序，也是无权限时的回退顺序
export const MENU_PATHS: ReadonlyArray<{ code: string; path: string }> = [
  { code: 'home', path: '/home' },
  { code: 'trading', path: '/trading' },
  { code: 'follow', path: '/follow' },
  { code: 'ai_trade', path: '/ai-trade' },
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

/**
 * 个人中心不挂菜单码，所有登录用户都能进，是最终回退页。
 *
 * 用 /profile 而不是 /config —— 配置页是**系统级**配置（节点令牌 / Webhook /
 * 短信），普通用户看到一堆管理员才用得上的东西不合适；个人中心才是个人自己的地方。
 */
export const FALLBACK_PATH = '/profile'

/** 第一个有权限的菜单路径；一个菜单都没有时回到个人中心 */
export function firstAllowedPath(hasMenu: (code: string) => boolean): string {
  return MENU_PATHS.find((m) => hasMenu(m.code))?.path ?? FALLBACK_PATH
}
