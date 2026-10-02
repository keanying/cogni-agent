import React, { useState, useEffect, useRef } from 'react';
import {
  Plus, Bot, Send, Cpu, Zap, Boxes, Terminal, Search, ChevronDown, Clock, Activity,
  Settings2, Sparkles, RefreshCw, ChevronUp, ChevronRight, AlertTriangle, Database, Compass, ArrowRightCircle,
  MessageSquare, Wrench, Trash2, KeyRound, Copy, GitBranch, Square, BarChart3, Sliders,
  Mountain, ListChecks, Layers, FileDown, Paperclip, Loader2, ArrowDown,
} from 'lucide-react';
import { Button } from '../components/ui/button';
import { Card, CardContent } from '../components/ui/card';
import { Badge } from '../components/ui/badge';
import { Input } from '../components/ui/input';
import { Avatar, AvatarFallback } from '../components/ui/avatar';
import { cn } from '../lib/utils';
import { modeLabel } from '../lib/modes';
import { motion, AnimatePresence } from 'framer-motion';
import PlanTrace from '../components/common/PlanTrace';
import { splitProvenanceTools, usedToolKeys, type ProvToolRow } from '../lib/provenanceView';
import { ChartScopePicker } from './Tools/QuickBI/ChartScopePicker';
import { directTarget, fixedTargetValue, savedScope, scopeFields } from './Tools/QuickBI/fixedTarget';
import ResultView, { ChartCard, emptyLive, liveFromRun, reduceQbi, CAP_LABEL, type QbiLive } from './Tools/QuickBI/ResultView';
import RunDetailPanel from '../components/common/RunDetailPanel';
import Markdown, { CopyButton } from '../components/common/Markdown';
import * as api from '../lib/api';
import { toast } from 'sonner';
import { useAppStore } from '../store/useAppStore';
import { confirmDialog } from '../components/ui/confirm';

/** 解读一块仪表板时不写问题直接发送:默认的整体解读口径 */
const ONE_CLICK_INSIGHT = '一键解读:对所选图表做整体解读 —— 关键指标表现、趋势与异常、各维度对比,并给出可执行的建议';

// quickbi 是第五类执行目标:它不走任务引擎(没有 task_id / LangGraph),
// 而是直连 Quick BI 的 SSE。放进同一个操练台是因为对用户来说都是「问一句、看结果」,
// 没必要为它单开一个长得不一样的对话页。
type ExecutorType = 'agent' | 'skill' | 'tool' | 'mcp' | 'quickbi';
type StepStatus = 'waiting' | 'running' | 'success' | 'error';
interface ExecutionStep { id: string; step: string; status: StepStatus; deltaMs?: number; data?: any; }
interface ChatMsg {
  id: string; role: 'user' | 'assistant'; content: string; time: string;
  // cost: 本地按配置单价估算(CNY); modelCost: 模型/网关响应里返回的费用(CNY),null 表示模型未返回计费
  steps?: ExecutionStep[];
  // 引用溯源:知识检索来源明细,回答里 [n] 标注对应,渲染在回答下方供用户核验
  citations?: { ref: string | number; source: string; type: string; url?: string }[];
  tokens?: { prompt: number; completion: number; cost: number; modelCost?: number | null;
             costReport?: { model: string; name?: string; calls: number; returned_cost: number; missing: number;
                            cost_usd: number; cost_cny: number | null }[]; duration?: string;
             totalDurationMs?: number;    // v1.1.2:整轮执行总耗时(后端首尾时间戳相减,毫秒)
             // 本轮实际生效的 Token 预算(后端 result.usage.token_budget)。0/缺省 = 不限制。
             budget?: number | null };
  status?: 'streaming' | 'done' | 'error' | 'waiting_human' | 'cancelled'; errorMessage?: string;
  statusHint?: string;   // 后端回报的阶段提示(排队中 / 执行中 / 等待审批)
  // L4 执行计划:动手前整份下发,步骤状态随执行推进更新。
  // 与 steps(timeline 事后追溯)的区别是它**包含还没执行的步骤**
  plan?: api.ExecutionPlan;
  taskId?: string;       // 用于「中止执行」与展示运行 ID
  mode?: string;         // 本轮实际走的执行模式(L0/L2/L4…)
  // E 方案(v0.1.5):对话内凭证确认。卡片只展示工具名+场景,绝不携带账号密码明文;
  // pending 时展示「确认保存 / 忽略」两个按钮,确认后任务恢复执行。
  credentialConfirm?: {
    taskId?: string; toolId?: string; toolName?: string;
    targetToolId?: string; scope?: string; createdAt?: string;
  };
  credentialConfirmState?: 'pending' | 'approved' | 'ignored' | 'expired';
  credentialConfirmNote?: string;
  // Quick BI 轮次:整段执行状态(思考过程 + 正式数据)都在这里,不复用 steps/content ——
  // 两边的事件模型不一样,硬塞进同一个结构只会让两边都别扭。
  qbi?: QbiLive;
  // 智能体画的图(sys_chart)。与 qbi 分开:那是 Quick BI 轮次的整段状态,
  // 这是本轮模型让服务端画出来的图,两者可以同时存在。
  charts?: api.QbiChart[];
}

/** 列表里的一条会话。kind 决定点开它走哪条加载链路、发消息走哪个接口。 */
interface SessionItem {
  id: string;
  kind: 'engine' | 'quickbi';
  title: string;
  executor_type: string;
  turn_count: number;
  updated_at: string;
  executor_id?: string;
  connection_id?: string;
  capability?: string;
  target?: Record<string, any>;
  scenic_id?: string | null;
}

// Quick BI 能力:默认「报表」——操练台最常见的用法是对着已有仪表板提问,
// 让它先按自动路由猜反而让人摸不着头脑。
const QBI_CAPS: { key: string; label: string }[] = [
  { key: 'dashboard_qa', label: '报表' },
  { key: 'dataset_qa', label: '问数' },
  { key: 'insight', label: '解读' },
  { key: 'report', label: '报告' },
  { key: 'auto', label: '自动路由' },
];
// 会话来自后端(numeri_core_conversations),不再是前端内存态——刷新不会丢

const toolLabel = (d: any) => d?.tool_display_name ?? d?.tool_name ?? '';

const STEP_META: Record<string, { icon: any; label: (d: any) => string }> = {
  mode_decision: { icon: Compass, label: (d) => `模式判定：${modeLabel(d?.mode)}` },
  knowledge_retrieval: { icon: Database, label: (d) => `知识检索${d?.hits != null ? `（命中 ${d.hits} 条）` : ''}` },
  tool_call: { icon: Wrench, label: (d) => `工具调用：${toolLabel(d)}${d?.kind && d.kind !== 'ok' ? `（${d.kind}）` : ''}` },
  // v1.1.2:generation 有「开始打点(streaming)」与「完成态」两个事件,实时流里
  // 先显示「内容生成中…」,完成后由完成态节点原位替换(见实时 timeline 去重逻辑)
  generation: { icon: MessageSquare, label: (d) => (d?.status === 'streaming' ? '内容生成中…' : '内容生成') },
  skill_advance: { icon: ArrowRightCircle, label: (d) => {
    const from = d?.from_skill_name;
    const to = d?.to_skill_name ?? d?.to_skill_id ?? '';
    if (from && to) return `推进到下一技能：${from} → ${to}`;
    if (to) return `推进到下一技能：${to}`;
    return '推进到下一技能';
  } },
  drift_check: { icon: Compass, label: (d) => (d?.on_track ? '目标校验：正常' : '目标校验：已纠偏') },
  // 以下为引擎强化后新增的步骤(v21/v22),没有对应条目时会退化成显示原始英文标识
  intent_check: { icon: Compass, label: (d) => {
    // v1.1 升级为 Triager 后,判定结果用 action(intent) 表达;旧的 verdict 分支保留兼容
    // degraded=True:Triager 调用失败降级为全量执行,不再误显示为「直接执行」
    if (d?.degraded) return '意图分诊：未生效，本轮全量执行';
    const actionLabel = { execute: '直接执行', clarify: '需澄清' }[d?.action as string];
    if (actionLabel) {
      const obj = d?.target_skills?.length ? ` → ${d.target_skills.join('、')}` : '';
      return `意图分诊：${actionLabel}${d?.intent ? `（${d.intent}）` : ''}${obj}`;
    }
    return `意图分诊：${{ clear: '明确', ambiguous: '需澄清', out_of_scope: '超出能力' }[d?.verdict as string] ?? d?.verdict ?? ''}`;
  } },
  tool_call_skipped: { icon: Wrench, label: (d) => `跳过重复调用：${toolLabel(d)}` },
  tool_disabled: { icon: AlertTriangle, label: (d) => `工具已停用：${toolLabel(d)}` },
  stall_finalize: { icon: AlertTriangle, label: () => '重复请求已拦截，直接收尾作答' },
  dag_layer: { icon: ArrowRightCircle, label: (d) => `并行执行第 ${(d?.layer ?? 0) + 1} 层（${(d?.skills || []).length} 个技能）` },
  moderation: { icon: AlertTriangle, label: (d) => `内容审核：${d?.side === 'input' ? '输入' : '输出'}${d?.passed === false ? ' 未通过' : ''}` },
  resolve_diagnostic: { icon: AlertTriangle, label: (d) => `解析提示：${d?.message ?? ''}` },
  // v1.1.6(P0-3):版本溯源——agent/skill/tool 实际解析来源,排查「配置改了但执行没跟上」
  resolve_provenance: { icon: GitBranch, label: (d) => {
    const p = d?.provenance;
    if (!p?.agent && !Object.keys(p?.skills || {}).length) return '版本溯源';
    const parts: string[] = [];
    if (p?.agent) parts.push(p.agent.name
      ? `Agent=${p.agent.name}（${provenanceSourceText(p.agent)}）`
      : `Agent=${provenanceSourceText(p.agent)}`);
    const n = Object.keys(p?.skills || {}).length;
    if (n) parts.push(`${n} 个 Skill`);
    return `版本溯源：${parts.join('，')}`;
  } },
};

// 15 号文档:解析来源文案(无默认版本概念;解析来源 = assigned / explicit_snapshot /
// pinned_snapshot;default_snapshot 仅存在于旧会话历史,文案标注「15 号前」以便辨认)
const provenanceSourceText = (n: any) => {
  if (!n) return '—';
  const ver = n.version_id
    ? ` v${n.version}（${String(n.version_id).slice(0, 12)}…）`
    : ` v${n.version}`;
  switch (n.source) {
    case 'assigned': return `指派版本${ver}`;
    case 'explicit_snapshot': return `显式指定版本${ver}`;
    case 'default_snapshot': return `默认快照(15 号前)${ver}`;
    case 'pinned_snapshot': return `pin 版本快照${ver}`;
    case 'latest_published': return `跟随最新${ver}`;
    default: return String(n.source ?? '—');
  }
};

const nowLabel = () => new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

// v1.1.2:执行总耗时格式化(毫秒 → 可读文本),供「执行链分析」卡片汇总展示
const formatDuration = (ms: number) => {
  if (!ms || ms < 0) return '--';
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  return `${m}分${Math.round(s % 60)}s`;
};

// 正式数据是否已经有东西可显示。思考阶段结论还没出来时,下面那个气泡应该整块不出现,
// 而不是留一个空框 —— 用户要的是「思考在上、结果在下」,不是两个框轮流空着。
const qbiHasData = (q?: QbiLive) => !!q && (
  q.charts.length > 0
  || !!(q.report && (q.report.chat_id || q.report.report_url))
  || q.errors.length > 0
  || Object.keys(q.sections).some((k) => !['check', 'related'].includes(k) && (q.sections[k] || '').trim())
);

/**
 * 智能体的图表 → 操练台能渲染的形态。
 *
 * 后端一张图同时发两种形态:ECharts 的 option(给 APP,它装的是 echarts)和一份中性的
 * 行列表(给这里,操练台装的是 recharts)。只发 option 的话,图在 APP 上有、在后台没有 ——
 * 而回答正文里还明明白白写着「雷达图已生成 ✅」,看的人只会觉得系统坏了。
 *
 * 拿不到行列表时返回 null(宁可不画,也不要画一张空图糊在那儿)。
 */
function agentChartToCard(c: any): api.QbiChart | null {
  const t = c?.table;
  if (!t || !Array.isArray(t.rows) || !t.rows.length || !Array.isArray(t.fields)) return null;
  return {
    id: `agent-${Math.random().toString(36).slice(2, 10)}`,
    title: String(c.title || t.title || '图表'),
    chart_type: String(t.chart_type || c.type || 'bar'),
    row_count: Number(t.row_count ?? t.rows.length),
    fields: t.fields, rows: t.rows,
    // 与 APP 同一份 option:后台也用 ECharts 画(见 EChartsView),行列表留给「表格」页签和兜底
    option: c.option && typeof c.option === 'object' ? c.option : undefined,
  };
}

/**
 * 历史轮次的 result.structured → 可渲染的图表列表。
 *
 * structured 在后端既可能是对象 `{cards, charts, insight, recommends}`,也可能是
 * 早期数据里的数组形态,这里两种都吃(和 APP 侧 parseStructuredBlock 同口径)。
 * 画不出来的条目静默丢掉 —— 历史回放不该因为一条脏数据整段炸掉。
 */
function replayCharts(structured: any): api.QbiChart[] | undefined {
  if (!structured) return undefined;
  const blocks = Array.isArray(structured) ? structured : [structured];
  const out: api.QbiChart[] = [];
  for (const b of blocks) {
    for (const c of (b?.charts || [])) {
      const card = agentChartToCard(c);
      if (card) out.push(card);
    }
  }
  return out.length ? out : undefined;
}

/** SSE 断点游标,按任务号记,**只活在本次页面加载内**。
 *  刻意不落 sessionStorage:见 consumeStream 里的说明 —— 游标比它所对应的
 *  渲染结果活得久,就会把执行链吃掉。 */
const _sseCursors = new Map<string, string>();

const Console: React.FC = () => {
  const { me } = useAppStore();
  const [mode, setMode] = useState<'chat' | 'debug'>('chat');
  const [executorType, setExecutorType] = useState<ExecutorType>('agent');
  const [targets, setTargets] = useState<{ id: string; name: string; badge?: string; desc?: string; published: boolean }[]>([]);
  const [targetId, setTargetId] = useState('');
  const [targetSearch, setTargetSearch] = useState('');
  const [showTargetPicker, setShowTargetPicker] = useState(false);
  // 15 号文档两层版本选择:''=自动解析(按当前用户在分发中心的指派命中,无默认版本兜底,
  // 未指派将由后端提示 agent_not_distributed);非空 = 显式钉到某 published 版本行 id
  const [targetVersionId, setTargetVersionId] = useState('');
  const [targetVersions, setTargetVersions] = useState<{ id: string; version: number | null }[]>([]);

  // 左侧列表要同时装两种会话:任务引擎的 conversations 与 Quick BI 自己的 sessions。
  // 它们在后端是两张表(一个挂 LangGraph thread,一个粘目标数据),但对用户来说
  // 都是「我之前问过的那段对话」,分两个列表放只会让人来回找。
  const [sessions, setSessions] = useState<SessionItem[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [loadingSessions, setLoadingSessions] = useState(false);
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [input, setInput] = useState('');
  /**
   * 「哪些会话正在执行」。
   *
   * 以前这里是一个全局的 isProcessing 布尔值:任何一个会话在跑,发送按钮就整个禁用,
   * 连**新建会话**都发不出消息。只要有一个任务卡住(模型不通/上游 hang),整个操练台
   * 就废了。对话本来就是可以多开的,忙闲必须按会话算。
   *
   * 新会话还没有 id,用 NEW_SESSION 这个哨兵占位,拿到真实 id 后迁移过去。
   */
  const NEW_SESSION = '__new__';
  const [busySessions, setBusySessions] = useState<Set<string>>(new Set());
  const sessionKey = activeSessionId || NEW_SESSION;
  const isProcessing = busySessions.has(sessionKey);

  const markBusy = (key: string, busy: boolean) =>
    setBusySessions((prev) => {
      const next = new Set(prev);
      if (busy) next.add(key); else next.delete(key);
      return next;
    });
  // ── Quick BI 执行态 ──────────────────────────────────────────
  const [qbiCap, setQbiCap] = useState('dashboard_qa');       // 默认报表,可切问数/解读/报告
  const [qbiScenic, setQbiScenic] = useState('');             // 归属景区(空=本账号所属)
  // 目标数据统一用一个值表示:`asset:<id>` 已登记的仪表板/数据集,`file:<id>` 上传的文件,
  // 空 = 按景区自动挑。两类东西在用户眼里都是「这一问打在哪份数据上」,不该分两个选择器。
  const [qbiTarget, setQbiTarget] = useState('');
  // 解读 · 仪表板的图表范围:undefined = 全选(对齐 Quick BI 智能解读「请确认图表范围」);换了目标数据就回到全选
  const [qbiChartIds, setQbiChartIds] = useState<string[] | undefined>(undefined);
  // 存量固定问题里显式选过「挑一份数据集」的(qa_scope=dataset):回填时记住,发起时原样带回
  const [qbiLegacyPickOne, setQbiLegacyPickOne] = useState(false);
  /** 换目标数据:图表范围回到全选;从固定问题 / 会话回填时按存的来 */
  const applyScope = (target?: Record<string, any> | null) => {
    const sc = savedScope(target);
    setQbiChartIds(sc.chartIds); setQbiLegacyPickOne(sc.legacyPickOne);
  };
  const [qbiFixedId, setQbiFixedId] = useState('');           // 带固定问题发起
  const [qbiDomain, setQbiDomain] = useState('');             // 数据域,只用来筛固定问题
  const [qbiDomains, setQbiDomains] = useState<string[]>([]);
  const [qbiAssets, setQbiAssets] = useState<api.QbiAsset[]>([]);
  // 上传文件也是一种「目标数据」:问数/解读/报告都能打在它上面。
  // 和仪表板、数据集放进同一个选择器,省得用户先想「我这份数据算哪一类」。
  const [qbiFiles, setQbiFiles] = useState<api.QbiFile[]>([]);
  const [qbiUploading, setQbiUploading] = useState(false);
  const qbiFileInput = useRef<HTMLInputElement>(null);
  const [qbiFixed, setQbiFixed] = useState<api.QbiFixedQuestion[]>([]);
  const [scenics, setScenics] = useState<{ id: string; name: string }[]>([]);
  const qbiAbort = useRef<AbortController | null>(null);

  const [showOverrideModal, setShowOverrideModal] = useState(false);
  const [overrideModel, setOverrideModel] = useState('');
  const [overrideKs, setOverrideKs] = useState('');
  const messagesEndRef = useRef<HTMLDivElement>(null);
  // 哪些消息的「执行过程」被展开了。默认全收起 —— 过程是排查时才看的,
  // 默认摊开会把真正要读的结论挤到屏幕外面。
  const [openTraces, setOpenTraces] = useState<Set<string>>(new Set());
  const toggleTrace = (id: string) => setOpenTraces((prev) => {
    const next = new Set(prev);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });

  const presets = [
    '帮我规划武夷山两日游行程，预算3000元，有小孩',
    '查询今日景区入园人数',
    '分析上周游客满意度趋势',
  ];
  // Quick BI 的快速提问要贴着「对着报表问数」这件事,套用上面那三句会答非所问
  const qbiPresets = [
    '上个月各景区销售额 TOP5',
    '解读一下这个仪表板最近的趋势和异常',
    '生成本月经营分析报告',
  ];

  // 右侧面板始终反映**最近一条助手消息**的运行情况。

  // 用最近一条而不是「正在跑的那条」:跑完之后信息仍然要能看到,

  // 否则排查问题时得重新发一遍才能看到用了哪个模型、花了多少 token。

  const lastAssistant = [...messages].reverse().find((m) => m.role === 'assistant');


  // 自动滚动只在「人本来就停在底部」时跟随。
  // 以前 messages 一变(每个 token、每条时间线事件)就 scrollIntoView —— 执行中往上翻看历史,
  // 下一帧就被拽回底部,整轮执行期间根本没法看上面的内容。
  // 现在:离底部 120px 以内算「在底部」,跟随新内容;往上翻了就不动,右下角给一个「回到最新」。
  const scrollBoxRef = useRef<HTMLDivElement>(null);
  const stickToBottomRef = useRef(true);
  const [showJumpToLatest, setShowJumpToLatest] = useState(false);
  const scrollToBottom = (smooth = false) => {
    const el = scrollBoxRef.current;
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: smooth ? 'smooth' : 'auto' });
    else messagesEndRef.current?.scrollIntoView();
  };
  const onMessagesScroll = () => {
    const el = scrollBoxRef.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
    stickToBottomRef.current = nearBottom;
    setShowJumpToLatest(!nearBottom);
  };
  const followLatest = () => { stickToBottomRef.current = true; setShowJumpToLatest(false); scrollToBottom(true); };
  const prevMsgCount = useRef(0);
  useEffect(() => {
    // 切会话 / 清空 / 新发一条(条数变少或从 0 开始)一律回到底部跟随
    if (messages.length < prevMsgCount.current || prevMsgCount.current === 0) stickToBottomRef.current = true;
    prevMsgCount.current = messages.length;
    if (stickToBottomRef.current) scrollToBottom();
    else setShowJumpToLatest(true);
  }, [messages]);

  // 会话列表来自后端。以前这里是纯前端内存态,刷新页面整份聊天记录就没了。
  /** 拉两条链路的会话并按时间合并。任一来源失败不影响另一条 —— 
   *  Quick BI 没配连接时它的接口会空/报错,不该把整个会话列表带没。 */
  const fetchSessions = async (): Promise<SessionItem[]> => {
    const [conv, qbi] = await Promise.allSettled([api.listConversations(), api.qbiListSessions()]);
    const out: SessionItem[] = [];
    if (conv.status === 'fulfilled') {
      out.push(...conv.value.items.map((c) => ({
        id: c.id, kind: 'engine' as const, title: c.title || '新会话',
        executor_type: c.executor_type, executor_id: c.executor_id,
        turn_count: c.turn_count, updated_at: c.updated_at,
      })));
    }
    if (qbi.status === 'fulfilled') {
      out.push(...qbi.value.items.map((s) => ({
        id: s.id, kind: 'quickbi' as const,
        title: s.title || 'Quick BI 会话',
        executor_type: 'quickbi', connection_id: s.connection_id,
        capability: s.capability, target: s.target, scenic_id: s.scenic_id ?? null,
        turn_count: s.turn_count, updated_at: s.updated_at || s.created_at || new Date().toISOString(),
      })));
    }
    return out.sort((a, b) => +new Date(b.updated_at) - +new Date(a.updated_at));
  };

  const loadSessions = async () => {
    setLoadingSessions(true);
    try { setSessions(await fetchSessions()); }
    catch { /* 未登录或接口不可用时静默,不打扰主流程 */ }
    finally { setLoadingSessions(false); }
  };

  // 每次进入页面自动定位到最后一次会话(后端按 updated_at 倒序,第一条即最近)。
  // 没有任何历史会话时才停留在新对话。
  useEffect(() => {
    (async () => {
      setLoadingSessions(true);
      try {
        const items = await fetchSessions();
        setSessions(items);
        if (items.length > 0) openSession(items[0]);
      } catch { /* 未登录或接口不可用时静默,不打扰主流程 */ }
      finally { setLoadingSessions(false); }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /** 打开一条 Quick BI 会话:把历轮问答从运行记录里还原成对话流。
   *  报告那种还没跑完的轮次会挂上后台跟踪 —— 关掉页面再回来照样能看到结果。 */
  const openQbiSession = async (item: SessionItem) => {
    setActiveSessionId(item.id);
    setExecutorType('quickbi');
    if (item.connection_id) setTargetId(item.connection_id);
    if (item.capability && item.capability !== 'auto') setQbiCap(item.capability);
    setQbiScenic(item.scenic_id || '');
    // 会话粘住的目标可能是数据集 cube_id / 仪表板 pageId —— 与固定问题同一套还原(fixedTarget.ts)
    setQbiTarget(fixedTargetValue(item.target, qbiAssets));
    applyScope(item.target);
    setQbiFixedId('');
    try {
      const detail = await api.qbiGetSession(item.id);
      const rebuilt: ChatMsg[] = [];
      for (const r of detail.messages || []) {
        rebuilt.push({ id: `u-${r.id}`, role: 'user', content: r.question,
                       time: new Date(r.created_at || Date.now()).toLocaleTimeString(
                         [], { hour: '2-digit', minute: '2-digit' }) });
        rebuilt.push({
          id: `a-${r.id}`, role: 'assistant', content: r.answer || '',
          time: new Date(r.created_at || Date.now()).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
          status: r.status === 'running' ? 'streaming' : r.status === 'failed' ? 'error' : 'done',
          errorMessage: r.error || undefined,
          qbi: { ...liveFromRun(r), streaming: r.status === 'running' },
        });
      }
      setMessages(rebuilt);
      // 未完成的报告:后台已经在收了,这里只负责把结果显示出来
      for (const r of (detail.messages || []).filter((m) => m.status === 'running')) {
        trackReport(r.id, `a-${r.id}`);
      }
    } catch (e: any) {
      toast.error(e.message || '加载 Quick BI 会话失败');
    }
  };

  /** 轮询一条未完成的报告直到出结果。服务端本来就在后台收(report_watcher),
   *  这里只是把已经收到的结果刷到界面上 —— 所以间隔放宽一点,不跟服务端抢。 */
  const trackReport = (runId: string, msgId: string) => {
    let stopped = false;
    const tick = async () => {
      if (stopped) return;
      try {
        const r = await api.qbiGetReport(runId);
        setMessages((prev) => prev.map((m) => (m.id === msgId
          ? { ...m, content: r.answer || m.content,
              status: r.status === 'running' ? 'streaming' : r.status === 'failed' ? 'error' : 'done',
              errorMessage: r.error || undefined,
              qbi: { ...liveFromRun(r), streaming: r.status === 'running' } }
          : m)));
        if (r.status !== 'running') { stopped = true; loadSessions(); return; }
      } catch { /* 拉不到就下一轮再说,报告本身在服务端继续跑 */ }
      setTimeout(tick, 15000);
    };
    setTimeout(tick, 5000);
  };

  /** 点开历史会话:把历轮问答从后端拉回来重建对话流。 */
  const openSession = async (conv: SessionItem) => {
    if (conv.kind === 'quickbi') return openQbiSession(conv);
    setActiveSessionId(conv.id);
    setExecutorType(conv.executor_type as ExecutorType);
    setTargetId(conv.executor_id || '');
    try {
      const { items } = await api.getConversationTurns(conv.id);
      const rebuilt: ChatMsg[] = [];
      const resumeTaskIds: string[] = [];
      for (const t of items) {
        rebuilt.push({ id: `u-${t.task_id}`, role: 'user', content: t.message,
                       time: new Date(t.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) });
        // 执行链回放:把时间线压成步骤条,让人一眼看出这轮调了什么
        const steps: ExecutionStep[] = (t.timeline || [])
          .filter((e: any) => ['mode_decision', 'tool_call', 'tool_call_skipped', 'tool_disabled',
                               'knowledge_retrieval', 'generation', 'intent_check', 'skill_advance',
                               'resolve_provenance'].includes(e.step))
          .map((e: any, i: number) => ({
            id: `${t.task_id}-${i}`,
            step: e.step,
            status: 'done' as StepStatus,
            // v1.1.2:回放也带上每步耗时(后端落库 duration),「执行链分析」据此汇总总耗时
            deltaMs: typeof e.duration === 'number' ? e.duration : 0,
            data: e,
          }));

        const running = t.in_progress;
        // 僵尸任务:RUNNING 但后端判定其租约已过期(执行侧已认定卡死、状态未落库)。
        // 不再按 streaming 处理(否则前端永远挂流、输入框锁死),有答案就展示答案,
        // 没有就提示中断,用户可重发。
        const zombie = !running && t.status === 'RUNNING';
        // 取消是正常业务终态(用户主动停止):渲染「已停止生成」灰字 + 已有部分回答,
        // 不走 error 红字——用户自己按的停止,给他看报错是体验事故。
        const cancelledTurn = !running && (t.cancelled || t.status === 'CANCELLED');
        // 回放也要带上运行信息:右侧「执行详情」面板在翻历史会话时同样有用 ——
        // 排查「上次那轮为什么慢/用了哪个模型」不该需要重发一遍。
        const _md = (t.timeline || []).find((e: any) => e?.step === 'mode_decision');
        const _pu = [...(t.timeline || [])].reverse().find((e: any) => e?.step === 'plan_update');
        rebuilt.push({
          id: `a-${t.task_id}`, role: 'assistant',
          taskId: t.task_id,
          mode: _md?.mode,
          plan: _pu?.plan,
          // 还在跑的轮次不要写"(该轮未产生回答)"——那句描述的是"跑完了但没产出",
          // 和"正在跑"是两回事。留空,下面会重新挂回 SSE 流继续接收。
          content: t.answer || (running ? '' : (t.error || (cancelledTurn ? '' : '(该轮未产生回答)'))),
          time: new Date(t.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
          steps,
          status: running ? 'streaming' : cancelledTurn ? 'cancelled' : zombie ? (t.answer ? 'done' : 'error') : t.status === 'COMPLETED' ? 'done' : 'error',
          statusHint: running ? '正在继续执行,已重新接入…' : zombie ? '上一轮执行中断,结果可能不完整,可重新发送' : cancelledTurn ? '已停止生成' : undefined,
          errorMessage: running || t.status === 'COMPLETED' || cancelledTurn ? undefined : (t.error || undefined),
          // 真实计费:后端 result.usage.cost_cny 为最终单价(配置价/litellm 自动/兜底);
          // 旧会话无该字段时回退固定估算(0.000015 元/token)
          tokens: t.usage ? { prompt: t.usage.prompt_tokens, completion: t.usage.completion_tokens,
                              cost: t.usage.cost_cny ?? (t.usage.prompt_tokens + t.usage.completion_tokens) * 0.000015,
                              modelCost: t.usage.model_cost_cny ?? null,
                              costReport: t.usage.cost_report ?? undefined,
                              // v1.1.2:回放整轮执行总耗时(后端首尾时间戳相减)
                              totalDurationMs: t.usage.total_duration_ms ?? undefined,
                              // 该轮当时生效的预算(旧数据无此字段 -> null -> 不画进度条)
                              budget: t.usage.token_budget ?? null } : undefined,
          // 引用溯源:历史消息带来源明细,刷新/重进会话后仍可核验
          citations: (t.citations || []).length ? t.citations : undefined,
          // 结构化结果回放:图早就随 result.structured 落库了(APP 一直读得到),
          // 这里以前漏读,于是操练台一刷新图就没了 —— 回答里还写着「雷达图已生成 ✅」,
          // 下面却空空如也。图是结论的一部分,不能只活在这次 SSE 连接里。
          charts: replayCharts(t.structured),
        });
        if (running) resumeTaskIds.push(t.task_id);
      }
      setMessages(rebuilt);
      // 有未跑完的轮次就重新挂回它的 SSE 流。退出再进来时任务可能还在后台跑,
      // 这样能继续看到剩下的输出,而不是停在一个空气泡上。
      for (const tid of resumeTaskIds) {
        // 只锁这一个会话。以前用全局标志,一个卡住的任务会让**所有**会话都发不出消息。
        markBusy(conv.id, true);
        consumeStream(tid, `a-${tid}`).finally(() => markBusy(conv.id, false));
      }
    } catch (e: any) {
      toast.error(e.message || '加载会话历史失败');
    }
  };

  const archiveSession = async (id: string, e: React.MouseEvent, kind: 'engine' | 'quickbi' = 'engine') => {
    e.stopPropagation();
    if (!await confirmDialog({
      title: '归档该会话',
      message: '归档后它不再出现在会话列表里,也不能继续追问。历史记录仍保留在后台,可按会话 ID 查。',
      confirmLabel: '归档',
      danger: true,
    })) return;
    try {
      await (kind === 'quickbi' ? api.qbiArchiveSession(id) : api.archiveConversation(id));
      if (activeSessionId === id) { setActiveSessionId(null); setMessages([]); }
      loadSessions();
    } catch (err: any) { toast.error(err.message || '归档失败'); }
  };

  // 复制会话 ID 到剪贴板,便于后续排查问题时把会话标识发给后端/研发
  const copySessionId = async (id: string, e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      await navigator.clipboard?.writeText(id);
      toast.success('会话 ID 已复制');
    } catch { toast.error('复制失败,请手动复制'); }
  };

  const tabs: { type: ExecutorType; label: string; icon: any }[] =
    mode === 'chat'
      ? [{ type: 'agent', label: 'Agent', icon: Bot }, { type: 'skill', label: 'Skill', icon: Zap },
         { type: 'quickbi', label: 'Quick BI', icon: BarChart3 }]
      : [{ type: 'tool', label: 'Tool', icon: Cpu }, { type: 'mcp', label: 'MCP', icon: Boxes }];

  useEffect(() => { setExecutorType(tabs[0].type); }, [mode]);

  // 切换执行目标类型时,如果当前会话不是这一类的,就开一段新会话。
  // 不这么做的话,Quick BI 会话的 id 会被当成任务引擎的 conversation_id 发出去
  // (反之亦然),后端要么找不到、要么把两种历史混进同一段对话。
  useEffect(() => {
    const cur = sessions.find((x) => x.id === activeSessionId);
    if (!cur) return;
    const want = executorType === 'quickbi' ? 'quickbi' : 'engine';
    if (cur.kind !== want) { setActiveSessionId(null); setMessages([]); }
  }, [executorType]);

  const loadTargets = async () => {
    try {
      if (executorType === 'agent') {
        const r = await api.listAgents();
        // 15 号文档:无默认版本概念,「可否执行」按 published_count(已发布版本数)推导
        setTargets(r.items.map((a: any) => ({ id: a.id, name: a.name, badge: (a.bound_skill_ids?.length || 0) > 1 ? 'multi-skill' : undefined, desc: a.description, published: (a.published_count ?? 0) > 0 })));
      } else if (executorType === 'skill') {
        const r = await api.listSkills();
        setTargets(r.items.map((s: any) => ({ id: s.id, name: s.name, badge: s.suggested_mode, desc: `绑定 ${s.tool_ids?.length || 0} 个工具`, published: (s.published_count ?? 0) > 0 })));
      } else if (executorType === 'quickbi') {
        const r = await api.qbiListConnections();
        setTargets(r.items.filter((c) => c.active !== false).map((c) => ({
          id: c.id, name: c.name, badge: c.is_default ? '默认' : undefined,
          desc: c.server_domain, published: true,
        })));
        // 没配过连接就别让人对着空选择器发呆
        if (!r.items.length) toast.info('还没有可用的 Quick BI 连接,先到 工具中心 → Quick BI → 连接配置 添加');
      } else if (executorType === 'tool') {
        const r = await api.listTools();
        setTargets(r.items.map((t: any) => ({ id: t.id, name: t.name, badge: t.type, desc: t.description, published: true })));
      } else {
        // MCP:这里要列的是**该服务端发现出来的工具**,不是服务端本身。
        //
        // 原先列的是 MCP 服务端(tc_mcp_servers 的 id),而执行侧把 executor_type
        // 为 tool/mcp 的 executor_id 当作 tc_tools 的主键去查 —— 于是选中任何一个
        // MCP 目标都必然报「Tool <服务端id> 不存在」,这条路根本跑不通。
        // 发现出来的 MCP 工具本身就是普通的 tc_tools 行(type='mcp'),直接列它们。
        const [tools, servers] = await Promise.all([
          api.listTools(),
          api.listMcpServers().catch(() => ({ items: [] as any[] })),
        ]);
        const serverName: Record<string, string> = {};
        (servers.items || []).forEach((m: any) => { serverName[m.id] = m.name; });
        const mcpTools = (tools.items || []).filter((t: any) => t.type === 'mcp');
        setTargets(mcpTools.map((t: any) => ({
          id: t.id, name: t.name,
          // 带上来源服务端:同名工具在两个服务端上都有时,光看工具名分不出是哪个
          badge: serverName[t.source_mcp_server_id] || 'MCP',
          desc: t.description, published: true,
        })));
        if (!mcpTools.length) {
          toast.info((servers.items || []).length
            ? '这些 MCP 服务端还没有发现出工具:到 工具中心 → MCP 里点「发现工具」'
            : '还没有配置 MCP 服务端,先到 工具中心 → MCP 添加');
        }
      }
    } catch (e: any) {
      toast.error(e.message || '加载执行目标失败');
    }
  };
  useEffect(() => { loadTargets(); }, [executorType]);

  const activeTarget = targets.find((t) => t.id === targetId);

  // Phase 5:选中 agent/skill 目标后,拉取其 published 版本列表供显式版本选择;
  // 无已发布版本的目标在选择器中禁用(必须在管理端发布后才能执行)。
  useEffect(() => {
    setTargetVersionId('');
    setTargetVersions([]);
    if ((executorType === 'agent' || executorType === 'skill') && targetId) {
      api.listResourceVersions(executorType, targetId).then((r) => {
        setTargetVersions((r.items || [])
          .filter((v) => v.status === 'published')
          .map((v) => ({ id: v.id, version: v.version })));
      }).catch(() => { /* 版本列表加载失败不阻塞对话,按自动解析处理 */ });
    }
  }, [executorType, targetId]);

  // 景区清单:Quick BI 提问要按景区关联看板,选择器里得有得选。
  // 拉不到(没权限/接口不可用)就退化成手填,不挡着用。
  useEffect(() => {
    if (executorType !== 'quickbi' || scenics.length) return;
    api.listScenicOrgs()
      .then((r) => setScenics((r.items || []).map((o: any) => ({
        id: o.scenic_id || o.id, name: o.scenic_name || o.name || o.scenic_id || o.id }))))
      .catch(() => { /* 静默:景区选择器留空,可手填 */ });
  }, [executorType]);

  // 资产**只按景区**过滤,不再按当前连接。
  //
  // 原先加了 connection_id:于是绑在另一条连接下的那块板压根不出现在下拉里 ——
  // 看起来像「这块板没登记」,而它在数据资产页里好好列着。而且这个过滤本来就是多余的:
  // 选了板之后连接是唯一确定的(资产登记在哪条连接下,就得用那条),
  // 后端现在按目标数据自动切连接(connection_for_target),不需要人先把连接挑对。
  //
  // 景区维度保留:集团级资产(归属为空)所有景区共用,景区自己的排在一起 —— 换景区就换一批可选看板。
  useEffect(() => {
    if (executorType !== 'quickbi' || !targetId) { setQbiAssets([]); setQbiFixed([]); return; }
    setQbiTarget('');
    setQbiFixedId('');
    api.qbiListAssets({ scenic_id: qbiScenic || undefined })
      .then((r) => setQbiAssets(r.items || [])).catch(() => setQbiAssets([]));
    api.qbiListFiles(targetId)
      .then((r) => setQbiFiles(r.items || [])).catch(() => setQbiFiles([]));
    api.qbiListFixed({ connection_id: targetId, scenic_id: qbiScenic || undefined,
                       domain: qbiDomain || undefined })
      .then((r) => setQbiFixed(r.items || [])).catch(() => setQbiFixed([]));
  }, [executorType, targetId, qbiScenic, qbiDomain]);

  useEffect(() => {
    if (executorType !== 'quickbi' || qbiDomains.length) return;
    api.qbiListDomains().then((r) => setQbiDomains(r.items || [])).catch(() => { /* 没有就不筛 */ });
  }, [executorType]);

  const startNewSession = () => {
    // 只清前端状态。后端会话在第一条消息发出时才创建——避免点一次「新建」就在
    // 数据库里留一条永远没有内容的空会话。
    setActiveSessionId(null);
    setMessages([]);
    setTargetId('');
    // 清掉哨兵:上一次新会话若异常中断,残留的忙碌标记会让新会话一直发不出去
    markBusy(NEW_SESSION, false);
  };


  /** E 方案(v0.1.5):对话内凭证确认超时(毫秒),与后端 _CREDENTIAL_CONFIRM_TTL_SECONDS 一致。
   *  超时仍未确认的,前端自动按「忽略」处理,防止任务永久挂起。 */
  const CREDENTIAL_CONFIRM_TTL_MS = 10 * 60 * 1000;

  /** 收到确认请求后注册超时自动忽略(幂等:后端对已恢复的任务返回 409,静默忽略即可)。 */
  const scheduleCredentialAutoIgnore = (taskId: string, assistantMsgId: string) => {
    setTimeout(() => {
      api.resolveCredentialConfirm(taskId, 'ignore').catch(() => {});
      setMessages((prev) => prev.map((m) =>
        m.id === assistantMsgId && m.credentialConfirmState === 'pending'
          ? { ...m, credentialConfirmState: 'ignored', credentialConfirmNote: '确认超时,已自动忽略' }
          : m));
    }, CREDENTIAL_CONFIRM_TTL_MS);
  };

  /** E 方案:用户点「确认保存 / 忽略」,把决定发给后端,后端恢复任务继续执行。 */
  const handleCredentialConfirm = async (msg: ChatMsg, action: 'approve' | 'ignore') => {
    const cc = msg.credentialConfirm;
    if (!cc?.taskId) return;
    // 先乐观更新,防止快速连点发出两个请求
    setMessages((prev) => prev.map((m) =>
      m.id === msg.id ? { ...m, credentialConfirmState: action === 'approve' ? 'approved' : 'ignored' } : m));
    try {
      const r = await api.resolveCredentialConfirm(cc.taskId, action);
      if (r.action === 'expired') {
        setMessages((prev) => prev.map((m) =>
          m.id === msg.id ? { ...m, credentialConfirmState: 'expired', credentialConfirmNote: '确认已超时,已自动忽略' } : m));
      }
    } catch (e: any) {
      setMessages((prev) => prev.map((m) =>
        m.id === msg.id ? { ...m, credentialConfirmState: 'pending' } : m));
      toast.error(e.message || '确认失败,请重试');
    }
  };

  /** 消费某个任务的 SSE 流并写回对应的助手消息。
   *  抽出来是因为两个地方都要用:发送新消息,以及**打开历史会话时重新挂回未跑完的轮次**。 */
  const consumeStream = async (task_id: string, assistantMsgId: string) => {
    let lastEventTime = Date.now();
    // SSE 断点续传:按 task 记录最后消费的事件 id,刷新/重连时通过 Last-Event-ID 头
    // 只续传新事件,不再每次从内存流开头全量重放(否则执行链一次性刷出 + 重复累积)。
    // 游标**只在本页生命周期内有效**,不能进 sessionStorage。
    // 它记的是「这些事件我已经渲染过了」,而渲染结果活在 React state 里 ——
    // 一刷新 state 全没了,游标却还在,于是重新挂流时后端只推新事件,
    // 已经跑过的那十几步执行链**永远不会回来**;而任务还在 RUNNING,
    // timeline 要等收尾才落库,历史接口也给不出来,页面上就是一片空白。
    // 放在内存里:同一页内的断线重连照常从断点续,刷新则从头全量重放(有 _sseId 去重兜底)。
    let lastEventId = _sseCursors.get(task_id);
    // 双保险去重:后端全量重放/重复推送的历史事件,按 _sseId 跳过,不重复渲染。
    const seenSseIds = new Set<string>();
    await api.streamChat(task_id, (evt) => {
        const now = Date.now();
        const deltaMs = now - lastEventTime;
        lastEventTime = now;
        const d0 = evt.data as any;
        if (d0?._sseId != null) {
          lastEventId = String(d0._sseId);
          _sseCursors.set(task_id, lastEventId);
          if (seenSseIds.has(lastEventId)) return;  // 已渲染过的重放事件,跳过
          seenSseIds.add(lastEventId);
        }
        setMessages((prev) => prev.map((m) => {
          if (m.id !== assistantMsgId) return m;
          // taskId 在流开始时就钉上:右侧详情面板要显示运行 ID,
          // 「中止执行」也要靠它。等 done 事件才赋值的话,执行中反而中止不了。
          const next = { ...m, steps: [...(m.steps || [])], taskId: m.taskId ?? task_id };
          // 确认/审批通过后任务恢复执行,后续 stage/timeline/token 事件会把
          // waiting_human 状态切回 streaming,重新显示「正在思考」光标。
          const resumeToStreaming = () => { if (next.status === 'waiting_human') next.status = 'streaming'; };
          if (evt.event === 'token') {
            resumeToStreaming();
            next.content += (evt.data as any).delta;
          } else if (evt.event === 'plan') {

            // 计划整份到达(执行开始前)。后面还没跑的步骤也要显示出来,

            // 用户才知道总共几步、现在第几步

            next.plan = evt.data as any;

          } else if (evt.event === 'timeline' && (evt.data as any)?.step === 'plan_update') {

            // 步骤状态更新:整份替换,不做增量合并 ——

            // 后端推的就是完整快照,增量合并只会引入不一致

            next.plan = (evt.data as any).plan;
          } else if (evt.event === 'timeline') {
            resumeToStreaming();
            const d = evt.data as any;
            // 耗时:优先用后端按真实 ts 算的 duration(相邻事件真实间隔);前端 deltaMs
            // 是相邻 SSE 事件**到达间隔**,断线重放/重连场景会严重失真(如 Triager 实际
            // 等 30s 超时却只显示几秒),只作兜底。
            const dur = typeof d.duration === 'number' ? d.duration : deltaMs;
            // v1.1.2:generation 有「开始打点(status=streaming)」与「完成态」两个事件,
            // 合并成一个节点——实时流先显示「内容生成中…」,完成态事件到达时原位替换
            // (回放/落库只存在完成态节点,天然无重复)。
            if (d.step === 'generation') {
              // v1.1.6 修复:多段内容生成时,新一段 generation 的「开始打点」不能再把已完成的
              // 历史 generation 节点一并清掉(旧逻辑 filter 全部 generation 节点,导致执行链
              // 只剩最后一段)。这里只移除上一次的「内容生成中…」流式占位(status=streaming),
              // 已完成节点原样保留——保证「几次内容生成 = 几个节点」。
              next.steps = next.steps!.filter((s: any) => !(s.step === 'generation' && s.data?.status === 'streaming'));
            }
            next.steps!.push({ id: `${next.steps!.length}`, step: d.step, status: 'success', deltaMs: dur, data: d });
          } else if (evt.event === 'usage') {
            const u = evt.data as any;
            // 流式过程中的 usage 事件没有成本,先按固定估算占位;done 后以后端真实 cost 覆盖
            next.tokens = { prompt: u.prompt_tokens, completion: u.completion_tokens,
                            cost: u.cost_cny ?? (u.prompt_tokens + u.completion_tokens) * 0.000015,
                            modelCost: u.model_cost_cny ?? null,
                            costReport: u.cost_report ?? undefined };
          } else if (evt.event === 'done') {
            next.status = 'done';
            // 本轮实际走的执行模式(L0/L2/L4),从完整 timeline 里取。
            // **不能按 SSE 事件取** —— mode_decision 是放在 graph_input 的初始
            // timeline 里随 checkpoint 落库的,不单独推事件。
            // 排查「为什么这次没规划」时,这是第一条线索。
            const _md = ((evt.data as any)?.timeline || [])
              .find((e: any) => e?.step === 'mode_decision');
            if (_md?.mode) next.mode = _md.mode;
            if (!next.content) next.content = (evt.data as any).answer || '';
            // 最终成本覆盖流式占位(配置价/litellm 自动匹配/兜底)
            const doneCost = (evt.data as any)?.usage?.cost_cny;
            const doneModelCost = (evt.data as any)?.usage?.model_cost_cny;
            const doneReport = (evt.data as any)?.usage?.cost_report;
            const doneBudget = (evt.data as any)?.usage?.token_budget;
            if (doneCost != null || doneModelCost != null || doneReport || doneBudget != null) {
              const prev: ChatMsg['tokens'] = next.tokens ?? { prompt: 0, completion: 0, cost: 0 };
              next.tokens = {
                prompt: prev?.prompt ?? 0,
                completion: prev?.completion ?? 0,
                cost: doneCost ?? prev?.cost ?? 0,
                modelCost: doneModelCost ?? null,
                costReport: doneReport ?? prev?.costReport,
                duration: prev?.duration,
                // v1.1.2:整轮执行总耗时(后端 = 执行分析结束时刻 - 开始时刻,毫秒)
                totalDurationMs: (evt.data as any)?.usage?.total_duration_ms,
                // 真实预算随 done 下发,右侧面板据此决定画不画进度条(0 = 不限制,不画)
                budget: doneBudget ?? prev?.budget ?? null,
              };
            }
            // 任务执行很快或 SSE 重连时任务已终态:后端不再逐条推 timeline 事件,
            // 而是把完整 timeline 塞进 done 事件的 data 里(_final_event)。
            // 此时 steps 还是空的,执行链分析卡片就不会渲染——这里从 done 补回来。
            const tl = (evt.data as any)?.timeline;
            if (Array.isArray(tl) && !(next.steps || []).length) {
              next.steps = tl.map((e: any, i: number) => ({
                id: `done-${i}`, step: e.step, status: 'success',
                deltaMs: typeof e.duration === 'number' ? e.duration : 0, data: e,
              }));
            }
            // 引用溯源:知识检索来源明细(回答里 [n] 标注对应)
            const doneCites = (evt.data as any)?.citations;
            if (Array.isArray(doneCites) && doneCites.length) next.citations = doneCites;
            // 智能体画的图。**兜底在这里而不是只靠实时 chart 事件**:
            // 任务跑得快或 SSE 重连时,后端不再逐条推事件,而是把结果整份塞进 done ——
            // 只听实时事件的话,那几种情况下图会凭空消失,而回答里还写着「雷达图已生成 ✅」。
            const doneCharts = (evt.data as any)?.structured?.charts;
            if (Array.isArray(doneCharts) && doneCharts.length) {
              next.charts = doneCharts.map(agentChartToCard).filter(Boolean) as api.QbiChart[];
            }
          } else if (evt.event === 'chart') {
            // 实时图表事件:一张一张来,边跑边显示
            const card = agentChartToCard(evt.data as any);
            if (card) next.charts = [...(next.charts || []), card];
          } else if (evt.event === 'waiting_human') {
            next.status = 'waiting_human';
          } else if (evt.event === 'credential_confirm') {
            // E 方案(v0.1.5):请求保存凭证,渲染「确认保存 / 忽略」卡片,等用户本人决定。
            const d = evt.data as any;
            next.status = 'waiting_human';
            next.credentialConfirm = {
              taskId: d.task_id, toolId: d.tool_id, toolName: d.tool_name,
              targetToolId: d.target_tool_id, scope: d.scope, createdAt: d.created_at,
            };
            next.credentialConfirmState = 'pending';
            scheduleCredentialAutoIgnore(task_id, assistantMsgId);
          } else if (evt.event === 'stage') {
            // 后端实际发布的执行阶段事件(understanding/querying/generating),
            // 携带人类可读的提示文案。之前前端只处理了 status,而后端发的是
            // stage —— 名字对不上导致等待时只有一个光标,不知道在干什么。
            resumeToStreaming();
            const d = evt.data as any;
            next.statusHint = d.text || d.stage || '';
          } else if (evt.event === 'status') {
            // 后端每隔几秒回报一次任务状态(排队中/执行中/等待审批/等待确认)。
            // 没有它,任务卡在队列里时前端只有一个转圈图标,看不出在等什么。
            const d = evt.data as any;
            next.statusHint = d.message || d.status;
            // 重连恢复:任务挂在「对话内凭证确认」上时,status 会带上 credential_confirm,
            // 据此重建确认卡片(不携带任何明文)。
            if (d.credential_confirm && !next.credentialConfirm) {
              const cc = d.credential_confirm;
              next.status = 'waiting_human';
              next.credentialConfirm = {
                taskId: cc.task_id, toolId: cc.tool_id, toolName: cc.tool_name,
                targetToolId: cc.target_tool_id, scope: cc.scope, createdAt: cc.created_at,
              };
              next.credentialConfirmState = 'pending';
              scheduleCredentialAutoIgnore(task_id, assistantMsgId);
            }
          } else if (evt.event === 'error') {
            next.status = 'error'; next.errorMessage = (evt.data as any).message;
          } else if (evt.event === 'cancelled') {
            const cd = evt.data as any;
            next.status = 'cancelled'; next.statusHint = cd.message || '已停止生成';
            // 后端 cancelled 终态携带权威部分回答(与历史落库同口径):停止瞬间以它兜底,
            // 保证「已停止生成 + answer(半截)」在任何重连/兜底场景都不丢。
            if (typeof cd.answer === 'string' && cd.answer) next.content = cd.answer;
          }
          return next;
        }));
    }, undefined, lastEventId);
  };

  /** Quick BI 一轮:直连 /admin/quickbi/run 的 SSE,事件 reduce 进消息的 qbi 字段。
   *  不走任务引擎 —— 那条链路要建 task/会话/检查点,而 Quick BI 这一问是它自己端到端完成的。 */
  const sendQuickBI = async (text: string, fixedId?: string) => {
    const fq = fixedId ? qbiFixed.find((f) => f.id === fixedId) : undefined;
    const [tKind0, tId0] = qbiTarget.split(':');
    const board0 = tKind0 === 'asset' ? qbiAssets.find((a) => a.id === tId0 && a.kind === 'dashboard') : undefined;
    // 「一键解读」:解读一块仪表板时不写问题也能发,按默认口径做整体解读(Quick BI 智能解读同款)
    const shown = text.trim() || fq?.question || fq?.title
      || (qbiCap === 'insight' && board0 ? ONE_CLICK_INSIGHT : '');
    if (!shown) { toast.error('请输入问题,或选择一条固定问题'); return; }
    if (!targetId) { toast.error('请先选择 Quick BI 连接'); return; }

    const userMsg: ChatMsg = { id: `u-${Date.now()}`, role: 'user', content: shown, time: nowLabel() };
    const aid = `a-${Date.now()}`;
    const [tKind, tId] = qbiTarget.split(':');
    const asset = tKind === 'asset' ? qbiAssets.find((a) => a.id === tId) : undefined;
    const pickedFile = tKind === 'file' ? qbiFiles.find((f) => f.id === tId) : undefined;
    const assistantMsg: ChatMsg = {
      id: aid, role: 'assistant', content: '', time: nowLabel(), status: 'streaming',
      qbi: { ...emptyLive(shown), capability: qbiCap === 'auto' ? (fq?.capability || undefined) : qbiCap },
    };
    setMessages((prev) => [...prev, userMsg, assistantMsg]);
    setInput('');
    const busyKey = activeSessionId || NEW_SESSION;
    markBusy(busyKey, true);
    qbiAbort.current = new AbortController();
    const patch = (fn: (l: QbiLive) => QbiLive) =>
      setMessages((prev) => prev.map((m) => (m.id === aid ? { ...m, qbi: fn(m.qbi || emptyLive(shown)) } : m)));

    const target = asset ? {
        asset_id: asset.id, asset_kind: asset.kind,
        // 解读 / 报表问数 + 仪表板:带上提问范围与图表范围(chart_ids 只有勾掉一部分才传,不传 = 全选)
        ...scopeFields(qbiCap, asset.kind === 'dashboard', qbiChartIds, qbiLegacyPickOne),
      }
      : pickedFile ? { file_id: pickedFile.id }
      // 固定问题上直接选的数据集 / 仪表板(没登记成资产):按原样传
      : directTarget(qbiTarget)
      // 资产不在当前列表里(比如按景区筛掉了):照样按 id 传,由后端校验
      ?? (tKind === 'asset' && tId ? { asset_id: tId } : undefined);
    let sessionId = activeSessionId;
    let runId: string | null = null;
    try {
      // 每一问都落在一个会话里,这样它才会出现在左侧列表、也才能多轮追问
      // (会话会粘住目标数据与选表结果)。没有活动会话就先开一个。
      if (!sessionId) {
        const created = await api.qbiCreateSession({
          connection_id: targetId,
          capability: qbiCap === 'auto' ? (fq?.capability || 'auto') : qbiCap,
          target: target || {},
          title: shown.slice(0, 40),
          scenic_id: qbiScenic || undefined,
        });
        sessionId = created.id;
        setActiveSessionId(created.id);
        markBusy(created.id, true);
        markBusy(NEW_SESSION, false);
        loadSessions();
      }
      const onEvent = (e: any) => {
        patch((l) => reduceQbi(l, e));
        if (e.type === 'run' && e.run_id) runId = e.run_id;
        if (e.type === 'done') {
          runId = e.run_id || runId;
          setMessages((prev) => prev.map((m) => (m.id === aid
            ? { ...m, status: e.status === 'failed' ? 'error' : e.status === 'running' ? 'streaming' : 'done',
                errorMessage: e.error || undefined,
                // 气泡正文取最终回答,复制/朗读这些既有能力就能直接用上
                content: e.answer || m.content }
            : m)));
          // 报告没跑完(status=running):服务端后台在继续收,这里挂个轮询把结果显示出来
          if (e.status === 'running' && (e.run_id || runId)) trackReport(e.run_id || runId!, aid);
        }
      };
      if (fixedId) {
        // 固定问题:走 /run 带 fixed_question_id,但把 session_id 一起交给后端,
        // 这一轮同样归到当前会话名下
        // 能力/目标数据照样带上:控件是可改的,改了就以控件为准
        // (后端对固定问题的规则是「调用方显式传的字段优先」)
        await api.qbiRunStream({
          connection_id: targetId, fixed_question_id: fixedId,
          capability: qbiCap === 'auto' ? undefined : qbiCap,
          target: target || undefined,
          scenic_id: qbiScenic || undefined, session_id: sessionId || undefined,
        }, onEvent, qbiAbort.current.signal);
      } else {
        await api.qbiChatStream(sessionId!, {
          question: shown,
          capability: qbiCap === 'auto' ? undefined : qbiCap,
          target,
        }, onEvent, qbiAbort.current.signal);
      }
    } catch (err: any) {
      const aborted = err?.name === 'AbortError';
      setMessages((prev) => prev.map((m) => (m.id === aid
        ? { ...m, status: aborted ? 'cancelled' : 'error',
            statusHint: aborted ? '已停止跟踪(报告仍在后台生成)' : undefined,
            errorMessage: aborted ? undefined : (err?.message || 'Quick BI 执行失败') } : m)));
      if (!aborted) toast.error(err?.message || 'Quick BI 执行失败');
    } finally {
      markBusy(busyKey, false);
      markBusy(NEW_SESSION, false);
      if (sessionId) markBusy(sessionId, false);
      qbiAbort.current = null;
      loadSessions();      // 轮次数与排序由后端维护,跑完拉一次
    }
  };

  const handleSend = async (text: string = input) => {
    stickToBottomRef.current = true;   // 自己发的消息:回到底部跟随这一轮
    if (executorType === 'quickbi') {
      if (isProcessing) { toast.info('上一轮还在执行,请等它结束'); return; }
      await sendQuickBI(text, qbiFixedId || undefined);
      return;
    }
    const isDebug = mode === 'debug';
    let params: Record<string, unknown> | undefined;
    if (isDebug) {
      if (!targetId) { toast.error('请先选择调试目标'); return; }
      try { params = text.trim() ? JSON.parse(text) : {}; }
      catch { toast.error('调试参数需要是合法 JSON'); return; }
    } else {
      if (!text.trim()) return;
      if (isProcessing) {
        // 发送按钮虽然 disabled,但键盘回车/输入框内操作仍可能走到这里;
        // 以前是静默 return——用户只见光标闪、消息发不出,完全不知道为什么。
        toast.info('上一轮任务仍在执行,请等待其结束后再发送');
        return;
      }
      if (!targetId) { toast.error('请先在左上角选择 Agent 或 Skill'); return; }
    }

    const userMsg: ChatMsg = { id: `u-${Date.now()}`, role: 'user', content: isDebug ? text || '{}' : text, time: nowLabel() };
    const assistantMsg: ChatMsg = { id: `a-${Date.now()}`, role: 'assistant', content: '', time: nowLabel(), steps: [], status: 'streaming' };
    setMessages((prev) => [...prev, userMsg, assistantMsg]);
    setInput('');
    const busyKey = activeSessionId || NEW_SESSION;
    markBusy(busyKey, true);

    let lastEventTime = Date.now();
    let resp: Awaited<ReturnType<typeof api.sendChat>> | null = null;
    try {
      const overrides: api.ChatOverrides = {};
      if (overrideModel.trim()) overrides.model = overrideModel.trim();
      if (overrideKs.trim()) overrides.knowledge_sources = { mode: 'replace', sources: overrideKs.split(',').map((s) => s.trim()).filter(Boolean) };

      // 对话模式下始终归属于某个会话:有 activeSessionId 就接着聊(带历史上下文),
      // 没有就让后端新建一个。调试模式是一次性调用,不建会话。
      resp = await api.sendChat({
        executorType, executorId: targetId, message: isDebug ? undefined : text, params,
        overrides: Object.keys(overrides).length ? overrides : undefined,
        // 15 号两层版本选择:显式钉版本 chip 才携带 executorVersionId;未选(自动解析)时
        // 后端按当前用户指派命中,无默认版本兜底,未指派将提示 agent_not_distributed
        executorVersionId: (executorType === 'agent' || executorType === 'skill') && targetVersionId ? targetVersionId : undefined,
        conversationId: isDebug ? null : activeSessionId,
        newConversation: isDebug ? false : !activeSessionId,
      });
      const task_id = resp.task_id;
      if (resp.conversation_id && resp.conversation_id !== activeSessionId) {
        setActiveSessionId(resp.conversation_id);
        // 忙碌标记从哨兵迁到真实会话 id,否则这一轮跑完前用户切走再切回来会显示成"空闲"
        markBusy(resp.conversation_id, true);
        markBusy(NEW_SESSION, false);
      }
      // 拿到会话 id 立即刷新列表:新会话要马上出现在顶部并高亮,否则流式回答期间
      // 左侧列表还是旧的,用户会以为在跟上一个会话交互。finalize 里还会再拉一次,
      // 那时标题/轮数/排序才是最终值。
      if (mode === 'chat') loadSessions();
      (resp.diagnostics || []).forEach((d) => toast.info(d));

      await consumeStream(task_id, assistantMsg.id);
    } catch (e: any) {
      setMessages((prev) => prev.map((m) => (m.id === assistantMsg.id ? { ...m, status: 'error', errorMessage: e.message } : m)));
      toast.error(e.message || '执行失败');
    } finally {
      // 新会话在发送过程中才拿到真实 id,这里哨兵、旧会话 key、真实会话 id 都清一遍,
      // 避免残留的忙碌标记把整个会话锁死(症状:第二轮发不出、发送按钮点不动)
      markBusy(busyKey, false);
      markBusy(NEW_SESSION, false);
      if (resp?.conversation_id) markBusy(resp.conversation_id, false);
      if (mode === 'chat') loadSessions();   // 轮次数/排序由后端维护,跑完拉一次
    }
  };

  const getTargetIcon = (type: string) => {
    switch (type) {
      case 'agent': return <Bot className="h-4 w-4" />;
      case 'skill': return <Zap className="h-4 w-4" />;
      case 'tool': return <Cpu className="h-4 w-4" />;
      case 'mcp': return <Boxes className="h-4 w-4" />;
      case 'quickbi': return <BarChart3 className="h-4 w-4" />;
      default: return <Terminal className="h-4 w-4" />;
    }
  };

  // allSteps:同一轮的全部步骤,版本溯源据此只展示「实际调用到」的工具
  const renderTimelineStep = (step: ExecutionStep, index: number, total: number, inProgress: boolean,
                              allSteps?: ExecutionStep[]) => {
    const isLast = index === total - 1;
    const meta = STEP_META[step.step] || { icon: Activity, label: () => step.step };
    return (
      <div key={step.id} className="relative pl-6 pb-4 last:pb-0">
        {!isLast && <div className="absolute left-[7px] top-[20px] bottom-0 w-[1px] bg-slate-200" />}
        <div className={cn(
          'absolute left-0 top-[6px] w-[14px] h-[14px] rounded-full border-2 bg-white flex items-center justify-center z-10',
          inProgress && isLast ? 'border-blue-500 bg-blue-500' : 'border-emerald-500 bg-emerald-500'
        )}>
          <div className="w-1.5 h-1.5 rounded-full bg-white" />
        </div>
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <meta.icon className="h-3 w-3 text-slate-400" />
            <span className="text-xs font-medium text-slate-700">{meta.label(step.data)}</span>
          </div>
          <span className="flex items-center gap-1.5 text-[10px] text-slate-400 font-mono">
            {step.data?.wall && <span>{step.data.wall}</span>}
            {/* v1.1.2:tool_call 优先展示后端记录的真实墙钟耗时(real_duration_ms)。
                时间线的 duration 是相邻事件间隔,并行执行的工具会显示成 ~0ms 假象;
                并行时额外标注「并行」,与真实耗时并列,避免误读。 */}
            {step.step === 'tool_call' && step.data?.parallel === true && (
              <span className="text-violet-500 border border-violet-200 bg-violet-50 rounded px-1">并行</span>
            )}
            {step.step === 'tool_call' && typeof step.data?.real_duration_ms === 'number'
              ? <span>{Math.round(step.data.real_duration_ms)}ms</span>
              : (step.deltaMs != null && <span>{step.deltaMs}ms</span>)}
          </span>
        </div>
        {/* v1.1.2:工具入参快照(后端脱敏 + 截断,凭证类工具不记录),确认「为什么这么调」 */}
        {step.step === 'tool_call' && step.data?.args && (
          <div className="mt-1.5 ml-[26px] text-[11px] font-mono text-slate-600 bg-slate-50 border border-slate-100 rounded-md px-2.5 py-1.5 break-all">
            <span className="font-semibold text-slate-500">入参：</span>{step.data.args}
          </div>
        )}
        {/* v1.1.1:timeline 新增 error 字段(format_error/error 具体原因),回放直接可见 */}
        {step.step === 'tool_call' && (step.data?.error || (step.data?.kind && !['ok', 'skipped'].includes(step.data.kind))) && (
          <div className="mt-1.5 ml-[26px] text-[11px] font-mono text-rose-600 bg-rose-50 border border-rose-100 rounded-md px-2.5 py-1.5 break-all">
            {step.data.error ? `执行失败：${step.data.error}` : `调用结果：${step.data.kind}`}
          </div>
        )}
        {/* v1.1:意图分诊展开「判断依据 → 结果 → 动作对象」,不再只有一句「直接执行」 */}
        {step.step === 'intent_check' && (step.data?.reason || step.data?.degraded
          || step.data?.target_skills?.length || step.data?.missing?.length || step.data?.params_filled) && (
          <div className="mt-1.5 ml-[26px] text-[11px] text-slate-600 bg-slate-50 border border-slate-100 rounded-md px-2.5 py-1.5 space-y-1 break-all">
            {step.data.degraded && (
              <div className="text-amber-600">
                <span className="font-medium">意图分诊未生效，本轮走全量执行（不裁剪、不澄清）</span>
                {/* 降级**为什么**发生:分诊模型没登记、超时、返回非法 JSON,三种的下一步完全不同。
                    只说「不可用」的话,运维拿到的是一个没有下一步的结论 —— 而这条信息后端本来就有。 */}
                {step.data.degraded_reason && (
                  <div className="mt-0.5 font-normal text-amber-700/80">{step.data.degraded_reason}</div>
                )}
              </div>
            )}
            {step.data.reason && <div><span className="font-semibold text-slate-500">判断：</span>{step.data.reason}</div>}
            {!!step.data.target_skills?.length && (
              // v1.1.2:只要让用户知道调用了哪个技能即可,不需要描述
              <div><span className="font-semibold text-slate-500">技能调用：</span>{step.data.target_skills.join('、')}</div>
            )}
            {!!step.data.missing?.length && (
              <div><span className="font-semibold text-slate-500">缺失参数：</span>{step.data.missing.join('、')}</div>
            )}
            {step.data.reply && <div><span className="font-semibold text-slate-500">回复用户：</span>{step.data.reply}</div>}
          </div>
        )}
        {/* v1.1.6(P0-3):版本溯源详情——agent→skill→tool 每层实际来源,一眼看出是否 pin 旧版 */}
        {step.step === 'resolve_provenance' && step.data?.provenance && (
          <div className="mt-1.5 ml-[26px] text-[11px] font-mono text-slate-600 bg-slate-50 border border-slate-100 rounded-md px-2.5 py-1.5 space-y-1 break-all">
            <div className="font-semibold text-slate-500">版本溯源：</div>
            <div><span className="font-semibold text-slate-500">
              {step.data.provenance.agent?.name ? `${step.data.provenance.agent.name}：` : 'Agent：'}
            </span>{provenanceSourceText(step.data.provenance.agent)}</div>
            {Object.entries(step.data.provenance.skills || {}).map(([sid, s]: [string, any]) => (
              <div key={sid}>
                <span className="font-semibold text-slate-500">
                  {s?.name ? `${s.name}：` : `Skill ${String(sid).slice(0, 8)}：`}
                </span>
                {provenanceSourceText(s)}
                {(() => {
                  // 只列本轮实际调用到的工具(随 tool_call 逐步出现);其余已绑定的只给数量,点开再看
                  const { used, unused } = splitProvenanceTools(s?.tools, usedToolKeys(allSteps));
                  const row = ({ id, tool: t }: ProvToolRow) => (
                    <div key={id} className="text-slate-500">
                      <span className="text-slate-400">
                        {t?.name ? `${t.name}：` : `工具 ${String(id).slice(0, 8)}：`}
                      </span>
                      {provenanceSourceText(t)}
                    </div>
                  );
                  if (!used.length && !unused.length) return null;
                  return (
                    <div className="ml-3 mt-0.5 space-y-0.5 border-l border-slate-200 pl-2">
                      {used.map(row)}
                      {!used.length && (
                        <div className="text-slate-400">{inProgress ? '尚未调用工具' : '本轮未调用工具'}</div>
                      )}
                      {unused.length > 0 && (
                        <details className="text-slate-400">
                          <summary className="cursor-pointer select-none hover:text-slate-500">
                            其余 {unused.length} 个已绑定工具{inProgress ? '暂未' : '未'}调用
                          </summary>
                          <div className="mt-0.5 space-y-0.5">{unused.map(row)}</div>
                        </details>
                      )}
                    </div>
                  );
                })()}
              </div>
            ))}
          </div>
        )}
      </div>
    );
  };
  // ── 会话按日期分组:今天 / 昨天 / 更早。
  // 一长串等宽的标题扫不出时间感,分组之后「刚才那个会话」一眼就能找到。
  const sessionGroups = (() => {
    const today = new Date(); today.setHours(0, 0, 0, 0);
    const yest = new Date(today.getTime() - 86400000);
    const buckets: { label: string; items: SessionItem[] }[] =
      [{ label: '今天', items: [] }, { label: '昨天', items: [] }, { label: '更早', items: [] }];
    sessions.forEach((s) => {
      const t = new Date(s.updated_at);
      buckets[t >= today ? 0 : t >= yest ? 1 : 2].items.push(s);
    });
    return buckets.filter((b) => b.items.length);
  })();

  /** 上传一份数据文件当作这一轮的目标数据(xls/xlsx/csv,≤5MB)。
   *  上传完自动选中 —— 用户点上传就是为了拿它问问题,还要再选一次是多余的。 */
  const uploadQbiFile = async (file: File) => {
    if (!targetId) { toast.error('请先选择 Quick BI 连接'); return; }
    setQbiUploading(true);
    try {
      const f = await api.qbiUploadFile(file, targetId);
      setQbiFiles((prev) => [f, ...prev.filter((x) => x.id !== f.id)]);
      setQbiTarget(`file:${f.id}`);
      if (f.warnings?.length) toast.warning(f.warnings.join(';'));
      else toast.success(`已上传 ${f.file_name}`);
    } catch (e: any) {
      toast.error(e?.message || '上传失败');
    } finally {
      setQbiUploading(false);
      if (qbiFileInput.current) qbiFileInput.current.value = '';
    }
  };

  /** 下载这一次运行的执行详情(Markdown)。浏览器直接存文件,不经过剪贴板 ——
   *  这份东西通常几十上百 KB,复制粘贴既不可靠也读不了。 */
  const exportRunDetail = async (runId: string) => {
    try {
      const md = await api.qbiRunDebugMarkdown(runId);
      const url = URL.createObjectURL(new Blob([md], { type: 'text/markdown;charset=utf-8' }));
      const a = document.createElement('a');
      a.href = url;
      a.download = `quickbi-run-${runId.slice(0, 8)}.md`;
      a.click();
      URL.revokeObjectURL(url);
      toast.success('已导出执行详情(不含任何凭证)');
    } catch (e: any) {
      toast.error(e?.message || '导出失败');
    }
  };

  /** 下载这一次**智能体**执行的完整链条(Markdown)。
   *
   *  与 Quick BI 那个按钮对称。有了它,出问题时不用再靠截图互相猜 ——
   *  截图里恰恰缺最要紧的那几样:每一步的提示词、工具入参、结果摘要。凭证已脱敏。 */
  const exportTaskDetail = async (taskId: string) => {
    try {
      const md = await api.taskDebugMarkdown(taskId);
      const url = URL.createObjectURL(new Blob([md], { type: 'text/markdown;charset=utf-8' }));
      const a = document.createElement('a');
      a.href = url;
      a.download = `agent-task-${taskId.slice(0, 8)}.md`;
      a.click();
      URL.revokeObjectURL(url);
      toast.success('已导出执行详情(不含任何凭证)');
    } catch (e: any) {
      toast.error(e?.message || '导出失败');
    }
  };

  const relTime = (iso: string) => {
    const diff = Date.now() - new Date(iso).getTime();
    if (diff < 60000) return '刚刚';
    if (diff < 3600000) return `${Math.floor(diff / 60000)}分钟前`;
    if (diff < 86400000) return `${Math.floor(diff / 3600000)}小时前`;
    return new Date(iso).toLocaleDateString([], { month: 'numeric', day: 'numeric' });
  };

  const TYPE_LABEL: Record<string, string> = {
    agent: 'Agent', skill: 'Skill', tool: 'Tool', mcp: 'MCP', quickbi: 'Quick BI',
  };

  const [targetKind, targetId2] = qbiTarget.split(':');
  /**
   * 目标数据按「属于哪块仪表板」分组。
   *
   * 数据集不是独立的东西 —— 它是某块板下面的一张表(板的 meta.cubes 里列着自己用到哪些 cube)。
   * 混成一个大列表之后「这个集属于哪块板」只能靠名字猜,两块板各十几个集时根本分不清,
   * 于是很容易拿 A 板的集去问 B 板的问题,答出来还像模像样。
   * 板下面挂它自己的集;两块板都没认领的集单独放一组(多半是单独登记的)。
   */
  const assetGroups = React.useMemo(() => {
    const boards = qbiAssets.filter((a) => a.kind === 'dashboard');
    const sets = qbiAssets.filter((a) => a.kind === 'dataset');
    const taken = new Set<string>();
    const groups = boards.map((b) => {
      const cubeIds = new Set((b.cubes || []).map((c) => c.cube_id));
      const mine = sets.filter((d) => cubeIds.has(d.ref_id));
      mine.forEach((d) => taken.add(d.id));
      return {
        key: b.id,
        label: `${b.name}${b.scenic_id ? '' : '（集团级）'} · 数据集 ${mine.length}`,
        items: [b, ...mine],
      };
    });
    const orphans = sets.filter((d) => !taken.has(d.id));
    if (orphans.length) groups.push({ key: '__orphan__', label: '未归属仪表板的数据集', items: orphans });
    return groups;
  }, [qbiAssets]);

  const activeAsset = targetKind === 'asset' ? qbiAssets.find((a) => a.id === targetId2) : undefined;
  const activeFile = targetKind === 'file' ? qbiFiles.find((f) => f.id === targetId2) : undefined;
  const activeFixed = qbiFixed.find((f) => f.id === qbiFixedId);
  const boundTargetLabel = (() => {
    if (!qbiTarget) return '';
    const [k, id] = qbiTarget.split(':');
    if (k === 'asset' && qbiAssets.some((a) => a.id === id)) return '';
    if (k === 'file' && qbiFiles.some((f) => f.id === id)) return '';
    const name = activeFixed?.target_data_name || activeFixed?.target?.cube_name || activeFixed?.target?.asset_name || id;
    if (k === 'cubes') return `集｜多表问数 ${id.split(',').length} 个数据集（问题绑定）`;
    const tag = k === 'cube' ? '集' : k === 'page' ? '板' : k === 'file' ? '文件' : '资产';
    return `${tag}｜${name}（问题绑定）`;
  })();
  const lastQbi = lastAssistant?.qbi;

  // 右侧面板:两条执行链路(任务引擎 / Quick BI)喂给同一个面板,
  // 免得同一块地方长出两套长相不同的详情。
  const runInfo: import('../components/common/RunDetailPanel').RunInfo | undefined =
    executorType === 'quickbi'
      ? (lastQbi ? {
          taskId: lastQbi.runId,
          mode: lastQbi.capability ? (CAP_LABEL[lastQbi.capability] || lastQbi.capability) : undefined,
          running: lastQbi.streaming,
          targetName: activeTarget?.name || 'Quick BI',
          targetSub: lastQbi.connectionName || activeTarget?.desc,
          tokens: lastQbi.usage ? {
            prompt: lastQbi.usage.prompt_tokens ?? 0,
            completion: lastQbi.usage.completion_tokens ?? 0,
            cost: 0,
            totalDurationMs: lastQbi.done?.duration_ms,
          } : (lastQbi.done?.duration_ms != null
            ? { prompt: 0, completion: 0, cost: 0, totalDurationMs: lastQbi.done.duration_ms } : undefined),
          extraRows: [
            ...(lastQbi.meta?.cube_name ? [{ label: '数据集', value: lastQbi.meta.cube_name }] : []),
            ...(lastQbi.traceId ? [{ label: 'traceId', value: lastQbi.traceId, mono: true }] : []),
          ],
          calls: [
            ...lastQbi.steps.map((st, i) => ({
              id: `qs-${i}`, title: st.title, sub: st.desc, status: 'done' as const,
            })),
            ...(lastQbi.streaming
              ? [{ id: 'qs-live', title: lastQbi.statuses[lastQbi.statuses.length - 1]?.message || '执行中…',
                   status: 'running' as const }]
              : []),
          ],
          contextItems: [
            ...(qbiScenic ? [{ icon: Mountain, label: '景区', value: scenics.find((x) => x.id === qbiScenic)?.name || qbiScenic }] : []),
            ...(activeAsset ? [{ icon: Database, label: '目标数据', value: activeAsset.name }] : []),
            ...(activeFile ? [{ icon: Paperclip, label: '上传文件', value: activeFile.file_name }] : []),
            ...(activeFixed ? [{ icon: ListChecks, label: '固定问题', value: activeFixed.title }] : []),
          ],
        } : (activeTarget ? { targetName: activeTarget.name, targetSub: activeTarget.desc } : undefined))
      : (lastAssistant ? {
          taskId: lastAssistant.taskId,
          mode: lastAssistant.mode,
          tokens: lastAssistant.tokens,
          plan: lastAssistant.plan,
          running: lastAssistant.status === 'streaming',
          targetName: activeTarget?.name,
          targetSub: activeTarget?.desc,
        } : (activeTarget ? { targetName: activeTarget.name, targetSub: activeTarget.desc } : undefined));

  return (
    <div className="flex h-[calc(100vh-56px)] overflow-hidden bg-[#f8fafc]">
      {/* ══════════ 左:会话列表 ══════════ */}
      <div className="flex w-[280px] shrink-0 flex-col border-r border-slate-200 bg-white">
        <div className="border-b border-slate-100 p-4">
          <Button className="h-10 w-full bg-[#1677ff] font-medium text-white shadow-sm hover:bg-[#4096ff]"
                  onClick={startNewSession}>
            <Plus className="mr-2 h-4 w-4" />新建对话
          </Button>
          <div className="mt-3 flex rounded-lg bg-slate-100 p-0.5">
            {(['chat', 'debug'] as const).map((m) => (
              <button key={m} onClick={() => { setMode(m); startNewSession(); }}
                className={cn('flex-1 rounded-md py-1.5 text-xs font-semibold transition-all',
                  mode === m ? 'bg-white text-[#1677ff] shadow-sm' : 'text-slate-500')}>
                {m === 'chat' ? '对话模式' : '调试模式'}
              </button>
            ))}
          </div>
        </div>

        <div className="custom-scrollbar flex-1 overflow-y-auto px-2 py-2">
          {loadingSessions && <div className="px-3 py-4 text-xs text-slate-400">加载中…</div>}
          {!loadingSessions && !sessions.length && (
            <div className="px-3 py-8 text-center text-xs text-slate-400">还没有会话，发一句话就开始了</div>
          )}
          {sessionGroups.map((g) => (
            <div key={g.label} className="mb-2">
              <div className="px-3 py-2 text-[11px] font-semibold text-slate-400">{g.label}</div>
              <div className="space-y-1">
                {g.items.map((session) => (
                  <div key={session.id} onClick={() => openSession(session)}
                    className={cn('group relative cursor-pointer rounded-xl px-3 py-2.5 transition-all',
                      activeSessionId === session.id
                        ? 'bg-blue-50 ring-1 ring-blue-100' : 'hover:bg-slate-50')}>
                    <div className="flex items-start justify-between gap-2">
                      <div className={cn('truncate text-[13px] font-medium',
                        activeSessionId === session.id ? 'text-blue-700' : 'text-slate-800')}>
                        {session.title || '新会话'}
                      </div>
                      <div className="flex shrink-0 items-center gap-0.5">
                        <button onClick={(e) => copySessionId(session.id, e)} title="复制会话 ID"
                          className="text-slate-300 opacity-0 transition-opacity hover:text-blue-500 group-hover:opacity-100">
                          <Copy className="h-3.5 w-3.5" />
                        </button>
                        <button onClick={(e) => archiveSession(session.id, e, session.kind)} title="归档会话"
                          className="text-slate-300 opacity-0 transition-opacity hover:text-rose-500 group-hover:opacity-100">
                          <Trash2 className="h-3.5 w-3.5" />
                        </button>
                      </div>
                    </div>
                    <div className="mt-0.5 truncate text-[11px] text-slate-400">
                      {TYPE_LABEL[session.executor_type] || session.executor_type} · {relTime(session.updated_at)}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* ══════════ 中:对话 ══════════ */}
      <div className="relative flex min-w-0 flex-1 flex-col bg-white">
        {/* 顶栏只交代「现在在跟谁说话」。选执行目标放在底部输入区 ——
            发消息前最后一眼看的是那里,选择器跟着走才顺手。 */}
        <div className="flex h-14 shrink-0 items-center justify-between border-b border-slate-100 px-6">
          <div className="flex min-w-0 items-center gap-2.5">
            <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-blue-50 text-blue-600">
              {getTargetIcon(executorType)}
            </div>
            <div className="min-w-0">
              <div className="truncate text-sm font-bold text-slate-900">
                {activeTarget ? activeTarget.name : `未选择${TYPE_LABEL[executorType]}`}
              </div>
              <div className="flex items-center gap-1.5 text-[11px]">
                {isProcessing ? (
                  <><span className="h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-500" />
                    <span className="text-emerald-600">运行中</span></>
                ) : (
                  <><span className="h-1.5 w-1.5 rounded-full bg-slate-300" />
                    <span className="text-slate-400">{activeTarget ? '就绪' : '请选择执行目标'}</span></>
                )}
              </div>
            </div>
          </div>
          <div className="flex items-center gap-1">
            {mode !== 'debug' && executorType !== 'quickbi' && (
              <Button variant="ghost" size="sm" onClick={() => setShowOverrideModal(true)}
                      className="h-8 gap-1.5 text-xs text-slate-500">
                <Settings2 className="h-3.5 w-3.5" />参数配置
              </Button>
            )}
            <Button variant="ghost" size="sm" onClick={loadSessions} className="h-8 gap-1.5 text-xs text-slate-500">
              <Clock className="h-3.5 w-3.5" />历史记录
            </Button>
            <Button variant="ghost" size="icon" className="h-8 w-8 text-slate-400 hover:text-slate-600" onClick={loadTargets}>
              <RefreshCw className="h-4 w-4" />
            </Button>
          </div>
        </div>

        <div ref={scrollBoxRef} onScroll={onMessagesScroll}
          className="custom-scrollbar flex-1 space-y-6 overflow-y-auto p-6">
          {messages.length === 0 && (
            <div className="flex h-full items-center justify-center text-sm text-slate-400">
              选择执行目标并发送消息，开始一次真实的解析/执行引擎调用
            </div>
          )}
          {messages.length > 0 && (
            <div className="flex items-center justify-center">
              <span className="rounded-full bg-slate-50 px-3 py-1 text-[11px] text-slate-400">
                对话开始 · {new Date().toLocaleDateString([], { year: 'numeric', month: 'long', day: 'numeric' })} {messages[0]?.time}
              </span>
            </div>
          )}
          {messages.map((msg) => (
            <div key={msg.id} className={cn('flex flex-col gap-3', msg.role === 'user' ? 'items-end' : 'items-start')}>
              {msg.role === 'user' ? (
                <div className="flex max-w-[85%] items-start gap-3">
                  <div className="group/umsg flex flex-col items-end gap-1">
                    <div className="mr-1 text-[11px] font-medium text-slate-400">{msg.time} · {me?.name || '我'}</div>
                    <div className="whitespace-pre-wrap rounded-2xl rounded-tr-none bg-[#1677ff] px-5 py-3 text-sm font-medium leading-relaxed text-white shadow-sm">{msg.content}</div>
                    <div className="opacity-0 transition-opacity group-hover/umsg:opacity-100">
                      <CopyButton text={msg.content} />
                    </div>
                  </div>
                  <Avatar className="h-9 w-9 shrink-0 border border-slate-100 shadow-sm">
                    <AvatarFallback className="bg-slate-100 text-xs font-bold text-slate-600">{me?.name?.[0] || 'U'}</AvatarFallback>
                  </Avatar>
                </div>
              ) : (
                <div className="flex max-w-[85%] flex-col gap-4">
                  {(msg.plan?.steps?.length || 0) > 0 && (
                    <div className="ml-12"><PlanTrace plan={msg.plan!} /></div>
                  )}

                  {/* ── 正在思考 / 思考过程 ────────────────────────
                      执行过程本身就是「思考」:跑到一半叫「正在思考…」,
                      跑完了就变成「思考过程」。结论是另一个输出,单独放在下面,
                      过程再长也顶不掉它。 */}
                  {((msg.steps?.length || 0) > 0 || msg.qbi || msg.status === 'streaming') && (() => {
                    const thinking = msg.status === 'streaming';
                    const lastQbiStatus = msg.qbi?.statuses?.length
                      ? msg.qbi.statuses[msg.qbi.statuses.length - 1]?.message
                      : '';
                    const hint = msg.statusHint || lastQbiStatus || '正在思考…';
                    const stepCount = msg.qbi ? (msg.qbi.steps.length || 0) : (msg.steps?.length || 0);
                    const open = openTraces.has(msg.id);
                    return (
                      <Card className={cn('ml-12 overflow-hidden bg-white shadow-sm',
                        thinking ? 'border-blue-200' : 'border-slate-200')}>
                        <button type="button" onClick={() => toggleTrace(msg.id)}
                          className={cn('flex w-full items-center justify-between border-b px-4 py-2.5 text-left transition-colors',
                            thinking
                              ? 'border-blue-100 bg-blue-50/60 hover:bg-blue-50'
                              : 'border-slate-100 bg-slate-50 hover:bg-slate-100')}>
                          <div className="flex min-w-0 items-center gap-2">
                            {open
                              ? <ChevronDown className="h-3.5 w-3.5 shrink-0 text-slate-400" />
                              : <ChevronRight className="h-3.5 w-3.5 shrink-0 text-slate-400" />}
                            {thinking
                              ? <Loader2 className="h-4 w-4 shrink-0 animate-spin text-blue-500" />
                              : <Activity className="h-4 w-4 shrink-0 text-slate-400" />}
                            <span className={cn('shrink-0 text-sm font-semibold', thinking ? 'text-blue-600' : 'text-slate-700')}>
                              {thinking ? '正在思考…' : '思考过程'}
                            </span>
                            {!open && !thinking && stepCount > 0 && (
                              <span className="truncate text-[11px] text-slate-400">
                                {stepCount} 步{msg.qbi?.reasoning ? ' · 含推理' : ''}
                              </span>
                            )}
                          </div>
                          {thinking ? (
                            <span className="ml-3 flex min-w-0 items-center gap-1.5 text-[11px] text-slate-400">
                              <span className="truncate">{hint}</span>
                              <motion.span animate={{ opacity: [1, 0] }} transition={{ duration: 0.8, repeat: Infinity }}
                                className="inline-block h-3 w-1 shrink-0 translate-y-0.5 bg-blue-500" />
                            </span>
                          ) : (
                            <span className={cn('ml-3 shrink-0 rounded border px-2 py-0.5 text-[10px] font-medium',
                              msg.status === 'error'
                                ? 'border-rose-100 bg-rose-50 text-rose-500'
                                : msg.status === 'cancelled'
                                  ? 'border-slate-200 bg-slate-50 text-slate-400'
                                  : 'border-emerald-100 bg-emerald-50 text-emerald-600')}>
                              {msg.status === 'error'
                                ? '思考中断'
                                : msg.status === 'cancelled'
                                  ? '已停止'
                                  : msg.tokens?.totalDurationMs != null
                                    ? `思考完成 · 耗时 ${formatDuration(msg.tokens.totalDurationMs)}`
                                    : msg.qbi?.done?.duration_ms != null
                                      ? `思考完成 · 耗时 ${formatDuration(msg.qbi.done.duration_ms)}`
                                      : '思考完成'}
                            </span>
                          )}
                        </button>
                        {open && (
                          <CardContent className="p-5">
                            {msg.qbi
                              ? <ResultView live={msg.qbi} part="thinking" compact />
                              : (msg.steps?.length
                                  ? msg.steps.map((step, idx) => renderTimelineStep(step, idx, msg.steps!.length, thinking, msg.steps))
                                  : <div className="text-xs text-slate-400">{hint}</div>)}
                          </CardContent>
                        )}
                      </Card>
                    );
                  })()}

                  {/* ── 正式数据 ──────────────────────────────────
                      结果是另外一个输出:思考没出结论前整块不渲染,避免空框。 */}
                  <div className={cn('flex items-start gap-3',
                    msg.status === 'streaming' && !msg.content && !qbiHasData(msg.qbi) && 'hidden')}>
                    <Avatar className="h-9 w-9 shrink-0 border border-slate-100 bg-slate-900 shadow-sm">
                      <Bot className="m-auto h-5 w-5 text-white" />
                    </Avatar>
                    <div className="flex min-w-0 flex-1 flex-col gap-2">
                      <div className="group/msg relative min-w-[100px] rounded-2xl rounded-tl-none border border-slate-200 bg-white px-5 py-4 text-sm leading-relaxed text-slate-700 shadow-sm">
                        {msg.qbi
                          ? <ResultView live={msg.qbi} part="data" compact />
                          : (msg.content && <Markdown content={msg.content} />)}
                        {/* 光标跟着「正在思考」那张卡走了,正文只在真有内容时才闪 */}
                        {msg.status === 'streaming' && (msg.content || qbiHasData(msg.qbi)) && (
                          <motion.span animate={{ opacity: [1, 0] }} transition={{ duration: 0.8, repeat: Infinity }}
                            className="ml-0.5 inline-block h-4 w-1.5 translate-y-0.5 bg-blue-500" />
                        )}
                        {msg.content && msg.status !== 'streaming' && !msg.qbi && (
                          <div className="mt-2 flex justify-end opacity-0 transition-opacity group-hover/msg:opacity-100">
                            <CopyButton text={msg.content} label="复制" />
                          </div>
                        )}
                        {msg.status === 'waiting_human' && !msg.credentialConfirm && (
                          <div className="mt-3 flex items-center gap-2 border-t border-slate-100 pt-3 text-xs text-amber-600">
                            <AlertTriangle className="h-3.5 w-3.5" />命中高风险工具，已转审批中心等待人工审批，通过后将自动恢复执行。
                          </div>
                        )}
                        {msg.credentialConfirm && (
                          <div className="mt-3 border-t border-slate-100 pt-3">
                            <div className="flex items-center gap-2 text-xs text-amber-600">
                              <KeyRound className="h-3.5 w-3.5 shrink-0" />
                              <span>
                                检测到要保存
                                <strong className="mx-1">{msg.credentialConfirm.toolName || '登录'}</strong>
                                凭证，请确认是否保存（仅本人可见，不展示账号密码）
                              </span>
                            </div>
                            {msg.credentialConfirmState === 'pending' ? (
                              <div className="mt-2 flex gap-2">
                                <Button size="sm" onClick={() => handleCredentialConfirm(msg, 'approve')}
                                  className="h-8 bg-[#1677ff] px-3 text-xs text-white hover:bg-[#4096ff]">确认保存</Button>
                                <Button size="sm" variant="outline" onClick={() => handleCredentialConfirm(msg, 'ignore')}
                                  className="h-8 px-3 text-xs text-slate-600">忽略</Button>
                              </div>
                            ) : (
                              <div className="mt-1.5 text-[11px] text-slate-400">
                                {msg.credentialConfirmNote || (msg.credentialConfirmState === 'approved'
                                  ? '已确认保存，凭证仅本人可见'
                                  : msg.credentialConfirmState === 'expired'
                                    ? '确认已超时，已自动忽略'
                                    : '已忽略，不保存该凭证')}
                              </div>
                            )}
                          </div>
                        )}
                        {msg.status === 'cancelled' && (
                          <div className="mt-3 flex items-center gap-2 border-t border-slate-100 pt-3 text-xs text-slate-400">
                            <Square className="h-3 w-3" />{msg.statusHint || '已停止生成'}
                          </div>
                        )}
                        {msg.status === 'error' && msg.errorMessage && (
                          <div className="mt-3 flex items-center gap-2 border-t border-slate-100 pt-3 text-xs text-red-500">
                            <AlertTriangle className="h-3.5 w-3.5" />{msg.errorMessage}
                          </div>
                        )}
                        {/* 智能体画的图。放在正文之后、来源之前 —— 用户要的是「结论 + 图」,
                            而图在这里是结论的一部分,不该被塞到最底下当附件 */}
                        {msg.charts && msg.charts.length > 0 && (
                          <div className="mt-3 space-y-3 border-t border-slate-100 pt-3">
                            {msg.charts.map((c) => <ChartCard key={c.id} chart={c} />)}
                          </div>
                        )}
                        {msg.citations && msg.citations.length > 0 && (
                          <div className="mt-3 border-t border-slate-100 pt-3">
                            <div className="mb-1.5 flex items-center gap-1 text-[11px] text-slate-400">
                              <Database className="h-3 w-3" />回答来源
                            </div>
                            <div className="flex flex-wrap gap-1.5">
                              {msg.citations.map((c, i) => (
                                <a key={i} href={c.url || undefined} target="_blank" rel="noreferrer"
                                   className={`rounded-md border border-slate-200 bg-slate-50 px-2 py-0.5 text-[11px] text-slate-500 ${c.url ? 'hover:bg-slate-100 hover:text-blue-600' : ''}`}>
                                  {c.type === 'tool_call'
                                    ? <span className="inline-flex items-center gap-1"><Wrench className="h-3 w-3 text-slate-400" />{c.source || c.type || '工具调用'}</span>
                                    : <>[{c.ref}] {c.source || c.type || '来源'}</>}
                                </a>
                              ))}
                            </div>
                          </div>
                        )}
                        {msg.tokens && (
                          <div className="mt-4 flex items-center gap-4 border-t border-slate-100 pt-4 text-[11px] text-slate-400">
                            <div className="flex items-center gap-1"><Sparkles className="h-3 w-3" /><span>Tokens: {msg.tokens.prompt} / {msg.tokens.completion}</span></div>
                            <div className="flex items-center gap-1" title="由后端按模型配置单价(单价表 > 模型手填 > 兜底)计算">
                              <span>费用: ¥{msg.tokens.cost.toFixed(4)}</span>
                            </div>
                            {msg.tokens.modelCost != null && (
                              <div className="flex items-center gap-1">
                                <span>模型侧: ¥{msg.tokens.modelCost.toFixed(4)}</span>
                                {Math.abs(msg.tokens.modelCost - msg.tokens.cost) > 0.0001 && (
                                  <span className={msg.tokens.modelCost > msg.tokens.cost ? 'text-amber-500' : 'text-emerald-500'}>
                                    差异 ¥{(msg.tokens.modelCost - msg.tokens.cost).toFixed(4)}
                                  </span>
                                )}
                              </div>
                            )}
                            {msg.tokens.costReport?.some(r => r.missing > 0) && (
                              <div className="flex items-center gap-1 text-slate-400" title={msg.tokens.costReport
                                .filter(r => r.missing > 0)
                                .map(r => `${r.name || r.model}: ${r.missing}/${r.calls} 次调用未返回计费`)
                                .join('; ')}>
                                <AlertTriangle className="h-3 w-3" />
                                <span>模型接口未返回官方计费，已按系统配置单价计费</span>
                              </div>
                            )}
                          </div>
                        )}
                      </div>
                    </div>
                  </div>
                </div>
              )}
            </div>
          ))}
          {showJumpToLatest && messages.length > 0 && (
            <div className="pointer-events-none sticky bottom-2 flex justify-center">
              <button type="button" onClick={followLatest}
                className="pointer-events-auto flex items-center gap-1 rounded-full border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-600 shadow-md hover:bg-slate-50">
                <ArrowDown size={13} /> {isProcessing ? '回到最新(执行中)' : '回到最新'}
              </button>
            </div>
          )}
          <div ref={messagesEndRef} />
        </div>

        {/* ══════════ 底:执行目标 + 输入 ══════════ */}
        <div className="border-t border-slate-100 bg-slate-50/50 px-6 py-4">
          {/* 不再限宽到 4xl:Quick BI 那一排控件在 896px 下装不下,会挤出一条横向滚动条。
              跟着中间栏走,留点边距就够了。 */}
          <div className="mx-auto flex w-full max-w-[1280px] flex-col gap-3">
            {/* 当前执行目标:类型页签 + 具体目标,发消息前最后确认的一行 */}
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-[11px] font-medium text-slate-400">当前执行目标：</span>
              <div className="flex items-center gap-1 rounded-lg bg-white p-0.5 ring-1 ring-slate-200">
                {tabs.map((t) => (
                  <button key={t.type} onClick={() => setExecutorType(t.type)}
                    className={cn('flex items-center gap-1 rounded-md px-2 py-1 text-[11px] font-semibold transition-all',
                      executorType === t.type ? 'bg-blue-50 text-blue-600' : 'text-slate-500 hover:bg-slate-50')}>
                    <t.icon className="h-3 w-3" />{t.label}
                  </button>
                ))}
              </div>
              <div className="relative">
                <button onClick={() => setShowTargetPicker((v) => !v)}
                  className="flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 hover:border-blue-200">
                  {getTargetIcon(executorType)}
                  {activeTarget ? activeTarget.name : '选择执行目标'}
                  <ChevronDown className="h-3 w-3 opacity-50" />
                </button>
                {showTargetPicker && (
                  <>
                    <div className="fixed inset-0 z-20" onClick={() => setShowTargetPicker(false)} />
                    <div className="absolute bottom-10 left-0 z-30 w-80 rounded-xl border border-slate-200 bg-white p-2 shadow-xl">
                      <div className="relative mb-2">
                        <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-slate-400" />
                        <Input value={targetSearch} onChange={(e) => setTargetSearch(e.target.value)} placeholder="搜索..." className="h-8 pl-8 text-xs" />
                      </div>
                      <div className="max-h-64 space-y-1 overflow-y-auto">
                        {targets.filter((t) => !targetSearch || t.name.includes(targetSearch)).map((t) => {
                          const noVersion = (executorType === 'agent' || executorType === 'skill') && !t.published;
                          return (
                            <button key={t.id} disabled={noVersion}
                              onClick={() => {
                                if (noVersion) { toast.info('该目标暂无已发布版本，请先在管理端完成发布后再执行'); return; }
                                setTargetId(t.id); setShowTargetPicker(false);
                              }}
                              className={cn('w-full rounded-lg px-3 py-2 text-left text-sm transition-colors',
                                targetId === t.id ? 'bg-blue-50 text-blue-700' : 'hover:bg-slate-50',
                                noVersion && 'cursor-not-allowed opacity-50 hover:bg-transparent')}>
                              <div className="flex items-center gap-2">
                                <span className="truncate font-medium">{t.name}</span>
                                {t.badge && <Badge variant="outline" className="text-[9px]">{t.badge}</Badge>}
                              </div>
                              <div className="mt-0.5 truncate text-[11px] text-slate-400">
                                {noVersion ? '暂无已发布版本，需先发布' : t.desc}
                              </div>
                            </button>
                          );
                        })}
                        {targets.length === 0 && <div className="px-3 py-4 text-center text-xs text-slate-400">暂无可用目标</div>}
                      </div>
                    </div>
                  </>
                )}
              </div>

              {/* 15 号两层版本选择:自动解析 / 钉版本 */}
              {(executorType === 'agent' || executorType === 'skill') && activeTarget && activeTarget.published && (
                <div className="flex items-center gap-1">
                  <button title="自动解析:按当前用户在分发中心的指派命中版本"
                    onClick={() => setTargetVersionId('')}
                    className={cn('rounded-md px-2 py-1 text-[10px] font-semibold transition-colors',
                      !targetVersionId ? 'bg-blue-600 text-white' : 'bg-white text-slate-500 ring-1 ring-slate-200 hover:bg-slate-50')}>
                    自动解析
                  </button>
                  {targetVersions.map((v) => (
                    <button key={v.id} title={`显式执行 v${v.version}（钉版,绕过当前用户指派解析）`}
                      onClick={() => setTargetVersionId(targetVersionId === v.id ? '' : v.id)}
                      className={cn('rounded-md px-2 py-1 text-[10px] font-semibold transition-colors',
                        targetVersionId === v.id ? 'bg-blue-600 text-white' : 'bg-white text-slate-500 ring-1 ring-slate-200 hover:bg-slate-50')}>
                      v{v.version}
                    </button>
                  ))}
                </div>
              )}
            </div>

            {/* Quick BI 控制条:能力 / 景区 / 数据域 / 目标数据 / 固定问题 挤在一行。
                控件都收窄成一行放得下的宽度 —— 换行会把「固定问题」甩到第二行,
                看起来像是跟输入框无关的另一块东西。 */}
            {executorType === 'quickbi' && (
              <div className="custom-scrollbar flex flex-nowrap items-center gap-1.5 overflow-x-auto rounded-xl border border-slate-200 bg-white px-2.5 py-1.5 2xl:overflow-x-visible">
                <Sliders className="h-3 w-3 shrink-0 text-slate-400" />
                <div className="flex shrink-0 items-center gap-0.5">
                  {QBI_CAPS.map((c) => (
                    <button key={c.key} onClick={() => setQbiCap(c.key)}
                      title={qbiFixedId ? `能力：${c.label}（会覆盖固定问题自带的能力）` : `能力：${c.label}`}
                      className={cn('rounded-md px-2 py-1 text-[11px] font-semibold transition-colors',
                        qbiCap === c.key ? 'bg-blue-600 text-white' : 'bg-slate-100 text-slate-500 hover:bg-slate-200')}>
                      {c.label}
                    </button>
                  ))}
                </div>

                <span className="mx-0.5 h-4 w-px shrink-0 bg-slate-200" />

                <Mountain className="h-3 w-3 shrink-0 text-slate-400" />
                <select value={qbiScenic} onChange={(e) => setQbiScenic(e.target.value)} title="景区"
                  className="h-7 w-[104px] shrink-0 rounded-md border border-slate-200 bg-white px-1.5 text-[11px] text-slate-700">
                  <option value="">本账号景区</option>
                  {scenics.map((sc) => <option key={sc.id} value={sc.id}>{sc.name}</option>)}
                </select>

                <Layers className="h-3 w-3 shrink-0 text-slate-400" />
                <select value={qbiDomain} onChange={(e) => setQbiDomain(e.target.value)} title="数据域(只筛固定问题)"
                  className="h-7 w-[92px] shrink-0 rounded-md border border-slate-200 bg-white px-1.5 text-[11px] text-slate-700">
                  <option value="">全部域</option>
                  {qbiDomains.map((d) => <option key={d} value={d}>{d}</option>)}
                </select>

                <Database className="h-3 w-3 shrink-0 text-slate-400" />
                <select value={qbiTarget}
                  onChange={(e) => {
                    const v = e.target.value;
                    setQbiTarget(v);
                    applyScope(null);
                    // 目标数据换了,能力要跟着它对齐 —— 选了「集」却停在「报表」上,
                    // 发起就是一句「仪表板问数需要指定仪表板」,而人刚刚明明选了目标数据,
                    // 根本不会想到问题出在左边那排按钮上。
                    // 只在**对不上**时才改,用户自己挑过的组合不去覆盖。
                    const a = v.startsWith('asset:') ? qbiAssets.find((x) => x.id === v.slice(6)) : undefined;
                    if (a?.kind === 'dataset' && ['dashboard_qa', 'report'].includes(qbiCap)) setQbiCap('dataset_qa');
                    if (a?.kind === 'dashboard' && qbiCap === 'dataset_qa') setQbiCap('dashboard_qa');
                    // 文件交给自动路由:file_qa 不在这排按钮里,硬设过去会变成一个没有任何按钮高亮的状态
                    if (v.startsWith('file:') && ['dashboard_qa', 'dataset_qa'].includes(qbiCap)) setQbiCap('auto');
                  }}
                  title={qbiFixedId ? '目标数据(会覆盖固定问题自带的目标)' : '目标数据:已登记的仪表板/数据集,或上传的文件'}
                  className="h-7 w-[150px] shrink-0 rounded-md border border-slate-200 bg-white px-1.5 text-[11px] text-slate-700">
                  <option value="">按景区自动关联</option>
                  {/* 固定问题绑的目标不在下面的列表里(直接选的数据集 / 仪表板,或被筛掉的资产):
                      单独列一项,控件显示的就是这一问真正要跑的数据 */}
                  {boundTargetLabel && <option value={qbiTarget}>{boundTargetLabel}</option>}
                  {/* 按仪表板分组:数据集本来就是某块板下面的,混成一个大列表之后
                      「这个集属于哪块板」全靠名字猜 —— 两块板各十几个集时根本分不清 */}
                  {assetGroups.map((g) => (
                    <optgroup key={g.key} label={g.label}>
                      {g.items.map((a) => (
                        <option key={a.id} value={`asset:${a.id}`} disabled={a.active === false}>
                          {a.kind === 'dashboard' ? '板' : '　集'}｜{a.name}
                          {a.active === false ? '（已停用）' : ''}
                        </option>
                      ))}
                    </optgroup>
                  ))}
                  {qbiFiles.length > 0 && (
                    <optgroup label="上传文件">
                      {qbiFiles.map((f) => <option key={f.id} value={`file:${f.id}`}>文件｜{f.file_name}</option>)}
                    </optgroup>
                  )}
                </select>
                {/* 仪表板 + 报表 / 解读:可确认组件范围(默认全部)。报表问数按官方做法用这些组件的数据集多表问 */}
                {activeAsset?.kind === 'dashboard' && (qbiCap === 'insight' || qbiCap === 'dashboard_qa') && (
                  <ChartScopePicker compact assetId={activeAsset.id} value={qbiChartIds} onChange={setQbiChartIds} />
                )}
                <ListChecks className="h-3 w-3 shrink-0 text-slate-400" />
                <select value={qbiFixedId}
                  onChange={(e) => {
                    const id = e.target.value;
                    setQbiFixedId(id);
                    if (!id) return;
                    setInput('');
                    // 选中固定问题后,把它自带的能力/目标数据回填到上面的控件 ——
                    // 控件显示的就是这一问将要执行的配置,用户想改随时能改(改了就以控件为准)
                    const f = qbiFixed.find((x) => x.id === id);
                    if (f) {
                      setQbiCap(f.capability || 'auto');
                      // 数据集存的是 cube_id、仪表板可能只存了 pageId —— 都要认(见 fixedTarget.ts)
                      setQbiTarget(fixedTargetValue(f.target, qbiAssets));
                      applyScope(f.target);
                    }
                  }}
                  title="固定问题" className="h-7 w-[150px] shrink-0 rounded-md border border-slate-200 bg-white px-1.5 text-[11px] text-slate-700">
                  <option value="">不用固定问题</option>
                  {qbiFixed.map((f) => <option key={f.id} value={f.id}>{f.title}</option>)}
                </select>
              </div>
            )}

            <div className="relative rounded-2xl border border-slate-200 bg-white p-2 shadow-sm transition-all focus-within:border-blue-300 focus-within:ring-2 focus-within:ring-blue-100">
              {/* 选中的数据文件贴在输入框顶部:它是「这一问带的附件」,
                  放在输入框里才符合直觉,也一眼看得见带没带 */}
              {activeFile && (
                <div className="mx-2 mt-1 flex w-fit items-center gap-1.5 rounded-lg bg-slate-100 px-2 py-1 text-[11px] text-slate-600">
                  <Paperclip className="h-3 w-3 text-slate-400" />
                  <span className="max-w-[240px] truncate">{activeFile.file_name}</span>
                  <button type="button" onClick={() => setQbiTarget('')} title="不带这个文件"
                    className="text-slate-400 hover:text-rose-500">×</button>
                </div>
              )}
              <textarea
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSend(); } }}
                placeholder={mode === 'debug' ? '调试参数(JSON)，如 {"city":"厦门"}'
                  : activeFixed ? `将发送固定问题：${activeFixed.question}`
                  : executorType === 'quickbi' && qbiCap === 'insight' && activeAsset?.kind === 'dashboard'
                    ? '输入解读思路或要求;不填直接发送 = 一键解读(按图表范围做整体解读)'
                  : '输入消息，Shift+Enter换行，Enter发送...'}
                className="custom-scrollbar max-h-[300px] min-h-[92px] w-full resize-none border-none bg-transparent p-3 text-sm text-slate-700 outline-none ring-0 focus:border-none focus:outline-none focus:ring-0 focus-visible:outline-none focus-visible:ring-0"
              />
              <div className="flex items-center justify-between px-2 pb-1">
                <div className="flex items-center gap-2">
                  {executorType === 'quickbi' && (
                    <>
                      {/* 上传入口放在输入框里:传文件本来就是「为这一问准备数据」,
                          跟打字是同一件事,不该跑到上面那排配置里去找 */}
                      <input ref={qbiFileInput} type="file" accept=".xls,.xlsx,.csv" className="hidden"
                        onChange={(e) => { const f = e.target.files?.[0]; if (f) uploadQbiFile(f); }} />
                      <button type="button" onClick={() => qbiFileInput.current?.click()}
                        disabled={qbiUploading}
                        title="上传数据文件(xls / xlsx / csv,≤5MB),问数 / 解读 / 报告都能打在它上面"
                        className="flex h-7 w-7 items-center justify-center rounded-lg text-slate-400 transition-colors hover:bg-slate-100 hover:text-blue-600 disabled:text-slate-200">
                        {qbiUploading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Paperclip className="h-4 w-4" />}
                      </button>
                    </>
                  )}
                  <span className="text-[10px] font-medium text-slate-400">Shift+Enter 换行</span>
                </div>
                <div className="flex items-center gap-2">
                  {isProcessing && executorType === 'quickbi' && (
                    <Button variant="outline" size="sm" onClick={() => qbiAbort.current?.abort()}
                      className="h-8 gap-1 border-rose-200 text-[11px] text-rose-600 hover:bg-rose-50">
                      <Square className="h-3 w-3" />停止
                    </Button>
                  )}
                  <Button onClick={() => handleSend()} disabled={isProcessing}
                    className="h-9 rounded-xl bg-[#1677ff] px-5 font-medium text-white transition-all hover:bg-[#4096ff] disabled:opacity-50">
                    <Send className={cn('mr-2 h-4 w-4', isProcessing && 'animate-pulse')} />发送
                  </Button>
                </div>
              </div>
            </div>

            {mode === 'chat' && (
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-[11px] font-medium text-slate-400">快速提问：</span>
                {(executorType === 'quickbi' ? qbiPresets : presets).map((preset, i) => (
                  <Button key={i} variant="outline" size="sm" onClick={() => handleSend(preset)}
                    className="h-auto rounded-full border-slate-200 bg-white px-3 py-1 text-xs font-medium text-slate-600 transition-all hover:border-blue-200 hover:bg-blue-50/50 hover:text-blue-600">
                    {preset}
                  </Button>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>

      {/* ══════════ 右:执行详情 ══════════ */}
      <div className="custom-scrollbar hidden w-[300px] shrink-0 overflow-y-auto border-l border-slate-200 bg-[#f8fafc] p-4 xl:block">
        {executorType === 'quickbi' && lastQbi?.runId && (
          /* 排障时最难的是把「刚才发生了什么」说清楚。这个按钮把上游原样事件、
             归一化事件与结果分区打成一份 Markdown,直接发给研发即可,凭证不在里面。 */
          <Button variant="outline" onClick={() => exportRunDetail(lastQbi.runId!)}
            className="mb-3 w-full gap-1.5 border-slate-200 text-[12px] text-slate-600">
            <FileDown className="h-3.5 w-3.5" />导出执行详情
          </Button>
        )}
        {executorType !== 'quickbi' && lastAssistant?.taskId && (
          /* 智能体侧的同一件事。此前只有 Quick BI 有导出,自己这条链出问题时
             只能截图 —— 而截图里没有提示词、没有工具入参、没有结果摘要,
             正是判断「哪一棒掉了」需要的三样。 */
          <Button variant="outline" onClick={() => exportTaskDetail(lastAssistant.taskId!)}
            className="mb-3 w-full gap-1.5 border-slate-200 text-[12px] text-slate-600">
            <FileDown className="h-3.5 w-3.5" />导出执行详情
          </Button>
        )}
        {/* Quick BI 走它自己的额度,不占平台 token 预算,显式传 0 关掉进度条;
            智能体侧不传 —— 由后端随本轮下发真实生效的预算(不限制时同样不画条)。 */}
        <RunDetailPanel
          info={runInfo}
          tokenBudget={executorType === 'quickbi' ? 0 : undefined}
          budgetNote={executorType === 'quickbi' ? 'Quick BI 用量计在 Quick BI 侧,不占平台 token 预算' : undefined}
          onAbort={async () => {
            if (executorType === 'quickbi') { qbiAbort.current?.abort(); return; }
            if (!lastAssistant?.taskId) return;
            try {
              await api.cancelTask(lastAssistant.taskId);
              toast.success('已请求中止,任务将在当前步骤结束后停止');
            } catch (e: any) {
              toast.error(e.message || '中止失败');
            }
          }}
        />
      </div>

      {showOverrideModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40" onClick={() => setShowOverrideModal(false)}>
          <div className="w-full max-w-md rounded-2xl bg-white p-5" onClick={(e) => e.stopPropagation()}>
            <h3 className="mb-4 text-sm font-bold text-slate-800">参数覆盖（作用于下一轮）</h3>
            <div className="space-y-3">
              <div>
                <label className="form-label">模型覆盖</label>
                <Input value={overrideModel} onChange={(e) => setOverrideModel(e.target.value)} placeholder="留空使用默认模型" />
              </div>
              <div>
                <label className="form-label">知识源覆盖（替换，逗号分隔）</label>
                <Input value={overrideKs} onChange={(e) => setOverrideKs(e.target.value)} placeholder="如 vector" />
              </div>
            </div>
            <div className="mt-5 flex justify-end gap-2">
              <Button variant="outline" onClick={() => setShowOverrideModal(false)}>取消</Button>
              <Button className="bg-[#1677ff]" onClick={() => setShowOverrideModal(false)}>应用</Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

export default Console;
