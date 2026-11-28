const { test } = require('node:test')
const assert = require('node:assert/strict')
const { chromium } = require('playwright')

const baseURL = process.env.NODES_TEST_URL || 'http://127.0.0.1:5173'

for (const width of [390, 1440]) {
  test(`离线节点删除确认 · ${width}px`, async (t) => {
    const browser = await chromium.launch({ channel: process.env.PLAYWRIGHT_CHANNEL })
    t.after(() => browser.close())
    const page = await browser.newPage({ viewport: { width, height: 850 } })
    await page.addInitScript(() => localStorage.setItem('token', 'test-fixture'))
    let socket
    await page.routeWebSocket('**/ws/admin*', (ws) => { socket = ws })
    const node = { node_id: 'node1', name: '离线测试节点', mt5_login: 735001, owner_user_id: 1, enabled: true, status: 'offline' }
    const version = '1'.repeat(64)
    const submissions = []
    await page.route((url) => url.pathname.startsWith('/api/'), async (route) => {
      const request = route.request()
      const path = new URL(request.url()).pathname
      let json = {}
      if (path === '/api/me') json = { user_id: 1, username: 'test', menus: ['nodes'], capabilities: [] }
      if (path === '/api/nodes') json = [{ ...node, deletion_pending: !!submissions.length }]
      if (path === '/api/nodes/node1/deletion-preview') {
        json = { node_count: 1, member_count: 1, binding_count: 0, other_group_count: 0,
          cancelled_application_count: 0, version, nodes: [node],
          blocked: [{ ...node, reasons: ['节点离线', '缺少 60 秒内的可读账户快照'] }] }
      }
      if (path === '/api/nodes/node1' && request.method() === 'DELETE') {
        submissions.push(request.postDataJSON())
        return route.fulfill({ status: 202, json: { status: 'pending', node_ids: ['node1'] } })
      }
      await route.fulfill({ json })
    })

    await page.goto(baseURL + '/nodes')
    const remove = page.getByRole('button', { name: '删除', exact: true })
    await remove.click()
    const dialog = page.getByRole('dialog')
    await dialog.waitFor()
    assert.match(await dialog.innerText(), /节点离线/)
    assert.match(await dialog.innerText(), /平掉全部持仓并撤销全部挂单/)
    const bounds = await dialog.boundingBox()
    assert.ok(bounds && bounds.x >= 0 && bounds.x + bounds.width <= width + 1)
    await dialog.getByRole('button', { name: '取消', exact: true }).click()
    await dialog.waitFor({ state: 'hidden' })
    assert.equal(submissions.length, 0)

    await remove.click()
    await dialog.getByRole('button', { name: '确认全平并删除' }).click()
    const pending = page.getByRole('button', { name: '等待删除' })
    await pending.waitFor()
    assert.equal(await pending.isDisabled(), true)
    assert.deepEqual(submissions, [{ version }])

    socket.send(JSON.stringify({ type: 'node_deletion', data: { node_id: 'node1', status: 'deleted' } }))
    await page.getByText('离线测试节点', { exact: true }).waitFor({ state: 'hidden' })
  })
}
