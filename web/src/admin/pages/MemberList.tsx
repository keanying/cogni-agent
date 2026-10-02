import { useState, useEffect, useCallback, useMemo, useRef } from "react";
import {
  UserCog, Plus, Search, Pencil, KeyRound, Ban, CheckCircle,
  ChevronLeft, ChevronRight, RefreshCw, Loader2, MapPin, Building2,
  X, Trash2,
} from "lucide-react";
import {
  C, mono, ui, shadow,
  Card, Chip, Btn, IconBtn, Input, Modal, Field, PageHead,
  Th, Td, Row, Empty, useToast,
} from "../kit";
import {
  listUsers, createUser, updateUser, resetPassword, deleteUser,
  getScenics, getRoles, getGroupInfo,
  type MemberListParams,
} from "../../lib/api";
import { confirmDialog } from '../../components/ui/confirm';
import { invalidateCapabilities } from '../../lib/capabilities';

/* ══════════ 角色映射 ══════════ */
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

/* ══════════ 用户管理页 ══════════ */
export default function MemberListPage() {
  const toast = useToast();

  // 列表数据
  const [rows, setRows] = useState<any[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // 筛选
  const [keyword, setKeyword] = useState("");
  const [filterRole, setFilterRole] = useState("");
  const [filterScenic, setFilterScenic] = useState("");
  const [filterActive, setFilterActive] = useState<string>("");
  const [page, setPage] = useState(1);
  const pageSize = 20;

  // 弹窗
  const [showForm, setShowForm] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [deletingName, setDeletingName] = useState("");
  const [deletingSubmit, setDeletingSubmit] = useState(false);

  // 表单
  const [form, setForm] = useState({
    name: "",
    phone: "",
    scenic_id: "" as string | null,
    position: "",
  });

  // 下拉选项(从 API 加载)
  const [scenicOptions, setScenicOptions] = useState<{ scenic_id: string; scenic_name: string; is_enabled: boolean }[]>([]);
  const [roleOptions, setRoleOptions] = useState<{ value: string; label: string; scope: string }[]>([]);

  const searchTimer = useRef<ReturnType<typeof setTimeout>>();
  const searchKeyword = useRef("");

  // ── 加载景区/角色下拉 ──
  useEffect(() => {
    (async () => {
      try {
        const [sr, rr] = await Promise.all([getScenics({ page_size: 100 }), getRoles()]);
        setScenicOptions(sr.items || []);
        setRoleOptions(rr.roles || []);
      } catch (e) {
        // 下拉加载失败不阻塞页面
      }
    })();
  }, []);

  // ── 加载列表 ──
  const fetchList = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const params: MemberListParams = { page, page_size: pageSize };
      if (searchKeyword.current) params.keyword = searchKeyword.current;
      if (filterRole) params.role = filterRole;
      if (filterScenic) params.scenic_id = filterScenic;
      if (filterActive !== "") params.is_active = filterActive === "1";
      const res = await listUsers(params);
      setRows(res.items || []);
      setTotal(res.total || 0);
    } catch (e: any) {
      setError(e.message || "网络错误");
    } finally {
      setLoading(false);
    }
  }, [page, filterRole, filterScenic, filterActive]);

  useEffect(() => {
    fetchList();
  }, [fetchList]);

  // ── 搜索防抖 ──
  function handleSearch(v: string) {
    setKeyword(v);
    searchKeyword.current = v;
    clearTimeout(searchTimer.current);
    searchTimer.current = setTimeout(() => {
      setPage(1);
      fetchList();
    }, 350);
  }

  // ── 筛选变化 ──
  function handleFilterChange(field: "role" | "scenic" | "active", value: string) {
    if (field === "role") setFilterRole(value);
    if (field === "scenic") setFilterScenic(value);
    if (field === "active") setFilterActive(value);
    setPage(1);
    // 同步触发列表刷新(useEffect 依赖会在下一次渲染时触发)
    setTimeout(fetchList, 0);
  }

  // ── 打开新增弹窗 ──
  function openCreate() {
    setEditingId(null);
    setForm({ name: "", phone: "", scenic_id: null, position: "" });
    setShowForm(true);
  }

  // ── 打开编辑弹窗 ──
  function openEdit(row: any) {
    setEditingId(row.id);
    setForm({
      name: row.name || "",
      phone: row.phone || "",
      scenic_id: row.scenic_id || null,
      position: row.position || "",
    });
    setShowForm(true);
  }

  // ── 提交表单 ──
  async function handleSubmit() {
    // 新增模式:需要校验全部必填字段
    if (!editingId) {
      if (!form.name.trim()) { toast("请填写姓名", "err"); return; }
      if (!form.phone.trim()) { toast("请填写手机号", "err"); return; }
    } else {
      // 编辑模式:仅校验姓名(账号/手机号不可修改)
      if (!form.name.trim()) { toast("请填写姓名", "err"); return; }
    }

    setSubmitting(true);
    try {
      if (editingId) {
        await updateUser(editingId, {
          name: form.name,
          scenic_id: form.scenic_id || null,
          position: form.position || undefined,
        });
        // 同 UserCenter:改的可能就是自己的归属景区,能力缓存要跟着作废
        invalidateCapabilities();
        toast(`已更新成员「${form.name}」`, "ok");
        setShowForm(false);
        fetchList();
      } else {
        // 新建：账号后端自动生成，角色根据组织决定（选景区→scenic_user，选集团→group_viewer）
        const autoRole = form.scenic_id ? "scenic_user" : "group_viewer";
        const res = await createUser({
          name: form.name,
          phone: form.phone,
          scenic_id: form.scenic_id,
          role: autoRole,
          position: form.position || undefined,
        });
        toast(`已创建成员「${form.name}」，账号 ${res.account}，初始密码 ${res.initial_password || '见短信'}`, "ok");
        setShowForm(false);
        fetchList();
      }
    } catch (e: any) {
      toast(e.message || "操作失败", "err");
    } finally {
      setSubmitting(false);
    }
  }

  // ── 重置密码 ──
  async function handleResetPassword(id: string, name: string) {
    if (!await confirmDialog({ title: `重置「${name}」的密码`,
      message: '重置后会生成一个新密码,使用者首次登录被要求修改;此前签发的登录令牌全部失效。',
      confirmLabel: '重置密码', danger: true })) return;
    try {
      const res = await resetPassword(id);
      toast(`已为「${name}」重置密码为 ${res?.new_password || '系统初始密码'}`, "ok");
    } catch (e: any) {
      toast(e.message || "操作失败", "err");
    }
  }

  // ── 启用/禁用开关 ──
  async function handleToggleActive(row: any) {
    const newActive = !row.is_active;
    try {
      await updateUser(row.id, { is_active: newActive });
      toast(newActive ? `「${row.name}」已启用` : `「${row.name}」已禁用`, "ok");
      fetchList();
    } catch (e: any) {
      toast(e.message || "操作失败", "err");
    }
  }

  // ── 删除用户 ──
  async function handleDelete() {
    if (!deletingId) return;
    setDeletingSubmit(true);
    try {
      await deleteUser(deletingId);
      toast(`已删除用户「${deletingName}」`, "ok");
      setDeletingId(null);
      setDeletingName("");
      fetchList();
    } catch (e: any) {
      toast(e.message || "删除失败", "err");
    } finally {
      setDeletingSubmit(false);
    }
  }

  // ── 分页 ──
  const totalPages = Math.max(1, Math.ceil(total / pageSize));

  return (
    <div className="grid gap-4">
      <PageHead pid="P03" module="M2 · user-center-service" title="用户管理" sub="用户中心 · 管理平台所有成员账号" />

      {/* 搜索 + 筛选 + 操作 */}
      <div className="flex items-center gap-3 flex-wrap">
        <div style={{ width: 260 }}>
          <Input icon={Search} placeholder="搜索姓名/账号/手机号…" value={keyword} onChange={(e: any) => handleSearch(e.target.value)} />
        </div>
        {/* 角色筛选 */}
        <SelectFilter
          value={filterRole}
          onChange={v => handleFilterChange("role", v)}
          placeholder="全部角色"
          options={[
            { value: "", label: "全部角色" },
            ...roleOptions.map(r => ({ value: r.value, label: r.label })),
          ]}
        />
        {/* 景区筛选 */}
        <SelectFilter
          value={filterScenic}
          onChange={v => handleFilterChange("scenic", v)}
          placeholder="全部景区"
          options={[
            { value: "", label: "全部景区" },
            ...scenicOptions.map(s => ({ value: s.scenic_id, label: s.scenic_name })),
          ]}
        />
        {/* 状态筛选 */}
        <SelectFilter
          value={filterActive}
          onChange={v => handleFilterChange("active", v)}
          placeholder="全部状态"
          options={[
            { value: "", label: "全部状态" },
            { value: "1", label: "已启用" },
            { value: "0", label: "已禁用" },
          ]}
        />
        <div className="flex-1" />
        <Btn ghost onClick={fetchList}>
          <RefreshCw size={13} />刷新
        </Btn>
        <Btn primary onClick={openCreate}>
          <Plus size={14} />新增成员
        </Btn>
      </div>

      {/* 列表 */}
      <Card
        title={`成员列表 · ${total} 条`}
        extra={<span className="text-[10.5px]" style={{ ...mono, color: C.faint }}>G-ISO-03: 管理员仅见本组织范围</span>}
      >
        {error ? (
          <div className="py-10 text-center">
            <span className="text-[12.5px]" style={{ color: C.red }}>{error}</span>
            <div className="mt-2"><Btn small onClick={fetchList}>重试</Btn></div>
          </div>
        ) : loading && rows.length === 0 ? (
          <div className="py-16 flex items-center justify-center">
            <Loader2 size={24} className="animate-spin" color={C.faint} />
          </div>
        ) : rows.length === 0 ? (
          <Empty text="暂无成员数据" />
        ) : (
          <>
            <table className="w-full">
              <thead>
                <tr>
                  <Th>姓名</Th>
                  <Th>账号</Th>
                  <Th w={130}>手机号</Th>
                  <Th>所属组织</Th>
                  <Th>角色</Th>
                  <Th>状态</Th>
                  <Th w={160}>最近登录</Th>
                  <Th w={120}>操作</Th>
                </tr>
              </thead>
              <tbody>
                {rows.map(row => (
                  <Row key={row.id}>
                    <Td>
                      <div className="flex items-center gap-2">
                        <span className="w-6 h-6 rounded-full flex items-center justify-center text-[10px] font-bold flex-shrink-0"
                          style={{ background: (ROLE_COLORS[row.role] || C.violet), color: "#fff" }}>
                          {row.name?.[0] || "?"}
                        </span>
                        <span style={{ color: C.text, fontWeight: 600 }}>{row.name}</span>
                      </div>
                    </Td>
                    <Td><span style={{ color: C.accent }}>{row.account}</span></Td>
                    <Td>{row.phone}</Td>
                    <Td>
                      {row.scenic_name ? (
                        <div className="flex items-center gap-1"><MapPin size={11} color={C.teal} /><span style={{ color: C.teal }}>{row.scenic_name}</span></div>
                      ) : (
                        <Chip label="集团" color={C.violet} />
                      )}
                    </Td>
                    <Td>
                      <Chip label={ROLE_LABELS[row.role] || row.role} color={ROLE_COLORS[row.role] || C.muted} />
                    </Td>
                    <Td>
                      <button onClick={() => handleToggleActive(row)} className="flex items-center gap-1 text-[11px] transition-colors"
                        style={{ ...mono, color: row.is_active ? C.green : C.red }}>
                        {row.is_active ? <><CheckCircle size={11} />已启用</> : <><Ban size={11} />已禁用</>}
                      </button>
                    </Td>
                    <Td><span style={{ color: C.faint }}>{row.last_login_at || "从未登录"}</span></Td>
                    <Td>
                      <div className="flex gap-1">
                        <IconBtn icon={Pencil} onClick={() => openEdit(row)} title="编辑" />
                        <IconBtn icon={KeyRound} color={C.amber} onClick={() => handleResetPassword(row.id, row.name)} title="重置密码" />
                        <IconBtn icon={Trash2} color={C.red} onClick={() => { setDeletingId(row.id); setDeletingName(row.name); }} title="删除" />
                      </div>
                    </Td>
                  </Row>
                ))}
              </tbody>
            </table>

            {/* 分页 */}
            {totalPages > 1 && (
              <div className="flex items-center justify-between mt-4 pt-3 border-t" style={{ borderColor: C.border }}>
                <span className="text-[11.5px]" style={{ ...mono, color: C.faint }}>
                  第 {page}/{totalPages} 页，共 {total} 条
                </span>
                <div className="flex items-center gap-1.5">
                  <Btn small ghost disabled={page <= 1} onClick={() => setPage(p => Math.max(1, p - 1))}>
                    <ChevronLeft size={13} />上一页
                  </Btn>
                  {Array.from({ length: Math.min(totalPages, 7) }, (_, i) => {
                    let num: number;
                    if (totalPages <= 7) {
                      num = i + 1;
                    } else if (page <= 4) {
                      num = i + 1;
                    } else if (page >= totalPages - 3) {
                      num = totalPages - 6 + i;
                    } else {
                      num = page - 3 + i;
                    }
                    return (
                      <button key={num} onClick={() => setPage(num)}
                        className="w-7 h-7 rounded-md text-[11.5px] font-semibold transition-all"
                        style={{ ...mono, color: num === page ? "#fff" : C.muted, background: num === page ? C.primary : "transparent" }}>
                        {num}
                      </button>
                    );
                  })}
                  <Btn small ghost disabled={page >= totalPages} onClick={() => setPage(p => Math.min(totalPages, p + 1))}>
                    下一页<ChevronRight size={13} />
                  </Btn>
                </div>
              </div>
            )}
          </>
        )}
      </Card>

      {/* ── 新增/编辑弹窗 ── */}
      {showForm && (
        <Modal
          title={editingId ? `编辑成员 · ${form.name}` : "新增成员"}
          icon={editingId ? Pencil : UserCog}
          color={editingId ? C.accent : C.violet}
          onClose={() => setShowForm(false)}
          w={580}
          footer={
            <>
              <Btn onClick={() => setShowForm(false)}>取消</Btn>
              <Btn primary onClick={handleSubmit} loading={submitting} disabled={submitting}>
                {editingId ? "保存修改" : "确认新增"}
              </Btn>
            </>
          }
        >
          <div className="grid grid-cols-2 gap-3.5">
            <Field label="姓名"><Input value={form.name} onChange={(e: any) => setForm({ ...form, name: e.target.value })} placeholder="请输入姓名" /></Field>
            <Field label="手机号"><Input value={form.phone} onChange={(e: any) => setForm({ ...form, phone: e.target.value })} placeholder="用于登录与短信验证" /></Field>
            <Field label="职位"><Input value={form.position} onChange={(e: any) => setForm({ ...form, position: e.target.value })} placeholder="如:运营专员" /></Field>
            <Field label="所属组织">
              <select className="w-full rounded-lg border px-2.5 h-9 outline-none text-[12.5px] transition-all"
                style={{ ...ui, background: C.panel, borderColor: C.border, color: C.text }}
                value={form.scenic_id || ""}
                onChange={e => setForm({ ...form, scenic_id: e.target.value || null })}>
                <option value="">-- 如不需要请选择集团 --</option>
                {scenicOptions.map(s => (
                  <option key={s.scenic_id} value={s.scenic_id}>
                    {s.scenic_id}（{s.scenic_name}）{s.is_enabled ? "" : " · 已停用"}
                  </option>
                ))}
              </select>
              {/* 这里只列「已登记」的景区(景区名称来自主数据 dim_prd_scenic_base_yr_f)。
                  找不到想绑的景区,多半是它还没登记 —— 直接说清楚去哪儿登记,
                  否则只会看到一个短短的下拉,不知道是没数据还是没权限。 */}
              <div className="mt-1 text-[11px]" style={{ ...mono, color: C.faint }}>
                找不到景区?到「系统设置 · 组织管理」用「登记景区」从景区主数据里添加。
              </div>
            </Field>
          </div>
          {editingId && (
            <div className="mt-3.5 rounded-lg border p-3.5" style={{ borderColor: C.border, background: C.panel }}>
              <div className="text-[11.5px] font-semibold mb-2" style={{ ...ui, color: C.text }}>密码重置</div>
              <div className="text-[11px] mb-2.5" style={{ ...mono, color: C.faint }}>将该用户密码重置为系统初始密码，重置后立即生效，使用者首次登录会被要求修改</div>
              <Btn small onClick={() => handleResetPassword(editingId!, form.name)}>
                <KeyRound size={12} />重置密码
              </Btn>
            </div>
          )}
        </Modal>
      )}

      {/* ── 删除确认弹窗 ── */}
      {deletingId && (
        <Modal
          title="确认删除用户"
          icon={Trash2}
          color={C.red}
          onClose={() => { setDeletingId(null); setDeletingName(""); }}
          w={420}
          footer={
            <>
              <Btn onClick={() => { setDeletingId(null); setDeletingName(""); }}>取消</Btn>
              <Btn primary style={{ background: C.red, borderColor: C.red }} onClick={handleDelete} loading={deletingSubmit} disabled={deletingSubmit}>
                确认删除
              </Btn>
            </>
          }
        >
          <div className="grid gap-3">
            <div className="text-[13px]" style={{ color: C.text }}>
              确定要删除用户 <span className="font-bold" style={{ color: C.red }}>{deletingName}</span> 吗？
            </div>
            <div className="text-[11.5px] rounded-lg border p-3" style={{ borderColor: C.red + "40", background: C.red + "08", color: C.muted }}>
              此操作不可撤销，该用户的所有数据将被永久删除。
            </div>
          </div>
        </Modal>
      )}
    </div>
  );
}

/* ══════════ 筛选下拉组件 ══════════ */
function SelectFilter({ value, onChange, options, placeholder }: {
  value: string;
  onChange: (v: string) => void;
  options: { value: string; label: string }[];
  placeholder: string;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const h = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, []);

  const selected = options.find(o => o.value === value);

  return (
    <div ref={ref} className="relative">
      <button onClick={() => setOpen(o => !o)}
        className="rounded-lg border px-2.5 flex items-center gap-2 text-[12px] transition-all"
        style={{ ...mono, height: 32, background: C.panel, borderColor: C.border, color: selected?.value ? C.text : C.faint }}>
        <span className="truncate" style={{ maxWidth: 100 }}>{selected?.label || placeholder}</span>
        <span style={{ color: C.faint, fontSize: 9 }}>▼</span>
      </button>
      {open && (
        <div className="absolute top-full left-0 mt-1 w-44 rounded-lg border py-1 z-[100] max-h-56 overflow-y-auto"
          style={{ background: C.card2, borderColor: C.borderStrong, boxShadow: shadow }}>
          {options.map(o => (
            <button key={o.value}
              onClick={() => { onChange(o.value); setOpen(false); }}
              className="w-full text-left px-3 py-1.5 text-[12px] transition-colors"
              style={{ ...mono, color: o.value === value ? C.accent : C.muted, background: o.value === value ? C.accent + "10" : "transparent" }}
              onMouseEnter={e => o.value !== value && (e.currentTarget.style.background = "rgba(148,163,184,0.06)")}
              onMouseLeave={e => o.value !== value && (e.currentTarget.style.background = "transparent")}>
              {o.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
