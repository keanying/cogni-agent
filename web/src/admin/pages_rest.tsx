import { useState, useMemo, useEffect, useCallback } from "react";
import {
  Boxes, Plug, BookOpen, Database, Globe2, Images, ListTodo, UserCheck, FlaskConical,
  Settings, Plus, Search, Play, Share2, Users2, MapPin, Clock, CheckCircle2, XCircle,
  Sparkles, Copy, Ban, ChevronRight, AlertTriangle, ScrollText,
  Cpu, Server, Trash2, Pencil, Loader2, KeyRound,
} from "lucide-react";
import { C, mono, ui, shadow, Chip, Card, Btn, IconBtn, Input, Modal, Field, PageHead, Th, Td, Row, useToast } from "./kit";
import { ShareModal, visMeta } from "./pages_core2";
import {
  listModelProviders, createModelProvider, updateModelProvider, deleteModelProvider,
  listModelConfigs, createModelConfig, updateModelConfig, deleteModelConfig,
  ModelProviderItem, ModelConfigItem,
} from "../lib/api";
import { confirmDialog } from '../components/ui/confirm';

/* ══════════ Skill 技能 ══════════ */
function SkillsPage() {
  const [share, setShare] = useState(false);
  const [vis, setVis] = useState<any>("org");
  const [tab, setTab] = useState(0);
  const [chain, setChain] = useState(["行程规划师 v4", "票务查询 v2"]);
  const toast = useToast();
  const tabs = ["① 基本信息", "② Prompt 模板", "③ 工具绑定", "④ 编排", "⑤ 版本历史"];
  return (
    <div className="grid gap-4">
      <PageHead pid="P06" module="M5 · tool-center-service" title="Skill 技能" sub="M5-04/05/06 · Skill = 工具×Prompt×约束 · 支持多 Skill 编排" />
      <div className="flex justify-between items-center">
        <span className="text-[11px] tracking-widest font-semibold" style={{ ...mono, color: C.faint }}>SKILL = 工具 × PROMPT 模板 × 参数约束</span>
        <div className="flex gap-2"><IconBtn icon={Share2} color={C.violet} onClick={() => setShare(true)} title="共享设置" /><Btn primary onClick={() => toast("已创建 Skill 草稿", "ok")}><Plus size={14} />新建 Skill</Btn></div>
      </div>
      <div className="rounded-lg border px-3.5 py-2.5 flex items-center gap-3" style={{ borderColor: C.violet + "40", background: C.violet + "0a" }}>
        <Boxes size={15} color={C.violet} />
        <span className="text-[12px]" style={{ ...mono, color: C.text }}>
          单 Skill 可独立执行,也可<span style={{ color: C.violet, fontWeight: 700 }}> 编排多个 Skill</span>协同完成任务:
          <span style={{ color: C.accent }}> 固定编排(顺序设计时就定死)</span> 或 <span style={{ color: C.amber }}> 动态编排(交给 Agent 运行时自己判断)</span>
        </span>
      </div>
      <div className="flex gap-1.5 border-b" style={{ borderColor: C.border }}>
        {tabs.map((t, i) => (
          <button key={t} onClick={() => setTab(i)} className="px-3.5 py-2.5 text-[12.5px] border-b-2 -mb-px transition-colors" style={{ ...ui, fontWeight: i === tab ? 700 : 500, color: i === tab ? C.accent : C.muted, borderColor: i === tab ? C.accent : "transparent" }}>{t}</button>
        ))}
      </div>
      {tab === 0 && (
        <Card title="基本信息">
          <div className="grid grid-cols-2 gap-4 text-[12.5px]" style={ui}>
            <div><div style={{ color: C.muted }}>名称</div><div className="font-semibold mt-0.5" style={{ color: C.text }}>行程规划师 <Chip label="v4" color={C.violet} /></div></div>
            <div><div style={{ color: C.muted }}>可见范围</div><div className="flex items-center gap-1.5 mt-1"><Users2 size={13} color={C.accent} /><span style={{ ...mono, color: C.accent }}>本景区共享</span></div></div>
            <div><div style={{ color: C.muted }}>建议运行模式(判定链参考,非强制)</div><div className="mt-1"><Chip label="L2 ReAct" color={C.amber} /></div></div>
            <div><div style={{ color: C.muted }}>状态</div><div className="mt-1"><Chip label="PUBLISHED" color={C.green} /></div></div>
            <div className="col-span-2"><div style={{ color: C.muted }}>可用知识源(供 M6 知识路由使用:执行时按此勾选去对应知识源检索)</div>
              <div className="flex gap-4 mt-1.5">{[["数据库知识", true], ["网页搜索", false], ["向量知识", true]].map(([k, on]) => (
                <label key={k as string} className="flex items-center gap-2 text-[12px]" style={{ color: C.text }}><input type="checkbox" defaultChecked={on as boolean} readOnly />{k}</label>
              ))}</div>
            </div>
          </div>
        </Card>
      )}
      {tab === 1 && (
        <Card title="Prompt 模板(变量高亮编辑)" extra={<Btn small onClick={() => toast("试运行中…3.2s 后返回结果", "info")}><Play size={12} />试运行</Btn>}>
          <pre className="rounded-lg border p-3.5 text-[12px] leading-relaxed whitespace-pre-wrap" style={{ ...mono, background: "#070a14", borderColor: C.border, color: C.text }}>
你是{"{{scenic_name}}"}的资深行程规划师。{"\n"}用户预算:<span style={{ color: C.accent }}>{"{{budget}}"}</span> 元,行程天数:<span style={{ color: C.accent }}>{"{{days}}"}</span>{"\n\n"}规则:{"\n"}1. 必须先检索景区知识,再查实时票务{"\n"}2. 引用数据须带来源;不确定时明确说明{"\n"}3. 输出按 Day 分段,含时间/票价/交通</pre>
        </Card>
      )}
      {tab === 2 && (
        <Card title="工具绑定与约束">
          <div className="grid grid-cols-2 gap-2.5">
            {[["sql_query", "low", "只读 · ≤10k 行"], ["crowd_api", "medium", "QPS ≤ 5"]].map(([n, r, c]) => (
              <div key={n as string} className="rounded-lg border p-2.5" style={{ borderColor: C.border, background: C.panel }}>
                <div className="flex items-center justify-between"><span className="text-[12.5px]" style={{ ...mono, color: C.accent }}>{n}</span><Chip label={r as string} color={r === "low" ? C.green : C.amber} /></div>
                <div className="text-[10.5px] mt-1" style={{ ...mono, color: C.faint }}>{c}</div>
              </div>
            ))}
          </div>
          <div className="text-[10.5px] mt-3" style={{ ...mono, color: C.faint }}>参数约束:max_steps=25 · budget=50k token</div>
        </Card>
      )}
      {tab === 3 && (
        <Card title="多 Skill 编排(拖拽排序 = 固定编排顺序)">
          <div className="text-[11.5px] mb-3.5" style={{ ...mono, color: C.muted }}>
            这里把多个已发布的 Skill 串成一条链:上下拖动改变执行顺序,即为「固定编排」——比如先查天气,再查客流,最后生成建议,顺序是设计时定死的。
            若想让 Agent 自己决定调用顺序(「动态编排」),把下面开关打开即可,顺序不再固定,由模型运行时判断该调用哪个。
          </div>
          <div className="space-y-2 mb-3.5">
            {chain.map((s, i) => (
              <div key={s} className="flex items-center gap-3 rounded-lg border px-3 py-2.5" style={{ borderColor: C.border, background: C.panel }}>
                <span className="w-5 h-5 rounded-full flex items-center justify-center text-[10px] font-bold flex-shrink-0" style={{ background: C.violet + "22", color: C.violet }}>{i + 1}</span>
                <span className="flex-1 text-[12.5px]" style={{ ...ui, color: C.text }}>{s}</span>
                <div className="flex gap-1">
                  <IconBtn icon={ChevronRight} title="下移" onClick={() => { if (i < chain.length - 1) { const c = [...chain]; [c[i], c[i + 1]] = [c[i + 1], c[i]]; setChain(c); toast("已调整执行顺序", "ok"); } }} />
                </div>
              </div>
            ))}
          </div>
          <label className="flex items-center gap-2 text-[12px] mb-2" style={{ color: C.text }}><input type="checkbox" />启用动态编排(交给 Agent 在 ReAct 循环中自行选用)</label>
          <Btn small onClick={() => toast("编排组合已保存,供任务执行时消费(M4-03/04)", "ok")}>保存编排配置</Btn>
        </Card>
      )}
      {tab === 4 && (
        <Card title="版本历史 · Prompt 独立版本化">
          <table className="w-full"><thead><tr><Th>版本</Th><Th>发布时间</Th><Th>发布人</Th><Th>变更说明</Th><Th w={80}></Th></tr></thead>
            <tbody>{[["v4(当前)", "2026-07-10", "李强", "增加预算约束提示", C.green, true], ["v3", "2026-06-22", "李强", "补充引用来源规则", C.faint, false], ["v2", "2026-05-30", "张敏", "首次上线", C.faint, false]].map(r => (
              <Row key={r[0] as string}><Td><span style={{ color: r[4] as string, fontWeight: (r[5] as boolean) ? 700 : 400 }}>{r[0]}</span></Td><Td>{r[1]}</Td><Td>{r[2]}</Td><Td>{r[3]}</Td>
                <Td>{!r[5] && <Btn small onClick={() => toast(`已回滚到 ${r[0]}`, "ok")}>回滚</Btn>}</Td></Row>
            ))}</tbody>
          </table>
        </Card>
      )}
      {share && <ShareModal name="行程规划师 Skill" vis={vis} onChange={setVis} onClose={() => setShare(false)} />}
    </div>
  );
}

/* ══════════ MCP 接入 ══════════ */
function McpPage() {
  const toast = useToast();
  const [rows, setRows] = useState([
    { n: "高德地图 MCP", u: "https://mcp.amap.com/sse", s: "已连接", cnt: "12", c: C.green },
    { n: "内部票务 MCP", u: "http://10.0.2.8:8331/mcp", s: "已连接", cnt: "6", c: C.green },
    { n: "天气服务 MCP", u: "https://weather.example/mcp", s: "认证失败", cnt: "—", c: C.red },
  ]);
  const [wizard, setWizard] = useState(false);
  const [name, setName] = useState(""); const [url, setUrl] = useState(""); const [authType, setAuthType] = useState("none"); const [cred, setCred] = useState("");
  return (
    <div className="grid gap-4">
      <PageHead pid="P07" module="M5 · tool-center-service" title="MCP 接入" sub="M5-07/08/09 · 外部 MCP Server 接入 · 工具发现导入 · 凭证保管" />
      <Card title="外部 MCP SERVER" extra={<Btn primary small onClick={() => setWizard(true)}><Plus size={13} />接入 Server</Btn>}>
        <table className="w-full"><thead><tr><Th>名称</Th><Th>地址</Th><Th>状态</Th><Th>发现工具</Th><Th w={140}>操作</Th></tr></thead>
          <tbody>{rows.map(r => (
            <Row key={r.n}><Td>{r.n}</Td><Td><span style={{ color: C.faint }}>{r.u}</span></Td>
              <Td><Chip label={r.s} color={r.c} /></Td><Td>{r.cnt}</Td>
              <Td><Btn small onClick={() => toast(`已从「${r.n}」拉取 ${r.cnt} 个工具,可勾选导入`, "ok")}>发现工具并导入</Btn></Td></Row>
          ))}</tbody>
        </table>
      </Card>
      {wizard && (
        <Modal title="接入外部 MCP Server" icon={Plug} color={C.amber} onClose={() => setWizard(false)}
          footer={<><Btn onClick={() => setWizard(false)}>取消</Btn><Btn primary onClick={() => {
            setRows([{ n: name || "新接入 Server", u: url || "—", s: "连接中…", cnt: "—", c: C.amber }, ...rows]);
            toast("正在测试连接,凭证已加密存入凭证库(不明文展示)", "ok"); setWizard(false); setName(""); setUrl(""); setCred("");
          }}>保存并测试连接</Btn></>}>
          <div className="space-y-3.5">
            <Field label="Server 名称"><Input placeholder="如:天气服务 MCP" value={name} onChange={(e: any) => setName(e.target.value)} /></Field>
            <Field label="Server 地址(SSE/HTTP 端点)"><Input placeholder="https://xxx.example/mcp" value={url} onChange={(e: any) => setUrl(e.target.value)} /></Field>
            <Field label="鉴权方式">
              <div className="flex gap-2">{[["none", "无需鉴权"], ["apikey", "API Key"], ["oauth", "OAuth2"]].map(([v, l]) => (
                <button key={v} onClick={() => setAuthType(v)} className="px-3 py-1.5 rounded-lg border text-[12px]" style={{ ...mono, borderColor: authType === v ? C.accent : C.border, background: authType === v ? C.accent + "18" : "transparent", color: authType === v ? C.accent : C.muted }}>{l}</button>
              ))}</div>
            </Field>
            {authType !== "none" && (
              <Field label={authType === "apikey" ? "API Key(加密保管,前端/Agent 不可见明文)" : "OAuth2 Client Secret"}>
                <Input placeholder="粘贴凭证…" value={cred} onChange={(e: any) => setCred(e.target.value)} />
              </Field>
            )}
            <div className="rounded-lg border px-3 py-2 flex items-center gap-2" style={{ borderColor: C.violet + "33", background: C.violet + "08" }}>
              <KeyRound size={13} color={C.violet} /><span className="text-[11px]" style={{ ...mono, color: C.text }}>凭证保存后仅首次可见完整值,之后调用由 MCP 层自动注入,任何页面/日志都不会明文展示</span>
            </div>
          </div>
        </Modal>
      )}
    </div>
  );
}

/* ══════════ Agent 注册中心 ══════════ */
function AgentHubPage() {
  const toast = useToast();
  const [showNew, setShowNew] = useState(false);
  const [detail, setDetail] = useState<any>(null);
  const [detailTab, setDetailTab] = useState(0);
  const [gateChecked, setGateChecked] = useState(false);
  const agents = [
    ["agt_7f3a9c21", "行程规划助手", "行程规划师(v4) + 票务查询(v2)", "PUBLISHED", "1,204", "156"],
    ["agt_b81e04d5", "舆情周报生成器", "舆情分析Skill · 固定编排", "PUBLISHED", "312", "42"],
    ["agt_2c90f7a3", "客流问答机器人", "客流问答(v1)", "DRAFT", "—", "0"],
  ];
  return (
    <div className="grid gap-4">
      <PageHead pid="P08" module="M5 · tool-center-service" title="Agent 注册中心" sub="G-API-02~09 · OpenAI 兼容开放平台 /v1/chat/completions" />
      <div className="rounded-lg border px-3.5 py-2.5 flex items-center gap-2" style={{ borderColor: C.accent + "40", background: C.accent + "0a" }}>
        <Sparkles size={15} color={C.accent} />
        <span className="text-[12px]" style={{ ...mono, color: C.text }}>将一个或多个 Skill 封装为对外可调用的 <b style={{ color: C.accent }}>Agent</b>,生成 <b style={{ color: C.accent }}>agent_id</b>,通过 <span style={{ color: C.amber }}>POST /v1/chat/completions</span> 调用</span>
      </div>
      <div className="flex justify-between items-center">
        <span className="text-[11px] tracking-widest font-semibold" style={{ ...mono, color: C.faint }}>AGENT 列表</span>
        <Btn primary onClick={() => setShowNew(true)}><Plus size={14} />新建 Agent</Btn>
      </div>
      <Card pad={false}>
        <table className="w-full"><thead><tr><Th>agent_id</Th><Th>名称</Th><Th>绑定 Skill</Th><Th>状态</Th><Th>30日调用</Th><Th>限流</Th><Th w={40}></Th></tr></thead>
          <tbody>{agents.map(a => (
            <Row key={a[0]} onClick={() => setDetail(a)}><Td><span style={{ color: C.accent }}>{a[0]}</span></Td><Td>{a[1]}</Td>
              <Td><span style={{ color: C.faint }}>{a[2]}</span></Td>
              <Td><Chip label={a[3] as string} color={a[3] === "PUBLISHED" ? C.green : C.muted} /></Td>
              <Td>{a[4]}</Td><Td>{a[5]} QPS</Td><Td><ChevronRight size={14} color={C.faint} /></Td></Row>
          ))}</tbody>
        </table>
      </Card>
      <div className="grid xl:grid-cols-2 gap-4">
        <Card title="API Key 管理(G-API-04)">
          {[["sk-zhilv-8h3f...a92c", "开放平台-合作方A", "2026-06-01", C.green], ["sk-zhilv-2m1e...770f", "内部测试", "2026-07-10", C.green], ["sk-zhilv-x009...ee12", "旧版集成(已吊销)", "2026-01-15", C.red]].map(k => (
            <div key={k[0]} className="flex items-center justify-between py-2.5" style={{ borderBottom: `1px solid ${C.border}` }}>
              <div><div className="text-[12px]" style={{ ...mono, color: C.text }}>{k[0]}</div><div className="text-[10.5px]" style={{ ...mono, color: C.faint }}>{k[1]} · 创建于 {k[2]}</div></div>
              <div className="flex items-center gap-2"><Chip label={k[3] === C.green ? "生效中" : "已吊销"} color={k[3] as string} /><IconBtn icon={Ban} onClick={() => toast("Key 已吊销", "ok")} /></div>
            </div>
          ))}
          <div className="pt-2"><Btn small primary onClick={() => toast("新 Key 已生成(仅本次明文展示)", "ok")}><Plus size={12} />创建 Key</Btn></div>
        </Card>
        <Card title="调用示例 · agt_7f3a9c21(G-API-06/07)">
          <pre className="rounded-lg border p-2.5 text-[10.5px] leading-relaxed overflow-x-auto" style={{ ...mono, background: "#070a14", borderColor: C.border, color: "#7dd3fc" }}>{`curl https://api.zhilv.ai/v1/chat/completions \\
  -H "Authorization: Bearer sk-zhilv-8h3f...a92c" \\
  -H "Content-Type: application/json" \\
  -d '{
    "agent_id": "agt_7f3a9c21",
    "messages": [{"role":"user","content":"规划武夷山两日行程"}],
    "stream": true
  }'`}</pre>
          <button onClick={() => toast("已复制到剪贴板", "ok")} className="flex items-center gap-1.5 mt-2"><Copy size={12} color={C.faint} /><span className="text-[10.5px]" style={{ ...mono, color: C.faint }}>复制代码</span></button>
        </Card>
      </div>
      {detail && (
        <Modal title={`${detail[1]} · ${detail[0]}`} icon={Sparkles} color={C.accent} onClose={() => { setDetail(null); setDetailTab(0); setGateChecked(false); }} w={700}
          footer={<Btn onClick={() => setDetail(null)}>关闭</Btn>}>
          <div className="flex gap-1.5 border-b mb-4" style={{ borderColor: C.border }}>
            {["调用统计", "在线调试", "发布门禁"].map((t, i) => (
              <button key={t} onClick={() => setDetailTab(i)} className="px-3 py-2 text-[12px] border-b-2 -mb-px" style={{ ...ui, fontWeight: i === detailTab ? 700 : 500, color: i === detailTab ? C.accent : C.muted, borderColor: i === detailTab ? C.accent : "transparent" }}>{t}</button>
            ))}
          </div>
          {detailTab === 0 && (
            <div>
              <div className="text-[11.5px] mb-3" style={{ ...mono, color: C.muted }}>按 api_key 与调用方统计该 Agent 的用量,用于成本核算与限流分析</div>
              <div className="grid grid-cols-3 gap-3 mb-3.5">
                {[["近7日调用", detail[4], C.accent], ["成功率", "98.6%", C.green], ["平均耗时", "2.1s", C.violet]].map(k => (
                  <div key={k[0] as string} className="rounded-lg border p-3" style={{ borderColor: C.border, background: C.panel }}><div className="text-[10.5px]" style={{ ...mono, color: C.faint }}>{k[0]}</div><div className="text-[18px] font-bold mt-0.5" style={{ color: k[2] as string }}>{k[1]}</div></div>
                ))}
              </div>
              <table className="w-full"><thead><tr><Th>调用方(api_key)</Th><Th>调用量</Th><Th>成本</Th></tr></thead>
                <tbody>{[["sk-zhilv-8h3f...a92c", "980", "¥18.20"], ["sk-zhilv-2m1e...770f", "224", "¥4.10"]].map(r => (
                  <Row key={r[0]}><Td><span style={{ color: C.accent }}>{r[0]}</span></Td><Td>{r[1]}</Td><Td>{r[2]}</Td></Row>
                ))}</tbody>
              </table>
            </div>
          )}
          {detailTab === 1 && (
            <div>
              <div className="text-[11.5px] mb-3" style={{ ...mono, color: C.muted }}>相当于该 Agent 的 Playground,不用写代码,直接在页面里试调用效果</div>
              <Input placeholder="输入测试消息,如:帮我规划两日行程" />
              <div className="rounded-lg border p-3 mt-2.5 text-[12.5px] leading-relaxed" style={{ ...ui, background: C.panel, borderColor: C.border, color: C.text, minHeight: 90 }}>
                (点击发送后,这里会像真实调用一样流式展示 Agent 的回复内容,包含耗时与 token 用量)
              </div>
              <div className="flex justify-end mt-2.5"><Btn primary small onClick={() => toast("调试请求已发送(等价于真实 API 调用)", "info")}><Play size={12} />发送测试</Btn></div>
            </div>
          )}
          {detailTab === 2 && (
            <div>
              <div className="text-[11.5px] mb-3" style={{ ...mono, color: C.muted }}>发布新版本前,先跑一遍评测回归集,不达标则不允许发布——避免带着质量下降的改动上线</div>
              {!gateChecked ? (
                <Btn primary onClick={() => setGateChecked(true)}><FlaskConical size={13} />运行发布门禁检查</Btn>
              ) : (
                <div className="rounded-lg border p-3.5" style={{ borderColor: C.red + "44", background: C.red + "0a" }}>
                  <div className="text-[12.5px] font-semibold mb-1.5" style={{ color: C.red }}>✕ 门禁未通过,阻断发布</div>
                  <div className="text-[11.5px]" style={{ ...mono, color: C.text }}>工具调用正确率 97.4% 低于基线 98.1%(-0.7%),详见评测中心报告。请优化后重新提交。</div>
                </div>
              )}
            </div>
          )}
        </Modal>
      )}
      {showNew && (
        <Modal title="新建 Agent" icon={Sparkles} color={C.accent} onClose={() => setShowNew(false)}
          footer={<><Btn onClick={() => setShowNew(false)}>取消</Btn><Btn primary onClick={() => { toast("Agent 已创建,agent_id: agt_" + Math.random().toString(16).slice(2, 10), "ok"); setShowNew(false); }}>创建并生成 agent_id</Btn></>}>
          <div className="space-y-3.5 text-[12.5px]">
            <Field label="Agent 名称"><Input placeholder="如:门票咨询助手" /></Field>
            <Field label="绑定 Skill(可多选,支持编排)"><div className="flex flex-wrap gap-1.5">{["行程规划师 v4", "票务查询 v2", "客流问答 v1"].map(sk => <Chip key={sk} label={sk} color={C.violet} />)}</div></Field>
            <div className="grid grid-cols-2 gap-3">
              <Field label="QPS 限流"><Input placeholder="5" /></Field>
              <Field label="日调用上限"><Input placeholder="10000" /></Field>
            </div>
            <Field label="允许调用的景区(留空=不限,G-API-03)"><Input icon={MapPin} placeholder="搜索景区(数据源:sys_orgs / scenic_id)…" /></Field>
          </div>
        </Modal>
      )}
    </div>
  );
}

/* ══════════ 知识中心 ══════════ */
function KnowledgePage() {
  const [tab, setTab] = useState(0);
  const toast = useToast();
  const tabs = [{ label: "数据库知识", icon: Database }, { label: "网页搜索知识", icon: Globe2 }, { label: "向量知识(图文)", icon: Images }];
  return (
    <div className="grid gap-4">
      <PageHead pid="P09" module="M6 · knowledge-service" title="知识中心" sub="M6-01~10 · 三类知识源,org_id 强隔离(G-ISO-01)" />
      <div className="flex gap-2">
        {tabs.map((t, i) => {
          const I = t.icon;
          return <button key={t.label} onClick={() => setTab(i)} className="px-3.5 py-2 rounded-lg border flex items-center gap-2 text-[13px] transition-all"
            style={{ ...ui, fontWeight: 600, color: i === tab ? C.accent : C.muted, borderColor: i === tab ? C.accent + "60" : C.border, background: i === tab ? C.accent + "12" : "transparent" }}>
            <I size={14} />{t.label}</button>;
        })}
      </div>
      {tab === 0 && (
        <div className="grid xl:grid-cols-2 gap-4">
          <Card title="库表登记 · CATALOG(M6-02)">
            {[["ads_visitor_daily", "景区每日客流汇总", "已登记 14 字段释义"], ["dim_scenic", "景区维表(5A 标注)", "已登记 9 字段释义"], ["ods_ticket_sale", "票务流水", "待补充释义"]].map(r => (
              <div key={r[0]} className="flex items-center justify-between py-2.5" style={{ borderBottom: `1px solid ${C.border}` }}>
                <div><div className="text-[12px]" style={{ ...mono, color: C.accent }}>{r[0]}</div><div className="text-[10.5px]" style={{ ...mono, color: C.faint }}>{r[1]}</div></div>
                <span className="text-[10.5px]" style={{ ...mono, color: C.faint }}>{r[2]}</span>
              </div>
            ))}
          </Card>
          <Card title="TEXT2SQL 测试(M6-03,经只读沙箱执行)">
            <Input placeholder="上周武夷山每日客流是多少?" />
            <pre className="rounded-lg border p-2.5 text-[11px] mt-2.5" style={{ ...mono, background: "#070a14", borderColor: C.border, color: "#7dd3fc" }}>{`SELECT dt, visitor_cnt FROM ads_visitor_daily\nWHERE scenic_id='WYS' AND dt >= now()-interval '7 day'`}</pre>
            <div className="text-[10.5px] mt-2" style={{ ...mono, color: C.green }}>✓ 语句校验通过(只读)· 沙箱返回 7 行 · 0.4s</div>
          </Card>
        </div>
      )}
      {tab === 1 && (
        <div className="grid xl:grid-cols-2 gap-4">
          <Card title="搜索引擎配置(M6-04)">
            {[["博查 API", "已启用", C.green], ["SearxNG(自建)", "备用", C.muted], ["Bing", "未配置", C.muted]].map(r => (
              <div key={r[0]} className="flex items-center justify-between py-2.5" style={{ borderBottom: `1px solid ${C.border}` }}>
                <span className="text-[12.5px]" style={{ ...mono, color: C.text }}>{r[0]}</span><Chip label={r[1] as string} color={r[2] as string} />
              </div>
            ))}
            <div className="text-[10.5px] mt-3" style={{ ...mono, color: C.faint }}>缓存 TTL:6h · 域名白名单:12 条(M6-05)</div>
          </Card>
          <Card title="域名白名单">
            <div className="flex flex-wrap gap-1.5">{["gov.cn", "wuyishan.gov.cn", "mct.gov.cn", "xinhuanet.com", "people.com.cn", "ctrip.com"].map(d => <Chip key={d} label={d} color={C.accent} />)}</div>
          </Card>
        </div>
      )}
      {tab === 2 && (
        <div className="grid xl:grid-cols-[1fr_320px] gap-4">
          <Card title="文档与图片 · PGVECTOR + CLIP(M6-06/07)" extra={<Btn small primary onClick={() => toast("上传窗口已打开", "info")}><Plus size={12} />上传</Btn>}>
            <table className="w-full"><thead><tr><Th>名称</Th><Th>类型</Th><Th>分块</Th><Th>状态</Th></tr></thead>
              <tbody>{[["武夷山导览手册.pdf", "文档", "128", "已索引", C.green], ["景点实拍图库(214张)", "图片", "214", "已索引", C.green], ["2026 票价政策.docx", "文档", "—", "切分中", C.amber]].map(r => (
                <Row key={r[0]}><Td>{r[0]}</Td><Td>{r[1]}</Td><Td>{r[2]}</Td><Td><Chip label={r[3] as string} color={r[4] as string} /></Td></Row>
              ))}</tbody>
            </table>
          </Card>
          <Card title="图文混检测试(M6-08)">
            <div className="text-[11px] mb-2.5" style={{ ...mono, color: C.muted }}>检索结果会带「来源标注」一起返回给生成节点,这样 Skill 生成内容时能注明信息出处,便于用户核实(M6-10)</div>
            <Input placeholder="九曲溪竹筏的照片和介绍" />
            {[["chunk#0231", "武夷山导览手册.pdf · 第42页", "0.91"], ["img#0087", "景点实拍图库 · 九曲溪-竹筏.jpg", "0.88"], ["chunk#0454", "2026票价政策.docx · 第3节", "0.74"]].map(r => (
              <div key={r[0]} className="flex items-center justify-between py-2.5" style={{ borderBottom: `1px solid ${C.border}` }}>
                <div><div className="text-[11.5px]" style={{ ...mono, color: C.text }}>{r[0]}</div><div className="text-[10px]" style={{ ...mono, color: C.faint }}>来源:{r[1]}</div></div>
                <span className="text-[11px]" style={{ ...mono, color: C.green }}>{r[2]}</span>
              </div>
            ))}
          </Card>
        </div>
      )}
    </div>
  );
}

/* ══════════ 任务中心 ══════════ */
function TasksPage() {
  const fsm = ["CREATED", "QUEUED", "RUNNING", "WAITING_HUMAN", "COMPLETED"];
  const curIdx = 3;
  const toast = useToast();
  const [sel, setSel] = useState("T-20480");
  const list = [
    ["T-20481", "行程规划:武夷山两日", "L2", "COMPLETED", "武夷山景区", C.green],
    ["T-20480", "本周舆情周报", "L1", "WAITING_HUMAN", "武夷山景区", C.orange],
    ["T-20479", "景区客流查询", "L0", "COMPLETED", "鼓浪屿景区", C.green],
    ["T-20478", "门票销量归因分析", "L2", "RUNNING", "武夷山景区", C.accent],
  ];
  return (
    <div className="grid gap-4">
      <PageHead pid="P10" module="M3 · control-plane-service" title="任务中心" sub="M3-01~08 · Task 状态机 · 全流程 trace_id 可追踪" />
      <Card title="任务列表(M3-07,点击查看详情)" pad={false}>
        <table className="w-full"><thead><tr><Th>ID</Th><Th>任务</Th><Th>模式</Th><Th>状态</Th><Th>所属景区</Th></tr></thead>
          <tbody>{list.map(t => (
            <Row key={t[0]} onClick={() => setSel(t[0] as string)} hi={sel === t[0]}>
              <Td><span style={{ color: C.accent }}>{t[0]}</span></Td><Td>{t[1]}</Td><Td><Chip label={t[2] as string} color={C.violet} /></Td>
              <Td><Chip label={t[3] as string} color={t[5] as string} /></Td><Td><span style={{ color: C.faint }}>{t[4]}</span></Td>
            </Row>
          ))}</tbody>
        </table>
      </Card>
      <Card title={`任务详情 · ${sel} 本周舆情周报(L1 工作流)`}>
        <div className="flex items-center gap-0 mb-4 flex-wrap">
          {fsm.map((s, i) => (
            <div key={s} className="flex items-center">
              <div className="px-3 py-1.5 rounded-lg border text-[11px]" style={{ ...mono, color: i <= curIdx ? (i === curIdx ? C.orange : C.green) : C.faint, borderColor: i === curIdx ? C.orange : C.border, background: i === curIdx ? C.orange + "14" : i < curIdx ? C.green + "0a" : "transparent", fontWeight: i === curIdx ? 700 : 400 }}>{s}</div>
              {i < fsm.length - 1 && <div className="w-8 h-px" style={{ background: i < curIdx ? C.green : C.border }} />}
            </div>
          ))}
        </div>
        <div className="grid xl:grid-cols-[1fr_320px] gap-4">
          <div>
            <div className="text-[11px] tracking-widest mb-2 font-semibold" style={{ ...mono, color: C.faint }}>执行时间线</div>
            {[["09:00:01", "任务入队 · 优先级 normal", C.violet], ["09:00:03", "Worker-2 认领 · 租约 10min", C.primary], ["09:00:05", "节点① SQL 取数 · 沙箱 0.8s", C.amber], ["09:00:09", "节点② Python 清洗 · 沙箱 2.1s", C.amber], ["09:00:18", "节点③ LLM 生成 · 4.1k token", C.green], ["09:00:19", "节点④ 对外发送 → HITL 中断", C.orange]].map(r => (
              <div key={r[0]} className="flex gap-3 py-1.5 text-[11.5px]" style={{ ...mono, borderBottom: `1px solid ${C.border}` }}>
                <span style={{ color: C.faint }}>{r[0]}</span><span style={{ color: r[2] as string }}>{r[1]}</span>
              </div>
            ))}
          </div>
          <div>
            <div className="text-[11px] tracking-widest mb-2 font-semibold" style={{ ...mono, color: C.faint }}>事件流水(OUTBOX,M7-01)</div>
            <div className="rounded-lg border p-2.5 space-y-1 text-[10.5px]" style={{ ...mono, background: "#070a14", borderColor: C.border }}>
              {["TaskStateChanged(QUEUED)", "TaskClaimed", "ToolCalled(sql, low)", "ToolCalled(python)", "LLMUsage(4.1k)", "ApprovalRequired(high)"].map(e => (
                <div key={e}><span style={{ color: C.orange }}>evt</span> <span style={{ color: C.text }}>{e}</span></div>
              ))}
            </div>
            <div className="flex gap-2 mt-3"><Btn small onClick={() => toast("任务已取消", "ok")}>取消任务</Btn><Btn small onClick={() => toast("已重新入队", "ok")}>重试</Btn></div>
          </div>
        </div>
      </Card>
    </div>
  );
}

/* ══════════ 审批中心 ══════════ */
function ApprovalPage() {
  const toast = useToast();
  const [items, setItems] = useState([
    { t: "T-20480 对外发送周报", risk: "high", wait: "等待 12 分钟", hot: true },
    { t: "T-20463 群发短信", risk: "high", wait: "等待 4.2 小时", hot: false },
    { t: "工具注册:crowd_api", risk: "medium", wait: "等待 1 小时", hot: false },
  ]);
  const [sel, setSel] = useState(0);
  return (
    <div className="grid gap-4">
      <PageHead pid="P11" module="M4/M7 · runtime + governance" title="审批中心" sub="M4-07 HITL interrupt · 高风险动作人工审批" />
      <div className="grid xl:grid-cols-[300px_1fr] gap-4">
        <Card title={`待审批 · ${items.length}`} pad={false}>
          {items.map((r, i) => (
            <div key={r.t} onClick={() => setSel(i)} className="rounded-lg border p-2.5 m-2 cursor-pointer transition-all"
              style={{ borderColor: sel === i ? C.violet : r.hot ? C.orange + "50" : C.border, background: sel === i ? C.violet + "0d" : r.hot ? C.orange + "0a" : "transparent" }}>
              <div className="text-[12.5px] font-semibold" style={{ ...ui, color: C.text }}>{r.t}</div>
              <div className="flex items-center gap-2 mt-1"><Chip label={r.risk} color={r.risk === "high" ? C.red : C.amber} /><span className="text-[10.5px]" style={{ ...mono, color: C.faint }}><Clock size={10} className="inline mr-0.5" />{r.wait}</span></div>
            </div>
          ))}
        </Card>
        <Card title={`审批详情 · ${items[sel]?.t || ""}`}>
          <div className="rounded-lg border p-3.5 mb-3.5 text-[12.5px] leading-relaxed" style={{ ...mono, background: C.panel, borderColor: C.border, color: C.text }}>
            <div style={{ color: C.faint }} className="mb-1.5">上下文快照</div>
            任务:本周舆情周报(L1)· 发起人:运营部-张三<br />待执行动作:send_notice(收件:合作渠道 23 人)<br />参数摘要:主题「W29 舆情周报」· 附件 1 · 内容审核已通过<br />护栏:预算余量 82% · 该工具本月第 4 次审批
          </div>
          <textarea placeholder="审批意见(可选)…" className="w-full rounded-lg border px-3 py-2 text-[12.5px] outline-none mb-3.5" rows={2} style={{ ...ui, background: C.panel, borderColor: C.border, color: C.text }} />
          <div className="flex gap-2">
            <Btn primary onClick={() => { toast("已通过,任务从 Checkpoint 恢复续跑", "ok"); setItems(items.filter((_, i) => i !== sel)); setSel(0); }}><CheckCircle2 size={14} />通过并续跑</Btn>
            <Btn danger onClick={() => { toast("已拒绝,任务标记 CANCELLED", "err"); setItems(items.filter((_, i) => i !== sel)); setSel(0); }}><XCircle size={14} />拒绝(任务取消)</Btn>
          </div>
        </Card>
      </div>
    </div>
  );
}

/* ══════════ 评测中心 ══════════ */
function EvalPage() {
  const toast = useToast();
  const [tab, setTab] = useState(0);
  const rows = [["任务成功率", "96.8%", "97.2%", "+0.4%", C.green], ["工具调用正确率", "98.1%", "97.4%", "-0.7%", C.red], ["幻觉率(引用核对)", "2.1%", "1.8%", "-0.3%", C.green], ["平均步数", "8.4", "7.9", "-0.5", C.green], ["平均成本", "¥0.21", "¥0.18", "-¥0.03", C.green]];
  const [cases, setCases] = useState([
    { q: "帮我规划武夷山两日行程,预算1500", exp: "包含Day1/Day2分段,票价与时间信息完整", tag: "行程规划" },
    { q: "武夷山景区今天开放吗?", exp: "准确回答开放状态,不编造", tag: "基础问答" },
    { q: "查一下上周客流数据", exp: "调用 sql_query 工具,返回真实数据而非编造", tag: "工具调用" },
  ]);
  return (
    <div className="grid gap-4">
      <PageHead pid="P12" module="M7 · governance-event-bus-service" title="评测中心" sub="M7-06/07 · 用例集管理 + 发布门禁,不达标阻断" />
      <div className="flex gap-1.5 border-b" style={{ borderColor: C.border }}>
        {["运行报告", "用例管理"].map((t, i) => (
          <button key={t} onClick={() => setTab(i)} className="px-3.5 py-2.5 text-[12.5px] border-b-2 -mb-px" style={{ ...ui, fontWeight: i === tab ? 700 : 500, color: i === tab ? C.accent : C.muted, borderColor: i === tab ? C.accent : "transparent" }}>{t}</button>
        ))}
      </div>
      {tab === 0 && (
        <Card title="发布门禁 · 行程规划师 v4 → v5" extra={<Chip label="门禁:1 项未达标 · 阻断发布" color={C.red} />}>
          <table className="w-full"><thead><tr><Th>指标</Th><Th>基线 v4</Th><Th>本版 v5</Th><Th>差值</Th></tr></thead>
            <tbody>{rows.map(r => (
              <Row key={r[0]}><Td>{r[0]}</Td><Td>{r[1]}</Td><Td>{r[2]}</Td><Td><span style={{ color: r[4] as string, fontWeight: 700 }}>{r[3]}</span></Td></Row>
            ))}</tbody>
          </table>
          <div className="text-[11px] mt-3.5" style={{ ...mono, color: C.faint }}>回归集:travel_planner_v1 · 42 用例 · 运行 3m12s · 触发规则:工具调用正确率不得低于基线</div>
        </Card>
      )}
      {tab === 1 && (
        <Card title="回归用例集 · travel_planner_v1" extra={<Btn small primary onClick={() => setCases([{ q: "新用例问题…", exp: "期望行为描述…", tag: "未分类" }, ...cases])}><Plus size={12} />新增用例</Btn>}>
          <div className="text-[11.5px] mb-3" style={{ ...mono, color: C.muted }}>每条用例 = 一个测试问题 + 期望的正确行为描述,发布新版本前会把全部用例喂给 Agent 跑一遍,自动比对结果是否达标</div>
          <table className="w-full"><thead><tr><Th>测试问题</Th><Th>期望行为</Th><Th>分类</Th><Th w={60}></Th></tr></thead>
            <tbody>{cases.map((c, i) => (
              <Row key={i}><Td>{c.q}</Td><Td><span style={{ color: C.faint }}>{c.exp}</span></Td><Td><Chip label={c.tag} color={C.violet} /></Td>
                <Td><IconBtn icon={Pencil} onClick={() => toast("编辑用例(内容同新增表单)", "info")} /></Td></Row>
            ))}</tbody>
          </table>
        </Card>
      )}
    </div>
  );
}

/* ══════════ 系统设置 ══════════ */
/* ══════════ 模型与预算（独立页面）══════════ */
function ModelBudgetPage() {
  const [subTab, setSubTab] = useState(0);
  const subTabs = ["模型厂商", "模型配置", "预算护栏"];
  return (
    <div className="grid gap-4">
      <PageHead pid="P14" module="M7 · governance-event-bus-service" title="模型与预算" sub="管理模型厂商、模型配置及预算护栏 · 仅 admin/super_admin 可见" />
      <div className="flex gap-2">{subTabs.map((t, i) => (
        <button key={t} onClick={() => setSubTab(i)} className="px-3.5 py-2 rounded-lg border text-[13px] transition-all" style={{ ...ui, fontWeight: 600, color: i === subTab ? C.accent : C.muted, borderColor: i === subTab ? C.accent + "60" : C.border, background: i === subTab ? C.accent + "12" : "transparent" }}>{t}</button>
      ))}</div>
      {subTab === 0 && <ModelProvidersTab />}
      {subTab === 1 && <ModelConfigsTab />}
      {subTab === 2 && <BudgetGuardrails />}
    </div>
  );
}

/* ══════════ 审计导出（独立页面）══════════ */
function AuditExportPage() {
  return (
    <div className="grid gap-4">
      <PageHead pid="P16" module="M7 · governance-event-bus-service" title="审计导出" sub="审计日志按月分区 · 留存 ≥180 天 · 支持导出 CSV · 仅 admin/super_admin 可见" />
      <Card title="审计导出">
        <div className="text-[12.5px]" style={{ ...mono, color: C.muted }}>
          审计日志按月分区 · 留存 ≥180 天 · 支持导出 CSV(M7-04/10,备案材料)。<br />
          功能将在后续版本中完善。
        </div>
      </Card>
    </div>
  );
}

/* ───── 模型厂商管理子页面 ───── */
function ModelProvidersTab() {
  const toast = useToast();
  const [providers, setProviders] = useState<ModelProviderItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [showModal, setShowModal] = useState(false);
  const [editId, setEditId] = useState<string | null>(null);
  const [form, setForm] = useState({ name: "", provider_type: "openai_compatible", base_url: "", api_key: "", description: "", is_enabled: true });

  const PROVIDER_TYPES = [
    { value: "openai_compatible", label: "OpenAI 兼容" },
    { value: "deepseek", label: "DeepSeek" },
    { value: "dashscope", label: "阿里云百炼" },
    { value: "ollama", label: "Ollama 本地" },
  ];

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await listModelProviders({ page_size: 100 });
      setProviders(res.items || []);
    } catch (e: any) {
      toast(e.message || "加载失败", "err");
    } finally { setLoading(false); }
  }, []);

  useEffect(() => { load(); }, [load]);

  function openNew() {
    setEditId(null);
    setForm({ name: "", provider_type: "openai_compatible", base_url: "", api_key: "", description: "", is_enabled: true });
    setShowModal(true);
  }

  function openEdit(p: ModelProviderItem) {
    setEditId(p.id);
    setForm({ name: p.name, provider_type: p.provider_type, base_url: p.base_url, api_key: "", description: p.description, is_enabled: p.is_enabled });
    setShowModal(true);
  }

  async function handleSave() {
    if (!form.name.trim() || !form.base_url.trim()) { toast("名称和 API 地址为必填项", "err"); return; }
    try {
      if (editId) {
        await updateModelProvider(editId, form);
        toast("厂商已更新", "ok");
      } else {
        await createModelProvider(form);
        toast("厂商已创建", "ok");
      }
      setShowModal(false);
      load();
    } catch (e: any) { toast(e.message || "操作失败", "err"); }
  }

  async function handleToggle(p: ModelProviderItem) {
    try {
      await updateModelProvider(p.id, { is_enabled: !p.is_enabled });
      load();
      toast(p.is_enabled ? "已禁用" : "已启用", "ok");
    } catch (e: any) { toast(e.message || "操作失败", "err"); }
  }

  async function handleDelete(id: string, name: string) {
    if (!await confirmDialog({ title: `删除厂商「${name}」`,
      message: '若该厂商下还有模型配置则无法删除,需先删掉它的模型。', confirmLabel: '删除', danger: true })) return;
    try {
      await deleteModelProvider(id);
      toast("厂商已删除", "ok");
      load();
    } catch (e: any) { toast(e.message || "删除失败", "err"); }
  }

  return (
    <Card title={`模型厂商 · ${providers.length} 个`} extra={<Btn primary small onClick={openNew}><Plus size={12} />新增厂商</Btn>}>
      {loading ? <div className="text-center py-8"><Loader2 size={20} className="animate-spin mx-auto" color={C.faint} /></div> : (
        <table className="w-full"><thead><tr><Th>名称</Th><Th>类型</Th><Th>API 地址</Th><Th>状态</Th><Th w={120}>操作</Th></tr></thead>
          <tbody>{providers.map(p => (
            <Row key={p.id}>
              <Td><span className="font-semibold" style={{ color: C.text }}>{p.name}</span>{p.description ? <div className="text-[10.5px] mt-0.5" style={{ color: C.faint }}>{p.description}</div> : null}</Td>
              <Td><Chip label={PROVIDER_TYPES.find(t => t.value === p.provider_type)?.label || p.provider_type} color={C.violet} /></Td>
              <Td><span className="text-[11px]" style={{ ...mono, color: C.faint }}>{p.base_url}</span></Td>
              <Td><Chip label={p.is_enabled ? "启用" : "禁用"} color={p.is_enabled ? C.green : C.muted} /></Td>
              <Td>
                <div className="flex gap-1">
                  <IconBtn icon={Pencil} onClick={() => openEdit(p)} title="编辑" />
                  <IconBtn icon={p.is_enabled ? Ban : CheckCircle2} onClick={() => handleToggle(p)} title={p.is_enabled ? "禁用" : "启用"} />
                  <IconBtn icon={Trash2} onClick={() => handleDelete(p.id, p.name)} title="删除" />
                </div>
              </Td>
            </Row>
          ))}</tbody>
        </table>
      )}
      {providers.length === 0 && !loading && <div className="text-center py-4 text-[12px]" style={{ color: C.faint }}>暂无模型厂商，点击右上角新增</div>}

      {showModal && (
        <Modal title={editId ? "编辑模型厂商" : "新增模型厂商"} icon={Server} color={C.violet} onClose={() => setShowModal(false)}
          footer={<><Btn onClick={() => setShowModal(false)}>取消</Btn><Btn primary onClick={handleSave}>{editId ? "保存" : "创建"}</Btn></>}>
          <div className="space-y-3.5">
            <Field label="厂商名称 *"><Input value={form.name} onChange={(e: any) => setForm({ ...form, name: e.target.value })} placeholder="如: DeepSeek" autoFocus /></Field>
            <Field label="厂商类型">
              <div className="flex flex-wrap gap-1.5">{PROVIDER_TYPES.map(t => (
                <button key={t.value} onClick={() => setForm({ ...form, provider_type: t.value })}
                  className="px-2.5 py-1 rounded-lg border text-[11.5px] transition-all"
                  style={{ borderColor: form.provider_type === t.value ? C.accent : C.border, background: form.provider_type === t.value ? C.accent + "18" : "transparent", color: form.provider_type === t.value ? C.accent : C.muted }}>
                  {t.label}
                </button>
              ))}</div>
            </Field>
            <Field label="API 地址 *"><Input value={form.base_url} onChange={(e: any) => setForm({ ...form, base_url: e.target.value })} placeholder="如: https://api.deepseek.com/v1" /></Field>
            <Field label="API Key（留空不修改，加密存储）"><Input value={form.api_key} onChange={(e: any) => setForm({ ...form, api_key: e.target.value })} placeholder={editId ? "留空不修改已有 Key" : "输入 API Key"} /></Field>
            <Field label="备注"><Input value={form.description} onChange={(e: any) => setForm({ ...form, description: e.target.value })} placeholder="可选备注" /></Field>
          </div>
        </Modal>
      )}
    </Card>
  );
}

/* ───── 模型配置管理子页面 ───── */
function ModelConfigsTab() {
  const toast = useToast();
  const [configs, setConfigs] = useState<ModelConfigItem[]>([]);
  const [providers, setProviders] = useState<ModelProviderItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [filterProvider, setFilterProvider] = useState("");
  const [showModal, setShowModal] = useState(false);
  const [editId, setEditId] = useState<string | null>(null);
  const [form, setForm] = useState({ name: "", provider_id: "", model_name: "", description: "", is_enabled: true, is_default: false, extra_params_str: "" });

  const loadProviders = useCallback(async () => {
    try {
      const res = await listModelProviders({ page_size: 100, is_enabled: true });
      setProviders(res.items || []);
    } catch (e: any) { /* ignore */ }
  }, []);

  const loadConfigs = useCallback(async () => {
    setLoading(true);
    try {
      const params: any = { page_size: 100 };
      if (filterProvider) params.provider_id = filterProvider;
      const res = await listModelConfigs(params);
      setConfigs(res.items || []);
    } catch (e: any) {
      toast(e.message || "加载失败", "err");
    } finally { setLoading(false); }
  }, [filterProvider]);

  useEffect(() => { loadProviders(); }, [loadProviders]);
  useEffect(() => { loadConfigs(); }, [loadConfigs]);

  function openNew() {
    setEditId(null);
    setForm({ name: "", provider_id: providers[0]?.id || "", model_name: "", description: "", is_enabled: true, is_default: false, extra_params_str: "" });
    setShowModal(true);
  }

  function openEdit(c: ModelConfigItem) {
    setEditId(c.id);
    setForm({
      name: c.name, provider_id: c.provider_id, model_name: c.model_name, description: c.description,
      is_enabled: c.is_enabled, is_default: c.is_default,
      extra_params_str: c.extra_params && Object.keys(c.extra_params).length > 0 ? JSON.stringify(c.extra_params, null, 2) : "",
    });
    setShowModal(true);
  }

  async function handleSave() {
    if (!form.name.trim() || !form.provider_id || !form.model_name.trim()) { toast("名称、厂商和模型名为必填项", "err"); return; }
    let extraParams = {};
    if (form.extra_params_str.trim()) {
      try { extraParams = JSON.parse(form.extra_params_str); } catch { toast("额外参数 JSON 格式不正确", "err"); return; }
    }
    try {
      const payload = { ...form, extra_params: extraParams };
      delete (payload as any).extra_params_str;
      if (editId) {
        await updateModelConfig(editId, payload);
        toast("模型配置已更新", "ok");
      } else {
        await createModelConfig(payload as any);
        toast("模型配置已创建", "ok");
      }
      setShowModal(false);
      loadConfigs();
    } catch (e: any) { toast(e.message || "操作失败", "err"); }
  }

  async function handleToggle(c: ModelConfigItem) {
    try {
      await updateModelConfig(c.id, { is_enabled: !c.is_enabled });
      loadConfigs();
      toast(c.is_enabled ? "已禁用" : "已启用", "ok");
    } catch (e: any) { toast(e.message || "操作失败", "err"); }
  }

  async function handleSetDefault(c: ModelConfigItem) {
    try {
      await updateModelConfig(c.id, { is_default: true });
      loadConfigs();
      toast(`已将「${c.name}」设为默认模型`, "ok");
    } catch (e: any) { toast(e.message || "操作失败", "err"); }
  }

  async function handleDelete(id: string, name: string) {
    if (!await confirmDialog({ title: `删除模型配置「${name}」`,
      message: '正在使用该配置的 Agent/Skill 会回退到默认模型。', confirmLabel: '删除', danger: true })) return;
    try {
      await deleteModelConfig(id);
      toast("模型配置已删除", "ok");
      loadConfigs();
    } catch (e: any) { toast(e.message || "删除失败", "err"); }
  }

  return (
    <Card title={`模型配置 · ${configs.length} 个`} extra={
      <div className="flex items-center gap-2">
        <select value={filterProvider} onChange={(e: any) => setFilterProvider(e.target.value)}
          className="rounded-lg border px-2.5 py-1.5 text-[11.5px] outline-none" style={{ background: C.panel, borderColor: C.border, color: C.text }}>
          <option value="">全部厂商</option>
          {providers.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select>
        <Btn primary small onClick={openNew}><Plus size={12} />新增模型</Btn>
      </div>
    }>
      {loading ? <div className="text-center py-8"><Loader2 size={20} className="animate-spin mx-auto" color={C.faint} /></div> : (
        <table className="w-full"><thead><tr><Th>名称</Th><Th>模型名</Th><Th>厂商</Th><Th>默认</Th><Th>状态</Th><Th w={140}>操作</Th></tr></thead>
          <tbody>{configs.map(c => (
            <Row key={c.id}>
              <Td><span className="font-semibold" style={{ color: C.text }}>{c.name}</span>{c.description ? <div className="text-[10.5px] mt-0.5" style={{ color: C.faint }}>{c.description}</div> : null}</Td>
              <Td><span className="text-[11px]" style={{ ...mono, color: C.accent }}>{c.model_name}</span></Td>
              <Td><Chip label={c.provider_name || c.provider_id} color={C.violet} /></Td>
              <Td>{c.is_default ? <Chip label="默认" color={C.amber} /> : <span className="text-[10px]" style={{ color: C.faint }}>—</span>}</Td>
              <Td><Chip label={c.is_enabled ? "启用" : "禁用"} color={c.is_enabled ? C.green : C.muted} /></Td>
              <Td>
                <div className="flex gap-1">
                  <IconBtn icon={Pencil} onClick={() => openEdit(c)} title="编辑" />
                  {!c.is_default && <IconBtn icon={Sparkles} onClick={() => handleSetDefault(c)} title="设为默认" />}
                  <IconBtn icon={c.is_enabled ? Ban : CheckCircle2} onClick={() => handleToggle(c)} title={c.is_enabled ? "禁用" : "启用"} />
                  <IconBtn icon={Trash2} onClick={() => handleDelete(c.id, c.name)} title="删除" />
                </div>
              </Td>
            </Row>
          ))}</tbody>
        </table>
      )}
      {configs.length === 0 && !loading && <div className="text-center py-4 text-[12px]" style={{ color: C.faint }}>暂无模型配置，点击右上角新增</div>}

      {showModal && (
        <Modal title={editId ? "编辑模型配置" : "新增模型配置"} icon={Cpu} color={C.accent} onClose={() => setShowModal(false)}
          footer={<><Btn onClick={() => setShowModal(false)}>取消</Btn><Btn primary onClick={handleSave}>{editId ? "保存" : "创建"}</Btn></>}>
          <div className="space-y-3.5">
            <Field label="展示名称 *"><Input value={form.name} onChange={(e: any) => setForm({ ...form, name: e.target.value })} placeholder="如: DeepSeek-R1" autoFocus /></Field>
            <Field label="所属厂商 *">
              <select value={form.provider_id} onChange={(e: any) => setForm({ ...form, provider_id: e.target.value })}
                className="w-full rounded-lg border px-3 py-2 text-[12.5px] outline-none" style={{ background: C.panel, borderColor: C.border, color: C.text }}>
                {providers.map(p => <option key={p.id} value={p.id}>{p.name} ({p.provider_type})</option>)}
              </select>
            </Field>
            <Field label="模型名(调用时使用) *"><Input value={form.model_name} onChange={(e: any) => setForm({ ...form, model_name: e.target.value })} placeholder="如: deepseek-r1" /></Field>
            <Field label="备注"><Input value={form.description} onChange={(e: any) => setForm({ ...form, description: e.target.value })} placeholder="可选备注" /></Field>
            <Field label="额外参数(JSON, 可选)">
              <textarea value={form.extra_params_str} onChange={(e: any) => setForm({ ...form, extra_params_str: e.target.value })}
                className="w-full rounded-lg border px-3 py-2 text-[11.5px] outline-none" rows={4} style={{ ...mono, background: C.panel, borderColor: C.border, color: C.text }}
                placeholder={`{"temperature": 0.7, "max_tokens": 4096}`} />
            </Field>
            <label className="flex items-center gap-2 text-[12px]" style={{ color: C.text }}>
              <input type="checkbox" checked={form.is_default} onChange={(e: any) => setForm({ ...form, is_default: e.target.checked })} />设为默认模型
            </label>
          </div>
        </Modal>
      )}
    </Card>
  );
}

/* ───── 预算护栏 ───── */
function BudgetGuardrails() {
  return (
    <div className="grid xl:grid-cols-2 gap-4">
      <Card title="预算护栏(M4-06/10)">
        {[["组织日预算", "¥200", 72], ["单任务 Token 上限", "50k", 40], ["最大循环步数", "25", 100]].map(r => (
          <div key={r[0] as string} className="mb-3.5">
            <div className="flex justify-between text-[12px] mb-1.5" style={mono}><span style={{ color: C.text }}>{r[0]}</span><span style={{ color: C.accent }}>{r[1]}</span></div>
            <div className="h-1.5 rounded-full" style={{ background: C.border }}><div className="h-full rounded-full" style={{ width: `${r[2]}%`, background: C.primary }} /></div>
          </div>
        ))}
        <div className="text-[10.5px]" style={{ ...mono, color: C.faint }}>80% 告警 · 100% 熔断,续跑需审批</div>
      </Card>
      <Card title="模型调用概览">
        <div className="text-[11.5px]" style={{ ...mono, color: C.muted }}>
          模型调用统计、成本核算、限流策略等配置将在后续版本中完善。
        </div>
      </Card>
    </div>
  );
}

/* ══════════ 系统日志 ══════════ */
function SystemLogPage() {
  const [traceOpen, setTraceOpen] = useState<string | null>("trc_9f8a2b1c");
  const logs = [
    ["09:12:04", "trc_9f8a2b1c", "POST", "/v1/chat/completions", "200", "1.8s", "agt_7f3a9c21", C.green],
    ["09:11:58", "trc_4e2c771a", "POST", "/chat", "200", "6.4s", "user:zhangmin", C.green],
    ["09:11:40", "trc_0a7d8e33", "POST", "/v1/chat/completions", "429", "0.1s", "限流:sk-zhilv-x009", C.amber],
    ["09:11:22", "trc_bb129f04", "POST", "/admin/users", "403", "0.05s", "越权:role=operator", C.red],
    ["09:10:59", "trc_c3e0d812", "POST", "/tasks/T-20478/retry", "500", "2.1s", "ToolExecutionError", C.red],
  ];
  const timeline = [
    ["09:12:04.012", "gateway", "接收请求 · API Key 校验通过 · org=WYS", C.accent],
    ["09:12:04.048", "control-plane", "建任务 T-20512 · QUEUED", C.violet],
    ["09:12:04.102", "control-plane", "Worker-1 认领 · RUNNING", C.violet],
    ["09:12:04.340", "runtime", "模式判定 → L2 · 护栏就位", C.amber],
    ["09:12:05.210", "tool-center", "ToolCalled(sql_query) · risk=low", C.amber],
    ["09:12:05.980", "tool-center", "沙箱返回 42 行 · 1.2s", C.muted],
    ["09:12:07.640", "runtime", "LLMUsage(3.2k/1.8k) · 内容审核通过", C.green],
    ["09:12:07.802", "gateway", "SSE 响应完成 · 200 · usage 回填", C.green],
  ];
  return (
    <div className="grid gap-4">
      <PageHead pid="P14" module="M7 · governance-event-bus-service" title="系统日志" sub="M7-11~14 · 仅 admin/super_admin 可见 · trace_id 全链路聚合" />
      <div className="rounded-lg border px-3.5 py-2.5 flex items-center gap-2" style={{ borderColor: C.violet + "40", background: C.violet + "0a" }}>
        <Shield_ />
        <span className="text-[11px]" style={{ ...mono, color: C.text }}>数据来自结构化日志索引(loguru sink,G-LOG-05),与业务审计表(gov_audit)分离,可通过 trace_id 互相跳转</span>
      </div>
      <div className="flex flex-wrap gap-2">
        {["trace_id", "时间范围", "模块", "级别", "org", "接口路径", "状态码"].map(f => (
          <div key={f} className="rounded-lg border px-2.5 py-1.5" style={{ borderColor: C.border, background: C.panel }}>
            <span className="text-[10.5px]" style={{ ...mono, color: C.faint }}>{f}</span>
          </div>
        ))}
        <Btn small><Search size={12} />查询</Btn>
      </div>
      <div className="grid xl:grid-cols-[1fr_380px] gap-4">
        <Card title="API 请求日志" pad={false}>
          <table className="w-full"><thead><tr><Th>时间</Th><Th>trace_id</Th><Th>方法</Th><Th>路径</Th><Th>状态</Th><Th>耗时</Th><Th>标识</Th></tr></thead>
            <tbody>{logs.map(l => (
              <Row key={l[1]} onClick={() => setTraceOpen(l[1] as string)} hi={traceOpen === l[1]}>
                <Td>{l[0]}</Td><Td><span style={{ color: C.violet }}>{l[1]}</span></Td><Td>{l[2]}</Td><Td>{l[3]}</Td>
                <Td><Chip label={l[4] as string} color={l[7] as string} /></Td><Td>{l[5]}</Td><Td><span style={{ color: C.faint }}>{l[6]}</span></Td>
              </Row>
            ))}</tbody>
          </table>
        </Card>
        <Card title={`调用链时间线 · ${traceOpen || "—"}`} extra={<ScrollText size={13} color={C.faint} />}>
          <div style={{ maxHeight: 380, overflowY: "auto" }}>
            {timeline.map((t, i) => (
              <div key={i} className="flex gap-2.5">
                <div className="flex flex-col items-center"><span className="w-2 h-2 rounded-full mt-1.5" style={{ background: t[3] as string }} />{i < timeline.length - 1 && <span className="w-px flex-1" style={{ background: C.border }} />}</div>
                <div className="pb-3.5"><div className="text-[10.5px]" style={{ ...mono, color: C.faint }}>{t[0]} · <span style={{ color: t[3] as string, fontWeight: 700 }}>{t[1]}</span></div><div className="text-[12px]" style={{ ...ui, color: C.text }}>{t[2]}</div></div>
              </div>
            ))}
          </div>
        </Card>
      </div>
      <Card title="错误聚合 Top 3(近 24h)">
        <table className="w-full"><thead><tr><Th>错误类型</Th><Th>次数</Th><Th>最近发生</Th><Th>关联模块</Th></tr></thead>
          <tbody>{[["ToolExecutionError · 沙箱超时", "12", "3 分钟前", "tool-center"], ["429 · API Key 限流", "8", "9 分钟前", "gateway"], ["403 · 越权访问", "3", "18 分钟前", "governance"]].map(r => (
            <Row key={r[0]}><Td><span style={{ color: C.red }}>{r[0]}</span></Td><Td>{r[1]}</Td><Td>{r[2]}</Td><Td>{r[3]}</Td></Row>
          ))}</tbody>
        </table>
      </Card>
    </div>
  );
}
function Shield_() { return <span style={{ display: "inline-flex" }}><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke={C.violet} strokeWidth="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" /></svg></span>; }

export { SkillsPage, McpPage, AgentHubPage, KnowledgePage, TasksPage, ApprovalPage, EvalPage, ModelBudgetPage, AuditExportPage, SystemLogPage };
