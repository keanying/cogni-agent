
import React, { useEffect, useState } from 'react';
import {
  Plus, Trash2, Play, CheckCircle2, XCircle, RefreshCw, Beaker, ClipboardCheck, Loader2, ArrowRight, Info,
} from 'lucide-react';
import { Card, CardContent } from '../../components/ui/card';
import { Button } from '../../components/ui/button';
import { Badge } from '../../components/ui/badge';
import { cn } from '../../lib/utils';
import { useNavigate } from 'react-router-dom';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '../../components/ui/tabs';
import { toast } from 'sonner';
import * as api from '../../lib/api';
import { confirmDialog } from '../../components/ui/confirm';

const EvaluationCenter: React.FC = () => {
  const navigate = useNavigate();
  const [cases, setCases] = useState<any[]>([]);
  const [batches, setBatches] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [runningId, setRunningId] = useState<string | null>(null);

  const load = async () => {
    setLoading(true);
    try {
      const [c, b] = await Promise.all([api.listEvalCases(), api.listEvalRunBatches()]);
      setCases(c.items); setBatches(b.items);
    } catch (e: any) { toast.error(e.message || '加载评测数据失败'); }
    finally { setLoading(false); }
  };
  useEffect(() => { load(); }, []);

  const runSingle = async (c: any) => {
    setRunningId(c.id);
    toast.info('正在运行评测（真实调用执行引擎，请稍候）...');
    try {
      const report: any = await api.triggerEvalRun({ target_type: c.target_type, target_id: c.target_id, case_ids: [c.id] });
      if (report.pass_rate === 1) toast.success(`运行完成：${report.passed}/${report.total} 通过`);
      else toast.error(`运行完成：${report.passed}/${report.total} 通过`);
      load();
    } catch (e: any) { toast.error(e.message || '运行失败'); }
    finally { setRunningId(null); }
  };

  const runAllForTarget = async (targetType: string, targetId: string) => {
    setRunningId(`${targetType}:${targetId}`);
    toast.info('正在运行该目标的全部用例...');
    try {
      const report: any = await api.triggerEvalRun({ target_type: targetType, target_id: targetId });
      toast.success(`运行完成：${report.passed}/${report.total} 通过`);
      load();
    } catch (e: any) { toast.error(e.message || '运行失败'); }
    finally { setRunningId(null); }
  };

  const removeCase = async (id: string) => {
    if (!await confirmDialog({ title: '删除该评测用例',
      message: '用例及其历史评测结果一并删除,发布门禁的通过率会按剩余用例重新计算。',
      confirmLabel: '删除', danger: true })) return;
    try { await api.deleteEvalCase(id); toast.success('已删除'); load(); }
    catch (e: any) { toast.error(e.message || '删除失败'); }
  };

  // 按 target 分组用例，便于"整组跑一遍"
  const groups = cases.reduce((acc: Record<string, any[]>, c) => {
    const k = `${c.target_type}:${c.target_id}`;
    (acc[k] = acc[k] || []).push(c);
    return acc;
  }, {});

  return (
    <div className="space-y-6 animate-in fade-in slide-in-from-bottom-4 duration-500">
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-slate-900 tracking-tight">评测中心</h1>
          <p className="text-slate-500 text-sm mt-1">通过自动化回归用例集验证智能体能力，建立科学的质量发布门禁。</p>
        </div>
        <div className="flex gap-3">
          <Button variant="outline" className="rounded-xl h-11 px-5 font-bold border-slate-200 gap-2" onClick={load}><RefreshCw size={15} />刷新</Button>
          <Button className="bg-[#2563eb] hover:bg-[#1d4ed8] text-white rounded-xl h-11 px-6 font-bold gap-2 shadow-lg shadow-blue-200" onClick={() => navigate('/audit/evaluation/new')}>
            <Plus size={17} />新建用例
          </Button>
        </div>
      </div>

      <div className="bg-blue-50/50 border border-blue-100 rounded-xl p-4 flex items-start gap-3">
        <Info size={16} className="text-blue-500 shrink-0 mt-0.5" />
        <p className="text-xs text-blue-800 leading-relaxed">
          用例运行会<span className="font-bold">真实调用执行引擎</span>（与操练台同一条链路），按「期望关键词命中率」判定通过与否；
          Agent 发布时会自动跑一遍其绑定的用例集作为发布门禁，通过率不达标将阻断发布。
        </p>
      </div>

      <Tabs defaultValue="reports" className="w-full">
        <TabsList className="bg-slate-100/50 p-1 border rounded-xl gap-1">
          <TabsTrigger value="reports" className="rounded-lg data-[state=active]:bg-white data-[state=active]:shadow-sm font-bold gap-2 px-6 py-2 text-xs uppercase tracking-widest">
            <ClipboardCheck size={14} />运行报告 ({batches.length})
          </TabsTrigger>
          <TabsTrigger value="cases" className="rounded-lg data-[state=active]:bg-white data-[state=active]:shadow-sm font-bold gap-2 px-6 py-2 text-xs uppercase tracking-widest">
            <Beaker size={14} />用例库 ({cases.length})
          </TabsTrigger>
        </TabsList>

        <div className="mt-6">
          <TabsContent value="reports">
            <div className="enterprise-table-container overflow-x-auto">
              <table className="enterprise-table">
                <thead><tr><th>评测目标</th><th>用例数</th><th className="text-center">通过率</th><th>运行时间</th><th className="text-right">操作</th></tr></thead>
                <tbody>
                  {loading ? (
                    <tr><td colSpan={5} className="py-16 text-center"><Loader2 className="animate-spin text-slate-300 mx-auto" size={24} /></td></tr>
                  ) : batches.length === 0 ? (
                    <tr><td colSpan={5} className="py-16 text-center text-slate-300">暂无运行记录，先在「用例库」中新建用例并运行</td></tr>
                  ) : batches.map((b) => (
                    <tr key={b.batch_id} className="cursor-pointer" onClick={() => navigate(`/audit/evaluation/detail?batch_id=${b.batch_id}`)}>
                      <td>
                        <div className="flex items-center gap-2">
                          <Badge variant="outline" className="text-[10px] uppercase font-bold">{b.target_type}</Badge>
                          <span className="font-mono text-xs text-slate-600">{(b.target_id || '').slice(0, 16)}</span>
                        </div>
                      </td>
                      <td><span className="text-xs font-bold text-slate-700">{b.total}</span></td>
                      <td>
                        <div className="flex items-center justify-center gap-2">
                          <div className="w-20 h-1.5 bg-slate-100 rounded-full overflow-hidden">
                            <div className={cn('h-full rounded-full', b.pass_rate === 1 ? 'bg-emerald-500' : b.pass_rate >= 0.6 ? 'bg-amber-500' : 'bg-rose-500')} style={{ width: `${(b.pass_rate || 0) * 100}%` }} />
                          </div>
                          <span className={cn('text-xs font-black tabular-nums', b.pass_rate === 1 ? 'text-emerald-600' : b.pass_rate >= 0.6 ? 'text-amber-600' : 'text-rose-600')}>
                            {b.passed}/{b.total}
                          </span>
                        </div>
                      </td>
                      <td><span className="text-xs text-slate-500 tabular-nums">{new Date(b.created_at).toLocaleString()}</span></td>
                      <td className="text-right"><Button variant="ghost" size="sm" className="h-8 text-xs font-bold text-blue-600 gap-1">查看详情 <ArrowRight size={13} /></Button></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </TabsContent>

          <TabsContent value="cases" className="space-y-5">
            {loading ? (
              <div className="flex justify-center py-16"><Loader2 className="animate-spin text-slate-300" size={24} /></div>
            ) : Object.keys(groups).length === 0 ? (
              <div className="text-center py-16 text-slate-300 text-sm">暂无用例，点击右上角新建</div>
            ) : Object.entries(groups).map(([key, groupCases]) => {
              const [targetType, targetId] = key.split(':');
              return (
                <Card key={key} className="border-slate-200 shadow-sm overflow-hidden">
                  <div className="px-5 py-3 bg-slate-50/70 border-b border-slate-100 flex items-center justify-between">
                    <div className="flex items-center gap-2">
                      <Badge variant="outline" className="text-[10px] uppercase font-bold">{targetType}</Badge>
                      <span className="font-mono text-xs text-slate-600">{targetId}</span>
                      <span className="text-[10px] text-slate-400">· {groupCases.length} 条用例</span>
                    </div>
                    <Button size="sm" disabled={runningId === key} className="h-8 bg-slate-900 hover:bg-black text-white text-[11px] font-bold gap-1.5"
                      onClick={() => runAllForTarget(targetType, targetId)}>
                      {runningId === key ? <Loader2 size={12} className="animate-spin" /> : <Play size={12} />}整组运行
                    </Button>
                  </div>
                  <CardContent className="p-0 divide-y divide-slate-50">
                    {groupCases.map((c) => (
                      <div key={c.id} className="px-5 py-3 flex items-center justify-between hover:bg-slate-50/50 transition-colors group">
                        <div className="min-w-0 flex-1">
                          <div className="text-sm font-bold text-slate-800 truncate">{c.name}</div>
                          <div className="text-[11px] text-slate-400 truncate mt-0.5">输入：{c.test_input}</div>
                          {c.expected_keywords?.length > 0 && (
                            <div className="flex flex-wrap gap-1 mt-1.5">
                              {c.expected_keywords.map((kw: string, i: number) => (
                                <Badge key={i} variant="outline" className="text-[9px] bg-emerald-50 text-emerald-600 border-emerald-100 px-1.5 py-0">{kw}</Badge>
                              ))}
                            </div>
                          )}
                        </div>
                        <div className="flex items-center gap-1 shrink-0 ml-3">
                          <Button size="sm" variant="outline" disabled={runningId === c.id} className="h-8 text-[11px] gap-1" onClick={() => runSingle(c)}>
                            {runningId === c.id ? <Loader2 size={11} className="animate-spin" /> : <Play size={11} />}运行
                          </Button>
                          <Button variant="ghost" size="icon" className="h-8 w-8 text-slate-300 hover:text-rose-500" onClick={() => removeCase(c.id)}><Trash2 size={14} /></Button>
                        </div>
                      </div>
                    ))}
                  </CardContent>
                </Card>
              );
            })}
          </TabsContent>
        </div>
      </Tabs>
    </div>
  );
};

export default EvaluationCenter;
