import { useState, useMemo, useRef, useEffect } from "react";
import {
  LayoutDashboard, MessageSquare, Wrench, Boxes, Plug, BookOpen, ListTodo,
  UserCheck, FlaskConical, Settings, ChevronRight, ChevronDown, Search, Plus, Play,
  CircleDot, Clock, CheckCircle2, XCircle, AlertTriangle, Shield, Zap, Send,
  PanelRightOpen, PanelLeftClose, PanelLeftOpen, RefreshCw, UserCog, KeyRound,
  ScrollText, Sparkles, Bell, ChevronsUpDown, LogOut, Smartphone,
} from "lucide-react";
import { C, mono, ui, shadow, Chip, Card, Btn, IconBtn, Input, PageHead, Th, Td, Row, useToast } from "./kit";

/* ── Dashboard ── */
function Dashboard() {
  const toast = useToast();
  const kpi = [
    { label: "今日任务", value: "1,284", delta: "+12.4%", c: C.accent, icon: ListTodo },
    { label: "任务成功率", value: "97.2%", delta: "+0.8%", c: C.green, icon: CheckCircle2 },
    { label: "Token 成本(今日)", value: "¥86.40", delta: "-6.2%", c: C.violet, icon: Zap },
    { label: "待审批 HITL", value: "3", delta: "2 条超 4h", c: C.orange, icon: AlertTriangle },
  ];
  const trend = [42, 55, 48, 70, 66, 82, 78, 95, 88, 104, 98, 121];
  const max = Math.max(...trend);
  const pts = trend.map((v, i) => `${20 + i * 46},${112 - (v / max) * 92}`).join(" ");
  const modes = [{ l: "L0 直接调用", v: 62, c: C.green }, { l: "L1 工作流", v: 23, c: C.accent }, { l: "L2 单Agent", v: 15, c: C.amber }];
  let acc = 0;
  const donut = modes.map(m => { const a0 = acc; acc += m.v; return { ...m, a0, a1: acc }; });
  const arc = (a0: number, a1: number) => {
    const r = 44, cx = 60, cy = 60, s = (a0 / 100) * 2 * Math.PI - Math.PI / 2, e = (a1 / 100) * 2 * Math.PI - Math.PI / 2;
    return `M ${cx + r * Math.cos(s)} ${cy + r * Math.sin(s)} A ${r} ${r} 0 ${a1 - a0 > 50 ? 1 : 0} 1 ${cx + r * Math.cos(e)} ${cy + r * Math.sin(e)}`;
  };
  const tasks = [
    ["T-20481", "行程规划:武夷山两日", "L2", "COMPLETED", "9 步 · ¥0.18", C.green],
    ["T-20480", "本周舆情周报", "L1", "WAITING_HUMAN", "待审批:对外发送", C.orange],
    ["T-20479", "景区客流查询", "L0", "COMPLETED", "1 步 · ¥0.01", C.green],
    ["T-20478", "门票销量归因分析", "L2", "RUNNING", "循环 4/25", C.accent],
    ["T-20477", "FAQ 知识问答", "L0", "FAILED", "预算熔断", C.red],
  ];
  return (
    <div className="grid gap-4">
      <div className="grid grid-cols-2 xl:grid-cols-4 gap-4">
        {kpi.map((k, i) => (
          <div key={k.label} className="rounded-xl border p-4 transition-all duration-200 hover:-translate-y-0.5"
            style={{ background: C.card, borderColor: C.border, boxShadow: shadow, animation: `riseIn .4s ease ${i * 0.05}s both` }}>
            <div className="flex items-center justify-between mb-2">
              <span className="text-[11.5px] font-medium" style={{ ...ui, color: C.muted }}>{k.label}</span>
              <k.icon size={14} color={k.c} style={{ opacity: 0.7 }} />
            </div>
            <div className="text-[26px] font-bold tracking-tight" style={{ ...ui, color: k.c }}>{k.value}</div>
            <div className="text-[10.5px] mt-1" style={{ ...mono, color: C.faint }}>{k.delta}</div>
          </div>
        ))}
      </div>
      <div className="grid xl:grid-cols-[1fr_300px] gap-4">
        <Card title="任务趋势 · 近 12 小时" extra={<IconBtn icon={RefreshCw} onClick={() => toast("已刷新趋势数据", "ok")} />}>
          <svg viewBox="0 0 540 124" className="w-full">
            <defs><linearGradient id="fade" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor={C.accent} stopOpacity="0.25" /><stop offset="100%" stopColor={C.accent} stopOpacity="0" /></linearGradient></defs>
            <polyline points={`20,112 ${pts} ${20 + 11 * 46},112`} fill="url(#fade)" stroke="none" />
            <polyline points={pts} fill="none" stroke={C.accent} strokeWidth="2" strokeLinejoin="round" />
            {trend.map((v, i) => <circle key={i} cx={20 + i * 46} cy={112 - (v / max) * 92} r="2.5" fill={C.accent} />)}
          </svg>
        </Card>
        <Card title="运行模式分布">
          <div className="flex items-center gap-4">
            <svg width="120" height="120"><circle cx="60" cy="60" r="44" fill="none" stroke={C.border} strokeWidth="12" />
              {donut.map(d => <path key={d.l} d={arc(d.a0, d.a1)} fill="none" stroke={d.c} strokeWidth="12" strokeLinecap="round" />)}
            </svg>
            <div className="space-y-2">
              {modes.map(m => (
                <div key={m.l} className="flex items-center gap-2 text-[12px]" style={mono}>
                  <span className="w-2 h-2 rounded-full" style={{ background: m.c }} />
                  <span style={{ color: C.text }}>{m.l}</span><span style={{ color: C.faint }}>{m.v}%</span>
                </div>
              ))}
            </div>
          </div>
        </Card>
      </div>
      <Card title="最近任务" extra={<Btn small onClick={() => toast("已刷新任务列表", "ok")}><RefreshCw size={12} />刷新</Btn>}>
        <table className="w-full"><thead><tr><Th>ID</Th><Th>任务</Th><Th>模式</Th><Th>状态</Th><Th>摘要</Th></tr></thead>
          <tbody>{tasks.map(t => (
            <Row key={t[0]}><Td>{t[0]}</Td><Td>{t[1]}</Td><Td><Chip label={t[2] as string} color={C.violet} /></Td>
              <Td><Chip label={t[3] as string} color={t[5] as string} /></Td><Td><span style={{ color: C.faint }}>{t[4]}</span></Td></Row>
          ))}</tbody>
        </table>
      </Card>
      <style>{`@keyframes riseIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}`}</style>
    </div>
  );
}

/* ── Chat ── */
function ChatPage() {
  const [drawer, setDrawer] = useState(true);
  const [msg, setMsg] = useState("");
  const [sent, setSent] = useState(false);
  const toast = useToast();
  const steps = [
    { t: "意图识别", d: "行程规划 · 需实时数据", c: C.green, done: true },
    { t: "模式判定 → L2", d: "L0✗ L1✗ L2✓ · 护栏就位", c: C.amber, done: true },
    { t: "RAG 检索", d: "向量知识 · 召回 6 分块", c: C.teal, done: true },
    { t: "工具调用", d: "sql_query · risk=low", c: C.amber, done: true },
    { t: "沙箱执行", d: "rows=42 · 1.2s", c: C.muted, done: true },
    { t: "生成中…", d: "LLM 流式输出", c: C.accent, done: false },
  ];
  return (
    <div className="grid h-[calc(100vh-148px)] gap-0 rounded-xl border overflow-hidden" style={{ borderColor: C.border, boxShadow: shadow, gridTemplateColumns: drawer ? "220px 1fr 300px" : "220px 1fr" }}>
      <div className="border-r p-3 space-y-1.5 overflow-y-auto" style={{ background: C.panel, borderColor: C.border }}>
        <Btn primary small onClick={() => toast("已新建会话", "ok")}><Plus size={13} />新会话</Btn>
        {["武夷山行程规划", "本周舆情周报", "客流数据问答"].map((s, i) => (
          <div key={s} className="px-2.5 py-2.5 rounded-lg text-[12.5px] cursor-pointer transition-all"
            style={{ ...ui, color: i === 0 ? C.text : C.muted, background: i === 0 ? C.primary + "20" : "transparent", border: i === 0 ? `1px solid ${C.primary}44` : "1px solid transparent" }}>{s}</div>
        ))}
      </div>
      <div className="flex flex-col" style={{ background: C.bg }}>
        <div className="px-4 py-3 border-b flex items-center justify-between flex-shrink-0" style={{ borderColor: C.border }}>
          <span className="text-[13px] font-semibold" style={{ ...ui, color: C.text }}>武夷山行程规划</span>
          <IconBtn icon={PanelRightOpen} onClick={() => setDrawer(!drawer)} />
        </div>
        <div className="flex-1 p-4 space-y-4 overflow-auto">
          <div className="flex justify-end"><div className="max-w-[70%] rounded-2xl rounded-tr-sm px-4 py-2.5 text-[13px]" style={{ ...ui, background: C.primary, color: "#fff" }}>帮我规划武夷山两日行程,预算 1500 以内</div></div>
          <div className="flex"><div className="max-w-[78%] rounded-2xl rounded-tl-sm px-4 py-2.5 text-[13px] leading-relaxed border" style={{ ...ui, background: C.card, borderColor: C.border, color: C.text }}>
            好的,已结合景区知识与实时票务为你规划:<br /><br /><b>Day 1</b> 天游峰(建议 8:00 前入园,余票充足)→ 九曲溪竹筏(14:00 场次余 42 张)…<span className="inline-block w-1.5 h-4 ml-0.5 align-middle" style={{ background: C.accent, animation: "blink 1s infinite" }} />
          </div></div>
        </div>
        <div className="p-3 border-t flex gap-2 flex-shrink-0" style={{ borderColor: C.border }}>
          <div className="flex-1"><Input placeholder="输入消息…" value={msg} onChange={(e: any) => setMsg(e.target.value)} /></div>
          <Btn primary onClick={() => { if (msg) { toast("消息已发送", "ok"); setMsg(""); } }}><Send size={14} />发送</Btn>
        </div>
      </div>
      {drawer && (
        <div className="border-l p-3.5 overflow-auto" style={{ background: C.panel, borderColor: C.border }}>
          <div className="text-[11px] tracking-widest mb-3 font-semibold" style={{ ...mono, color: C.faint }}>执行过程 · TASK T-20481</div>
          {steps.map((s, i) => (
            <div key={s.t} className="flex gap-2.5">
              <div className="flex flex-col items-center">
                <span className="w-5 h-5 rounded-full border flex items-center justify-center flex-shrink-0" style={{ borderColor: s.c, background: s.done ? s.c + "22" : "transparent" }}>
                  {s.done ? <CheckCircle2 size={12} color={s.c} /> : <CircleDot size={12} color={s.c} className="animate-pulse" />}
                </span>
                {i < steps.length - 1 && <span className="w-px flex-1 my-0.5" style={{ background: C.border }} />}
              </div>
              <div className="pb-3.5">
                <div className="text-[12.5px] font-semibold" style={{ ...ui, color: s.c }}>{s.t}</div>
                <div className="text-[10.5px]" style={{ ...mono, color: C.faint }}>{s.d}</div>
              </div>
            </div>
          ))}
          <div className="mt-2 rounded-lg border p-2.5" style={{ borderColor: C.orange + "40", background: C.orange + "0a" }}>
            <div className="text-[10.5px]" style={{ ...mono, color: C.orange }}>护栏:循环 2/25 · 工具 1/10 · Token 5.2k/50k</div>
          </div>
        </div>
      )}
      <style>{`@keyframes blink{50%{opacity:0}}`}</style>
    </div>
  );
}

export { Dashboard, ChatPage };
