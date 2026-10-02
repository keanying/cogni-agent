import { useState, useEffect, useCallback } from "react";
import {
  ShieldCheck, Shield, Plus, Trash2, RefreshCw, Loader2, Users2,
  ChevronDown, ChevronRight, AlertTriangle, CheckCircle2, XCircle,
  Zap,
} from "lucide-react";
import {
  C, mono, ui, shadow,
  Card, Chip, Btn, IconBtn, Input, Modal, Field, PageHead, useToast,
  Th, Td, Row, Empty,
} from "../kit";
import {
  getRoleDetails, listPermissions, addPolicy, removePolicy,
  getUsersByRole, batchUpdateRole, getRoles,
  type RoleDetailItem, type PolicyItem, type PolicyMatrix, type UsersByRoleItem,
} from "../../lib/api";

/* ══════════ 常量 ══════════ */
const ROLE_LABELS: Record<string, string> = {
  super_admin: "超级管理员",
  group_admin: "集团管理员",
  group_developer: "集团开发者",
  group_viewer: "集团查看者",
  scenic_user: "景区用户",
};
const ROLE_COLORS: Record<string, string> = {
  super_admin: C.rose,
  group_admin: C.violet,
  group_developer: C.accent,
  group_viewer: C.orange,
  scenic_user: C.teal,
};
const RESOURCE_LABELS: Record<string, string> = {
  "*": "全部资源",
  system_center: "系统中心",
  members: "成员管理",
  tools: "工具",
  skills: "技能",
  agents: "Agent",
  scenic_data: "景区数据",
};

/* ══════════ 角色与权限页面 ══════════ */
export default function RolePermissionPage() {
  const toast = useToast();
  const [tab, setTab] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // ── 角色详情(含权限) ──
  const [roleDetails, setRoleDetails] = useState<RoleDetailItem[]>([]);
  // ── 策略列表 ──
  const [policies, setPolicies] = useState<PolicyItem[]>([]);
  const [groupings, setGroupings] = useState<PolicyItem[]>([]);
  const [matrix, setMatrix] = useState<PolicyMatrix | null>(null);
  // ── 用户按角色分组 ──
  const [usersByRole, setUsersByRole] = useState<UsersByRoleItem[]>([]);
  // ── 基础角色下拉 ──
  const [roleOptions, setRoleOptions] = useState<{ value: string; label: string }[]>([]);

  // ── 弹窗状态 ──
  const [showAddPolicy, setShowAddPolicy] = useState(false);
  const [addPolicyForm, setAddPolicyForm] = useState({ ptype: "p", sub: "", obj: "", act: "read" });
  const [submitting, setSubmitting] = useState(false);

  // ── 删除确认 ──
  const [delTarget, setDelTarget] = useState<PolicyItem | null>(null);

  // ── 批量改角色 ──
  const [batchModal, setBatchModal] = useState(false);
  const [batchRole, setBatchRole] = useState("");
  const [selectedUsers, setSelectedUsers] = useState<string[]>([]);
  const [currentRoleGroup, setCurrentRoleGroup] = useState("");

  // ── 展开状态 ──
  const [expandedRoles, setExpandedRoles] = useState<Set<string>>(new Set());
  const [expandedUsers, setExpandedUsers] = useState<Set<string>>(new Set());

  const toggleExpandRole = (role: string) => {
    setExpandedRoles(prev => {
      const next = new Set(prev);
      next.has(role) ? next.delete(role) : next.add(role);
      return next;
    });
  };
  const toggleExpandUsers = (role: string) => {
    setExpandedUsers(prev => {
      const next = new Set(prev);
      next.has(role) ? next.delete(role) : next.add(role);
      return next;
    });
  };

  // ── 数据加载 ──
  const loadData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [roleRes, permRes, usersRes, basicRolesRes] = await Promise.all([
        getRoleDetails(),
        listPermissions(),
        getUsersByRole(),
        getRoles(),
      ]);
      setRoleDetails(roleRes.roles || []);
      setPolicies(permRes.policies || []);
      setGroupings(permRes.groupings || []);
      setMatrix(permRes.matrix || null);
      setUsersByRole(usersRes.roles || []);
      setRoleOptions((basicRolesRes.roles || []).map((r: any) => ({ value: r.value, label: r.label })));
    } catch (e: any) {
      setError(e.message || "加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadData(); }, [loadData]);

  // ── 新增策略 ──
  async function handleAddPolicy() {
    if (!addPolicyForm.sub.trim()) { toast("请填写角色/子角色", "err"); return; }
    if (!addPolicyForm.obj.trim()) { toast("请填写资源/父角色", "err"); return; }
    setSubmitting(true);
    try {
      const res = await addPolicy({
        ptype: addPolicyForm.ptype,
        sub: addPolicyForm.sub.trim(),
        obj: addPolicyForm.obj.trim(),
        act: addPolicyForm.ptype === "g" ? "" : addPolicyForm.act,
      });
      toast("策略已添加", "ok");
      setShowAddPolicy(false);
      setAddPolicyForm({ ptype: "p", sub: "", obj: "", act: "read" });
      loadData();
    } catch (e: any) {
      toast(e.message || "操作失败", "err");
    } finally { setSubmitting(false); }
  }

  // ── 删除策略 ──
  async function handleRemovePolicy() {
    if (!delTarget) return;
    try {
      const res = await removePolicy({
        ptype: delTarget.ptype,
        sub: delTarget.sub,
        obj: delTarget.obj,
        act: delTarget.act,
      });
      toast("策略已删除", "ok");
      setDelTarget(null);
      loadData();
    } catch (e: any) {
      toast(e.message || "操作失败", "err");
    }
  }

  // ── 批量改角色 ──
  async function handleBatchRole() {
    if (!batchRole) { toast("请选择目标角色", "err"); return; }
    if (selectedUsers.length === 0) { toast("请选择用户", "err"); return; }
    try {
      const res = await batchUpdateRole(selectedUsers, batchRole);
      toast(`已将 ${res.updated_count} 个用户角色更新`, "ok");
      setBatchModal(false);
      setSelectedUsers([]);
      setBatchRole("");
      loadData();
    } catch (e: any) {
      toast(e.message || "操作失败", "err");
    }
  }

  function openBatchModal(roleGroup: string) {
    setCurrentRoleGroup(roleGroup);
    const ug = usersByRole.find(u => u.role === roleGroup);
    setSelectedUsers(ug ? ug.users.map((u: any) => u.id) : []);
    setBatchRole("");
    setBatchModal(true);
  }

  function toggleUserSelection(userId: string) {
    setSelectedUsers(prev =>
      prev.includes(userId) ? prev.filter(id => id !== userId) : [...prev, userId]
    );
  }

  const tabs = [
    { label: "角色概览", id: 0 },
    { label: "策略管理", id: 1 },
    { label: "用户角色分配", id: 2 },
  ];

  if (loading) {
    return (
      <div className="grid gap-4">
        <PageHead pid="P13" module="M7 · governance-event-bus-service" title="角色与权限" sub="系统设置 · Casbin RBAC 权限治理" />
        <div className="py-16 flex items-center justify-center">
          <Loader2 size={24} className="animate-spin" color={C.faint} />
        </div>
      </div>
    );
  }

  return (
    <div className="grid gap-4">
      <PageHead pid="P13" module="M7 · governance-event-bus-service" title="角色与权限" sub="系统设置 · Casbin RBAC 权限策略管理 · 用户角色分配" />

      {error && (
        <div className="rounded-lg border px-4 py-3 flex items-center gap-2" style={{ borderColor: C.red + "44", background: C.red + "0a" }}>
          <XCircle size={14} color={C.red} />
          <span className="text-[12px]" style={{ color: C.red }}>{error}</span>
          <div className="flex-1" />
          <Btn small onClick={loadData}>重试</Btn>
        </div>
      )}

      {/* Tab 切换 */}
      <div className="flex gap-1.5 border-b" style={{ borderColor: C.border }}>
        {tabs.map(t => (
          <button key={t.id} onClick={() => setTab(t.id)}
            className="px-4 py-2.5 text-[12.5px] border-b-2 -mb-px transition-colors"
            style={{ ...ui, fontWeight: tab === t.id ? 700 : 500, color: tab === t.id ? C.accent : C.muted, borderColor: tab === t.id ? C.accent : "transparent" }}>
            {t.label}
          </button>
        ))}
        <div className="flex-1" />
        <Btn ghost onClick={loadData}><RefreshCw size={13} />刷新</Btn>
      </div>

      {/* ══════════ Tab 1: 角色概览 ══════════ */}
      {tab === 0 && (
        <div className="grid gap-4">
          {/* 角色卡片 */}
          <div className="grid grid-cols-1 xl:grid-cols-5 gap-3">
            {roleDetails.map(r => (
              <div key={r.value} className="rounded-xl border p-4 transition-all cursor-pointer"
                style={{
                  borderColor: expandedRoles.has(r.value) ? (ROLE_COLORS[r.value] || C.violet) + "60" : C.border,
                  background: expandedRoles.has(r.value) ? (ROLE_COLORS[r.value] || C.violet) + "0d" : C.card2,
                  boxShadow: expandedRoles.has(r.value) ? `0 0 0 1px ${(ROLE_COLORS[r.value] || C.violet)}40` : undefined,
                }}
                onClick={() => toggleExpandRole(r.value)}>
                <div className="flex items-center justify-between mb-2">
                  <div className="flex items-center gap-2">
                    <span className="w-7 h-7 rounded-lg flex items-center justify-center text-[10px] font-bold"
                      style={{ background: ROLE_COLORS[r.value] || C.violet, color: "#fff" }}>
                      {r.label[0]}
                    </span>
                    <span className="text-[13px] font-bold" style={{ color: C.text }}>{r.label}</span>
                  </div>
                  <div className="flex items-center gap-1">
                    <Chip label={r.scope} color={ROLE_COLORS[r.value] || C.muted} />
                    {expandedRoles.has(r.value) ? <ChevronDown size={14} color={C.faint} /> : <ChevronRight size={14} color={C.faint} />}
                  </div>
                </div>
                <div className="text-[10.5px]" style={{ ...mono, color: C.faint }}>
                  {r.value} · {r.permissions.length} 条权限
                  {r.inherited_from.length > 0 && ` · 继承: ${r.inherited_from.map(p => ROLE_LABELS[p] || p).join(", ")}`}
                </div>

                {/* 展开详情 */}
                {expandedRoles.has(r.value) && (
                  <div className="mt-3 pt-3 border-t space-y-1.5" style={{ borderColor: C.border }} onClick={e => e.stopPropagation()}>
                    {/* 直接权限 */}
                    {r.permissions.filter(p => !p.inherited).length > 0 && (
                      <div>
                        <div className="text-[10px] font-bold mb-1" style={{ color: C.muted }}>直接权限</div>
                        {r.permissions.filter(p => !p.inherited).map((p, i) => (
                          <div key={i} className="flex items-center gap-1.5 text-[10.5px]" style={mono}>
                            <span style={{ color: C.accent }}>{p.resource}</span>
                            <span style={{ color: C.faint }}>:</span>
                            <Chip label={p.action} color={p.action === "*" ? C.rose : p.action === "write" ? C.amber : C.green} />
                          </div>
                        ))}
                      </div>
                    )}
                    {/* 继承权限 */}
                    {r.permissions.filter(p => p.inherited).length > 0 && (
                      <div className="mt-2">
                        <div className="text-[10px] font-bold mb-1" style={{ color: C.muted }}>继承权限</div>
                        {r.permissions.filter(p => p.inherited).map((p, i) => (
                          <div key={i} className="flex items-center gap-1.5 text-[10.5px]" style={mono}>
                            <span style={{ color: C.accent, opacity: 0.7 }}>{p.resource}</span>
                            <span style={{ color: C.faint }}>:</span>
                            <Chip label={p.action} color={C.faint} />
                            <span className="text-[9px]" style={{ color: C.faint }}>← {ROLE_LABELS[p.inherit_from!] || p.inherit_from}</span>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </div>
            ))}
          </div>

          {/* 权限矩阵表 */}
          <Card title="权限矩阵（角色 × 资源 → 操作）" extra={<span className="text-[10px]" style={{ ...mono, color: C.faint }}>Casbin RBAC 实际 enforce 结果</span>}>
            {matrix ? (
              <div className="overflow-x-auto">
                <table className="w-full text-[11px]" style={mono}>
                  <thead>
                    <tr>
                      <Th w={120}>角色</Th>
                      {matrix.resources.map(res => (
                        <Th key={res}>{RESOURCE_LABELS[res] || res}</Th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {matrix.roles.map(role => (
                      <tr key={role} className="border-b" style={{ borderColor: C.border }}>
                        <td className="px-3 py-2.5">
                          <Chip label={ROLE_LABELS[role] || role} color={ROLE_COLORS[role] || C.muted} />
                        </td>
                        {matrix.resources.map(res => {
                          const actions = matrix.matrix[role]?.[res] || [];
                          const hasAll = actions.includes("*");
                          const hasWrite = actions.includes("write");
                          const hasRead = actions.includes("read");
                          return (
                            <td key={res} className="px-2 py-2.5 text-center">
                              {actions.length === 0 ? (
                                <span style={{ color: C.faint }}>—</span>
                              ) : (
                                <div className="flex items-center justify-center gap-1 flex-wrap">
                                  {hasAll && <Chip label="RW" color={C.green} />}
                                  {!hasAll && hasWrite && <Chip label="R" color={C.green} />}
                                  {!hasAll && hasRead && <Chip label="R" color={C.green} />}
                                </div>
                              )}
                            </td>
                          );
                        })}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <Empty text="暂无矩阵数据" />
            )}
          </Card>
        </div>
      )}

      {/* ══════════ Tab 2: 策略管理 ══════════ */}
      {tab === 2 && (
        <div className="grid gap-4">
          <div className="flex items-center gap-2">
            <Btn primary onClick={() => setShowAddPolicy(true)}><Plus size={14} />新增策略</Btn>
          </div>

          {/* P 策略 */}
          <Card title={`P 策略（权限策略）· ${policies.length} 条`} extra={<span className="text-[10px]" style={{ ...mono, color: C.faint }}>sub(角色) → obj(资源): act(操作)</span>}>
            {policies.length === 0 ? <Empty text="暂无 P 策略" /> : (
              <table className="w-full">
                <thead>
                  <tr>
                    <Th>角色(sub)</Th>
                    <Th>资源(obj)</Th>
                    <Th>操作(act)</Th>
                    <Th w={80}>操作</Th>
                  </tr>
                </thead>
                <tbody>
                  {policies.map((p, i) => (
                    <Row key={i}>
                      <Td><Chip label={p.sub} color={ROLE_COLORS[p.sub] || C.accent} /></Td>
                      <Td><span style={{ color: C.accent }}>{p.obj}</span></Td>
                      <Td><Chip label={p.act} color={p.act === "*" ? C.rose : C.green} /></Td>
                      <Td>
                        <IconBtn icon={Trash2} color={C.red} onClick={() => setDelTarget(p)} title="删除策略" />
                      </Td>
                    </Row>
                  ))}
                </tbody>
              </table>
            )}
          </Card>

          {/* G 策略 */}
          <Card title={`G 策略（角色继承）· ${groupings.length} 条`} extra={<span className="text-[10px]" style={{ ...mono, color: C.faint }}>child → parent 继承链</span>}>
            {groupings.length === 0 ? <Empty text="暂无 G 策略" /> : (
              <table className="w-full">
                <thead>
                  <tr>
                    <Th>子角色(child)</Th>
                    <Th>箭头</Th>
                    <Th>父角色(parent)</Th>
                    <Th w={80}>操作</Th>
                  </tr>
                </thead>
                <tbody>
                  {groupings.map((g, i) => (
                    <Row key={i}>
                      <Td><Chip label={g.sub} color={ROLE_COLORS[g.sub] || C.accent} /></Td>
                      <Td><span style={{ color: C.faint }}>→ 继承 →</span></Td>
                      <Td><Chip label={g.obj} color={ROLE_COLORS[g.obj] || C.violet} /></Td>
                      <Td>
                        <IconBtn icon={Trash2} color={C.red} onClick={() => setDelTarget(g)} title="删除继承" />
                      </Td>
                    </Row>
                  ))}
                </tbody>
              </table>
            )}
          </Card>
        </div>
      )}

      {/* ══════════ Tab 3: 用户角色分配 ══════════ */}
      {tab === 1 && (
        <div className="grid gap-4">
          {usersByRole.length === 0 ? (
            <Empty text="暂无用户数据" />
          ) : (
            usersByRole.map(ug => (
              <Card
                key={ug.role}
                title={
                  <div className="flex items-center gap-2">
                    <span className="w-6 h-6 rounded-lg flex items-center justify-center text-[9px] font-bold"
                      style={{ background: ROLE_COLORS[ug.role] || C.violet, color: "#fff" }}>
                      {ug.role_label[0]}
                    </span>
                    <span className="text-[13px] font-bold" style={{ color: C.text }}>{ug.role_label}</span>
                    <Chip label={`${ug.count} 人`} color={ROLE_COLORS[ug.role] || C.muted} />
                  </div>
                }
                extra={
                  <div className="flex items-center gap-2">
                    <button onClick={() => toggleExpandUsers(ug.role)}
                      className="text-[10.5px] flex items-center gap-1 transition-colors"
                      style={{ color: C.muted }}>
                      {expandedUsers.has(ug.role) ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                      {expandedUsers.has(ug.role) ? "收起" : `展开 (${ug.count})`}
                    </button>
                    <Btn small onClick={() => openBatchModal(ug.role)}>
                      <ShieldCheck size={12} />批量改角色
                    </Btn>
                  </div>
                }
              >
                {expandedUsers.has(ug.role) && (
                  ug.users.length === 0 ? (
                    <Empty text="该角色下暂无用户" />
                  ) : (
                    <table className="w-full">
                      <thead>
                        <tr>
                          <Th>姓名</Th>
                          <Th>账号</Th>
                          <Th>手机号</Th>
                          <Th>所属组织</Th>
                          <Th>状态</Th>
                          <Th>最近登录</Th>
                        </tr>
                      </thead>
                      <tbody>
                        {ug.users.map((u: any) => (
                          <Row key={u.id}>
                            <Td>
                              <div className="flex items-center gap-2">
                                <span className="w-5 h-5 rounded-full flex items-center justify-center text-[9px] font-bold"
                                  style={{ background: ROLE_COLORS[u.role] || C.violet, color: "#fff" }}>
                                  {u.name?.[0] || "?"}
                                </span>
                                <span style={{ color: C.text, fontWeight: 600 }}>{u.name}</span>
                              </div>
                            </Td>
                            <Td><span style={{ color: C.accent }}>{u.account}</span></Td>
                            <Td>{u.phone}</Td>
                            <Td>
                              {u.scenic_name ? (
                                <span style={{ color: C.teal }}>{u.scenic_name}</span>
                              ) : (
                                <Chip label="集团" color={C.violet} />
                              )}
                            </Td>
                            <Td>
                              <Chip label={u.is_active ? "已启用" : "已禁用"} color={u.is_active ? C.green : C.red} />
                            </Td>
                            <Td><span style={{ color: C.faint }}>{u.last_login_at || "从未登录"}</span></Td>
                          </Row>
                        ))}
                      </tbody>
                    </table>
                  )
                )}
              </Card>
            ))
          )}
        </div>
      )}

      {/* ── 新增策略弹窗 ── */}
      {showAddPolicy && (
        <Modal
          title="新增 Casbin 策略"
          icon={Shield}
          color={C.violet}
          onClose={() => setShowAddPolicy(false)}
          w={520}
          footer={
            <>
              <Btn onClick={() => setShowAddPolicy(false)}>取消</Btn>
              <Btn primary onClick={handleAddPolicy} loading={submitting} disabled={submitting}>
                确认添加
              </Btn>
            </>
          }>
          <div className="space-y-3.5">
            <Field label="策略类型">
              <div className="flex gap-2">
                {[
                  { v: "p", l: "P 策略（权限策略）", desc: "角色→资源:操作" },
                  { v: "g", l: "G 策略（角色继承）", desc: "子角色→父角色" },
                ].map(opt => (
                  <button key={opt.v} onClick={() => setAddPolicyForm({ ...addPolicyForm, ptype: opt.v, act: opt.v === "g" ? "" : "read" })}
                    className="flex-1 rounded-lg border p-2.5 text-left transition-all"
                    style={{
                      borderColor: addPolicyForm.ptype === opt.v ? (ROLE_COLORS[opt.v] || C.accent) + "60" : C.border,
                      background: addPolicyForm.ptype === opt.v ? C.accent + "0d" : C.card2,
                    }}>
                    <div className="text-[11.5px] font-bold" style={{ color: addPolicyForm.ptype === opt.v ? C.accent : C.muted }}>{opt.l}</div>
                    <div className="text-[10px] mt-0.5" style={{ ...mono, color: C.faint }}>{opt.desc}</div>
                  </button>
                ))}
              </div>
            </Field>

            <div className="grid grid-cols-2 gap-3">
              <Field label={addPolicyForm.ptype === "p" ? "角色(sub)" : "子角色(child)"}>
                <select className="w-full rounded-lg border px-2.5 h-9 outline-none text-[12.5px]"
                  style={{ ...ui, background: C.panel, borderColor: C.border, color: C.text }}
                  value={addPolicyForm.sub}
                  onChange={e => setAddPolicyForm({ ...addPolicyForm, sub: e.target.value })}>
                  <option value="">选择角色…</option>
                  {roleOptions.map(r => (
                    <option key={r.value} value={r.value}>{r.label} ({r.value})</option>
                  ))}
                </select>
              </Field>
              <Field label={addPolicyForm.ptype === "p" ? "资源(obj)" : "父角色(parent)"}>
                {addPolicyForm.ptype === "p" ? (
                  <select className="w-full rounded-lg border px-2.5 h-9 outline-none text-[12.5px]"
                    style={{ ...ui, background: C.panel, borderColor: C.border, color: C.text }}
                    value={addPolicyForm.obj}
                    onChange={e => setAddPolicyForm({ ...addPolicyForm, obj: e.target.value })}>
                    <option value="">选择资源…</option>
                    {Object.entries(RESOURCE_LABELS).map(([k, v]) => (
                      <option key={k} value={k}>{v} ({k})</option>
                    ))}
                  </select>
                ) : (
                  <select className="w-full rounded-lg border px-2.5 h-9 outline-none text-[12.5px]"
                    style={{ ...ui, background: C.panel, borderColor: C.border, color: C.text }}
                    value={addPolicyForm.obj}
                    onChange={e => setAddPolicyForm({ ...addPolicyForm, obj: e.target.value })}>
                    <option value="">选择父角色…</option>
                    {roleOptions.map(r => (
                      <option key={r.value} value={r.value}>{r.label} ({r.value})</option>
                    ))}
                  </select>
                )}
              </Field>
            </div>

            {addPolicyForm.ptype === "p" && (
              <Field label="操作(act)">
                <select className="w-full rounded-lg border px-2.5 h-9 outline-none text-[12.5px]"
                  style={{ ...ui, background: C.panel, borderColor: C.border, color: C.text }}
                  value={addPolicyForm.act}
                  onChange={e => setAddPolicyForm({ ...addPolicyForm, act: e.target.value })}>
                  <option value="*">全部 (*)</option>
                  <option value="read">只读 (read)</option>
                  <option value="write">写入 (write)</option>
                </select>
              </Field>
            )}

            <div className="rounded-lg border px-3 py-2 flex items-center gap-2" style={{ borderColor: C.amber + "44", background: C.amber + "0a" }}>
              <AlertTriangle size={13} color={C.amber} />
              <span className="text-[10.5px]" style={{ ...mono, color: C.amber }}>
                新增策略会立即生效，请谨慎操作。此操作会写入审计日志。
              </span>
            </div>
          </div>
        </Modal>
      )}

      {/* ── 删除确认弹窗 ── */}
      {delTarget && (
        <Modal
          title="确认删除策略"
          icon={Trash2}
          color={C.red}
          onClose={() => setDelTarget(null)}
          w={420}
          footer={
            <>
              <Btn onClick={() => setDelTarget(null)}>取消</Btn>
              <Btn danger onClick={handleRemovePolicy}>确认删除</Btn>
            </>
          }>
          <div className="space-y-2 text-[12.5px]" style={ui}>
            <div>确认删除以下策略？此操作不可逆。</div>
            <div className="rounded-lg border p-3" style={{ background: C.panel, borderColor: C.border }}>
              <div className="text-[11px] mb-1.5" style={{ ...mono, color: C.muted }}>策略详情</div>
              <div className="space-y-1 text-[11px]" style={mono}>
                <div>
                  <span style={{ color: C.faint }}>类型：</span>
                  <Chip label={delTarget.ptype === "p" ? "P策略" : "G策略"} color={delTarget.ptype === "p" ? C.accent : C.violet} />
                </div>
                <div><span style={{ color: C.faint }}>sub：</span><span style={{ color: C.text }}>{delTarget.sub}</span></div>
                <div><span style={{ color: C.faint }}>obj：</span><span style={{ color: C.text }}>{delTarget.obj}</span></div>
                {delTarget.ptype === "p" && (
                  <div><span style={{ color: C.faint }}>act：</span><span style={{ color: C.text }}>{delTarget.act}</span></div>
                )}
              </div>
            </div>
          </div>
        </Modal>
      )}

      {/* ── 批量改角色弹窗 ── */}
      {batchModal && (
        <Modal
          title={`批量改角色 · ${ROLE_LABELS[currentRoleGroup] || currentRoleGroup}`}
          icon={ShieldCheck}
          color={C.accent}
          onClose={() => { setBatchModal(false); setSelectedUsers([]); }}
          w={640}
          footer={
            <>
              <Btn onClick={() => { setBatchModal(false); setSelectedUsers([]); }}>取消</Btn>
              <Btn primary onClick={handleBatchRole}>
                将 {selectedUsers.length} 个用户改为 {ROLE_LABELS[batchRole] || batchRole || "目标角色"}
              </Btn>
            </>
          }>
          <div className="space-y-3.5">
            <div className="text-[11.5px]" style={{ ...mono, color: C.muted }}>
              以下是从当前角色组中可选的用户，勾选后将统一迁移至目标角色。
            </div>

            {/* 全选/取消 */}
            <div className="flex items-center gap-3">
              <label className="flex items-center gap-2 text-[11.5px] cursor-pointer" style={{ color: C.accent }}
                onClick={e => {
                  const allIds = usersByRole.find(u => u.role === currentRoleGroup)?.users.map((u: any) => u.id) || [];
                  setSelectedUsers(selectedUsers.length === allIds.length ? [] : allIds);
                  e.stopPropagation();
                }}>
                <input type="checkbox" readOnly
                  checked={(() => {
                    const allIds = usersByRole.find(u => u.role === currentRoleGroup)?.users.map((u: any) => u.id) || [];
                    return allIds.length > 0 && allIds.every((id: string) => selectedUsers.includes(id));
                  })()} />
                全选/取消
              </label>
              <span className="text-[10.5px]" style={{ color: C.faint }}>
                已选 {selectedUsers.length} / {usersByRole.find(u => u.role === currentRoleGroup)?.count || 0}
              </span>
            </div>

            {/* 用户列表 */}
            <div className="max-h-64 overflow-y-auto rounded-lg border" style={{ borderColor: C.border }}>
              {(usersByRole.find(u => u.role === currentRoleGroup)?.users || []).map((u: any) => (
                <label key={u.id} className="flex items-center gap-3 px-3 py-2 border-b cursor-pointer transition-colors"
                  style={{ borderColor: C.border }}
                  onMouseEnter={e => (e.currentTarget.style.background = "rgba(148,163,184,0.04)")}
                  onMouseLeave={e => (e.currentTarget.style.background = "transparent")}>
                  <input type="checkbox"
                    checked={selectedUsers.includes(u.id)}
                    onChange={() => toggleUserSelection(u.id)} />
                  <span style={{ color: C.text, fontWeight: 600, fontSize: 12 }}>{u.name}</span>
                  <span style={{ color: C.faint, fontSize: 11, ...mono }}>{u.account}</span>
                  <span className="flex-1" />
                  <Chip label={u.scenic_name || "集团"} color={u.scenic_name ? C.teal : C.violet} />
                </label>
              ))}
              {(!usersByRole.find(u => u.role === currentRoleGroup)?.users.length) && (
                <div className="py-6 text-center"><Empty text="该角色下暂无用户" /></div>
              )}
            </div>

            {/* 目标角色选择 */}
            <Field label="目标角色">
              <select className="w-full rounded-lg border px-2.5 h-9 outline-none text-[12.5px]"
                style={{ ...ui, background: C.panel, borderColor: C.border, color: C.text }}
                value={batchRole}
                onChange={e => setBatchRole(e.target.value)}>
                <option value="">选择目标角色…</option>
                {roleOptions.filter(r => r.value !== currentRoleGroup).map(r => (
                  <option key={r.value} value={r.value}>{r.label} ({r.value})</option>
                ))}
              </select>
            </Field>
          </div>
        </Modal>
      )}
    </div>
  );
}
