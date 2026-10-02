
import React, { useEffect, useState } from 'react';
import {
  LayoutDashboard, Zap, TrendingUp, Clock, AlertCircle, CheckCircle2, Loader2,
  ArrowRight, ShieldAlert, Calendar, Eye, XCircle
} from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '../components/ui/card';
import { Button } from '../components/ui/button';
import { Badge } from '../components/ui/badge';
import { cn } from '../lib/utils';
import { useNavigate } from 'react-router-dom';
import * as api from '../lib/api';
import { toast } from 'sonner';

const STATUS_META: Record<string, { label: string; color: string; icon: any }> = {
  COMPLETED: { label: '已完成', color: 'text-emerald-500', icon: CheckCircle2 },
  FAILED: { label: '已失败', color: 'text-rose-500', icon: AlertCircle },
  RUNNING: { label: '运行中', color: 'text-[#1677ff]', icon: Loader2 },
  QUEUED: { label: '排队中', color: 'text-slate-400', icon: Clock },
  WAITING_HUMAN: { label: '待审批', color: 'text-amber-500', icon: ShieldAlert },
  CANCELLED: { label: '已取消', color: 'text-slate-400', icon: XCircle },
};

const Dashboard: React.FC = () => {
  const navigate = useNavigate();
  const [summary, setSummary] = useState<any>(null);
  const [tasks, setTasks] = useState<any[]>([]);
  const [approvals, setApprovals] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedApproval, setSelectedApproval] = useState<any>(null);
  const [approvalDetail, setApprovalDetail] = useState<any>(null);
  const [opinion, setOpinion] = useState('');

  const load = async () => {
    setLoading(true);
    try {
      const [s, t, a] = await Promise.all([
        api.dashboardSummary(7),
        api.dashboardRecentTasks(10),
        api.listApprovals('pending'),
      ]);
      setSummary(s); setTasks(t.items); setApprovals(a.items);
    } catch (e: any) {
      toast.error(e.message || '加载工作台数据失败');
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => { load(); }, []);

  const openApprovalDetail = async (item: any) => {
    setSelectedApproval(item);
    setOpinion('');
    try { setApprovalDetail(await api.getApproval(item.id)); } catch { setApprovalDetail(null); }
  };

  const handleResolve = async (approved: boolean) => {
    if (!approved && !opinion.trim()) { toast.error('驳回必须填写审批意见'); return; }
    try {
      await api.resolveApproval(selectedApproval.id, approved, opinion);
      toast.success(approved ? '已通过，任务将从断点恢复执行' : '已驳回，任务将被取消');
      setSelectedApproval(null);
      load();
    } catch (e: any) {
      toast.error(e.message || '操作失败');
    }
  };

  const stats = [
    { label: '总任务数', value: summary?.total_tasks ?? '-', icon: LayoutDashboard, color: 'text-blue-600', bg: 'bg-blue-50', border: 'border-blue-100' },
    { label: `近${summary?.period_days ?? 7}天成功率`, value: summary?.success_rate != null ? `${(summary.success_rate * 100).toFixed(1)}%` : '-', icon: CheckCircle2, color: 'text-emerald-600', bg: 'bg-emerald-50', border: 'border-emerald-100' },
    { label: 'Token 用量(近7天)', value: summary ? (summary.token_usage.prompt_tokens + summary.token_usage.completion_tokens).toLocaleString() : '-', icon: Zap, color: 'text-indigo-600', bg: 'bg-indigo-50', border: 'border-indigo-100' },
    { label: '待审批', value: summary?.pending_approvals ?? '-', icon: ShieldAlert, color: 'text-amber-600', bg: 'bg-amber-50', border: 'border-amber-100' },
  ];

  return (
    <div className="space-y-6 animate-fade-in-up">
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-slate-900 tracking-tight">工作台</h1>
          <p className="text-slate-500 text-sm mt-1">欢迎回来，这是您的业务概览。</p>
        </div>
        <div className="flex items-center gap-3">
          <Button variant="outline" className="h-10 rounded-xl border-slate-200 bg-white hover:bg-slate-50 text-slate-600 gap-2 shadow-sm">
            <Calendar size={16} />
            {new Date().toLocaleDateString('zh-CN', { year: 'numeric', month: 'long', day: 'numeric' })}
          </Button>
          <Button className="h-10 rounded-xl bg-[#1677ff] hover:bg-[#1677ff]/90 text-white gap-2 shadow-lg shadow-[#1677ff]/20 active-scale" onClick={() => navigate('/console')}>
            <Zap size={16} />
            发起新任务
          </Button>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
        {stats.map((stat, i) => (
          <Card key={i} className={cn('border shadow-l1 hover-lift', stat.border)}>
            <CardContent className="p-6">
              <div className="flex items-start justify-between">
                <div>
                  <p className="text-sm font-medium text-slate-500">{stat.label}</p>
                  <h3 className="text-2xl font-bold text-slate-900 mt-2 tabular-nums">{loading ? '…' : stat.value}</h3>
                </div>
                <div className={cn('p-2.5 rounded-xl', stat.bg, stat.color)}>
                  <stat.icon size={20} />
                </div>
              </div>
            </CardContent>
          </Card>
        ))}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <Card className="lg:col-span-2 border-slate-200 shadow-l1 overflow-hidden">
          <CardHeader className="flex flex-row items-center justify-between pb-2 border-b border-slate-50">
            <div>
              <CardTitle className="text-base font-bold text-slate-800">任务状态分布</CardTitle>
              <CardDescription className="text-xs">最近 {tasks.length} 条任务按状态统计</CardDescription>
            </div>
            <Badge variant="outline" className="bg-blue-50 text-blue-600 border-blue-100 font-normal">实时数据</Badge>
          </CardHeader>
          <CardContent className="pt-6">
            {loading ? (
              <div className="h-[220px] flex items-center justify-center text-slate-300 text-sm">加载中...</div>
            ) : tasks.length === 0 ? (
              <div className="h-[220px] flex items-center justify-center text-slate-300 text-sm">暂无任务数据</div>
            ) : (
              <div className="space-y-3">
                {Object.entries(tasks.reduce((acc: Record<string, number>, t) => { acc[t.status] = (acc[t.status] || 0) + 1; return acc; }, {})).map(([status, count]) => {
                  const meta = STATUS_META[status] || { label: status, color: 'text-slate-400', icon: Clock };
                  const pct = (count / tasks.length) * 100;
                  return (
                    <div key={status} className="flex items-center gap-3">
                      <div className="w-24 flex items-center gap-1.5 text-xs font-medium text-slate-600 shrink-0">
                        <meta.icon size={12} className={meta.color} />{meta.label}
                      </div>
                      <div className="flex-1 h-2 bg-slate-100 rounded-full overflow-hidden">
                        <div className="h-full bg-[#1677ff] rounded-full transition-all" style={{ width: `${pct}%` }} />
                      </div>
                      <span className="w-8 text-right text-xs font-mono text-slate-400">{count}</span>
                    </div>
                  );
                })}
              </div>
            )}
          </CardContent>
        </Card>

        <Card className="border-slate-200 shadow-l1 flex flex-col">
          <CardHeader className="flex flex-row items-center justify-between border-b border-slate-50">
            <CardTitle className="text-base font-bold text-slate-800 flex items-center gap-2">
              <ShieldAlert size={18} className="text-amber-500" />
              待处理审批
            </CardTitle>
            <Button variant="ghost" size="sm" className="text-[10px] text-slate-400 hover:text-[#1677ff]" onClick={() => navigate('/audit/approval')}>
              查看全部
            </Button>
          </CardHeader>
          <CardContent className="flex-1 p-0 overflow-y-auto custom-scrollbar max-h-[400px]">
            <div className="divide-y divide-slate-50">
              {approvals.length === 0 && <div className="p-6 text-sm text-slate-300 text-center">暂无待处理审批</div>}
              {approvals.map((item) => (
                <div key={item.id} className="p-4 hover:bg-slate-50/80 transition-colors group cursor-pointer" onClick={() => openApprovalDetail(item)}>
                  <div className="flex items-start justify-between mb-2">
                    <Badge className={cn('text-[10px] px-1.5 py-0 h-4 border-none font-normal',
                      item.risk_level === 'high' ? 'bg-rose-50 text-rose-600' : item.risk_level === 'medium' ? 'bg-amber-50 text-amber-600' : 'bg-blue-50 text-blue-600')}>
                      {item.risk_level.toUpperCase()}
                    </Badge>
                    <span className="text-[10px] text-slate-400">{new Date(item.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</span>
                  </div>
                  <h4 className="text-sm font-bold text-slate-700 mb-1 group-hover:text-[#1677ff] transition-colors">{item.type}</h4>
                  <div className="flex items-center justify-between">
                    <span className="text-xs text-slate-400">发起人: {item.applicant_user_id}</span>
                    <ArrowRight size={14} className="text-slate-300 group-hover:text-[#1677ff] transition-transform group-hover:translate-x-1" />
                  </div>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      </div>

      <Card className="border-slate-200 shadow-l1">
        <CardHeader className="flex flex-row items-center justify-between border-b border-slate-50">
          <div>
            <CardTitle className="text-base font-bold text-slate-800">最近任务列表</CardTitle>
            <CardDescription className="text-xs">实时展示系统正在运行或已完成的任务记录</CardDescription>
          </div>
          <Button variant="outline" size="sm" className="rounded-xl border-slate-200 text-slate-500 gap-2 h-9 shadow-sm" onClick={() => navigate('/audit/tasks')}>
            <Eye size={14} />
            查看详细审计
          </Button>
        </CardHeader>
        <CardContent className="p-0">
          <div className="overflow-x-auto no-scrollbar">
            <table className="w-full text-sm text-left">
              <thead>
                <tr className="bg-slate-50 text-slate-500 font-medium">
                  <th className="px-6 py-4">任务 ID</th>
                  <th className="px-6 py-4">执行目标</th>
                  <th className="px-6 py-4">来源</th>
                  <th className="px-6 py-4">状态</th>
                  <th className="px-6 py-4">创建时间</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {tasks.map((task) => {
                  const meta = STATUS_META[task.status] || { label: task.status, color: 'text-slate-400', icon: Clock };
                  return (
                    <tr key={task.id} className="hover:bg-slate-50/50 transition-colors">
                      <td className="px-6 py-4 font-mono text-xs text-slate-500">{task.id.slice(0, 8)}</td>
                      <td className="px-6 py-4">
                        <Badge variant="outline" className="bg-white border-slate-200 text-slate-500 font-normal mr-1.5">{task.executor_type}</Badge>
                        <span className="text-xs text-slate-400 font-mono">{task.executor_id?.slice(0, 12)}</span>
                      </td>
                      <td className="px-6 py-4 text-slate-500">{task.source}</td>
                      <td className="px-6 py-4">
                        <div className="flex items-center gap-2">
                          <meta.icon className={cn('h-3 w-3', meta.color, task.status === 'RUNNING' && 'animate-spin')} />
                          <span className={meta.color}>{meta.label}</span>
                        </div>
                      </td>
                      <td className="px-6 py-4 tabular-nums text-slate-500">{new Date(task.created_at).toLocaleString()}</td>
                    </tr>
                  );
                })}
                {tasks.length === 0 && !loading && (
                  <tr><td colSpan={5} className="px-6 py-10 text-center text-slate-300">暂无任务</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </CardContent>
      </Card>

      {selectedApproval && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm p-4">
          <Card className="w-full max-w-lg bg-white border-slate-200 shadow-l3 overflow-hidden rounded-2xl">
            <CardHeader className="bg-slate-50/80 border-b border-slate-100">
              <div className="flex items-center justify-between">
                <CardTitle className="text-lg font-bold text-slate-800">审批详情</CardTitle>
                <button className="h-8 w-8 rounded-full hover:bg-slate-100 flex items-center justify-center text-slate-400" onClick={() => setSelectedApproval(null)}>✕</button>
              </div>
            </CardHeader>
            <CardContent className="p-6 space-y-6">
              <div className="space-y-4">
                <div className="flex items-center justify-between">
                  <span className="text-xs text-slate-400 uppercase tracking-wider font-bold">审批类型</span>
                  <Badge className="bg-blue-50 text-blue-600 border-none">{selectedApproval.type}</Badge>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-xs text-slate-400 uppercase tracking-wider font-bold">发起人</span>
                  <span className="text-sm font-medium text-slate-700">{selectedApproval.applicant_user_id}</span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-xs text-slate-400 uppercase tracking-wider font-bold">风险等级</span>
                  <Badge className={cn('border-none', selectedApproval.risk_level === 'high' ? 'bg-rose-50 text-rose-600' : 'bg-amber-50 text-amber-600')}>
                    {selectedApproval.risk_level.toUpperCase()}
                  </Badge>
                </div>
              </div>

              {approvalDetail?.context && (
                <div className="p-4 bg-slate-50 rounded-xl border border-slate-100">
                  <h5 className="text-xs font-bold text-slate-500 mb-2 uppercase tracking-wider">上下文快照</h5>
                  <pre className="text-xs text-slate-600 whitespace-pre-wrap font-mono">{JSON.stringify(approvalDetail.context, null, 2)}</pre>
                </div>
              )}

              <div className="flex flex-col gap-3">
                <input value={opinion} onChange={(e) => setOpinion(e.target.value)} placeholder="审批意见（驳回必填）"
                  className="w-full h-10 px-3 rounded-lg border border-slate-200 text-sm outline-none focus:ring-2 focus:ring-blue-100" />
                <div className="flex items-center gap-3">
                  <Button className="flex-1 h-11 bg-emerald-600 hover:bg-emerald-700 text-white rounded-xl shadow-lg shadow-emerald-600/20" onClick={() => handleResolve(true)}>
                    通过审批
                  </Button>
                  <Button variant="outline" className="flex-1 h-11 border-rose-200 text-rose-600 hover:bg-rose-50 rounded-xl" onClick={() => handleResolve(false)}>
                    驳回
                  </Button>
                </div>
              </div>
            </CardContent>
          </Card>
        </div>
      )}
    </div>
  );
};

export default Dashboard;
