const { test } = require('node:test')
const assert = require('node:assert/strict')
const { chromium } = require('playwright')

const baseURL = process.env.TRADING_TEST_URL || 'http://127.0.0.1:5173'

for (const width of [390, 1440]) {
  test(`历史交易排除 AI 智能加仓 · ${width}px`, async (t) => {
    const browser = await chromium.launch({ channel: process.env.PLAYWRIGHT_CHANNEL })
    t.after(() => browser.close())
    const page = await browser.newPage({ viewport: { width, height: 900 } })
    await page.addInitScript(() => localStorage.setItem('token', 'test-fixture'))
    await page.routeWebSocket('**/ws/admin*', () => {})
    const deal = { entry_type: 'OUT', symbol: 'XAUUSD', position_direction: 'BUY', volume: 0.01, price: 3646, deal_time: 1750000000 }
    const normal = [
      { ...deal, deal_id: 1, position_id: 10, magic: 81, opening_method: 'grid', net_profit: 7 },
      { ...deal, deal_id: 2, position_id: 20, magic: 0, net_profit: -2 },
    ]
    const ai = { ...deal, deal_id: 3, position_id: 30, magic: 91, opening_method: 'ai', net_profit: 50 }
    const filters = []
    await page.route(url => url.pathname.startsWith('/api/'), async route => {
      const url = new URL(route.request().url())
      let json = {}
      if (url.pathname === '/api/me') json = { user_id: 1, menus: ['trading'], capabilities: [] }
      if (url.pathname === '/api/nodes') json = [{ node_id: 'primary', owner_user_id: 1, enabled: true, is_primary: true, status: 'online' }]
      if (url.pathname === '/api/nodes/primary/account') json = { updated_at: 1, positions: [], orders: [] }
      if (url.pathname === '/api/deals/nodes/primary/deals') {
        const filter = url.searchParams.get('exclude_ai')
        filters.push(filter)
        json = filter === 'true'
          ? { items: normal, total: 2, summary: { data_available: true, win_rate: 50, total_profit: 5 } }
          : { items: [...normal, ai], total: 3, summary: { data_available: true, win_rate: 67, total_profit: 55 } }
      }
      await route.fulfill({ json })
    })

    await page.goto(baseURL + '/trading')
    await page.getByRole('button', { name: /^历史交易/ }).click()
    await page.getByText('近 7 日 2 笔', { exact: true }).waitFor()
    const stats = await page.locator('.tr-hist-pills').innerText()
    assert.match(stats, /胜率 50%/)
    assert.match(stats, /累计盈亏 \+5\.00/)
    assert.deepEqual(await page.locator('.tr-magic-head b').allTextContents(), ['智能吸筹', '手工单'])
    assert.equal(await page.locator('.tr-hist-grid.tr-line').count(), 2)
    assert.ok(filters.length > 0 && filters.every(filter => filter === 'true'))
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth) <= width)
  })
}
