import React, { useState } from 'react';
import {
  ChevronDown, ChevronRight, Check, Loader2, X, Clock, Database, Code2,
  BookOpen, Brain, FileText, AlertTriangle, Cog, SkipForward,
} from 'lucide-react';
import { cn } from '../../lib/utils';
import type { ExecutionPlan, PlanStep } from '../../lib/api';

/**
 * L4 执行计划时间线。
 *
 * 与既有 timeline 步骤条的分工:
 * - timeline 记录「**已经**发生了什么」(事后追溯)
 * - 这里展示「**打算**做什么 + 做到哪一步」(执行中的进度)
 *
 * 关键差别在于**未执行的步骤也会显示**(「等待上一步完成」)——
 * 计划是动手前整份下发的,所以用户一开始就知道总共几步、现在第几步。
 * 这是「等待」和「知道在等什么」的区别。
 *
 * 跑完自动收起(标题行留摘要),出问题时可展开看每步的结果与发现项。
 */

const KIND_ICON: Record<PlanStep['kind'], any> = {
  tool: Database, script: Code2, knowledge: BookOpen, analyze: Brain, compose: FileText,
};

const STATUS_CLASS: Record<PlanStep['status'], string> = {
  done: 'plan-done', running: 'plan-running', failed: 'plan-failed',
  pending: 'plan-pending', skipped: 'plan-skipped',
};

const StatusIcon: React.FC<{ status: PlanStep['status'] }> = ({ status }) => {
  if (status === 'done') return <Check size={12} />;
  if (status === 'running') return <Loader2 size={12} className="animate-spin" />;
  if (status === 'failed') return <X size={12} />;
  if (status === 'skipped') return <SkipForward size={11} />;
  return <Clock size={12} />;
};

/** 右侧状态文案。已完成显示实测耗时;未开始要说清在等什么,而不是只画个灰点。 */
const statusText = (s: PlanStep) => {
  if (s.status === 'done') return s.duration_ms != null ? `✓ ${(s.duration_ms / 1000).toFixed(2)}s` : '✓ 完成';
  if (s.status === 'running') return '进行中…';
  if (s.status === 'failed') return '失败';
  if (s.status === 'skipped') return '未执行';
  return '等待上一步完成…';
};

const statusColor = (s: PlanStep) =>
  s.status === 'done' ? 'text-emerald-500'
    : s.status === 'failed' ? 'text-rose-500'
    : s.status === 'running' ? 'text-blue-500' : 'text-slate-300';

const PlanTrace: React.FC<{ plan: ExecutionPlan; defaultOpen?: boolean }> = ({ plan, defaultOpen }) => {
  const running = plan.steps.some((s) => s.status === 'running' || s.status === 'pending');
  const [open, setOpen] = useState(defaultOpen ?? running);

  if (!plan?.steps?.length) return null;

  const done = plan.steps.filter((s) => s.status === 'done').length;
  const failed = plan.steps.some((s) => s.status === 'failed');

  return (
    <div className="mb-3 rounded-xl border border-slate-200 bg-slate-50/60">
      <button onClick={() => setOpen((v) => !v)}
              className="flex w-full items-center gap-2 px-3 py-2 text-left transition-colors hover:bg-slate-100/60">
        <Cog size={13} className={cn('shrink-0', running ? 'animate-spin text-blue-500' : 'text-slate-400')} />
        <span className="text-[11px] font-semibold uppercase tracking-wider text-slate-500">执行计划</span>
        <span className={cn('text-[11px]', failed ? 'text-rose-500' : 'text-slate-400')}>
          {done}/{plan.steps.length} 步{failed ? ' · 有失败' : running ? '' : ' · 已完成'}
        </span>
        {!open && plan.summary && (
          <span className="ml-1 truncate text-[11px] text-slate-400">— {plan.summary}</span>
        )}
        <span className="ml-auto shrink-0 text-slate-300">
          {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
        </span>
      </button>

      {open && (
        <div className="space-y-2 border-t border-slate-200 px-3 py-2.5">
          {plan.summary && (
            <p className="mb-2 text-[11px] leading-relaxed text-slate-500">{plan.summary}</p>
          )}
          {plan.steps.map((s, i) => {
            const Icon = KIND_ICON[s.kind] || Brain;
            const isLast = i === plan.steps.length - 1;
            return (
              <div key={s.id}
                   className={cn('relative flex gap-2.5', STATUS_CLASS[s.status], !isLast && 'plan-step-line')}>
                <div className="plan-icon relative flex h-8 w-8 shrink-0 items-center justify-center rounded-full">
                  <StatusIcon status={s.status} />
                </div>

                <div className={cn('min-w-0 flex-1 pb-2',
                  (s.status === 'pending' || s.status === 'skipped') && 'opacity-60')}>
                  <div className="flex items-center justify-between gap-2">
                    <span className="flex min-w-0 items-center gap-1.5 text-xs font-medium text-slate-700">
                      <Icon size={11} className="shrink-0 text-slate-400" />
                      <span className="truncate">{s.title}</span>
                    </span>
                    <span className={cn('shrink-0 text-[11px] font-medium', statusColor(s))}>
                      {statusText(s)}
                    </span>
                  </div>

                  {s.detail && <div className="mt-0.5 text-[11px] leading-relaxed text-slate-400">{s.detail}</div>}

                  {/* 结果摘要用深色终端块:它是「机器返回的东西」,
                      和上面那行人话描述在视觉上要能一眼分开 */}
                  {s.result_summary && s.status !== 'pending' && (
                    <div className="mt-1.5 rounded-md bg-[#0f1b2d] px-2 py-1.5 font-mono text-[10.5px] leading-relaxed text-emerald-300">
                      → {s.result_summary}
                    </div>
                  )}

                  {/* 发现项单独用警示色:混在普通摘要里很容易被划过去 */}
                  {(s.findings || []).map((f, fi) => (
                    <div key={fi}
                         className="mt-1.5 rounded-md border border-amber-100 bg-amber-50 px-2 py-1.5 text-[10.5px] leading-relaxed text-slate-600">
                      <AlertTriangle size={10} className="mr-1 inline text-amber-500" />{f}
                    </div>
                  ))}

                  {/* 正在跑但还没结果时给个「在动」的信号,
                      否则「正在执行」和「卡住了」看起来一模一样 */}
                  {s.status === 'running' && !s.result_summary && (
                    <div className="mt-1.5 flex gap-1">
                      <span className="plan-dot" /><span className="plan-dot" /><span className="plan-dot" />
                    </div>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
};

export default PlanTrace;
