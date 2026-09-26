<script setup lang="ts">
// 用户权限：超级管理员在此建用户/角色、勾选菜单、把节点划给用户。
// 菜单与拦截以后端为准；本页只做配置与展示。
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import 'element-plus/es/components/message/style/css'
import FormLabel from '@/components/FormLabel.vue'
import { useAuthStore } from '@/stores/auth'
import { useHubStore } from '@/stores/hub'
import { useRbacStore } from '@/stores/rbac'
import { confirmAction } from '@/utils/confirm'
import type {
  NodeAssignConflict,
  NodeOut,
  RoleOut,
  UserOut,
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
const nodeFilter = ref<'all' | 'unassigned' | 'mine' | 'others'>('all')
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
})
const roleFormError = ref('')
const roleSaving = ref(false)

const filteredUsers = computed(() => {
  const q = userQuery.value.trim().toLowerCase()
  if (!q) return rbac.users
  return rbac.users.filter((u) => {
    const roles = u.roles.map((r) => r.name).join(' ')
    return [u.username, u.display_name || '', roles].some((s) => s.toLowerCase().includes(q))
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
    if (nodeFilter.value === 'unassigned' && n.owner_user_id != null) return false
    if (nodeFilter.value === 'mine' && n.owner_user_id !== nodeTarget.value?.id) return false
    if (nodeFilter.value === 'others') {
      if (n.owner_user_id == null || n.owner_user_id === nodeTarget.value?.id) return false
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

function nodeOwnerLabel(n: NodeOut): string {
  if (n.owner_user_id == null) return '管理员'
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
      await rbac.updateUser(editingUser.value.id, {
        display_name: f.display_name.trim() || null,
        is_active: f.is_active,
      })
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
    const result = await rbac.assignNodes(nodeTarget.value.id, selectedNodeIds.value, confirm)
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
            placeholder="搜索用户名 / 显示名 / 角色…"
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
          <div v-for="u in filteredUsers" :key="u.id" class="list-card card">
            <div class="list-card-head row between">
              <strong>{{ u.display_name || u.username }}</strong>
              <span class="tag" :class="u.is_active ? 'green' : ''">{{ u.is_active ? '启用' : '禁用' }}</span>
            </div>
            <div class="list-field"><span class="k">用户名</span><span class="v">{{ u.username }}</span></div>
            <div class="list-field"><span class="k">角色</span><span class="v">{{ roleNames(u) }}</span></div>
            <div class="list-field"><span class="k">2FA</span><span class="v">{{ u.totp_enabled ? '已绑定' : '未绑定' }}</span></div>
            <div class="list-field"><span class="k">节点</span><span class="v">{{ u.is_admin ? '全部' : u.node_ids.length }}</span></div>
            <div class="list-field"><span class="k">分组 / 策略</span><span class="v">{{ u.group_count }} / {{ u.strategy_count }}</span></div>
            <div class="list-card-actions">
              <button class="btn-sm btn-ghost" @click="openEditUser(u)">编辑</button>
              <button class="btn-sm btn-ghost" @click="openRoles(u)">角色</button>
              <button v-if="!u.is_admin" class="btn-sm btn-ghost" @click="openNodes(u)">分配节点</button>
              <button class="btn-sm btn-ghost" @click="openPwd(u)">重置密码</button>
              <button v-if="u.totp_enabled" class="btn-sm btn-ghost" @click="reset2fa(u)">重置 2FA</button>
              <button class="btn-sm btn-ghost" :disabled="isSelf(u)" @click="toggleActive(u)">
                {{ u.is_active ? '禁用' : '启用' }}
              </button>
              <button class="btn-sm btn-danger" :disabled="isSelf(u)" @click="removeUser(u)">删除</button>
            </div>
          </div>
          <div v-if="!filteredUsers.length" class="muted" style="padding: 12px 0">
            {{ loading ? '加载中…' : '没有匹配的用户' }}
          </div>
        </div>

        <div class="desktop-only perm-table-wrap">
          <table>
            <thead>
              <tr>
                <th>用户</th>
                <th>角色</th>
                <th>状态</th>
                <th>2FA</th>
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
                <td>{{ roleNames(u) }}</td>
                <td><span class="tag" :class="u.is_active ? 'green' : ''">{{ u.is_active ? '启用' : '禁用' }}</span></td>
                <td>{{ u.totp_enabled ? '已绑定' : '未绑定' }}</td>
                <td>{{ u.is_admin ? '全部' : u.node_ids.length }}</td>
                <td>{{ u.group_count }} / {{ u.strategy_count }}</td>
                <td class="muted">{{ fmtTime(u.created_at) }}</td>
                <td class="right">
                  <div class="perm-actions">
                    <button class="btn-sm btn-ghost" @click="openEditUser(u)">编辑</button>
                    <button class="btn-sm btn-ghost" @click="openRoles(u)">角色</button>
                    <button v-if="!u.is_admin" class="btn-sm btn-ghost" @click="openNodes(u)">分配节点</button>
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
                <td colspan="8" class="muted" style="padding: 18px">
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
          <div v-for="r in filteredRoles" :key="r.id" class="list-card card">
            <div class="list-card-head row between">
              <strong>{{ r.name }}</strong>
              <span class="tag" :class="r.enabled ? 'green' : ''">{{ r.enabled ? '启用' : '停用' }}</span>
            </div>
            <div class="list-field"><span class="k">编码</span><span class="v">{{ r.code }}</span></div>
            <div class="list-field"><span class="k">类型</span><span class="v">{{ r.is_builtin ? '内置' : '自定义' }}</span></div>
            <div class="list-field"><span class="k">用户数</span><span class="v">{{ r.user_count }}</span></div>
            <div class="list-field list-field-block">
              <span class="k">菜单</span>
              <span class="v">{{ isAdminRole(r) ? '全部' : menuNames(r.menus) }}</span>
            </div>
            <div class="list-card-actions">
              <button class="btn-sm btn-ghost" @click="openEditRole(r)">{{ isAdminRole(r) ? '查看' : '编辑' }}</button>
              <button class="btn-sm btn-danger" :disabled="r.is_builtin" @click="removeRole(r)">删除</button>
            </div>
          </div>
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
            <div>
              <FormLabel field-id="perm-active" text="状态" help="禁用后无法登录，已登录会话立即失效。" />
              <select id="perm-active" v-model="userForm.is_active" :disabled="editingUser != null && isSelf(editingUser)">
                <option :value="true">启用</option>
                <option :value="false">禁用</option>
              </select>
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
          <div class="h1">分配节点 · {{ nodeTarget.username }}</div>
          <p class="muted" style="font-size: 12px; margin: 4px 0 0">
            整体替换该用户名下节点：勾上的划给他，去掉的回到管理员名下。节点上还有进行中的策略任务时不能改；仍在分组里时需确认后移出。
          </p>
        </div>
        <div class="modal-body">
          <div class="row perm-node-tools">
            <input
              v-model="nodeQuery"
              type="search"
              placeholder="搜索节点名称 / ID / MT5…"
              aria-label="搜索可分配节点"
              style="flex: 1"
            />
            <select v-model="nodeFilter" class="input-sm">
              <option value="all">全部</option>
              <option value="unassigned">管理员名下</option>
              <option value="mine">已分配给该用户</option>
              <option value="others">其他用户</option>
            </select>
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
              <select
                id="perm-role-enabled"
                v-model="roleForm.enabled"
                :disabled="editingRole?.is_builtin"
              >
                <option :value="true">启用</option>
                <option :value="false">停用</option>
              </select>
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
</style>
