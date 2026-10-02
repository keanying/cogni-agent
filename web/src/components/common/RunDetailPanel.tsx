import React from 'react';
import {
  Activity, Cpu, Timer, Coins, Wrench, StopCircle, Check, Loader2, X, Clock, Layers,
} from 'lucide-react';
import { Button } from '../ui/button';
import { cn } from '../../lib/utils';
import type { ExecutionPlan, PlanStep } from '../../lib/api';

/**
 * 右侧「执行详情」面板。
 *
 * 与中间对话区的分工:**对话区讲「发生了什么」(给用户看结果),
 * 这里讲「怎么发生的」(给排查问题的人看)** —— 运行 ID、实际用的模型、
 * 耗时、Token、每个工具调用的明细。出问题时第一时间要看的就是这些。
 *
 * 数据全部来自后端已有的字段,不需要新增接口:
 * 耗时是服务端首尾时间戳相减,不是前端按事件到达时间估的(那会把网络和渲染算进去)。
 */

export interface RunInfo {
  taskId?: string;
  mode?: string;
  model?: string;
  tokens?: { prompt: number; completion: number; cost: number;
             modelCost?: number | null; totalDurationMs?: number;
             /** 后端下发的本轮**实际生效**的 Token 预算。0 / 缺省 = 不限制,不画进度条。
              *  这个数只能来自后端(result.usage.token_budget)—— 前端写死一个常数
              *  就会出现「17,587 / 8,000 接近上限」这种实际没超的假告警。 */
             budget?: number | null;
             costReport?: { model: string; name?: string; calls: number }[] };
  plan?: ExecutionPlan;
  running?: boolean;
  /** 本轮跑的是谁(Agent / Skill / Quick BI 连接),显示在面板顶部 */
  targetName?: string;
  targetSub?: string;
  /** 额外的键值行(如 Quick BI 的能力/景区/目标数据),排在固定几行之后 */
  extraRows?: { label: string; value: React.ReactNode; mono?: boolean }[];
  /** 调用/步骤记录。Quick BI 没有 plan,但有自己的执行步骤,走同一套卡片渲染 */
  calls?: { id: string; title: string; sub?: string; status?: 'done' | 'running' | 'failed' | 'waiting' }[];
  /** 本轮上下文(景区、目标数据、固定问题…):让人知道这句问话是带着什么背景发出去的 */
  contextItems?: { icon?: any; label: string; value: string }[];
}

const Row: React.FC<{ icon: any; label: string; value: React.ReactNode; mono?: boolean }> =
  ({ icon: Icon, label, value, mono }) => (
  <div className="flex items-center justify-between py-1.5">
    <span className="flex items-center gap-1.5 text-[11px] text-slate-400">
      <Icon size={12} />{label}
    </span>
    <span className={cn('max-w-[60%] truncate text-[11.5px] font-semibold text-slate-700',
      mono && 'font-mono text-[11px]')}>{value}</span>
  </div>
);

const StepDot: React.FC<{ s: PlanStep }> = ({ s }) => {
  const tone = s.status === 'done' ? 'bg-emerald-500'
    : s.status === 'running' ? 'bg-blue-500'
    : s.status === 'failed' ? 'bg-rose-500' : 'bg-slate-300';
  return (
    <div className={cn('flex h-5 w-5 shrink-0 items-center justify-center rounded', tone)}>
      {s.status === 'done' ? <Check size={11} className="text-white" />
        : s.status === 'running' ? <Loader2 size={11} className="animate-spin text-white" />
        : s.status === 'failed' ? <X size={11} className="text-white" />
        : <Clock size={11} className="text-white" />}
    </div>
  );
};

const RunDetailPanel: React.FC<{
  info?: RunInfo;
  onAbort?: () => void;
  /** 平台 token 预算的**显式覆盖**。传 0 / null = 这条链路压根不走平台预算(如 Quick BI)。
   *  不传 = 用后端随本轮下发的真实预算(info.tokens.budget),这是默认且正确的来源。 */
  tokenBudget?: number | null;
  /** 没有预算时的一句说明,替代那条会误导人的进度条 */
  budgetNote?: string;
}> = ({ info, onAbort, tokenBudget, budgetNote }) => {
  if (!info?.taskId && !info?.targetName) {
    return (
      <div className="rounded-2xl border border-slate-200 bg-white p-5">
        <div className="mb-1 flex items-center gap-1.5">
          <Activity size={14} className="text-slate-400" />
          <span className="text-[13px] font-bold text-slate-700">执行详情</span>
        </div>
        <p className="py-8 text-center text-[11px] text-slate-300">发起对话后显示运行信息</p>
      </div>
    );
  }

  const t = info.tokens;
  const total = (t?.prompt || 0) + (t?.completion || 0);
  // 调用方显式传了(含 0/null,表示「这条链不走平台预算」)就听它的;没传才用后端下发的。
  const effBudget = tokenBudget !== undefined ? tokenBudget : (t?.budget ?? null);
  const budgeted = !!effBudget && effBudget > 0;
  const pct = Math.min(100, Math.round((total / Math.max(effBudget || 1, 1)) * 100));
  // 工具/脚本步骤才进「工具调用记录」—— analyze/compose 是模型自己在想,没有调用可言
  const toolSteps = (info.plan?.steps || []).filter((s) => s.kind === 'tool' || s.kind === 'script');
  const calls = info.calls || [];
  // 实际用的模型:优先取后端成本明细里回报的(那是真正发出去的),没有才用配置值
  const model = t?.costReport?.[0]?.name || t?.costReport?.[0]?.model || info.model;

  return (
    <div className="space-y-3">
      <div className="mb-1 flex items-center justify-between px-1">
        <div className="flex items-center gap-1.5">
          <Activity size={14} className="text-slate-400" />
          <span className="text-[13px] font-bold text-slate-700">执行详情</span>
        </div>
        {info.running && (
          <span className="flex items-center gap-1 rounded-full bg-blue-50 px-2 py-0.5 text-[10px] font-semibold text-blue-600">
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-blue-500" />运行中
          </span>
        )}
      </div>

      <div className="rounded-2xl border border-slate-200 bg-white p-4">
        {info.targetName && (
          <div className="mb-3 flex items-center gap-2.5 border-b border-slate-50 pb-3">
            <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-blue-50 text-blue-600">
              <Activity size={16} />
            </div>
            <div className="min-w-0">
              <div className="truncate text-[13px] font-bold text-slate-800">{info.targetName}</div>
              {info.targetSub && <div className="truncate text-[11px] text-slate-400">{info.targetSub}</div>}
            </div>
          </div>
        )}

        <div className="divide-y divide-slate-50">
          {info.taskId && <Row icon={Activity} label="运行 ID" value={info.taskId.slice(0, 12)} mono />}
          {model && <Row icon={Cpu} label="模型" value={model} mono />}
          {info.mode && <Row icon={Layers} label="执行模式" value={info.mode} mono />}
          <Row icon={Timer} label="总耗时"
               value={t?.totalDurationMs != null ? `${(t.totalDurationMs / 1000).toFixed(2)}s`
                      : (info.running ? '进行中…' : '—')} />
          {(info.extraRows || []).map((r) => (
            <Row key={r.label} icon={Layers} label={r.label} value={r.value} mono={r.mono} />
          ))}
        </div>

        {/* 预算进度条只在「确实有预算护栏」时才有意义,而「有没有护栏」只有后端知道:
            - Quick BI 这类外部能力压根不走平台额度(调用方显式传 0);
            - 护栏出厂默认不限制,没在 Skill 上配 Token 预算就没有上限。
            这两种情况都只报消耗、不画条 —— 以前前端写死 8,000 当上限,于是
            一轮正常的 17,587 token 被画成 100% 红条 + 「即将熔断」,纯属吓人。 */}
        {budgeted ? (
          <div className="mt-3">
            <div className="mb-1 flex items-center justify-between">
              <span className="flex items-center gap-1.5 text-[11px] text-slate-400">
                <Coins size={12} />Token 消耗
              </span>
              <span className="font-mono text-[11px] font-semibold text-slate-700">
                {total.toLocaleString()} / {(effBudget as number).toLocaleString()}
              </span>
            </div>
            <div className="h-1.5 overflow-hidden rounded-full bg-slate-100">
              <div className={cn('h-full rounded-full transition-all',
                pct > 90 ? 'bg-rose-500' : pct > 70 ? 'bg-amber-500' : 'bg-blue-500')}
                style={{ width: `${pct}%` }} />
            </div>
            {t && (
              <div className="mt-1 flex justify-between font-mono text-[10px] text-slate-400">
                <span>输入 {t.prompt.toLocaleString()}</span>
                <span>输出 {t.completion.toLocaleString()}</span>
              </div>
            )}
            {pct > 90 && (
              <p className="mt-1 text-[10px] text-rose-500">接近预算上限,超出后会被护栏熔断</p>
            )}
          </div>
        ) : total > 0 ? (
          <div className="mt-3">
            <div className="flex items-center justify-between">
              <span className="flex items-center gap-1.5 text-[11px] text-slate-400">
                <Coins size={12} />Token 消耗
              </span>
              <span className="font-mono text-[11px] font-semibold text-slate-700">{total.toLocaleString()}</span>
            </div>
            <div className="mt-1 flex justify-between font-mono text-[10px] text-slate-400">
              <span>输入 {(t?.prompt ?? 0).toLocaleString()}</span>
              <span>输出 {(t?.completion ?? 0).toLocaleString()}</span>
            </div>
            {budgetNote && <p className="mt-1 text-[10px] text-slate-400">{budgetNote}</p>}
          </div>
        ) : budgetNote ? (
          <p className="mt-3 text-[10px] text-slate-400">{budgetNote}</p>
        ) : null}
      </div>

      {toolSteps.length > 0 && (
        <div className="rounded-2xl border border-slate-200 bg-white p-4">
          <div className="mb-2 text-[11px] font-medium uppercase tracking-wider text-slate-500">
            工具调用记录
          </div>
          {/* 按状态整块着色,而不是在一行里放个小圆点 ——
              扫一眼就知道卡在哪一步 */}
          <div className="space-y-2">
            {toolSteps.map((s) => {
              const box = s.status === 'done' ? 'bg-emerald-50 border-emerald-100'
                : s.status === 'running' ? 'bg-blue-50 border-blue-100'
                : s.status === 'failed' ? 'bg-rose-50 border-rose-100'
                : 'bg-slate-50 border-slate-100';
              const sub = s.status === 'done'
                ? [s.result_summary?.replace(/^返回\s*/, ''),
                   s.duration_ms != null ? `${(s.duration_ms / 1000).toFixed(2)}s` : null]
                    .filter(Boolean).join(' · ')
                : s.status === 'running' ? '执行中…'
                : s.status === 'failed' ? (s.result_summary || '失败')
                : s.status === 'skipped' ? '未执行' : '等待中…';
              return (
                <div key={s.id} className={cn('flex items-start gap-2.5 rounded-lg border p-2.5', box)}>
                  <StepDot s={s} />
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-xs font-medium text-slate-700">
                      {s.tool_name || s.title}
                    </div>
                    <div className="mt-0.5 truncate text-[11px] text-slate-400">{sub}</div>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {calls.length > 0 && (
        <div className="rounded-2xl border border-slate-200 bg-white p-4">
          <div className="mb-2 text-[11px] font-medium uppercase tracking-wider text-slate-500">执行步骤</div>
          <div className="space-y-2">
            {calls.map((c) => {
              const box = c.status === 'failed' ? 'bg-rose-50 border-rose-100'
                : c.status === 'running' ? 'bg-blue-50 border-blue-100'
                : c.status === 'waiting' ? 'bg-slate-50 border-slate-100'
                : 'bg-emerald-50 border-emerald-100';
              return (
                <div key={c.id} className={cn('flex items-start gap-2.5 rounded-lg border p-2.5', box)}>
                  <StepDot s={{ status: c.status === 'waiting' ? 'pending' : (c.status || 'done') } as PlanStep} />
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-xs font-medium text-slate-700">{c.title}</div>
                    {c.sub && <div className="mt-0.5 truncate text-[11px] text-slate-400">{c.sub}</div>}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {(info.contextItems || []).length > 0 && (
        <div className="rounded-2xl border border-slate-200 bg-white p-4">
          <div className="mb-2 text-[11px] font-medium uppercase tracking-wider text-slate-500">本轮上下文</div>
          <div className="space-y-1.5">
            {info.contextItems!.map((c) => {
              const Icon = c.icon || Layers;
              return (
                <div key={c.label} className="flex items-center gap-2 rounded-lg bg-slate-50 px-2.5 py-1.5">
                  <Icon size={12} className="shrink-0 text-slate-400" />
                  <span className="shrink-0 text-[11px] text-slate-400">{c.label}</span>
                  <span className="truncate text-[11.5px] font-medium text-slate-700">{c.value}</span>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {info.running && onAbort && (
        <Button variant="outline" onClick={onAbort}
                className="w-full gap-1.5 border-rose-200 text-[12px] text-rose-600 hover:bg-rose-50">
          <StopCircle size={14} />中止执行
        </Button>
      )}
    </div>
  );
};

export default RunDetailPanel;
