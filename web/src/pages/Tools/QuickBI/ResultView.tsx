import React, { useMemo, useState } from 'react';
import EChartsView from '../../../components/charts/EChartsView';
import {
  ResponsiveContainer, BarChart, Bar, LineChart, Line, PieChart, Pie, Cell, XAxis, YAxis, Tooltip, CartesianGrid, Legend,
  RadarChart, Radar, PolarGrid, PolarAngleAxis, PolarRadiusAxis,
  AreaChart, Area, ScatterChart, Scatter, ZAxis, FunnelChart, Funnel, LabelList,
  RadialBarChart, RadialBar, Treemap,
} from 'recharts';
import {
  AlertTriangle, BarChart3, Brain, CheckCircle2, ChevronDown, ChevronRight, Code2, Database, ExternalLink, FileText,
  Loader2, Table2, Timer, Route,
} from 'lucide-react';
import Markdown from '../../../components/common/Markdown';
import { Badge } from '../../../components/ui/badge';
import { CopyId } from '../../../components/ui/PageHelpers';
import { cn } from '../../../lib/utils';
import type { QbiChart, QbiEvent, QbiRun } from '../../../lib/api';

/** 执行过程与结果的前端状态:流式事件逐个 reduce 进来,历史运行记录也转换成同一形状。 */
export interface QbiLive {
  runId?: string;
  capability?: string;
  label?: string;
  routed?: boolean;
  connectionName?: string;
  source?: string;
  trial?: boolean;
  question?: string;
  statuses: { stage: string; message: string }[];
  reasoning: string;
  sections: Record<string, string>;
  sql: string;
  code: string;
  charts: QbiChart[];
  steps: { title: string; desc?: string; level?: number; links?: any[] }[];
  errors: { message: string; code?: string }[];
  meta: Record<string, any>;
  report?: { chat_id?: string; report_url?: string | null; finished?: boolean; success?: boolean; error?: string | null };
  usage?: any;
  traceId?: string;
  done?: { status: string; duration_ms?: number; error?: string | null; report_url?: string | null };
  streaming: boolean;
}

export const emptyLive = (question?: string): QbiLive => ({
  question, statuses: [], reasoning: '', sections: {}, sql: '', code: '', charts: [], steps: [], errors: [],
  meta: {}, streaming: true,
});

// 结果上的徽标只用四个字面:问数 / 报表 / 解读 / 报告。
// 数据来源(数据集 / 上传文件 / 仪表板)另有一栏显示,不必塞进名字里重复一遍。
export const CAP_LABEL: Record<string, string> = {
  dataset_qa: '问数', file_qa: '问数', dashboard_qa: '报表',
  insight: '解读', report: '报告', auto: '自动路由',
};
export const CAP_COLOR: Record<string, string> = {
  dataset_qa: 'bg-blue-50 text-blue-600 border-blue-100', file_qa: 'bg-cyan-50 text-cyan-600 border-cyan-100',
  dashboard_qa: 'bg-orange-50 text-orange-600 border-orange-100', insight: 'bg-violet-50 text-violet-600 border-violet-100',
  report: 'bg-emerald-50 text-emerald-600 border-emerald-100',
};

export function reduceQbi(s: QbiLive, e: QbiEvent): QbiLive {
  const n: QbiLive = { ...s };
  switch (e.type) {
    case 'run':
      Object.assign(n, { runId: e.run_id, capability: e.capability, label: e.label, routed: e.routed,
        connectionName: e.connection_name, source: e.capability_source, trial: e.trial });
      if (e.chat_id) n.report = { ...(n.report || {}), chat_id: e.chat_id };
      break;
    case 'status': n.statuses = [...s.statuses, { stage: e.stage, message: e.message }]; break;
    case 'trace': n.traceId = e.trace_id; break;
    case 'reasoning': n.reasoning = s.reasoning + (e.delta || ''); break;
    case 'text': {
      const k = e.section || 'answer';
      n.sections = { ...s.sections, [k]: (s.sections[k] || '') + (e.delta || '') };
      break;
    }
    case 'sql': n.sql = s.sql + (e.delta || ''); break;
    case 'code': n.code = s.code + (e.delta || ''); break;
    case 'chart': if (e.chart) n.charts = [...s.charts, e.chart]; break;
    case 'step': n.steps = [...s.steps, { title: e.title, desc: e.desc, level: e.level, links: e.links }]; break;
    case 'error': n.errors = [...s.errors, { message: e.message, code: e.code }]; break;
    case 'meta': n.meta = { ...s.meta, ...e }; break;
    case 'usage': n.usage = e; break;
    case 'report': n.report = { ...(s.report || {}), ...e }; break;
    case 'done':
      n.done = { status: e.status, duration_ms: e.duration_ms, error: e.error, report_url: e.report_url };
      n.streaming = false;
      if (e.run_id) n.runId = e.run_id;
      if (e.report_url) n.report = { ...(n.report || {}), report_url: e.report_url, finished: true, success: true };
      if (e.status === 'running' && e.chat_id) n.report = { ...(n.report || {}), chat_id: e.chat_id, finished: false };
      break;
    default: break;
  }
  return n;
}

export function liveFromRun(r: QbiRun): QbiLive {
  const res = r.result || {};
  return {
    runId: r.id, capability: r.capability, label: r.capability_label, source: r.capability_source,
    question: r.question,
    statuses: [], reasoning: res.reasoning || '', sections: res.sections || (r.answer ? { answer: r.answer } : {}),
    sql: res.sql || '', code: res.code || '', charts: res.charts || [], steps: res.steps || [],
    errors: r.error ? [{ message: r.error, code: r.error_code || undefined }] : (res.errors || []),
    meta: res.meta || {}, usage: res.usage, traceId: r.trace_id || undefined,
    report: r.capability === 'report' ? { chat_id: r.chat_id || undefined, report_url: r.report_url,
      finished: r.status !== 'running', success: r.status === 'success' } : undefined,
    done: { status: r.status, duration_ms: r.duration_ms || undefined, error: r.error, report_url: r.report_url },
    streaming: false,
  };
}

const SECTION_ORDER: [string, string][] = [
  ['answer', ''], ['report', '分析报告'], ['conclusion', '结论'], ['summary', '摘要'], ['check', '提示'], ['related', '相关信息'],
];
// 分类配色:顺序固定、**不循环生成新色**。过了色觉分离与明度带校验
// (旧的 #eab308 太亮、#64748b 没有彩度,两项都不合格,已替换)。
// 与服务端 chart_templates.PALETTE 同一组值 —— 同一张图在 APP 和后台该是同一个颜色。
const PALETTE = ['#2563eb', '#ea580c', '#059669', '#7c3aed', '#dc2626', '#0891b2', '#ca8a04', '#db2777'];

/** 管理端(recharts)画不出来的类型:显示数据表 + 一句说明,而不是一块空白画布。
 *  这份清单和服务端 chart_templates.ECHARTS_ONLY_TYPES 对应;两边都以「能不能画」
 *  为准,而不是谁默认谁会画 —— 都以为对方会画的结果就是谁都没画。 */
const ECHARTS_ONLY = new Set(['combo', 'heatmap', 'calendar', 'boxplot', 'sunburst', 'themeriver']);

/** 堆叠类:这几种要把各系列摞起来,而不是并排。 */
const STACKED = new Set(['stacked_bar', 'stacked_area']);

const Fold: React.FC<{ title: React.ReactNode; icon: any; defaultOpen?: boolean; children: React.ReactNode }> = (
  { title, icon: Icon, defaultOpen, children }) => {
  const [open, setOpen] = useState(!!defaultOpen);
  return (
    <div className="rounded-xl border border-slate-100 bg-slate-50/60">
      <button type="button" onClick={() => setOpen(!open)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs font-semibold text-slate-500 hover:text-slate-700">
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}<Icon size={13} />{title}
      </button>
      {open && <div className="border-t border-slate-100 px-3 py-2">{children}</div>}
    </div>
  );
};

function fmt(v: any): string {
  if (typeof v === 'number') return Number.isInteger(v) ? v.toLocaleString() : v.toLocaleString(undefined, { maximumFractionDigits: 4 });
  return v == null ? '' : String(v);
}

export const ChartCard: React.FC<{ chart: QbiChart }> = ({ chart }) => {
  const dims = chart.fields.filter((f) => f.role !== 'metric');
  const metrics = chart.fields.filter((f) => f.role === 'metric');
  // 有 ECharts option(智能体的 sys_chart / 结构化图表都带)就直接用 ECharts 画,
  // 和 APP 同一份 option、同一个渲染库 —— 双轴、极值标注、热力/日历/箱线这些都能画。
  // ECharts 加载或渲染失败时退回下面的 recharts / 数据表(echartsFailed)。
  const [echartsFailed, setEchartsFailed] = useState(false);
  const useECharts = !!chart.option && !echartsFailed;
  // 走 recharts 时,ECharts 独有的那几种画不了 —— 直接显示数据表,并在卡片上说明原因。
  // 装作能画(给一块空白画布)比说不能画糟得多:看的人会以为数据是空的。
  const echartsOnly = !useECharts && ECHARTS_ONLY.has(chart.chart_type);
  const plottable = useECharts || (metrics.length > 0 && dims.length > 0 && chart.rows.length > 0 &&
    !echartsOnly && !['table', 'indicator'].includes(chart.chart_type));
  const [mode, setMode] = useState<'chart' | 'table'>(plottable ? 'chart' : 'table');
  const x = dims[0]?.name;
  const rows = useMemo(() => chart.rows.slice(0, 200), [chart.rows]);

  const renderChart = () => {
    const t = chart.chart_type;

    if ((t === 'pie' || t === 'donut') && metrics[0]) {
      return (
        <PieChart>
          <Pie data={rows} dataKey={metrics[0].name} nameKey={x} label
               outerRadius={100} innerRadius={t === 'donut' ? 52 : 0} paddingAngle={1}>
            {rows.map((_, i) => <Cell key={i} fill={PALETTE[i % PALETTE.length]} />)}
          </Pie>
          <Tooltip /><Legend />
        </PieChart>
      );
    }

    // 漏斗:逐级转化。只画第一个数值列 —— 漏斗讲的是一条路径掉了多少人。
    if (t === 'funnel' && metrics[0]) {
      const data = rows.map((r, i) => ({ ...r, fill: PALETTE[i % PALETTE.length] }));
      return (
        <FunnelChart>
          <Tooltip />
          <Funnel dataKey={metrics[0].name} data={data} isAnimationActive={false}>
            <LabelList position="right" dataKey={x} className="fill-slate-600 text-[11px]" />
          </Funnel>
        </FunnelChart>
      );
    }

    // 仪表盘:recharts 没有 gauge,用 RadialBar 画同一件事(一个值对着它的上限)。
    // 超过 100% 时刻度跟着放大,不截断成满格 —— 截断会把「超额完成」画成「刚好完成」。
    if (t === 'gauge' && metrics[0] && rows[0]) {
      const v = Number(rows[0][metrics[0].name]) || 0;
      const max = Math.max(100, v);
      return (
        <RadialBarChart data={[{ name: String(rows[0][x as string] ?? ''), value: v, fill: PALETTE[0] }]}
                        innerRadius="62%" outerRadius="94%" startAngle={210} endAngle={-30}>
          <PolarAngleAxis type="number" domain={[0, max]} tick={false} />
          <RadialBar dataKey="value" background={{ fill: '#e2e8f0' }} cornerRadius={8} isAnimationActive={false} />
          <Tooltip />
        </RadialBarChart>
      );
    }

    // 矩形树图:类别多到饼图切不开时看占比。
    if (t === 'treemap' && metrics[0]) {
      const data = rows.map((r, i) => ({
        name: String(r[x as string] ?? ''), size: Number(r[metrics[0].name]) || 0,
        fill: PALETTE[i % PALETTE.length],
      }));
      return <Treemap data={data} dataKey="size" nameKey="name" isAnimationActive={false}
                      stroke="#fff" content={undefined as any} />;
    }

    // 散点:横轴也是数值。第一个维度列在服务端已经是数值了(builtin_chart 会校验)。
    if (t === 'scatter' && metrics[0]) {
      const data = rows.map((r) => ({ x: Number(r[x as string]), y: Number(r[metrics[0].name]) }))
        .filter((d) => Number.isFinite(d.x) && Number.isFinite(d.y));
      return (
        <ScatterChart>
          <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
          <XAxis type="number" dataKey="x" name={x} tick={{ fontSize: 11 }} />
          <YAxis type="number" dataKey="y" name={metrics[0].name} tick={{ fontSize: 11 }} />
          <ZAxis range={[60, 60]} />
          <Tooltip cursor={{ strokeDasharray: '3 3' }} />
          <Scatter data={data} fill={PALETTE[0]} isAnimationActive={false} />
        </ScatterChart>
      );
    }

    // 桑基:recharts 的 Sankey 要 nodes+links,而这里拿到的是「从 → 到 · 数值」的行。
    // 与其把行反解成图结构(解错了画出来的流向是错的,而图看着完全正常),
    // 不如横着画成排名条 —— 量的大小照样读得出来,流向由标签文字说明。
    if (t === 'sankey' && metrics[0]) {
      return (
        <BarChart data={rows} layout="vertical">
          <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
          <XAxis type="number" tick={{ fontSize: 11 }} />
          <YAxis type="category" dataKey={x} width={150} tick={{ fontSize: 11 }} />
          <Tooltip /><Legend />
          <Bar dataKey={metrics[0].name} fill={PALETTE[0]} radius={[0, 3, 3, 0]} />
        </BarChart>
      );
    }

    if (t === 'area' || t === 'stacked_area') {
      return (
        <AreaChart data={rows}>
          <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
          <XAxis dataKey={x} tick={{ fontSize: 11 }} /><YAxis tick={{ fontSize: 11 }} /><Tooltip /><Legend />
          {metrics.map((m, i) => (
            <Area key={m.name} type="monotone" dataKey={m.name}
                  stackId={STACKED.has(t) ? 'a' : undefined}
                  stroke={PALETTE[i % PALETTE.length]} fill={PALETTE[i % PALETTE.length]}
                  fillOpacity={STACKED.has(t) ? 0.75 : 0.18} strokeWidth={2} />
          ))}
        </AreaChart>
      );
    }
    if (chart.chart_type === 'radar') {
      // 雷达图各轴量程差得极大(票房两万 vs 口碑 3.6),同一把尺子画出来口碑会贴在圆心 ——
      // 图出来了、颜色也对,看图的人只会以为口碑是 0。所以按每根轴自己的最大值归一到
      // 0~100 再画,原值放在 tooltip 里(要的是形状,不是刻度)。
      const norm = rows.map((r) => {
        const top = Math.max(...metrics.map((m) => Number(r[m.name]) || 0), 0) || 1;
        const o: any = { [x as string]: r[x as string] };
        metrics.forEach((m) => {
          o[m.name] = Math.round(((Number(r[m.name]) || 0) / top) * 1000) / 10;
          o[`__raw_${m.name}`] = r[m.name];
        });
        return o;
      });
      return (
        <RadarChart data={norm} outerRadius="72%">
          <PolarGrid stroke="#e2e8f0" />
          <PolarAngleAxis dataKey={x} tick={{ fontSize: 11 }} />
          <PolarRadiusAxis tick={false} axisLine={false} domain={[0, 100]} />
          {metrics.map((m, i) => (
            <Radar key={m.name} name={m.name} dataKey={m.name}
              stroke={PALETTE[i % PALETTE.length]} fill={PALETTE[i % PALETTE.length]} fillOpacity={0.25} />
          ))}
          <Tooltip formatter={(v: any, n: any, p: any) => [fmt(p?.payload?.[`__raw_${n}`] ?? v), n]} />
          <Legend />
        </RadarChart>
      );
    }
    if (t === 'line') {
      return (
        <LineChart data={rows}>
          <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
          <XAxis dataKey={x} tick={{ fontSize: 11 }} /><YAxis tick={{ fontSize: 11 }} /><Tooltip /><Legend />
          {metrics.map((m, i) => <Line key={m.name} type="monotone" dataKey={m.name} stroke={PALETTE[i % PALETTE.length]} dot={false} strokeWidth={2} />)}
        </LineChart>
      );
    }
    const horizontal = ['ranking', 'horizontal_bar'].includes(t);
    const stacked = STACKED.has(t);
    return (
      <BarChart data={rows} layout={horizontal ? 'vertical' : 'horizontal'}>
        <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
        {horizontal
          ? (<><XAxis type="number" tick={{ fontSize: 11 }} /><YAxis type="category" dataKey={x} width={130} tick={{ fontSize: 11 }} /></>)
          : (<><XAxis dataKey={x} tick={{ fontSize: 11 }} /><YAxis tick={{ fontSize: 11 }} /></>)}
        <Tooltip /><Legend />
        {metrics.map((m, i) => (
          <Bar key={m.name} dataKey={m.name} stackId={stacked ? 'a' : undefined}
               fill={PALETTE[i % PALETTE.length]}
               radius={stacked ? 0 : (horizontal ? [0, 3, 3, 0] : [3, 3, 0, 0])} />
        ))}
      </BarChart>
    );
  };

  return (
    <div className="rounded-xl border border-slate-200 bg-white p-3">
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="min-w-0">
          <div className="truncate text-sm font-bold text-slate-800">{chart.title}</div>
          <div className="text-[11px] text-slate-400">{chart.chart_type} · {chart.row_count} 行{chart.truncated ? '(已截断)' : ''}</div>
          {/* 说清楚「这里为什么是表不是图」。不说的话,看的人会以为这张图坏了或者数据是空的 ——
              而它在 APP 上是正常渲染的,两边对不上却没有任何解释,最难排查。 */}
          {echartsOnly && (
            <div className="mt-0.5 text-[11px] text-amber-600">
              这类图后台暂不支持渲染（APP 上是图形），下面是它的完整数据
            </div>
          )}
        </div>
        <div className="flex shrink-0 gap-1">
          {plottable && (
            <button type="button" onClick={() => setMode('chart')}
              className={cn('rounded-md p-1.5', mode === 'chart' ? 'bg-blue-50 text-blue-600' : 'text-slate-400 hover:bg-slate-50')}>
              <BarChart3 size={14} />
            </button>
          )}
          <button type="button" onClick={() => setMode('table')}
            className={cn('rounded-md p-1.5', mode === 'table' ? 'bg-blue-50 text-blue-600' : 'text-slate-400 hover:bg-slate-50')}>
            <Table2 size={14} />
          </button>
        </div>
      </div>
      {chart.chart_type === 'indicator' && metrics.length > 0 && rows.length === 1 && mode === 'table' ? (
        <div className="flex flex-wrap gap-4 py-2">
          {metrics.map((m) => (
            <div key={m.name}>
              <div className="text-[11px] text-slate-400">{m.name}</div>
              <div className="text-2xl font-bold text-slate-900">{fmt(rows[0][m.name])}</div>
            </div>
          ))}
        </div>
      ) : mode === 'chart' && plottable && useECharts ? (
        <EChartsView option={chart.option} height={300} onFail={() => setEchartsFailed(true)} />
      ) : mode === 'chart' && plottable ? (
        <div className="h-64 w-full"><ResponsiveContainer width="100%" height="100%">{renderChart()}</ResponsiveContainer></div>
      ) : (
        <div className="max-h-72 overflow-auto rounded-lg border border-slate-100">
          <table className="w-full text-xs">
            <thead className="sticky top-0 bg-slate-50">
              <tr>{chart.fields.map((f) => <th key={f.name} className="whitespace-nowrap px-2 py-1.5 text-left font-semibold text-slate-600">{f.name}</th>)}</tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={i} className="border-t border-slate-100">
                  {chart.fields.map((f) => (
                    <td key={f.name} className={cn('whitespace-nowrap px-2 py-1', f.role === 'metric' && 'text-right font-mono')}>{fmt(r[f.name])}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {chart.conclusion && <p className="mt-2 text-xs text-slate-500">{chart.conclusion}</p>}
    </div>
  );
};

/**
 * `part` 决定渲染哪一半:
 *   data     —— 正式数据:结论正文、图表/表格、报告入口。用户真正要读的东西。
 *   thinking —— 思考过程:阶段提示、推理、SQL、分析代码、执行步骤。排查时才看。
 *   all      —— 两者都渲染(Quick BI 自己的页面沿用这个)。
 * 分开的理由很实际:推理文字又长又快,混在一起会把结论冲得找不着。
 */
const ResultView: React.FC<{ live: QbiLive; compact?: boolean; part?: 'all' | 'data' | 'thinking' }> = (
  { live, compact, part = 'all' }) => {
  const status = live.streaming ? 'running' : live.done?.status;
  const answerParts = SECTION_ORDER
    .filter(([k]) => (live.sections[k] || '').trim())
    // check/related 是过程性提示,不是结论,跟着思考过程走
    .filter(([k]) => (part === 'all' ? true
      : part === 'data' ? !['check', 'related'].includes(k)
      : ['check', 'related'].includes(k)));
  const showData = part !== 'thinking';
  const showThinking = part !== 'data';
  const lastStatus = live.statuses[live.statuses.length - 1];
  return (
    <div className="space-y-3">
      <div className={cn('flex flex-wrap items-center gap-2 text-xs', part === 'data' && 'hidden')}>
        {live.capability && (
          <Badge className={cn('border', CAP_COLOR[live.capability] || 'bg-slate-50 text-slate-600')}>
            {CAP_LABEL[live.capability] || live.label || live.capability}
          </Badge>
        )}
        {live.source && <span className="text-slate-400">{live.source}</span>}
        {live.routed && <span className="inline-flex items-center gap-1 text-slate-400"><Route size={12} />按意图自动路由</span>}
        {status === 'running' && <span className="inline-flex items-center gap-1 text-blue-600"><Loader2 size={12} className="animate-spin" />
          {live.capability === 'report' && !live.streaming ? '报告生成中' : (lastStatus?.message || '执行中…')}</span>}
        {status === 'success' && <span className="inline-flex items-center gap-1 text-emerald-600"><CheckCircle2 size={12} />成功</span>}
        {status === 'failed' && <span className="inline-flex items-center gap-1 text-rose-600"><AlertTriangle size={12} />失败</span>}
        {live.done?.duration_ms != null && <span className="inline-flex items-center gap-1 text-slate-400"><Timer size={12} />{(live.done.duration_ms / 1000).toFixed(1)}s</span>}
        {live.meta?.cube_name && <span className="inline-flex items-center gap-1 text-slate-500"><Database size={12} />{live.meta.cube_name}
          {live.meta.cube_how && <span className="text-slate-400">({({ table_search: '智能选表', relevance: '名称相关性', name_exact: '名称匹配', dashboard_single: '仪表板数据集' } as any)[live.meta.cube_how] || live.meta.cube_how})</span>}</span>}
        {live.trial && <Badge variant="warning">试用凭证</Badge>}
        {live.runId && !compact && <CopyId id={live.runId} />}
        {live.traceId && !compact && <span className="font-mono text-[10px] text-slate-300">trace {live.traceId}</span>}
      </div>

      {showThinking && live.statuses.length > 0 && live.streaming && (
        <div className="space-y-0.5 text-[11px] text-slate-400">
          {live.statuses.slice(-4).map((s, i) => <div key={i}>· {s.message}</div>)}
        </div>
      )}

      {/* 默认收起:思考过程又长又快,展开着会把结论冲得看不见。要看的人点一下就有 */}
      {showThinking && live.reasoning && (
        <Fold title="思考过程" icon={Brain}>
          <div className="whitespace-pre-wrap text-xs leading-relaxed text-slate-500">{live.reasoning}</div>
        </Fold>
      )}

      {answerParts.map(([k, label]) => (
        <div key={k} className={cn(k === 'check' && 'rounded-xl border border-amber-100 bg-amber-50 px-3 py-2')}>
          {label && <div className="mb-1 text-[11px] font-bold uppercase text-slate-400">{label}</div>}
          <Markdown content={live.sections[k]} />
        </div>
      ))}

      {showData && live.charts.length > 0 && (
        <div className={cn('grid gap-3', live.charts.length > 1 && !compact ? 'lg:grid-cols-2' : 'grid-cols-1')}>
          {live.charts.map((c, i) => <ChartCard key={c.id + i} chart={c} />)}
        </div>
      )}

      {showData && live.report && (live.report.chat_id || live.report.report_url) && (
        <div className="flex flex-wrap items-center gap-3 rounded-xl border border-emerald-100 bg-emerald-50/60 px-3 py-2 text-xs">
          <FileText size={14} className="text-emerald-600" />
          {live.report.report_url ? (
            <a href={live.report.report_url} target="_blank" rel="noreferrer"
              className="inline-flex items-center gap-1 font-semibold text-emerald-700 underline">
              打开报告<ExternalLink size={12} />
            </a>
          ) : <span className="text-emerald-700">报告生成中(通常需要 3~10 分钟)</span>}
          {live.report.chat_id && <span className="font-mono text-[10px] text-emerald-500">chatId {live.report.chat_id}</span>}
        </div>
      )}

      {showThinking && live.sql && (
        <Fold title="生成的 SQL" icon={Code2}><pre className="whitespace-pre-wrap font-mono text-[11px] text-slate-600">{live.sql}</pre></Fold>
      )}
      {showThinking && live.code && (
        <Fold title="分析代码(Python)" icon={Code2}><pre className="whitespace-pre-wrap font-mono text-[11px] text-slate-600">{live.code}</pre></Fold>
      )}
      {showThinking && live.steps.length > 0 && (
        <Fold title={`执行步骤(${live.steps.length})`} icon={Route}
              defaultOpen={live.streaming && live.capability === 'report'}>
          <div className="max-h-72 space-y-1.5 overflow-auto">
            {live.steps.map((s, i) => (
              <div key={i} className={cn('text-xs', (s.level || 1) > 1 && 'pl-4')}>
                <div className="font-semibold text-slate-600">{s.title}</div>
                {s.desc && <div className="whitespace-pre-wrap text-slate-500">{s.desc}</div>}
                {(s.links || []).map((l: any, j: number) => (
                  <a key={j} href={l.link} target="_blank" rel="noreferrer" className="block truncate text-blue-500 underline">{l.title}</a>
                ))}
              </div>
            ))}
          </div>
        </Fold>
      )}
      {live.errors.length > 0 && part !== 'thinking' && (
        <div className="space-y-1 rounded-xl border border-rose-100 bg-rose-50 px-3 py-2 text-xs text-rose-600">
          {live.errors.map((e, i) => <div key={i}><AlertTriangle size={12} className="mr-1 inline" />{e.message}</div>)}
        </div>
      )}
      {showThinking && live.usage && live.usage.total_tokens != null && (
        <div className="text-[11px] text-slate-400">Token:输入 {live.usage.prompt_tokens ?? '-'} · 输出 {live.usage.completion_tokens ?? '-'} · 合计 {live.usage.total_tokens}</div>
      )}
    </div>
  );
};

export default ResultView;
