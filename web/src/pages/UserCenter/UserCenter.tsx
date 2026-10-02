import React, { useEffect, useState, useCallback } from 'react';
import {
  Users, Loader2, Plus, KeyRound, RefreshCw, Shield, ShieldCheck,
  Search, Trash2, Edit3, Lock, Unlock, UserCog, CheckCircle2,
} from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '../../components/ui/card';
import { Button } from '../../components/ui/button';
import { Badge } from '../../components/ui/badge';
import { Input } from '../../components/ui/input';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '../../components/ui/tabs';
import { cn } from '../../lib/utils';
import { toast } from 'sonner';
import * as api from '../../lib/api';
import { invalidateCapabilities } from '../../lib/capabilities';
import { useAppStore } from '../../store/useAppStore';
import { Modal, PasswordReveal, ConfirmModal, Paginator } from '../../components/ui/PageHelpers';

// ─── 常量 ────────────────────────────────────────────────
const ROLES = [
  { value: 'scenic_operator', label: '景区操作员', scenicRequired: true },
  { value: 'scenic_admin', label: '景区管理员', scenicRequired: true },
  { value: 'group_viewer', label: '集团只读', scenicRequired: false },
  { value: 'group_developer', label: '集团开发', scenicRequired: false },
  { value: 'group_admin', label: '集团管理员', scenicRequired: false },
  { value: 'super_admin', label: '超级管理员', scenicRequired: false },
];

const ROLE_LABEL_MAP: Record<string, string> = {};
ROLES.forEach((r) => { ROLE_LABEL_MAP[r.value] = r.label; });

/**
 * 成员 / 角色 / 权限。
 *
 * 独立页面时自带标题与页签;`embedded` 时只渲染内容,页签由宿主(系统设置)提供 ——
 * 三个页签共享同一份加载状态与用户列表,所以是一个组件挂在三个页签下面,
 * 而不是拆成三个组件各自去拉一遍数据。
 */
const UserCenter: React.FC<{
  embedded?: boolean;
  tab?: string;
  onTabChange?: (v: string) => void;
}> = ({ embedded = false, tab, onTabChange }) => {
  const { me } = useAppStore();
  const isSuperAdmin = me?.role === 'super_admin';
  const isGroupAdmin = me?.role === 'group_admin';
  const allowed = isSuperAdmin || isGroupAdmin;

  // ── 通用状态 ──
  const [loading, setLoading] = useState(true);
  const [innerTab, setInnerTab] = useState('members');
  const activeTab = embedded ? (tab || 'members') : innerTab;
  const setActiveTab = (v: string) => { if (embedded) onTabChange?.(v); else setInnerTab(v); };
  const [newPwdInfo, setNewPwdInfo] = useState<{ account: string; password: string } | null>(null);
  const [confirm, setConfirm] = useState<{ title: string; message: string; danger?: boolean; onConfirm: () => Promise<any> } | null>(null);
  const [confirmLoading, setConfirmLoading] = useState(false);
  const [saving, setSaving] = useState(false);

  // ── 成员管理状态 ──
  const [users, setUsers] = useState<any[]>([]);
  const [userTotal, setUserTotal] = useState(0);
  const [userPage, setUserPage] = useState(1);
  const [userPageSize, setUserPageSize] = useState(20);
  const [userTotalPages, setUserTotalPages] = useState(0);
  const [userSearch, setUserSearch] = useState('');
  const [userRoleFilter, setUserRoleFilter] = useState('');
  const [userActiveFilter, setUserActiveFilter] = useState<boolean | ''>('');
  const [userModal, setUserModal] = useState(false);
  const [editingUser, setEditingUser] = useState<any | null>(null);
  const [userForm, setUserForm] = useState({ name: '', phone: '', role: 'scenic_operator', scenic_id: '', position: '' });
  const [scenicList, setScenicList] = useState<{ scenic_id: string; scenic_name: string; scenic_grade_level?: string }[]>([]);
  const [scenicSearch, setScenicSearch] = useState('');
  const [scenicDropdown, setScenicDropdown] = useState(false);
  const [selectedScenicNameState, setSelectedScenicNameState] = useState<string | null>(null);
  const scenicSearchTimer = React.useRef<ReturnType<typeof setTimeout> | null>(null);

  // ── 角色与权限状态 ──
  const [roles, setRoles] = useState<api.RoleInfo[]>([]);
  const [permissionsData, setPermissionsData] = useState<{ policies: any[]; groupings: any[] }>({ policies: [], groupings: [] });
  const [permDomain, setPermDomain] = useState<'admin' | 'app'>('admin');  // 当前查看的权限域
  const [permModal, setPermModal] = useState(false);
  const [permForm, setPermForm] = useState({ sub: '', obj: '', act: '', domain: 'admin' });
  const [usersByRole, setUsersByRole] = useState<Record<string, any[]>>({});
  const [batchRoleModal, setBatchRoleModal] = useState(false);
  const [batchUserIds, setBatchUserIds] = useState<string[]>([]);
  const [batchRole, setBatchRole] = useState('');

  // ── 数据加载 ──
  const loadUsers = useCallback(async () => {
    try {
      const params: api.UserListParams = { page: userPage, page_size: userPageSize };
      if (userSearch) params.keyword = userSearch;
      if (userRoleFilter) params.role = userRoleFilter;
      if (userActiveFilter !== '') params.is_active = userActiveFilter as boolean;
      const r = await api.listUsersPaged(params);
      setUsers(r.items);
      setUserTotal(r.total);
      setUserTotalPages(r.total_pages);
    } catch (e: any) { toast.error(e.message || '加载成员失败'); }
  }, [userPage, userPageSize, userSearch, userRoleFilter, userActiveFilter]);

  const loadRolesAndPerms = useCallback(async () => {
    try {
      const [rolesRes, permsRes, usersByRoleRes] = await Promise.all([
        api.listRoles(true, permDomain),
        api.listPermissions(permDomain),
        isSuperAdmin ? api.listUsersByRole() : Promise.resolve({}),
      ]);
      setRoles(rolesRes);
      setPermissionsData(permsRes);
      if (isSuperAdmin) setUsersByRole(usersByRoleRes);
    } catch (e: any) { toast.error(e.message || '加载角色权限失败'); }
  }, [isSuperAdmin, permDomain]);

  const load = useCallback(async () => {
    setLoading(true);
    await Promise.all([loadUsers(), loadRolesAndPerms()]);
    setLoading(false);
  }, [loadUsers, loadRolesAndPerms]);

  useEffect(() => { if (allowed) load(); else setLoading(false); }, [allowed]);
  useEffect(() => { if (allowed) loadUsers(); }, [loadUsers]);
  useEffect(() => { if (allowed && (activeTab === 'roles' || activeTab === 'permissions')) loadRolesAndPerms(); }, [loadRolesAndPerms, activeTab]);

  // ── 确认操作封装 ──
  const doConfirm = async (fn: () => Promise<any>) => {
    setConfirmLoading(true);
    try { await fn(); setConfirm(null); load(); } catch (e: any) { toast.error(e.message || '操作失败'); }
    finally { setConfirmLoading(false); }
  };

  const wrap = async (fn: () => Promise<any>, okMsg: string, close: () => void) => {
    setSaving(true);
    try { await fn(); toast.success(okMsg); close(); load(); } catch (e: any) { toast.error(e.message || '操作失败'); }
    finally { setSaving(false); }
  };

  // ── 成员操作 ──
  const openCreateUser = async () => {
    setEditingUser(null);
    setUserForm({ name: '', phone: '', role: 'scenic_operator', scenic_id: '', position: '' });
    setScenicSearch('');
    setScenicList([]);
    setUserModal(true);
  };
  const openEditUser = async (u: any) => {
    setEditingUser(u);
    setUserForm({ name: u.name || '', phone: u.phone || '', role: u.role || 'scenic_operator', scenic_id: u.scenic_id || '', position: u.position || '' });
    setScenicSearch('');
    setScenicList([]);
    // 编辑时直接使用列表中已带出的 scenic_name
    if (u.scenic_name) setSelectedScenicNameState(u.scenic_name);
    else setSelectedScenicNameState(null);
    setUserModal(true);
  };

  // ── 景区搜索(防抖, 全部走后端) ──
  const handleScenicSearch = (keyword: string) => {
    setScenicSearch(keyword);
    if (scenicSearchTimer.current) clearTimeout(scenicSearchTimer.current);
    if (!keyword.trim()) {
      setScenicList([]);
      setScenicDropdown(false);
      return;
    }
    scenicSearchTimer.current = setTimeout(async () => {
      try {
        const r = await api.getScenics({ keyword, is_enabled: true, page_size: 50 });
        setScenicList(r.items || []);
        setScenicDropdown(true);
      } catch { setScenicList([]); }
    }, 300);
  };

  // 选中景区时: 关注 → setUserForm
  const handleSelectScenic = (scenic: { scenic_id: string; scenic_name: string }) => {
    setUserForm(prev => ({ ...prev, scenic_id: scenic.scenic_id }));
    setSelectedScenicNameState(scenic.scenic_name);
    setScenicSearch('');
    setScenicDropdown(false);
  };

  // 清除已选景区
  const handleClearScenic = () => {
    setUserForm(prev => ({ ...prev, scenic_id: '' }));
    setSelectedScenicNameState(null);
    setScenicSearch('');
    setScenicList([]);
  };

  const handleSaveUser = async () => {
    setSaving(true);
    try {
      if (editingUser) {
        await api.updateUser(editingUser.id, {
          name: userForm.name, scenic_id: userForm.scenic_id || null,
          role: userForm.role, position: userForm.position || null,
        });
        // 改的可能就是自己的角色/归属景区。后端已当场生效,前端这份能力缓存
        // 不作废的话界面还按旧角色显示按钮 —— 表现是「该能点的点不了」,没人会想到是缓存
        invalidateCapabilities();
        toast.success('成员信息已更新');
      } else {
        const r = await api.createUser({ name: userForm.name, phone: userForm.phone, scenic_id: userForm.scenic_id || null, role: userForm.role, position: userForm.position || null });
        setNewPwdInfo({ account: r.account, password: r.initial_password });
      }
      setUserModal(false);
      load();
    } catch (e: any) { toast.error(e.message || '保存失败'); }
    finally { setSaving(false); }
  };

  const handleResetPassword = (u: any) => {
    api.resetPassword(u.id)
      .then((r) => { setNewPwdInfo({ account: u.account, password: r.new_password }); load(); })
      .catch((e: any) => toast.error(e.message || '重置失败'));
  };

  const handleDeleteUser = (u: any) => {
    if (u.id === me?.id) { toast.error('不能删除自己'); return; }
    setConfirm({ title: '删除成员', message: `确定要删除「${u.name || u.account}」吗？此操作不可恢复。`, danger: true, onConfirm: () => doConfirm(() => api.deleteUser(u.id)) });
  };

  // ── 权限操作 ──
  const handleAddPermission = () => wrap(
    () => api.createPermission(permForm.sub, permForm.obj, permForm.act, permForm.domain),
    '策略已添加', () => { setPermModal(false); setPermForm({ sub: '', obj: '', act: '', domain: permDomain }); }
  );

  const handleDeletePermission = (sub: string, obj: string, act: string, domain: string) => {
    setConfirm({ title: '删除策略', message: `确定要删除策略[${domain}]「${sub} → ${obj}:${act}」吗？`, danger: true, onConfirm: () => doConfirm(() => api.deletePermission(sub, obj, act, domain)) });
  };

  // ── 批量角色 ──
  const handleBatchRole = () => wrap(
    () => api.batchUpdateRole(batchUserIds, batchRole),
    `已为 ${batchUserIds.length} 名用户分配角色`, () => { setBatchRoleModal(false); setBatchUserIds([]); setBatchRole(''); }
  );

  const toggleUserInBatch = (uid: string) => {
    setBatchUserIds((prev) => prev.includes(uid) ? prev.filter((id) => id !== uid) : [...prev, uid]);
  };

  if (!allowed) {
    return (
      <div className="flex flex-col items-center justify-center py-24 gap-3">
        <ShieldCheck size={40} className="text-slate-200" />
        <p className="text-sm text-slate-400">用户中心仅 super_admin / group_admin 可访问</p>
      </div>
    );
  }

  const selectedRole = ROLES.find((r) => r.value === userForm.role);
  const isEditing = !!editingUser;

  return (
    <div className={cn(!embedded && 'space-y-6 animate-in fade-in slide-in-from-bottom-4 duration-500')}>
      {!embedded && (
        <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
          <div>
            <h1 className="text-2xl font-bold text-slate-900 tracking-tight">用户中心</h1>
            <p className="text-slate-500 text-sm mt-1">成员管理 · 角色管理 · 权限管理。</p>
          </div>
          <Button variant="outline" className="h-10 rounded-xl border-slate-200 gap-2 font-medium" onClick={load}><RefreshCw size={15} />刷新</Button>
        </div>
      )}

      {loading ? <div className="flex justify-center py-20"><Loader2 className="animate-spin text-slate-300" size={28} /></div> : (
        <Tabs value={activeTab} onValueChange={setActiveTab} className="w-full">
          {!embedded && (
            <TabsList className="bg-slate-100/50 p-1 border rounded-xl gap-1 flex-wrap h-auto">
              <TabsTrigger value="members" className="rounded-lg data-[state=active]:bg-white data-[state=active]:shadow-sm font-bold gap-2 px-5 py-2 text-xs uppercase tracking-widest"><Users size={14} />成员管理</TabsTrigger>
              <TabsTrigger value="roles" className="rounded-lg data-[state=active]:bg-white data-[state=active]:shadow-sm font-bold gap-2 px-5 py-2 text-xs uppercase tracking-widest"><Shield size={14} />角色管理</TabsTrigger>
              <TabsTrigger value="permissions" className="rounded-lg data-[state=active]:bg-white data-[state=active]:shadow-sm font-bold gap-2 px-5 py-2 text-xs uppercase tracking-widest"><KeyRound size={14} />权限管理</TabsTrigger>
            </TabsList>
          )}

          <div className={cn('space-y-6', !embedded && 'mt-6')}>
            {/* ═══════════════════════ 成员管理 ═══════════════════════ */}
            <TabsContent value="members">
              <div className="flex flex-wrap items-center gap-3 mb-4">
                <div className="relative flex-1 min-w-[200px] max-w-sm">
                  <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
                  <Input className="pl-9 h-9 text-sm" placeholder="搜索姓名/账号/手机号..." value={userSearch}
                    onChange={(e) => { setUserSearch(e.target.value); setUserPage(1); }} />
                </div>
                <select value={userRoleFilter} onChange={(e) => { setUserRoleFilter(e.target.value); setUserPage(1); }}
                  className="h-9 px-3 rounded-lg border border-slate-200 text-sm outline-none bg-white">
                  <option value="">全部角色</option>
                  {ROLES.map((r) => <option key={r.value} value={r.value}>{r.label}</option>)}
                </select>
                <select value={userActiveFilter === '' ? '' : String(userActiveFilter)} onChange={(e) => { const v = e.target.value; setUserActiveFilter(v === '' ? '' : v === 'true'); setUserPage(1); }}
                  className="h-9 px-3 rounded-lg border border-slate-200 text-sm outline-none bg-white">
                  <option value="">全部状态</option>
                  <option value="true">已启用</option>
                  <option value="false">已禁用</option>
                </select>
                <Button size="sm" className="bg-[#2563eb] h-9 gap-1.5 font-bold" onClick={openCreateUser}><Plus size={14} />新增成员</Button>
                {isSuperAdmin && (
                  <Button size="sm" variant="outline" className="h-9 gap-1.5 font-bold" onClick={() => { setBatchRoleModal(true); setBatchUserIds([]); setBatchRole(''); }}>
                    <UserCog size={14} />批量分配角色
                  </Button>
                )}
              </div>

              <Card className="border-slate-200 shadow-sm overflow-hidden">
                <CardContent className="p-0 overflow-x-auto">
                  <table className="enterprise-table">
                    <thead><tr><th>姓名</th><th>账号</th><th>手机号</th><th>角色</th><th>职位</th><th>归属</th><th className="text-center">状态</th><th className="text-right">操作</th></tr></thead>
                    <tbody>
                      {users.map((u) => (
                        <tr key={u.id}>
                          <td><span className="font-bold text-slate-800">{u.name}</span></td>
                          <td><span className="font-mono text-xs text-slate-500">{u.account}</span></td>
                          <td><span className="font-mono text-xs text-slate-500">{u.phone || '-'}</span></td>
                          <td><Badge variant="outline" className="text-[10px] bg-violet-50 text-violet-600 border-violet-100 font-bold">{ROLE_LABEL_MAP[u.role] || u.role}</Badge></td>
                          <td><span className="text-xs text-slate-500">{u.position || '-'}</span></td>
                          <td>
                            <span className="text-xs text-slate-500">
                              {u.scenic_id ? (
                                <span>
                                  <span className="font-medium text-slate-700">{u.scenic_name || u.scenic_id}</span>
                                  {u.scenic_name && <span className="text-slate-400 ml-0.5">({u.scenic_id})</span>}
                                </span>
                              ) : (
                                <Badge variant="outline" className="text-[10px] bg-slate-100 text-slate-500 border-slate-200">集团</Badge>
                              )}
                            </span>
                          </td>
                          <td className="text-center"><Badge variant="outline" className={cn('text-[10px] font-bold', u.is_active ? 'bg-emerald-50 text-emerald-600 border-emerald-100' : 'bg-slate-100 text-slate-400')}>{u.is_active ? '启用' : '禁用'}</Badge></td>
                          <td className="text-right">
                            <div className="flex justify-end gap-1">
                              <Button variant="ghost" size="sm" className="h-8 text-[11px] gap-1 text-slate-500" onClick={() => openEditUser(u)}><Edit3 size={13} />编辑</Button>
                              <Button variant="ghost" size="sm" className="h-8 text-[11px] gap-1 text-slate-500" onClick={() => handleResetPassword(u)}><KeyRound size={13} />重置密码</Button>
                              <Button variant="ghost" size="icon" className="h-8 w-8 text-slate-400" onClick={() => wrap(() => api.updateUser(u.id, { is_active: !u.is_active }), '状态已更新', () => {})}>
                                {u.is_active ? <Lock size={14} /> : <Unlock size={14} />}
                              </Button>
                              <Button variant="ghost" size="icon" className="h-8 w-8 text-red-400 hover:text-red-600" onClick={() => handleDeleteUser(u)}><Trash2 size={14} /></Button>
                            </div>
                          </td>
                        </tr>
                      ))}
                      {users.length === 0 && <tr><td colSpan={8} className="py-12 text-center text-slate-300">暂无成员</td></tr>}
                    </tbody>
                  </table>
                </CardContent>
                <Paginator page={userPage} pageSize={userPageSize} total={userTotal} totalPages={userTotalPages}
                  onPageChange={setUserPage} onPageSizeChange={(s) => { setUserPageSize(s); setUserPage(1); }} />
              </Card>
            </TabsContent>

            {/* ═══════════════════════ 角色管理 ═══════════════════════ */}
            <TabsContent value="roles" className="space-y-6">
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
                {roles.map((role) => (
                  <Card key={role.role} className="border-slate-200 shadow-sm hover:shadow-md transition-shadow">
                    <CardHeader className="pb-2">
                      <CardTitle className="text-sm font-bold text-slate-800 flex items-center gap-2">
                        <ShieldCheck size={16} className="text-violet-500" />{role.label}
                      </CardTitle>
                    </CardHeader>
                    <CardContent>
                      <Badge variant="outline" className="text-[10px] bg-slate-50 font-mono mb-2">{role.role}</Badge>
                      {role.permissions && role.permissions.length > 0 && (
                        <div className="flex flex-wrap gap-1 mt-2">
                          {role.permissions.slice(0, 6).map((p: any, i: number) => (
                            <Badge key={i} variant="outline" className="text-[10px] bg-blue-50 text-blue-600 border-blue-100">{p.obj || p}</Badge>
                          ))}
                          {role.permissions.length > 6 && <Badge variant="outline" className="text-[10px] text-slate-400">+{role.permissions.length - 6}</Badge>}
                        </div>
                      )}
                    </CardContent>
                  </Card>
                ))}
              </div>

              {isSuperAdmin && (
                <Card className="border-slate-200 shadow-sm overflow-hidden">
                  <CardHeader className="border-b bg-slate-50/50 py-4 px-6">
                    <CardTitle className="text-sm font-black uppercase tracking-widest text-slate-600">角色继承（Groupings）</CardTitle>
                  </CardHeader>
                  <CardContent className="p-0 overflow-x-auto">
                  <table className="enterprise-table">
                    <thead><tr><th>角色</th><th>继承自</th></tr></thead>
                    <tbody>
                      {(() => {
                        const groupRoles = new Set(['group_viewer', 'group_developer', 'group_admin', 'super_admin']);
                        const groupLine = permissionsData.groupings.filter((g: any) => {
                          const sub = g.sub || g[0];
                          return groupRoles.has(sub);
                        });
                        const scenicLine = permissionsData.groupings.filter((g: any) => {
                          const sub = g.sub || g[0];
                          return !groupRoles.has(sub);
                        });

                        const rows: React.ReactNode[] = [];
                        // ── 集团线 ──
                        if (groupLine.length > 0) {
                          rows.push(
                            <tr key="sep-group" className="bg-slate-50/70">
                              <td colSpan={2} className="py-2">
                                <span className="text-[11px] font-bold text-slate-400 uppercase tracking-wider">
                                  <span className="inline-block w-1.5 h-1.5 rounded-full bg-blue-400 mr-1.5 align-middle"></span>
                                  集团线
                                </span>
                              </td>
                            </tr>
                          );
                          groupLine.forEach((g: any, i: number) => {
                            rows.push(
                              <tr key={`group-${i}`}>
                                <td className="pl-6"><Badge variant="outline" className="font-mono text-[11px]">{g.sub || g[0]}</Badge></td>
                                <td><Badge variant="outline" className="font-mono text-[11px] bg-blue-50 text-blue-600">{g.role || g[1]}</Badge></td>
                              </tr>
                            );
                          });
                        }

                        // ── 景区线 ──
                        if (scenicLine.length > 0) {
                          rows.push(
                            <tr key="sep-scenic" className="bg-slate-50/70">
                              <td colSpan={2} className="py-2">
                                <span className="text-[11px] font-bold text-slate-400 uppercase tracking-wider">
                                  <span className="inline-block w-1.5 h-1.5 rounded-full bg-emerald-400 mr-1.5 align-middle"></span>
                                  景区线
                                </span>
                              </td>
                            </tr>
                          );
                          scenicLine.forEach((g: any, i: number) => {
                            rows.push(
                              <tr key={`scenic-${i}`}>
                                <td className="pl-6"><Badge variant="outline" className="font-mono text-[11px]">{g.sub || g[0]}</Badge></td>
                                <td><Badge variant="outline" className="font-mono text-[11px] bg-emerald-50 text-emerald-600">{g.role || g[1]}</Badge></td>
                              </tr>
                            );
                          });
                        }

                        if (rows.length === 0) {
                          rows.push(<tr key="empty"><td colSpan={2} className="py-8 text-center text-slate-300">暂无角色继承关系</td></tr>);
                        }
                        return rows;
                      })()}
                      </tbody>
                    </table>
                  </CardContent>
                </Card>
              )}

              {isSuperAdmin && Object.keys(usersByRole).length > 0 && (
                <Card className="border-slate-200 shadow-sm overflow-hidden">
                  <CardHeader className="border-b bg-slate-50/50 py-4 px-6">
                    <CardTitle className="text-sm font-black uppercase tracking-widest text-slate-600">用户角色分布</CardTitle>
                  </CardHeader>
                  <CardContent className="p-0 overflow-x-auto">
                    <table className="enterprise-table">
                      <thead><tr><th>角色</th><th>用户数</th><th>用户</th></tr></thead>
                      <tbody>
                        {Object.entries(usersByRole).map(([role, userList]) => (
                          <tr key={role}>
                            <td><Badge variant="outline" className="bg-violet-50 text-violet-600 border-violet-100 font-bold">{ROLE_LABEL_MAP[role] || role}</Badge></td>
                            <td><span className="font-mono text-sm text-slate-600">{userList.length}</span></td>
                            <td><div className="flex flex-wrap gap-1">{userList.slice(0, 8).map((u: any) => <Badge key={u.id} variant="outline" className="text-[10px]">{u.name}</Badge>)}</div></td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </CardContent>
                </Card>
              )}
            </TabsContent>

            {/* ═══════════════════════ 权限管理 ═══════════════════════ */}
            <TabsContent value="permissions" className="space-y-6">
              {/* 权限域切换 */}
              <div className="flex items-center gap-2">
                <span className="text-xs font-bold text-slate-500 uppercase tracking-wider">权限域：</span>
                <div className="flex bg-slate-100 rounded-lg p-1 gap-0.5">
                  <button
                    onClick={() => setPermDomain('admin')}
                    className={cn('px-4 py-1.5 rounded-md text-xs font-bold transition-all',
                      permDomain === 'admin' ? 'bg-white text-slate-800 shadow-sm' : 'text-slate-500 hover:text-slate-700')}
                  >管理端</button>
                  <button
                    onClick={() => setPermDomain('app')}
                    className={cn('px-4 py-1.5 rounded-md text-xs font-bold transition-all',
                      permDomain === 'app' ? 'bg-white text-slate-800 shadow-sm' : 'text-slate-500 hover:text-slate-700')}
                  >APP端</button>
                </div>
                <Badge variant="outline" className="text-[10px] font-mono text-slate-400 ml-auto">{permDomain}</Badge>
              </div>

              <Card className="border-slate-200 shadow-sm overflow-hidden">
                <CardHeader className="border-b bg-slate-50/50 py-4 px-6 flex flex-row items-center justify-between">
                  <CardTitle className="text-sm font-black uppercase tracking-widest text-slate-600">权限策略（Policies）</CardTitle>
                  <Button size="sm" className="bg-[#2563eb] h-9 gap-1.5 font-bold" onClick={() => { setPermForm({ sub: '', obj: '', act: '', domain: permDomain }); setPermModal(true); }}><Plus size={14} />新增策略</Button>
                </CardHeader>
                <CardContent className="p-0 overflow-x-auto">
                  <table className="enterprise-table">
                    <thead><tr><th>域</th><th>角色 (sub)</th><th>资源 (obj)</th><th>操作 (act)</th><th className="text-right">操作</th></tr></thead>
                    <tbody>
                      {permissionsData.policies.map((p, i) => (
                        <tr key={i}>
                          <td><Badge variant="outline" className={cn('text-[10px] font-bold', p.domain === 'app' ? 'bg-emerald-50 text-emerald-600 border-emerald-100' : 'bg-blue-50 text-blue-600 border-blue-100')}>{p.domain || p[3] || 'admin'}</Badge></td>
                          <td><Badge variant="outline" className="font-mono text-[11px]">{p.sub || p[0]}</Badge></td>
                          <td><span className="font-mono text-xs text-slate-600">{p.obj || p[1]}</span></td>
                          <td><span className="font-mono text-xs text-slate-600">{p.act || p[2]}</span></td>
                          <td className="text-right">
                            <Button variant="ghost" size="icon" className="h-8 w-8 text-red-400 hover:text-red-600"
                              onClick={() => handleDeletePermission(p.sub || p[0], p.obj || p[1], p.act || p[2], p.domain || p[3] || 'admin')}><Trash2 size={14} /></Button>
                          </td>
                        </tr>
                      ))}
                      {permissionsData.policies.length === 0 && <tr><td colSpan={5} className="py-8 text-center text-slate-300">暂无 {permDomain === 'app' ? 'APP端' : '管理端'} 权限策略</td></tr>}
                    </tbody>
                  </table>
                </CardContent>
              </Card>
            </TabsContent>
          </div>
        </Tabs>
      )}

      {/* ═══════════════════════ 弹窗区 ═══════════════════════ */}

      {/* 新增/编辑成员 */}
      {userModal && (
        <Modal title={isEditing ? '编辑成员' : '新增成员'} onClose={() => setUserModal(false)}
          footer={<><Button variant="outline" onClick={() => setUserModal(false)}>取消</Button>
            <Button disabled={saving} className="bg-[#2563eb] gap-1.5" onClick={handleSaveUser}>{saving && <Loader2 size={14} className="animate-spin" />}{isEditing ? '保存' : '创建'}</Button></>}>
          <div className="space-y-1.5"><label className="form-label">姓名</label><Input value={userForm.name} onChange={(e) => setUserForm({ ...userForm, name: e.target.value })} /></div>
          {!isEditing && <div className="space-y-1.5"><label className="form-label">手机号</label><Input value={userForm.phone} onChange={(e) => setUserForm({ ...userForm, phone: e.target.value })} /></div>}
          <div className="space-y-1.5"><label className="form-label">角色</label>
            <select value={userForm.role} onChange={(e) => setUserForm({ ...userForm, role: e.target.value })} className="w-full h-10 px-3 rounded-lg border border-slate-200 text-sm outline-none">
              {ROLES.map((r) => <option key={r.value} value={r.value}>{r.label}</option>)}
            </select>
          </div>
          {selectedRole?.scenicRequired && (
            <div className="space-y-1.5 relative">
              <label className="form-label">所属景区</label>
              {/* 搜索输入框 */}
              <Input
                value={scenicSearch || selectedScenicNameState || ''}
                onChange={(e) => handleScenicSearch(e.target.value)}
                onFocus={() => { if (scenicList.length > 0) setScenicDropdown(true); }}
                onBlur={() => setTimeout(() => setScenicDropdown(false), 200)}
                placeholder={userForm.scenic_id ? '' : '输入景区名称搜索...'}
                className="w-full"
              />
              {/* 选中后的景区显示 */}
              {userForm.scenic_id && (
                <div className="flex items-center gap-2 mt-1">
                  <Badge variant="outline" className="text-[11px] bg-blue-50 text-blue-600 border-blue-100">
                    {userForm.scenic_id}
                    {selectedScenicNameState && <span className="ml-1 text-slate-400">({selectedScenicNameState})</span>}
                  </Badge>
                  <button className="text-xs text-slate-400 hover:text-red-500" onClick={handleClearScenic}>清除</button>
                </div>
              )}
              {/* 下拉列表: 仅在后端搜索有结果时展示 */}
              {scenicDropdown && scenicList.length > 0 && (
                <div className="absolute z-50 left-0 right-0 mt-1 max-h-60 overflow-y-auto bg-white border border-slate-200 rounded-lg shadow-lg">
                  {scenicList.map((s) => (
                    <div
                      key={s.scenic_id}
                      className={cn(
                        'px-3 py-2 cursor-pointer hover:bg-blue-50 text-sm border-b border-slate-50 last:border-b-0',
                        userForm.scenic_id === s.scenic_id && 'bg-blue-50 text-blue-700 font-medium'
                      )}
                      onMouseDown={(e) => { e.preventDefault(); handleSelectScenic(s); }}
                    >
                      <div className="font-medium text-slate-800">{s.scenic_name}</div>
                      <div className="text-[11px] text-slate-400 flex gap-2">
                        <span>{s.scenic_id}</span>
                        {s.scenic_grade_level && <span className="text-amber-500">{s.scenic_grade_level}</span>}
                      </div>
                    </div>
                  ))}
                </div>
              )}
              {/* 搜索无结果提示 */}
              {scenicDropdown && scenicList.length === 0 && scenicSearch.trim() && (
                <div className="absolute z-50 left-0 right-0 mt-1 bg-white border border-slate-200 rounded-lg shadow-lg px-3 py-6 text-center text-sm text-slate-400">
                  未找到匹配的景区
                </div>
              )}
            </div>
          )}
          <div className="space-y-1.5"><label className="form-label">职位（选填）</label><Input value={userForm.position} onChange={(e) => setUserForm({ ...userForm, position: e.target.value })} /></div>
        </Modal>
      )}

      {/* 新增权限策略 */}
      {permModal && (
        <Modal title="新增权限策略" onClose={() => setPermModal(false)} wide
          footer={<><Button variant="outline" onClick={() => setPermModal(false)}>取消</Button><Button disabled={saving} className="bg-[#2563eb] gap-1.5" onClick={handleAddPermission}>{saving && <Loader2 size={14} className="animate-spin" />}添加</Button></>}>
          <div className="bg-slate-50 border border-slate-100 rounded-xl p-3 text-[11px] text-slate-500 leading-relaxed">
            格式：角色 (sub) 对 资源 (obj) 拥有 操作 (act) 权限。<br />
            常用资源名：user, role, agent, tool, skill, knowledge, system, task, approval, eval<br />
            APP端资源名建议以 app: 前缀命名（如 app:dashboard, app:ticket:view）
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-4 gap-3">
            <div className="space-y-1.5"><label className="form-label">角色 (sub)</label>
              <select value={permForm.sub} onChange={(e) => setPermForm({ ...permForm, sub: e.target.value })} className="w-full h-10 px-3 rounded-lg border border-slate-200 text-sm outline-none">
                <option value="">选择角色</option>
                {ROLES.map((r) => <option key={r.value} value={r.value}>{r.label}</option>)}
              </select>
            </div>
            <div className="space-y-1.5"><label className="form-label">资源 (obj)</label><Input value={permForm.obj} onChange={(e) => setPermForm({ ...permForm, obj: e.target.value })} placeholder="如 user, agent" /></div>
            <div className="space-y-1.5"><label className="form-label">操作 (act)</label><Input value={permForm.act} onChange={(e) => setPermForm({ ...permForm, act: e.target.value })} placeholder="如 read, write" /></div>
            <div className="space-y-1.5"><label className="form-label">权限域</label>
              <select value={permForm.domain} onChange={(e) => setPermForm({ ...permForm, domain: e.target.value })} className="w-full h-10 px-3 rounded-lg border border-slate-200 text-sm outline-none">
                <option value="admin">管理端</option>
                <option value="app">APP端</option>
              </select>
            </div>
          </div>
        </Modal>
      )}

      {/* 批量角色分配 */}
      {batchRoleModal && (
        <Modal title="批量分配角色" onClose={() => setBatchRoleModal(false)} wide
          footer={<><Button variant="outline" onClick={() => setBatchRoleModal(false)}>取消</Button>
            <Button disabled={saving || batchUserIds.length === 0 || !batchRole} className="bg-[#2563eb] gap-1.5" onClick={handleBatchRole}>{saving && <Loader2 size={14} className="animate-spin" />}分配</Button></>}>
          <div className="space-y-1.5"><label className="form-label">目标角色</label>
            <select value={batchRole} onChange={(e) => setBatchRole(e.target.value)} className="w-full h-10 px-3 rounded-lg border border-slate-200 text-sm outline-none">
              <option value="">选择角色</option>
              {ROLES.map((r) => <option key={r.value} value={r.value}>{r.label}</option>)}
            </select>
          </div>
          <div className="space-y-1.5">
            <label className="form-label">选择用户（已选 {batchUserIds.length} 人）</label>
            <div className="max-h-56 overflow-y-auto border border-slate-200 rounded-lg divide-y divide-slate-100">
              {users.map((u) => (
                <label key={u.id} className="flex items-center gap-3 px-3 py-2 hover:bg-slate-50 cursor-pointer">
                  <input type="checkbox" checked={batchUserIds.includes(u.id)} onChange={() => toggleUserInBatch(u.id)} className="rounded" />
                  <span className="text-sm font-medium text-slate-700">{u.name}</span>
                  <span className="text-xs text-slate-400 font-mono">{u.account}</span>
                  <Badge variant="outline" className="text-[10px] ml-auto">{ROLE_LABEL_MAP[u.role] || u.role}</Badge>
                </label>
              ))}
              {users.length === 0 && <div className="px-3 py-8 text-center text-slate-300 text-sm">请先在成员管理中搜索需要的用户</div>}
            </div>
          </div>
        </Modal>
      )}

      {/* 密码展示 */}
      {newPwdInfo && <PasswordReveal account={newPwdInfo.account} password={newPwdInfo.password} onClose={() => setNewPwdInfo(null)} />}

      {/* 确认弹窗 */}
      {confirm && <ConfirmModal title={confirm.title} message={confirm.message} danger={confirm.danger} loading={confirmLoading} onConfirm={() => doConfirm(confirm.onConfirm)} onCancel={() => setConfirm(null)} />}
    </div>
  );
};

export default UserCenter;
