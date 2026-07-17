<script setup lang="ts">
// 操作审计页：中控台 / 节点操作记录，可展开查看操作前后数据
import { computed, onMounted, ref } from 'vue'
import { useHubStore } from '@/stores/hub'
import type { AuditRecord } from '@/api/types'

const hub = useHubStore()

const items = ref<AuditRecord[]>([])
const page = ref(1)
const pageSize = ref(20)
const total = ref(0)
const loading = ref(false)
const category = ref<'' | 'console' | 'node'>('')
const expanded = ref<Record<number, boolean>>({})

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize.value)))

async function loadAudits(): Promise<void> {
  loading.value = true
  try {
    const res = await hub.fetchAudits(page.value, pageSize.value, category.value || null)
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
  return { cls: '', text: cat || '其它' }
}

function actionLabel(action: string): string {
  const m: Record<string, string> = {
    set_filters: '保存过滤规则',
    manual_signal: '手动触发信号',
    create_node: '创建节点',
    update_node: '更新节点',
    delete_node: '删除节点',
    batch_lot: '批量设置手数',
    close_node: '单节点平仓',
    close_all: '全局平仓',
    close_batch: '批量平仓',
  }
  return m[action] || action
}

function resultTag(result: string): { cls: string; text: string } {
  if (result === 'ok') return { cls: 'green', text: '成功' }
  if (result === 'offline') return { cls: 'amber', text: '离线' }
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
        记录中控台与节点相关操作，点击行可展开查看操作前后数据
      </p>
    </div>

    <div class="card card-pad audits-panel">
      <div class="row between" style="margin-bottom: 12px; flex-wrap: wrap; gap: 8px">
        <span class="muted" style="font-size: 12px">共 {{ total }} 条 · 点击行展开详情</span>
        <div class="row" style="gap: 8px; flex-wrap: wrap">
          <label class="row muted" style="font-size: 12px; gap: 6px">
            分类
            <select v-model="category" class="input-sm" @change="onCategoryChange">
              <option value="">全部</option>
              <option value="console">中控台</option>
              <option value="node">节点</option>
            </select>
          </label>
          <label class="row muted" style="font-size: 12px; gap: 6px">
            每页
            <select v-model.number="pageSize" class="input-sm" @change="onPageSizeChange">
              <option :value="10">10</option>
              <option :value="20">20</option>
              <option :value="50">50</option>
            </select>
          </label>
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
