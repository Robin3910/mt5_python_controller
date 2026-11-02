<script setup lang="ts">
// 操作审计页：中控台 / 节点 / 账号权限 / 系统操作记录，可展开查看操作前后数据。
// 版式 1:1 复刻 UI/H5.html 的「操作审计」tab；普通用户只看到本人的操作（后端按操作人过滤）。
import { computed, onMounted, ref } from 'vue'
import { useAuthStore } from '@/stores/auth'
import { useHubStore } from '@/stores/hub'
import type { AuditRecord } from '@/api/types'

const hub = useHubStore()
const auth = useAuthStore()

const items = ref<AuditRecord[]>([])
const page = ref(1)
const pageSize = ref(10)
const total = ref(0)
const loading = ref(false)

type AuditCategory = 'all' | 'console' | 'node' | 'auth' | 'system'
const category = ref<AuditCategory>('all')
const CATEGORY_OPTIONS: { key: AuditCategory; label: string }[] = [
  { key: 'all', label: '全部' },
  { key: 'console', label: '中控台' },
  { key: 'node', label: '节点' },
  { key: 'auth', label: '账号权限' },
  { key: 'system', label: '系统' },
]
const PAGE_SIZES = [10, 20, 50]

const expanded = ref<Record<number, boolean>>({})

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize.value)))

async function loadAudits(): Promise<void> {
  loading.value = true
  try {
    const res = await hub.fetchAudits(page.value, pageSize.value, category.value)
    items.value = res.items
    total.value = res.total
    if (res.page !== page.value) page.value = res.page
  } finally {
    loading.value = false
  }
}

function goPage(next: number): void {
  const p = Math.min(Math.max(1, next), totalPages.value)
  if (p === page.value) return
  page.value = p
  loadAudits()
}

function onPageSizeChange(next: number): void {
  pageSize.value = next
  page.value = 1
  loadAudits()
}

function onCategoryChange(next: AuditCategory): void {
  if (category.value === next) return
  category.value = next
  page.value = 1
  loadAudits()
}

function toggleRow(id: number): void {
  expanded.value[id] = !expanded.value[id]
}

function isExpanded(id: number): boolean {
  return !!expanded.value[id]
}

function fmtTime(sec: number | null | undefined): string {
  if (!sec) return '—'
  const d = new Date(sec * 1000)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

function fmtJson(data: unknown): string {
  if (data === null || data === undefined) return '—'
  try {
    return JSON.stringify(data, null, 2)
  } catch {
    return String(data)
  }
}

/** 分类药丸：原型里中控台蓝、节点紫 */
function categoryTag(cat: string | null): { cls: string; text: string } {
  if (cat === 'console') return { cls: 'blue', text: '中控台' }
  if (cat === 'node') return { cls: '', text: '节点' }
  if (cat === 'auth') return { cls: 'amber', text: '账号权限' }
  if (cat === 'system') return { cls: 'grey', text: '系统' }
  return { cls: 'grey', text: cat || '其它' }
}

/** 结果标记：原型只有「成功 / 跳过」两种，其余沿用既有语义 */
function resultTag(result: string): { cls: string; text: string } {
  if (result === 'ok' || result === 'accepted' || result === 'success') return { cls: 'ok', text: '成功' }
  if (result === 'offline') return { cls: 'warn', text: '离线' }
  if (result === 'skipped') return { cls: 'skip', text: '跳过' }
  if (result === 'pending') return { cls: 'warn', text: '待完成' }
  if (result === 'rejected') return { cls: 'warn', text: '拒绝' }
  if (result === 'cancel_failed') return { cls: 'fail', text: '撤单失败' }
  if (result === 'fail' || result === 'failed') return { cls: 'fail', text: '失败' }
  return { cls: 'skip', text: result }
}

function actionLabel(action: string): string {
  const m: Record<string, string> = {
    set_filters: '保存过滤规则',
    manual_signal: '手动触发信号',
    limit_watch_signal: '限价监听触发',
    create_node: '创建节点',
    update_node: '更新节点',
    delete_node: '删除节点',
    batch_lot: '批量设置手数',
    close_node: '单节点平仓',
    close_all: '全局平仓',
    close_batch: '批量平仓',
    close_group_dispatch: '策略子任务平仓',
    close_group: '分组一键平仓',
    login: '登录',
    login_failed: '登录失败',
    login_2fa_failed: '2FA 验证失败',
    change_password: '修改密码',
    create_user: '新建用户',
    update_user: '更新用户',
    set_user_roles: '分配角色',
    reset_user_password: '重置用户密码',
    reset_user_2fa: '重置用户 2FA',
    assign_nodes: '分配节点',
    delete_user: '删除用户',
    create_role: '新建角色',
    update_role: '更新角色',
    set_role_menus: '分配菜单',
    delete_role: '删除角色',
    rotate_node_token: '重置节点令牌',
    dashboard_enrollment: '面板申请节点',
    dashboard_add_instance: '面板添加实例',
    dashboard_import_node: '面板导入节点',
    dashboard_bind_instance: '面板绑定实例',
    dashboard_edit_instance: '面板编辑实例',
    dashboard_edit_env: '面板保存节点配置',
    dashboard_edit_config: '面板保存配置',
    dashboard_connection_config: '面板连接配置',
    dashboard_save_connection: '面板保存连接',
    dashboard_start: '面板启动节点',
    dashboard_stop: '面板停止节点',
    dashboard_restart: '面板重启节点',
    dashboard_remove_instance: '面板移除实例',
    dashboard_set_daemon: '面板切换守护',
    dashboard_daemon_on: '面板开启守护',
    dashboard_daemon_off: '面板关闭守护',
    dashboard_daemon_grant: '面板取得守护授权',
    dashboard_daemon_restart: '守护自动重启',
    dashboard_refresh_health: '面板刷新健康',
    dashboard_view_status: '面板查看状态',
    dashboard_view_log: '面板查看日志',
    dashboard_clear_log: '面板清空日志显示',
    dashboard_open_cwd: '面板打开工作目录',
    dashboard_version_check: '面板检查版本',
    dashboard_download_package: '面板下载安装包',
    dashboard_update: '面板更新客户端',
    dashboard_update_client: '面板更新客户端',
    dashboard_rollback: '面板回滚客户端',
    dashboard_rollback_client: '面板回滚客户端',
    dashboard_replace: '面板替换客户端',
    dashboard_replace_client: '面板替换客户端',
    dashboard_credential_issue: '签发节点专属令牌',
    dashboard_credential_verify: '验证节点专属令牌',
    dashboard_credential_rotate: '重签节点专属令牌',
    dashboard_logout: '面板退出登录',
  }
  return m[action] || action
}

onMounted(loadAudits)
</script>

<template>
  <div class="pf-page au-page">
    <section class="pf-card">
      <!-- 卡片头 -->
      <div class="pf-card-head">
        <div class="pf-card-head-main">
          <span class="pf-card-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" />
              <path d="m9 11 3 3L22 4" />
            </svg>
          </span>
          <div>
            <p class="pf-card-title">操作审计</p>
            <p class="pf-card-sub">
              共 <b>{{ total }}</b> 条记录 · {{ auth.isAdmin ? '记录全部用户的操作' : '仅显示你本人的操作' }}
            </p>
          </div>
        </div>
        <button type="button" class="pf-btn au-refresh" :disabled="loading" @click="loadAudits">
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            stroke-linecap="round"
            stroke-linejoin="round"
            :class="{ spin: loading }"
          >
            <path d="M21 2v6h-6" />
            <path d="M3 12a9 9 0 0 1 15-6.7L21 8" />
            <path d="M3 22v-6h6" />
            <path d="M21 12a9 9 0 0 1-15 6.7L3 16" />
          </svg>
          {{ loading ? '刷新中…' : '刷新' }}
        </button>
      </div>

      <!-- 分类筛选 -->
      <div class="pf-seg au-filter">
        <button
          v-for="opt in CATEGORY_OPTIONS"
          :key="opt.key"
          type="button"
          class="pf-seg-item"
          :class="{ active: category === opt.key }"
          @click="onCategoryChange(opt.key)"
        >
          {{ opt.label }}
        </button>
      </div>

      <!-- 记录表 -->
      <div class="pf-rows au-rows">
        <div class="pf-rows-inner">
          <div class="pf-row au-thead">
            <span>操作时间</span>
            <span>分类</span>
            <span>操作</span>
            <span>目标</span>
            <span>操作人</span>
            <span>结果</span>
            <span>IP</span>
          </div>

          <template v-for="row in items" :key="row.id">
            <div class="pf-row au-row" @click="toggleRow(row.id)">
              <span class="c-muted">{{ fmtTime(row.ts) }}</span>
              <span>
                <span class="pf-pill" :class="categoryTag(row.category).cls">
                  {{ categoryTag(row.category).text }}
                </span>
              </span>
              <span class="c-mono" :title="row.action">{{ actionLabel(row.action) }}</span>
              <span class="c-muted" :title="row.target || ''">{{ row.target || '—' }}</span>
              <span class="c-muted">{{ row.operator }}</span>
              <span>
                <span class="pf-result" :class="resultTag(row.result).cls">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                    <path v-if="resultTag(row.result).cls === 'ok'" d="M20 6 9 17l-5-5" />
                    <path v-else-if="resultTag(row.result).cls === 'fail'" d="M18 6 6 18M6 6l12 12" />
                    <circle v-else cx="12" cy="12" r="9" />
                  </svg>
                  {{ resultTag(row.result).text }}
                </span>
              </span>
              <span class="c-dim au-ip">{{ row.ip || '—' }}</span>
            </div>

            <div v-if="isExpanded(row.id)" class="au-detail">
              <div class="au-detail-kv">
                <div class="pf-metric">
                  <p class="pf-metric-k">记录 ID</p>
                  <p class="pf-metric-v">{{ row.id }}</p>
                </div>
                <div class="pf-metric">
                  <p class="pf-metric-k">动作码</p>
                  <p class="pf-metric-v">{{ row.action }}</p>
                </div>
              </div>
              <div class="au-diff">
                <div>
                  <p class="pf-label">操作前数据</p>
                  <pre class="token-box au-json">{{ fmtJson(row.before) }}</pre>
                </div>
                <div>
                  <p class="pf-label">操作后数据</p>
                  <pre class="token-box au-json">{{ fmtJson(row.after) }}</pre>
                </div>
              </div>
              <div v-if="row.params" class="au-params">
                <p class="pf-label">附加参数</p>
                <pre class="token-box au-json">{{ fmtJson(row.params) }}</pre>
              </div>
            </div>
          </template>

          <p v-if="!items.length && !loading" class="pf-empty">当前筛选条件下暂无审计记录</p>
          <p v-else-if="loading" class="pf-empty">加载中…</p>
        </div>
      </div>

      <!-- 分页 -->
      <div class="au-foot">
        <div class="au-pagesize">
          <span class="au-foot-label">每页</span>
          <button
            v-for="n in PAGE_SIZES"
            :key="n"
            type="button"
            class="pf-seg-item mini"
            :class="{ active: pageSize === n }"
            @click="onPageSizeChange(n)"
          >
            {{ n }}
          </button>
          <span class="au-foot-label">条</span>
        </div>
        <div class="au-pager">
          <button
            type="button"
            class="pf-page-btn"
            :disabled="page <= 1 || loading"
            aria-label="上一页"
            @click="goPage(page - 1)"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="m15 18-6-6 6-6" />
            </svg>
          </button>
          <span class="au-page-num">第 {{ page }} / {{ totalPages }} 页</span>
          <button
            type="button"
            class="pf-page-btn"
            :disabled="page >= totalPages || loading"
            aria-label="下一页"
            @click="goPage(page + 1)"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="m9 18 6-6-6-6" />
            </svg>
          </button>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
/* 版式 1:1 复刻 UI/H5.html 的「操作审计」tab；
   卡片 / 按钮 / 药丸等共用外观见 styles.css 的 pf- 前缀类。 */
.au-page {
  max-width: 1080px;
}

.au-refresh svg.spin {
  animation: spin-mini-rotate 0.8s linear infinite;
}

.au-filter {
  margin-top: 12px;
}

.au-thead,
.au-row {
  grid-template-columns: 120px 76px minmax(0, 1fr) minmax(0, 1fr) 76px 64px 110px;
}

.au-row {
  cursor: pointer;
}

.au-ip {
  font-family: var(--mono);
  font-size: 11px;
}

/* ── 展开详情 ── */
.au-detail {
  padding: 12px 8px;
  border-bottom: 1px solid #161f30;
  background: rgba(6, 10, 18, 0.35);
}

.au-detail-kv {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 220px), 1fr));
  gap: 8px;
  margin-bottom: 12px;
}

.au-diff {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}

.au-params {
  margin-top: 12px;
}

.au-json {
  width: 100%;
  max-width: 100%;
  margin: 0;
  font-size: 12px;
  white-space: pre-wrap;
  overflow-x: auto;
  max-height: 360px;
}

/* ── 分页条 ── */
.au-foot {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-top: 12px;
}

.au-pagesize,
.au-pager {
  display: flex;
  align-items: center;
  gap: 6px;
}

.au-foot-label {
  font-size: 10px;
  color: var(--pf-dim);
}

.au-page-num {
  font-family: var(--mono);
  font-size: 10px;
  color: var(--pf-muted);
}

@media (max-width: 900px) {
  .au-diff {
    grid-template-columns: 1fr;
  }
}
</style>
