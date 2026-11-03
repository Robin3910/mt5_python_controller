// 构建产物自检：防止「编译通过但线上白屏」。
//
// 背景（2026-11-03 事故）：nginx 镜像里 dist/assets/installCanvasRenderer-*.js
// 的第 18218 字节被写坏（')' -> '9'），线上直接抛
//   SyntaxError: Unexpected identifier 'n'
// 页面全白，但 npm run build 阶段完全无感知。此脚本在打包后、镜像落盘前拦截：
//   1. 每个 chunk 都能被解析（等价 node --check，但在一个进程内批量跑）
//   2. index.html / 各 chunk 引用到的静态资源都真实存在
//   3. 没有空文件、残留 .map
//
// 用法：node verify-build.mjs [distDir]
import { readdirSync, readFileSync, statSync, existsSync, mkdtempSync, writeFileSync, rmSync } from 'node:fs'
import { join, resolve, basename } from 'node:path'
import { tmpdir } from 'node:os'
import { execFileSync } from 'node:child_process'

const distDir = resolve(process.argv[2] || 'dist')
const assetsDir = join(distDir, 'assets')

const errors = []
const warnings = []

if (!existsSync(assetsDir)) {
  console.error(`[verify-build] 找不到产物目录: ${assetsDir}`)
  process.exit(1)
}

const files = readdirSync(assetsDir)
const jsFiles = files.filter((f) => f.endsWith('.js'))
const cssFiles = files.filter((f) => f.endsWith('.css'))

// ---------- 1. 每个 chunk 语法可解析 ----------
// 用 .mjs 副本走 node --check：vite 产物是 ESM，直接对 .js 检查会误报
// "Cannot use import statement outside a module"（取决于包的 type 字段）。
const tmp = mkdtempSync(join(tmpdir(), 'verify-build-'))
let checked = 0
try {
  for (const name of jsFiles) {
    const copy = join(tmp, basename(name).replace(/\.js$/, '') + '.mjs')
    writeFileSync(copy, readFileSync(join(assetsDir, name)))
    try {
      execFileSync(process.execPath, ['--check', copy], { stdio: 'pipe' })
      checked++
    } catch (err) {
      const msg = (err.stderr ? err.stderr.toString() : String(err))
        .split('\n')
        .filter((l) => l.trim() && !l.startsWith('    at ') && !l.startsWith('Node.js v'))
        .slice(0, 4)
        .join(' | ')
      errors.push(`[语法错误] ${name}: ${msg}`)
    }
  }
} finally {
  rmSync(tmp, { recursive: true, force: true })
}

// ---------- 2. 引用到的资源都存在 ----------
// 每条引用记为 (引用串, 相对于哪个目录解析)
const referenced = []

const htmlPath = join(distDir, 'index.html')
if (!existsSync(htmlPath)) {
  errors.push('缺少 dist/index.html')
} else {
  const html = readFileSync(htmlPath, 'utf8')
  for (const m of html.matchAll(/(?:src|href)="\/([^"]+)"/g)) {
    referenced.push([m[1], distDir])
  }
}

// chunk 内的静态资源引用，两种形态：
//   import ... from"./index-D4jBoOdx.js"        -> 相对 assets 目录
//   ["assets/TrendPanel-DeFAJRm1.js", ...]      -> 相对 dist 目录
for (const name of jsFiles) {
  const code = readFileSync(join(assetsDir, name), 'utf8')
  for (const m of code.matchAll(/["']((?:\.\/)?assets\/[^"']+\.(?:js|css))["']/g)) {
    referenced.push([m[1].replace(/^\.\//, ''), distDir])
  }
  for (const m of code.matchAll(/["'](\.\/[^"']+\.(?:js|css))["']/g)) {
    referenced.push([m[1].replace(/^\.\//, ''), assetsDir])
  }
}

const seenRefs = new Set()
for (const [ref, base] of referenced) {
  const key = `${base}::${ref}`
  if (seenRefs.has(key)) continue
  seenRefs.add(key)
  const target = join(base, ref.replace(/^\//, ''))
  if (!existsSync(target)) {
    errors.push(`[引用缺失] ${ref} 被引用但不存在`)
  } else if (statSync(target).size === 0) {
    errors.push(`[空文件] ${ref}`)
  }
}

// ---------- 3. 卫生检查 ----------
for (const name of files) {
  if (name.endsWith('.map')) {
    warnings.push(`残留 sourcemap: ${name}`)
  }
  if (statSync(join(assetsDir, name)).size === 0) {
    errors.push(`[空文件] assets/${name}`)
  }
}

// ---------- 输出 ----------
for (const w of warnings) console.warn(`[verify-build][warn] ${w}`)

if (errors.length) {
  for (const e of errors) console.error(`[verify-build] ${e}`)
  console.error(
    `[verify-build] 产物校验失败（${errors.length} 项）。` +
      `常见原因：Docker layer 写入时字节被写坏，或磁盘/网络问题。` +
      `请用 --no-cache 重建镜像。`,
  )
  process.exit(1)
}

console.log(
  `[verify-build] OK: ${checked}/${jsFiles.length} 个 chunk 语法通过，` +
    `${cssFiles.length} 个 css，${seenRefs.size} 条引用全部命中。`,
)
