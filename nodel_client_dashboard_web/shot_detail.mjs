/**
 * 视觉验收：节点详情面板不再展示运行日志、策略任务日志，节点信息里没有状态地址。
 *
 * 用法：
 *   node shot_detail.mjs <url>
 */
import { chromium } from 'playwright'

const url = process.argv[2]
if (!url) {
  console.error('用法: node shot_detail.mjs <url>')
  process.exit(2)
}

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
await page.goto(url, { waitUntil: 'networkidle' })

// 点第一个节点卡片，打开详情面板
await page.waitForSelector('.node-card', { timeout: 15000 })
await page.click('.node-card')
await page.waitForTimeout(800)

// 滚到详情区
await page.evaluate(() => {
  document.querySelector('.console-detail')?.scrollIntoView({ block: 'end' })
})
await page.waitForTimeout(400)

const dom = await page.evaluate(() => {
  const summaries = Array.from(document.querySelectorAll('.console-summary'))
  const detail = document.querySelector('.console-detail')
  const body = document.body.innerText || ''
  return {
    detailVisible: !!detail,
    summaryCount: summaries.length,
    summaryText: summaries.map((s) => s.innerText || '').join('\n'),
    logBlocks: document.querySelectorAll('.console-log').length,
    hasStatusAddr: body.includes('状态地址'),
    hasTaskWord: body.includes('策略任务'),
  }
})

await page.screenshot({ path: 'e2e_console_detail.png', fullPage: false })

const problems = []
if (!dom.detailVisible) problems.push('详情面板没出现')
if (dom.summaryCount !== 1) problems.push(`摘要块应只剩 1 个，实际 ${dom.summaryCount}`)
if (dom.logBlocks) problems.push(`仍有 ${dom.logBlocks} 个运行日志块`)
if (dom.hasStatusAddr) problems.push('仍有「状态地址」')
if (dom.hasTaskWord) problems.push('仍出现「策略任务」')

console.log(JSON.stringify({ dom, problems }, null, 2))
await browser.close()

if (problems.length) {
  console.log('[shot-detail] 结论：失败')
  process.exit(1)
}
console.log('[shot-detail] 结论：通过（无运行日志、无策略任务日志、无状态地址）')
