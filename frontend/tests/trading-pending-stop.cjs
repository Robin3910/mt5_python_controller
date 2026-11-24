const { test } = require('node:test')
const assert = require('node:assert/strict')
const { chromium } = require('playwright')

const baseURL = process.env.TRADING_TEST_URL || 'http://127.0.0.1:5173'

for (const width of [390, 1440]) {
  test(`挂单分组操作 · ${width}px`, async (t) => {
    const browser = await chromium.launch({ channel: process.env.PLAYWRIGHT_CHANNEL })
    t.after(() => browser.close())
    const page = await browser.newPage({ viewport: { width, height: 900 } })
    await page.addInitScript(() => localStorage.setItem('token', 'test-fixture'))
    let socket
    await page.routeWebSocket('**/ws/admin*', ws => { socket = ws })
    const order = { symbol: 'XAUUSD', type: 'BUY_LIMIT', volume: 0.01, price_open: 3600 }
    const orders = [
      { ...order, ticket: 10, magic: 81, opening_method: 'grid' },
      { ...order, ticket: 11, magic: 81, opening_method: 'grid' },
      { ...order, ticket: 12, magic: 92, opening_method: 'trend_limit' },
      { ...order, ticket: 13, magic: 0 },
    ]
    const commands = []
    await page.route(url => url.pathname.startsWith('/api/'), async route => {
      const request = route.request()
      const path = new URL(request.url()).pathname
      let json = {}
      if (path === '/api/me') json = { user_id: 1, menus: ['trading', 'groups'], capabilities: [] }
      if (path === '/api/nodes') json = [{ node_id: 'primary', owner_user_id: 1, enabled: true, is_primary: true, status: 'online' }]
      if (path === '/api/nodes/primary/account') json = { updated_at: 1, positions: [], orders }
      if (path.endsWith('/deals')) json = { items: [], total: 0 }
      if (path === '/api/trading/pending-task') {
        const ticket = new URL(request.url()).searchParams.get('ticket')
        const magic = ticket === '10' ? 81 : 92
        const firstId = ticket === '10' ? 42 : 52
        json = { task_id: firstId, group_id: 'g', symbol: 'XAUUSD', status: 'running', dispatches: [
          { id: firstId, node_id: 'primary', symbol: 'XAUUSD', magic, status: 'running' },
          { id: firstId + 1, node_id: 'child', status: 'opened' },
          { id: firstId + 2, node_id: 'past', status: 'done' },
        ] }
      }
      if (request.method() === 'POST') { commands.push(path); json = { status: 'closing' } }
      await route.fulfill({ json })
    })
    await page.goto(baseURL + '/trading')
    const group = page.locator('.tr-pend-book .tr-magic-head').filter({ hasText: width === 390 ? '智能吸筹' : '限价开仓' })
    await group.waitFor()
    const stop = group.getByRole('button', { name: '停止', exact: true })
    assert.equal(await stop.count(), 1)
    assert.equal(await page.locator('.tr-pend-book .tr-magic-head').filter({ hasText: '手工单' }).getByRole('button').count(), 0)
    const refButton = page.getByRole('button', { name: '撤单', exact: true }).first()
    const right = async loc => loc.evaluate(el => el.getBoundingClientRect().right)
    assert.ok(Math.abs(await right(stop) - await right(refButton)) < 2)
    await stop.click()
    const dialog = page.getByRole('dialog')
    assert.match(await dialog.innerText(), /撤销该任务的挂单、平掉该任务的全部持仓/)
    assert.match(await dialog.innerText(), /primary、child/)
    await dialog.getByRole('button', { name: '取消' }).click()
    assert.equal(commands.length, 0)
    await stop.click()
    await dialog.getByRole('button', { name: '确认停止' }).click()
    await page.getByText(/已下发停止 XAUUSD/).waitFor()
    const ids = width === 390 ? [42, 43] : [52, 53]
    assert.deepEqual(commands.sort(), ids.map(id => `/api/groups/g/dispatches/${id}/close`))

    await stop.click()
    await dialog.waitFor()
    socket.send(JSON.stringify({ type: 'node_status', data: { node_id: 'primary', status: 'offline' } }))
    await dialog.getByRole('button', { name: '确认停止' }).click()
    await page.getByText('主节点或挂单已变化，请重新选择任务').waitFor()
    assert.equal(await stop.isDisabled(), true)
    assert.equal(commands.length, 2)
  })
}
