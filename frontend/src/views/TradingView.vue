<script setup lang="ts">
// 交易页：针对主节点触发三种开仓策略（市价/限价/网格）
// 首次触发自动按 (策略类型-节点名-MT5 登录号) 创建专属分组+策略；后续直接复用
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import 'element-plus/es/components/message/style/css'
import 'element-plus/es/components/dialog/style/css'
import { useRouter } from 'vue-router'
import { useHubStore } from '@/stores/hub'
import { quickTrigger, type TradingStrategyType, type QuickTriggerResult } from '@/api/trading'
import type { AccountSnapshot, NodeOut } from '@/api/types'

const hub = useHubStore()
const router = useRouter()

const STRATEGY_LABEL: Record<TradingStrategyType, string> = {
  trend_market: '市价开仓',
  trend_limit: '限价开仓',
  grid: '智能吸筹',
}

const strategyType = ref<TradingStrategyType>('trend_market')

// 当前主节点：取 owner == 当前用户（或管理员名下）的 is_primary 节点
const primaryNode = computed<NodeOut | undefined>(() => {
  return hub.nodes.find((n) => n.is_primary && n.enabled) as NodeOut | undefined
})

// 节点账户快照（用于取现价 / 持仓）
const account = computed<AccountSnapshot | null>(() => {
  if (!primaryNode.value) return null
  return hub.accounts[primaryNode.value.node_id] || null
})

// 在线状态：与项目其它视图保持一致 —— hub.statuses[xx] === 'online' || n.status === 'online'
function nodeOnline(n: NodeOut): boolean {
  return hub.statuses[n.node_id] === 'online' || n.status === 'online'
}
const isOnline = computed(() => {
  if (!primaryNode.value) return false
  return nodeOnline(primaryNode.value)
})

// 当前品种的持仓数
const positionsCount = computed(() => {
  if (!account.value || !form.value.symbol) return 0
  const positions = account.value.positions || []
  return positions.filter((p) => p.symbol === form.value.symbol).length
})

// 当前品种的中间价（市价模式自动判断方向用）
const currentMid = computed(() => {
  if (!account.value || !form.value.symbol) return 0
  const q = account.value.quotes?.[form.value.symbol]
  if (q && typeof q.mid === 'number' && q.mid > 0) return q.mid
  return 0
})

const form = ref({
  symbol: 'XAUUSD',
  volume: 0.01,
  sl: 0 as number | null,
  tp: 0 as number | null,
  entry_price: 0 as number | null,
  direction: 'long' as 'long' | 'short' | 'all',
  remark: '',
})

watch(strategyType, () => {
  // 切策略时清空入场价
  form.value.entry_price = 0
})

const canSubmit = computed(() => {
  if (!primaryNode.value || !isOnline.value) return false
  if (!form.value.symbol || form.value.volume <= 0) return false
  if (strategyType.value === 'trend_limit' && (!form.value.entry_price || form.value.entry_price <= 0)) return false
  return true
})

const dialogVisible = ref(false)
const submitting = ref(false)
const lastError = ref('')

function openDialog() {
  if (!canSubmit.value) return
  lastError.value = ''
  dialogVisible.value = true
}

async function submit() {
  if (!primaryNode.value) return
  submitting.value = true
  lastError.value = ''
  try {
    const payload = {
      node_id: primaryNode.value.node_id,
      strategy_type: strategyType.value,
      symbol: form.value.symbol,
      volume: form.value.volume,
      sl: form.value.sl && form.value.sl > 0 ? form.value.sl : null,
      tp: form.value.tp && form.value.tp > 0 ? form.value.tp : null,
      entry_price: strategyType.value === 'trend_limit' ? form.value.entry_price : null,
      direction:
        strategyType.value === 'grid'
          ? ((form.value.direction === 'long' ? 'all' : 'sell') as 'all' | 'buy' | 'sell')
          : (null as 'all' | 'buy' | 'sell' | null),
      remark: form.value.remark || null,
    }
    const r: QuickTriggerResult = await quickTrigger(payload)
    if (r.result.status === 'accepted') {
      ElMessage.success(
        `已触发 ${STRATEGY_LABEL[strategyType.value]} ${form.value.symbol}（${r.result.targets ?? 0} 个目标节点）`,
      )
      dialogVisible.value = false
    } else if (r.result.status === 'duplicate') {
      ElMessage.warning('重复信号被抑制（5 秒内已有相同信号）')
    } else if (r.result.status === 'rejected') {
      lastError.value = r.result.reason || '触发被拒收'
    } else {
      ElMessage.info(`已提交：${r.result.status}`)
    }
  } catch (e) {
    const err = e as { response?: { data?: { detail?: string } }; message?: string }
    lastError.value = err?.response?.data?.detail || err?.message || '触发失败'
  } finally {
    submitting.value = false
  }
}

const dialogGroupName = computed(() => {
  if (!primaryNode.value) return ''
  const login = primaryNode.value.mt5_login ?? 'unknown'
  return `${strategyType.value}-${primaryNode.value.name}-${login}`
})

onMounted(async () => {
  await hub.fetchNodes()
  // 拉取主节点账户快照（如已有则跳过；store 也可能在等 WS 推送）
  if (primaryNode.value && !hub.accounts[primaryNode.value.node_id]) {
    try {
      await hub.fetchNodeAccount(primaryNode.value.node_id)
    } catch {
      /* 节点尚未上报快照，忽略 */
    }
  }
})
</script>

<template>
  <div class="trading-page">
    <div class="row between page-header">
      <div class="h1">交易</div>
    </div>

    <!-- 空态：无主节点 -->
    <div v-if="!primaryNode" class="card card-pad empty-state">
      <div class="empty-title">尚未设置主节点</div>
      <div class="empty-desc">请先在【节点】页将一个节点标记为主节点，再回到此页面触发开仓。</div>
      <button class="btn-primary" @click="router.push('/nodes')">前往节点页</button>
    </div>

    <template v-else>
      <!-- 策略选择 -->
      <div class="card card-pad strategy-tabs">
        <button
          v-for="(label, key) in STRATEGY_LABEL"
          :key="key"
          class="strategy-tab"
          :class="{ active: strategyType === key }"
          @click="strategyType = key as TradingStrategyType"
        >
          {{ label }}
        </button>
      </div>

      <!-- 主节点 / 操作节点 / 持仓 三块 -->
      <div class="grid-3">
        <div class="card card-pad">
          <div class="label">主节点</div>
          <div class="row" style="gap: 8px; margin-top: 8px">
            <span class="pill pill-primary">主节点</span>
            <strong>{{ primaryNode.name }}</strong>
          </div>
          <div class="muted small" style="margin-top: 6px">
            MT5 登录：{{ primaryNode.mt5_login ?? '—' }} · 服务器：{{ primaryNode.mt5_server || '—' }}
          </div>
        </div>

        <div class="card card-pad">
          <div class="label">操作节点</div>
          <div class="row" style="gap: 8px; margin-top: 8px">
            <span :class="['pill', isOnline ? 'pill-live' : 'pill-offline']">
              <span :class="['dot', isOnline ? 'online' : 'offline']"></span>
              {{ isOnline ? '在线' : '离线' }}
            </span>
          </div>
          <div v-if="account && currentMid > 0" class="muted small" style="margin-top: 6px">
            {{ form.symbol }} 现价：{{ currentMid.toFixed(5) }}
          </div>
        </div>

        <div class="card card-pad">
          <div class="label">持仓</div>
          <div class="big" style="margin-top: 8px">{{ positionsCount }}</div>
        </div>
      </div>

      <!-- 参数表单 -->
      <div class="card card-pad form-card">
        <div class="label">开仓参数</div>
        <div class="form-grid">
          <label>
            <span>开仓品种</span>
            <input v-model="form.symbol" placeholder="如 XAUUSD" />
          </label>
          <label>
            <span>开仓手数</span>
            <input v-model.number="form.volume" type="number" step="0.01" min="0.01" />
          </label>
          <label v-if="strategyType === 'trend_limit'">
            <span>入场价（必填）</span>
            <input v-model.number="form.entry_price" type="number" step="0.0001" />
          </label>
          <label v-if="strategyType !== 'grid'">
            <span>止损价</span>
            <input v-model.number="form.sl" type="number" step="0.0001" min="0" />
          </label>
          <label v-if="strategyType !== 'grid'">
            <span>止盈价</span>
            <input v-model.number="form.tp" type="number" step="0.0001" min="0" />
          </label>
          <label v-if="strategyType === 'grid'">
            <span>网格方向</span>
            <select v-model="form.direction">
              <option value="long">多头（默认）</option>
              <option value="short">空头</option>
            </select>
          </label>
          <label class="span-2">
            <span>备注</span>
            <input v-model="form.remark" maxlength="64" />
          </label>
        </div>

        <div class="row" style="margin-top: 16px; justify-content: flex-end">
          <button class="btn-primary" :disabled="!canSubmit" @click="openDialog">触发信号</button>
        </div>
        <div v-if="!isOnline" class="hint warn">主节点离线，无法触发</div>
      </div>
    </template>

    <!-- 触发弹窗 -->
    <el-dialog
      v-model="dialogVisible"
      :title="`触发信号 - ${STRATEGY_LABEL[strategyType]}`"
      width="520px"
    >
      <div class="dialog-form">
        <div class="kv"><span class="k">分组</span><span class="v">{{ dialogGroupName }}</span></div>
        <div class="kv"><span class="k">策略</span><span class="v">{{ STRATEGY_LABEL[strategyType] }}（自动复用/创建）</span></div>
        <div class="kv"><span class="k">绑定品种</span><span class="v">{{ form.symbol }}</span></div>
        <div class="kv" v-if="strategyType !== 'grid'"><span class="k">止损价</span><span class="v">{{ form.sl || '—' }}</span></div>
        <div class="kv" v-if="strategyType !== 'grid'"><span class="k">止盈价</span><span class="v">{{ form.tp || '—' }}</span></div>
        <div class="kv" v-if="strategyType === 'trend_limit'"><span class="k">入场价</span><span class="v">{{ form.entry_price }}</span></div>
        <div class="kv" v-if="strategyType === 'grid'"><span class="k">方向</span><span class="v">{{ form.direction === 'long' ? '多头' : '空头' }}</span></div>
        <div class="kv" v-if="form.remark"><span class="k">备注</span><span class="v">{{ form.remark }}</span></div>

        <div class="hint">提示：首次触发会按 (策略类型-节点名-MT5 登录号) 自动创建「分组+策略」并把主节点加入；后续直接复用。</div>
        <div v-if="lastError" class="hint error">{{ lastError }}</div>
      </div>

      <template #footer>
        <button class="btn-ghost" @click="dialogVisible = false">取消</button>
        <button class="btn-primary" :disabled="submitting" @click="submit">
          {{ submitting ? '提交中...' : '确认触发' }}
        </button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.trading-page { display: flex; flex-direction: column; gap: 16px; }
.strategy-tabs { display: flex; gap: 8px; }
.strategy-tab {
  flex: 1; padding: 14px;
  background: rgba(255, 255, 255, 0.04);
  border: 1px solid rgba(255, 255, 255, 0.1);
  border-radius: 8px; color: inherit; cursor: pointer; font-size: 14px;
}
.strategy-tab.active { border-color: #4f8cff; background: rgba(79, 140, 255, 0.08); }
.grid-3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 16px; }
@media (max-width: 768px) { .grid-3 { grid-template-columns: 1fr; } }
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px 16px; margin-top: 12px; }
.form-grid label { display: flex; flex-direction: column; gap: 4px; }
.form-grid label.span-2 { grid-column: span 2; }
.form-grid input, .form-grid select {
  background: rgba(255, 255, 255, 0.04);
  border: 1px solid rgba(255, 255, 255, 0.1);
  color: inherit; padding: 8px 10px; border-radius: 6px;
}
.kv { display: grid; grid-template-columns: 100px 1fr; padding: 6px 0; }
.kv .k { color: var(--muted, #888); }
.dialog-form { display: flex; flex-direction: column; gap: 4px; }
.hint { margin-top: 12px; font-size: 12px; color: var(--muted, #888); }
.hint.warn { color: #f5a623; }
.hint.error { color: #e74c3c; }
.empty-state { text-align: center; padding: 40px 20px; }
.empty-title { font-size: 18px; font-weight: 600; margin-bottom: 8px; }
.empty-desc { color: var(--muted, #888); margin-bottom: 16px; }
.big { font-size: 28px; font-weight: 700; }
.pill { display: inline-flex; align-items: center; gap: 6px; padding: 2px 8px; border-radius: 12px; font-size: 12px; }
.pill-primary { background: rgba(79, 140, 255, 0.15); color: #4f8cff; }
</style>
