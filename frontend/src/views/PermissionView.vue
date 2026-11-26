<script setup lang="ts">
// 用户权限：超级管理员在此建用户/角色、勾选菜单、把节点划给用户。
// 菜单与拦截以后端为准；本页只做配置与展示。
import { computed, onMounted, ref } from 'vue'
import { ElDropdown, ElDropdownItem, ElDropdownMenu, ElMessage } from 'element-plus'
import 'element-plus/es/components/dropdown/style/css'
import 'element-plus/es/components/message/style/css'
import FormLabel from '@/components/FormLabel.vue'
import TagSelect from '@/components/TagSelect.vue'
import type { TagOption } from '@/components/tagOption'
import { useAuthStore } from '@/stores/auth'
import { useHubStore } from '@/stores/hub'
import { CAPABILITIES, useRbacStore } from '@/stores/rbac'
import { confirmAction } from '@/utils/confirm'
import type {
  NodeAssignConflict,
  NodeOut,
  RoleOut,
  UserOut,
  UserUpdatePayload,
} from '@/api/types'

const USERNAME_RE = /^[A-Za-z0-9_.-]{3,32}$/
const ROLE_CODE_RE = /^[a-z][a-z0-9_]{1,31}$/
const MIN_PASSWORD = 6

const auth = useAuthStore()
const hub = useHubStore()
const rbac = useRbacStore()

const tab = ref<'users' | 'roles'>('users')
const loading = ref(false)
const userQuery = ref('')
const roleQuery = ref('')

const showUserForm = ref(false)
const userFormMode = ref<'create' | 'edit'>('create')
const editingUser = ref<UserOut | null>(null)
const userForm = ref({
  username: '',
  password: '',
  display_name: '',
  is_active: true,
  role_ids: [] as number[],
})
const userFormError = ref('')
const userSaving = ref(false)
/** 手机号绑定输入：列表接口只回显脱敏号码，因此这里存「用户新输入的号码」 */
const phoneInput = ref('')
/** 勾选后提交空串解绑手机号 */
const phoneUnbind = ref(false)

const showRoles = ref(false)
const roleTarget = ref<UserOut | null>(null)
const selectedRoleIds = ref<number[]>([])
const rolesError = ref('')
const rolesSaving = ref(false)

const showPwd = ref(false)
const pwdTarget = ref<UserOut | null>(null)
const newPassword = ref('')
const confirmPassword = ref('')
const pwdError = ref('')
const pwdSaving = ref(false)

const showNodes = ref(false)
const nodeTarget = ref<UserOut | null>(null)
const selectedNodeIds = ref<string[]>([])
const nodeQuery = ref('')
const reclaimAdminId = ref<number>()
type NodeFilter = 'all' | 'unassigned' | 'mine' | 'others'
const nodeFilter = ref<NodeFilter>('all')
const NODE_FILTER_TAGS: TagOption<NodeFilter>[] = [
  { value: 'all', label: '全部' },
  { value: 'unassigned', label: '管理员名下' },
  { value: 'mine', label: '已分配给该用户' },
  { value: 'others', label: '其他用户' },
]
const USER_ACTIVE_TAGS: TagOption<boolean>[] = [
  { value: true, label: '启用', tone: 'on' },
  { value: false, label: '禁用', tone: 'off' },
]
const ROLE_ENABLED_TAGS: TagOption<boolean>[] = [
  { value: true, label: '启用', tone: 'on' },
  { value: false, label: '停用', tone: 'off' },
]
const nodeError = ref('')
const nodeHint = ref('')
const pendingMemberships = ref<NodeAssignConflict & { reason: 'confirm_required' } | null>(null)
const nodeSaving = ref(false)

const showRoleForm = ref(false)
const roleFormMode = ref<'create' | 'edit'>('create')
const editingRole = ref<RoleOut | null>(null)
const roleForm = ref({
  code: '',
  name: '',
  remark: '',
  enabled: true,
  menus: [] as string[],
  capabilities: [] as string[],
})
const roleFormError = ref('')
const roleSaving = ref(false)

const filteredUsers = computed(() => {
  const q = userQuery.value.trim().toLowerCase()
  if (!q) return rbac.users
  return rbac.users.filter((u) => {
    const roles = u.roles.map((r) => r.name).join(' ')
    return [u.username, u.display_name || '', u.phone || '', roles].some((s) => s.toLowerCase().includes(q))
  })
})

const filteredRoles = computed(() => {
  const q = roleQuery.value.trim().toLowerCase()
  if (!q) return rbac.roles
  return rbac.roles.filter((r) =>
    [r.code, r.name, r.remark || '', r.menus.join(' ')].some((s) => s.toLowerCase().includes(q)),
  )
})

const enabledRoles = computed(() => rbac.roles.filter((r) => r.enabled))

const filteredAssignNodes = computed(() => {
  const q = nodeQuery.value.trim().toLowerCase()
  return hub.nodes.filter((n) => {
    const adminOwned = rbac.users.some(u => u.is_admin && u.id === n.owner_user_id)
    if (nodeFilter.value === 'unassigned' && !adminOwned) return false
    if (nodeFilter.value === 'mine' && n.owner_user_id !== nodeTarget.value?.id) return false
    if (nodeFilter.value === 'others') {
      if (adminOwned || n.owner_user_id === nodeTarget.value?.id) return false
    }
    if (!q) return true
    return [n.name, n.node_id, String(n.mt5_login || ''), n.owner_username || '']
      .some((s) => s.toLowerCase().includes(q))
  })
})

const selectedNodeCount = computed(() => selectedNodeIds.value.length)

function errText(e: unknown, fallback: string): string {
  const err = e as { response?: { data?: { detail?: unknown } }; message?: string }
  const d = err?.response?.data?.detail
  if (typeof d === 'string' && d) return d
  if (Array.isArray(d) && d.length) {
    const first = d[0] as { msg?: string }
    return first?.msg || fallback
  }
  if (d && typeof d === 'object' && 'message' in d) {
    const msg = (d as { message?: unknown }).message
    if (typeof msg === 'string' && msg) return msg
  }
  return err?.message || fallback
}

function assignConflict(e: unknown): NodeAssignConflict | null {
  const err = e as { response?: { status?: number; data?: { detail?: unknown } } }
  if (err.response?.status !== 409) return null
  const d = err.response.data?.detail
  if (d && typeof d === 'object' && 'reason' in d) return d as NodeAssignConflict
  return null
}

function fmtTime(sec: number | null | undefined): string {
  return sec ? new Date(sec * 1000).toLocaleString() : '—'
}

function roleNames(user: UserOut): string {
  return user.roles.map((r) => r.name).join('、') || '—'
}

function menuNames(codes: string[]): string {
  return codes.map((c) => rbac.menuName(c)).join('、') || '—'
}

function capabilityNames(codes: string[]): string {
  return codes.map((c) => CAPABILITIES.find((cap) => cap.code === c)?.label || c).join('、') || '—'
}

/** 单条能力码的展示名（与 capabilityNames 一致，但返回单数）；找不到时回落到原码。 */
function capabilityLabel(code: string): string {
  return CAPABILITIES.find((cap) => cap.code === code)?.label || code
}

function nodeOwnerLabel(n: NodeOut): string {
  if (n.owner_user_id == null) return '历史未归属'
  return n.owner_username || `用户 #${n.owner_user_id}`
}

function defaultUserRoleId(): number | null {
  return rbac.roles.find((r) => r.code === 'user')?.id ?? null
}

function isSelf(user: UserOut): boolean {
  return user.id === auth.me?.user_id
}

function isAdminRole(role: RoleOut): boolean {
  return role.code === 'admin'
}

async function loadAll(): Promise<void> {
  loading.value = true
  try {
    await Promise.all([rbac.fetchUsers(), rbac.fetchRoles(), rbac.fetchMenus(), hub.fetchNodes()])
  } catch (e: unknown) {
    ElMessage.error(errText(e, '加载失败'))
  } finally {
    loading.value = false
  }
}

onMounted(loadAll)

function openCreateUser(): void {
  userFormMode.value = 'create'
  editingUser.value = null
  const def = defaultUserRoleId()
  userForm.value = {
    username: '',
    password: '',
    display_name: '',
    is_active: true,
    role_ids: def ? [def] : [],
  }
  phoneInput.value = ''
  phoneUnbind.value = false
  userFormError.value = ''
  showUserForm.value = true
}

function openEditUser(user: UserOut): void {
  userFormMode.value = 'edit'
  editingUser.value = user
  userForm.value = {
    username: user.username,
    password: '',
    display_name: user.display_name || '',
    is_active: user.is_active,
    role_ids: user.roles.map((r) => r.id),
  }
  phoneInput.value = ''
  phoneUnbind.value = false
  userFormError.value = ''
  showUserForm.value = true
}

function toggleUserRole(id: number, checked: boolean): void {
  const set = new Set(userForm.value.role_ids)
  if (checked) set.add(id)
  else set.delete(id)
  userForm.value.role_ids = [...set]
}

async function saveUser(): Promise<void> {
  const f = userForm.value
  userFormError.value = ''
  if (userFormMode.value === 'create') {
    if (!USERNAME_RE.test(f.username.trim())) {
      userFormError.value = '用户名须为 3~32 位字母数字及 _ . -'
      return
    }
    if (f.password.length < MIN_PASSWORD) {
      userFormError.value = `密码至少 ${MIN_PASSWORD} 位`
      return
    }
  }
  // 手机号：留空且未勾选解绑 = 不改；填了号码 = 绑定；勾选解绑 = 传空串
  const phone = phoneInput.value.trim()
  if (phone && !/^1[3-9]\d{9}$/.test(phone)) {
    userFormError.value = '手机号须为 11 位中国大陆号码'
    return
  }
  userSaving.value = true
  try {
    if (userFormMode.value === 'create') {
      await rbac.createUser({
        username: f.username.trim(),
        password: f.password,
        display_name: f.display_name.trim() || null,
        is_active: f.is_active,
        role_ids: f.role_ids,
      })
      ElMessage.success('用户已创建')
    } else if (editingUser.value) {
      const patch: UserUpdatePayload = {
        display_name: f.display_name.trim() || null,
        is_active: f.is_active,
      }
      if (phoneUnbind.value) patch.phone = ''
      else if (phone) patch.phone = phone
      await rbac.updateUser(editingUser.value.id, patch)
      ElMessage.success('用户已更新')
    }
    showUserForm.value = false
  } catch (e: unknown) {
    userFormError.value = errText(e, '保存失败')
  } finally {
    userSaving.value = false
  }
}

async function toggleActive(user: UserOut): Promise<void> {
  const next = user.is_active ? '禁用' : '启用'
  const extra = user.is_active ? '禁用后该用户全部会话立即失效。' : '启用后可重新登录。'
  if (!(await confirmAction(`确认${next}用户「${user.username}」？\n\n${extra}`))) return
  try {
    await rbac.updateUser(user.id, { is_active: !user.is_active })
    ElMessage.success(`已${next}`)
  } catch (e: unknown) {
    ElMessage.error(errText(e, `${next}失败`))
  }
}

function openRoles(user: UserOut): void {
  roleTarget.value = user
  selectedRoleIds.value = user.roles.map((r) => r.id)
  rolesError.value = ''
  showRoles.value = true
}

function toggleSelectedRole(id: number, checked: boolean): void {
  const set = new Set(selectedRoleIds.value)
  if (checked) set.add(id)
  else set.delete(id)
  selectedRoleIds.value = [...set]
}

async function saveRoles(): Promise<void> {
  if (!roleTarget.value) return
  rolesError.value = ''
  rolesSaving.value = true
  try {
    await rbac.setUserRoles(roleTarget.value.id, selectedRoleIds.value)
    ElMessage.success('角色已更新')
    showRoles.value = false
  } catch (e: unknown) {
    rolesError.value = errText(e, '分配角色失败')
  } finally {
    rolesSaving.value = false
  }
}

function openPwd(user: UserOut): void {
  pwdTarget.value = user
  newPassword.value = ''
  confirmPassword.value = ''
  pwdError.value = ''
  showPwd.value = true
}

async function savePwd(): Promise<void> {
  if (!pwdTarget.value) return
  pwdError.value = ''
  if (newPassword.value.length < MIN_PASSWORD) {
    pwdError.value = `密码至少 ${MIN_PASSWORD} 位`
    return
  }
  if (newPassword.value !== confirmPassword.value) {
    pwdError.value = '两次输入的密码不一致'
    return
  }
  if (!(await confirmAction(
    `确认重置「${pwdTarget.value.username}」的密码？\n\n该用户全部旧会话将立即失效。`,
    '确认重置密码',
  ))) return
  pwdSaving.value = true
  try {
    await rbac.resetPassword(pwdTarget.value.id, newPassword.value)
    ElMessage.success('密码已重置')
    showPwd.value = false
  } catch (e: unknown) {
    pwdError.value = errText(e, '重置失败')
  } finally {
    pwdSaving.value = false
  }
}

async function reset2fa(user: UserOut): Promise<void> {
  if (!(await confirmAction(
    `确认清除「${user.username}」的 2FA 绑定？\n\n该用户下次登录将不再要求动态码，可自行重新绑定。`,
    '确认重置 2FA',
  ))) return
  try {
    await rbac.reset2fa(user.id)
    ElMessage.success('2FA 已清除')
  } catch (e: unknown) {
    ElMessage.error(errText(e, '重置 2FA 失败'))
  }
}

async function removeUser(user: UserOut): Promise<void> {
  if (!(await confirmAction(
    `确认删除用户「${user.username}」？\n\n名下仍有节点、分组或策略时无法删除，请先回收或改为禁用。`,
    '确认删除',
  ))) return
  try {
    await rbac.deleteUser(user.id)
    ElMessage.success('用户已删除')
  } catch (e: unknown) {
    ElMessage.error(errText(e, '删除失败'))
  }
}

async function openNodes(user: UserOut): Promise<void> {
  nodeTarget.value = user
  reclaimAdminId.value = rbac.activeAdmins.find(u => u.id !== user.id)?.id
  selectedNodeIds.value = [...user.node_ids]
  nodeQuery.value = ''
  nodeFilter.value = 'all'
  nodeError.value = ''
  nodeHint.value = ''
  pendingMemberships.value = null
  showNodes.value = true
  try {
    await hub.fetchNodes()
  } catch {
    /* 列表仍可用已缓存节点 */
  }
}

function isNodeSelected(id: string): boolean {
  return selectedNodeIds.value.includes(id)
}

function toggleNode(id: string, checked: boolean): void {
  pendingMemberships.value = null
  nodeError.value = ''
  const set = new Set(selectedNodeIds.value)
  if (checked) set.add(id)
  else set.delete(id)
  selectedNodeIds.value = [...set]
}

function selectVisibleNodes(checked: boolean): void {
  pendingMemberships.value = null
  const set = new Set(selectedNodeIds.value)
  for (const n of filteredAssignNodes.value) {
    if (checked) set.add(n.node_id)
    else set.delete(n.node_id)
  }
  selectedNodeIds.value = [...set]
}

function formatMemberships(conflict: Extract<NodeAssignConflict, { reason: 'confirm_required' }>): string {
  return conflict.memberships
    .map((m) => `${m.node_name || m.node_id} → ${m.group_name || m.group_id}`)
    .join('\n')
}

async function saveNodes(confirm = false): Promise<void> {
  if (!nodeTarget.value) return
  nodeError.value = ''
  nodeHint.value = ''
  nodeSaving.value = true
  try {
    const result = await rbac.assignNodes(nodeTarget.value.id, selectedNodeIds.value, confirm, reclaimAdminId.value)
    await hub.fetchNodes()
    const parts: string[] = []
    if (result.added.length) parts.push(`划入 ${result.added.length} 个`)
    if (result.removed.length) parts.push(`收回 ${result.removed.length} 个`)
    if (result.removed_memberships.length) parts.push(`移出 ${result.removed_memberships.length} 条分组成员`)
    ElMessage.success(parts.length ? `节点已分配（${parts.join('，')}）` : '节点归属未变化')
    showNodes.value = false
    pendingMemberships.value = null
  } catch (e: unknown) {
    const conflict = assignConflict(e)
    if (conflict?.reason === 'active_tasks') {
      const names = conflict.nodes.map((n) => `${n.name || n.node_id}（${n.tasks} 个任务）`).join('、')
      nodeError.value = `${conflict.message}：${names}`
      pendingMemberships.value = null
    } else if (conflict?.reason === 'confirm_required') {
      pendingMemberships.value = conflict
      nodeHint.value = conflict.message
    } else {
      nodeError.value = errText(e, '分配失败')
      pendingMemberships.value = null
    }
  } finally {
    nodeSaving.value = false
  }
}

function openCreateRole(): void {
  roleFormMode.value = 'create'
  editingRole.value = null
  roleForm.value = {
    code: '',
    name: '',
    remark: '',
    enabled: true,
    menus: rbac.assignableMenus.map((m) => m.code),
    capabilities: [],
  }
  roleFormError.value = ''
  showRoleForm.value = true
}

function openEditRole(role: RoleOut): void {
  roleFormMode.value = 'edit'
  editingRole.value = role
  roleForm.value = {
    code: role.code,
    name: role.name,
    remark: role.remark || '',
    enabled: role.enabled,
    menus: [...role.menus],
    capabilities: [...(role.capabilities || [])],
  }
  roleFormError.value = ''
  showRoleForm.value = true
}

function toggleRoleMenu(code: string, checked: boolean): void {
  const set = new Set(roleForm.value.menus)
  if (checked) set.add(code)
  else set.delete(code)
  roleForm.value.menus = rbac.assignableMenus.filter((m) => set.has(m.code)).map((m) => m.code)
}

function toggleCapability(code: string, checked: boolean): void {
  const set = new Set(roleForm.value.capabilities)
  if (checked) set.add(code)
  else set.delete(code)
  roleForm.value.capabilities = [...set]
}

async function saveRole(): Promise<void> {
  const f = roleForm.value
  roleFormError.value = ''
  if (roleFormMode.value === 'create' && !ROLE_CODE_RE.test(f.code.trim())) {
    roleFormError.value = '角色编码须以小写字母开头，2~32 位字母数字下划线'
    return
  }
  if (!f.name.trim()) {
    roleFormError.value = '请填写角色名称'
    return
  }
  roleSaving.value = true
  try {
    if (roleFormMode.value === 'create') {
      await rbac.createRole({
        code: f.code.trim(),
        name: f.name.trim(),
        remark: f.remark.trim() || null,
        enabled: f.enabled,
        menus: f.menus,
        capabilities: f.capabilities,
      })
      ElMessage.success('角色已创建')
    } else if (editingRole.value) {
      const locked = isAdminRole(editingRole.value)
      if (!locked) {
        await rbac.updateRole(editingRole.value.id, {
          name: f.name.trim(),
          remark: f.remark.trim() || null,
          enabled: f.enabled,
        })
        await rbac.setRoleMenus(editingRole.value.id, f.menus)
        await rbac.setRoleCapabilities(editingRole.value.id, f.capabilities)
      }
      ElMessage.success('角色已更新')
    }
    showRoleForm.value = false
  } catch (e: unknown) {
    roleFormError.value = errText(e, '保存失败')
  } finally {
    roleSaving.value = false
  }
}

async function removeRole(role: RoleOut): Promise<void> {
  if (!(await confirmAction(
    `确认删除角色「${role.name}」？\n\n仍有用户使用时无法删除。`,
    '确认删除',
  ))) return
  try {
    await rbac.deleteRole(role.id)
    ElMessage.success('角色已删除')
  } catch (e: unknown) {
    ElMessage.error(errText(e, '删除失败'))
  }
}
</script>

<template>
  <div class="perm-page">
    <div class="page-header">
      <div class="h1">用户权限</div>
      <p class="muted" style="font-size: 13px; margin-top: 4px">
        为角色勾选菜单，把用户加入角色；把节点划给用户后，对方可自建分组与策略。超级管理员固定拥有全部菜单与全部数据。
      </p>
    </div>

    <div class="card card-pad">
      <div class="tabs">
        <button class="tab" :class="{ active: tab === 'users' }" @click="tab = 'users'">
          用户
          <span class="pill">{{ rbac.users.length }}</span>
        </button>
        <button class="tab" :class="{ active: tab === 'roles' }" @click="tab = 'roles'">
          角色
          <span class="pill">{{ rbac.roles.length }}</span>
        </button>
      </div>

      <template v-if="tab === 'users'">
        <div class="row between perm-toolbar">
          <input
            v-model="userQuery"
            type="search"
            placeholder="搜索用户名 / 显示名 / 手机号 / 角色…"
            aria-label="搜索用户"
            style="width: 28%"
          />
          <div class="row" style="gap: 8px">
            <button class="btn-sm btn-ghost" :disabled="loading" @click="loadAll">
              {{ loading ? '刷新中…' : '刷新' }}
            </button>
            <button class="btn-primary" @click="openCreateUser">+ 新建用户</button>
          </div>
        </div>

        <div class="list-cards mobile-only">
          <article v-for="u in filteredUsers" :key="u.id" class="perm-card">
            <div class="perm-card-summary">
              <header class="perm-card-head">
                <span class="perm-card-avatar" :class="{ inactive: !u.is_active }" aria-hidden="true">
                  {{ Array.from(u.display_name || u.username)[0]?.toUpperCase() }}
                </span>
                <div class="perm-card-identity">
                  <strong class="perm-card-name" :title="u.display_name || u.username">{{ u.display_name || u.username }}</strong>
                  <span v-if="u.display_name && u.display_name !== u.username" class="perm-card-handle" :title="u.username">{{ u.username }}</span>
                </div>
                <span class="tag perm-card-status" :class="{ green: u.is_active }">{{ u.is_active ? '启用' : '已禁用' }}</span>
                <div class="perm-card-meta">
                  <span>{{ roleNames(u) }}</span>
                  <span v-if="u.is_admin" class="tag amber">全部能力</span>
                  <template v-else>
                    <span v-for="code in u.capabilities" :key="code" class="tag blue">{{ capabilityLabel(code) }}</span>
                  </template>
                </div>
              </header>

              <dl class="perm-user-counts">
                <div><dt>节点</dt><dd>{{ u.node_ids.length }}</dd></div>
                <div><dt>分组</dt><dd>{{ u.group_count }}</dd></div>
                <div><dt>策略</dt><dd>{{ u.strategy_count }}</dd></div>
              </dl>

              <div class="perm-user-security">
                <span :class="{ muted: !u.phone }">
                  <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="6" y="2.5" width="12" height="19" rx="2"/><path d="M11 18h2"/></svg>
                  {{ u.phone || '手机未绑定' }}
                  <span v-if="u.phone && !u.phone_verified" class="tag amber">未验证</span>
                </span>
                <span :class="u.totp_enabled ? 'perm-user-verified' : 'muted'">
                  <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 22s8-4 8-11V6l-8-4-8 4v5c0 7 8 11 8 11Z"/><path v-if="u.totp_enabled" d="m9 12 2 2 4-4"/></svg>
                  2FA {{ u.totp_enabled ? '已开启' : '未绑定' }}
                </span>
              </div>
            </div>

            <div class="perm-card-actions">
              <button type="button" class="btn-ghost" @click="openEditUser(u)">
                <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M11 4H5a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2h13a2 2 0 0 0 2-2v-6M8 16l1-4L19 2l3 3L12 15l-4 1Z"/></svg>
                编辑
              </button>
              <button type="button" class="btn-ghost" @click="openNodes(u)">
                <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="2" y="3" width="20" height="7" rx="2"/><rect x="2" y="14" width="20" height="7" rx="2"/></svg>
                分配节点
              </button>
              <ElDropdown
                trigger="click"
                placement="bottom-end"
                popper-class="perm-user-menu"
                :popper-options="{ modifiers: [{ name: 'flip', options: { fallbackPlacements: ['top-end'] } }] }"
                :persistent="false"
                :show-arrow="false"
              >
                <button type="button" class="btn-ghost perm-user-more" :aria-label="`${u.username} 的更多操作`">
                  <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="4" cy="12" r="2.3"/><circle cx="12" cy="12" r="2.3"/><circle cx="20" cy="12" r="2.3"/></svg>
                </button>
                <template #dropdown>
                  <ElDropdownMenu>
                    <ElDropdownItem @click="openRoles(u)">分配角色</ElDropdownItem>
                    <ElDropdownItem @click="openPwd(u)">重置密码</ElDropdownItem>
                    <ElDropdownItem v-if="u.totp_enabled" @click="reset2fa(u)">重置 2FA</ElDropdownItem>
                    <ElDropdownItem :disabled="isSelf(u)" @click="toggleActive(u)">{{ u.is_active ? '禁用' : '启用' }}</ElDropdownItem>
                    <ElDropdownItem class="perm-user-delete" :disabled="isSelf(u)" @click="removeUser(u)">删除</ElDropdownItem>
                  </ElDropdownMenu>
                </template>
              </ElDropdown>
            </div>
          </article>
          <div v-if="!filteredUsers.length" class="muted" style="padding: 12px 0">
            {{ loading ? '加载中…' : '没有匹配的用户' }}
          </div>
        </div>

        <div class="desktop-only perm-table-wrap">
          <table>
            <thead>
              <tr>
                <th>用户</th>
                <th>手机号</th>
                <th>角色</th>
                <th>状态</th>
                <th>2FA</th>
                <th>能力</th>
                <th>节点</th>
                <th>分组 / 策略</th>
                <th>创建时间</th>
                <th class="right">操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="u in filteredUsers" :key="u.id">
                <td>
                  <div class="perm-user-cell">
                    <strong>{{ u.display_name || u.username }}</strong>
                    <span v-if="u.display_name" class="muted">{{ u.username }}</span>
                    <span v-if="u.is_admin" class="tag amber">超管</span>
                  </div>
                </td>
                <td>
                  <span v-if="u.phone">{{ u.phone }}</span>
                  <span v-else class="muted">未绑定</span>
                  <span v-if="u.phone && !u.phone_verified" class="tag amber">未验证</span>
                </td>
                <td>{{ roleNames(u) }}</td>
                <td><span class="tag" :class="u.is_active ? 'green' : ''">{{ u.is_active ? '启用' : '禁用' }}</span></td>
                <td>{{ u.totp_enabled ? '已绑定' : '未绑定' }}</td>
                <td class="perm-menus">
                  <span v-if="u.is_admin" class="tag amber">全部能力</span>
                  <span v-else-if="!u.capabilities?.length" class="muted">—</span>
                  <span v-else class="cap-tags">
                    <span v-for="code in u.capabilities" :key="code" class="tag blue">
                      {{ capabilityLabel(code) }}
                    </span>
                  </span>
                </td>
                <td>{{ u.node_ids.length }}</td>
                <td>{{ u.group_count }} / {{ u.strategy_count }}</td>
                <td class="muted">{{ fmtTime(u.created_at) }}</td>
                <td class="right">
                  <div class="perm-actions">
                    <button class="btn-sm btn-ghost" @click="openEditUser(u)">编辑</button>
                    <button class="btn-sm btn-ghost" @click="openRoles(u)">角色</button>
                    <button class="btn-sm btn-ghost" @click="openNodes(u)">分配节点</button>
                    <button class="btn-sm btn-ghost" @click="openPwd(u)">重置密码</button>
                    <button v-if="u.totp_enabled" class="btn-sm btn-ghost" @click="reset2fa(u)">重置 2FA</button>
                    <button class="btn-sm btn-ghost" :disabled="isSelf(u)" @click="toggleActive(u)">
                      {{ u.is_active ? '禁用' : '启用' }}
                    </button>
                    <button class="btn-sm btn-danger" :disabled="isSelf(u)" @click="removeUser(u)">删除</button>
                  </div>
                </td>
              </tr>
              <tr v-if="!filteredUsers.length">
                <td colspan="10" class="muted" style="padding: 18px">
                  {{ loading ? '加载中…' : '没有匹配的用户' }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </template>

      <template v-else>
        <div class="row between perm-toolbar">
          <input
            v-model="roleQuery"
            type="search"
            placeholder="搜索角色编码 / 名称 / 菜单…"
            aria-label="搜索角色"
            style="width: 28%"
          />
          <div class="row" style="gap: 8px">
            <button class="btn-sm btn-ghost" :disabled="loading" @click="loadAll">
              {{ loading ? '刷新中…' : '刷新' }}
            </button>
            <button class="btn-primary" @click="openCreateRole">+ 新建角色</button>
          </div>
        </div>

        <div class="list-cards mobile-only">
          <article v-for="r in filteredRoles" :key="r.id" class="perm-card perm-role-card">
            <div class="perm-card-summary">
              <header class="perm-card-head">
                <span class="perm-card-avatar perm-role-avatar" :class="{ inactive: !r.enabled }" aria-hidden="true">
                  <svg viewBox="0 0 24 24"><path d="M12 22s8-4 8-11V6l-8-4-8 4v5c0 7 8 11 8 11Z"/><path d="m9 12 2 2 4-4"/></svg>
                </span>
                <div class="perm-card-identity">
                  <strong class="perm-card-name" :title="r.name">{{ r.name }}</strong>
                  <span class="perm-card-handle" :title="r.code">{{ r.code }}</span>
                </div>
                <span class="tag perm-card-status" :class="{ green: r.enabled }">{{ r.enabled ? '启用' : '已停用' }}</span>
                <div class="perm-card-meta">
                  <span class="tag" :class="{ blue: r.is_builtin }">{{ r.is_builtin ? '内置' : '自定义' }}</span>
                  <span>{{ r.user_count }} 位用户</span>
                </div>
              </header>

              <div v-if="isAdminRole(r) || !r.menus.length" class="perm-role-menus perm-role-menu-row">
                <span>菜单权限</span>
                <strong>{{ isAdminRole(r) ? '全部菜单' : '未分配' }}</strong>
              </div>
              <details v-else class="perm-role-menus">
                <summary class="perm-role-menu-row">
                  <span>菜单权限</span>
                  <strong>{{ r.menus.length }} 项</strong>
                </summary>
                <div class="cap-tags perm-role-menu-tags">
                  <span v-for="code in r.menus" :key="code" class="tag blue">{{ rbac.menuName(code) }}</span>
                </div>
              </details>
            </div>
            <div class="perm-card-actions perm-role-actions">
              <button type="button" class="btn-ghost" @click="openEditRole(r)">{{ isAdminRole(r) ? '查看' : '编辑' }}</button>
              <button v-if="!r.is_builtin" type="button" class="btn-ghost perm-role-remove" @click="removeRole(r)">删除</button>
            </div>
          </article>
          <div v-if="!filteredRoles.length" class="muted" style="padding: 12px 0">
            {{ loading ? '加载中…' : '没有匹配的角色' }}
          </div>
        </div>

        <div class="desktop-only perm-table-wrap">
          <table>
            <thead>
              <tr>
                <th>角色</th>
                <th>编码</th>
                <th>类型</th>
                <th>菜单</th>
                <th>能力</th>
                <th>用户数</th>
                <th>状态</th>
                <th class="right">操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="r in filteredRoles" :key="r.id">
                <td>
                  <strong>{{ r.name }}</strong>
                  <div v-if="r.remark" class="muted" style="font-size: 12px; margin-top: 2px">{{ r.remark }}</div>
                </td>
                <td>{{ r.code }}</td>
                <td><span class="tag" :class="r.is_builtin ? 'blue' : ''">{{ r.is_builtin ? '内置' : '自定义' }}</span></td>
                <td class="perm-menus">{{ isAdminRole(r) ? '全部菜单' : menuNames(r.menus) }}</td>
                <td class="perm-menus">
                  <span v-if="isAdminRole(r)">全部</span>
                  <span v-else-if="!r.capabilities?.length">—</span>
                  <span v-else>{{ capabilityNames(r.capabilities) }}</span>
                </td>
                <td>{{ r.user_count }}</td>
                <td><span class="tag" :class="r.enabled ? 'green' : ''">{{ r.enabled ? '启用' : '停用' }}</span></td>
                <td class="right">
                  <div class="perm-actions">
                    <button class="btn-sm btn-ghost" @click="openEditRole(r)">{{ isAdminRole(r) ? '查看' : '编辑' }}</button>
                    <button class="btn-sm btn-danger" :disabled="r.is_builtin" @click="removeRole(r)">删除</button>
                  </div>
                </td>
              </tr>
              <tr v-if="!filteredRoles.length">
                <td colspan="7" class="muted" style="padding: 18px">
                  {{ loading ? '加载中…' : '没有匹配的角色' }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </template>
    </div>

    <div v-if="showUserForm" class="modal-mask" @click.self="showUserForm = false">
      <div class="card card-pad modal">
        <div class="modal-header">
          <div class="h1">{{ userFormMode === 'create' ? '新建用户' : '编辑用户' }}</div>
          <p class="muted" style="font-size: 12px; margin: 4px 0 0">
            {{ userFormMode === 'create' ? '未指定角色时会绑定内置「普通用户」。' : '改角色请用「角色」按钮；禁用会立即踢掉全部会话。' }}
          </p>
        </div>
        <div class="modal-body">
          <div class="form-grid">
            <div>
              <FormLabel field-id="perm-username" text="用户名" help="3~32 位字母数字及 _ . -，创建后不可改。" />
              <input
                id="perm-username"
                v-model="userForm.username"
                :disabled="userFormMode === 'edit'"
                autocomplete="off"
                placeholder="例如 alice"
              />
            </div>
            <div v-if="userFormMode === 'create'">
              <FormLabel field-id="perm-password" text="初始密码" help="至少 6 位；用户可在配置页自行修改。" />
              <input id="perm-password" v-model="userForm.password" type="password" autocomplete="new-password" />
            </div>
            <div>
              <FormLabel field-id="perm-display" text="显示名" help="顶栏展示用，可空。" />
              <input id="perm-display" v-model="userForm.display_name" maxlength="64" placeholder="可选" />
            </div>
            <div v-if="userFormMode === 'edit'">
              <FormLabel
                field-id="perm-phone"
                text="手机号"
                help="11 位中国大陆号码。绑定后该用户可用验证码登录；手工绑定标记为未验证。"
              />
              <input
                id="perm-phone"
                v-model="phoneInput"
                inputmode="numeric"
                maxlength="11"
                :disabled="phoneUnbind"
                :placeholder="editingUser?.phone || '未绑定'"
              />
              <label class="row muted" style="gap: 6px; font-size: 12px; cursor: pointer; margin-top: 6px">
                <input v-model="phoneUnbind" type="checkbox" @change="phoneInput = ''" />
                解绑该用户的手机号
              </label>
            </div>
            <div>
              <FormLabel field-id="perm-active" text="状态" help="禁用后无法登录，已登录会话立即失效。" />
              <TagSelect
                id="perm-active"
                v-model="userForm.is_active"
                :options="USER_ACTIVE_TAGS"
                :disabled="editingUser != null && isSelf(editingUser)"
                aria-label="启用状态"
              />
            </div>
            <div v-if="userFormMode === 'create'" class="span-full">
              <FormLabel text="角色" help="可多选。不选则绑定内置普通用户角色。" />
              <div class="check-list">
                <label v-for="r in enabledRoles" :key="r.id" class="check-item">
                  <input
                    type="checkbox"
                    :checked="userForm.role_ids.includes(r.id)"
                    @change="toggleUserRole(r.id, ($event.target as HTMLInputElement).checked)"
                  />
                  <span>{{ r.name }}</span>
                  <span class="muted">{{ r.code }}</span>
                </label>
              </div>
            </div>
            <div v-if="userFormError" class="span-full perm-error">{{ userFormError }}</div>
          </div>
        </div>
        <div class="modal-footer">
          <button class="btn-ghost" @click="showUserForm = false">取消</button>
          <button class="btn-primary" :disabled="userSaving" @click="saveUser">
            {{ userSaving ? '保存中…' : '保存' }}
          </button>
        </div>
      </div>
    </div>

    <div v-if="showRoles && roleTarget" class="modal-mask" @click.self="showRoles = false">
      <div class="card card-pad modal">
        <div class="modal-header">
          <div class="h1">分配角色 · {{ roleTarget.username }}</div>
          <p class="muted" style="font-size: 12px; margin: 4px 0 0">
            整体替换该用户的角色。不能移除自己的超级管理员，也不能让系统里一个超管都不剩。
          </p>
        </div>
        <div class="modal-body">
          <div class="check-list">
            <label v-for="r in rbac.roles" :key="r.id" class="check-item" :class="{ dim: !r.enabled }">
              <input
                type="checkbox"
                :checked="selectedRoleIds.includes(r.id)"
                :disabled="!r.enabled && !selectedRoleIds.includes(r.id)"
                @change="toggleSelectedRole(r.id, ($event.target as HTMLInputElement).checked)"
              />
              <span>{{ r.name }}</span>
              <span class="muted">{{ r.code }}{{ r.enabled ? '' : '（已停用）' }}</span>
            </label>
          </div>
          <p v-if="rolesError" class="perm-error">{{ rolesError }}</p>
        </div>
        <div class="modal-footer">
          <button class="btn-ghost" @click="showRoles = false">取消</button>
          <button class="btn-primary" :disabled="rolesSaving" @click="saveRoles">
            {{ rolesSaving ? '保存中…' : '保存' }}
          </button>
        </div>
      </div>
    </div>

    <div v-if="showPwd && pwdTarget" class="modal-mask" @click.self="showPwd = false">
      <div class="card card-pad modal">
        <div class="modal-header">
          <div class="h1">重置密码 · {{ pwdTarget.username }}</div>
          <p class="muted" style="font-size: 12px; margin: 4px 0 0">至少 6 位。重置后该用户全部旧会话立即失效。</p>
        </div>
        <div class="modal-body">
          <div class="form-grid">
            <div>
              <FormLabel field-id="perm-new-pwd" text="新密码" help="至少 6 位。" />
              <input id="perm-new-pwd" v-model="newPassword" type="password" autocomplete="new-password" />
            </div>
            <div>
              <FormLabel field-id="perm-confirm-pwd" text="确认密码" help="再输入一遍，防止看错。" />
              <input id="perm-confirm-pwd" v-model="confirmPassword" type="password" autocomplete="new-password" />
            </div>
            <div v-if="pwdError" class="perm-error">{{ pwdError }}</div>
          </div>
        </div>
        <div class="modal-footer">
          <button class="btn-ghost" @click="showPwd = false">取消</button>
          <button class="btn-primary" :disabled="pwdSaving" @click="savePwd">
            {{ pwdSaving ? '重置中…' : '确认重置' }}
          </button>
        </div>
      </div>
    </div>

    <div v-if="showNodes && nodeTarget" class="modal-mask" @click.self="showNodes = false">
      <div class="card card-pad modal modal-lg">
        <div class="modal-header">
          <div class="h1">
            分配节点 · {{ nodeTarget.is_admin ? '自分配' : nodeTarget.username }}
          </div>
          <p class="muted" style="font-size: 12px; margin: 4px 0 0">
            勾上的节点划给该账号，去掉的节点回收到所选管理员名下。有在跑任务时不能变更；移出分组或解除跟单须二次确认。
          </p>
        </div>
        <div class="modal-body">
          <label v-if="nodeTarget.node_ids.some(id => !selectedNodeIds.includes(id))">回收到管理员
            <select v-model="reclaimAdminId" class="pf-input">
              <option v-for="u in rbac.activeAdmins.filter(u => u.id !== nodeTarget?.id)" :key="u.id" :value="u.id">{{ u.username }}</option>
            </select>
            <span v-if="!reclaimAdminId">无其他管理员，请在节点授权中选择接收用户。</span>
          </label>
          <div class="row perm-node-tools">
            <input
              v-model="nodeQuery"
              type="search"
              placeholder="搜索节点名称 / ID / MT5…"
              aria-label="搜索可分配节点"
              style="flex: 1"
            />
            <TagSelect v-model="nodeFilter" :options="NODE_FILTER_TAGS" aria-label="节点筛选" />
          </div>
          <div class="row between" style="margin: 8px 0 10px">
            <label class="row muted" style="gap: 6px; font-size: 12px; cursor: pointer">
              <input
                type="checkbox"
                :checked="filteredAssignNodes.length > 0 && filteredAssignNodes.every((n) => isNodeSelected(n.node_id))"
                @change="selectVisibleNodes(($event.target as HTMLInputElement).checked)"
              />
              全选当前列表
            </label>
            <span class="muted" style="font-size: 12px">已选 {{ selectedNodeCount }} 个</span>
          </div>
          <div class="check-list check-list-tall">
            <label
              v-for="n in filteredAssignNodes"
              :key="n.node_id"
              class="check-item"
              :class="{ selected: isNodeSelected(n.node_id) }"
            >
              <input
                type="checkbox"
                :checked="isNodeSelected(n.node_id)"
                @change="toggleNode(n.node_id, ($event.target as HTMLInputElement).checked)"
              />
              <span class="perm-node-name">{{ n.name }}</span>
              <span class="muted">{{ n.mt5_login || n.node_id }}</span>
              <span class="tag" :class="n.owner_user_id == null ? '' : (n.owner_user_id === nodeTarget.id ? 'green' : 'amber')">
                {{ nodeOwnerLabel(n) }}
              </span>
            </label>
            <p v-if="!filteredAssignNodes.length" class="muted" style="padding: 8px 0">没有匹配的节点</p>
          </div>
          <p v-if="nodeHint" class="perm-hint">{{ nodeHint }}</p>
          <pre v-if="pendingMemberships" class="token-box perm-conflict">{{ formatMemberships(pendingMemberships) }}</pre>
          <p v-if="nodeError" class="perm-error">{{ nodeError }}</p>
        </div>
        <div class="modal-footer">
          <button class="btn-ghost" @click="showNodes = false">取消</button>
          <button
            v-if="pendingMemberships"
            class="btn-primary"
            :disabled="nodeSaving"
            @click="saveNodes(true)"
          >
            {{ nodeSaving ? '处理中…' : '确认并移出分组' }}
          </button>
          <button v-else class="btn-primary" :disabled="nodeSaving" @click="saveNodes(false)">
            {{ nodeSaving ? '保存中…' : '保存' }}
          </button>
        </div>
      </div>
    </div>

    <div v-if="showRoleForm" class="modal-mask" @click.self="showRoleForm = false">
      <div class="card card-pad modal modal-lg">
        <div class="modal-header">
          <div class="h1">
            {{ roleFormMode === 'create' ? '新建角色' : (editingRole && isAdminRole(editingRole) ? '查看角色' : '编辑角色') }}
          </div>
          <p class="muted" style="font-size: 12px; margin: 4px 0 0">
            菜单是代码注册表，这里只能勾选。中控台、客户端版本、事件、用户权限仅超级管理员可见，不能分配。
          </p>
        </div>
        <div class="modal-body">
          <div class="form-grid two">
            <div>
              <FormLabel field-id="perm-role-code" text="编码" help="小写字母开头，2~32 位字母数字下划线；创建后不可改。" />
              <input
                id="perm-role-code"
                v-model="roleForm.code"
                :disabled="roleFormMode === 'edit'"
                placeholder="例如 viewer"
              />
            </div>
            <div>
              <FormLabel field-id="perm-role-name" text="名称" help="列表与分配弹窗里显示的名字。" />
              <input
                id="perm-role-name"
                v-model="roleForm.name"
                maxlength="64"
                :disabled="editingRole != null && isAdminRole(editingRole)"
                placeholder="例如 只读观察"
              />
            </div>
            <div class="span-full">
              <FormLabel field-id="perm-role-remark" text="备注" help="可选，方便区分用途。" />
              <input
                id="perm-role-remark"
                v-model="roleForm.remark"
                maxlength="255"
                :disabled="editingRole != null && isAdminRole(editingRole)"
              />
            </div>
            <div>
              <FormLabel field-id="perm-role-enabled" text="状态" help="内置角色不可停用。停用后已持有该角色的用户会立刻丢掉对应菜单。" />
              <TagSelect
                id="perm-role-enabled"
                v-model="roleForm.enabled"
                :options="ROLE_ENABLED_TAGS"
                :disabled="editingRole?.is_builtin"
                aria-label="角色状态"
              />
            </div>
            <div class="span-full">
              <FormLabel text="菜单" help="只出现可分配菜单。超级管理员角色固定拥有全部菜单。" />
              <div class="check-list">
                <label
                  v-for="m in rbac.assignableMenus"
                  :key="m.code"
                  class="check-item"
                  :class="{ selected: roleForm.menus.includes(m.code) }"
                >
                  <input
                    type="checkbox"
                    :checked="roleForm.menus.includes(m.code)"
                    :disabled="editingRole != null && isAdminRole(editingRole)"
                    @change="toggleRoleMenu(m.code, ($event.target as HTMLInputElement).checked)"
                  />
                  <span>{{ m.name }}</span>
                  <span class="muted">{{ m.path }}</span>
                </label>
              </div>
              <p v-if="editingRole && isAdminRole(editingRole)" class="muted" style="font-size: 12px; margin-top: 8px">
                超级管理员角色固定，不可改名称、菜单或停用。
              </p>
            </div>

            <!-- 能力：细粒度功能授权 -->
            <div class="span-full">
              <FormLabel text="能力" help="细粒度功能授权。管理员角色固定拥有全部能力，无需单独设置。" />
              <div class="check-list">
                <label
                  v-for="cap in CAPABILITIES"
                  :key="cap.code"
                  class="check-item"
                  :class="{ selected: roleForm.capabilities.includes(cap.code) }"
                >
                  <input
                    type="checkbox"
                    :checked="roleForm.capabilities.includes(cap.code)"
                    :disabled="Boolean(editingRole && isAdminRole(editingRole))"
                    @change="toggleCapability(cap.code, ($event.target as HTMLInputElement).checked)"
                  />
                  <span>{{ cap.label }}</span>
                  <span class="muted">{{ cap.description }}</span>
                </label>
              </div>
              <p v-if="editingRole && isAdminRole(editingRole)" class="muted" style="font-size: 12px; margin-top: 8px">
                管理员角色固定拥有全部能力，此处不可修改。
              </p>
            </div>
            <div v-if="roleFormError" class="span-full perm-error">{{ roleFormError }}</div>
          </div>
        </div>
        <div class="modal-footer">
          <button class="btn-ghost" @click="showRoleForm = false">
            {{ editingRole && isAdminRole(editingRole) ? '关闭' : '取消' }}
          </button>
          <button
            v-if="!(editingRole && isAdminRole(editingRole))"
            class="btn-primary"
            :disabled="roleSaving"
            @click="saveRole"
          >
            {{ roleSaving ? '保存中…' : '保存' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.perm-toolbar {
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 14px;
}
.perm-table-wrap { overflow-x: auto; }
.perm-user-cell {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.perm-user-cell strong { font-family: var(--font); }
.perm-menus {
  max-width: 360px;
  white-space: normal;
  line-height: 1.45;
}
.perm-actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 6px;
}
.perm-card {
  min-width: 0;
  overflow: hidden;
  border: 1px solid var(--glass-border);
  border-radius: 16px;
  background: linear-gradient(155deg, #151c2d, #101726);
}
.perm-card-summary { padding: 14px; }
.perm-card-head {
  display: grid;
  grid-template-columns: 38px minmax(0, 1fr) auto;
  align-items: start;
  gap: 4px 10px;
}
.perm-card-avatar {
  display: grid;
  place-items: center;
  grid-row: span 2;
  height: 38px;
  border: 1px solid var(--glass-highlight);
  border-radius: 11px;
  background: linear-gradient(135deg, var(--primary), #4f46e5);
  color: #fff;
  font-size: 17px;
  font-weight: 700;
}
.perm-card-avatar.inactive { background: #273344; color: #a1afc3; }
.perm-card-identity { min-width: 0; }
.perm-card-name, .perm-card-handle {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.perm-card-name { font-size: 16px; line-height: 1.3; }
.perm-card-handle { color: var(--muted); font-size: 12px; }
.perm-card-meta {
  display: flex;
  grid-column: 2 / -1;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 6px;
  color: #9eacc2;
  font-size: 12px;
  overflow-wrap: anywhere;
}
.perm-card-meta .tag { padding: 1px 6px; white-space: normal; word-break: normal; }
.perm-card-status { border-radius: 999px; padding: 3px 7px; }
.perm-card-status::before {
  content: '';
  display: inline-block;
  width: 6px;
  height: 6px;
  margin-right: 5px;
  border-radius: 50%;
  background: currentColor;
  vertical-align: 1px;
}
.perm-user-counts {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  margin: 12px 0 0;
  padding: 9px 0;
  border: 1px solid var(--glass-border);
  border-radius: 11px;
  background: var(--glass);
  text-align: center;
}
.perm-user-counts div { display: flex; flex-direction: column-reverse; gap: 2px; }
.perm-user-counts div + div { border-left: 1px solid var(--glass-border); }
.perm-user-counts dd { margin: 0; font-size: 18px; font-weight: 700; line-height: 1.2; }
.perm-user-counts dt { color: var(--muted); font-size: 11px; }
.perm-user-security {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 6px 10px;
  margin-top: 10px;
  font-size: 12px;
}
.perm-user-security > span { display: inline-flex; align-items: center; gap: 5px; }
.perm-user-security svg, .perm-card-actions svg {
  flex: none;
  width: 15px;
  height: 15px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.6;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.perm-user-verified { color: #34d399; }
.perm-card-actions {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr)) 44px;
  gap: 8px;
  padding: 10px 14px;
  border-top: 1px solid var(--glass-border);
  background: rgba(6, 10, 18, 0.22);
}
.perm-card-actions button {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 5px;
  width: 100%;
  min-height: 44px;
  padding: 5px;
  font-size: 13px;
  font-weight: 650;
  white-space: nowrap;
}
.perm-card-actions svg { color: #97a6bb; }
.perm-card-actions .perm-user-more svg { width: 24px; height: 24px; fill: currentColor; stroke: none; }
.perm-card-actions button:focus-visible, .perm-role-menu-row:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.perm-role-avatar svg {
  width: 19px;
  height: 19px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.7;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.perm-role-menus {
  margin-top: 12px;
  border: 1px solid var(--glass-border);
  border-radius: 11px;
  background: var(--glass);
  color: #9eacc2;
  font-size: 12px;
}
.perm-role-menu-row { display: flex; align-items: center; gap: 8px; min-height: 42px; padding: 6px 10px; }
.perm-role-menu-row strong { margin-left: auto; color: var(--text); }
summary.perm-role-menu-row { border-radius: 10px; cursor: pointer; list-style: none; }
summary.perm-role-menu-row::-webkit-details-marker { display: none; }
summary.perm-role-menu-row::after {
  content: '';
  width: 6px;
  height: 6px;
  margin: -3px 2px 0 0;
  border-right: 1px solid currentColor;
  border-bottom: 1px solid currentColor;
  transform: rotate(45deg);
}
.perm-role-menus[open] summary::after { transform: translateY(3px) rotate(225deg); }
.perm-role-menu-tags { padding: 0 10px 10px; }
.perm-role-menu-tags .tag { white-space: normal; word-break: normal; overflow-wrap: anywhere; }
.perm-role-actions { display: flex; }
.perm-role-actions .perm-role-remove { flex: 0 0 80px; color: #f87171; }
:global(.perm-user-menu.el-popper) {
  --el-bg-color-overlay: #151e2e;
  --el-border-color-light: var(--glass-highlight);
  --el-text-color-regular: #d5ddeb;
  --el-text-color-disabled: #7b8ba8;
  --el-dropdown-menuItem-hover-fill: #203250;
  --el-dropdown-menuItem-hover-color: var(--text);
  --el-dropdown-menu-box-shadow: var(--shadow);
  min-width: 144px;
}
:global(.perm-user-menu .el-dropdown-menu__item) { min-height: 44px; }
:global(.perm-user-menu .perm-user-delete:not(.is-disabled)) { color: #f87171; }
.check-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.check-list-tall {
  max-height: min(420px, 50vh);
  overflow-y: auto;
  padding-right: 2px;
}
.check-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border: 1px solid var(--glass-border);
  border-radius: var(--radius-sm);
  cursor: pointer;
}
.check-item:hover { background: var(--glass); }
.check-item.selected { border-color: var(--border-bright); }
.check-item.dim { opacity: 0.65; }
.check-item .muted { font-size: 12px; }
.perm-node-tools { gap: 8px; flex-wrap: wrap; }
.perm-node-name { font-weight: 600; }
.perm-error { color: var(--red); font-size: 13px; margin: 8px 0 0; }
.perm-hint { color: var(--amber); font-size: 13px; margin: 10px 0 6px; }
.perm-conflict {
  white-space: pre-wrap;
  font-size: 12px;
  margin: 0 0 8px;
}
@media (max-width: 768px) {
  .perm-toolbar input { width: 100% !important; }
  .perm-toolbar .row,
  .perm-toolbar button { width: 100%; }
  .modal-footer .btn-ghost,
  .modal-footer .btn-primary {
    flex: 1;
    min-height: 44px;
  }
}

/* 能力标签：用户列表「能力」列里的多个 tag 同行换行 */
.cap-tags {
  display: inline-flex;
  flex-wrap: wrap;
  gap: 4px;
}
</style>
