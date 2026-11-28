const { test } = require('node:test')
const assert = require('node:assert/strict')
const { chromium } = require('playwright')

const baseURL = process.env.TRADING_TEST_URL || 'http://127.0.0.1:5173'

for (const width of [320, 390, 1440]) {
  test(`当前持仓列对齐 · ${width}px`, async (t) => {
    const browser = await chromium.launch({ channel: process.env.PLAYWRIGHT_CHANNEL })
    t.after(() => browser.close())
    const page = await browser.newPage({ viewport: { width, height: 900 }, locale: 'en-US' })
    await page.addInitScript(() => localStorage.setItem('token', 'test-fixture'))
    await page.routeWebSocket('**/ws/admin*', () => {})
    const position = { symbol: 'BTCUSD', type: 'BUY', magic: 81, opening_method: 'trend_market', volume: 0.16, profit: -2.22 }
    const positions = [
      { ...position, ticket: 1, price_open: 59897.75, tp: 0, sl: 59680.36 },
      { ...position, ticket: 2, price_open: 109898, tp: 109974.67, sl: 109680.36, profit: -1234.56 },
    ]
    await page.route(url => url.pathname.startsWith('/api/'), async route => {
      const path = new URL(route.request().url()).pathname
      let json = {}
      if (path === '/api/me') json = { user_id: 1, menus: ['trading'], capabilities: [] }
      if (path === '/api/nodes') json = [{ node_id: 'primary', owner_user_id: 1, enabled: true, is_primary: true, status: 'online' }]
      if (path === '/api/nodes/primary/account') json = { updated_at: 1, positions, orders: [] }
      if (path.endsWith('/deals')) json = { items: [], total: 0 }
      await route.fulfill({ json })
    })

    await page.goto(baseURL + '/trading')
    const table = page.locator('.tr-scroll').filter({ has: page.locator('.tr-pos-grid.tr-head') })
    await table.getByText('59,897.75', { exact: true }).waitFor()
    const layout = await table.evaluate(scroll => {
      const head = scroll.querySelector('.tr-pos-grid.tr-head')
      const rows = [...scroll.querySelectorAll('.tr-pos-grid.tr-line')]
      const textRange = cell => {
        const range = document.createRange()
        range.selectNodeContents(cell)
        return range
      }
      const textRect = cell => textRange(cell).getBoundingClientRect()
      const center = rect => (rect.left + rect.right) / 2
      const direction = center(textRect(head.children[1]))
      const tableWidth = head.getBoundingClientRect().width
      const problems = []
      if (getComputedStyle(head).gridTemplateColumns.split(' ').length !== head.children.length) problems.push('表头多出网格列')
      for (const row of rows) {
        const cells = row.children
        if (Math.abs(direction - center(cells[1].querySelector('.dir').getBoundingClientRect())) > 1) problems.push('方向列错位')
        const open = textRect(cells[3])
        const prices = [...cells[4].children].map(textRect)
        const pnl = textRect(cells[5])
        if ([cells[3], ...cells[4].children, cells[5]].some(cell => textRange(cell).getClientRects().length > 1)) problems.push('金额数字被拆行')
        if (open.right + 4 > Math.min(...prices.map(rect => rect.left))) problems.push('开仓价挤到止盈止损')
        if (Math.max(...prices.map(rect => rect.right)) + 4 > pnl.left) problems.push('止盈止损挤到浮动盈亏')
      }
      const section = scroll.querySelector('.tr-magic-head').getBoundingClientRect()
      if (Math.abs(section.width - tableWidth) > 1) problems.push('分组和表头宽度不同')
      scroll.scrollLeft = scroll.scrollWidth
      const button = rows.at(-1).querySelector('button').getBoundingClientRect()
      const viewport = scroll.getBoundingClientRect()
      if (button.left < viewport.left - 1 || button.right > viewport.right + 1) problems.push('平仓按钮被遮挡')
      return { problems, clientWidth: scroll.clientWidth, scrollWidth: scroll.scrollWidth }
    })
    assert.deepEqual(layout.problems, [], JSON.stringify(layout))
    if (width === 320) assert.ok(layout.scrollWidth > layout.clientWidth)
    else assert.equal(layout.scrollWidth, layout.clientWidth)
  })
}
