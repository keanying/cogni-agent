const C = {
  bg: "#0b1020", card: "#131a30", border: "rgba(148,163,184,0.16)", text: "#e2e8f0", muted: "#8b9bb4",
  cyan: "#22d3ee", sky: "#38bdf8", violet: "#a78bfa", indigo: "#818cf8", amber: "#fbbf24",
  slate: "#94a3b8", teal: "#2dd4bf", green: "#34d399", pink: "#f472b6", red: "#f87171", blue: "#60a5fa", purple: "#c084fc", rose: "#fb7185",
};
const mono = "'JetBrains Mono',monospace";
const ui = "'Inter',sans-serif";

function Mod({ x, y, w, h, color, no, title, en, subs }: any) {
  return (
    <g>
      <rect x={x} y={y} width={w} height={h} rx="8" fill={color + "0e"} stroke={color} strokeWidth="1.4" strokeOpacity="0.8" />
      <text x={x + 18} y={y + 27} fill={color} fontFamily={ui} fontSize="17" fontWeight="800">{no}  {title}</text>
      <text x={x + w - 18} y={y + 26} textAnchor="end" fill={color} fillOpacity="0.6" fontFamily={mono} fontSize="12">{en}</text>
      {subs && subs.map((s: any, i: number) => {
        const cols = subs.length;
        const sw = (w - 36 - (cols - 1) * 14) / cols;
        const sx = x + 18 + i * (sw + 14);
        return (
          <g key={i}>
            <rect x={sx} y={y + 40} width={sw} height={h - 58} rx="6" fill={color + "10"} stroke={color} strokeWidth="0.9" strokeOpacity="0.4" />
            <text x={sx + 12} y={y + 61} fill={C.text} fontFamily={ui} fontSize="13.5" fontWeight="700">{s.t}</text>
            {s.ls.map((l: string, j: number) => (
              <text key={j} x={sx + 12} y={y + 81 + j * 18} fill={C.text} fillOpacity="0.72" fontFamily={mono} fontSize="11.5">· {l}</text>
            ))}
          </g>
        );
      })}
    </g>
  );
}

export default function ArchDiagram() {
  const W = 1400, H = 1560;
  const MX = 84, MW = 1040;
  const Arrow = ({ d, color = C.slate, label, lx, ly, dashed, anchor = "start", fs = 12 }: any) => (
    <g>
      <path d={d} fill="none" stroke={color} strokeWidth="1.6" strokeOpacity={dashed ? 0.6 : 0.9}
        strokeDasharray={dashed ? "6,5" : undefined} markerEnd={`url(#${dashed ? "ae" : "ag"})`} />
      {label && <text x={lx} y={ly} textAnchor={anchor} fill={color} fontFamily={ui} fontSize={fs} fontWeight="700"
        style={{ paintOrder: "stroke", stroke: C.bg, strokeWidth: 5 }}>{label}</text>}
    </g>
  );

  // 模块纵向坐标(插入 user-center 后统一重排)
  const y1 = 224;               // ① gateway            h74
  const y2 = y1 + 74 + 16;      // ② user-center         h118
  const y3 = y2 + 118 + 16;     // ③ control-plane       h100
  const y4 = y3 + 100 + 16;     // ④ runtime             h134
  const y5 = y4 + 134 + 16;     // ⑤ tool-center         h134
  const y6 = y5 + 134 + 16;     // ⑥ knowledge           h134
  const y7 = y6 + 134 + 16;     // ⑦ governance-event-bus h134
  const yWorker = y7 + 134 + 16;

  return (
    <div style={{ minHeight: "100vh", background: C.bg, fontFamily: ui, padding: "32px 20px" }}>
      <div style={{ maxWidth: 1420, margin: "0 auto" }}>
        <div style={{ display: "flex", alignItems: "baseline", gap: 14, marginBottom: 6 }}>
          <span style={{ color: C.text, fontSize: 28, fontWeight: 800 }}>智旅 · 架构总览</span>
          <span style={{ color: C.cyan, fontFamily: mono, fontSize: 14, fontWeight: 600 }}>zhilv-agent-core</span>
          <span style={{ color: C.muted, fontFamily: mono, fontSize: 11.5, letterSpacing: 2 }}>MODULAR MONOLITH · 7 MODULES</span>
        </div>
        <div style={{ color: C.muted, fontFamily: mono, fontSize: 13, marginBottom: 18 }}>
          一个包一键启动 · 实例无状态,扩容 = 加实例 · 模块边界 = 未来微服务拆分线
        </div>
        <div style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 12, padding: 16, overflowX: "auto" }}>
          <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} style={{ display: "block", margin: "0 auto", minWidth: 1200 }}>
            <defs>
              <marker id="ag" markerWidth="8" markerHeight="8" refX="6" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 Z" fill={C.slate} /></marker>
              <marker id="ae" markerWidth="7" markerHeight="7" refX="5.5" refY="3.5" orient="auto"><path d="M0,0 L7,3.5 L0,7 Z" fill={C.pink} opacity="0.85" /></marker>
            </defs>

            {/* 客户端 */}
            {["Web / 移动端 APP", "管理后台 WebUI", "OpenAPI"].map((c, i) => (
              <g key={c}>
                <rect x={MX + i * 360} y={14} width={320} height={54} rx="7" fill={C.cyan + "0e"} stroke={C.cyan} strokeOpacity="0.7" strokeWidth="1.2" />
                <text x={MX + 160 + i * 360} y={47} textAnchor="middle" fill={C.cyan} fontFamily={ui} fontSize="15.5" fontWeight="700">{c}</text>
              </g>
            ))}

            {/* LB + 扩容说明 */}
            <rect x={MX} y={98} width={800} height={48} rx="7" fill={C.sky + "0c"} stroke={C.sky} strokeOpacity="0.65" strokeWidth="1.2" />
            <text x={MX + 400} y={128} textAnchor="middle" fill={C.sky} fontFamily={ui} fontSize="15" fontWeight="700">负载均衡 Nginx / SLB</text>
            <Arrow d={`M ${MX + 400} 68 L ${MX + 400} 98`} />
            <g>
              <rect x={952} y={92} width={332} height={60} rx="7" fill={C.indigo + "0c"} stroke={C.indigo} strokeOpacity="0.5" strokeDasharray="6,5" />
              <rect x={962} y={100} width={20} height={12} rx="2" fill="none" stroke={C.indigo} strokeOpacity="0.7" />
              <rect x={968} y={106} width={20} height={12} rx="2" fill="none" stroke={C.indigo} strokeOpacity="0.5" />
              <rect x={974} y={112} width={20} height={12} rx="2" fill="none" stroke={C.indigo} strokeOpacity="0.35" />
              <text x={1006} y={115} fill={C.indigo} fontFamily={ui} fontSize="13" fontWeight="700">水平扩展 = 再起实例</text>
              <text x={1006} y={135} fill={C.text} fillOpacity="0.65" fontFamily={mono} fontSize="11">实例无状态 · 竞争同一 PG 任务队列</text>
            </g>

            {/* 实例容器 */}
            <rect x={60} y={176} width={1180} height={yWorker - 176 + 58} rx="12" fill="rgba(99,102,241,0.035)" stroke={C.indigo} strokeOpacity="0.4" strokeWidth="1.6" />
            <text x={MX} y={206} fill={C.indigo} fontFamily={ui} fontSize="16" fontWeight="800">单个实例 · zhilv-agent-core(app 包,一键启动)</text>
            <Arrow d={`M ${MX + 400} 146 L ${MX + 400} 176`} label="HTTP / SSE" lx={MX + 414} ly={168} />

            {/* ① gateway */}
            <Mod x={MX} y={y1} w={MW} h={74} color={C.sky} no="①" title="gateway-service" en="接入层"
              subs={[{ t: "routes:chat / admin / tasks", ls: ["限流 · 请求路由 · SSE 流式出口 · 断线续传(鉴权转发 ②)"] }]} />

            {/* ② user-center (NEW) */}
            <Mod x={MX} y={y2} w={MW} h={118} color={C.rose} no="②" title="user-center-service" en="用户中心 · 账号与登录"
              subs={[
                { t: "auth 登录", ls: ["手机号+密码登录", "短信验证码(SMS)", "签发 JWT · 登录设备记录"] },
                { t: "users 用户管理", ls: ["姓名/账号/景区/职位", "增删改(仅管理后台)", "APP 端仅登录,不可自注册"] },
                { t: "org 组织归属", ls: ["景区(租户)绑定", "角色分配(对接 ⑦ Casbin)", "最近登录时间/设备列表"] },
              ]} />

            {/* ③ control-plane */}
            <Mod x={MX} y={y3} w={MW} h={100} color={C.violet} no="③" title="control-plane-service" en="任务生命周期"
              subs={[
                { t: "queue / lease", ls: ["SKIP LOCKED 竞争认领 · 优先级+老化", "租约 10min · 心跳续租"] },
                { t: "fsm 状态机", ls: ["含 WAITING_HUMAN · 乐观锁 version", "PG 为唯一事实源"] },
              ]} />

            {/* ④ runtime */}
            <Mod x={MX} y={y4} w={MW} h={134} color={C.indigo} no="④" title="runtime-service" en="执行引擎"
              subs={[
                { t: "engine", ls: ["LangGraph + PostgresSaver", "L0/L1/L2 判定(L3 预留)", "多 Skill 编排:固定DAG / 动态ReAct", "护栏熔断 · HITL interrupt"] },
                { t: "llm", ls: ["LiteLLM 多模型路由", "输入/输出双侧内容审核", "任务/租户双层 Token 预算"] },
              ]} />

            {/* ⑤ tool-center */}
            <Mod x={MX} y={y5} w={MW} h={134} color={C.amber} no="⑤" title="tool-center-service" en="工具中心"
              subs={[
                { t: "tool", ls: ["注册 / 版本 / Schema", "风险分级 high→审批"] },
                { t: "skill", ls: ["工具×Prompt×约束", "可编排标记 · 组合模板"] },
                { t: "mcp", ls: ["统一调用入口", "外部 Server 接入", "三元鉴权 · 凭证库"] },
                { t: "sandbox", ls: ["P0 subprocess 限额", "P1 Docker 隔离"] },
              ]} />

            {/* ⑥ knowledge */}
            <Mod x={MX} y={y6} w={MW} h={134} color={C.teal} no="⑥" title="knowledge-service" en="知识中心 · router 多源融合"
              subs={[
                { t: "database-knowledge", ls: ["库表元数据登记", "Text2SQL → 只读沙箱"] },
                { t: "websearch-knowledge", ls: ["搜索适配 · 摘要缓存", "TTL · 域名白名单"] },
                { t: "multi2vec-clip", ls: ["pgvector 文本+图像", "图文混检 · 租户隔离"] },
              ]} />

            {/* ⑦ governance-event-bus */}
            <Mod x={MX} y={y7} w={MW} h={134} color={C.pink} no="⑦" title="governance-event-bus-service" en="治理及事件总线"
              subs={[
                { t: "events", ls: ["Outbox 同事务发件", "CloudEvents · Schema 注册", "派发器★后期换 JetStream"] },
                { t: "audit / cost", ls: ["审计月分区 ≥180 天", "LLMUsage 成本聚合"] },
                { t: "evals / policy", ls: ["回归集 + 发布门禁", "Casbin RBAC(角色数据源 ②)"] },
              ]} />

            {/* worker/common */}
            <rect x={MX} y={yWorker} width={MW} height={42} rx="6" fill="rgba(148,163,184,0.07)" stroke={C.slate} strokeOpacity="0.4" />
            <text x={MX + 18} y={yWorker + 26} fill={C.slate} fontFamily={mono} fontSize="12">worker/ 后台协程:认领循环 · Outbox 派发 · 租约巡检      common/ 配置 · DB · OTel(公共层)</text>

            {/* 主链路 */}
            <Arrow d={`M ${MX + 60} ${y1 + 74} L ${MX + 60} ${y2}`} label="鉴权转发" lx={MX + 74} ly={y1 + 74 + 13} />
            <Arrow d={`M ${MX + 60} ${y2 + 118} L ${MX + 60} ${y3}`} label="用户/租户上下文" lx={MX + 74} ly={y2 + 118 + 13} />
            <Arrow d={`M ${MX + 60} ${y3 + 100} L ${MX + 60} ${y4}`} label="派发认领(Pull)" lx={MX + 74} ly={y3 + 100 + 13} />
            <Arrow d={`M ${MX + 60} ${y4 + 134} L ${MX + 60} ${y5}`} label="工具/Skill 调用 mcp.invoke()" lx={MX + 74} ly={y4 + 134 + 13} />
            <Arrow d={`M ${MX + 620} ${y4 + 134} Q ${MX + 660} ${y4 + 175} ${MX + 660} ${y6}`} label="知识检索 router.retrieve()" lx={MX + 676} ly={y4 + 174} />
            <Arrow d={`M ${MX + 60} ${y5 + 134} L ${MX + 60} ${y6}`} />

            {/* 事件汇流竖线(右侧) */}
            {(() => {
              const sx = 1176;
              const taps = [y2 + 59, y3 + 50, y4 + 67, y5 + 67, y6 + 67];
              return (
                <g>
                  {taps.map(y => (
                    <path key={y} d={`M ${MX + MW} ${y} L ${sx} ${y}`} stroke={C.pink} strokeWidth="1.4" strokeOpacity="0.55" strokeDasharray="6,5" />
                  ))}
                  <path d={`M ${sx} ${taps[0]} L ${sx} ${y7 + 67} L ${MX + MW} ${y7 + 67}`} stroke={C.pink} strokeWidth="1.6" strokeOpacity="0.65" strokeDasharray="6,5" fill="none" markerEnd="url(#ae)" />
                  <text x={sx + 8} y={(taps[0] + y7 + 67) / 2} fill={C.pink} fontFamily={ui} fontSize="12.5" fontWeight="700"
                    transform={`rotate(-90 ${sx + 8} ${(taps[0] + y7 + 67) / 2})`} textAnchor="middle"
                    style={{ paintOrder: "stroke", stroke: C.bg, strokeWidth: 5 }}>状态变更 emit() → ⑦ 事件总线</text>
                </g>
              );
            })()}

            {/* 数据层 */}
            {(() => {
              const yd = yWorker + 96;
              return (
                <>
                  {[
                    { x: MX, w: 440, c: C.blue, t: "PostgreSQL(唯一必选)", ls: "任务队列 · Checkpoint · 用户/注册表 · Outbox · pgvector" },
                    { x: MX + 470, w: 290, c: C.red, t: "Redis(可选)", ls: "限流 · 幂等键 · 跨实例 SSE 中转" },
                    { x: MX + 790, w: 250, c: C.purple, t: "MinIO / 本地盘", ls: "文件产物 · 租户前缀" },
                  ].map(d => (
                    <g key={d.t}>
                      <rect x={d.x} y={yd} width={d.w} height={66} rx="7" fill={d.c + "0e"} stroke={d.c} strokeOpacity="0.7" strokeWidth="1.2" />
                      <text x={d.x + 16} y={yd + 26} fill={d.c} fontFamily={ui} fontSize="15" fontWeight="700">{d.t}</text>
                      <text x={d.x + 16} y={yd + 48} fill={C.text} fillOpacity="0.68" fontFamily={mono} fontSize="11.5">{d.ls}</text>
                    </g>
                  ))}
                  <Arrow d={`M ${MX + 220} ${yWorker + 64} L ${MX + 220} ${yd}`} label="状态全部外置 → 实例无状态" lx={MX + 236} ly={yd - 8} />
                  <Arrow d={`M ${MX + 615} ${yWorker + 64} L ${MX + 615} ${yd}`} />
                  <Arrow d={`M ${MX + 915} ${yWorker + 64} L ${MX + 915} ${yd}`} />
                </>
              );
            })()}

            {/* 演进条 */}
            {(() => {
              const ye = yWorker + 96 + 66 + 32;
              const items = ["A 单机单包(现在)", "B 多实例 + LB", "C ROLE 分组 · Docker 沙箱", "D Outbox→JetStream · 启用 L3", "E 完整微服务平台"];
              const bw = 216, gap = 24;
              return (
                <>
                  <rect x={60} y={ye} width={1240} height={124} rx="10" fill="rgba(52,211,153,0.045)" stroke={C.green} strokeOpacity="0.4" />
                  <text x={MX} y={ye + 30} fill={C.green} fontFamily={ui} fontSize="15.5" fontWeight="800">演进路径(零重写)</text>
                  {items.map((t, i) => {
                    const bx = MX + i * (bw + gap);
                    return (
                      <g key={t}>
                        <rect x={bx} y={ye + 48} width={bw} height={52} rx="7" fill={C.green + (i === 0 ? "1a" : "0a")} stroke={C.green} strokeOpacity={i === 0 ? 0.85 : 0.45} strokeWidth={i === 0 ? 1.6 : 1} />
                        <text x={bx + bw / 2} y={ye + 79} textAnchor="middle" fill={i === 0 ? C.green : C.text} fillOpacity={i === 0 ? 1 : 0.8} fontFamily={ui} fontSize="13" fontWeight={i === 0 ? 800 : 600}>{t}</text>
                        {i < 4 && <path d={`M ${bx + bw} ${ye + 74} L ${bx + bw + gap} ${ye + 74}`} stroke={C.green} strokeOpacity="0.55" strokeWidth="1.6" markerEnd="url(#ag)" />}
                      </g>
                    );
                  })}
                </>
              );
            })()}
          </svg>
        </div>
        <div style={{ color: C.muted, fontFamily: mono, fontSize: 12.5, marginTop: 12, lineHeight: 1.8 }}>
          模块纪律:跨模块只走接口 · 各管各的表前缀(usr_ / task_ / tc_ / kb_ / evt_ / gov_)· 状态变更必经 emit() · 工具/Skill 必经 mcp.invoke() · 知识必经 router.retrieve() · 登录鉴权统一收口 ②
        </div>
      </div>
    </div>
  );
}
