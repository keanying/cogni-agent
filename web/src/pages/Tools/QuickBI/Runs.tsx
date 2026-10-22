import React, { useEffect, useState } from 'react';
import { Loader2, RefreshCw, FileDown, Square } from 'lucide-react';
import { toast } from 'sonner';
import { Button } from '../../../components/ui/button';
import { Badge } from '../../../components/ui/badge';
import { Modal } from '../../../components/ui/PageHelpers';
import { confirmDialog } from '../../../components/ui/confirm';
import { BatchLogModal, type BatchLogLine } from './BatchOps';
import * as api from '../../../lib/api';
import { cn } from '../../../lib/utils';
import ResultView, { CAP_COLOR, liveFromRun } from './ResultView';
import { CAPABILITY_OPTIONS, describeTarget, selectCls } from './shared';

const MODE_LABEL: Record<string, string> = { direct: '直接调用', fixed: '固定问题', chat: '对话', report: '报告', tool: 'Agent 工具' };
const SOURCE_LABEL: Record<string, string> = { console: '管理台', api: '开放 API', agent: '执行引擎' };

// 一键终止「到底做了什么」:批量一次动几十条,顶上不写清楚口径,
// 看日志的人只能靠猜 —— 尤其是「已不是进行中」这类正常但看着像失败的结果。
const ABORT_ABOUT = [
  '只动「进行中」的运行,已收尾的一条都不碰。',
  '记录判为失败(error_code=aborted);问句、答案、耗时、执行留痕全部保留。',
  '按全部进行中的算,不限当前这一页;景区账号只能终止自己景区的。',
  '中途自己跑完的那条会记「已不是进行中」—— 这是达成,不是失败。',
  '报告类上游没有取消接口,终止的是平台不再跟踪它,Quick BI 侧可能仍在生成。',
];

const Runs: React.FC = () => {
  const [items, setItems] = useState<api.QbiRun[]>([]);
  const [loading, setLoading] = useState(true);
  // 任务号是「运行记录 ↔ 任务」的唯一可靠线索:APP 对话 / 执行引擎调起的运行,光看问句和时间认不出是哪个任务
  const [filters, setFilters] = useState({ capability: '', mode: '', status: '', q: '', task_id: '' });
  const [detail, setDetail] = useState<api.QbiRun | null>(null);
  // 一键终止的批量日志:跑完也不自动关 —— 关掉就看不到哪条失败了,
  // 而「漏一条」恰恰是批量操作最需要被看见的结果。
  const [abortOpen, setAbortOpen] = useState(false);
  const [abortRunning, setAbortRunning] = useState(false);
  const [abortLines, setAbortLines] = useState<BatchLogLine[]>([]);
  const [abortStat, setAbortStat] = useState<{ total: number; succeeded: number; failed: number } | null>(null);
  const [page, setPage] = useState(0);
  const PAGE = 30;
  // 问题名称搜索单独存一份输入:每敲一个字就发一次请求会把列表刷成抖动,
  // 停手 300ms 后才落进筛选条件去查。
  const [qInput, setQInput] = useState('');

  const load = async () => {
    setLoading(true);
    try { setItems((await api.qbiListRuns({ ...filters, limit: PAGE, offset: page * PAGE })).items); }
    catch (e: any) { toast.error(e.message); }
    finally { setLoading(false); }
  };
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [filters, page]);
  useEffect(() => {
    const t = setTimeout(() => { setPage(0); setFilters((f) => ({ ...f, q: qInput.trim() })); }, 300);
    return () => clearTimeout(t);
  }, [qInput]);

  const exportDetail = async (runId: string) => {
    try {
      const md = await api.qbiRunDebugMarkdown(runId);
      const url = URL.createObjectURL(new Blob([md], { type: 'text/markdown;charset=utf-8' }));
      const a = document.createElement('a');
      a.href = url;
      a.download = `quickbi-run-${runId.slice(0, 8)}.md`;
      a.click();
      URL.revokeObjectURL(url);
      toast.success('已导出执行详情(不含任何凭证)');
    } catch (e: any) { toast.error(e?.message || '导出失败'); }
  };

  const open = async (r: api.QbiRun) => {
    try { setDetail(r.capability === 'report' && r.status === 'running' ? await api.qbiGetReport(r.id) : await api.qbiGetRun(r.id)); }
    catch (e: any) { toast.error(e.message); }
  };

  /** 单条终止:这条自己卡住了(或不想再等)时用。 */
  const abortOne = async (id: string) => {
    if (!await confirmDialog({
      title: '终止这条运行',
      message: '记录会被判为失败(error_code=aborted),问题、答案、耗时这些仍然保留。\n'
        + '如果是报告,终止的是平台不再跟踪它 —— Quick BI 那边可能还在生成。',
      confirmLabel: '终止', danger: true,
    })) return;
    try {
      const r = await api.qbiAbortRun(id);
      toast.success(r.terminated ? '已终止' : (r.hint || '无需终止'));
      setDetail(null); load();
    } catch (e: any) { toast.error(e.message); }
  };

  /** 一键终止:清掉卡在「进行中」的那些。多数是发版/重启掐断留下的僵尸 ——
   *  执行体早就不在了,除了判死没有别的收尾办法。按全部算,不限于当前这一页。
   *
   *  跑的过程用批量日志弹窗逐条滚:批量动几十条时只给「成功 N / 失败 M」回答不了
   *  「漏的是哪一条」,而漏一条恰恰是最该被看见的结果。 */
  const abortAll = async () => {
    const onPage = items.filter((r) => r.status === 'running').length;
    if (!await confirmDialog({
      title: '一键终止进行中的运行',
      message: '所有「进行中」的运行都会被判为失败(error_code=aborted),已收尾的不受影响。\n'
        + (onPage ? `当前页有 ${onPage} 条进行中;实际按全部进行中的算(不限这一页)。` : '按全部进行中的算,不限当前这一页。')
        + '\n\n报告类只是停止跟踪,Quick BI 侧可能仍在生成。',
      confirmLabel: '全部终止', danger: true,
    })) return;
    const now = () => new Date().toLocaleTimeString('zh-CN', { hour12: false });
    const push = (l: BatchLogLine) => setAbortLines((ls) => [...ls, { at: now(), ...l }]);
    setAbortLines([]); setAbortStat(null); setAbortRunning(true); setAbortOpen(true);
    try {
      await api.qbiAbortRunsStream({}, (e) => {
        if (e.type === 'start') push({ text: e.message || `开始处理 ${e.total} 条` });
        else if (e.type === 'item') push({ ok: e.ok,
          text: `[${e.index}/${e.total}] ${e.title || e.id?.slice(0, 8) || ''} · ${e.ok ? '成功' : '失败'}${e.message ? ` · ${e.message}` : ''}` });
        else if (e.type === 'done') {
          setAbortStat({ total: e.total || 0, succeeded: e.succeeded || 0, failed: e.failed || 0 });
          push({ text: `完成:成功 ${e.succeeded} / 失败 ${e.failed}(共 ${e.total})`, ok: !e.failed });
        }
      });
    } catch (e: any) {
      push({ ok: false, text: `请求失败:${e.message}` });
    } finally {
      setAbortRunning(false);
      load();     // 批量动完不刷列表,列表还是旧值,会让人以为没生效
    }
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <select className={cn(selectCls, 'w-44')} value={filters.capability} onChange={(e) => { setPage(0); setFilters({ ...filters, capability: e.target.value }); }}>
          <option value="">全部能力</option>
          {CAPABILITY_OPTIONS.filter((o) => o.key !== 'auto').map((o) => <option key={o.key} value={o.key}>{o.label}</option>)}
        </select>
        <select className={cn(selectCls, 'w-36')} value={filters.mode} onChange={(e) => { setPage(0); setFilters({ ...filters, mode: e.target.value }); }}>
          <option value="">全部场景</option>
          {Object.entries(MODE_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
        <select className={cn(selectCls, 'w-32')} value={filters.status} onChange={(e) => { setPage(0); setFilters({ ...filters, status: e.target.value }); }}>
          <option value="">全部状态</option><option value="success">成功</option><option value="failed">失败</option><option value="running">进行中</option>
        </select>
        {/* 运行记录最常用的找法就是「那条问题刚才跑成什么样了」 */}
        <input className={cn(selectCls, 'w-72')} placeholder="按问题名称搜(问句正文 / 固定问题标题)" value={qInput}
          onChange={(e) => setQInput(e.target.value)} />
        <input className={cn(selectCls, 'w-56')} placeholder="按任务号查(APP / 执行引擎)" value={filters.task_id}
          onChange={(e) => { setPage(0); setFilters({ ...filters, task_id: e.target.value.trim() }); }} />
        <Button variant="outline" onClick={load} className="gap-1.5"><RefreshCw size={14} />刷新</Button>
        <Button variant="outline" onClick={abortAll}
          className="gap-1.5 border-rose-200 text-rose-600 hover:bg-rose-50">
          <Square size={14} />一键终止进行中
        </Button>
      </div>
      <div className="overflow-hidden rounded-2xl border border-slate-200 bg-white">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-left text-xs text-slate-500">
            <tr><th className="px-3 py-2">时间</th><th className="px-3 py-2">问题</th><th className="px-3 py-2">能力</th>
              <th className="px-3 py-2">场景 / 来源</th><th className="px-3 py-2">任务号</th><th className="px-3 py-2">目标</th><th className="px-3 py-2">状态</th><th className="px-3 py-2 text-right">耗时</th></tr>
          </thead>
          <tbody>
            {loading && <tr><td colSpan={8} className="py-10 text-center"><Loader2 className="mx-auto animate-spin text-slate-300" /></td></tr>}
            {!loading && items.length === 0 && <tr><td colSpan={8} className="py-10 text-center text-xs text-slate-400">暂无运行记录</td></tr>}
            {!loading && items.map((r) => (
              <tr key={r.id} className="cursor-pointer border-t border-slate-100 hover:bg-slate-50" onClick={() => open(r)}>
                <td className="whitespace-nowrap px-3 py-2 text-xs text-slate-400">{r.created_at ? new Date(r.created_at).toLocaleString() : ''}</td>
                <td className="max-w-sm px-3 py-2 text-slate-700">
                  <div className="truncate">{r.question}</div>
                  {/* 固定问题运行:存的问句已拼过口径、{{变量}} 也替换过,跟固定问题列表里
                      那条问题的标题不是同一段文本 —— 搜标题命中时,靠这行才看得出来历 */}
                  {r.fixed_question_title && r.fixed_question_title !== r.question && (
                    <div className="truncate text-[10px] text-slate-400">固定问题:{r.fixed_question_title}</div>
                  )}
                </td>
                <td className="px-3 py-2"><Badge className={cn('border', CAP_COLOR[r.capability])}>{r.capability_label}</Badge></td>
                <td className="px-3 py-2 text-xs text-slate-500">{MODE_LABEL[r.mode] || r.mode} · {SOURCE_LABEL[r.source] || r.source}</td>
                <td className="px-3 py-2 font-mono text-[11px] text-slate-400">
                  {r.task_id
                    ? <button type="button" title={`触发本次运行的任务 ${r.task_id}(点击按它筛选)`} className="hover:text-blue-600"
                        onClick={(e) => { e.stopPropagation(); setPage(0); setFilters({ ...filters, task_id: r.task_id || '' }); }}>
                        {r.task_id.slice(0, 8)}</button>
                    : <span className="text-slate-300">—</span>}
                </td>
                <td className="max-w-[200px] truncate px-3 py-2 text-xs text-slate-500">{describeTarget(r.target)}</td>
                <td className="px-3 py-2 text-xs">
                  <span className={r.status === 'success' ? 'text-emerald-600' : r.status === 'failed' ? 'text-rose-500' : 'text-blue-500'}>
                    {({ success: '成功', failed: '失败', running: '进行中' } as any)[r.status] || r.status}</span>
                  {r.error && <div className="max-w-[220px] truncate text-[10px] text-rose-400" title={r.error}>{r.error}</div>}
                  {r.status === 'running' && (
                    <button type="button" className="mt-0.5 block text-[10px] text-rose-500 hover:underline"
                      onClick={(e) => { e.stopPropagation(); abortOne(r.id); }}>终止</button>
                  )}
                </td>
                <td className="px-3 py-2 text-right text-xs text-slate-400">{r.duration_ms != null ? `${(r.duration_ms / 1000).toFixed(1)}s` : '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="flex items-center justify-end gap-2 border-t border-slate-100 px-3 py-2 text-xs">
          <Button size="sm" variant="outline" disabled={page === 0} onClick={() => setPage(page - 1)}>上一页</Button>
          <span className="text-slate-400">第 {page + 1} 页</span>
          <Button size="sm" variant="outline" disabled={items.length < PAGE} onClick={() => setPage(page + 1)}>下一页</Button>
        </div>
      </div>
      {detail && (
        <Modal title="运行详情" wide onClose={() => setDetail(null)}>
          <div className="flex items-start justify-between gap-3">
            <div className="flex-1 rounded-xl bg-slate-50 px-3 py-2 text-sm text-slate-700">{detail.question}</div>
            {/* 排障时把这一份发给研发即可:含上游原样事件与归一化事件,不含任何凭证 */}
            <Button size="sm" variant="outline" className="shrink-0 gap-1.5" onClick={() => exportDetail(detail.id)}>
              <FileDown size={13} />导出执行详情
            </Button>
            {detail.status === 'running' && (
              <Button size="sm" variant="outline" className="shrink-0 gap-1.5 border-rose-200 text-rose-600 hover:bg-rose-50"
                onClick={() => abortOne(detail.id)}>
                <Square size={13} />终止这条
              </Button>
            )}
          </div>
          <ResultView live={liveFromRun(detail)} />
          <details className="text-xs text-slate-400">
            <summary className="cursor-pointer">原始记录</summary>
            <pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap font-mono text-[10px]">{JSON.stringify({ ...detail, result: undefined }, null, 2)}</pre>
          </details>
        </Modal>
      )}
      {/* 一键终止的批量日志:逐条滚,跑完由用户自己关 */}
      {abortOpen && (
        <BatchLogModal title="一键终止进行中" lines={abortLines} running={abortRunning}
          total={abortStat?.total} succeeded={abortStat?.succeeded} failed={abortStat?.failed}
          about={ABORT_ABOUT} onClose={() => setAbortOpen(false)} />
      )}
    </div>
  );
};

export default Runs;
