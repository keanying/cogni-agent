
import React, { useEffect, useState } from 'react';
import { ArrowLeft, CheckCircle2, XCircle, Clock, Zap, Loader2, ClipboardCheck } from 'lucide-react';
import { Button } from '../../components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '../../components/ui/card';
import { Badge } from '../../components/ui/badge';
import { cn } from '../../lib/utils';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { toast } from 'sonner';
import * as api from '../../lib/api';

const EvaluationDetail: React.FC = () => {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const batchId = params.get('batch_id');
  const [report, setReport] = useState<any>(null);
  const [caseMap, setCaseMap] = useState<Record<string, any>>({});
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!batchId) { setLoading(false); return; }
    (async () => {
      try {
        const [r, cs] = await Promise.all([api.getEvalRunDetail(batchId), api.listEvalCases()]);
        setReport(r);
        setCaseMap(Object.fromEntries(cs.items.map((c: any) => [c.id, c])));
      } catch (e: any) { toast.error(e.message || '加载报告失败'); }
      finally { setLoading(false); }
    })();
  }, [batchId]);

  if (loading) return <div className="flex justify-center py-20"><Loader2 className="animate-spin text-slate-300" size={28} /></div>;
  if (!report) return <div className="text-center py-20 text-slate-400">未找到该运行报告</div>;

  const items: any[] = report.items || [];
  const passed = items.filter((i) => i.passed).length;
  const passRate = items.length ? passed / items.length : 0;
  const totalTokens = items.reduce((s, i) => s + (i.prompt_tokens || 0) + (i.completion_tokens || 0), 0);
  const avgLatency = items.length ? Math.round(items.reduce((s, i) => s + (i.latency_ms || 0), 0) / items.length) : 0;

  const kpis = [
    { label: '通过率', value: `${(passRate * 100).toFixed(0)}%`, color: passRate === 1 ? 'text-emerald-600' : passRate >= 0.6 ? 'text-amber-600' : 'text-rose-600', icon: ClipboardCheck },
    { label: '通过 / 总数', value: `${passed} / ${items.length}`, color: 'text-slate-900', icon: CheckCircle2 },
    { label: '平均耗时', value: `${avgLatency}ms`, color: 'text-blue-600', icon: Clock },
    { label: '累计 Tokens', value: totalTokens.toLocaleString(), color: 'text-indigo-600', icon: Zap },
  ];

  return (
    <div className="max-w-5xl mx-auto space-y-6 animate-in fade-in duration-500">
      <div className="flex items-center gap-4">
        <Button variant="ghost" size="icon" onClick={() => navigate('/audit/evaluation')} className="rounded-full"><ArrowLeft className="h-5 w-5" /></Button>
        <div>
          <h1 className="text-2xl font-bold text-slate-900 tracking-tight">运行报告详情</h1>
          <p className="text-slate-500 text-xs font-mono mt-0.5">{batchId}</p>
        </div>
      </div>

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        {kpis.map((k) => (
          <Card key={k.label} className="border-slate-200 shadow-sm">
            <CardContent className="p-4">
              <div className="flex items-center justify-between mb-1.5">
                <span className="text-[11px] font-medium text-slate-500">{k.label}</span>
                <k.icon size={13} className={k.color} style={{ opacity: 0.7 }} />
              </div>
              <div className={cn('text-2xl font-bold tabular-nums', k.color)}>{k.value}</div>
            </CardContent>
          </Card>
        ))}
      </div>

      <Card className="border-slate-200 shadow-sm overflow-hidden">
        <CardHeader className="border-b bg-slate-50/50 py-4 px-6">
          <CardTitle className="text-sm font-black uppercase tracking-widest text-slate-600">用例逐条结果</CardTitle>
        </CardHeader>
        <CardContent className="p-0 divide-y divide-slate-50">
          {items.map((r, i) => {
            const c = caseMap[r.case_id];
            return (
              <div key={i} className="p-5 space-y-3">
                <div className="flex items-start justify-between gap-4">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2 mb-1">
                      {r.passed
                        ? <Badge className="bg-emerald-50 text-emerald-600 border-none text-[10px] font-black gap-1"><CheckCircle2 size={10} />通过</Badge>
                        : <Badge className="bg-rose-50 text-rose-600 border-none text-[10px] font-black gap-1"><XCircle size={10} />未通过</Badge>}
                      <span className="text-sm font-bold text-slate-800 truncate">{c?.name || r.case_id}</span>
                    </div>
                    {c?.test_input && <div className="text-[11px] text-slate-400">输入：{c.test_input}</div>}
                  </div>
                  <div className="text-right shrink-0">
                    <div className="text-[10px] text-slate-400 font-mono">{r.latency_ms}ms</div>
                    <div className="text-[10px] text-slate-400 font-mono">{(r.prompt_tokens || 0) + (r.completion_tokens || 0)} tokens</div>
                  </div>
                </div>

                {c?.expected_keywords?.length > 0 && (
                  <div className="flex flex-wrap gap-1">
                    <span className="text-[10px] text-slate-400 mr-1">期望关键词：</span>
                    {c.expected_keywords.map((kw: string, ki: number) => {
                      const hit = (r.actual_output || '').includes(kw);
                      return (
                        <Badge key={ki} variant="outline" className={cn('text-[9px] px-1.5 py-0', hit ? 'bg-emerald-50 text-emerald-600 border-emerald-100' : 'bg-slate-50 text-slate-400 border-slate-200')}>
                          {kw}{hit ? ' ✓' : ''}
                        </Badge>
                      );
                    })}
                  </div>
                )}

                <div className={cn('rounded-xl p-3.5 text-xs leading-relaxed', r.error ? 'bg-rose-50 text-rose-700 border border-rose-100' : 'bg-slate-50 text-slate-700 border border-slate-100')}>
                  {r.error ? <span className="font-medium">执行错误：{r.error}</span> : (r.actual_output || <span className="text-slate-300">（无输出）</span>)}
                </div>
              </div>
            );
          })}
          {items.length === 0 && <div className="py-16 text-center text-slate-300">该报告没有用例结果</div>}
        </CardContent>
      </Card>
    </div>
  );
};

export default EvaluationDetail;
