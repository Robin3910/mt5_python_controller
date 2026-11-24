const { test } = require('node:test')
const assert = require('node:assert/strict')
const { chromium } = require('playwright')

// 使用 Playwright 与已启动的前端运行 node --test tests/ai-card-refresh.cjs，所有业务接口均使用模拟数据。
const baseURL = process.env.AI_TEST_URL || 'http://127.0.0.1:5173'

for (const mode of ['刷新按钮', '45 秒轮询']) {
  test(mode + '更新分组持仓', async (t) => {
    const browser = await chromium.launch({ channel: process.env.PLAYWRIGHT_CHANNEL })
    t.after(() => browser.close())
    const manual = mode === '刷新按钮'
    const page = await browser.newPage({ viewport: { width: manual ? 390 : 1280, height: 900 } })
    await page.addInitScript(() => localStorage.setItem('token', 'test-fixture'))
    let socket
    await page.routeWebSocket('**/ws/admin*', ws => { socket = ws })
    await page.clock.install()
    let positions = []
    let accountReads = 0
    const group = { group_id: 'g', my_nodes: [{ node_id: 'mine', is_primary: manual, follow_state: 'running' }] }
    if (manual) group.my_nodes.push({ node_id: 'child', is_primary: false, follow_state: 'running' })
    const robot = { group_id: 'g', leader_user_id: manual ? 1 : 2, marketable: true, strategy_name: '测试策略', primary_node: { node_id: manual ? 'mine' : 'leader' } }
    let accountUnavailable = false
    let beforeAccountReply
    await page.route(url => url.pathname.startsWith('/api/'), async (route) => {
      const path = new URL(route.request().url()).pathname
      let json = {}
      if (path === '/api/me') json = { user_id: 1, menus: ['ai_trade'], capabilities: [] }
      if (path === '/api/nodes') json = []
      if (path === '/api/ai-follow/robots') json = { robots: [] }
      if (path === '/api/ai-follow/nodes') json = { nodes: [], summary: {} }
      if (path === '/api/ai-follow/me') json = { following: [{ is_owner: manual, robot, groups: [group] }], applications: [], summary: {} }
      if (path === '/api/nodes/mine/account') {
        if (accountUnavailable) return route.fulfill({ status: 404, json: {} })
        json = { positions, updated_at: ++accountReads }
        await beforeAccountReply?.()
      }
      if (path === '/api/nodes/child/account') {
        json = { positions: positions.length ? [{ ticket: 1, magic: 11, volume: 0.15, profit: -2 }] : [], updated_at: accountReads }
      }
      if (path.endsWith('/follow-channels')) {
        json = { magics: [{ group_id: 'g', market: 'ai', magic: 11 }, { group_id: 'other', market: 'ai', magic: 22 }] }
      }
      await route.fulfill({ json })
    })
    const mapping = () => page.waitForResponse(resp => resp.url().endsWith('/mine/follow-channels'))
    await Promise.all([mapping(), page.goto(baseURL + '/ai-trade')])
    const stats = page.locator('.ai-card .ai-stats')
    const values = stats.locator('strong')
    const refresh = () => manual ? page.getByRole('button', { name: '刷新', exact: true }).click() : page.clock.fastForward(45_000)
    await values.first().filter({ hasText: '+0.00 U' }).waitFor()
    positions = [{ ticket: 1, magic: 11, volume: 0.1, profit: 24.5 }, { ticket: 2, magic: 22, volume: 1, profit: 100 }]
    await Promise.all([mapping(), refresh()])
    const expected = manual ? ['+22.50 U', '2 笔', '0.25 手'] : ['+24.50 U', '1 笔', '0.10 手']
    await values.first().filter({ hasText: expected[0] }).waitFor({ timeout: 4000 })
    assert.deepEqual((await values.allTextContents()).slice(0, 3), expected)
    positions = []
    await Promise.all([mapping(), refresh()])
    await values.first().filter({ hasText: '+0.00 U' }).waitFor({ timeout: 4000 })
    assert.deepEqual((await values.allTextContents()).slice(0, 3), ['+0.00 U', '0 笔', '0.00 手'])
    assert.equal(accountReads, 3)
    accountUnavailable = true
    await Promise.all([mapping(), refresh()])
    await values.first().filter({ hasText: '—' }).waitFor()
    assert.deepEqual((await values.allTextContents()).slice(0, 3), ['—', '—', '—'])
    accountUnavailable = false
    await Promise.all([mapping(), refresh()])
    await values.first().filter({ hasText: '+0.00 U' }).waitFor()

    if (manual) {
      beforeAccountReply = async () => {
        socket.send(JSON.stringify({ type: 'account', data: {
          node_id: 'mine', updated_at: accountReads + 1,
          positions: [{ ticket: 3, magic: 11, volume: 0.2, profit: -6 }],
        } }))
        await values.first().filter({ hasText: '-6.00 U' }).waitFor()
      }
      await Promise.all([page.waitForResponse(resp => resp.url().endsWith('/mine/account')), refresh()])
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))))
      assert.deepEqual((await values.allTextContents()).slice(0, 3), ['-6.00 U', '1 笔', '0.20 手'])
    }
  })
}
