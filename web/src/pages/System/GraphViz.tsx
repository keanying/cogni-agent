import { useMemo, useState } from 'react';
import {
  AlertTriangle, CheckCircle2, Loader2, Search, Workflow,
} from 'lucide-react';
import { getGraphTrace, type GraphVizData } from '../../lib/api';

const STEP_META: Record<string, { label: string; cls: string }> = {
  mode_decision: { label: '模式决策', cls: 'bg-violet-100 text-violet-700' },
  intent_check: { label: '意图判定', cls: 'bg-sky-100 text-sky-700' },
  knowledge_retrieval: { label: '知识检索', cls: 'bg-teal-100 text-teal-700' },
  resolve_diagnostic: { label: '解析诊断', cls: 'bg-slate-100 text-slate-600' },
  generation: { label: '生成回答', cls: 'bg-blue-100 text-blue-700' },
  tool_call: { label: '工具调用', cls: 'bg-amber-100 text-amber-700' },
  tool_call_skipped: { label: '跳过重复', cls: 'bg-orange-100 text-orange-700' },
  tool_start: { label: '工具开始', cls: 'bg-amber-100 text-amber-700' },
  tool_executing: { label: '工具执行中', cls: 'bg-amber-100 text-amber-700' },
  tool_disabled: { label: '工具停用', cls: 'bg-red-100 text-red-700' },
  advance_skill: { label: '技能升级', cls: 'bg-fuchsia-100 text-fuchsia-700' },
  skill: { label: '技能执行', cls: 'bg-fuchsia-100 text-fuchsia-700' },
};

const STATUS_META: Record<string, { label: string; cls: string }> = {
  PENDING: { label: '排队中', cls: 'bg-slate-100 text-slate-600' },
  RUNNING: { label: '执行中', cls: 'bg-blue-100 text-blue-700' },
  COMPLETED: { label: '已完成', cls: 'bg-emerald-100 text-emerald-700' },
  FAILED: { label: '失败', cls: 'bg-red-100 text-red-700' },
  WAITING_HUMAN: { label: '待人工', cls: 'bg-amber-100 text-amber-700' },
  CANCELLED: { label: '已取消', cls: 'bg-slate-100 text-slate-600' },
};

const NODE_W = 150;
const NODE_H = 56;
const GAP = 64;

/** 固定模板的节点排列优先级(未知节点排最后) */
function nodeOrder(nodes: GraphVizData['graph']['nodes']) {
  const priority = ['START', 'intent_check', 'knowledge', 'agent', 'tools', 'advance_skill', 'layer', 'synthesize', 'run', 'END'];
  return [...nodes].sort((a, b) => {
    const pa = priority.indexOf(a.id);
    const pb = priority.indexOf(b.id);
    return (pa === -1 ? 99 : pa) - (pb === -1 ? 99 : pb);
  });
}

function GraphSvg({ data }: { data: GraphVizData }) {
  const { nodes, edges, executed } = data.graph;
  const ordered = nodeOrder(nodes);
  const cols = ordered.length;
  const width = Math.max(360, cols * NODE_W + (cols - 1) * GAP + 40);
  const height = 230;
  const pos: Record<string, { x: number; y: number }> = {};
  ordered.forEach((n, i) => {
    pos[n.id] = { x: 20 + i * (NODE_W + GAP), y: 80 };
  });

  const edgePath = (from: string, to: string) => {
    const a = pos[from];
    const b = pos[to];
    if (!a || !b) return '';
    const x1 = a.x + NODE_W;
    const y1 = a.y + NODE_H / 2;
    const x2 = b.x;
    const y2 = b.y + NODE_H / 2;
    if (x2 >= x1) {
      const mx = (x1 + x2) / 2;
      return `M ${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`;
    }
    // 回边(agent ⇄ tools / advance_skill):向上绕行,不覆盖正向边
    const cy = y1 - 70;
    const bend = Math.max(50, (x1 - x2) / 2);
    return `M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x1 + bend} ${cy}, ${(x1 + x2) / 2} ${cy} S ${x2 - bend} ${y2}, ${x2} ${y2}`;
  };

  const edgeUsed = (from: string, to: string) =>
    executed.includes(from) && executed.includes(to);

  return (
    <div className="overflow-x-auto rounded-xl border border-slate-200 bg-white">
      <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} className="block">
        <defs>
          <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M 0 0 L 10 5 L 0 10 z" fill="#94a3b8" />
          </marker>
          <marker id="arrow-hot" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M 0 0 L 10 5 L 0 10 z" fill="#3b82f6" />
          </marker>
        </defs>
        {edges.map((e, i) => {
          const used = edgeUsed(e.from, e.to);
          return (
            <path key={i} d={edgePath(e.from, e.to)} fill="none"
              stroke={used ? '#3b82f6' : '#cbd5e1'} strokeWidth={used ? 2.5 : 1.5}
              strokeDasharray={used ? undefined : '5 5'} markerEnd={`url(#${used ? 'arrow-hot' : 'arrow'})`} />
          );
        })}
        {ordered.map((n) => {
          const hot = executed.includes(n.id);
          const p = pos[n.id];
          const isStart = n.id === 'START';
          const isEnd = n.id === 'END';
          return (
            <g key={n.id}>
              <rect x={p.x} y={p.y} width={NODE_W} height={NODE_H} rx={12}
                fill={isStart || isEnd ? (hot ? '#dbeafe' : '#f1f5f9') : hot ? '#eff6ff' : '#f8fafc'}
                stroke={hot ? '#3b82f6' : '#e2e8f0'} strokeWidth={hot ? 2 : 1} />
              <circle cx={p.x + 16} cy={p.y + NODE_H / 2} r={8}
                fill={isStart || isEnd ? '#3b82f6' : hot ? '#3b82f6' : '#cbd5e1'} />
              <text x={p.x + 30} y={p.y + 24} fontSize="13" fontWeight={600} fill="#0f172a">{n.label}</text>
              <text x={p.x + 30} y={p.y + 42} fontSize="11" fill="#64748b">{n.id}</text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}

export default function GraphViz() {
  const [taskId, setTaskId] = useState('');
  const [data, setData] = useState<GraphVizData | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const query = async () => {
    const id = taskId.trim();
    if (!id) return;
    setLoading(true);
    setError('');
    setData(null);
    try {
      setData(await getGraphTrace(id));
    } catch (e: any) {
      setError(e.message || '查询失败');
    } finally {
      setLoading(false);
    }
  };

  const statusMeta = data ? (STATUS_META[data.status] || { label: data.status, cls: 'bg-slate-100 text-slate-600' }) : null;

  return (
    <div className="mx-auto max-w-5xl space-y-6 p-6">
      <header className="flex items-center gap-3">
        <span className="flex h-11 w-11 items-center justify-center rounded-xl bg-blue-50 text-blue-600">
          <Workflow className="h-6 w-6" />
        </span>
        <div>
          <h1 className="text-lg font-semibold text-slate-800">链路可视化</h1>
          <p className="text-xs text-slate-500">按任务还原 langgraph 执行图与逐步执行链,排查"走到哪、卡在哪"</p>
        </div>
      </header>

      {/* 查询栏 */}
      <div className="flex gap-2">
        <input
          value={taskId}
          onChange={(e) => setTaskId(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && query()}
          placeholder="输入任务 ID(如 7c9e4b8a-…)"
          className="min-w-0 flex-1 rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-200"
        />
        <button onClick={query} disabled={loading || !taskId.trim()}
          className="inline-flex items-center gap-1.5 rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-50">
          {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Search className="h-4 w-4" />}
          查询
        </button>
      </div>

      {error && (
        <div className="flex items-center gap-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-600">
          <AlertTriangle className="h-4 w-4 shrink-0" />{error}
        </div>
      )}

      {data && statusMeta && (
        <>
          {/* 任务信息 */}
          <div className="grid gap-3 rounded-xl border border-slate-200 bg-white p-4 sm:grid-cols-2 lg:grid-cols-4">
            <Info label="任务 ID" value={data.task_id} mono />
            <Info label="状态" value={<span className={`rounded-full px-2 py-0.5 text-xs font-medium ${statusMeta.cls}`}>{statusMeta.label}</span>} />
            <Info label="执行器" value={`${data.executor_type} / ${data.executor_id}`} mono />
            <Info label="模式" value={<span className="rounded-full bg-violet-100 px-2 py-0.5 text-xs font-semibold text-violet-700">{data.mode}</span>} />
          </div>

          {data.error && (
            <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-700">
              <b>错误:</b> {data.error}
            </div>
          )}

          {/* 执行图 */}
          <section className="space-y-2">
            <h2 className="text-sm font-semibold text-slate-700">langgraph 执行图</h2>
            <GraphSvg data={data} />
            <p className="text-xs text-slate-400">
              蓝色 = 本轮实际执行过的节点/边;灰色虚线 = 模板中的候选路径(未走到)。
            </p>
          </section>

          {/* 执行链 */}
          <section className="space-y-2">
            <h2 className="text-sm font-semibold text-slate-700">逐步执行链({data.steps.length})</h2>
            <div className="space-y-2">
              {data.steps.length === 0 && <p className="rounded-lg border border-slate-200 bg-white p-4 text-sm text-slate-400">无执行记录(任务可能尚未开始)。</p>}
              {data.steps.map((s, i) => {
                const meta = STEP_META[s.step] || { label: s.step, cls: 'bg-slate-100 text-slate-600' };
                return (
                  <div key={i} className="flex items-start gap-3 rounded-xl border border-slate-200 bg-white p-3">
                    <span className={`w-24 shrink-0 rounded-full px-2 py-0.5 text-center text-xs font-medium ${meta.cls}`}>{meta.label}</span>
                    <div className="min-w-0 flex-1 space-y-0.5 text-xs">
                      <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-slate-500">
                        {s.mode && <span>模式 <b className="text-slate-700">{s.mode}</b></span>}
                        {s.intent && <span>意图 <b className="text-slate-700">{s.intent}</b></span>}
                        {s.tool_name && <span>工具 <b className="font-mono text-slate-700">{s.tool_name}</b></span>}
                        {s.kind && <span>结果 <b className="text-slate-700">{s.kind}</b></span>}
                        {s.verdict && <span>判定 <b className="text-slate-700">{s.verdict}</b></span>}
                        {s.skill_id && <span>技能 <b className="font-mono text-slate-700">{s.skill_id}</b></span>}
                        {s.reason && <span className="text-slate-400">{s.reason}</span>}
                      </div>
                      {s.error && <div className="mt-1 break-all rounded bg-rose-50 px-2 py-1 font-mono text-[11px] text-rose-600">{s.error}</div>}
                    </div>
                    {s.error && <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-red-500" />}
                    {!s.error && ['tool_call', 'generation'].includes(s.step) && <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-emerald-500" />}
                  </div>
                );
              })}
            </div>
          </section>
        </>
      )}
    </div>
  );
}

function Info({ label, value, mono }: { label: string; value: React.ReactNode; mono?: boolean }) {
  return (
    <div className="min-w-0">
      <div className="text-xs text-slate-400">{label}</div>
      <div className={`mt-0.5 truncate text-sm font-medium text-slate-700 ${mono ? 'font-mono text-xs' : ''}`}>{value}</div>
    </div>
  );
}
