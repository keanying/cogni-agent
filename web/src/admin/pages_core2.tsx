import { useState, useMemo } from "react";
import {
  UserCog, KeyRound, Smartphone, MapPin, Plus, Search, Pencil, Trash2, Share2,
  Globe, Lock, Users2, Wrench, AlertTriangle, ChevronRight, Zap, Copy, Ban, Sparkles, Play,
} from "lucide-react";
import { C, mono, ui, shadow, Chip, Card, Btn, IconBtn, Input, Modal, Field, PageHead, Th, Td, Row, Empty, ConfirmBar, useToast } from "./kit";

/* ══════════ 共享设置弹窗(Tool/Skill 通用,真实state) ══════════ */
type Vis = "private" | "org" | "public";
function ShareModal({ name, vis, onChange, onClose }: { name: string; vis: Vis; onChange: (v: Vis) => void; onClose: () => void }) {
  const toast = useToast();
  const [grants, setGrants] = useState([{ type: "user", label: "李强", perm: "可编辑" }, { type: "scenic", label: "鼓浪屿景区 · 全员", perm: "景区全员授权" }]);
  const visOpts: { v: Vis; label: string; icon: any; c: string }[] = [
    { v: "private", label: "私有(仅创建者)", icon: Lock, c: C.muted },
    { v: "org", label: "本景区共享", icon: Users2, c: C.accent },
    { v: "public", label: "公共组件(全平台)", icon: Globe, c: C.green },
  ];
  return (
    <Modal title={`共享设置 · ${name}`} icon={Share2} color={C.violet} onClose={onClose} w={620}
      footer={<><Btn onClick={onClose}>取消</Btn><Btn primary onClick={() => { toast("共享设置已保存(G-AUTH)", "ok"); onClose(); }}>保存共享设置</Btn></>}>
      <div className="text-[11.5px] mb-4" style={{ ...mono, color: C.muted }}>G-AUTH-01~08 · 可见范围 + 定向授权,决定谁能在工具中心看到并调用该资源</div>
      <div className="text-[11px] tracking-widest mb-2 font-semibold" style={{ ...mono, color: C.faint }}>可见范围</div>
      <div className="grid grid-cols-3 gap-2 mb-4">
        {visOpts.map(o => {
          const I = o.icon;
          return (
            <button key={o.v} onClick={() => onChange(o.v)} className="rounded-lg border px-3 py-3 flex flex-col items-center gap-1.5 transition-all"
              style={{ borderColor: vis === o.v ? o.c : C.border, background: vis === o.v ? o.c + "14" : "transparent" }}>
              <I size={16} color={o.c} /><span className="text-[11.5px]" style={{ ...ui, color: vis === o.v ? o.c : C.muted, fontWeight: vis === o.v ? 700 : 400 }}>{o.label}</span>
            </button>
          );
        })}
      </div>
      {vis === "public" && (
        <div className="rounded-lg border px-3 py-2 mb-4 flex items-center gap-2" style={{ borderColor: C.amber + "44", background: C.amber + "0d" }}>
          <AlertTriangle size={13} color={C.amber} /><span className="text-[11.5px]" style={{ ...mono, color: C.amber }}>G-AUTH-04:设为公共组件需 admin/super_admin 权限确认,操作将记入审计日志</span>
        </div>
      )}
      <div className="text-[11px] tracking-widest mb-2 font-semibold" style={{ ...mono, color: C.faint }}>定向授权(可叠加,不改变上方可见范围)</div>
      <div className="space-y-2 mb-3">
        {grants.map((g, i) => (
          <div key={i} className="flex items-center justify-between rounded-lg border px-3 py-2" style={{ borderColor: g.type === "scenic" ? C.teal + "44" : C.border, background: g.type === "scenic" ? C.teal + "0a" : C.panel }}>
            <div className="flex items-center gap-2">
              {g.type === "user" ? <span className="w-6 h-6 rounded-full flex items-center justify-center text-[11px] font-bold" style={{ background: C.violet, color: "#fff" }}>{g.label[0]}</span> : <MapPin size={14} color={C.teal} />}
              <span className="text-[12.5px]" style={{ ...ui, color: C.text }}>{g.label}</span><Chip label={g.perm} color={g.type === "scenic" ? C.teal : C.accent} />
            </div>
            <IconBtn icon={Trash2} color={C.red} onClick={() => setGrants(grants.filter((_, j) => j !== i))} />
          </div>
        ))}
      </div>
      <div className="flex gap-2">
        <div className="flex-1"><Input icon={Search} placeholder="搜索用户,按人授权…" /></div>
        <Btn small onClick={() => { setGrants([...grants, { type: "scenic", label: "武夷山景区 · 全员", perm: "景区全员授权(G-AUTH-08)" }]); }}><MapPin size={12} />授权给景区</Btn>
      </div>
    </Modal>
  );
}

/* ══════════ Tool 工具中心 ══════════ */
type ToolRow = { name: string; desc: string; ver: string; risk: "low" | "medium" | "high"; status: "PUBLISHED" | "DRAFT"; calls: string; vis: Vis };
const SEED_TOOLS: ToolRow[] = [
  { name: "sql_query", desc: "业务库只读查询", ver: "v3", risk: "low", status: "PUBLISHED", calls: "12.4k", vis: "public" },
  { name: "crowd_api", desc: "景区客流查询 API", ver: "v1", risk: "medium", status: "PUBLISHED", calls: "3.1k", vis: "org" },
  { name: "send_notice", desc: "对外消息发送", ver: "v2", risk: "high", status: "PUBLISHED", calls: "208", vis: "private" },
  { name: "py_analyze", desc: "Python 数据分析", ver: "v5", risk: "medium", status: "DRAFT", calls: "—", vis: "org" },
];
const riskColor = { low: C.green, medium: C.amber, high: C.red };
const visMeta: Record<Vis, [any, string, string]> = { private: [Lock, C.muted, "私有"], org: [Users2, C.accent, "本景区"], public: [Globe, C.green, "公共"] };

function ToolsPage() {
  const toast = useToast();
  const [rows, setRows] = useState(SEED_TOOLS);
  const [q, setQ] = useState("");
  const [wizard, setWizard] = useState(false);
  const [wStep, setWStep] = useState(0);
  const [shareTarget, setShareTarget] = useState<string | null>(null);
  const [detailTarget, setDetailTarget] = useState<ToolRow | null>(null);
  const [testResult, setTestResult] = useState<null | { ok: boolean; msg: string }>(null);
  const [newName, setNewName] = useState("");
  const [newRisk, setNewRisk] = useState<"low" | "medium" | "high">("low");

  const filtered = useMemo(() => rows.filter(t => !q || t.name.includes(q) || t.desc.includes(q)), [rows, q]);
  const shareRow = rows.find(r => r.name === shareTarget);

  function finishWizard() {
    setRows([{ name: newName || "new_tool", desc: "新注册工具", ver: "v1", risk: newRisk, status: "DRAFT", calls: "—", vis: "private" }, ...rows]);
    toast(`工具「${newName || "new_tool"}」已提交,DRAFT 状态待沙箱验证`, "ok");
    setWizard(false); setWStep(0); setNewName("");
  }

  return (
    <div className="grid gap-4">
      <PageHead pid="P05" module="M5 · tool-center-service" title="Tool 工具" sub="M5-01/02/03/10/11 · 注册 → 风险分级 → 沙箱验证 → 发布" />
      <div className="flex items-center justify-between">
        <div style={{ width: 300 }}><Input icon={Search} placeholder="搜索工具…" value={q} onChange={(e: any) => setQ(e.target.value)} /></div>
        <Btn primary onClick={() => setWizard(true)}><Plus size={14} />注册工具</Btn>
      </div>
      <Card title={`工具列表 · ${filtered.length} 条`} extra={<span className="text-[10.5px]" style={{ ...mono, color: C.faint }}>G-AUTH-05:按可见范围+授权过滤</span>}>
        <table className="w-full"><thead><tr><Th>名称</Th><Th>描述</Th><Th>版本</Th><Th>风险</Th><Th>可见范围</Th><Th>状态</Th><Th>30日调用</Th><Th w={70}></Th></tr></thead>
          <tbody>{filtered.map(t => {
            const [VI, VC, VL] = visMeta[t.vis];
            return (
              <Row key={t.name}>
                <Td><span style={{ color: C.accent }}>{t.name}</span></Td><Td>{t.desc}</Td><Td>{t.ver}</Td>
                <Td><Chip label={t.risk} color={riskColor[t.risk]} /></Td>
                <Td><div className="flex items-center gap-1.5"><VI size={12} color={VC} /><span className="text-[11px]" style={{ ...mono, color: VC }}>{VL}</span></div></Td>
                <Td><Chip label={t.status} color={t.status === "PUBLISHED" ? C.green : C.muted} /></Td>
                <Td>{t.calls}</Td>
                <Td><div className="flex gap-1"><IconBtn icon={Share2} color={C.violet} onClick={() => setShareTarget(t.name)} title="共享设置" /><IconBtn icon={ChevronRight} onClick={() => { setDetailTarget(t); setTestResult(null); }} title="权限测试详情" /></div></Td>
              </Row>
            );
          })}</tbody>
        </table>
      </Card>

      {wizard && (
        <Modal title="注册工具" icon={Wrench} color={C.amber} onClose={() => { setWizard(false); setWStep(0); }} w={680}
          footer={wStep < 2
            ? <><Btn onClick={() => setWizard(false)}>取消</Btn><Btn primary onClick={() => setWStep(wStep + 1)}>下一步</Btn></>
            : <><Btn onClick={() => setWStep(1)}>上一步</Btn><Btn primary onClick={finishWizard}><Zap size={13} />提交并进入沙箱验证</Btn></>}>
          <div className="flex items-center gap-2 mb-4">
            {["① 定义", "② 安全", "③ 验证"].map((s, i) => (
              <span key={s} className="text-[12px] px-2.5 py-1 rounded-md transition-colors" style={{ ...mono, color: i === wStep ? "#fff" : i < wStep ? C.green : C.faint, background: i === wStep ? C.primary : i < wStep ? C.green + "18" : C.panel }}>{s}</span>
            ))}
          </div>
          {wStep === 0 && (
            <div className="grid grid-cols-2 gap-3.5 text-[12.5px]">
              <Field label="工具名称"><Input placeholder="如 crowd_api" value={newName} onChange={(e: any) => setNewName(e.target.value)} /></Field>
              <Field label="工具描述"><Input placeholder="一句话描述用途" /></Field>
              <div className="col-span-2"><Field label="入参 JSON Schema">
                <pre className="rounded-lg border p-3 text-[11.5px] leading-relaxed" style={{ ...mono, background: "#070a14", borderColor: C.border, color: "#7dd3fc" }}>{`{ "type":"object", "properties": {\n    "scenic_id": {"type":"string"},\n    "date": {"type":"string","format":"date"} },\n  "required": ["scenic_id"] }`}</pre>
              </Field></div>
            </div>
          )}
          {wStep === 1 && (
            <div className="space-y-3.5">
              <Field label="风险分级(M5-03:high 风险强制走 HITL)">
                <div className="flex gap-2">{(["low", "medium", "high"] as const).map(r => (
                  <button key={r} onClick={() => setNewRisk(r)} className="px-3 py-1.5 rounded-lg border text-[12px]" style={{ ...mono, borderColor: newRisk === r ? riskColor[r] : C.border, background: newRisk === r ? riskColor[r] + "18" : "transparent", color: newRisk === r ? riskColor[r] : C.muted }}>{r}</button>
                ))}</div>
              </Field>
              <Field label="凭证(M5-09,加密保管,Agent 不可见明文)"><Input placeholder="API Key / 密钥(可选)" /></Field>
            </div>
          )}
          {wStep === 2 && (
            <div className="rounded-lg border p-4" style={{ borderColor: C.green + "33", background: C.green + "08" }}>
              <div className="text-[12.5px] mb-2" style={{ ...ui, color: C.green, fontWeight: 700 }}>沙箱 Dry-run 模拟通过</div>
              <div className="text-[11.5px] space-y-1" style={{ ...mono, color: C.text }}>
                <div>✓ Schema 一致性校验通过</div><div>✓ 超时限额 · LimitRange 生效</div><div>✓ 出口白名单命中</div>
              </div>
            </div>
          )}
        </Modal>
      )}
      {detailTarget && (
        <Modal title={`权限测试 · ${detailTarget.name}`} icon={Wrench} color={C.amber} onClose={() => setDetailTarget(null)} w={620}
          footer={<Btn onClick={() => setDetailTarget(null)}>关闭</Btn>}>
          <div className="text-[11.5px] mb-4" style={{ ...mono, color: C.muted }}>
            工具调用前会做「双重校验」——先看这个用户能不能<b style={{ color: C.text }}>看见</b>这个工具(可见范围+定向授权),再看能不能<b style={{ color: C.text }}>调用</b>它(景区×角色×工具三元鉴权)。本面板用于测试:换一个用户/角色,模拟调用这个工具会不会被拦下。
          </div>
          <div className="grid grid-cols-2 gap-3.5 mb-4">
            <Field label="模拟用户(景区)"><select className="w-full rounded-lg border px-2.5 h-9 outline-none text-[12.5px]" style={{ ...ui, background: C.panel, borderColor: C.border, color: C.text }}>
              <option>李强 · 武夷山景区 · 景区管理员</option><option>王芳 · 鼓浪屿景区 · 客服主管</option><option>陈杰 · 鼓浪屿景区 · 运营专员(未被授权)</option>
            </select></Field>
            <Field label="尝试的操作"><select className="w-full rounded-lg border px-2.5 h-9 outline-none text-[12.5px]" style={{ ...ui, background: C.panel, borderColor: C.border, color: C.text }}>
              <option>调用(use)</option><option>编辑(edit)</option><option>管理/删除(manage)</option>
            </select></Field>
          </div>
          <Btn primary onClick={() => setTestResult(Math.random() > 0.4 ? { ok: true, msg: "校验通过:该用户所在景区拥有此工具的共享授权,且角色满足风险等级要求" } : { ok: false, msg: "拦截:该用户不在可见范围内,也没有单独的定向授权记录" })}>
            <Play size={13} />运行权限测试
          </Btn>
          {testResult && (
            <div className="mt-3.5 rounded-lg border p-3.5" style={{ borderColor: (testResult.ok ? C.green : C.red) + "44", background: (testResult.ok ? C.green : C.red) + "0a" }}>
              <div className="text-[12.5px] font-semibold mb-1" style={{ color: testResult.ok ? C.green : C.red }}>{testResult.ok ? "✓ 允许调用" : "✕ 拦截调用"}</div>
              <div className="text-[11.5px]" style={{ ...mono, color: C.text }}>{testResult.msg}</div>
            </div>
          )}
        </Modal>
      )}
      {shareTarget && shareRow && (
        <ShareModal name={shareTarget} vis={shareRow.vis} onChange={(v) => setRows(rows.map(r => r.name === shareTarget ? { ...r, vis: v } : r))} onClose={() => setShareTarget(null)} />
      )}
    </div>
  );
}

export { ToolsPage, ShareModal, visMeta, riskColor };
export type { Vis };
