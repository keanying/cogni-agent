import { useState, useEffect, useRef } from "react";
import {
  LayoutDashboard, MessageSquare, Wrench, Boxes, Plug, BookOpen, ListTodo,
  UserCheck, FlaskConical, Settings, ChevronRight, ChevronDown, PanelLeftClose, PanelLeftOpen,
  UserCog, KeyRound, ScrollText, Sparkles, Bell, ChevronsUpDown, LogOut, Zap, MapPin,
  Loader2, XCircle,
} from "lucide-react";
import { C, mono, ui, shadow, shadowLg, Chip, ToastCtx, ToastHost, Btn, Input } from "./kit";
import { Dashboard, ChatPage } from "./pages_core";
import { ToolsPage } from "./pages_core2";
import {
  SkillsPage, McpPage, AgentHubPage, KnowledgePage, TasksPage,
  ApprovalPage, EvalPage, ModelBudgetPage, AuditExportPage, SystemLogPage,
} from "./pages_rest";
import MemberListPage from "./pages/MemberList";
import RolePermissionPage from "./pages/RolePermission";
import { logout, loginByPhonePassword, loginByAccountPassword } from "../lib/api";

/* ══════════ 角色标签 ══════════ */
const ROLE_LABELS: Record<string, string> = {
  super_admin: "超级管理员",
  group_admin: "集团管理员",
  group_developer: "集团开发者",
  group_viewer: "集团查看者",
  scenic_user: "景区用户",
};

const NAV = [
  { id: "dash", label: "工作台", icon: LayoutDashboard, pid: "P01", el: <Dashboard /> },
  { id: "chat", label: "对话", icon: MessageSquare, pid: "P01b", el: <ChatPage /> },
  {
    id: "usercenter", label: "用户中心", icon: UserCog, pid: "M1·M2",
    children: [
      { id: "users", label: "用户管理", pid: "P03", el: <MemberListPage /> },
    ],
  },
  {
    id: "toolscenter", label: "工具中心", icon: Wrench, pid: "M5",
    children: [
      { id: "tools", label: "Tool 工具", pid: "P05", el: <ToolsPage /> },
      { id: "skills", label: "Skill 技能", pid: "P06", el: <SkillsPage /> },
      { id: "mcp", label: "MCP 接入", pid: "P07", el: <McpPage /> },
      { id: "agents", label: "Agent 注册中心", pid: "P08", el: <AgentHubPage /> },
    ],
  },
  {
    id: "knowledge", label: "知识与任务", icon: BookOpen, pid: "M3·M6",
    children: [
      { id: "kb", label: "知识中心", pid: "P09", el: <KnowledgePage /> },
      { id: "tasks", label: "任务中心", pid: "P10", el: <TasksPage /> },
      { id: "hitl", label: "审批中心", pid: "P11", el: <ApprovalPage /> },
      { id: "eval", label: "评测中心", pid: "P12", el: <EvalPage /> },
    ],
  },
  {
    id: "settings", label: "系统设置", icon: Settings, pid: "M7",
    children: [
      { id: "roleperm", label: "角色与权限", pid: "P13", el: <RolePermissionPage /> },
      { id: "modelbudget", label: "模型与预算", pid: "P14", el: <ModelBudgetPage /> },
      { id: "syslog", label: "系统日志", pid: "P15", el: <SystemLogPage /> },
      { id: "audit", label: "审计导出", pid: "P16", el: <AuditExportPage /> },
    ],
  },
];
const flatNav = NAV.reduce((acc: any[], n: any) => {
  if (n.children) acc.push(...n.children);
  else acc.push(n);
  return acc;
}, []);
const findGroup = (id: string) => NAV.find((n: any) => n.children?.some((c: any) => c.id === id)) as any;

/* ══════════ JWT 解码(仅解析 payload,不做签名校验) ══════════ */
function parseJwt(token: string): { sub: string; role: string; scenic_id: string; exp?: number } | null {
  try {
    const payload = token.split(".")[1];
    const json = atob(payload);
    return JSON.parse(json);
  } catch { return null; }
}

/* ══════════ 登录页(管理端:仅支持手机号+密码 / 账号+密码) ══════════ */
function LoginPage({ onLogin }: { onLogin: (role: string) => void }) {
  // 管理端二级 Tab: 手机号 / 账号
  const [pwMode, setPwMode] = useState<"phone" | "account">("phone");

  // 表单字段
  const [phone, setPhone] = useState("");
  const [account, setAccount] = useState("");
  const [password, setPassword] = useState("");

  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  // ── 切换模式时清空状态 ──
  function switchPwMode(m: "phone" | "account") {
    setPwMode(m); setError("");
    setPhone(""); setAccount(""); setPassword("");
  }

  // ── 提交 ──
  async function handleSubmit() {
    setError(""); setLoading(true);
    try {
      let data: { role?: string };

      if (pwMode === "phone") {
        if (!phone.trim() || !password.trim()) { setError("手机号和密码为必填项"); setLoading(false); return; }
        data = await loginByPhonePassword(phone.trim(), password.trim());
      } else {
        if (!account.trim() || !password.trim()) { setError("账号和密码为必填项"); setLoading(false); return; }
        data = await loginByAccountPassword(account.trim(), password.trim());
      }

      onLogin(data.role || "");
    } catch (e: any) {
      setError(e.message || "登录失败");
    } finally { setLoading(false); }
  }

  return (
    <div className="min-h-screen flex items-center justify-center px-4" style={{ background: `linear-gradient(135deg, ${C.bg} 0%, #0b1424 100%)` }}>
      <div className="w-full max-w-[420px]">
        {/* Logo */}
        <div className="text-center mb-10">
          <div className="inline-flex w-14 h-14 rounded-2xl items-center justify-center mb-5"
            style={{ background: `linear-gradient(135deg, ${C.primary}, ${C.accent})`, boxShadow: `0 8px 32px rgba(99,102,241,0.3)` }}>
            <Zap size={28} color="#fff" />
          </div>
          <div className="text-[22px] font-bold tracking-tight" style={{ color: C.text }}>智旅 Agent</div>
          <div className="text-[11px] mt-1.5" style={{ ...mono, color: C.faint }}>zhilv-agent-core · 管理后台</div>
        </div>

        {/* 登录卡片 */}
        <div className="rounded-2xl border" style={{ background: C.card2, borderColor: C.border, boxShadow: shadowLg }}>
          <div className="p-7">
            <div className="space-y-4">
              {/* Tab: 手机号 / 账号 */}
              <div className="flex gap-1 rounded-lg p-0.5" style={{ background: C.bg }}>
                <button onClick={() => switchPwMode("phone")}
                  className="flex-1 py-1.5 rounded-md text-[12px] font-semibold transition-all duration-150"
                  style={{ color: pwMode === "phone" ? C.text : C.faint, background: pwMode === "phone" ? C.card2 : "transparent" }}>
                  手机号
                </button>
                <button onClick={() => switchPwMode("account")}
                  className="flex-1 py-1.5 rounded-md text-[12px] font-semibold transition-all duration-150"
                  style={{ color: pwMode === "account" ? C.text : C.faint, background: pwMode === "account" ? C.card2 : "transparent" }}>
                  账号
                </button>
              </div>

              {/* 手机号密码登录 */}
              {pwMode === "phone" && (
                <>
                  <div>
                    <label className="block text-[11px] mb-1.5 font-semibold tracking-wide" style={{ color: C.muted }}>手机号</label>
                    <Input value={phone}
                      onChange={(e: any) => setPhone(e.target.value)}
                      placeholder="请输入手机号" autoFocus disabled={loading} />
                  </div>
                  <div>
                    <label className="block text-[11px] mb-1.5 font-semibold tracking-wide" style={{ color: C.muted }}>密码</label>
                    <Input type="password" value={password}
                      onChange={(e: any) => setPassword(e.target.value)} placeholder="请输入密码"
                      onKeyDown={(e: any) => e.key === "Enter" && handleSubmit()} disabled={loading} />
                  </div>
                </>
              )}

              {/* 账号密码登录 */}
              {pwMode === "account" && (
                <>
                  <div>
                    <label className="block text-[11px] mb-1.5 font-semibold tracking-wide" style={{ color: C.muted }}>账号</label>
                    <Input value={account}
                      onChange={(e: any) => setAccount(e.target.value)}
                      placeholder="请输入账号名" autoFocus disabled={loading} />
                  </div>
                  <div>
                    <label className="block text-[11px] mb-1.5 font-semibold tracking-wide" style={{ color: C.muted }}>密码</label>
                    <Input type="password" value={password}
                      onChange={(e: any) => setPassword(e.target.value)} placeholder="请输入密码"
                      onKeyDown={(e: any) => e.key === "Enter" && handleSubmit()} disabled={loading} />
                  </div>
                </>
              )}
            </div>

            {/* 错误信息 */}
            {error && (
              <div className="mt-4 text-[12px] rounded-lg px-3.5 py-2.5 flex items-center gap-2"
                style={{ background: `${C.red}14`, color: C.red, border: `1px solid ${C.red}30` }}>
                <XCircle size={14} />{error}
              </div>
            )}

            {/* 提交按钮 */}
            <button onClick={handleSubmit} disabled={loading}
              className="w-full rounded-lg py-2.5 mt-5 text-[14px] font-bold flex items-center justify-center gap-2 transition-all duration-150"
              style={{
                background: loading ? C.primary + "60" : `linear-gradient(135deg, ${C.primary}, ${C.primaryHi})`,
                color: "#fff", boxShadow: loading ? "none" : `0 4px 16px rgba(99,102,241,0.35)`,
                cursor: loading ? "default" : "pointer", opacity: loading ? 0.7 : 1,
              }}>
              {loading ? <Loader2 size={16} className="animate-spin" /> : null}
              {loading ? "登录中…" : "登录"}
            </button>
          </div>
        </div>

        <div className="text-center mt-5 text-[11px]" style={{ color: C.faint }}>
          忘记密码请联系管理员
        </div>
      </div>
    </div>
  );
}

/* ══════════ 主应用 ══════════ */
export default function AdminApp() {
  // 从 URL 路径恢复导航状态
  const getInitialActive = () => {
    const path = window.location.pathname.replace(/^\//, "");
    if (path && flatNav.some((n: any) => n.id === path)) return path;
    return "dash";
  };
  const [active, setActive] = useState(getInitialActive);
  const [collapsed, setCollapsed] = useState(false);
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(new Set(["toolscenter", "knowledge"]));
  const [toasts, setToasts] = useState<{ id: number; msg: string; kind: string }[]>([]);
  const [menuOpen, setMenuOpen] = useState(false);
  const [loggedIn, setLoggedIn] = useState(false);
  const [userName, setUserName] = useState("");
  const [userRole, setUserRole] = useState("");
  const idRef = useRef(0);
  const cur = flatNav.find((n: any) => n.id === active) as any;

  function toast(msg: string, kind: "ok" | "err" | "info" = "info") {
    const id = ++idRef.current;
    setToasts(t => [...t, { id, msg, kind }]);
    setTimeout(() => setToasts(t => t.filter(x => x.id !== id)), 3200);
  }

  // 检查是否已有有效 token(过期自动跳过)
  useEffect(() => {
    const token = localStorage.getItem("zhilv_access_token");
    if (token) {
      const claims = parseJwt(token);
      // exp 为秒级 Unix 时间戳
      if (claims && claims.exp && claims.exp * 1000 > Date.now()) {
        setUserName(ROLE_LABELS[claims.role] || claims.role);
        setUserRole(claims.role);
        setLoggedIn(true);
        return;
      }
    }
    setLoggedIn(false);
  }, []);

  // 监听 token 过期触发的全局强制登出事件
  useEffect(() => {
    const h = () => handleLogout();
    window.addEventListener("zhilv:force-logout", h);
    return () => window.removeEventListener("zhilv:force-logout", h);
  }, []);

  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if (e.key === "[" && (e.metaKey || e.ctrlKey)) setCollapsed(c => !c);
    };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, []);

  // 自动展开包含当前active的菜单组
  useEffect(() => {
    for (const n of NAV) {
      if ((n as any).children && (n as any).children.some((c: any) => c.id === active)) {
        setExpandedGroups(g => new Set([...g, n.id]));
        break;
      }
    }
  }, [active]);

  // 登录成功回调
  function handleLogin(role: string) {
    setUserRole(role);
    setUserName(ROLE_LABELS[role] || role);
    setLoggedIn(true);
  }

  // 退出登录
  function handleLogout() {
    logout();
    setLoggedIn(false);
    setUserName("");
    setUserRole("");
    setActive("dash");
  }

  // 未登录 → 登录页
  if (!loggedIn) {
    return <LoginPage onLogin={handleLogin} />;
  }

  return (
    <ToastCtx.Provider value={toast}>
      <div className="min-h-screen flex" style={{ background: C.bg, ...ui }}>
        {/* ── Sidebar ── */}
        <aside className="flex-shrink-0 border-r flex flex-col transition-all duration-200" style={{ width: collapsed ? 60 : 218, background: C.panel, borderColor: C.border }}>
          <div className="flex items-center gap-2 px-3 py-4" style={{ minHeight: 60 }}>
            <span className="w-8 h-8 rounded-lg flex items-center justify-center flex-shrink-0" style={{ background: `linear-gradient(135deg, ${C.primary}, ${C.accent})` }}><Zap size={16} color="#fff" /></span>
            {!collapsed && <div className="overflow-hidden"><div className="text-[14.5px] font-bold whitespace-nowrap" style={{ color: C.text }}>智旅 Agent</div>
              <div className="text-[9.5px] whitespace-nowrap" style={{ ...mono, color: C.faint }}>zhilv-agent-core · v1.2</div></div>}
          </div>
          <div className="flex-1 overflow-y-auto px-2.5 pb-3">
            {NAV.map((n: any) => {
              // 有 children 的是可展开的菜单组
              if (n.children) {
                const isExpanded = expandedGroups.has(n.id);
                const hasActive = n.children.some((c: any) => c.id === active);
                const toggleGroup = () => {
                  setExpandedGroups(g => {
                    const next = new Set(g);
                    if (next.has(n.id)) next.delete(n.id); else next.add(n.id);
                    return next;
                  });
                };
                return (
                  <div key={n.id} className="mb-0.5">
                    <button onClick={toggleGroup} title={collapsed ? n.label : undefined}
                      className="w-full flex items-center gap-2.5 px-2.5 py-2 rounded-lg text-[12.5px] transition-all duration-150"
                      style={{ fontWeight: hasActive ? 700 : 600, color: hasActive ? C.text : C.muted }}
                      onMouseEnter={e => !hasActive && (e.currentTarget.style.background = "rgba(148,163,184,0.06)")}
                      onMouseLeave={e => !hasActive && (e.currentTarget.style.background = "transparent")}>
                      <n.icon size={15} className="flex-shrink-0" />
                      {!collapsed && <>
                        <span className="flex-1 text-left whitespace-nowrap overflow-hidden text-ellipsis">{n.label}</span>
                        {isExpanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                      </>}
                    </button>
                    {isExpanded && !collapsed && (
                      <div className="ml-2.5 border-l" style={{ borderColor: C.border }}>
                        {n.children.map((c: any) => (
                          <button key={c.id} onClick={() => { setActive(c.id); window.history.pushState({}, '', `/${c.id}`); }}
                            className="w-full flex items-center gap-2.5 pl-6 pr-2.5 py-1.5 rounded-r-lg text-[12px] mb-0.5 relative transition-all duration-150"
                            style={{ fontWeight: active === c.id ? 700 : 500, color: active === c.id ? "#fff" : C.muted, background: active === c.id ? C.primary : "transparent" }}
                            onMouseEnter={e => active !== c.id && (e.currentTarget.style.background = "rgba(148,163,184,0.06)")}
                            onMouseLeave={e => active !== c.id && (e.currentTarget.style.background = "transparent")}>
                            {active === c.id && <span className="absolute left-0 top-1.5 bottom-1.5 w-[3px] rounded-full" style={{ background: C.accent }} />}
                            <span className="whitespace-nowrap overflow-hidden text-ellipsis">{c.label}</span>
                          </button>
                        ))}
                      </div>
                    )}
                    {/* Collapsed mode: simple separator */}
                    {collapsed && <div className="border-t my-1 mx-1" style={{ borderColor: C.border, opacity: 0.5 }} />}
                  </div>
                );
              }
              // 独立页面项
              return (
                <button key={n.id} onClick={() => { setActive(n.id); window.history.pushState({}, '', `/${n.id}`); }} title={collapsed ? n.label : undefined}
                  className="w-full flex items-center gap-2.5 px-2.5 py-2 rounded-lg text-[12.5px] mb-0.5 relative transition-all duration-150"
                  style={{ fontWeight: active === n.id ? 700 : 500, color: active === n.id ? "#fff" : C.muted, background: active === n.id ? C.primary : "transparent" }}
                  onMouseEnter={e => active !== n.id && (e.currentTarget.style.background = "rgba(148,163,184,0.06)")}
                  onMouseLeave={e => active !== n.id && (e.currentTarget.style.background = "transparent")}>
                  {active === n.id && <span className="absolute left-0 top-1.5 bottom-1.5 w-[3px] rounded-full" style={{ background: C.accent }} />}
                  <n.icon size={15} className="flex-shrink-0" />
                  {!collapsed && <span className="whitespace-nowrap overflow-hidden text-ellipsis">{n.label}</span>}
                </button>
              );
            })}
          </div>
          <button onClick={() => setCollapsed(c => !c)} className="mx-2.5 mb-3 flex items-center gap-2 px-2.5 py-2 rounded-lg text-[11.5px] transition-colors" style={{ color: C.faint, background: "rgba(148,163,184,0.05)" }}>
            {collapsed ? <PanelLeftOpen size={14} /> : <PanelLeftClose size={14} />}
            {!collapsed && <span style={mono}>折叠(⌘[)</span>}
          </button>
        </aside>

        {/* ── Main ── */}
        <main className="flex-1 min-w-0 flex flex-col">
          <header className="h-[56px] border-b flex items-center justify-between px-5 flex-shrink-0" style={{ borderColor: C.border, background: "rgba(14,20,36,0.85)", backdropFilter: "blur(8px)" }}>
            <div className="flex items-center gap-2 text-[13px]">
              {(() => {
                const group = findGroup(active);
                if (group) {
                  return <>
                    <span style={{ ...mono, color: C.faint }}>{group.pid}</span>
                    <ChevronRight size={12} color={C.faint} />
                    <span style={{ color: C.text, fontWeight: 600 }}>{group.label}</span>
                    <ChevronRight size={12} color={C.faint} />
                    <span className="font-bold" style={{ color: C.text }}>{cur?.label}</span>
                  </>;
                }
                return <>
                  <span style={{ ...mono, color: C.faint }}>{cur?.pid}</span>
                  <ChevronRight size={12} color={C.faint} />
                  <span className="font-bold" style={{ color: C.text }}>{cur?.label}</span>
                </>;
              })()}
            </div>
            <div className="flex items-center gap-3">
              <Chip label="景区:武夷山 · scenic_id" color={C.teal} />
              <Chip label="实例 ×2 在线" color={C.green} />
              <div className="relative">
                <Bell size={16} color={C.muted} />
                <span className="absolute -top-1 -right-1 w-3.5 h-3.5 rounded-full flex items-center justify-center text-[8px] font-bold" style={{ background: C.red, color: "#fff" }}>3</span>
              </div>
              <div className="relative">
                <button onClick={() => setMenuOpen(m => !m)} className="flex items-center gap-1.5 pl-1 pr-1.5 py-1 rounded-lg transition-colors" style={{ background: menuOpen ? "rgba(148,163,184,0.08)" : "transparent" }}>
                  <span className="w-7 h-7 rounded-full flex items-center justify-center text-[11px] font-bold" style={{ background: C.rose, color: "#fff" }}>P</span>
                  <div className="text-left"><div className="text-[11.5px] font-semibold leading-none" style={{ color: C.text }}>{userName}</div><div className="text-[9.5px] leading-none mt-0.5" style={{ ...mono, color: C.rose }}>{userRole}</div></div>
                  <ChevronsUpDown size={12} color={C.faint} />
                </button>
                {menuOpen && (
                  <div className="absolute right-0 top-11 w-48 rounded-lg border py-1.5 z-50" style={{ background: C.card2, borderColor: C.borderStrong, boxShadow: shadow }} onMouseLeave={() => setMenuOpen(false)}>
                    <div className="px-3 py-2 border-b" style={{ borderColor: C.border }}><div className="text-[11px]" style={{ ...mono, color: C.faint }}>当前角色</div></div>
                    <button className="w-full flex items-center gap-2 px-3 py-2 text-[12px] hover:bg-white/5" style={{ color: C.text }}><Settings size={13} />账号设置</button>
                    <button onClick={handleLogout} className="w-full flex items-center gap-2 px-3 py-2 text-[12px] hover:bg-white/5" style={{ color: C.red }}><LogOut size={13} />退出登录</button>
                  </div>
                )}
              </div>
            </div>
          </header>
          <div className="flex-1 overflow-y-auto p-5" key={active} style={{ animation: "pageIn .25s ease" }}>
            {cur?.el}
          </div>
        </main>
        <ToastHost items={toasts} />
        <style>{`
          @keyframes pageIn { from { opacity: 0; transform: translateY(6px);} to { opacity: 1; transform: translateY(0);} }
          ::-webkit-scrollbar { width: 8px; height: 8px; }
          ::-webkit-scrollbar-thumb { background: rgba(148,163,184,0.18); border-radius: 4px; }
          ::-webkit-scrollbar-track { background: transparent; }
        `}</style>
      </div>
    </ToastCtx.Provider>
  );
}
