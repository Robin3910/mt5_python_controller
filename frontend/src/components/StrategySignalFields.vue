<script setup lang="ts">
// 策略信号的公共表单项：品种、方向、手数、备注、止损、止盈、入场价。
// 手动触发弹窗与分组内「编辑策略」弹窗共用，定向勾选不在这里。
import { computed } from 'vue'
import FormLabel from '@/components/FormLabel.vue'
import TagSelect from '@/components/TagSelect.vue'
import type { TagOption } from '@/components/tagOption'
import type { ManualSignalAction } from '@/api/types'
import type { StrategySignalDraft } from '@/components/strategySignal'

const SIGNAL_HELP = {
  symbol:
    '信号品种。分组链路按「绑定策略的品种」匹配：只有已启用、且绑定了同品种启用策略的分组才会收到本信号。' +
    '不同券商的后缀差异（XAUUSD / XAUUSDm / XAUUSD.pro）会自动归一化后匹配。',
  action:
    'BUY / SELL 触发策略托管开仓：命中分组的有效节点会收到首单参数与策略规则快照，之后由节点自主按规则加仓。' +
    'CLOSE 是终止指令，平掉命中分组内进行中任务对应魔术号的持仓并结束节点侧监控。',
  volume:
    '首单手数。分组链路直接采用此手数（仅受单笔上限保护），不走节点的按币种手数策略。',
  stop_loss:
    '首单止损价（绝对价格），留空表示不设。\n' +
    '策略模版2（以损定量趋势单）必填：它的手数就是由风险金额与止损距离反推的，缺止损会被拒收。',
  take_profit: '首单止盈价（绝对价格），留空表示不设。',
  entry_price:
    '限价开仓的挂单价（对应 Webhook 的 limit_price 字段），留空表示不设。\n' +
    '只有配成「限价」开仓的策略模版2 会用它：底仓与分散仓全部挂在这个价。\n' +
    '这类策略缺了入场价会被拒收；配成「市价」的策略与其它模版忽略该字段。\n' +
    '入场价必须落在止损价的盈利侧（多单高于止损、空单低于止损），否则挂单一成交就已越过止损。',
  comment: '订单备注，会写入 MT5 订单的 comment 字段，便于对账。',
}

const ACTION_TAGS: TagOption<ManualSignalAction>[] = [
  { value: 'BUY', label: 'BUY', hint: '策略托管开多', tone: 'buy' },
  { value: 'SELL', label: 'SELL', hint: '策略托管开空', tone: 'sell' },
  { value: 'CLOSE', label: 'CLOSE', hint: '终止任务并平仓', tone: 'close' },
]

const form = defineModel<StrategySignalDraft>({ required: true })

withDefaults(
  defineProps<{
    idPrefix?: string
    symbolOptions?: string[]
  }>(),
  {
    idPrefix: 'signal',
    symbolOptions: () => [],
  },
)

const isCloseAction = computed(() => form.value.action === 'CLOSE')
</script>

<template>
  <div class="form-grid two">
    <div>
      <FormLabel :field-id="`${idPrefix}-symbol`" text="信号品种" :help="SIGNAL_HELP.symbol" />
      <input :id="`${idPrefix}-symbol`" v-model="form.symbol" placeholder="例如：XAUUSD" />
      <div v-if="symbolOptions.length" class="symbol-picks">
        <span class="muted">已配置：</span>
        <button
          v-for="s in symbolOptions"
          :key="s"
          type="button"
          class="btn-sm btn-ghost"
          @click="form.symbol = s"
        >
          {{ s }}
        </button>
      </div>
    </div>
    <div>
      <FormLabel text="信号方向" :help="SIGNAL_HELP.action" />
      <TagSelect v-model="form.action" :options="ACTION_TAGS" aria-label="信号方向" />
    </div>

    <div v-if="!isCloseAction" class="trigger-param-grid">
      <div>
        <FormLabel :field-id="`${idPrefix}-volume`" text="首单手数" :help="SIGNAL_HELP.volume" />
        <input
          :id="`${idPrefix}-volume`"
          v-model.number="form.volume"
          type="number"
          min="0.01"
          step="0.01"
        />
      </div>
      <div>
        <FormLabel :field-id="`${idPrefix}-comment`" text="订单备注" :help="SIGNAL_HELP.comment" />
        <input :id="`${idPrefix}-comment`" v-model="form.comment" placeholder="选填" />
      </div>
      <div>
        <FormLabel :field-id="`${idPrefix}-sl`" text="止损价" :help="SIGNAL_HELP.stop_loss" />
        <input
          :id="`${idPrefix}-sl`"
          v-model.number="form.stop_loss"
          type="number"
          step="0.01"
          placeholder="留空表示不设"
        />
      </div>
      <div>
        <FormLabel :field-id="`${idPrefix}-tp`" text="止盈价" :help="SIGNAL_HELP.take_profit" />
        <input
          :id="`${idPrefix}-tp`"
          v-model.number="form.take_profit"
          type="number"
          step="0.01"
          placeholder="留空表示不设"
        />
      </div>
      <div>
        <FormLabel
          :field-id="`${idPrefix}-entry`"
          text="入场价（限价开仓）"
          :help="SIGNAL_HELP.entry_price"
        />
        <input
          :id="`${idPrefix}-entry`"
          v-model.number="form.entry_price"
          type="number"
          step="any"
          placeholder="留空表示不设"
        />
      </div>
    </div>
    <p v-else class="span-full trigger-warning">
      CLOSE 会平掉命中分组内进行中任务对应魔术号的持仓并结束节点侧策略监控，不影响按币种分发链路的持仓。
    </p>
  </div>
</template>

<style scoped>
.symbol-picks {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
  font-size: 12px;
}

.trigger-warning {
  margin: 0;
  padding: 10px 12px;
  border: 1px solid rgba(245, 158, 11, 0.25);
  border-radius: 8px;
  background: rgba(245, 158, 11, 0.08);
  color: #fbbf24;
  font-size: 12px;
  line-height: 1.6;
}

.trigger-param-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
  grid-column: 1 / -1;
  min-width: 0;
}

.trigger-param-grid > div {
  min-width: 0;
}

@media (max-width: 768px) {
  .trigger-param-grid {
    gap: 10px;
  }
}
</style>
