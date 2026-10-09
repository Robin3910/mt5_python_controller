<script setup lang="ts">
// 操作审计页：中控台 / 节点 / 账号权限 / 系统操作记录，可展开查看操作前后数据。
// 普通用户只看到本人的操作（后端按操作人过滤）
import { computed, onMounted, ref } from 'vue'
import TagSelect from '@/components/TagSelect.vue'
import type { TagOption } from '@/components/tagOption'
import { useAuthStore } from '@/stores/auth'
import { useHubStore } from '@/stores/hub'
import type { AuditRecord } from '@/api/types'

const hub = useHubStore()
const auth = useAuthStore()

const items = ref<AuditRecord[]>([])
const page = ref(1)
const pageSize = ref(20)
const total = ref(0)
const loading = ref(false)
type AuditCategory = 'all' | 'console' | 'node' | 'auth' | 'system'
const category = ref<AuditCategory>('all')
const CATEGORY_TAGS: TagOption<AuditCategory>[] = [
  { value: 'all', label: '全部' },
  { value: 'console', label: '中控台' },
  { value: 'node', label: '节点' },
  { value: 'auth', label: '账号权限' },
  { value: 'system', label: '系统' },
]
const PAGE_SIZE_TAGS: TagOption<number>[] = [
  { value: 10, label: '10' },
  { value: 20, label: '20' },
  { value: 50, label: '50' },
]
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

function onPageSizeChange(): void {
  page.value = 1
  loadAudits()
}

function onCategoryChange(): void {
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
  return sec ? new Date(sec * 1000).toLocaleString() : '—'
}

function fmtJson(data: unknown): string {
  if (data === null || data === undefined) return '—'
  try {
    return JSON.stringify(data, null, 2)
  } catch {
    return String(data)
  }
}

function categoryTag(cat: string | null): { cls: string; text: string } {
  if (cat === 'console') return { cls: 'blue', text: '中控台' }
  if (cat === 'node') return { cls: 'green', text: '节点' }
  if (cat === 'auth') return { cls: 'amber', text: '账号权限' }
  if (cat === 'system') return { cls: '', text: '系统' }
  return { cls: '', text: cat || '其它' }
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

function resultTag(result: string): { cls: string; text: string } {
  if (result === 'ok' || result === 'accepted') return { cls: 'green', text: '成功' }
  if (result === 'offline') return { cls: 'amber', text: '离线' }
  if (result === 'skipped') return { cls: '', text: '跳过' }
  if (result === 'pending') return { cls: 'amber', text: '待完成' }
  if (result === 'rejected') return { cls: 'amber', text: '拒绝' }
  if (result === 'cancel_failed') return { cls: 'red', text: '撤单失败' }
  if (result === 'fail' || result === 'failed') return { cls: 'red', text: '失败' }
  return { cls: '', text: result }
}

onMounted(loadAudits)
</script>

<template>
  <div class="audits-page">
    <div class="page-header">
      <div class="h1">操作审计</div>
      <p class="muted" style="font-size: 13px; margin-top: 4px">
        {{ auth.isAdmin ? '记录全部用户的操作' : '仅显示你本人的操作' }}，点击行可展开查看操作前后数据
      </p>
    </div>

    <div class="card card-pad audits-panel">
      <div class="row between" style="margin-bottom: 12px; flex-wrap: wrap; gap: 8px">
        <span class="muted" style="font-size: 12px">共 {{ total }} 条 · 点击行展开详情</span>
        <div class="row" style="gap: 8px; flex-wrap: wrap">
          <div class="row muted" style="font-size: 12px; gap: 6px; align-items: center">
            <span>分类</span>
            <TagSelect v-model="category" :options="CATEGORY_TAGS" aria-label="分类" @change="onCategoryChange" />
          </div>
          <div class="row muted" style="font-size: 12px; gap: 6px; align-items: center">
            <span>每页</span>
            <TagSelect v-model="pageSize" :options="PAGE_SIZE_TAGS" aria-label="每页条数" @change="onPageSizeChange" />
          </div>
          <button class="btn-sm btn-ghost" :disabled="loading" @click="loadAudits">
            {{ loading ? '刷新中…' : '刷新' }}
          </button>
        </div>
      </div>

      <template v-if="items.length">
        <div class="list-cards mobile-only">
          <div
            v-for="row in items"
            :key="row.id"
            class="list-card card clickable"
            @click="toggleRow(row.id)"
          >
            <div class="list-card-head row between">
              <strong>{{ actionLabel(row.action) }}</strong>
              <span class="muted">{{ isExpanded(row.id) ? '▾' : '▸' }}</span>
            </div>
            <div class="list-field">
              <span class="k">时间</span>
              <span class="v muted" style="font-size: 12px">{{ fmtTime(row.ts) }}</span>
            </div>
            <div class="list-field">
              <span class="k">分类</span>
              <span class="v">
                <span class="tag" :class="categoryTag(row.category).cls">{{ categoryTag(row.category).text }}</span>
              </span>
            </div>
            <div class="list-field">
              <span class="k">结果</span>
              <span class="v">
                <span class="tag" :class="resultTag(row.result).cls">{{ resultTag(row.result).text }}</span>
              </span>
            </div>
            <div class="list-field">
              <span class="k">操作人</span>
              <span class="v muted" style="font-size: 12px">{{ row.operator }}</span>
            </div>
            <div v-if="isExpanded(row.id)" class="list-card-detail">
              <div class="muted" style="font-size: 12px; margin-bottom: 6px">操作前</div>
              <pre class="token-box audits-json">{{ fmtJson(row.before) }}</pre>
              <div class="muted" style="font-size: 12px; margin: 12px 0 6px">操作后</div>
              <pre class="token-box audits-json">{{ fmtJson(row.after) }}</pre>
            </div>
          </div>
        </div>

        <div class="audits-table-wrap desktop-only">
          <table class="audits-table">
            <thead>
              <tr>
                <th class="col-expand"></th>
                <th class="col-time">时间</th>
                <th class="col-cat">分类</th>
                <th class="col-action">操作</th>
                <th class="col-target">目标</th>
                <th class="col-op">操作人</th>
                <th class="col-result">结果</th>
                <th class="col-ip">IP</th>
              </tr>
            </thead>
            <tbody>
              <template v-for="row in items" :key="row.id">
                <tr class="clickable" @click="toggleRow(row.id)">
                  <td class="muted col-expand">{{ isExpanded(row.id) ? '▾' : '▸' }}</td>
                  <td class="muted col-time">{{ fmtTime(row.ts) }}</td>
                  <td class="col-cat">
                    <span class="tag" :class="categoryTag(row.category).cls">{{ categoryTag(row.category).text }}</span>
                  </td>
                  <td class="col-action">{{ actionLabel(row.action) }}</td>
                  <td class="muted col-target audits-break">{{ row.target || '—' }}</td>
                  <td class="col-op">{{ row.operator }}</td>
                  <td class="col-result">
                    <span class="tag" :class="resultTag(row.result).cls">{{ resultTag(row.result).text }}</span>
                  </td>
                  <td class="muted col-ip">{{ row.ip || '—' }}</td>
                </tr>
                <tr v-if="isExpanded(row.id)" class="detail-row">
                  <td colspan="8">
                    <div class="audits-detail">
                      <div class="grid cols-2 audits-detail-kv" style="gap: 12px; margin-bottom: 12px">
                        <div class="kv"><span class="k">记录 ID</span><span class="v">{{ row.id }}</span></div>
                        <div class="kv"><span class="k">动作码</span><span class="v">{{ row.action }}</span></div>
                      </div>
                      <div class="audits-diff">
                        <div>
                          <div class="muted" style="font-size: 12px; margin-bottom: 6px">操作前数据</div>
                          <pre class="token-box audits-json">{{ fmtJson(row.before) }}</pre>
                        </div>
                        <div>
                          <div class="muted" style="font-size: 12px; margin-bottom: 6px">操作后数据</div>
                          <pre class="token-box audits-json">{{ fmtJson(row.after) }}</pre>
                        </div>
                      </div>
                      <div v-if="row.params" style="margin-top: 12px">
                        <div class="muted" style="font-size: 12px; margin-bottom: 6px">附加参数</div>
                        <pre class="token-box audits-json">{{ fmtJson(row.params) }}</pre>
                      </div>
                    </div>
                  </td>
                </tr>
              </template>
            </tbody>
          </table>
        </div>
      </template>

      <div v-else-if="!loading" class="muted" style="font-size: 13px; padding: 8px 0">暂无操作审计记录。</div>
      <div v-else class="muted" style="font-size: 13px; padding: 8px 0">加载中…</div>

      <div v-if="totalPages > 1" class="row between" style="margin-top: 16px">
        <span class="muted" style="font-size: 12px">第 {{ page }} / {{ totalPages }} 页</span>
        <div class="row" style="gap: 8px">
          <button class="btn-sm btn-ghost" :disabled="page <= 1 || loading" @click="goPage(page - 1)">上一页</button>
          <button class="btn-sm btn-ghost" :disabled="page >= totalPages || loading" @click="goPage(page + 1)">下一页</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.audits-page {
  width: 100%;
  min-width: 0;
}

.audits-panel {
  width: 100%;
  min-width: 0;
}

.audits-table-wrap {
  width: 100%;
  min-width: 0;
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
}

.audits-table {
  width: 100%;
  min-width: 0;
  table-layout: fixed;
}

.audits-table th,
.audits-table td {
  white-space: normal;
  word-break: break-word;
  overflow-wrap: anywhere;
  vertical-align: top;
  font-size: 12px;
}

.audits-table .col-expand { width: 28px; white-space: nowrap; }
.audits-table .col-time { width: 14%; white-space: nowrap; }
.audits-table .col-cat { width: 8%; white-space: nowrap; }
.audits-table .col-action { width: 16%; }
.audits-table .col-target { width: auto; }
.audits-table .col-op { width: 10%; }
.audits-table .col-result { width: 8%; white-space: nowrap; }
.audits-table .col-ip { width: 12%; }

.audits-detail {
  padding: 8px 0 4px;
  min-width: 0;
}

.audits-detail-kv {
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 220px), 1fr));
}

.audits-diff {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}

.audits-json {
  width: 100%;
  max-width: 100%;
  margin: 0;
  font-size: 12px;
  white-space: pre-wrap;
  overflow-x: auto;
  max-height: 360px;
}

.audits-break {
  word-break: break-word;
  overflow-wrap: anywhere;
}

@media (max-width: 900px) {
  .audits-diff {
    grid-template-columns: 1fr;
  }
  .audits-table {
    table-layout: auto;
  }
}
</style>
