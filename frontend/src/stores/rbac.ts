import { defineStore } from 'pinia'
import api from '@/api/client'
import type {
  MenuDef,
  RoleCreatePayload,
  RoleOut,
  RoleUpdatePayload,
  UserCreatePayload,
  UserNodesResult,
  UserOut,
  UserUpdatePayload,
} from '@/api/types'

interface RbacState {
  users: UserOut[]
  roles: RoleOut[]
  menus: MenuDef[]
}

// 用户权限管理（仅超级管理员）：用户、角色、菜单注册表
export const useRbacStore = defineStore('rbac', {
  state: (): RbacState => ({ users: [], roles: [], menus: [] }),
  getters: {
    /** 可分配给角色的菜单（仅管理员菜单不出现在勾选项里） */
    assignableMenus: (s): MenuDef[] => s.menus.filter((m) => m.assignable),
    menuName: (s) => (code: string): string => s.menus.find((m) => m.code === code)?.name || code,
  },
  actions: {
    async fetchUsers(): Promise<void> {
      this.users = (await api.get<UserOut[]>('/api/users')).data
    },
    async fetchRoles(): Promise<void> {
      this.roles = (await api.get<RoleOut[]>('/api/roles')).data
    },
    async fetchMenus(): Promise<void> {
      this.menus = (await api.get<MenuDef[]>('/api/menus')).data
    },
    async createUser(payload: UserCreatePayload): Promise<UserOut> {
      const { data } = await api.post<UserOut>('/api/users', payload)
      await this.fetchUsers()
      return data
    },
    async updateUser(id: number, patch: UserUpdatePayload): Promise<void> {
      await api.patch(`/api/users/${id}`, patch)
      await this.fetchUsers()
    },
    async setUserRoles(id: number, roleIds: number[]): Promise<void> {
      await api.put(`/api/users/${id}/roles`, { role_ids: roleIds })
      await Promise.all([this.fetchUsers(), this.fetchRoles()])
    },
    async resetPassword(id: number, newPassword: string): Promise<void> {
      await api.post(`/api/users/${id}/reset-password`, { new_password: newPassword })
    },
    async reset2fa(id: number): Promise<void> {
      await api.post(`/api/users/${id}/reset-2fa`)
      await this.fetchUsers()
    },
    /** 整体替换用户名下节点；409 由调用方按 detail.reason 提示并决定是否带 confirm 重发 */
    async assignNodes(id: number, nodeIds: string[], confirm = false): Promise<UserNodesResult> {
      const { data } = await api.put<UserNodesResult>(`/api/users/${id}/nodes`, {
        node_ids: nodeIds,
        confirm,
      })
      await this.fetchUsers()
      return data
    },
    async deleteUser(id: number): Promise<void> {
      await api.delete(`/api/users/${id}`)
      await this.fetchUsers()
    },
    async createRole(payload: RoleCreatePayload): Promise<RoleOut> {
      const { data } = await api.post<RoleOut>('/api/roles', payload)
      await this.fetchRoles()
      return data
    },
    async updateRole(id: number, patch: RoleUpdatePayload): Promise<void> {
      await api.patch(`/api/roles/${id}`, patch)
      await this.fetchRoles()
    },
    async setRoleMenus(id: number, menus: string[]): Promise<void> {
      await api.put(`/api/roles/${id}/menus`, { menus })
      await this.fetchRoles()
    },
    async deleteRole(id: number): Promise<void> {
      await api.delete(`/api/roles/${id}`)
      await this.fetchRoles()
    },
  },
})
