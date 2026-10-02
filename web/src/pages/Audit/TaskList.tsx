
import React, { useEffect, useState } from 'react';
import {
  Search, Activity, CheckCircle2, XCircle, AlertCircle, StopCircle, Eye, User, Zap,
  Cpu, Box, Layers, Terminal, Loader2, RefreshCw, Clock, ShieldAlert, X,
} from 'lucide-react';
import { Card, CardContent } from '../../components/ui/card';
import { Button } from '../../components/ui/button';
import { Badge } from '../../components/ui/badge';
import { Input } from '../../components/ui/input';
import { cn } from '../../lib/utils';
import { toast } from 'sonner';
import * as api from '../../lib/api';
import { useAppStore } from '../../store/useAppStore';
import { confirmDialog } from '../../components/ui/confirm';

const STATUS_META: Record<string, { label: string; cls: string; icon: any }> = {
  CREATED: { label: '已创建', cls: 'bg-slate-50 text-slate-500 border-slate-200', icon: Clock },
  QUEUED: { label: '排队中', cls: 'bg-slate-50 text-slate-500 border-slate-200', icon: Clock },
  RUNNING: { label: '运行中', cls: 'bg-blue-50 text-blue-600 border-blue-100', icon: Activity },
  WAITING_HUMAN: { label: '待审批', cls: 'bg-amber-50 text-amber-600 border-amber-100', icon: ShieldAlert },
  COMPLETED: { label: '已完成', cls: 'bg-emerald-50 text-emerald-600 border-emerald-100', icon: CheckCircle2 },
  FAILED: { label: '已失败', cls: 'bg-rose-50 text-rose-600 border-rose-100', icon: XCircle },
  CANCELLED: { label: '已取消', cls: 'bg-slate-100 text-slate-500 border-slate-200', icon: AlertCircle },
};

const TYPE_ICON: Record<string, any> = { skill: Zap, agent: Layers, tool: Terminal, mcp: Box };

const TaskList: React.FC = () => {
  const { me } = useAppStore();
  const [tasks, setTasks] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [statusFilter, setStatusFilter] = useState('');
  const [keyword, setKeyword] = useState('');
  const [detail, setDetail] = useState<any>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  const canStop = me?.role === 'super_admin' || me?.role === 'group_admin';

  const load = async () => {
    setLoading(true);
    try { setTasks((await api.listTasks(statusFilter ? { status: statusFilter } : undefined)).items); }
    catch (e: any) { toast.error(e.message || '加载任务列表失败'); }
    finally { setLoading(false); }
  };
  useEffect(() => { load(); }, [statusFilter]);

  const openDetail = async (id: string) => {
    setDetailLoading(true);
    try { setDetail(await api.getTask(id)); }
    catch (e: any) { toast.error(e.message || '加载详情失败'); }
    finally { setDetailLoading(false); }
  };

  const handleCancel = async (id: string) => {
    if (!await confirmDialog({ title: '停止该任务',
      message: '任务会在当前步骤结束后停止,已产生的部分结果保留;停止后不能恢复执行。',
      confirmLabel: '停止', danger: true })) return;
    try { await api.cancelTask(id); toast.success('任务已停止'); setDetail(null); load(); }
    catch (e: any) { toast.error(e.message || '停止失败'); }
  };

  const filtered = tasks.filter((t) => !keyword || t.id.includes(keyword) || (t.executor_id || '').includes(keyword));
  const counts = tasks.reduce((acc: Record<string, number>, t) => { acc[t.status] = (acc[t.status] || 0) + 1; return acc; }, {});

  const kpis = [
    { label: '当前列表任务数', value: tasks.length, color: 'text-slate-900', icon: Activity },
    { label: '运行中', value: counts.RUNNING || 0, color: 'text-blue-600', icon: Activity },
    { label: '已完成', value: counts.COMPLETED || 0, color: 'text-emerald-600', icon: CheckCircle2 },
    { label: '失败 / 取消', value: (counts.FAILED || 0) + (counts.CANCELLED || 0), color: 'text-rose-600', icon: XCircle },
  ];

  return (
    <div className="space-y-6 animate-in fade-in slide-in-from-bottom-4 duration-500">
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-slate-900 tracking-tight">任务中心</h1>
          <p className="text-slate-500 text-sm mt-1">记录全系统任务执行过程、状态机进度及资源消耗明细。</p>
        </div>
        <Button variant="outline" className="h-10 rounded-xl border-slate-200 gap-2 font-medium" onClick={load}>
          <RefreshCw size={15} />刷新
        </Button>
      </div>

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        {kpis.map((k) => (
          <Card key={k.label} className="border-slate-200 shadow-sm">
            <CardContent className="p-4">
              <div className="flex items-center justify-between mb-1.5">
                <span className="text-[11px] font-medium text-slate-500">{k.label}</span>
                <k.icon size={13} className={k.color} style={{ opacity: 0.7 }} />
              </div>
              <div className={cn('text-2xl font-bold tabular-nums', k.color)}>{loading ? '…' : k.value}</div>
            </CardContent>
          </Card>
        ))}
      </div>

      <Card className="border-slate-200 shadow-sm overflow-hidden bg-white/50 backdrop-blur-sm">
        <CardContent className="p-4">
          <div className="flex flex-col md:flex-row gap-3">
            <div className="relative flex-1">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" size={16} />
              <Input value={keyword} onChange={(e) => setKeyword(e.target.value)} placeholder="搜索任务 ID 或执行目标 ID..." className="pl-10 h-11 bg-white border-slate-200 rounded-xl focus-visible:ring-blue-500" />
            </div>
            <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}
              className="h-11 px-4 rounded-xl border border-slate-200 bg-white text-sm outline-none font-medium text-slate-600">
              <option value="">全部状态</option>
              {Object.keys(STATUS_META).map((s) => <option key={s} value={s}>{STATUS_META[s].label}</option>)}
            </select>
          </div>
        </CardContent>
      </Card>

      <div className="enterprise-table-container overflow-x-auto">
        <table className="enterprise-table">
          <thead>
            <tr>
              <th>任务 ID</th><th>执行目标</th><th>来源</th><th>景区</th>
              <th className="text-center">状态</th><th>创建时间</th><th className="text-right">操作</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan={7} className="py-16 text-center"><Loader2 className="animate-spin text-slate-300 mx-auto" size={24} /></td></tr>
            ) : filtered.length === 0 ? (
              <tr><td colSpan={7} className="py-16 text-center text-slate-300">暂无任务</td></tr>
            ) : filtered.map((task) => {
              const meta = STATUS_META[task.status] || { label: task.status, cls: 'bg-slate-50 text-slate-400', icon: AlertCircle };
              const TypeIcon = TYPE_ICON[task.executor_type] || Cpu;
              return (
                <tr key={task.id} className="group">
                  <td><span className="font-mono text-xs text-slate-500">{task.id.slice(0, 12)}</span></td>
                  <td>
                    <div className="flex items-center gap-2">
                      <TypeIcon size={14} className="text-slate-400" />
                      <div className="flex flex-col">
                        <span className="text-xs font-bold text-slate-700">{task.executor_type}</span>
                        <span className="text-[10px] font-mono text-slate-400">{(task.executor_id || '-').slice(0, 16)}</span>
                      </div>
                    </div>
                  </td>
                  <td><span className="text-xs text-slate-500">{task.source}</span></td>
                  <td><span className="text-xs text-slate-500">{task.scenic_id || '集团'}</span></td>
                  <td>
                    <Badge variant="outline" className={cn('rounded-full font-black text-[9px] uppercase tracking-widest gap-1 flex items-center w-fit mx-auto px-2 py-0.5', meta.cls)}>
                      <meta.icon size={11} className={task.status === 'RUNNING' ? 'animate-spin' : ''} />{meta.label}
                    </Badge>
                  </td>
                  <td><span className="text-xs text-slate-500 tabular-nums">{new Date(task.created_at).toLocaleString()}</span></td>
                  <td className="text-right">
                    <div className="flex justify-end gap-1">
                      <Button variant="ghost" size="icon" className="h-8 w-8 rounded-lg text-slate-400 hover:text-blue-600" onClick={() => openDetail(task.id)}>
                        <Eye size={16} />
                      </Button>
                      {task.status === 'RUNNING' && canStop && (
                        <Button variant="ghost" size="icon" className="h-8 w-8 rounded-lg text-rose-400 hover:text-rose-600 hover:bg-rose-50" onClick={() => handleCancel(task.id)}>
                          <StopCircle size={16} />
                        </Button>
                      )}
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {(detail || detailLoading) && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm p-4" onClick={() => setDetail(null)}>
          <Card className="w-full max-w-2xl bg-white shadow-l3 rounded-2xl max-h-[85vh] flex flex-col" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between px-6 py-4 border-b border-slate-100">
              <h3 className="text-base font-bold text-slate-800">任务详情</h3>
              <button className="h-8 w-8 rounded-full hover:bg-slate-100 flex items-center justify-center text-slate-400" onClick={() => setDetail(null)}><X size={16} /></button>
            </div>
            <div className="p-6 overflow-y-auto custom-scrollbar space-y-4">
              {detailLoading || !detail ? <div className="py-10 flex justify-center"><Loader2 className="animate-spin text-slate-300" size={22} /></div> : (
                <>
                  <div className="grid grid-cols-2 gap-3 text-xs">
                    <div className="p-3 rounded-xl bg-slate-50 border border-slate-100"><div className="text-[10px] text-slate-400 uppercase font-bold mb-1">Task ID</div><div className="font-mono text-slate-700 break-all">{detail.id}</div></div>
                    <div className="p-3 rounded-xl bg-slate-50 border border-slate-100"><div className="text-[10px] text-slate-400 uppercase font-bold mb-1">状态</div>
                      <Badge variant="outline" className={cn('text-[10px]', (STATUS_META[detail.status] || {}).cls)}>{(STATUS_META[detail.status] || {}).label || detail.status}</Badge>
                    </div>
                  </div>
                  <div>
                    <div className="text-[10px] text-slate-400 uppercase font-bold mb-2">Payload（入参快照）</div>
                    <pre className="bg-slate-900 text-emerald-400 rounded-xl p-4 text-[11px] font-mono overflow-x-auto max-h-52">{JSON.stringify(detail.payload, null, 2)}</pre>
                  </div>
                  {detail.result && (
                    <div>
                      <div className="text-[10px] text-slate-400 uppercase font-bold mb-2">Result（执行结果）</div>
                      <pre className="bg-slate-900 text-blue-400 rounded-xl p-4 text-[11px] font-mono overflow-x-auto max-h-52">{JSON.stringify(detail.result, null, 2)}</pre>
                    </div>
                  )}
                  {detail.status === 'RUNNING' && canStop && (
                    <Button variant="outline" className="w-full border-rose-200 text-rose-600 hover:bg-rose-50 rounded-xl gap-2" onClick={() => handleCancel(detail.id)}>
                      <StopCircle size={15} />停止该任务
                    </Button>
                  )}
                </>
              )}
            </div>
          </Card>
        </div>
      )}
    </div>
  );
};

export default TaskList;
