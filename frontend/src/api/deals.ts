import api from './client'
import type { LeaderPerformance, PaginatedDeals } from './types'

/** 某节点的成交明细分页（订单历史 Tab）。响应自带 summary（与 items 同条件）。 */
export async function fetchNodeDeals(
  nodeId: string,
  page = 1,
  pageSize = 50,
  params: Record<string, string | number> = {},
): Promise<PaginatedDeals> {
  const { data } = await api.get<PaginatedDeals>(`/api/deals/nodes/${nodeId}/deals`, {
    params: { page, page_size: pageSize, ...params },
  })
  return data
}

/** 带单员绩效（累计收益 / 收益率 / 胜率 / 笔数 + 跟单人数） */
export async function fetchLeaderPerformance(
  leaderUserId: number,
  window = '30d',
): Promise<LeaderPerformance> {
  const { data } = await api.get<LeaderPerformance>(
    `/api/deals/follow/leaders/${leaderUserId}/performance`,
    { params: { window } },
  )
  return data
}
