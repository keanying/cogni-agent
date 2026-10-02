import React, { useEffect, useState } from 'react';
import { Loader2, RefreshCw, FileDown } from 'lucide-react';
import { toast } from 'sonner';
import { Button } from '../../../components/ui/button';
import { Badge } from '../../../components/ui/badge';
import { Modal } from '../../../components/ui/PageHelpers';
import * as api from '../../../lib/api';
import { cn } from '../../../lib/utils';
import ResultView, { CAP_COLOR, liveFromRun } from './ResultView';
import { CAPABILITY_OPTIONS, describeTarget, selectCls } from './shared';

const MODE_LABEL: Record<string, string> = { direct: '直接调用', fixed: '固定问题', chat: '对话', report: '报告', tool: 'Agent 工具' };
const SOURCE_LABEL: Record<string, string> = { console: '管理台', api: '开放 API', agent: '执行引擎' };

const Runs: React.FC = () => {
  const [items, setItems] = useState<api.QbiRun[]>([]);
  const [loading, setLoading] = useState(true);
  const [filters, setFilters] = useState({ capability: '', mode: '', status: '' });
  const [detail, setDetail] = useState<api.QbiRun | null>(null);
  const [page, setPage] = useState(0);
  const PAGE = 30;

  const load = async () => {
    setLoading(true);
    try { setItems((await api.qbiListRuns({ ...filters, limit: PAGE, offset: page * PAGE })).items); }
    catch (e: any) { toast.error(e.message); }
    finally { setLoading(false); }
  };
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [filters, page]);

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
        <Button variant="outline" onClick={load} className="gap-1.5"><RefreshCw size={14} />刷新</Button>
      </div>
      <div className="overflow-hidden rounded-2xl border border-slate-200 bg-white">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-left text-xs text-slate-500">
            <tr><th className="px-3 py-2">时间</th><th className="px-3 py-2">问题</th><th className="px-3 py-2">能力</th>
              <th className="px-3 py-2">场景 / 来源</th><th className="px-3 py-2">目标</th><th className="px-3 py-2">状态</th><th className="px-3 py-2 text-right">耗时</th></tr>
          </thead>
          <tbody>
            {loading && <tr><td colSpan={7} className="py-10 text-center"><Loader2 className="mx-auto animate-spin text-slate-300" /></td></tr>}
            {!loading && items.length === 0 && <tr><td colSpan={7} className="py-10 text-center text-xs text-slate-400">暂无运行记录</td></tr>}
            {!loading && items.map((r) => (
              <tr key={r.id} className="cursor-pointer border-t border-slate-100 hover:bg-slate-50" onClick={() => open(r)}>
                <td className="whitespace-nowrap px-3 py-2 text-xs text-slate-400">{r.created_at ? new Date(r.created_at).toLocaleString() : ''}</td>
                <td className="max-w-sm truncate px-3 py-2 text-slate-700">{r.question}</td>
                <td className="px-3 py-2"><Badge className={cn('border', CAP_COLOR[r.capability])}>{r.capability_label}</Badge></td>
                <td className="px-3 py-2 text-xs text-slate-500">{MODE_LABEL[r.mode] || r.mode} · {SOURCE_LABEL[r.source] || r.source}</td>
                <td className="max-w-[200px] truncate px-3 py-2 text-xs text-slate-500">{describeTarget(r.target)}</td>
                <td className="px-3 py-2 text-xs">
                  <span className={r.status === 'success' ? 'text-emerald-600' : r.status === 'failed' ? 'text-rose-500' : 'text-blue-500'}>
                    {({ success: '成功', failed: '失败', running: '进行中' } as any)[r.status] || r.status}</span>
                  {r.error && <div className="max-w-[220px] truncate text-[10px] text-rose-400" title={r.error}>{r.error}</div>}
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
          </div>
          <ResultView live={liveFromRun(detail)} />
          <details className="text-xs text-slate-400">
            <summary className="cursor-pointer">原始记录</summary>
            <pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap font-mono text-[10px]">{JSON.stringify({ ...detail, result: undefined }, null, 2)}</pre>
          </details>
        </Modal>
      )}
    </div>
  );
};

export default Runs;
