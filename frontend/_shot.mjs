// 临时脚本：用 Playwright 在 390x844 的移动视口下渲染 AI 交易页，
// 通过 route 拦截 mock 后端数据，检查手机端配色效果。验证完即删。
import { chromium, devices } from 'playwright'

const OUT = 'd:/work_space/mt5_python_controller/frontend/_shots'
const URL = 'http://localhost:5173/ai-trade'

const robots = [
  {
    leader_user_id: 101,
    username: 'quant_alpha',
    display_name: '量化阿尔法',
    strategy_name: '趋势突破 V3',
    strategy_names: ['趋势突破 V3', '波段轮动 V2'],
    marketable: true,
    follower_count: 328,
    follower_node_count: 328,
    my_follow_node_count: 0,
    primary_node: { node_id: 'n-1', name: '主节点 A', mt5_login: 520011 },
    performance: {
      data_available: true,
      total_profit: 18452.36,
      profit_rate: 42.18,
      profit_rate_valid: true,
      win_rate: 68.4,
      total_trades: 913,
      updated_at: 1761210000,
    },
  },
  {
    leader_user_id: 102,
    username: 'fx_master',
    display_name: '外汇老王',
    strategy_name: '黄金网格',
    strategy_names: ['黄金网格'],
    marketable: true,
    follower_count: 89,
    follower_node_count: 89,
    my_follow_node_count: 2,
    primary_node: { node_id: 'n-2', name: 'MT5-88001', mt5_login: 88001 },
    performance: {
      data_available: true,
      total_profit: -3210.88,
      profit_rate: -8.42,
      profit_rate_valid: true,
      win_rate: 41.2,
      total_trades: 204,
      updated_at: 1761210000,
    },
  },
  {
    leader_user_id: 103,
    username: 'scalp_king',
    display_name: '剥头皮王',
    strategy_name: '剥 scalp XAU',
    strategy_names: ['剥 scalp XAU'],
    marketable: false,
    follower_count: 12,
    follower_node_count: 12,
    my_follow_node_count: 0,
    primary_node: { node_id: 'n-3', name: '主节点 C', mt5_login: 33007 },
    performance: { data_available: false },
  },
]

const detail = {
  robot: robots[0],
  groups: [
    { group_id: 'g1', group_name: '趋势突破 V3 · 欧美盘', selectable: true, my_follow_node_ids: ['node-a'] },
    { group_id: 'g2', group_name: '波段轮动 V2 · 亚盘', selectable: true, my_follow_node_ids: [] },
  ],
  my_nodes: [
    { node_id: 'node-a', name: '主力节点 01', mt5_login: 520011 },
    { node_id: 'node-b', name: '副节点 02', mt5_login: 520012 },
    { node_id: 'node-c', name: '备用节点 03', mt5_login: 520013 },
  ],
}

const mine = {
  following: [
    {
      robot: robots[0],
      groups: [{ group_id: 'g1', group_name: '趋势突破 V3 · 欧美盘', batch_limit: 2, active_task_count: 1, my_nodes: [{ node_id: 'node-a', name: '主力节点 01', mt5_login: 520011, joined_at: 1759000000 }] }],
    },
  ],
  summary: {},
}

const browser = await chromium.launch()
const ctx = await browser.newContext({
  ...devices['iPhone 13'],
  locale: 'zh-CN',
  timezoneId: 'Asia/Shanghai',
})

// 路由守卫要求 localStorage 里有 token，否则会被踢回 /login
await ctx.addInitScript(() => {
  localStorage.setItem('token', 'mock-token')
})

await ctx.route('**/api/**', async (route) => {
  const url = route.request().url()
  const json = (body) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })

  if (url.includes('/api/me')) {
    return json({ username: 'trader', display_name: '交易员', is_admin: false, menus: ['ai_trade', 'home', 'trading', 'follow'], capabilities: [] })
  }
  if (url.includes('/api/ai-follow/robots/')) return json(detail)
  if (url.includes('/api/ai-follow/robots')) return json({ robots })
  if (url.includes('/api/ai-follow/me')) return json(mine)
  return json({})
})

const page = await ctx.newPage()
page.on('console', (m) => console.log('[console]', m.type(), m.text()))
page.on('pageerror', (e) => console.log('[pageerror]', e.message))

await page.goto(URL, { waitUntil: 'networkidle' })
await page.waitForTimeout(1500)

console.log('URL after goto:', page.url())
console.log('has .m-tab:', await page.locator('.m-tab').count())
console.log('has .m-robot:', await page.locator('.m-robot').count())
console.log('body classes:', await page.evaluate(() => document.body.className))
console.log('main html head:', (await page.evaluate(() => document.querySelector('main')?.innerHTML?.slice(0, 400))) ?? 'no main')

const name = process.argv[2] || 'list'
await page.screenshot({ path: `${OUT}/${name}-list.png`, fullPage: true })

// 切到「我的跟单」——用索引而不是中文文本匹配（PowerShell 下文本匹配会乱码）
const tabs = page.locator('.m-tab')
const tabCount = await tabs.count()
if (tabCount > 1) {
  await tabs.nth(1).click()
  await page.waitForTimeout(600)
  await page.screenshot({ path: `${OUT}/${name}-mine.png`, fullPage: true })
}

// 打开详情全屏页
await tabs.nth(0).click()
await page.waitForTimeout(400)
const card = page.locator('.m-robot').first()
if (await card.count()) {
  await card.click()
  await page.waitForTimeout(1200)
  await page.screenshot({ path: `${OUT}/${name}-detail.png`, fullPage: true })
}

await browser.close()
console.log('shots written to', OUT)
