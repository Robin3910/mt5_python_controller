/**
 * 与本机 HTTP/SSE 桥通信的 API 层。
 *
 * 桥地址在 URL query 里带一次性 session token（见 dashboard_bridge.py）：
 * EventSource 不能自定义 header，所以 token 只能走 query。
 */

export interface NodeItem {
  id: string
  title: string
  path_short: string
  status_line: string
  alive: boolean
  health: string
  daemon: boolean
  mt5_login: number
  algo_trading_disabled: boolean
  node_id: string
  /** 券商服务器名（如 ICMarkets-Live）；后端没给时为空串。 */
  server_label: string
  /** 交易账号 ID（「ID」胶囊里的数字）；没有时为空串。 */
  account_label: string
  uptime_text: string
  mt5_connected: boolean
  mt5_authenticated: boolean
  algo_trading_ok: boolean
  algo_trading_known: boolean
  badge_text: string
  badge_tone: string
  pending_review: boolean
  /** 该节点今天的信号数。 */
  today_signals: number
  /** 该节点今天的平均延迟（毫秒）；null = 没有可算的样本。 */
  avg_latency_ms: number | null
}

export interface HeroStats {
  total: number
  online: number
  starting: number
  stopped: number
  pending: number
  /** 今日信号：今天（UTC+8 自然日）下发到本机可见节点的信号条数。 */
  today_signals: number
  /** 平均延迟（毫秒）；null = 没有可算的样本，前端显示「--」。 */
  avg_latency_ms: number | null
  alerts: number
}

export interface Snapshot {
  type: 'snapshot' | 'locked'
  seq?: number
  authenticated: boolean
  is_admin?: boolean
  username?: string
  reason?: string
  hero?: HeroStats
  nodes?: NodeItem[]
  busy?: Record<string, string>
  version?: Record<string, unknown>
  /** 版本更新徽标：后端发布版本 + 本机待更新实例数。 */
  update?: UpdateBadge
}

/** 版本更新徽标。 */
export interface UpdateBadge {
  /** 后端当前发布版本；空串表示尚未发布任何版本。 */
  published: string
  /** 本机版本低于发布版本的实例数。 */
  pending: number
}

export interface DetailChip {
  key: string
  text: string
  tone: string
  visible: boolean
}

export interface DetailPayload {
  empty: boolean
  title: string
  subtitle: string
  daemon: boolean
  summary: string
  alive: boolean
  health: string
  busy: boolean
  chips: DetailChip[]
}

export interface BatchResult {
  id: string
  name: string
  ok: boolean
  message: string
}

export interface ConnectionInfo {
  ok: boolean
  base: string
  username: string
  is_admin: boolean
  node_count: number
}

/** 后端配置状态：让登录页能区分「没配 APP_URL」与「账号密码错」。 */
export interface ConfigStatus {
  ok: boolean
  app_url: string
  env_path: string
  env_exists: boolean
  auto_login: boolean
  error: string
}

export interface EnvPayload {
  ok: boolean
  cwd: string
  text: string
  values: Record<string, string>
  env_path: string
}

export interface VersionInfo {
  ok: boolean
  current: Record<string, unknown>
  published: string
  available: Array<Record<string, unknown>>
}

export interface LocalVersionInfo {
  ok: boolean
  current: string
  backups: string[]
}

export interface DirCheck {
  ok: boolean
  message: string
  terminal: string
  terminal_path?: string
  existing_version?: string
  env_exists?: boolean
  suggested_name?: string
}

export interface ImportVersionItem {
  version: string
  size: number
  sha256: string
  notes: string
  is_current: boolean
}

export interface ImportVersions {
  ok: boolean
  error: string
  versions: ImportVersionItem[]
  current: string
}

export interface ImportStep {
  key: string
  label: string
  pct: number
  status: 'pending' | 'active' | 'done' | 'failed'
}

export interface ImportProgress {
  task_id: string
  steps: ImportStep[]
  log: string[]
  done: boolean
  error: string
  result: Record<string, unknown>
  elapsed: number
}

/** 版本更新：单个实例的版本信息。 */
export interface UpdateInstance {
  id: string
  name: string
  /** 本机当前客户端版本；空串表示读不到。 */
  version: string
  /** 本机可回滚的备份版本（新版在前）。 */
  backups: string[]
  alive: boolean
}

/** 版本更新：后端可下载的版本。 */
export interface UpdateVersionItem {
  version: string
  size: number
  sha256: string
  notes: string
  is_current: boolean
}

/** 版本更新：检测结果。 */
export interface UpdateDetect {
  ok: boolean
  error: string
  /** 后端当前发布版本。 */
  published: string
  versions: UpdateVersionItem[]
  notes: string
}

/** 版本更新：本机实例 + 发布版本 + 待更新数。 */
export interface UpdateInstances {
  ok: boolean
  error: string
  instances: UpdateInstance[]
  published: string
  pending: number
}

/** 版本更新：单个实例的判定（升级 / 降级 / 相同）。 */
export interface UpdateRow {
  id: string
  name: string
  current: string
  target: string
  kind: string
  label: string
}

/** 版本更新：更新前的确认摘要。 */
export interface UpdatePrepare {
  ok: boolean
  error: string
  summary: string
  rows: UpdateRow[]
  /** 目标版本低于当前版本的实例名（需要二次确认）。 */
  downgrades: string[]
}

/** 版本更新：单个实例的结果。 */
export interface UpdateResultRow {
  id: string
  name: string
  ok: boolean
  message: string
  old_version: string
  new_version: string
}

/** 版本更新：任务的进度与结果。 */
export interface UpdateProgress {
  task_id: string
  kind: string
  steps: ImportStep[]
  log: string[]
  done: boolean
  error: string
  result: {
    ok_count?: number
    total?: number
    results?: UpdateResultRow[]
  }
  elapsed: number
}

/** 从当前 URL 取回 session token。 */
export function sessionToken(): string {
  return new URLSearchParams(window.location.search).get('token') ?? ''
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const token = sessionToken()
  const sep = path.includes('?') ? '&' : '?'
  const url = `${path}${sep}token=${encodeURIComponent(token)}`

  const resp = await fetch(url, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(init?.headers ?? {}),
    },
  })

  let payload: unknown = null
  const text = await resp.text()
  if (text) {
    try {
      payload = JSON.parse(text)
    } catch {
      throw new Error(`接口返回了非 JSON 内容（HTTP ${resp.status}）`)
    }
  }

  if (!resp.ok) {
    const message =
      payload && typeof payload === 'object' && 'error' in payload
        ? String((payload as { error: unknown }).error)
        : `请求失败（HTTP ${resp.status}）`
    throw new Error(message)
  }

  return payload as T
}

export const api = {
  state: () => request<Snapshot>('/api/state'),

  login: (username: string, password: string, code = '') =>
    request<{ ok: boolean; requires_2fa: boolean; error: string }>('/api/login', {
      method: 'POST',
      body: JSON.stringify({ username, password, code }),
    }),

  logout: () => request<{ ok: boolean }>('/api/logout', { method: 'POST', body: '{}' }),

  config: () => request<ConfigStatus>('/api/config'),

  detail: (instanceId: string) =>
    request<DetailPayload>(`/api/detail?id=${encodeURIComponent(instanceId)}`),

  action: (action: string, instanceId: string) =>
    request<{ ok: boolean }>('/api/action', {
      method: 'POST',
      body: JSON.stringify({ action, id: instanceId }),
    }),

  batch: (action: string) =>
    request<{ ok: boolean; results: BatchResult[] }>('/api/batch', {
      method: 'POST',
      body: JSON.stringify({ action }),
    }),

  refresh: () => request<{ ok: boolean }>('/api/refresh', { method: 'POST', body: '{}' }),

  // —— 对话框类 ——
  connection: () => request<ConnectionInfo>('/api/connection'),

  testConnection: () =>
    request<{ ok: boolean; message: string }>('/api/connection/test', {
      method: 'POST',
      body: '{}',
    }),

  readEnv: (instanceId: string) =>
    request<EnvPayload>(`/api/env/read?id=${encodeURIComponent(instanceId)}`),

  writeEnv: (instanceId: string, text: string) =>
    request<{ ok: boolean }>('/api/env/write', {
      method: 'POST',
      body: JSON.stringify({ id: instanceId, text }),
    }),

  versionInfo: () => request<VersionInfo>('/api/version/info'),

  localVersions: (instanceId: string) =>
    request<LocalVersionInfo>(`/api/version/local?id=${encodeURIComponent(instanceId)}`),

  // —— 导入节点 ——
  checkDir: (path: string) =>
    request<DirCheck>(`/api/import/check-dir?path=${encodeURIComponent(path)}`),

  importVersions: () => request<ImportVersions>('/api/import/versions'),

  /** 打开系统目录选择器（GUI 线程弹窗，用户取消时 path 为空）。 */
  pickDir: () =>
    request<{ ok: boolean; path: string; cancelled: boolean }>('/api/import/pick-dir', {
      method: 'POST',
      body: '{}',
    }),

  prepareImport: (payload: {
    directory: string
    name: string
    version: string
    sha256: string
    account: string
    write_env: boolean
  }) =>
    request<{ ok: boolean; error: string; summary: string; login?: number }>(
      '/api/import/prepare',
      { method: 'POST', body: JSON.stringify(payload) },
    ),

  startImport: (payload: {
    directory: string
    name: string
    version: string
    sha256: string
    account: string
    write_env: boolean
  }) =>
    request<{ ok: boolean; error: string; task_id: string }>('/api/import/start', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  importProgress: (taskId: string) =>
    request<ImportProgress>(`/api/import/progress?task_id=${encodeURIComponent(taskId)}`),

  // —— 版本更新 ——
  updateInstances: () => request<UpdateInstances>('/api/version-update/instances'),

  updateDetect: () => request<UpdateDetect>('/api/version-update/detect'),

  updatePrepare: (payload: { version: string; ids: string[] }) =>
    request<UpdatePrepare>('/api/version-update/prepare', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  updateStart: (payload: { version: string; sha256: string; ids: string[] }) =>
    request<{ ok: boolean; error: string; task_id: string }>('/api/version-update/start', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  updateRollback: (payload: { version: string; ids: string[] }) =>
    request<{ ok: boolean; error: string; task_id: string }>('/api/version-update/rollback', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  updateProgress: (taskId: string) =>
    request<UpdateProgress>(`/api/version-update/progress?task_id=${encodeURIComponent(taskId)}`),

  // —— 启动诊断 ——
  /** 启动体检报告：查 WebView2 / .NET / 前端产物 / 打包依赖。 */
  diagnose: () => request<Record<string, unknown>>('/api/diagnose'),

  /** 前端脚本错误（白屏排查）。 */
  clientLogs: () =>
    request<{ ok: boolean; count: number; entries: Array<Record<string, unknown>> }>(
      '/api/client-logs',
    ),
}

/**
 * 订阅桥的 SSE 推送。返回取消订阅函数。
 *
 * EventSource 不能带自定义 header，所以 token 走 query。
 */
export function subscribe(onSnapshot: (snap: Snapshot) => void): () => void {
  const token = sessionToken()
  const source = new EventSource(`/api/events?token=${encodeURIComponent(token)}`)

  const handle = (event: MessageEvent) => {
    try {
      onSnapshot(JSON.parse(event.data) as Snapshot)
    } catch {
      /* 忽略坏包，不打断连接 */
    }
  }

  source.addEventListener('snapshot', handle as EventListener)
  source.onmessage = handle

  return () => source.close()
}
